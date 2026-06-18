"""
nuke_shell_final.py — Nuke Overton Shell events then re-scrape source ID 3.
"""
import sys, os, pymysql, json
sys.path.insert(0, "/opt/openclaw")

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
cur = conn.cursor()

# Nuke by _openclaw_source meta
cur.execute("""
    SELECT DISTINCT post_id FROM wp_postmeta
    WHERE meta_key = '_openclaw_source' AND meta_value = 'Overton Shell Events'
""")
post_ids = list(set(r[0] for r in cur.fetchall()))

if not post_ids:
    print("No events found to nuke.")
else:
    ids = ",".join(str(i) for i in post_ids)
    cur.execute(f"DELETE FROM wp_tec_occurrences WHERE post_id IN ({ids})")
    cur.execute(f"DELETE FROM wp_tec_events WHERE post_id IN ({ids})")
    cur.execute(f"DELETE FROM wp_term_relationships WHERE object_id IN ({ids})")
    cur.execute(f"DELETE FROM wp_postmeta WHERE post_id IN ({ids})")
    cur.execute(f"DELETE FROM wp_posts WHERE ID IN ({ids})")
    conn.commit()
    print(f"Nuked {len(post_ids)} Overton Shell events.")

# Load source + city directly from DB
cur.execute("SELECT id, name, url, source_type, config_json FROM wp_openclaw_sources WHERE id = 3")
row = cur.fetchone()
if not row:
    print("ERROR: Source ID 3 not found in wp_openclaw_sources.")
    conn.close()
    sys.exit(1)

src = {
    "id":          row[0],
    "name":        row[1],
    "url":         row[2],
    "source_type": row[3],
}
if row[4]:
    try:
        src.update(json.loads(row[4]))
    except Exception:
        pass

cur.execute("SELECT id, name, slug, timezone FROM wp_openclaw_cities WHERE slug = 'memphis'")
crow = cur.fetchone()
if not crow:
    print("ERROR: Memphis not found in wp_openclaw_cities.")
    conn.close()
    sys.exit(1)

city = {"id": crow[0], "name": crow[1], "slug": crow[2], "timezone": crow[3]}
conn.close()

print(f"Scraping: {src['name']} ({src['url']})")

from scraper import scrape_source
from db import insert_event

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

print(f"\nDone. {ok}/{len(events)} inserted.")
