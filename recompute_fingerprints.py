#!/usr/bin/env python3
"""
recompute_fingerprints.py — fix version-skew in stored fingerprints.

Root cause (confirmed 2026-06-27): make_fingerprint()'s formula has been
revised multiple times this week (title normalization, date-only
truncation, resolved_times handling). Posts created under an OLDER
version of the formula still have their OLD fingerprint stored in
wp_openclaw_fingerprints / postmeta._openclaw_fp. The live code only
ever LOOKS UP fingerprints computed with the CURRENT formula, so any
post whose stored fingerprint predates the latest formula change is
invisible to get_fingerprint_post_id() -- every new scrape creates a
fresh duplicate for it, even though the post already exists.

This is different from (and a superset of) the missing-fingerprint gap
fixed by fix_fingerprints.py on 2026-06-26 -- that script only handled
posts with NO fingerprint row at all. This script recomputes the
fingerprint for EVERY post using the live make_fingerprint() and
OVERWRITES whatever was stored before, then merges any posts that
turn out to collide under the corrected formula.

Run with --dry-run (default) first. Pass --apply to actually write.
"""

import argparse
import sys

sys.path.insert(0, "/opt/openclaw")

import pymysql

from db import make_fingerprint, get_connection  # use the LIVE function

WP_PREFIX = "wp_"


def fetch_all_events(conn):
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT p.ID AS post_id, p.post_title,
                   MAX(CASE WHEN pm.meta_key='_EventStartDateUTC' THEN pm.meta_value END) AS start_utc,
                   MAX(CASE WHEN pm.meta_key='_EventStartDate'    THEN pm.meta_value END) AS start_local,
                   MAX(CASE WHEN pm.meta_key='_openclaw_city'     THEN pm.meta_value END) AS city_slug,
                   MAX(CASE WHEN pm.meta_key='_openclaw_fp'       THEN pm.meta_value END) AS old_fp
            FROM {WP_PREFIX}posts p
            LEFT JOIN {WP_PREFIX}postmeta pm ON pm.post_id = p.ID
            WHERE p.post_type = 'tribe_events'
              AND p.post_status IN ('publish','draft')
            GROUP BY p.ID, p.post_title
        """)
        return cur.fetchall()


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
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    conn = get_connection()
    try:
        events = fetch_all_events(conn)
        print(f"Loaded {len(events)} tribe_events posts.")

        no_city = [e for e in events if not e["city_slug"]]
        no_date = [e for e in events if not (e["start_utc"] or e["start_local"])]
        if no_city:
            print(f"WARNING: {len(no_city)} posts have no _openclaw_city meta -- SKIPPED.")
        if no_date:
            print(f"WARNING: {len(no_date)} posts have no start date meta -- SKIPPED.")

        groups = {}   # new_fp -> list of rows
        recompute_count = 0

        for e in events:
            if not e["city_slug"] or not (e["start_utc"] or e["start_local"]):
                continue
            event_dict = {"title": e["post_title"], "city_slug": e["city_slug"]}
            resolved = {"start_utc": e["start_utc"] or "", "start_local": e["start_local"] or ""}
            new_fp = make_fingerprint(event_dict, resolved_times=resolved)
            e["new_fp"] = new_fp
            if new_fp != (e["old_fp"] or ""):
                recompute_count += 1
            groups.setdefault(new_fp, []).append(e)

        dupe_groups = {fp: rows for fp, rows in groups.items() if len(rows) > 1}
        to_delete = []
        for fp, rows in dupe_groups.items():
            rows_sorted = sorted(rows, key=lambda r: r["post_id"])
            to_delete.extend(r["post_id"] for r in rows_sorted[1:])

        print(f"\nPosts whose fingerprint CHANGES under the current formula: {recompute_count}")
        print(f"Duplicate groups found under CURRENT formula: {len(dupe_groups)}")
        print(f"Duplicate posts to DELETE (keeping earliest per group): {len(to_delete)}")

        if dupe_groups:
            print("\nSample duplicate groups (up to 15):")
            for i, (fp, rows) in enumerate(dupe_groups.items()):
                if i >= 15:
                    break
                rows_sorted = sorted(rows, key=lambda r: r["post_id"])
                info = [(r["post_id"], r["post_title"], r["old_fp"]) for r in rows_sorted]
                print(f"  KEEP {info[0]}  ->  DELETE {info[1:]}")

        if not args.apply:
            print("\nDRY RUN -- no changes written. Re-run with --apply to execute.")
            return

        print("\nAPPLYING changes...")

        # 1. Delete duplicates FIRST (before rewriting fingerprints, so we
        #    don't try to write two rows for the same new_fp).
        deleted = 0
        for post_id in to_delete:
            delete_post_completely(conn, post_id)
            deleted += 1
        conn.commit()
        print(f"Deleted {deleted} duplicate posts.")

        # 2. Recompute fingerprint for every SURVIVING post (the kept one
        #    in each group, plus every post that was never a dupe).
        surviving_ids = {post_id for fp, rows in groups.items()
                          for post_id in [sorted(rows, key=lambda r: r["post_id"])[0]["post_id"]]}
        # also include all non-dupe posts
        for fp, rows in groups.items():
            if len(rows) == 1:
                surviving_ids.add(rows[0]["post_id"])

        survivors = [e for e in events if e["post_id"] in surviving_ids]

        with conn.cursor() as cur:
            for e in survivors:
                new_fp = e["new_fp"]
                # postmeta: overwrite _openclaw_fp
                cur.execute(
                    f"UPDATE {WP_PREFIX}postmeta SET meta_value=%s "
                    f"WHERE post_id=%s AND meta_key='_openclaw_fp'",
                    (new_fp, e["post_id"])
                )
                if cur.rowcount == 0:
                    cur.execute(
                        f"INSERT INTO {WP_PREFIX}postmeta (post_id, meta_key, meta_value) "
                        f"VALUES (%s, '_openclaw_fp', %s)",
                        (e["post_id"], new_fp)
                    )
                # fingerprints table: delete any old row for this post_id,
                # then insert the corrected one.
                cur.execute(f"DELETE FROM {WP_PREFIX}openclaw_fingerprints WHERE post_id=%s", (e["post_id"],))
                cur.execute(
                    f"INSERT IGNORE INTO {WP_PREFIX}openclaw_fingerprints (fp, post_id, created) "
                    f"VALUES (%s, %s, NOW())",
                    (new_fp, e["post_id"])
                )
        conn.commit()
        print(f"Recomputed fingerprints for {len(survivors)} surviving posts.")
        print("\nDone.")

    finally:
        conn.close()


if __name__ == "__main__":
    main()
