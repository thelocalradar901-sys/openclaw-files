"""
shell_rebuild_v4.py — Nuke ALL Overton Shell events (by source name) and re-scrape.
"""
import sys
sys.path.insert(0, "/opt/openclaw")

import pymysql
import os

# Load env vars
with open("/etc/openclaw/openclaw.env") as f:
    for line in f:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

conn = pymysql.connect(
    host=os.environ["WP_DB_HOST"],
    user=os.environ["WP_DB_USER"],
    password=os.environ["WP_DB_PASSWORD"],
    database=os.environ["WP_DB_NAME"],
    charset="utf8mb4",
)
prefix = os.environ.get("WP_PREFIX", "wp_")
cur = conn.cursor()

# Find by source name meta key (catches both old and new inserts)
cur.execute(f"""
    SELECT DISTINCT post_id FROM {prefix}postmeta
    WHERE meta_key = '_openclaw_source_name'
    AND meta_value LIKE '%Overton%'
""")
post_ids = [r[0] for r in cur.fetchall()]

# Also catch by source_id = 3
cur.execute(f"""
    SELECT DISTINCT post_id FROM {prefix}postmeta
    WHERE meta_key = '_openclaw_source_id' AND meta_value = '3'
""")
post_ids += [r[0] for r in cur.fetchall()]
post_ids = list(set(post_ids))

if post_ids:
    id_list = ",".join(str(i) for i in post_ids)
    cur.execute(f"DELETE FROM {prefix}tec_occurrences WHERE post_id IN ({id_list})")
    cur.execute(f"DELETE FROM {prefix}tec_events WHERE post_id IN ({id_list})")
    cur.execute(f"DELETE FROM {prefix}term_relationships WHERE object_id IN ({id_list})")
    cur.execute(f"DELETE FROM {prefix}postmeta WHERE post_id IN ({id_list})")
    cur.execute(f"DELETE FROM {prefix}posts WHERE ID IN ({id_list})")
    conn.commit()
    print(f"Deleted {len(post_ids)} existing Overton Shell events.")
else:
    print("No existing events found by name or source ID — checking by post title...")
    # Last resort: find by source_name in postmeta
    cur.execute(f"""
        SELECT DISTINCT post_id FROM {prefix}postmeta
        WHERE meta_key = 'source_name' AND meta_value LIKE '%Overton%'
    """)
    post_ids = [r[0] for r in cur.fetchall()]
    if post_ids:
        id_list = ",".join(str(i) for i in post_ids)
        cur.execute(f"DELETE FROM {prefix}tec_occurrences WHERE post_id IN ({id_list})")
        cur.execute(f"DELETE FROM {prefix}tec_events WHERE post_id IN ({id_list})")
        cur.execute(f"DELETE FROM {prefix}term_relationships WHERE object_id IN ({id_list})")
        cur.execute(f"DELETE FROM {prefix}postmeta WHERE post_id IN ({id_list})")
        cur.execute(f"DELETE FROM {prefix}posts WHERE ID IN ({id_list})")
        conn.commit()
        print(f"Deleted {len(post_ids)} events via source_name fallback.")
    else:
        print("Still nothing found. Run this to debug:")
        print(f"  SELECT meta_key, meta_value FROM {prefix}postmeta WHERE post_id = <one of the post IDs you see in WP admin>;")

conn.close()

# Re-scrape
from db import get_sources, get_cities
from scraper import scrape_source
from scrape_source import insert_event

sources = [s for s in get_sources() if s["id"] == 3]
if not sources:
    print("ERROR: Source ID 3 not found.")
    sys.exit(1)

src = sources[0]
print(f"Scraping: {src['name']} ({src['url']})")

cities = get_cities()
city = next((c for c in cities if c.get("slug") == "memphis"), None)
if not city:
    print("ERROR: Memphis city not found.")
    sys.exit(1)

events = scrape_source(src, city)
print(f"Scraped {len(events)} events — inserting...")

ok = 0
for e in events:
    try:
        insert_event(e)
        print(f"  + {e['start_date'][:16]}  {e['title'][:60]}")
        ok += 1
    except Exception as ex:
        print(f"  ! FAILED {e['title'][:40]}: {ex}")

print(f"\nDone. {ok}/{len(events)} events inserted.")
