"""
eventim.py — See Tickets (Eventim US) Affiliate API scraper for OpenClaw.

API docs: prod-seetickets-core.seeticketsusa.us / Affiliates API v5.39.178
Auth: header-based (api-key + api-secret), NOT oauth/basic.
Note: EVENTIM_ACCOUNT_PASSWORD is for the affiliate web portal login only —
it is not used by this API and is not sent in any request here.

Key difference from ticketmaster.py: this feed is affId + venueId scoped,
NOT geo/lat-long scoped. There is no "search near Memphis" — you either
pull the whole affiliate feed and filter by venue.city client-side, or
target specific venueIds per metro.
"""

import requests
import logging
from time import sleep

from config import EVENTIM_API_KEY, EVENTIM_API_SECRET, EVENTIM_AFF_ID

log = logging.getLogger("openclaw.eventim")

BASE_URL = "https://prod-seetickets-core.seeticketsusa.us"
EVENTS_ENDPOINT = f"{BASE_URL}/api/v2/affiliates/events"
TICKET_TYPES_ENDPOINT = f"{BASE_URL}/api/v2/affiliates/events/{{event_id}}/ticket-types"

API_KEY = EVENTIM_API_KEY
API_SECRET = EVENTIM_API_SECRET
AFF_ID = EVENTIM_AFF_ID or "40"  # affiliate ID 40 — fallback if env/config unset

PER_PAGE = 100
MAX_PAGES = 20  # safety cap, same spirit as MAX_PAGES on ticketmaster.py

# Which of our 4 metro city names/states to keep when filtering the
# national affiliate feed client-side (see fetch_events_for_city below).
TLR_CITY_FILTERS = {
    "memphis":    {"city": "Memphis",    "state": "TN"},
    "nashville":  {"city": "Nashville",  "state": "TN"},
    "denver":     {"city": "Denver",     "state": "CO"},
    "birmingham": {"city": "Birmingham", "state": "AL"},
}


def _headers():
    if not API_KEY or not API_SECRET:
        raise RuntimeError("EVENTIM_API_KEY / EVENTIM_API_SECRET not set in environment")
    return {
        "api-key": API_KEY,
        "api-secret": API_SECRET,
    }


def _fetch_og_image(ticket_url):
    """
    Eventim/See Tickets' Affiliate API provides no image field at all
    (confirmed against AffiliateEvent schema). Best-effort fallback:
    fetch the whiteLabelUrl page itself and pull og:image, same pattern
    as ticketmaster.py's _fetch_ticketweb_image(). Silent failure,
    returns "" -- never blocks an event from saving.
    """
    if not ticket_url:
        return ""
    try:
        import requests as _rq
        from bs4 import BeautifulSoup
        headers = {
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/126.0.0.0 Safari/537.36"),
        }
        resp = _rq.get(ticket_url, headers=headers, timeout=10)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        tag = soup.find("meta", {"property": "og:image"})
        if tag and tag.get("content"):
            return tag["content"]
    except Exception as e:
        log.warning("Eventim og:image fetch failed for %s: %s", ticket_url, e)
    return ""


def _get(url, params, retries=3, backoff=2):
    """GET with simple retry/backoff, mirroring ticketmaster.py's _request pattern."""
    for attempt in range(1, retries + 1):
        resp = requests.get(url, headers=_headers(), params=params, timeout=30)
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code == 401:
            log.error("Eventim/SeeTickets 401 Unauthorized — check api-key/api-secret")
            resp.raise_for_status()
        if resp.status_code == 403:
            log.error("Eventim/SeeTickets 403 Forbidden — missing AFFILIATES_FEED_READ privilege?")
            resp.raise_for_status()
        if resp.status_code >= 500:
            log.warning(f"Eventim/SeeTickets {resp.status_code} server error, attempt {attempt}/{retries}")
            sleep(backoff * attempt)
            continue
        resp.raise_for_status()
    raise RuntimeError(f"Eventim/SeeTickets request failed after {retries} retries: {url}")


