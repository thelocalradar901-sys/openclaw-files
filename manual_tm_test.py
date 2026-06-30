"""
manual_tm_test.py — one-off manual trigger to verify TM chunking fix
without waiting for the hourly scheduler.

Usage on server:
    cd /opt/openclaw
    python3 manual_tm_test.py denver
    python3 manual_tm_test.py nashville
    python3 manual_tm_test.py          # runs all cities
"""

import logging
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

from config import load_cities
from ticketmaster import pull_city

target = sys.argv[1].lower() if len(sys.argv) > 1 else None

cities = load_cities()
if target:
    cities = [c for c in cities if c["slug"] == target]
    if not cities:
        print(f"No city matching slug '{target}'. Available: {[c['slug'] for c in load_cities()]}")
        sys.exit(1)

for city in cities:
    print(f"\n=== {city['name']} (lat={city.get('lat')}, lng={city.get('lng')}) ===")
    events = pull_city(city)
    print(f"=== {city['name']}: {len(events)} total events pulled ===\n")
