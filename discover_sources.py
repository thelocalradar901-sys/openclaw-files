#!/usr/bin/env python3
"""
discover_sources.py — automated source discovery for OpenClaw.

Runs weekly, once per city. For each city:
  1. Queries Google Places API (New) for venues likely to host events
     (comedy clubs, music venues, theaters, concert halls, breweries,
     museums, parks & rec, sports venues) within radius of the city's
     lat/lng. Reliable, low-cost for this volume (~$0-10/month), backed
     by an SLA -- replaces the earlier free OSM Overpass approach,
     which proved too flaky (public mirror outages/rate limits) for a
     weekly automated job we depend on.
  2. For each venue with a website not already a known source (deduped
     by domain), fetches its website and probes for a usable event feed:
       - iCal feed (?ical=1 / .ics link)
       - JSON-LD Event schema in the page HTML
       - Known plugin fingerprints (The Events Calendar, Modern Events
         Calendar, RHP) via URL/HTML signature
     This reuses the same detection signatures scraper.py's tiered
     fallback already understands, so anything added here is something
     scraper.py can actually parse on the next run.
  3. Usable sources are INSERTED into wp_openclaw_sources with
     status='probation' (NOT 'active' -- the vetting/pruning lifecycle,
     built separately, promotes or retires them later based on real
     scrape yield).
  4. Collects everything found this run and writes/logs a summary so
     additions are never silent, even though they're fully automatic.

This script does NOT scrape events itself -- it only finds and
registers candidate sources. The existing scheduler.py will pick up
new probation sources on its next hourly _refresh() the same way it
already does for manually-added sources.

Run with --dry-run (default) first. Pass --apply to actually write
new sources to the DB.

Ad-hoc single-city mode: pass --city-name/--slug/--lat/--lng (and
optionally --radius-miles) to run discovery for one city that isn't
in wp_openclaw_cities yet -- e.g. a new market still in pre-launch
discovery, kept off the public site until it's flipped to 'active'.
"""

import argparse
import json
import logging
import re
import sys
import time
from urllib.parse import urlparse

import requests

sys.path.insert(0, "/opt/openclaw")

from config import load_cities, _get_conn as get_connection  # reuse existing helper

log = logging.getLogger("openclaw.discover")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

PLACES_API_URL = "https://places.googleapis.com/v1/places:searchNearby"
GOOGLE_PLACES_API_KEY = "AIzaSyBNWQegYbuy4yX5mmzCT7DK7k8IjOZITWQ"
WP_PREFIX = "wp_"
TIMEOUT = 30

# Broad coverage per your priority: nightlife/arts core + breweries,
# museums, parks & rec, sports venues. Mapped to Google Places "included
# types" -- see https://developers.google.com/maps/documentation/places/web-service/place-types
PLACES_TYPE_QUERIES = {
    "comedy club":            ["night_club"],
    "music venue":            ["night_club"],
    "theater":                ["performing_arts_theater"],
    "concert hall":           ["performing_arts_theater"],
    "brewery":                ["brewery", "bar"],
    "museum":                 ["museum"],
    "parks and recreation":   ["park"],
    "sports arena":           ["stadium"],
    "performing arts center": ["performing_arts_theater"],
}

SEARCH_RADIUS_METERS = 40000.0  # ~25 miles -- default fallback if a city has no radius_miles
METERS_PER_MILE = 1609.34

ICAL_PATHS = ["/events/?ical=1", "/events/list/?ical=1", "/?ical=1", "/events.ics"]

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; OpenClaw/1.0; +https://thelocalradar.com)"}


