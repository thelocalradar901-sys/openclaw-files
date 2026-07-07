"""
eventon.py

Generic adapter for venue sites running the EventON WordPress plugin
(60k+ installs -- Lafayette's is our first specimen, won't be the last).

WHY THIS EXISTS
EventON renders its calendar entirely client-side: no JSON-LD, no iCal
link, no server-rendered event markup in the initial HTML. The calendar
is populated via an AJAX POST to wp-admin/admin-ajax.php, then rendered
through Handlebars.js templates in the browser. That means our normal
tier chain (iCal -> JSON-LD -> RHP -> heuristic) will always come up
empty on these sites, and Playwright is overkill when we can just call
the same AJAX endpoint the browser calls.

STATUS: SKELETON -- needs one manual step before it's live.
EventON does not publish a single fixed AJAX action name across all
versions/configs. You need to capture the real request once:

    1. Open the target site's calendar page in a browser.
    2. DevTools -> Network -> filter "Fetch/XHR".
    3. Click to a different month (or reload) to trigger the calendar's
       own AJAX call.
    4. Find the POST to `/wp-admin/admin-ajax.php`.
    5. Copy: the `action` form field, any other form fields sent
       (commonly things like `evo_month`, `evo_year`, a nonce/`security`
       token, `type`), and the shape of the JSON response.
    6. Fill in AJAX_ACTION_CANDIDATES (or hardcode the confirmed action)
       and adjust `_parse_eventon_response()` to match the real response
       shape if it differs from the documented JSON Data structure below.

Once confirmed for one EventON site, this same action name is very
likely correct for every other EventON install (it's a fixed WordPress
hook name in the plugin's own code, not something venues customize) --
so this file should need zero further edits to cover new EventON venues,
only new `source` rows pointing at it.

Reference: EventON's own "Event API" addon documents this JSON shape for
event objects: event-id, name, start, end, details, image_url,
location_name, location_address, location_lat, location_lon,
organizer_name, all_day_event, learnmore_link, event_subtitle.
The AJAX calendar endpoint returns essentially the same event fields,
just wrapped differently depending on plugin version.
"""

import json
import logging
from datetime import datetime
from typing import Optional
from urllib.parse import urljoin

import requests

logger = logging.getLogger("openclaw.eventon")

# Fill in once confirmed via devtools. Left as a list so the probe function
# can try several in one pass if you're not sure which is live on a given
# EventON version.
AJAX_ACTION_CANDIDATES = [
    "eventon_load_events",   # placeholder guess
    "evo_get_events",        # placeholder guess
    "eventon_evcal_ajax",    # placeholder guess
    "evo_main_ajax",         # placeholder guess, per EventON's evo_ajax_headers()
]

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

DEFAULT_TIMEOUT = 15


def _admin_ajax_url(base_domain: str) -> str:
    base_domain = base_domain.rstrip("/")
    return urljoin(base_domain + "/", "wp-admin/admin-ajax.php")


def probe_ajax_endpoint(base_domain: str, month: int, year: int) -> None:
    """
    Dev helper: fire each candidate action against the target site and
    print what comes back, so you can quickly confirm which action name
    is live without re-opening devtools. Run this standalone:

        python eventon.py probe https://lafayettes.com 7 2026

    Once you find the one that returns real event JSON, move it to the
    front of AJAX_ACTION_CANDIDATES (or delete the others) and remove
    this probe call from any production path.
    """
    url = _admin_ajax_url(base_domain)
    headers = {"User-Agent": USER_AGENT}

    for action in AJAX_ACTION_CANDIDATES:
        payload = {
            "action": action,
            "evo_month": month,
            "evo_year": year,
        }
        try:
            resp = requests.post(url, data=payload, headers=headers, timeout=DEFAULT_TIMEOUT)
        except requests.RequestException as e:
            print(f"[{action}] request failed: {e}")
            continue

        snippet = resp.text[:300].replace("\n", " ")
        print(f"[{action}] status={resp.status_code} len={len(resp.text)} body[:300]={snippet}")


