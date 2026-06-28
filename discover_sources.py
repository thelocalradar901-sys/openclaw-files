#!/usr/bin/env python3
"""
discover_sources.py — automated source discovery for OpenClaw.

Runs weekly, once per city. For each city:
  1. Queries Brave Search's Place Search (local) endpoint for venue
     categories likely to host events (comedy clubs, music venues,
     theaters, concert halls, breweries, museums, parks & rec, sports
     venues).
  2. For each place result not already a known source (deduped by
     domain), fetches its website and probes for a usable event feed:
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
  4. Collects everything found this run and emails a summary so
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
from urllib.parse import urlparse

import requests

sys.path.insert(0, "/opt/openclaw")

from config import load_cities, get_connection  # reuse existing helpers
from config import BREVO_API_KEY, BREVO_DIGEST_TO  # reuse existing email config

log = logging.getLogger("openclaw.discover")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

BRAVE_API_KEY = "BSAd9foOtfvtXhPP36H06SqS8dtznb2"
BRAVE_PLACE_URL = "https://api.search.brave.com/res/v1/local/place_search"
WP_PREFIX = "wp_"
TIMEOUT = 15

# Broad coverage per your priority: nightlife/arts core + breweries,
# museums, parks & rec, sports venues.
CATEGORIES = [
    "comedy club",
    "music venue",
    "theater",
    "concert hall",
    "brewery",
    "museum",
    "parks and recreation",
    "sports arena",
    "performing arts center",
]

ICAL_PATHS = ["/events/?ical=1", "/events/list/?ical=1", "/?ical=1", "/events.ics"]

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; OpenClaw/1.0; +https://thelocalradar.com)"}


def brave_place_search(query: str, lat: float, lng: float) -> list[dict]:
    """Query Brave's local Place Search endpoint near a coordinate."""
    headers = {"Accept": "application/json", "X-Subscription-Token": BRAVE_API_KEY}
    params = {"q": query, "lat": lat, "lng": lng, "count": 20}
    try:
        resp = requests.get(BRAVE_PLACE_URL, headers=headers, params=params, timeout=TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        return data.get("results", []) or data.get("places", [])
    except requests.RequestException as e:
        log.warning("Brave place search failed for '%s': %s", query, e)
        return []


def existing_domains(conn) -> set[str]:
    with conn.cursor() as cur:
        cur.execute(f"SELECT url FROM {WP_PREFIX}openclaw_sources")
        domains = set()
        for r in cur.fetchall():
            try:
                domains.add(urlparse(r["url"]).netloc.lower().lstrip("www."))
            except Exception:
                pass
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

    for category in CATEGORIES:
        results = brave_place_search(category, city["lat"], city["lng"])
        log.info("'%s' near %s: %d results", category, city["name"], len(results))

        for place in results:
            site_url = (place.get("url") or place.get("website") or "").strip()
            name = (place.get("title") or place.get("name") or "").strip()
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
        with conn.cursor() as cur:
            for f in found:
                cur.execute(
                    f"INSERT INTO {WP_PREFIX}openclaw_sources "
                    f"(city_slug, name, url, source_type, status, notes, browser_ua) "
                    f"VALUES (%s, %s, %s, %s, 'probation', %s, 0)",
                    (f["city_slug"], f["name"], f["url"], f["source_type"],
                     f"Auto-discovered via Brave Place Search ({f['category']})")
                )
        conn.commit()

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

    if not BREVO_API_KEY or not BREVO_DIGEST_TO:
        log.warning("BREVO_API_KEY / BREVO_DIGEST_TO not configured -- "
                    "summary logged only, no email sent.")
        return

    try:
        resp = requests.post(
            "https://api.brevo.com/v3/smtp/email",
            headers={"api-key": BREVO_API_KEY, "Content-Type": "application/json"},
            json={
                "sender": {"name": "OpenClaw", "email": "noreply@thelocalradar.com"},
                "to": [{"email": BREVO_DIGEST_TO}],
                "subject": f"OpenClaw: {len(all_found)} new sources added to probation",
                "textContent": body,
            },
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        log.info("Summary email sent to %s", BREVO_DIGEST_TO)
    except requests.RequestException as e:
        log.error("Failed to send summary email: %s", e)


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
