"""
scrape_source.py — Run OpenClaw scraper for a single source ID

Usage:
  /opt/openclaw/venv/bin/python /opt/openclaw/scrape_source.py --source-id 3
  /opt/openclaw/venv/bin/python /opt/openclaw/scrape_source.py --source-id 3 --dry-run
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
    """Load a source row from wp_openclaw_sources by ID.
    Extra config stored in the 'notes' column as JSON (optional).
    """
    import json as _json
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT * FROM {WP_PREFIX}openclaw_sources WHERE id=%s LIMIT 1",
                (source_id,)
            )
            row = cur.fetchone()
        if not row:
            return None
        # Parse optional JSON extras from notes column
        extra = {}
        notes = row.get("notes") or ""
        if notes.strip().startswith("{"):
            try:
                extra = _json.loads(notes)
            except Exception:
                pass
        stype = (row.get("source_type") or "html_auto").strip()
        if stype in ("auto", "squarespace"):
            stype = "html_auto"
        return {
            "id":          row["id"],
            "name":        row.get("name") or row["url"],
            "url":         row["url"],
            "source_type": stype,
            "city_slug":   row.get("city_slug", ""),
            **extra,
        }
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description="Run OpenClaw scraper for one source")
    parser.add_argument("--source-id", type=int, required=True, help="wp_openclaw_sources.id")
    parser.add_argument("--dry-run", action="store_true", help="Print events without inserting")
    args = parser.parse_args()

    source = get_source(args.source_id)
    if not source:
        log.error("Source ID %d not found in %sopenclaw_sources", args.source_id, WP_PREFIX)
        sys.exit(1)

    log.info("Source: [%d] %s (%s) — %s",
             args.source_id, source["name"], source["source_type"], source["url"])

    # Resolve city config
    cities = load_cities()
    city = next((c for c in cities if c["slug"] == source["city_slug"]), None)
    if not city:
        log.warning("City slug '%s' not found — using fallback", source["city_slug"])
        city = {
            "slug":     source["city_slug"],
            "name":     source["city_slug"].title(),
            "timezone": "America/Chicago",
        }
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

    # Insert
    inserted = 0
    failed   = 0
    for ev in events:
        try:
            result = db.insert_event(ev)
            if result:
                inserted += 1
        except Exception as e:
            log.warning("Failed to insert '%s': %s", ev.get("title", "")[:50], e)
            failed += 1

    log.info("Done — inserted: %d, failed: %d", inserted, failed)


if __name__ == "__main__":
    main()
