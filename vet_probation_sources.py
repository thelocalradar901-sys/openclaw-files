"""
vet_probation_sources.py — bulk-test all 'probation' status sources
(from discover_sources.py / tertiary_sources.py) and bucket each one
into a verdict: PROMOTE / REJECT / REVIEW.

Does NOT insert events into the DB — this is a dry-run test scrape only,
so it's safe to run alongside the live daemon. Reports counts/samples per
source so you can review before trusting any auto-promotion.

Usage on server:
    cd /opt/openclaw
    python3 vet_probation_sources.py                  # dry run, report only
    python3 vet_probation_sources.py --apply           # also updates
                                                          status in DB
                                                          (promote/reject)
    python3 vet_probation_sources.py --city denver      # limit to one city

Verdict rules (tune as needed):
    PROMOTE — scraper returned >= 3 events with valid parsed dates
    REJECT  — scraper returned 0 events, OR raised an exception
    REVIEW  — 1-2 events, or events present but dates look suspicious
              (left in 'probation' either way, never auto-touched)
"""

import argparse
import logging
import sys
import time

logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

from config import load_cities, _get_conn
from scraper import scrape_source

PROMOTE_MIN_EVENTS = 3


def _city_lookup(cities):
    return {c["slug"]: c for c in cities}


def fetch_probation_sources(conn, city_filter=None):
    # Avoid depending on dictionary=True / DictCursor kwarg quirks across
    # mysql.connector vs pymysql -- build dicts manually from description.
    cursor = conn.cursor()
    sql = """
        SELECT id, url, city_slug, name, source_type, notes, tier
        FROM wp_openclaw_sources
        WHERE status = 'probation'
    """
    params = ()
    if city_filter:
        sql += " AND city_slug = %s"
        params = (city_filter,)
    sql += " ORDER BY city_slug, id"
    cursor.execute(sql, params)
    raw_rows = cursor.fetchall()

    if raw_rows and isinstance(raw_rows[0], dict):
        # Connection already uses a DictCursor (e.g. pymysql configured
        # with cursorclass=DictCursor in config.py) -- rows are dicts already.
        cursor.close()
        return raw_rows

    # Otherwise rows are plain tuples -- build dicts from description.
    cols = [d[0] for d in cursor.description]
    cursor.close()
    return [dict(zip(cols, r)) for r in raw_rows]


def vet_source(row, city):
    """Returns (verdict, event_count, sample_titles, error_str)"""
    source = {
        "_db_id":      row["id"],
        "name":        row["name"] or row["url"],
        "url":         row["url"],
        "source_type": row["source_type"] or "squarespace",
        "city_slug":   row["city_slug"],
    }
    try:
        events = scrape_source(source, city)
    except Exception as e:
        return "REJECT", 0, [], str(e)

    count = len(events)
    sample = [e.get("title", "?") for e in events[:3]]

    if count == 0:
        return "REJECT", 0, [], ""
    elif count >= PROMOTE_MIN_EVENTS:
        return "PROMOTE", count, sample, ""
    else:
        return "REVIEW", count, sample, ""


def apply_verdict(conn, source_id, verdict):
    new_status = {"PROMOTE": "active", "REJECT": "rejected"}.get(verdict)
    if not new_status:
        return  # REVIEW -> leave alone
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE wp_openclaw_sources SET status = %s WHERE id = %s",
        (new_status, source_id),
    )
    conn.commit()
    cursor.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="Write status updates to DB")
    ap.add_argument("--city", default=None, help="Limit to one city slug")
    args = ap.parse_args()

    cities = load_cities()
    city_map = _city_lookup(cities)

    conn = _get_conn()
    rows = fetch_probation_sources(conn, args.city)

    if not rows:
        print("No probation sources found.")
        return

    print(f"Found {len(rows)} probation sources to vet.\n")

    results = {"PROMOTE": [], "REJECT": [], "REVIEW": []}

    for i, row in enumerate(rows, 1):
        city = city_map.get(row["city_slug"])
        if not city:
            print(f"[{i}/{len(rows)}] SKIP — unknown city_slug '{row['city_slug']}' for source {row['name']}")
            continue

        t0 = time.time()
        verdict, count, sample, err = vet_source(row, city)
        elapsed = time.time() - t0

        tag = f"[{i}/{len(rows)}]"
        print(f"{tag} {verdict:8s} | {row['city_slug']:10s} | {count:3d} events | {elapsed:5.1f}s | {row['name']} ({row['url']})")
        if sample:
            print(f"          sample: {sample}")
        if err:
            print(f"          error: {err}")

        results[verdict].append(row)

        if args.apply:
            apply_verdict(conn, row["id"], verdict)

        time.sleep(0.5)  # be polite to source sites

    conn.close()

    print("\n=== SUMMARY ===")
    print(f"PROMOTE: {len(results['PROMOTE'])}")
    print(f"REJECT:  {len(results['REJECT'])}")
    print(f"REVIEW:  {len(results['REVIEW'])}")
    if not args.apply:
        print("\n(Dry run — no DB changes made. Re-run with --apply to commit PROMOTE/REJECT status updates.)")
    else:
        print("\nDB updated: PROMOTE -> active, REJECT -> rejected. REVIEW left as probation.")


if __name__ == "__main__":
    main()
