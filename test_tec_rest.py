"""
test_tec_rest.py — directly calls scrape_source() with source_type=tec_rest
against Bham Now, bypassing the Monitor plugin's Fire button entirely, so we
see the real traceback instead of a swallowed "0 events" result.

Usage:
    cd /opt/openclaw
    python3 test_tec_rest.py
"""

import logging
import traceback

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

from scraper import scrape_source

source = {
    "_db_id":        0,
    "name":          "Bham Now",
    "url":           "https://bhamnow.com/bhamn-events/",
    "source_type":   "tec_rest",
    "city_slug":     "birmingham",
    "require_image": True,
}
city = {
    "slug": "birmingham",
    "name": "Birmingham",
    "timezone": "America/Chicago",
}

try:
    events = scrape_source(source, city)
    print(f"\n=== RESULT: {len(events)} events ===")

    from collections import Counter
    date_counts = Counter(e["start_date"][:10] for e in events if e.get("start_date"))
    no_date_count = sum(1 for e in events if not e.get("start_date"))
    print(f"\n=== DATE DISTRIBUTION ({len(date_counts)} distinct dates, {no_date_count} events with no date) ===")
    for d, count in sorted(date_counts.items()):
        print(f"  {d}: {count} events")

    print(f"\n=== FIRST 5 SAMPLE ===")
    for e in events[:5]:
        print(f"  - {e['title']!r} | {e['start_date']} | img={bool(e['image_url'])}")
except Exception:
    print("\n=== EXCEPTION (this is what the Monitor UI swallowed) ===")
    traceback.print_exc()