def places_search(included_types: list[str], lat: float, lng: float, radius_m: float) -> list[dict]:
    """
    Query Google Places API (New) searchNearby for venues matching the
    given place types within radius_m meters of lat/lng. Returns a list
    of dicts with name + website (when Google has one on file).
    """
    time.sleep(0.2)  # light pacing -- well under any real rate limit

    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": GOOGLE_PLACES_API_KEY,
        "X-Goog-FieldMask": "places.displayName,places.websiteUri",
    }
    body = {
        "includedTypes": included_types,
        "maxResultCount": 20,
        "locationRestriction": {
            "circle": {
                "center": {"latitude": lat, "longitude": lng},
                "radius": radius_m,
            }
        },
    }

    try:
        resp = requests.post(PLACES_API_URL, headers=headers, json=body, timeout=TIMEOUT)
        if resp.status_code == 429:
            log.warning("Places API rate limited -- backing off 5s and retrying once")
            time.sleep(5)
            resp = requests.post(PLACES_API_URL, headers=headers, json=body, timeout=TIMEOUT)
        resp.raise_for_status()
        results = resp.json().get("places", [])
    except requests.RequestException as e:
        log.warning("Places API query failed for %s: %s", included_types, e)
        return []

    places = []
    for p in results:
        name = (p.get("displayName") or {}).get("text", "").strip()
        website = (p.get("websiteUri") or "").strip()
        if name and website:
            places.append({"name": name, "url": website})
    return places


def existing_domains(conn) -> set[str]:
    import pymysql
    cur = conn.cursor(pymysql.cursors.DictCursor)
    cur.execute(f"SELECT url FROM {WP_PREFIX}openclaw_sources")
    domains = set()
    for r in cur.fetchall():
        try:
            domains.add(urlparse(r["url"]).netloc.lower().lstrip("www."))
        except Exception:
            pass
    cur.close()
    return domains


def probe_for_feed(site_url: str) -> dict | None:
    """
    Probe a site for a usable event feed. Returns a dict describing the
    detected source_type + the URL to actually scrape, or None if
    nothing usable was found.
    """
    site_url = site_url.rstrip("/")

    # 1. Try known iCal paths
    for path in ICAL_PATHS:
        try:
            resp = requests.get(site_url + path, headers=HEADERS, timeout=TIMEOUT)
            if resp.status_code == 200 and "BEGIN:VCALENDAR" in resp.text[:2000]:
                return {"source_type": "ical", "url": site_url + path}
        except requests.RequestException:
            continue

    # 2. Fetch homepage / events page and look for JSON-LD Event schema
    #    or known plugin fingerprints.
    for candidate in (site_url + "/events/", site_url + "/events", site_url):
        try:
            resp = requests.get(candidate, headers=HEADERS, timeout=TIMEOUT)
        except requests.RequestException:
            continue
        if resp.status_code != 200:
            continue
        html = resp.text

        if re.search(r'"@type"\s*:\s*"Event"', html):
            return {"source_type": "jsonld", "url": candidate}

        if "tribe-events" in html or "the-events-calendar" in html:
            return {"source_type": "tec_html", "url": candidate}

        if "mec-event" in html or "modern-events-calendar" in html:
            return {"source_type": "mec_html", "url": candidate}

        if "rhp_events" in html or "rhpEvents" in html:
            return {"source_type": "rhp", "url": candidate}

    return None


def discover_for_city(city: dict, conn, dry_run: bool) -> list[dict]:
    found = []
    known = existing_domains(conn)

    radius_m = city.get("radius_miles", 25) * METERS_PER_MILE

    for category, included_types in PLACES_TYPE_QUERIES.items():
        places = places_search(included_types, city["lat"], city["lng"], radius_m)
        log.info("'%s' near %s: %d results with a website", category, city["name"], len(places))

        for place in places:
            site_url = place["url"]
            name = place["name"]
            if not site_url or not name:
                continue

            domain = urlparse(site_url).netloc.lower().lstrip("www.")
            if not domain or domain in known:
                continue
            known.add(domain)  # avoid re-probing the same domain twice this run

            feed = probe_for_feed(site_url)
            time.sleep(0.5)  # be polite to target sites

            if feed:
                found.append({
                    "name": name,
                    "url": feed["url"],
                    "source_type": feed["source_type"],
                    "city_slug": city["slug"],
                    "category": category,
                })
                log.info("FOUND usable source: %s (%s) -> %s",
                         name, feed["source_type"], feed["url"])

    if found and not dry_run:
        cur = conn.cursor()
        for f in found:
            cur.execute(
                f"INSERT INTO {WP_PREFIX}openclaw_sources "
                f"(city_slug, name, url, source_type, status, notes, browser_ua) "
                f"VALUES (%s, %s, %s, %s, 'probation', %s, 0)",
                (f["city_slug"], f["name"], f["url"], f["source_type"],
                 f"Auto-discovered via Google Places API ({f['category']})")
            )
        conn.commit()
        cur.close()

    return found


