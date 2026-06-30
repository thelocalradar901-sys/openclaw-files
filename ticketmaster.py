"""
ticketmaster.py — Ticketmaster Discovery API puller

Pulls events by lat/lng radius. Outputs event dicts with:
  start_utc   = UTC datetime string  "YYYY-MM-DD HH:MM:SS"
  start_local = local datetime string "YYYY-MM-DD HH:MM:SS"  (venue city timezone)
  timezone    = IANA tz string e.g. "America/Chicago"

db.py uses start_utc for _EventStartDateUTC and start_local for _EventStartDate.
Ticket links include affiliate ID 7097599.
"""

import logging
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

from config import (
    TICKETMASTER_API_KEY, TM_SEGMENTS, TM_RADIUS,
    TM_UNIT, TM_SIZE, TM_AFFILIATE_ID,
)

log = logging.getLogger("openclaw.ticketmaster")

TM_BASE = "https://app.ticketmaster.com/discovery/v2/events.json"
UTC     = ZoneInfo("UTC")


def pull_city(city: dict) -> list[dict]:
    """
    Pull all upcoming TM events for a city. Returns list of normalized
    event dicts.

    Ticketmaster's Discovery API has a hard ceiling: with size=200, only
    pages 0-4 (results 0-999) are servable -- requesting page 5+ returns
    a 400 Bad Request, regardless of how many totalPages the API claims
    exist. For a dense city across a 90-day window this ceiling can
    genuinely be hit, silently truncating events from page 5 onward on
    every single pull (confirmed in production logs for Denver/Nashville,
    2026-06-29).

    Fix: split the 90-day lookahead into 15-day chunks and query each
    chunk separately. No realistic single 15-day window for any of our
    cities should approach 1000 events, so each chunk stays safely
    within the first 5 pages.
    """
    if not TICKETMASTER_API_KEY:
        log.error("TICKETMASTER_API_KEY not set — skipping TM pull")
        return []

    lat = city.get("lat")
    lng = city.get("lng")
    if not lat or not lng:
        log.warning("No lat/lng for %s — skipping TM pull", city["name"])
        return []

    tz_name = city.get("timezone", _guess_timezone(city["slug"]))
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz     = ZoneInfo("America/Chicago")
        tz_name = "America/Chicago"

    log.info("Pulling TM for %s (%.4f, %.4f, %smi, tz=%s)",
             city["name"], lat, lng, TM_RADIUS, tz_name)

    now_utc = datetime.now(UTC)
    CHUNK_DAYS  = 15
    TOTAL_DAYS  = 90
    all_events  = []
    seen_ids    = set()  # guard against double-counting events that span a chunk boundary

    chunk_start = now_utc
    horizon     = now_utc + timedelta(days=TOTAL_DAYS)

    while chunk_start < horizon:
        chunk_end = min(chunk_start + timedelta(days=CHUNK_DAYS), horizon)
        chunk_events = _pull_window(city, lat, lng, tz, tz_name, chunk_start, chunk_end)
        for ev in chunk_events:
            ext_id = ev.get("external_id")
            if ext_id and ext_id in seen_ids:
                continue
            if ext_id:
                seen_ids.add(ext_id)
            all_events.append(ev)
        chunk_start = chunk_end

    log.info("TM pulled %d events for %s (across %d-day chunks)",
             len(all_events), city["name"], CHUNK_DAYS)
    return all_events