def fetch_all_events():
    """
    Pull the full affiliate feed via retrieveAll=true (simplest option — no
    manual pagination loop needed). Use this first to see actual coverage
    before deciding whether per-venueId targeting is worth building.
    """
    if not AFF_ID:
        raise RuntimeError(
            "EVENTIM_AFF_ID not set. This is your affiliate ID number from the "
            "See Tickets affiliate portal — separate from your API key/secret."
        )

    params = {
        "affId": AFF_ID,
        "retrieveAll": "true",
    }
    data = _get(EVENTS_ENDPOINT, params)
    return data.get("data", [])


def fetch_events_paginated():
    """
    Manual pagination path (use if retrieveAll=true times out or the feed
    is too large to pull in one shot).
    """
    if not AFF_ID:
        raise RuntimeError("EVENTIM_AFF_ID not set")

    all_events = []
    continuation_token = None
    page = 0

    while page < MAX_PAGES:
        params = {"affId": AFF_ID, "perPage": PER_PAGE}
        if continuation_token:
            params["continuationToken"] = continuation_token

        data = _get(EVENTS_ENDPOINT, params)
        events = data.get("data", [])
        all_events.extend(events)

        continuation_token = data.get("meta", {}).get("pagination", {}).get("continuationToken")
        page += 1

        if not continuation_token or not events:
            break

    return all_events


def filter_events_by_city(events, city_slug):
    """
    Client-side city filter, since the API has no geo param.
    Matches against venue.city / venue.state from AffiliateEvent.venue.
    """
    target = TLR_CITY_FILTERS.get(city_slug)
    if not target:
        raise ValueError(f"Unknown city_slug: {city_slug}")

    matched = []
    for ev in events:
        venue = ev.get("venue") or {}
        if (venue.get("city", "").strip().lower() == target["city"].lower()
                and venue.get("state", "").strip().upper() == target["state"]):
            matched.append(ev)
    return matched


