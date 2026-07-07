"""
find_fuzzy_dupes.py — find cross-source duplicate PAIRS that never share an
exact fingerprint (e.g. Eventim "X at Venue - City, ST" vs a venue-direct
"X @ Venue"), using the same normalize_title_for_matching() + difflib +
prefix-match logic as find_cross_source_match() in db.py.

Read-only. Groups candidates by (city_slug, canonical date) same as the
live matcher, then reports any pair scoring >= TITLE_SIMILARITY_THRESHOLD
that do NOT already share an exact fingerprint (those were already handled
by recompute_fingerprints.py). For each pair, also reports which side has
an image, so you can see at a glance which post to keep.
"""
import sys
sys.path.insert(0, "/opt/openclaw")
import difflib
import re

_NUMBERED_OCCURRENCE_RE = re.compile(
    r"\((?:night|day|part|show|set)\s*\d+\)", re.IGNORECASE
)

_LINEUP_SHOW_RE = re.compile(
    r"\b(?:featuring|feat\.|lineup)\b", re.IGNORECASE
)

def _is_lineup_show(title: str) -> bool:
    """
    True if a title looks like a multi-performer showcase/lineup listing
    (contains "featuring", "feat.", or "lineup"). These recurring-series
    shows (songwriter rounds, tribute nights, cabaret nights) share so
    much generic boilerplate across genuinely DIFFERENT real shows that
    no similarity-ratio or prefix-comparison heuristic reliably tells
    them apart -- confirmed 2026-07-07: "Gay Ole Opry Presents: Lilith
    Feral lineup TBA" (a cabaret night) scored 0.82 against BOTH
    "BACKSTAGE NASHVILLE! ... featuring Aaron Raitiere..." (a songwriter
    showcase) and "The Long Players performing Fleetwood Mac's
    'Rumours' featuring Todd Sharp..." (a tribute band) -- three
    unrelated shows. Rather than chase an ever-more-specific heuristic,
    ANY pair where either side is a lineup-style show is pulled out of
    auto-merge entirely and flagged for manual review instead.
    """
    return bool(_LINEUP_SHOW_RE.search(title or ""))
from collections import defaultdict
from db import (
    normalize_title_for_matching, _is_headliner_prefix_match,
    TITLE_SIMILARITY_THRESHOLD, make_fingerprint, get_connection
)

WP_PREFIX = "wp_"

conn = get_connection()
with conn.cursor() as cur:
    cur.execute(f"""
        SELECT p.ID AS post_id, p.post_title,
            MAX(CASE WHEN pm.meta_key='_EventStartDateUTC' THEN pm.meta_value END) AS start_utc,
            MAX(CASE WHEN pm.meta_key='_EventStartDate'    THEN pm.meta_value END) AS start_local,
            MAX(CASE WHEN pm.meta_key='_openclaw_city'     THEN pm.meta_value END) AS city_slug,
            MAX(CASE WHEN pm.meta_key='_openclaw_source'   THEN pm.meta_value END) AS source_name,
            MAX(CASE WHEN pm.meta_key='_thumbnail_id'      THEN pm.meta_value END) AS thumb_id
        FROM {WP_PREFIX}posts p
        LEFT JOIN {WP_PREFIX}postmeta pm ON pm.post_id = p.ID
        WHERE p.post_type = 'tribe_events' AND p.post_status IN ('publish','draft')
        GROUP BY p.ID, p.post_title
    """)
    rows = cur.fetchall()

# Bucket by (city, canonical date) same as find_cross_source_match()
buckets = defaultdict(list)
for r in rows:
    canonical = (r["start_utc"] or r["start_local"] or "").strip()[:10]
    city = (r["city_slug"] or "").strip().lower()
    if not canonical or not city:
        continue
    r["_canonical_date"] = canonical
    r["_norm_title"] = normalize_title_for_matching(r["post_title"] or "")
    r["_fp"] = make_fingerprint({"title": r["post_title"], "start_utc": r["start_utc"],
                                  "start_local": r["start_local"], "city_slug": r["city_slug"]})
    buckets[(city, canonical)].append(r)

found = 0
for (city, date), posts in buckets.items():
    n = len(posts)
    for i in range(n):
        for j in range(i + 1, n):
            a, b = posts[i], posts[j]
            if a["_fp"] == b["_fp"]:
                continue  # exact match, already handled
            # Fixture/matchup guard -- see merge_fuzzy_dupes.py for full
            # rationale. "Team A vs Team B" titles must never be fuzzy-
            # matched; only exact fingerprint matches are safe for fixtures.
            a_is_fixture = " vs " in f' {a["_norm_title"]} '
            b_is_fixture = " vs " in f' {b["_norm_title"]} '
            if a_is_fixture or b_is_fixture:
                continue

            # Numbered-occurrence guard -- see merge_fuzzy_dupes.py for
            # full rationale. "(Night 2)"/"(Day 1)"/"(Part 2)" etc mark
            # genuinely different real performances, not duplicates.
            if _NUMBERED_OCCURRENCE_RE.search(a["post_title"] or "") or \
               _NUMBERED_OCCURRENCE_RE.search(b["post_title"] or ""):
                continue

            if _is_lineup_show(a["post_title"] or "") or _is_lineup_show(b["post_title"] or ""):
                continue

            ratio = difflib.SequenceMatcher(None, a["_norm_title"], b["_norm_title"]).ratio()
            is_prefix = (_is_headliner_prefix_match(a["_norm_title"], b["_norm_title"]) or
                         _is_headliner_prefix_match(b["_norm_title"], a["_norm_title"]))
            if is_prefix:
                ratio = max(ratio, TITLE_SIMILARITY_THRESHOLD)
            if ratio >= TITLE_SIMILARITY_THRESHOLD:
                found += 1
                a_img = "HAS image" if a["thumb_id"] else "no image"
                b_img = "HAS image" if b["thumb_id"] else "no image"
                print(f"[{ratio:.2f}] ({city}, {date})")
                print(f"    A: id={a['post_id']:<7} src={a['source_name']!s:<15} {a_img:<10} '{a['post_title']}'")
                print(f"    B: id={b['post_id']:<7} src={b['source_name']!s:<15} {b_img:<10} '{b['post_title']}'")
                print()

print(f"Total fuzzy cross-source duplicate pairs found: {found}")
conn.close()