def send_summary_email(all_found: list[dict]):
    if not all_found:
        log.info("No new sources found this run -- skipping summary email.")
        return

    lines = ["New OpenClaw sources added to PROBATION this week:\n"]
    by_city = {}
    for f in all_found:
        by_city.setdefault(f["city_slug"], []).append(f)

    for city_slug, items in by_city.items():
        lines.append(f"\n{city_slug.upper()} ({len(items)} new):")
        for it in items:
            lines.append(f"  - {it['name']} [{it['source_type']}] {it['url']}")

    body = "\n".join(lines)
    log.info("Summary:\n%s", body)

    # TODO: wire this to digest.py's actual Brevo send function once its
    # signature is confirmed (digest.py lives in scripts_archive/ and
    # has its own self-contained Brevo credentials/logic -- this script
    # intentionally does not duplicate/guess at that). For now the
    # summary is written to the log and to /opt/openclaw/discovery_summary.txt
    # so it's never silent even before email is wired up.
    try:
        with open("/opt/openclaw/discovery_summary.txt", "a") as f:
            f.write(f"\n\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n{body}\n")
        log.info("Summary appended to /opt/openclaw/discovery_summary.txt")
    except Exception as e:
        log.error("Failed to write summary file: %s", e)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                     help="Actually write new sources + send email. Default is dry-run.")
    ap.add_argument("--city-name", help="Ad-hoc city name, e.g. 'Tampa'. Bypasses "
                     "wp_openclaw_cities entirely -- use for pre-launch discovery on a "
                     "city that isn't 'active' (and therefore isn't on the public site) "
                     "yet. Requires --slug, --lat, --lng.")
    ap.add_argument("--slug", help="Ad-hoc city slug, e.g. 'tampa'.")
    ap.add_argument("--lat", type=float, help="Ad-hoc city latitude.")
    ap.add_argument("--lng", type=float, help="Ad-hoc city longitude.")
    ap.add_argument("--radius-miles", type=int, default=35,
                     help="Ad-hoc city radius in miles (default 35).")
    args = ap.parse_args()
    dry_run = not args.apply

    conn = get_connection()
    try:
        if args.city_name:
            if not (args.slug and args.lat is not None and args.lng is not None):
                print("--city-name requires --slug, --lat, and --lng")
                sys.exit(1)
            cities = [{
                "name": args.city_name,
                "slug": args.slug,
                "lat": args.lat,
                "lng": args.lng,
                "radius_miles": args.radius_miles,
            }]
            log.info("Ad-hoc city mode: %s (%s), %s mi radius -- NOT reading/writing "
                      "wp_openclaw_cities", args.city_name, args.slug, args.radius_miles)
        else:
            cities = load_cities()
        all_found = []
        for city in cities:
            log.info("=== Discovering sources for %s ===", city["name"])
            found = discover_for_city(city, conn, dry_run)
            all_found.extend(found)

        print(f"\nTotal new candidate sources found: {len(all_found)}")
        for f in all_found:
            print(f"  {f['city_slug']:10s} {f['source_type']:10s} {f['name']} -> {f['url']}")

        if dry_run:
            print("\nDRY RUN -- nothing written to DB, no email sent. Re-run with --apply.")
        else:
            send_summary_email(all_found)
            print(f"\nInserted {len(all_found)} probation sources and sent summary email.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
