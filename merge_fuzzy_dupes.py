"""
merge_fuzzy_dupes.py — merge cross-source duplicate PAIRS (found by
find_fuzzy_dupes.py's logic) into a single post, field by field, instead
of blindly keeping one and deleting the other.

Field-level merge rules:
  - Ticket URL (_EventURL): winner decided by the SAME AFFILIATE_PRIORITY
    ranking used in update_event() (ticketmaster > eventim > none).
    A post with a lower-priority/no affiliate link never overrides a
    higher-priority one, but if the picked "primary" post has no
    affiliate link and the other one does, the affiliate link is pulled
    IN rather than lost.
  - Image (_thumbnail_id): keep it if primary already has one; otherwise
    pull in the other post's image reference.
  - Description (post_content): keep primary's if non-empty and
    reasonably substantial; otherwise pull in the other's if longer.
  - Primary post chosen as whichever of the pair has the affiliate URL;
    if neither/both do, whichever has the image; final tie-break is
    earliest post_id (most likely to have existing traffic/links to it).

The LOSING post is fully deleted (posts, postmeta, tec_events,
tec_occurrences, term_relationships, openclaw_fingerprints row) same as
the cancelled-event delete path in insert_event(). The winning post's fp
row is left as-is; the losing post's fp (if any) is deleted so it can't
be food for a stale future lookup.

Run with no flags = dry run (prints planned merges, writes nothing).
Run with --apply to execute.
"""
import sys
import argparse
sys.path.insert(0, "/opt/openclaw")
import difflib
import re

_NUMBERED_OCCURRENCE_RE = re.compile(
    r"\((?:night|day|part|show|set)\s*\d+\)", re.IGNORECASE
)
from collections import defaultdict
from db import (
    normalize_title_for_matching, _is_headliner_prefix_match,
    TITLE_SIMILARITY_THRESHOLD, make_fingerprint, get_connection
)

WP_PREFIX = "wp_"

AFFILIATE_PRIORITY = ["ticketmaster", "eventim"]
AFFILIATE_MARKERS = {
    "ticketmaster": lambda u: "aaid=" in u,
    "eventim":      lambda u: ("seeticketsusa.us" in u or
                                "eventim.us" in u or
                                "nts_trk=" in u),
}

def affiliate_rank(url: str):
    url = url or ""
    for i, src in enumerate(AFFILIATE_PRIORITY):
        if AFFILIATE_MARKERS[src](url):
            return i
    return None  # no known affiliate link


