"""
ticketmaster.py – Ticketmaster Discovery API puller for OpenClaw

Pulls events by lat/lng + radius. Normalizes to the standard event dict
that db.insert_event() expects. Affiliate links are appended automatically.
"""

import logging
import time
from datetime import datetime, timedelta, timezone

import requests

from config import TICKETMASTER_API_KEY, TM_SEGMENTS, TM_RADIUS, TM_UNIT, TM_SIZE, TM_AFFILIATE_ID

log = logging.getLogger("openclaw.ticketmaster")

TM_BASE = "https://app.ticketmaster.com/discovery/v2/events.json"


def pull_city(city: dict) -> list[dict]:
    """Pull all upcoming TM events for a city dict (must have lat, lng, slug, name)."""
    if not TICKETMASTER_API_KEY:
        log.error("TICKETMASTER_API_KEY not set — skipping Ticketmaster pull")
        return []

    lat = city.get("lat")
    lng = city.get("lng")
    if not lat or not lng:
        log.warning("No lat/lng for city %s — skipping", city["name"])
        return []

    log.info("Pulling Ticketmaster for %s (%.4f, %.4f, %s mi)", city["name"], lat, lng, TM_RADIUS)

    now      = datetime.now(timezone.utc)
    start_dt = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    end_dt   = (now + timedelta(days=90)).strftime("%Y-%m-%dT%H:%M:%SZ")

    events = []
    page   = 0

    while True:
        params = {
            "apikey":        TICKETMASTER_API_KEY,
            "latlong":       f"{lat},{lng}",
            "radius":        TM_RADIUS,
            "unit":          TM_UNIT,
            "startDateTime": start_dt,
            "endDateTime":   end_dt,
            "segmentName":   ",".join(TM_SEGMENTS),
            "size":          TM_SIZE,
            "page":          page,
            "sort":          "date,asc",
            "locale":        "*",
        }

        try:
            resp = requests.get(TM_BASE, params=params, timeout=20)
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as e:
            log.error("Ticketmaster API error (page %d): %s", page, e)
            break

        raw_events = data.get("_embedded", {}).get("events", [])
        if not raw_events:
            break

        for raw in raw_events:
            normalized = _normalize(raw, city)
            if normalized:
                events.append(normalized)

        page_info   = data.get("page", {})
        total_pages = page_info.get("totalPages", 1)
        log.debug("TM %s page %d/%d — %d events so far", city["name"], page + 1, total_pages, len(events))

        if page + 1 >= total_pages:
            break
        page += 1
        time.sleep(0.25)

    log.info("Ticketmaster: pulled %d events for %s", len(events), city["name"])
    return events


def _normalize(raw: dict, city: dict) -> dict | None:
    try:
        title = (raw.get("name") or "").strip()
        if not title:
            return None

        # Dates
        dates      = raw.get("dates", {}).get("start", {})
        start_raw  = dates.get("dateTime") or dates.get("localDate")
        if not start_raw:
            return None
        start_date = _parse_dt(start_raw)

        end_dates  = raw.get("dates", {}).get("end", {})
        end_raw    = end_dates.get("dateTime") or end_dates.get("localDate")
        end_date   = _parse_dt(end_raw) if end_raw else start_date

        # Timezone — TM gives localTimezone when available
        tz = raw.get("dates", {}).get("timezone") or city.get("timezone", "America/Chicago")

        # Venue
        venues        = raw.get("_embedded", {}).get("venues", [{}])
        venue         = venues[0] if venues else {}
        venue_name    = (venue.get("name") or "").strip()
        venue_address = venue.get("address", {}).get("line1", "")
        venue_city    = venue.get("city",    {}).get("name", "")
        venue_state   = venue.get("state",   {}).get("stateCode", "")
        venue_zip     = venue.get("postalCode", "")

        # Image — prefer 16:9, largest
        image_url = _best_image(raw.get("images", []))

        # Ticket URL with affiliate ID appended
        ticket_url = raw.get("url", "")
        if ticket_url and TM_AFFILIATE_ID:
            sep = "&" if "?" in ticket_url else "?"
            ticket_url = f"{ticket_url}{sep}aaid={TM_AFFILIATE_ID}"

        # Cost
        cost = ""
        price_ranges = raw.get("priceRanges", [])
        if price_ranges:
            pr = price_ranges[0]
            mn = pr.get("min")
            mx = pr.get("max")
            if mn is not None and mx is not None:
                cost = f"${mn:.0f}–${mx:.0f}" if mn != mx else f"${mn:.0f}"
            elif mn is not None:
                cost = f"From ${mn:.0f}"

        # Classifications → categories (db.py maps these to TLR slugs)
        categories = []
        for cl in raw.get("classifications", []):
            for key in ("segment", "genre", "subGenre"):
                val = cl.get(key, {}).get("name", "")
                if val and val not in ("Undefined", "") and val not in categories:
                    categories.append(val)

        description = raw.get("info") or raw.get("pleaseNote") or ""

        # Scraper dates come in as start_local for Ticketmaster (they're local time)
        return {
            "title":            title,
            "description":      description,
            "start_local":      start_date,
            "end_local":        end_date,
            "start_utc":        "",     # db.py will derive UTC from start_local + timezone
            "end_utc":          "",
            "timezone":         tz,
            "venue_name":       venue_name,
            "venue_address":    venue_address,
            "venue_city":       venue_city,
            "venue_state":      venue_state,
            "venue_zip":        venue_zip,
            "organizer_name":   "",
            "image_url":        image_url,
            "ticket_url":       ticket_url,
            "cost":             cost,
            "source_name":      "Ticketmaster",
            "city_slug":        city["slug"],
            "categories":       categories,
            "tags":             [],
            "external_id":      f"tm_{raw.get('id', '')}",
            "_needs_enrichment": not description,
        }
    except Exception as e:
        log.warning("Failed to normalize TM event '%s': %s", raw.get("name"), e)
        return None


def _parse_dt(raw: str) -> str:
    if not raw:
        return ""
    try:
        raw = raw.replace("Z", "+00:00")
        dt  = datetime.fromisoformat(raw)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        try:
            return datetime.strptime(raw[:10], "%Y-%m-%d").strftime("%Y-%m-%d 00:00:00")
        except ValueError:
            return raw


def _best_image(images: list) -> str:
    if not images:
        return ""
    pool = [i for i in images if i.get("ratio") == "16_9"] or images
    pool = sorted(pool, key=lambda i: i.get("width", 0), reverse=True)
    return pool[0].get("url", "") if pool else ""