def _pull_window(city: dict, lat: float, lng: float, tz: ZoneInfo, tz_name: str,
                  window_start: datetime, window_end: datetime) -> list[dict]:
    """Pull all TM events within a single date window, paginating safely."""
    start_str = window_start.strftime("%Y-%m-%dT%H:%M:%SZ")
    end_str   = window_end.strftime("%Y-%m-%dT%H:%M:%SZ")

    events = []
    page   = 0
    MAX_PAGES = 5  # TM hard ceiling with size=200 (results 0-999)

    while page < MAX_PAGES:
        params = {
            "apikey":        TICKETMASTER_API_KEY,
            "latlong":       f"{lat},{lng}",
            "radius":        TM_RADIUS,
            "unit":          TM_UNIT,
            "startDateTime": start_str,
            "endDateTime":   end_str,
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
            log.error("TM API error page %d for %s [%s..%s]: %s",
                      page, city["name"], start_str, end_str, e)
            break

        # TM returns fault dict when API key is bad
        if "fault" in data:
            log.error("TM API fault for %s: %s", city["name"], data["fault"])
            break

        raw_events  = data.get("_embedded", {}).get("events", [])
        total_pages = data.get("page", {}).get("totalPages", 1)

        if not raw_events:
            break

        for raw in raw_events:
            ev = _normalize(raw, city, tz, tz_name)
            if ev:
                events.append(ev)

        log.debug("TM %s [%s..%s] page %d/%d — %d events",
                  city["name"], start_str, end_str, page + 1,
                  min(total_pages, MAX_PAGES), len(events))

        if page + 1 >= total_pages:
            break
        if page + 1 >= MAX_PAGES:
            log.warning("TM %s [%s..%s] hit %d-page safety cap -- "
                        "window may have more events than this chunk size can serve. "
                        "Consider a smaller CHUNK_DAYS if this recurs.",
                        city["name"], start_str, end_str, MAX_PAGES)
            break
        page += 1
        time.sleep(0.25)

    return events


def _normalize(raw: dict, city: dict, tz: ZoneInfo, tz_name: str) -> dict | None:
    try:
        title = (raw.get("name") or "").strip()
        if not title:
            return None

        # ── Dates ─────────────────────────────────────────────────────────────
        dates     = raw.get("dates", {})
        start_obj = dates.get("start", {})
        end_obj   = dates.get("end", {})

        # TM can provide: dateTime (ISO with tz), localDate, localTime separately
        start_utc, start_local = _parse_tm_date(start_obj, tz)
        end_utc,   end_local   = _parse_tm_date(end_obj,   tz)

        if not start_utc and not start_local:
            return None

        if not end_utc:
            end_utc   = start_utc
            end_local = start_local

        # ── Venue ─────────────────────────────────────────────────────────────
        venues        = raw.get("_embedded", {}).get("venues", [{}])
        venue         = venues[0] if venues else {}
        venue_name    = (venue.get("name") or "").strip()
        venue_address = venue.get("address", {}).get("line1", "")
        venue_city    = venue.get("city",    {}).get("name", "")
        venue_state   = venue.get("state",   {}).get("stateCode", "")
        venue_zip     = venue.get("postalCode", "")

        # Use venue timezone if TM provides it, otherwise use city default
        venue_tz = dates.get("timezone") or tz_name

        # ── Image ─────────────────────────────────────────────────────────────
        image_url = _best_image(raw.get("images", []))

        # ── Ticket URL with affiliate ID ──────────────────────────────────────
        ticket_url = raw.get("url", "")
        if ticket_url and TM_AFFILIATE_ID:
            sep = "&" if "?" in ticket_url else "?"
            ticket_url = f"{ticket_url}{sep}aaid={TM_AFFILIATE_ID}"

        # ── Price ─────────────────────────────────────────────────────────────
        cost = ""
        for pr in raw.get("priceRanges", []):
            mn = pr.get("min")
            mx = pr.get("max")
            if mn is not None:
                cost = f"${mn:.0f}–${mx:.0f}" if (mx and mx != mn) else f"${mn:.0f}"
                break

        # ── Classifications → category hints ──────────────────────────────────
        categories = []
        for cl in raw.get("classifications", []):
            for key in ("segment", "genre", "subGenre"):
                val = (cl.get(key) or {}).get("name", "")
                if val and val.lower() not in ("undefined", "") and val not in categories:
                    categories.append(val)

        description = (raw.get("info") or raw.get("pleaseNote") or "").strip()

        return {
            "title":             title,
            "description":       description,
            "start_utc":         start_utc,
            "start_local":       start_local,
            "end_utc":           end_utc,
            "end_local":         end_local,
            "timezone":          venue_tz,
            "venue_name":        venue_name,
            "venue_address":     venue_address,
            "venue_city":        venue_city,
            "venue_state":       venue_state,
            "venue_zip":         venue_zip,
            "organizer_name":    "",
            "image_url":         image_url,
            "ticket_url":        ticket_url,
            "cost":              cost,
            "source_name":       "Ticketmaster",
            "city_slug":         city["slug"],
            "categories":        categories,
            "tags":              [],
            "external_id":       f"tm_{raw.get('id', '')}",
            "_needs_enrichment": not description,
        }
    except Exception as e:
        log.warning("Failed to normalize TM event '%s': %s", raw.get("name"), e)
        return None


def _parse_tm_date(date_obj: dict, tz: ZoneInfo) -> tuple[str, str]:
    """
    Parse a TM date object into (utc_string, local_string).
    TM can provide:
      - dateTime: "2026-11-26T15:00:00Z"  or  "2026-11-26T19:00:00-05:00"
      - localDate: "2026-11-26"
      - localTime: "19:00:00"
    Returns ("", "") if nothing parseable.
    """
    if not date_obj:
        return "", ""

    fmt = "%Y-%m-%d %H:%M:%S"

    # Prefer dateTime — it has full timezone info
    dt_str = date_obj.get("dateTime", "")
    if dt_str:
        try:
            dt_utc = datetime.fromisoformat(dt_str.replace("Z", "+00:00")).astimezone(UTC)
            dt_local = dt_utc.astimezone(tz)
            return dt_utc.strftime(fmt), dt_local.strftime(fmt)
        except Exception:
            pass

    # Fall back to localDate + localTime
    local_date = date_obj.get("localDate", "")
    local_time = date_obj.get("localTime", "00:00:00")
    if local_date:
        try:
            combined = f"{local_date} {local_time[:8]}"
            dt_local = datetime.strptime(combined, fmt).replace(tzinfo=tz)
            dt_utc   = dt_local.astimezone(UTC)
            return dt_utc.strftime(fmt), dt_local.strftime(fmt)
        except Exception:
            pass

    return "", ""


def _guess_timezone(city_slug: str) -> str:
    """Best-guess IANA timezone for known cities."""
    return {
        "memphis":    "America/Chicago",
        "nashville":  "America/Chicago",
        "birmingham": "America/Chicago",
        "denver":     "America/Denver",
    }.get(city_slug, "America/Chicago")


def _best_image(images: list) -> str:
    if not images:
        return ""
    pool = [i for i in images if i.get("ratio") == "16_9"] or images
    pool = sorted(pool, key=lambda i: i.get("width", 0), reverse=True)
    return pool[0].get("url", "") if pool else ""