def find_pairs(conn):
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT p.ID AS post_id, p.post_title, p.post_content,
                MAX(CASE WHEN pm.meta_key='_EventStartDateUTC' THEN pm.meta_value END) AS start_utc,
                MAX(CASE WHEN pm.meta_key='_EventStartDate'    THEN pm.meta_value END) AS start_local,
                MAX(CASE WHEN pm.meta_key='_openclaw_city'     THEN pm.meta_value END) AS city_slug,
                MAX(CASE WHEN pm.meta_key='_openclaw_source'   THEN pm.meta_value END) AS source_name,
                MAX(CASE WHEN pm.meta_key='_thumbnail_id'      THEN pm.meta_value END) AS thumb_id,
                MAX(CASE WHEN pm.meta_key='_EventURL'          THEN pm.meta_value END) AS ticket_url,
                MAX(CASE WHEN pm.meta_key='_openclaw_fp'       THEN pm.meta_value END) AS fp
            FROM {WP_PREFIX}posts p
            LEFT JOIN {WP_PREFIX}postmeta pm ON pm.post_id = p.ID
            WHERE p.post_type = 'tribe_events' AND p.post_status IN ('publish','draft')
            GROUP BY p.ID, p.post_title, p.post_content
        """)
        rows = cur.fetchall()

    buckets = defaultdict(list)
    for r in rows:
        canonical = (r["start_utc"] or r["start_local"] or "").strip()[:10]
        city = (r["city_slug"] or "").strip().lower()
        if not canonical or not city:
            continue
        r["_norm_title"] = normalize_title_for_matching(r["post_title"] or "")
        r["_fp"] = make_fingerprint({"title": r["post_title"], "start_utc": r["start_utc"],
                                      "start_local": r["start_local"], "city_slug": r["city_slug"]})
        buckets[(city, canonical)].append(r)

    pairs = []
    for (city, date), posts in buckets.items():
        n = len(posts)
        for i in range(n):
            for j in range(i + 1, n):
                a, b = posts[i], posts[j]
                if a["_fp"] == b["_fp"]:
                    continue
                # Fixture/matchup guard: titles like "Team A vs Team B" must
                # never be fuzzy-matched against each other. Confirmed
                # 2026-07-07: "Liverpool FC vs Sunderland" scored 0.86
                # against "Birmingham Legion FC vs Brooklyn FC" -- two
                # completely unrelated real-world games -- because shared
                # boilerplate ("FC", "vs", "Womens Volleyball") inflates
                # character-overlap similarity regardless of which teams
                # are actually playing. Also caught same-tournament
                # different-opponent pairs (Buffs Classic vs Central
                # Arkansas vs. vs Denver Pioneers). Fixture titles are
                # only ever safe to treat as the same event via the EXACT
                # fingerprint path (make_fingerprint()), never via fuzzy
                # similarity here.
                a_is_fixture = " vs " in f' {a["_norm_title"]} '
                b_is_fixture = " vs " in f' {b["_norm_title"]} '
                if a_is_fixture or b_is_fixture:
                    continue

                # Numbered-occurrence guard: "(Night 2)", "(Night 3)",
                # "(Day 1)", "(Part 2)" etc mark genuinely DIFFERENT real
                # performances in a multi-night residency, not duplicate
                # listings of the same show. Confirmed 2026-07-07: Eric
                # Church (Night 2) vs (Night 3), The Avett Brothers
                # (Night 2) vs (Night 3), Tedeschi Trucks Band, and Andrea
                # Bocelli (Night 2) all scored above threshold against
                # either each other or the base title -- would have
                # deleted real, separately-ticketed shows. If EITHER title
                # has a numbered-occurrence marker at all, skip the pair;
                # too risky to try to compare the numbers and allow a
                # "same number" case through, since a stray marker on only
                # one side is itself a sign these are different listings.
                if _NUMBERED_OCCURRENCE_RE.search(a["post_title"] or "") or \
                   _NUMBERED_OCCURRENCE_RE.search(b["post_title"] or ""):
                    continue

                ratio = difflib.SequenceMatcher(None, a["_norm_title"], b["_norm_title"]).ratio()
                is_prefix = (_is_headliner_prefix_match(a["_norm_title"], b["_norm_title"]) or
                             _is_headliner_prefix_match(b["_norm_title"], a["_norm_title"]))
                if is_prefix:
                    ratio = max(ratio, TITLE_SIMILARITY_THRESHOLD)
                if ratio >= TITLE_SIMILARITY_THRESHOLD:
                    pairs.append((ratio, a, b))
    return pairs


def plan_merge(a, b):
    """Decide primary/secondary and the field values the primary should end up with."""
    a_rank = affiliate_rank(a["ticket_url"])
    b_rank = affiliate_rank(b["ticket_url"])

    # Lower rank number = higher priority. None = no affiliate link.
    if a_rank is not None and (b_rank is None or a_rank <= b_rank):
        primary, secondary = a, b
    elif b_rank is not None and (a_rank is None or b_rank < a_rank):
        primary, secondary = b, a
    elif a["thumb_id"] and not b["thumb_id"]:
        primary, secondary = a, b
    elif b["thumb_id"] and not a["thumb_id"]:
        primary, secondary = b, a
    else:
        primary, secondary = (a, b) if a["post_id"] < b["post_id"] else (b, a)

    updates = {}
    # Pull in affiliate URL if primary lacks one but secondary has one
    if affiliate_rank(primary["ticket_url"]) is None and affiliate_rank(secondary["ticket_url"]) is not None:
        updates["_EventURL"] = secondary["ticket_url"]
    # Pull in image if primary lacks one
    if not primary["thumb_id"] and secondary["thumb_id"]:
        updates["_thumbnail_id"] = secondary["thumb_id"]
    # Pull in description if primary's is empty/short and secondary's is longer
    p_len = len((primary["post_content"] or "").strip())
    s_len = len((secondary["post_content"] or "").strip())
    if s_len > p_len:
        updates["post_content"] = secondary["post_content"]

    return primary, secondary, updates


def apply_merge(conn, primary, secondary, updates):
    with conn.cursor() as cur:
        for key, val in updates.items():
            if key == "post_content":
                cur.execute(f"UPDATE {WP_PREFIX}posts SET post_content=%s WHERE ID=%s",
                            (val, primary["post_id"]))
            else:
                cur.execute(f"DELETE FROM {WP_PREFIX}postmeta WHERE post_id=%s AND meta_key=%s",
                            (primary["post_id"], key))
                cur.execute(f"INSERT INTO {WP_PREFIX}postmeta (post_id,meta_key,meta_value) VALUES (%s,%s,%s)",
                            (primary["post_id"], key, val))

        sid = secondary["post_id"]
        cur.execute(f"DELETE FROM {WP_PREFIX}tec_events WHERE post_id=%s", (sid,))
        cur.execute(f"DELETE FROM {WP_PREFIX}tec_occurrences WHERE post_id=%s", (sid,))
        cur.execute(f"DELETE FROM {WP_PREFIX}term_relationships WHERE object_id=%s", (sid,))
        cur.execute(f"DELETE FROM {WP_PREFIX}openclaw_fingerprints WHERE post_id=%s", (sid,))
        cur.execute(f"DELETE FROM {WP_PREFIX}postmeta WHERE post_id=%s", (sid,))
        cur.execute(f"DELETE FROM {WP_PREFIX}posts WHERE ID=%s", (sid,))
    conn.commit()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    conn = get_connection()
    pairs = find_pairs(conn)
    print(f"Fuzzy cross-source duplicate pairs found: {len(pairs)}\n")

    for ratio, a, b in pairs:
        primary, secondary, updates = plan_merge(a, b)
        print(f"[{ratio:.2f}] KEEP {primary['post_id']} '{primary['post_title']}' "
              f"(src={primary['source_name']})")
        print(f"        MERGE-FROM {secondary['post_id']} '{secondary['post_title']}' "
              f"(src={secondary['source_name']}) -- then DELETE it")
        if updates:
            for k in updates:
                print(f"        pulling in: {k}")
        else:
            print(f"        (no fields needed from secondary)")
        print()

        if args.apply:
            apply_merge(conn, primary, secondary, updates)

    if args.apply:
        print(f"APPLIED: merged and deleted {len(pairs)} duplicate posts.")
    else:
        print("DRY RUN -- no changes written. Re-run with --apply to execute.")
    conn.close()


if __name__ == "__main__":
    main()
