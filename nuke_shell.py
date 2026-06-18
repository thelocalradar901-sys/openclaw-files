"""
nuke_shell.py — Delete all Overton Park Shell events (source ID 3) and re-scrape.
Run as: sudo /opt/openclaw/venv/bin/python3 /tmp/nuke_shell.py
"""
import sys
sys.path.insert(0, "/opt/openclaw")

import pymysql
import os

# Load env vars from openclaw config file
with open("/etc/openclaw/openclaw.env") as f:
    for line in f:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

# ── DB connection ─────────────────────────────────────────────────────────────
conn = pymysql.connect(
    host=os.environ["WP_DB_HOST"],
    user=os.environ["WP_DB_USER"],
    password=os.environ["WP_DB_PASSWORD"],
    database=os.environ["WP_DB_NAME"],
    charset="utf8mb4",
)
prefix = os.environ.get("WP_PREFIX", "wp_")
cur = conn.cursor()

# ── Step 1: find posts belonging to source 3 ─────────────────────────────────
cur.execute(f"""
    SELECT post_id FROM {prefix}postmeta
    WHERE meta_key = '_openclaw_source_id' AND meta_value = '3'
""")
rows = cur.fetchall()
post_ids = [r[0] for r in rows]

if not post_ids:
    print("No events found for source ID 3 — nothing to nuke.")
    conn.close()
    sys.exit(0)

print(f"Found {len(post_ids)} events to delete for source ID 3 (Overton Park Shell)...")

id_list = ",".join(str(i) for i in post_ids)

# ── Step 2: delete TEC tables first (FK-safe order) ──────────────────────────
cur.execute(f"DELETE FROM {prefix}tec_occurrences WHERE post_id IN ({id_list})")
cur.execute(f"DELETE FROM {prefix}tec_events     WHERE post_id IN ({id_list})")
cur.execute(f"DELETE FROM {prefix}term_relationships WHERE object_id IN ({id_list})")
cur.execute(f"DELETE FROM {prefix}postmeta WHERE post_id IN ({id_list})")
cur.execute(f"DELETE FROM {prefix}posts    WHERE ID IN ({id_list})")
conn.commit()
conn.close()
print(f"Deleted {len(post_ids)} events and all associated meta/TEC rows.")

# ── Step 3: re-scrape ─────────────────────────────────────────────────────────
from db import get_sources, get_cities
from scraper import scrape_source
from scrape_source import insert_event

sources = [s for s in get_sources() if s["id"] == 3]
if not sources:
    print("ERROR: Source ID 3 not found in wp_openclaw_sources.")
    sys.exit(1)

src = sources[0]
print(f"Scraping source: {src['name']} ({src['url']})")

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
