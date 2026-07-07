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

import os
import requests
import logging
from time import sleep

log = logging.getLogger("openclaw.eventim")

BASE_URL = "https://prod-seetickets-core.seeticketsusa.us"
EVENTS_ENDPOINT = f"{BASE_URL}/api/v2/affiliates/events"
TICKET_TYPES_ENDPOINT = f"{BASE_URL}/api/v2/affiliates/events/{{event_id}}/ticket-types"

API_KEY = os.environ.get("EVENTIM_API_KEY")
API_SECRET = os.environ.get("EVENTIM_API_SECRET")
AFF_ID = os.environ.get("EVENTIM_AFF_ID", "40")  # affiliate ID 40 — hardcoded fallback

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


def normalize_event(raw_event):
    """
    Convert a raw AffiliateEvent object into OpenClaw's internal event dict
    shape (same fields your other scrapers produce before hitting
    make_fingerprint() / db.py's insert path). Fill in exact field names
    once we confirm the internal schema you use in scraper.py.
    """
    venue = raw_event.get("venue") or {}
    artist = raw_event.get("mainAct") or {}

    return {
        "source": "eventim",  # or "seetickets" — decide naming convention
        "external_id": raw_event.get("id"),
        "title": raw_event.get("title"),
        "description": raw_event.get("description"),
        "start_datetime": raw_event.get("eventDate") or raw_event.get("startDate"),
        "end_datetime": raw_event.get("endDate"),
        "timezone": raw_event.get("timeZone"),
        "venue_name": venue.get("name"),
        "venue_city": venue.get("city"),
        "venue_state": venue.get("state"),
        "venue_zip": venue.get("zipcode"),
        "venue_lat": venue.get("latitude"),
        "venue_lng": venue.get("longitud"),  # NOTE: API typo is "longitud", not a bug on our end
        "artist": artist.get("name"),
        "genre": raw_event.get("genre"),
        "tags": raw_event.get("tags", []),
        "affiliate_url": raw_event.get("whiteLabelUrl") or raw_event.get("regularEventUrl"),
        "min_price": raw_event.get("minTicketPrice"),
        "max_price": raw_event.get("maxTicketPrice"),
        "status": raw_event.get("eventStatus"),  # active / cancelled / rescheduled
        "is_festival": raw_event.get("isFestival", False),
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    events = fetch_all_events()
    log.info(f"Fetched {len(events)} total events from Eventim/See Tickets affiliate feed")
    for slug in TLR_CITY_FILTERS:
        city_events = filter_events_by_city(events, slug)
        log.info(f"  {slug}: {len(city_events)} matching events")
