#!/usr/bin/env python3
"""
fix_fingerprints.py — one-time backfill + dedupe for the missing-fingerprint bug.

Root cause: some existing TEC event posts (across all cities) have no row in
wp_openclaw_fingerprints. Because get_fingerprint_post_id() does a lookup
against that table only, any post missing a row there is invisible to the
dedup check -- every new scrape re-inserts it as if it were brand new.

This script:
  1. Walks every published/draft tribe_events post.
  2. Computes its fingerprint the SAME way db.py's make_fingerprint() does
     (title normalized | date-only | city_slug), using its OWN stored
     _EventStartDateUTC/_EventStartDate and the city term/tag already on
     the post.
  3. For each fingerprint, keeps the EARLIEST post (lowest ID = created
     first = the "real" one events should attach to going forward) and:
       - Inserts a wp_openclaw_fingerprints row pointing fp -> kept post_id
         (for posts that already only have ONE copy, this just closes the
         gap with no merge needed)
       - For any OTHER post(s) sharing that same fingerprint (true dupes),
         deletes them outright (postmeta, tec_events, tec_occurrences,
         term_relationships, the post row itself).

Run with --dry-run first (default). Pass --apply to actually write.
"""

import argparse
import hashlib
import re
import sys

import pymysql

DB_HOST = "localhost"
DB_PORT = 3306
DB_USER = "wpuser"
DB_PASS = "wpDB_pass789"
DB_NAME = "wordpress"
WP_PREFIX = "wp_"

_PROMO_SUFFIX_RE = re.compile(
    r"\s*\|\s*Official.*$|\s*\(SOLD OUT\)\s*$",
    re.IGNORECASE,
)


def normalize_title_for_matching(title: str) -> str:
    cleaned = _PROMO_SUFFIX_RE.sub("", (title or "").strip())
    return cleaned.strip().lower()