def normalize_event(raw_event, city_slug):
    """
    Convert a raw AffiliateEvent object into OpenClaw's internal event dict
    shape, following ticketmaster.py's pattern (start_utc/start_local/
    timezone) rather than the generic _ev() pattern used by html scrapers --
    because, like TM, Eventim/See Tickets gives us full ISO datetime plus
    an explicit IANA timezone per event, so we can pre-compute both here
    instead of making db.py guess from a bare local date string.

    NOTE ON NAMING: this is the official Affiliate API feed, a completely
    different data path from the existing _scrape_seetickets() in
    scraper.py (which scrapes individual venue HTML pages on the See
    Tickets platform as a tertiary source, no affiliate tracking).
    Deliberately using "eventim" as source_name/external_id prefix here,
    NOT "seetickets", to avoid confusing the two in logs/DB/fingerprints.
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo

    venue = raw_event.get("venue") or {}
    artist = raw_event.get("mainAct") or {}

    tz_name = raw_event.get("timeZone") or "America/Chicago"
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("America/Chicago")
        tz_name = "America/Chicago"

    def _parse(dt_str):
        """Returns (utc_str, local_str) in 'YYYY-MM-DD HH:MM:SS', or ("","")."""
        if not dt_str:
            return "", ""
        try:
            # API examples show offset-style ISO e.g. "2022-08-10T08:14:05+0000"
            dt = datetime.strptime(dt_str, "%Y-%m-%dT%H:%M:%S%z")
            dt_utc = dt.astimezone(ZoneInfo("UTC"))
            dt_local = dt_utc.astimezone(tz)
            fmt = "%Y-%m-%d %H:%M:%S"
            return dt_utc.strftime(fmt), dt_local.strftime(fmt)
        except Exception:
            log.warning("Eventim: couldn't parse date '%s' for event %s",
                        dt_str, raw_event.get("id"))
            return "", ""

    # eventDate is used for single-date events; startDate for multi-date
    start_utc, start_local = _parse(raw_event.get("eventDate") or raw_event.get("startDate"))
    end_utc, end_local = _parse(raw_event.get("endDate"))
    if not end_utc:
        end_utc, end_local = start_utc, start_local

    if not start_utc and not start_local:
        return None  # matches ticketmaster.py's "skip unparseable dates" rule

    # ── Price ────────────────────────────────────────────────────────────
    cost = ""
    mn = raw_event.get("minTicketPrice")
    mx = raw_event.get("maxTicketPrice")
    if mn is not None:
        cost = f"${mn}-${mx}" if (mx and mx != mn) else f"${mn}"

    # ── Categories (genre + festival/music flags, mirrors TM's classifications) ──
    categories = []
    genre = raw_event.get("genre")
    if genre:
        categories.append(genre)

    ticket_url = raw_event.get("whiteLabelUrl") or raw_event.get("regularEventUrl") or ""
    image_url = _fetch_og_image(ticket_url)

    return {
        "title": (raw_event.get("title") or "").strip(),
        "description": (raw_event.get("description") or "").strip(),
        "start_utc": start_utc,
        "start_local": start_local,
        "end_utc": end_utc,
        "end_local": end_local,
        "timezone": tz_name,
        "venue_name": venue.get("name", ""),
        "venue_address": venue.get("street", ""),
        "venue_city": venue.get("city", ""),
        "venue_state": venue.get("state", ""),
        "venue_zip": venue.get("zipcode", ""),
        "organizer_name": "",
        "image_url": image_url,
        "ticket_url": ticket_url,
        "cost": cost,
        "source_name": "Eventim",
        "city_slug": city_slug,
        "categories": categories,
        "tags": raw_event.get("tags", []),
        "external_id": f"eventim_{raw_event.get('id', '')}",
        "_needs_enrichment": not raw_event.get("description"),
    }


def pull_all_cities() -> dict:
    """
    Fetches the national feed ONCE, then filters/normalizes per city.
    Returns {city_slug: [event_dicts]}. This is the entry point scheduler.py
    should use -- registered as a single job, not one-per-city like TM --
    since unlike TM there's no per-city API call to make, so doing it
    per-city would mean 4 redundant full-feed fetches per tick.
    """
    raw_events = fetch_all_events()
    result = {}
    for city_slug in TLR_CITY_FILTERS:
        city_raw = filter_events_by_city(raw_events, city_slug)
        events = []
        for raw in city_raw:
            ev = normalize_event(raw, city_slug)
            if ev:
                events.append(ev)
        result[city_slug] = events
        log.info("Eventim: %d events for %s", len(events), city_slug)
    return result


def pull_city(city: dict) -> list[dict]:
    """
    Matches ticketmaster.py's pull_city(city) interface so scheduler.py can
    register this the same way TM is registered. Pulls the full national
    feed (cheap: ~6.5k events, one API call) then filters to this city.

    NOTE: unlike TM, there's no per-city API call to make -- the whole
    feed comes back in one shot regardless of city, so if this runs for
    all 4 cities on the same schedule tick, consider caching the full
    fetch_all_events() result once per run rather than calling it 4x.
    """
    city_slug = city.get("slug", "") if isinstance(city, dict) else str(city)
    if city_slug not in TLR_CITY_FILTERS:
        log.warning("Eventim: no city filter configured for slug '%s'", city_slug)
        return []

    raw_events = fetch_all_events()
    city_raw = filter_events_by_city(raw_events, city_slug)

    events = []
    for raw in city_raw:
        ev = normalize_event(raw, city_slug)
        if ev:
            events.append(ev)

    log.info("Eventim pulled %d events for %s", len(events), city.get("name", city_slug))
    return events


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    events = fetch_all_events()
    log.info(f"Fetched {len(events)} total events from Eventim/See Tickets affiliate feed")
    for slug in TLR_CITY_FILTERS:
        city_events = filter_events_by_city(events, slug)
        log.info(f"  {slug}: {len(city_events)} matching events")
        # Spot-check the first normalized event so we can see real field
        # values before wiring into db.py
        if city_events:
            sample = normalize_event(city_events[0], slug)
            log.info(f"    sample normalized event: {sample}")