def fetch_events(
    base_domain: str,
    month: Optional[int] = None,
    year: Optional[int] = None,
    ajax_action: Optional[str] = None,
) -> list[dict]:
    """
    Fetch events for a given month from an EventON-powered site.

    base_domain: e.g. "https://lafayettes.com"
    month/year: defaults to current month if not given
    ajax_action: override to skip trying all candidates once confirmed

    Returns a list of normalized event dicts (see _normalize_event).
    Returns [] on failure -- caller should log/track this like any other
    source failure, not raise, to keep the scheduler loop resilient.
    """
    now = datetime.now()
    month = month or now.month
    year = year or now.year

    url = _admin_ajax_url(base_domain)
    headers = {"User-Agent": USER_AGENT}
    actions_to_try = [ajax_action] if ajax_action else AJAX_ACTION_CANDIDATES

    for action in actions_to_try:
        payload = {
            "action": action,
            "evo_month": month,
            "evo_year": year,
        }
        try:
            resp = requests.post(url, data=payload, headers=headers, timeout=DEFAULT_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
        except (requests.RequestException, json.JSONDecodeError) as e:
            logger.debug(f"eventon action '{action}' failed on {base_domain}: {e}")
            continue

        events = _parse_eventon_response(data, base_domain)
        if events:
            logger.info(f"eventon: fetched {len(events)} events from {base_domain} via action='{action}'")
            return events

    logger.warning(
        f"eventon: no working AJAX action found for {base_domain}. "
        f"Run `python eventon.py probe {base_domain} {month} {year}` and "
        f"update AJAX_ACTION_CANDIDATES."
    )
    return []


def _parse_eventon_response(data, base_domain: str) -> list[dict]:
    """
    Normalize whatever shape the AJAX response comes back in.

    EventON versions vary here -- sometimes it's {"events": [...]},
    sometimes a bare list, sometimes keyed by date ({"2026-07-12": [...]}).
    Handle the common shapes; extend as needed once you see the real
    response from probe_ajax_endpoint().
    """
    raw_events = []

    if isinstance(data, list):
        raw_events = data
    elif isinstance(data, dict):
        if "events" in data and isinstance(data["events"], list):
            raw_events = data["events"]
        else:
            # keyed-by-date shape: flatten all values that are lists
            for v in data.values():
                if isinstance(v, list):
                    raw_events.extend(v)

    normalized = []
    for raw in raw_events:
        try:
            normalized.append(_normalize_event(raw, base_domain))
        except (KeyError, TypeError) as e:
            logger.debug(f"eventon: skipping malformed event {raw!r}: {e}")
    return normalized


def _normalize_event(raw: dict, base_domain: str) -> dict:
    """
    Map an EventON JSON event object to OpenClaw's internal event dict.

    NOTE: field names on the right (title, start, end, ...) should match
    whatever insert_event() in db.py expects -- adjust to match your
    actual schema before wiring this in. Field names on the left are per
    EventON's documented "JSON Data structure" (event-id, name, start,
    end, details, image_url, location_name, location_address,
    location_lat, location_lon, organizer_name, learnmore_link,
    all_day_event).
    """
    external_id = raw.get("event-id") or raw.get("id")

    return {
        "title": raw.get("name", "").strip(),
        "description": raw.get("details", "").strip(),
        "start": _to_iso(raw.get("start")),
        "end": _to_iso(raw.get("end")),
        "all_day": bool(raw.get("all_day_event")),
        "image_url": raw.get("image_url") or None,
        "venue_name": raw.get("location_name") or None,
        "venue_address": raw.get("location_address") or None,
        "lat": raw.get("location_lat") or None,
        "lon": raw.get("location_lon") or None,
        "organizer_name": raw.get("organizer_name") or None,
        "event_url": raw.get("learnmore_link") or base_domain,
        "source_external_id": str(external_id) if external_id else None,
        "source": "eventon",
    }


def _to_iso(value) -> Optional[str]:
    """
    EventON typically sends start/end as unix timestamps (seconds).
    Adjust here if the real response uses ISO strings or milliseconds
    (see eventim.py's millisecond ISO fix for a precedent).
    """
    if value is None:
        return None
    try:
        # try unix timestamp first
        ts = int(value)
        return datetime.utcfromtimestamp(ts).isoformat()
    except (ValueError, TypeError):
        # fall back: assume it's already a parseable string
        return str(value)


if __name__ == "__main__":
    import sys

    if len(sys.argv) >= 5 and sys.argv[1] == "probe":
        _, _, domain, month_arg, year_arg = sys.argv[:5]
        probe_ajax_endpoint(domain, int(month_arg), int(year_arg))
    else:
        print("Usage: python eventon.py probe <base_domain> <month> <year>")
        print('Example: python eventon.py probe https://lafayettes.com 7 2026')