def make_fingerprint(title: str, start_utc: str, start_local: str, city_slug: str) -> str:
    canonical_full = (start_utc or start_local or "").strip()
    canonical_date = canonical_full[:10]
    raw = "|".join([
        normalize_title_for_matching(title),
        canonical_date,
        (city_slug or "").strip().lower(),
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def get_connection():
    return pymysql.connect(
        host=DB_HOST, port=DB_PORT, user=DB_USER, password=DB_PASS,
        database=DB_NAME, charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor, autocommit=False,
    )


def fetch_all_events(conn):
    """
    Pull every tribe_events post with its start time meta and city
    (read from the 'city' tag taxonomy used by The Local Radar, which
    matches the city_slug used at scrape time).
    """
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT p.ID AS post_id, p.post_title, p.post_status, p.post_date,
                   MAX(CASE WHEN pm.meta_key='_EventStartDateUTC' THEN pm.meta_value END) AS start_utc,
                   MAX(CASE WHEN pm.meta_key='_EventStartDate'    THEN pm.meta_value END) AS start_local
            FROM {WP_PREFIX}posts p
            LEFT JOIN {WP_PREFIX}postmeta pm ON pm.post_id = p.ID
            WHERE p.post_type = 'tribe_events'
              AND p.post_status IN ('publish','draft')
            GROUP BY p.ID, p.post_title, p.post_status, p.post_date
        """)
        rows = cur.fetchall()

    # City: read from term_relationships -> term_taxonomy -> terms,
    # restricted to the taxonomy used for city tags ('post_tag', filtered
    # to known city slugs since that taxonomy also holds non-city tags).
    KNOWN_CITY_SLUGS = {"memphis", "nashville", "birmingham", "denver"}

    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT tr.object_id AS post_id, t.slug AS slug
            FROM {WP_PREFIX}term_relationships tr
            JOIN {WP_PREFIX}term_taxonomy tt ON tt.term_taxonomy_id = tr.term_taxonomy_id
            JOIN {WP_PREFIX}terms t ON t.term_id = tt.term_id
            WHERE tt.taxonomy = 'post_tag'
        """)
        city_map = {}
        for r in cur.fetchall():
            if r["slug"] in KNOWN_CITY_SLUGS:
                city_map[r["post_id"]] = r["slug"]

    for r in rows:
        r["city_slug"] = city_map.get(r["post_id"], "")
    return rows


def existing_fingerprint_post_ids(conn):
    with conn.cursor() as cur:
        cur.execute(f"SELECT post_id FROM {WP_PREFIX}openclaw_fingerprints")
        return {r["post_id"] for r in cur.fetchall()}


def delete_post_completely(conn, post_id: int):
    with conn.cursor() as cur:
        cur.execute(f"DELETE FROM {WP_PREFIX}postmeta WHERE post_id=%s", (post_id,))
        cur.execute(f"DELETE FROM {WP_PREFIX}tec_events WHERE post_id=%s", (post_id,))
        cur.execute(f"DELETE FROM {WP_PREFIX}tec_occurrences WHERE post_id=%s", (post_id,))
        cur.execute(f"DELETE FROM {WP_PREFIX}term_relationships WHERE object_id=%s", (post_id,))
        cur.execute(f"DELETE FROM {WP_PREFIX}openclaw_fingerprints WHERE post_id=%s", (post_id,))
        cur.execute(f"DELETE FROM {WP_PREFIX}posts WHERE ID=%s", (post_id,))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="Actually write changes. Default is dry-run.")
    args = ap.parse_args()

    conn = get_connection()
    try:
        events = fetch_all_events(conn)
        already_fingerprinted = existing_fingerprint_post_ids(conn)

        print(f"Loaded {len(events)} tribe_events posts.")
        print(f"{len(already_fingerprinted)} already have a fingerprint row.")

        no_city = [e for e in events if not e["city_slug"]]
        if no_city:
            print(f"WARNING: {len(no_city)} posts have no recognized city tag "
                  f"and will be SKIPPED (can't safely fingerprint without city). "
                  f"First few: {[e['post_id'] for e in no_city[:10]]}")

        groups = {}  # fp -> list of event rows
        for e in events:
            if not e["city_slug"]:
                continue
            fp = make_fingerprint(e["post_title"], e["start_utc"], e["start_local"], e["city_slug"])
            groups.setdefault(fp, []).append(e)

        to_insert_fp = []   # (fp, keep_post_id)
        to_delete = []       # post_id list (dupes beyond the kept one)
        dupe_groups = 0

        for fp, rows in groups.items():
            rows_sorted = sorted(rows, key=lambda r: r["post_id"])
            keep = rows_sorted[0]
            dupes = rows_sorted[1:]

            if keep["post_id"] not in already_fingerprinted:
                to_insert_fp.append((fp, keep["post_id"]))

            if dupes:
                dupe_groups += 1
                for d in dupes:
                    to_delete.append(d["post_id"])

        print(f"\nFingerprint rows to BACKFILL (gap-closing, no deletion): {len(to_insert_fp)}")
        print(f"Duplicate groups found: {dupe_groups}")
        print(f"Duplicate posts to DELETE (keeping earliest per group): {len(to_delete)}")

        if dupe_groups:
            print("\nSample duplicate groups (up to 15):")
            shown = 0
            for fp, rows in groups.items():
                if len(rows) > 1 and shown < 15:
                    rows_sorted = sorted(rows, key=lambda r: r["post_id"])
                    titles = [(r["post_id"], r["post_title"], r["city_slug"],
                               r["start_local"] or r["start_utc"]) for r in rows_sorted]
                    print(f"  KEEP {titles[0]}  ->  DELETE {titles[1:]}")
                    shown += 1

        if not args.apply:
            print("\nDRY RUN — no changes written. Re-run with --apply to execute.")
            return

        print("\nAPPLYING changes...")
        with conn.cursor() as cur:
            for fp, post_id in to_insert_fp:
                cur.execute(
                    f"INSERT IGNORE INTO {WP_PREFIX}openclaw_fingerprints (fp, post_id, created) "
                    f"VALUES (%s, %s, NOW())",
                    (fp, post_id)
                )
        conn.commit()
        print(f"Inserted {len(to_insert_fp)} fingerprint rows.")

        deleted = 0
        for post_id in to_delete:
            delete_post_completely(conn, post_id)
            deleted += 1
        conn.commit()
        print(f"Deleted {deleted} duplicate posts.")

        print("\nDone.")

    finally:
        conn.close()


if __name__ == "__main__":
    main()
