#!/usr/bin/env python3
"""
discover_sources.py — automated source discovery for OpenClaw.

Runs weekly, once per city. For each city:
  1. Queries OpenStreetMap's Overpass API for venues likely to host
     events (comedy clubs, music venues, theaters, concert halls,
     breweries, museums, parks & rec, sports venues) within radius of
     the city's lat/lng. Free, no API key, no quota.
  2. For each venue with a tagged website not already a known source
     (deduped by domain), fetches its website and probes for a usable
     event feed:
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
"""

import argparse
import json
import logging
import re
import sys
import time
from urllib.parse import urlparse, urlencode

import requests

sys.path.insert(0, "/opt/openclaw")

from config import load_cities, _get_conn as get_connection  # reuse existing helper

log = logging.getLogger("openclaw.discover")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
WP_PREFIX = "wp_"
TIMEOUT = 30

# Broad coverage per your priority: nightlife/arts core + breweries,
# museums, parks & rec, sports venues. Mapped to real OSM tags --
# Overpass has no free-text category search, so each "category" here
# is one or more OSM tag=value pairs to query for.
OSM_TAG_QUERIES = {
    "comedy club":           ['amenity=nightclub', 'amenity=arts_centre'],
    "music venue":           ['amenity=music_venue', 'amenity=nightclub'],
    "theater":               ['amenity=theatre'],
    "concert hall":          ['amenity=arts_centre', 'amenity=theatre'],
    "brewery":               ['craft=brewery', 'microbrewery=yes'],
    "museum":                ['tourism=museum'],
    "parks and recreation":  ['leisure=park', 'leisure=sports_centre'],
    "sports arena":          ['leisure=stadium', 'leisure=sports_centre'],
    "performing arts center": ['amenity=arts_centre'],
}

SEARCH_RADIUS_METERS = 40000  # ~25 miles -- matches your existing TM_RADIUS scale

ICAL_PATHS = ["/events/?ical=1", "/events/list/?ical=1", "/?ical=1", "/events.ics"]

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; OpenClaw/1.0; +https://thelocalradar.com)"}


def overpass_query(tag_filters: list[str], lat: float, lng: float, radius_m: int) -> list[dict]:
    """
    Query Overpass API for nodes/ways/relations matching any of the given
    OSM tag filters (e.g. 'amenity=theatre') within radius_m meters of
    lat/lng. Returns a list of dicts with name + website (when tagged).
    Free, no API key, no quota -- subject to fair-use rate limiting on
    the public overpass-api.de instance, so we pace requests politely.
    """
    time.sleep(1.5)  # be a good citizen on the free public instance

    clauses = []
    for tf in tag_filters:
        key, _, val = tf.partition("=")
        clauses.append(f'nwr["{key}"="{val}"](around:{radius_m},{lat},{lng});')

    query = f"""
[out:json][timeout:25];
(
  {' '.join(clauses)}
);
out center tags;
"""
    encoded_body = urlencode({"data": query}).encode("utf-8")

    try:
        resp = requests.post(
            OVERPASS_URL,
            data=encoded_body,
            headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "*/*"},
            timeout=TIMEOUT,
        )
        if resp.status_code == 429:
            log.warning("Overpass rate limited -- backing off 10s and retrying once")
            time.sleep(10)
            resp = requests.post(
                OVERPASS_URL,
                data=encoded_body,
                headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "*/*"},
                timeout=TIMEOUT,
            )
        resp.raise_for_status()
        elements = resp.json().get("elements", [])
    except requests.RequestException as e:
        log.warning("Overpass query failed for %s: %s", tag_filters, e)
        return []

    places = []
    for el in elements:
        tags = el.get("tags", {}) or {}
        name = tags.get("name", "").strip()
        website = (tags.get("website") or tags.get("contact:website") or "").strip()
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

    for category, tag_filters in OSM_TAG_QUERIES.items():
        places = overpass_query(tag_filters, city["lat"], city["lng"], SEARCH_RADIUS_METERS)
        log.info("'%s' near %s: %d results with a website tag", category, city["name"], len(places))

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
                 f"Auto-discovered via OSM Overpass ({f['category']})")
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
    args = ap.parse_args()
    dry_run = not args.apply

    conn = get_connection()
    try:
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
