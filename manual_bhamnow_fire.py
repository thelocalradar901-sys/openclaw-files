"""
manual_bhamnow_fire.py — actually scrapes Bham Now (tec_rest, 35-day
horizon, photo-only) and inserts real events into WordPress.

Bypasses the Monitor plugin's Fire button entirely, since Fire has a
180-second timeout and this source genuinely takes 10-13 minutes given
its density (~1000 events across up to 20 paginated requests).

The normal 2-hour scheduler will pick this source up on its own with no
timeout issue -- this script exists purely to populate the site RIGHT
NOW instead of waiting for the next scheduled cycle.

Usage:
    cd /opt/openclaw
    nohup python3 manual_bhamnow_fire.py > /tmp/bhamnow_fire.log 2>&1 &
    disown -h
"""

import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

from config import load_cities, load_dynamic_sources
from scraper import scrape_source
from db import insert_event

cities = load_cities()
city = next((c for c in cities if c["slug"] == "birmingham"), None)
if not city:
    print("ERROR: could not find 'birmingham' in load_cities() -- check DB.")
    raise SystemExit(1)

sources_by_city = load_dynamic_sources()
bham_sources = [s for s in sources_by_city.get("birmingham", []) if s["name"] == "Bham Now"]
if not bham_sources:
    print("ERROR: could not find 'Bham Now' in wp_openclaw_sources for birmingham.")
    raise SystemExit(1)

source = bham_sources[0]
source["require_image"] = True  # photo-only, per 2026-07-02 decision -- forced
                                  # here in case the Notes JSON field on the
                                  # source row hasn't been updated yet

print(f"Source config: {source}")
print(f"City config: {city}\n")

events = scrape_source(source, city)
print(f"\n=== SCRAPED: {len(events)} events (require_image applied) ===")

inserted = skipped = 0
for ev in events:
    if insert_event(ev, city):
        inserted += 1
    else:
        skipped += 1

print(f"\n=== DONE: {inserted} inserted, {skipped} skipped ===")
