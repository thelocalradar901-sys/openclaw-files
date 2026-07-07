"""
check_dupe_images.py — one-off safety check before recompute_fingerprints.py --apply.

Re-derives the same KEEP/DELETE duplicate groups recompute_fingerprints.py
would produce (same-title collisions under the current make_fingerprint()
formula), then checks whether any DELETE-target post has a featured image
that its KEEP-target twin lacks. Read-only -- no writes.
"""
import sys
sys.path.insert(0, "/opt/openclaw")
from collections import defaultdict
from db import make_fingerprint, get_connection

WP_PREFIX = "wp_"

conn = get_connection()
with conn.cursor() as cur:
    cur.execute(f"""
        SELECT p.ID AS post_id, p.post_title,
            MAX(CASE WHEN pm.meta_key='_EventStartDateUTC' THEN pm.meta_value END) AS start_utc,
            MAX(CASE WHEN pm.meta_key='_EventStartDate'    THEN pm.meta_value END) AS start_local,
            MAX(CASE WHEN pm.meta_key='_openclaw_city'     THEN pm.meta_value END) AS city_slug,
            MAX(CASE WHEN pm.meta_key='_thumbnail_id'      THEN pm.meta_value END) AS thumb_id
        FROM {WP_PREFIX}posts p
        LEFT JOIN {WP_PREFIX}postmeta pm ON pm.post_id = p.ID
        WHERE p.post_type = 'tribe_events' AND p.post_status IN ('publish','draft')
        GROUP BY p.ID, p.post_title
    """)
    rows = cur.fetchall()

groups = defaultdict(list)
for r in rows:
    event = {"title": r["post_title"], "start_utc": r["start_utc"],
             "start_local": r["start_local"], "city_slug": r["city_slug"]}
    fp = make_fingerprint(event)
    groups[fp].append(r)

mismatches = 0
checked = 0
for fp, posts in groups.items():
    if len(posts) < 2:
        continue
    posts_sorted = sorted(posts, key=lambda r: r["post_id"])
    keep, dupes = posts_sorted[0], posts_sorted[1:]
    for d in dupes:
        checked += 1
        if d["thumb_id"] and not keep["thumb_id"]:
            mismatches += 1
            print(f"⚠️  KEEP {keep['post_id']} '{keep['post_title']}' has NO image, "
                  f"but DELETE-target {d['post_id']} DOES -- would lose image if deleted")

print(f"\nChecked {checked} would-be-deleted posts across {sum(1 for p in groups.values() if len(p)>1)} groups.")
print(f"Image-loss mismatches found: {mismatches}")
conn.close()
