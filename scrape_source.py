"""
scrape_source.py — Run OpenClaw scraper for a single source ID

Usage:
  /opt/openclaw/venv/bin/python /opt/openclaw/scrape_source.py --source-id 3
"""

import argparse
import logging
import os
import sys

# ── Bootstrap env ─────────────────────────────────────────────────────────────
env_file = "/etc/openclaw/openclaw.env"
if os.path.exists(env_file):
    with open(env_file) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

sys.path.insert(0, "/opt/openclaw")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("openclaw.scrape_source")

from config import load_cities, _get_conn, WP_PREFIX
from scraper import scrape_source
import db

def get_source(source_id: int) -> dict | None:
    conn = _get_conn()
    try:
        import json as _json
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT * FROM {WP_PREFIX}openclaw_sources WHERE id=%s LIMIT 1",
                (source_id,)
            )
            row = cur.fetchone()
        if not row:
            return None
        extra = {}
        try:
            if row.get("notes"):
                extra = _json.loads(row["notes"])
        except Exception:
            pass
        stype = (row["source_type"] or "html_auto").strip()
        if stype in ("auto", "squarespace"):
            stype = "html_auto"
        return {
            "_db_id":      row["id"],
            "name":        row["name"] or row["url"],
            "url":         row["url"],
            "source_type": stype,
            "city_slug":   row["city_slug"],
            **extra,
        }
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-id", type=int, required=True, help="wp_openclaw_sources.id")
    parser.add_argument("--dry-run", action="store_true", help="Print events without inserting")
    args = parser.parse_args()

    source = get_source(args.source_id)
    if not source:
        log.error("Source ID %d not found", args.source_id)
        sys.exit(1)

    log.info("Source: [%d] %s (%s) — %s", args.source_id, source["name"], source["source_type"], source["url"])

    # Find city config
    cities = load_cities()
    city = next((c for c in cities if c["slug"] == source["city_slug"]), None)
    if not city:
        # Fallback
        city = {"slug": source["city_slug"], "name": source["city_slug"].title(), "timezone": "America/Chicago"}
    log.info("City: %s (tz=%s)", city["name"], city.get("timezone", "?"))

    # Scrape
    events = scrape_source(source, city)
    log.info("Scraped %d events", len(events))

    if not events:
        log.warning("No events returned — nothing to insert")
        return

    if args.dry_run:
        for ev in events:
            print(f"  {ev['start_date']} | {ev['title'][:60]}")
        return

    # Insert via db.py
    inserted = 0
    updated  = 0
    skipped  = 0
    for ev in events:
        result = db.upsert_event(ev)
        if result == "inserted":
            inserted += 1
        elif result == "updated":
            updated += 1
        else:
            skipped += 1

    log.info("Done — inserted: %d, updated: %d, skipped: %d", inserted, updated, skipped)


if __name__ == "__main__":
    main()
