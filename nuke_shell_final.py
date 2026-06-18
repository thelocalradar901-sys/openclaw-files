"""
nuke_shell_final.py — Nuke Overton Shell events by _openclaw_source meta key, then re-scrape.
"""
import sys, os, pymysql
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

cur.execute("""
    SELECT DISTINCT post_id FROM wp_postmeta
    WHERE meta_key = '_openclaw_source' AND meta_value = 'Overton Shell Events'
""")
post_ids = list(set(r[0] for r in cur.fetchall()))

if not post_ids:
    print("No events found — nothing to nuke.")
else:
    ids = ",".join(str(i) for i in post_ids)
    cur.execute(f"DELETE FROM wp_tec_occurrences WHERE post_id IN ({ids})")
    cur.execute(f"DELETE FROM wp_tec_events WHERE post_id IN ({ids})")
    cur.execute(f"DELETE FROM wp_term_relationships WHERE object_id IN ({ids})")
    cur.execute(f"DELETE FROM wp_postmeta WHERE post_id IN ({ids})")
    cur.execute(f"DELETE FROM wp_posts WHERE ID IN ({ids})")
    conn.commit()
    print(f"Nuked {len(post_ids)} Overton Shell events.")

conn.close()

# Re-scrape
import importlib
db = importlib.import_module("db")
from scraper import scrape_source
from scrape_source import insert_event

# Get source 3
all_sources = db.get_db_sources() if hasattr(db, 'get_db_sources') else None
if all_sources is None:
    # Try common names
    for fn in ('get_sources', 'fetch_sources', 'load_sources'):
        if hasattr(db, fn):
            all_sources = getattr(db, fn)()
            break

if not all_sources:
    print("ERROR: Can't find get_sources in db.py — listing available functions:")
    print([x for x in dir(db) if not x.startswith('_')])
    sys.exit(1)

src = next((s for s in all_sources if s.get("id") == 3), None)
if not src:
    print("ERROR: Source ID 3 not found.")
    sys.exit(1)

# Get Memphis city
for fn in ('get_cities', 'fetch_cities', 'load_cities'):
    if hasattr(db, fn):
        cities = getattr(db, fn)()
        break
else:
    print("ERROR: Can't find get_cities in db.py")
    sys.exit(1)

city = next((c for c in cities if c.get("slug") == "memphis"), None)
if not city:
    print("ERROR: Memphis not found.")
    sys.exit(1)

print(f"Scraping: {src['name']} ({src['url']})")
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
