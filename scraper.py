"""
scraper.py — OpenClaw HTML/API event scraper

Source types (configured in openclaw-monitor WP plugin):
  html_auto    — Universal: tries iCal → multi-iCal → JSON-LD → RHP →
                 heuristic CSS → Ollama AI
  seetickets   — SeeTickets embedded widget
  tec_rest     — WordPress TEC REST API (/wp-json/tribe/events/v1/events)
  ical_url     — Bare .ics URL
  json_api     — Generic paginated JSON endpoint
  generic_html — CSS selector-based with Ollama fallback

All fetchers return list of dicts with these keys:
  title, description, start_date, end_date, image_url, ticket_url,
  venue_name, venue_address, venue_city, venue_state, venue_zip,
  organizer_name, cost, source_name, city_slug, categories, tags,
  external_id, _needs_enrichment

2026-07-15 pass: added retry/backoff on all network fetches, a per-event
iCal harvesting tier for Squarespace-style collections (Riverside Revival),
a date-range parsing fix (Parker Arts-style "Month D - Month D, YYYY"),
an orphan-heading fallback for sites that link the poster image instead of
the title itself (Graceland Live/Wix), a shared year-inference helper used
by both the RHP and heuristic tiers, and a today's-date anchor in the
Ollama prompt so year-less date text doesn't get silently mis-dated there
either. See inline comments at each change for the site/incident that
motivated it.
"""

import logging
import random
import re
import time
from datetime import datetime, date, timedelta as _timedelta, timezone as _timezone
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from dateutil import parser as dp_parser

log = logging.getLogger("openclaw.scraper")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; OpenClaw/1.0; +https://thelocalradar.com)",
    "Accept":     "text/html,application/xhtml+xml,*/*;q=0.9",
}
TIMEOUT = 20

# A small number of sources (so far: Visit Music City / visitmusiccity.com)
# return 403 Forbidden specifically to the transparent OpenClaw User-Agent
# above, while a plain browser UA gets through fine on the exact same URL.
# This is opt-in PER SOURCE via a "browser_ua": true flag in
# wp_openclaw_sources -- it deliberately does not change the default
# HEADERS used everywhere else, so every other source keeps identifying
# itself honestly as OpenClaw unless a source specifically needs this.
BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Accept":     "text/html,application/xhtml+xml,*/*;q=0.9",
}


def _headers_for(source: dict) -> dict:
    """Returns BROWSER_HEADERS only if this source has browser_ua=True set,
    otherwise the normal transparent OpenClaw HEADERS used by default."""
    return BROWSER_HEADERS if source.get("browser_ua") else HEADERS


def _ajax_headers_for(source: dict) -> dict:
    """
    Headers for AJAX-endpoint fetches (see _scrape_ajax_paginate). A real
    browser's own JS makes XHR/fetch calls with an X-Requested-With
    header and a JSON-favoring Accept header, distinct from what it sends
    for an ordinary page load.

    Confirmed real-world trigger: Ryman Auditorium's events_ajax endpoint
    returned 406 Not Acceptable to the normal page-navigation-shaped
    Accept header used everywhere else in this file ("text/html,
    application/xhtml+xml,*/*;q=0.9"), while the EXACT SAME URL succeeded
    both for a real browser's XHR call and for a bare `curl` with no
    Accept header override at all. That rules out an IP/ASN block (curl
    succeeded from the same box) -- the specific "text/html first" Accept
    header is what's triggering the server's content-negotiation
    rejection, not the absence of browser-like headers in general.
    """
    base = _headers_for(source)
    return {
        **base,
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest",
    }


# ── Network helper ────────────────────────────────────────────────────────────

def _request_get(url, *, headers=None, params=None, timeout=TIMEOUT,
                  retries=2, source_name=""):
    """
    Wrapper around requests.get() with a short retry/backoff for transient
    failures -- timeouts, connection resets, and 429/5xx responses.

    A meaningful chunk of "rejected" sources aren't structurally broken at
    all -- they're flaky infrastructure: a venue CMS timing out under
    load, a CDN blip, a momentary 503 during our own scrape window. One
    retry with a short backoff recovers most of those for free, without
    adding real latency to the sources that already succeed on the first
    try (the common case).

    Does NOT retry plain 4xx errors other than 429 -- a 404 or 403 isn't
    going to fix itself by asking again, and browser_ua-style blocks
    should be solved with that flag, not by hammering the source.
    """
    last_exc = None
    for attempt in range(retries + 1):
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=timeout)
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt < retries:
                    wait = (2 ** attempt) + random.uniform(0, 0.5)
                    log.info("[%s] HTTP %d on attempt %d/%d for %s -- retrying in %.1fs",
                             source_name, resp.status_code, attempt + 1, retries + 1, url, wait)
                    time.sleep(wait)
                    continue
            resp.raise_for_status()
            return resp
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            last_exc = e
            if attempt < retries:
                wait = (2 ** attempt) + random.uniform(0, 0.5)
                log.info("[%s] %s on attempt %d/%d for %s -- retrying in %.1fs",
                         source_name, type(e).__name__, attempt + 1, retries + 1, url, wait)
                time.sleep(wait)
                continue
            raise
    if last_exc:
        raise last_exc
    # Unreachable in practice (raise_for_status() above always either
    # returns or raises), but keeps the type checker/callers honest.
    raise RuntimeError(f"_request_get exhausted retries for {url}")


# ── Entry point ───────────────────────────────────────────────────────────────

def scrape_source(source: dict, city) -> list[dict]:
    if isinstance(city, dict):
        city_slug = city.get("slug", "")
        city_name = city.get("name", "")
        tz_name   = city.get("timezone", "America/Chicago")
    else:
        city_slug = str(city)
        city_name = str(city).title()
        tz_name   = "America/Chicago"

    stype = (source.get("source_type") or "html_auto").lower().strip()
    if stype in ("auto", "squarespace"):
        stype = "html_auto"

    log.info("Scraping '%s' (%s) for %s", source.get("name", source.get("url")), stype, city_name)

    try:
        if stype == "html_auto":
            events = _scrape_html_auto(source, city_slug, city_name, tz_name)
        elif stype == "seetickets":
            events = _scrape_seetickets(source, city_slug, city_name)
        elif stype == "tec_rest":
            events = _scrape_tec_rest(source, city_slug, city_name)
        elif stype == "ical_url":
            events = _scrape_ical_url(source, city_slug, city_name, tz_name)
        elif stype == "json_api":
            events = _scrape_json_api(source, city_slug, city_name)
        elif stype == "generic_html":
            events = _scrape_generic_html(source, city_slug, city_name)
        elif stype == "ajax_paginate":
            events = _scrape_ajax_paginate(source, city_slug, city_name)
        else:
            log.warning("Unknown source_type '%s' — trying html_auto", stype)
            events = _scrape_html_auto(source, city_slug, city_name, tz_name)
    except Exception as e:
        log.error("scrape_source crashed for '%s': %s", source.get("url"), e, exc_info=True)
        return []

    # Optional per-source filter: skip events with no image. Opt-in via
    # "require_image": true in wp_openclaw_sources.notes JSON config --
    # off by default so this never silently changes existing sources'
    # behavior. Applied here (not per-tier) so it works uniformly no
    # matter which scraping tier actually produced the events.
    if source.get("require_image"):
        before = len(events)
        events = [e for e in events if e.get("image_url")]
        dropped = before - len(events)
        if dropped:
            log.info("[%s] require_image filter: dropped %d/%d events with no image",
                     source.get("name"), dropped, before)

    return events


# ── Event dict factory ────────────────────────────────────────────────────────

def _ev(title, description, start_date, end_date, venue_name, ticket_url,
        source, city_slug, city_name,
        image_url="", cost="", external_id="", needs_enrichment=False) -> dict:
    return {
        "title":             title,
        "description":       description,
        "start_date":        start_date,
        "end_date":          end_date or start_date,
        "venue_name":        venue_name,
        "venue_address":     "",
        "venue_city":        city_name,
        "venue_state":       "",
        "venue_zip":         "",
        "organizer_name":    "",
        "image_url":         image_url,
        "ticket_url":        ticket_url,
        "cost":              cost,
        "source_name":       source.get("name", source.get("url", "")),
        "city_slug":         city_slug,
        "categories":        [],
        "tags":              [],
        "external_id":       external_id,
        "_needs_enrichment": needs_enrichment,
    }


# ── Title cleanup ─────────────────────────────────────────────────────────────

_TITLE_JUNK_SUFFIXES = re.compile(
    r"\s*[|\-–—]\s*(?:Tickets?|Buy Tickets?|Eventbrite|Ticketmaster|"
    r"Live Nation|See Tickets|AXS|Etix)\s*$",
    re.IGNORECASE,
)


def _clean_title(title: str) -> str:
    """Strip common boilerplate ticketing-platform suffixes some sites glue
    onto their heading/title text (e.g. 'Fred Eaglesmith - Tickets' or
    'Elmiene | Ticketmaster'), so events don't carry that noise into the
    title field that just has to get cleaned up again downstream."""
    return _TITLE_JUNK_SUFFIXES.sub("", title or "").strip()


# ── Relay fetch (ASN/IP-reputation blocks) ────────────────────────────────────
#
# Some sources block requests from the Hetzner IP range outright, at the
# firewall/WAF level, regardless of User-Agent -- confirmed on Parker Arts
# (403 on both the transparent OpenClaw UA and a full browser UA from the
# same box). browser_ua solves sites that fingerprint on header content;
# this solves sites that fingerprint on WHERE the request is coming from.
# Same underlying idea as the existing ticketweb-image-relay Cloudflare
# Worker (which solves this for TicketWeb's *images* specifically) --
# generalized here into a reusable full-page relay any source can opt
# into, instead of building a new one-off Worker per blocked site.
#
# Opt-in per source via {"html_relay": true} in wp_openclaw_sources.notes,
# same convention as browser_ua/require_image. Requires HTML_RELAY_URL to
# be set in config.py / openclaw.env pointing at the deployed Worker (see
# accompanying cloudflare-html-relay-worker.js). If it isn't configured,
# this logs a warning once and falls straight back to a direct fetch
# rather than crashing the source -- a relay-flagged source with no relay
# configured yet should degrade to "still 403s, same as before," not
# "throws."

def _relay_get(url, *, timeout=TIMEOUT, source_name=""):
    from urllib.parse import quote
    try:
        from config import HTML_RELAY_URL
    except Exception:
        HTML_RELAY_URL = ""

    if not HTML_RELAY_URL:
        log.warning("[%s] html_relay requested but HTML_RELAY_URL is not configured -- "
                    "falling back to a direct fetch (will likely still be blocked)",
                    source_name)
        return _request_get(url, headers=HEADERS, timeout=timeout, source_name=source_name)

    relay_url = f"{HTML_RELAY_URL.rstrip('/')}/?url={quote(url, safe='')}"
    # Give the relay itself a little extra headroom beyond our own timeout,
    # since it's making its own outbound fetch on top of ours.
    return _request_get(relay_url, timeout=timeout + 10, source_name=f"{source_name} (relay)")


def _fetch_source_page(source: dict, url: str, timeout=TIMEOUT):
    """Fetch a source's page, routing through the relay if this source has
    html_relay=true configured (see _relay_get docstring)."""
    if source.get("html_relay"):
        return _relay_get(url, timeout=timeout, source_name=source.get("name", ""))
    return _request_get(url, headers=_headers_for(source), timeout=timeout,
                         source_name=source.get("name", ""))


# ── html_auto: universal, multi-tier ──────────────────────────────────────────

def _scrape_html_auto(source: dict, city_slug: str, city_name: str,
                      tz_name: str = "America/Chicago") -> list[dict]:
    url = source["url"]
    try:
        resp = _fetch_source_page(source, url, timeout=TIMEOUT)
    except Exception as e:
        log.warning("Fetch failed for %s: %s", source.get("name"), e)
        return []

    html    = resp.text
    no_ical = source.get("no_ical", False)

    # Tier 1: iCal (single collection-level feed)
    ical_url = _detect_ical(html, url, no_ical=no_ical)
    if ical_url:
        events = _parse_ical(ical_url, source, city_slug, city_name, tz_name)
        if events:
            log.info("[%s] iCal: %d events", source.get("name"), len(events))
            return events

    # Tier 1.5: multiple per-event iCal exports. Squarespace event
    # collections (confirmed on Riverside Revival) don't expose a single
    # calendar-level feed the way Tier 1 looks for -- but every individual
    # event carries its own "?format=ical" export link right on the
    # listing page. Tier 1's _detect_ical deliberately ignores those
    # (a lone per-event link should never be mistaken for a full-calendar
    # feed) -- this tier is the deliberate "yes, and": if MANY per-event
    # ical links share the same parent path, that's a strong signal this
    # whole page is exactly that kind of listing, so fetch each one.
    if not no_ical:
        event_ical_urls = _detect_multi_ical(html, url)
        if event_ical_urls:
            events = _parse_multi_ical(event_ical_urls, source, city_slug, city_name, tz_name)
            if events:
                log.info("[%s] Multi-iCal: %d events from %d per-event feeds",
                         source.get("name"), len(events), len(event_ical_urls))
                return events

    # Tier 2: JSON-LD
    events = _parse_jsonld(html, source, city_slug, city_name)
    if events:
        log.info("[%s] JSON-LD: %d events", source.get("name"), len(events))
        return events

    # Tier 2.5: RHP events plugin (seen on Hi Tone Cafe and possibly other
    # venue sites running the same WP plugin). Distinct, stable markup:
    # an <a id="eventTitle" class="url" href=".../event/.../"> WRAPS an
    # <h2 class="... rhp-event__title--list ...">, which is the opposite
    # nesting of what the generic heading-fallback heuristic expects (it
    # looks for an <a> inside the heading, not the heading inside an <a>).
    # Cheap to detect, so try it before falling to the slower/fuzzier
    # generic heuristic tier.
    events = _parse_rhp(html, url, source, city_slug, city_name, tz_name)
    if events:
        log.info("[%s] RHP plugin: %d events", source.get("name"), len(events))
        return events

    # Tier 3: Heuristic CSS
    events = _parse_heuristic(html, url, source, city_slug, city_name, tz_name)
    if events:
        log.info("[%s] Heuristic: %d events", source.get("name"), len(events))
        return events

    # Tier 4: Ollama
    log.info("[%s] Trying Ollama", source.get("name"))
    events = _ollama_extract(html, source, city_slug, city_name)
    if events:
        log.info("[%s] Ollama: %d events", source.get("name"), len(events))
    else:
        log.warning("[%s] All tiers failed", source.get("name"))
    return events


def _detect_ical(html: str, base_url: str, no_ical: bool = False):
    """Find a calendar-level iCal feed. Ignores individual event ICS links --
    see _detect_multi_ical for how those are handled instead."""
    if no_ical:
        return None
    from urllib.parse import urlparse
    soup = BeautifulSoup(html, "html.parser")
    tag  = soup.find("link", {"type": "text/calendar"})
    if tag and tag.get("href"):
        return _abs(tag["href"], base_url)
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if ".ics" in href or "format=ical" in href:
            abs_href = _abs(href, base_url)
            path     = urlparse(abs_href).path.rstrip("/")
            # Only use calendar-level feeds — skip individual event pages
            depth = len([s for s in path.split("/") if s])
            if depth <= 1:
                return abs_href
    return None


def _detect_multi_ical(html: str, base_url: str, min_links: int = 3):
    """
    Detect a Squarespace-style events collection: many individual event
    pages each exposing their own "?format=ical" (or "*.ics") export
    link, all sharing the same immediate parent path (i.e. siblings in
    the same collection), rather than one link scattered somewhere
    unrelated on the page.

    min_links guards against a page that just happens to have one or two
    stray ".ics" links (e.g. a single "add this one event to your
    calendar" button) -- that's not "the whole page is a listing of
    these," just isolated confetti, and shouldn't trigger a bulk fetch.

    Returns a list of absolute per-event iCal URLs, or [] if nothing
    qualifies.
    """
    from urllib.parse import urlparse
    soup = BeautifulSoup(html, "html.parser")
    hits: dict[str, list[str]] = {}
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "format=ical" not in href and not href.endswith(".ics"):
            continue
        abs_href = _abs(href, base_url)
        path     = urlparse(abs_href).path.rstrip("/")
        parts    = [p for p in path.split("/") if p]
        if len(parts) < 2:
            continue  # calendar-level -- _detect_ical already handles this case
        parent_path = "/".join(parts[:-1])
        hits.setdefault(parent_path, []).append(abs_href)

    if not hits:
        return []
    _parent_path, links = max(hits.items(), key=lambda kv: len(kv[1]))
    if len(links) < min_links:
        return []
    # de-dupe while preserving order (a page can link the same event's
    # ICS export more than once -- e.g. once from a thumbnail, once from
    # a "add to calendar" button)
    seen, ordered = set(), []
    for link in links:
        if link not in seen:
            seen.add(link)
            ordered.append(link)
    return ordered


# Backstop: a listing page detected as having more than this many
# individual per-event ICS links to fetch one-by-one is either
# mis-detected or genuinely needs a real calendar-level feed found some
# other way -- don't hammer the site fetching that many individual pages
# every scrape cycle.
MAX_MULTI_ICAL_EVENTS = 75


def _parse_multi_ical(urls: list, source: dict, city_slug: str, city_name: str,
                      tz_name: str = None) -> list[dict]:
    events = []
    for ical_url in urls[:MAX_MULTI_ICAL_EVENTS]:
        events.extend(_parse_ical(ical_url, source, city_slug, city_name, tz_name))
        # Same courtesy delay already used between paginated requests
        # elsewhere in this file (TEC REST, JSON API) -- we're about to
        # make up to MAX_MULTI_ICAL_EVENTS individual requests to the
        # same host back-to-back.
        time.sleep(0.3)
    return events


def _fetch_og_image(url: str) -> str:
    """
    Best-effort og:image fetch for sources whose own feed carries no image.

    iCal as a format has no image field at all -- DTSTART/SUMMARY/
    DESCRIPTION/LOCATION/URL only -- so every event out of _parse_ical
    would otherwise always have image_url="". Most WordPress event
    plugins (TEC included, confirmed on Bham Now) auto-populate an
    og:image meta tag on the individual event page regardless, so
    following the event's own ticket_url and reading that one tag back
    recovers it cheaply without needing a second, heavier scrape tier.

    Failure is always silent and returns "" -- a slow/blocking/missing
    og:image on one event's page must never break that event from
    being saved with everything else it already has.
    """
    if not url:
        return ""
    try:
        resp = _request_get(url, headers=HEADERS, timeout=10, retries=1)
        soup = BeautifulSoup(resp.text, "html.parser")
        tag  = soup.find("meta", {"property": "og:image"})
        if tag and tag.get("content"):
            return tag["content"]
    except Exception as e:
        log.debug("og:image fetch failed for %s: %s", url, e)
    return ""


def _parse_ical(ical_url: str, source: dict, city_slug: str, city_name: str,
                tz_name: str = None) -> list[dict]:
    """Parse an iCal feed, converting UTC datetimes to local city time."""
    try:
        from icalendar import Calendar
        try:
            local_tz = ZoneInfo(tz_name) if tz_name else ZoneInfo("UTC")
        except Exception:
            local_tz = ZoneInfo("UTC")

        resp = _request_get(ical_url, headers=_headers_for(source), timeout=TIMEOUT,
                             source_name=source.get("name", ""))
        cal    = Calendar.from_ical(resp.content)
        events = []
        for comp in cal.walk():
            if comp.name != "VEVENT":
                continue
            title   = str(comp.get("SUMMARY", "")).strip()
            dtstart = comp.get("DTSTART")
            if not title or not dtstart:
                continue
            dt = dtstart.dt
            # All-day date (no time) — use midnight local
            if isinstance(dt, date) and not isinstance(dt, datetime):
                dt = datetime(dt.year, dt.month, dt.day, tzinfo=local_tz)
            # Timezone-aware datetime — convert to local
            elif hasattr(dt, "tzinfo") and dt.tzinfo is not None:
                dt = dt.astimezone(local_tz)
            # Naive datetime — assume already local, leave as-is
            start = dt.strftime("%Y-%m-%d %H:%M:%S")

            dtend = comp.get("DTEND")
            edt   = dtend.dt if dtend else dt
            if isinstance(edt, date) and not isinstance(edt, datetime):
                edt = datetime(edt.year, edt.month, edt.day, tzinfo=local_tz)
            elif hasattr(edt, "tzinfo") and edt.tzinfo is not None:
                edt = edt.astimezone(local_tz)
            end = edt.strftime("%Y-%m-%d %H:%M:%S")

            events.append(_ev(
                title=_clean_title(title),
                description=str(comp.get("DESCRIPTION", "")).strip(),
                start_date=start, end_date=end,
                venue_name=str(comp.get("LOCATION", source.get("name", ""))).strip(),
                ticket_url=str(comp.get("URL", "")).strip(),
                source=source, city_slug=city_slug, city_name=city_name,
            ))

        # iCal carries no image field of its own (see _fetch_og_image
        # docstring) -- backfill from each event's own page only for the
        # events that actually need it, so sources that DO somehow have
        # an image already (e.g. a future tier change) aren't re-fetched
        # needlessly.
        for ev in events:
            if not ev.get("image_url") and ev.get("ticket_url"):
                ev["image_url"] = _fetch_og_image(ev["ticket_url"])

        return events
    except Exception as e:
        log.warning("iCal parse failed for %s: %s", ical_url, e)
        return []


def _parse_jsonld(html: str, source: dict, city_slug: str, city_name: str) -> list[dict]:
    import json
    soup   = BeautifulSoup(html, "html.parser")
    events = []
    for script in soup.find_all("script", {"type": "application/ld+json"}):
        try:
            data = json.loads(script.string or "")
        except Exception:
            continue
        items = data if isinstance(data, list) else [data]
        if isinstance(data, dict) and "@graph" in data:
            items = data["@graph"]

        # Multi-day festivals/series sometimes nest individual dates under
        # a parent Event's "subEvent" array instead of listing them as
        # top-level Event objects. Flatten those in too, so a single
        # umbrella listing doesn't silently collapse into one entry (or
        # get skipped for missing a usable startDate on the parent).
        expanded = []
        for item in items:
            if isinstance(item, dict) and "Event" in str(item.get("@type", "")):
                expanded.append(item)
                sub = item.get("subEvent")
                if isinstance(sub, dict):
                    sub = [sub]
                if isinstance(sub, list):
                    expanded.extend(s for s in sub if isinstance(s, dict))
        items = expanded or items

        for item in items:
            if not isinstance(item, dict):
                continue
            if "Event" not in str(item.get("@type", "")):
                continue
            title     = _clean_title(item.get("name", "").strip())
            start_raw = item.get("startDate", "")
            if not title or not start_raw:
                continue
            loc        = item.get("location", {})
            venue_name = (loc.get("name", source.get("name", ""))
                          if isinstance(loc, dict) else source.get("name", ""))
            img        = item.get("image")
            image_url  = (
                img if isinstance(img, str)
                else img.get("url", "") if isinstance(img, dict)
                else (img[0] if isinstance(img, list) and img and isinstance(img[0], str)
                      else img[0].get("url", "") if isinstance(img, list) and img else "")
            )
            events.append(_ev(
                title=title,
                description=item.get("description", "").strip(),
                start_date=_normalize_dt(start_raw),
                end_date=_normalize_dt(item.get("endDate", start_raw)),
                venue_name=venue_name,
                ticket_url=item.get("url", item.get("@id", "")).strip(),
                source=source, city_slug=city_slug, city_name=city_name,
                image_url=image_url,
            ))
    return events


_EVENT_SELECTORS = [
    "article.eventlist-event", "li.eventlist-event",
    "[class*='event-item']", "[class*='event_item']",
    "[class*='eventCard']", "[class*='event-card']",
    "[class*='event-list-item']", "[class*='tribe-event']",
    "article[class*='event']", "li[class*='event']", ".vevent",
]


_MONTH_YEAR_RE = re.compile(
    r"\b(January|February|March|April|May|June|July|August|September|"
    r"October|November|December)\s+(\d{4})\b", re.IGNORECASE
)

# Loose date fragment with no year, e.g. "Thu, Jun 18" or "Friday, June 18".
_LOOSE_DATE_RE = re.compile(
    r"\b(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*,?\s+"
    r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2}\b",
    re.IGNORECASE,
)

# Full month name + day + 4-digit year, no day-of-week prefix (e.g. Ryman/
# AXS-style "June 19, 2026 7:00 PM"). Distinct from _LOOSE_DATE_RE above,
# which requires a leading day-of-week and has no year of its own (used
# for Etix-style "Thu, Jun 18" dates that need year-inference anchoring
# instead). This pattern already carries its own year, so no inference
# is needed when it matches.
_LOOSE_DATE_WITH_YEAR_RE = re.compile(
    r"\b(?:January|February|March|April|May|June|July|August|September|"
    r"October|November|December)\s+\d{1,2},?\s+\d{4}"
    r"(?:\s+\d{1,2}:\d{2}\s*(?:am|pm))?\b",
    re.IGNORECASE,
)

# Date RANGE with a single trailing year, e.g. "June 26 - July 19, 2026"
# or "May 30 - August 8, 2026" (Parker Arts-style season/exhibit runs).
# Without this, _LOOSE_DATE_WITH_YEAR_RE above would match the trailing
# "July 19, 2026" fragment instead -- silently picking the LAST day of
# the run as an event's start_date instead of the first, which is wrong
# for anything that isn't a single-day show (multi-week exhibits,
# extended musical/theater runs, festival date spans).
_DATE_RANGE_RE = re.compile(
    r"\b((?:January|February|March|April|May|June|July|August|September|"
    r"October|November|December)\s+\d{1,2})\s*[-–—]\s*"
    r"(?:January|February|March|April|May|June|July|August|September|"
    r"October|November|December)\s+\d{1,2},?\s+(\d{4})\b",
    re.IGNORECASE,
)

# "Show: 7 pm" preferred over "Doors: 6 pm" as the actual event start time.
_SHOW_TIME_RE = re.compile(
    r"Show:?\s*(\d{1,2}(?::\d{2})?\s*(?:am|pm))", re.IGNORECASE
)
_DOORS_TIME_RE = re.compile(
    r"Doors:?\s*(\d{1,2}(?::\d{2})?\s*(?:am|pm))", re.IGNORECASE
)

# Etix/Hi-Tone-style headings embed the date INSIDE the title link text,
# e.g. "DIY Memphis Presents: [Big Room] Thu, Jun 18". Strips a trailing
# "[Dow,] Mon DD" fragment off so it doesn't end up glued onto the title.
_TRAILING_DATE_RE = re.compile(
    r"\s+(?:(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*,?\s+)?"
    r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2}$",
    re.IGNORECASE,
)

# Price line: blank = free, "$X" = flat, "$X to $Y" = range.
_PRICE_RE = re.compile(r"\$\s*([\d.]+)(?:\s*to\s*\$?\s*([\d.]+))?", re.IGNORECASE)

# Priority order for picking the primary ticket link out of a container
# that may have Buy Tickets/Free Show, RSVP, and More Info links all
# pointing different places. Lower index wins.
_TICKET_LINK_PRIORITY = ("buy ticket", "free show", "get ticket", "ticket", "rsvp")

# Tags commonly used for section headings on calendar pages (month/year
# dividers like "June 2026"). Checked against tag name during the
# document-order walk in _parse_heuristic.
_HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}

# CSS background-image url(...) -- fallback image source for sites that
# render posters as a div's background-image instead of a real <img>.
_BG_IMAGE_RE = re.compile(r"background-image:\s*url\(\s*['\"]?(.*?)['\"]?\s*\)", re.IGNORECASE)


def _section_anchor_date(month_name: str, year: str):
    """Build a default datetime anchored to the 1st of the given month/year,
    used to fill in missing year (and month) components when an event's
    visible date text has no year, e.g. 'Thu, Jun 18'."""
    try:
        return dp_parser.parse(f"1 {month_name} {year}")
    except Exception:
        return None


def _year_inferred_default(date_str: str):
    """
    Build a default anchor datetime for a date string that has no year of
    its own (e.g. "Thu, Jun 18" or "FRIDAY, JULY 17"), by rolling forward
    to next year if the month has already passed relative to today.

    Shared by _parse_rhp and _parse_heuristic (previously each had its own
    inline copy of this logic; _scrape_seetickets still builds the
    equivalent string directly via _infer_year() since its date text
    already comes pre-split into separate month/day tokens rather than a
    single string to re-parse).

    Returns None if date_str already contains an explicit 4-digit year
    (in which case the string's own year should win), or has no
    recognizable month name at all (in which case _parse_fuzzy_date's
    sentinel-date safety rail should do its job and reject it outright
    rather than have us hand it a made-up anchor).
    """
    if not date_str or re.search(r"\b\d{4}\b", date_str):
        return None
    month_match = re.search(
        r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)",
        date_str, re.IGNORECASE
    )
    if not month_match:
        return None
    year = _infer_year(month_match.group(1)[:3].title())
    return datetime(year, 1, 1)


def _dedupe_nested_matches(elements: list) -> list:
    """
    Keep only the OUTERMOST matches from a CSS selector result -- drop any
    matched element that is itself a descendant of another matched element
    in the same result set.

    Confirmed real-world trigger: WordPress's "The Events Calendar" plugin
    uses BEM-style class naming for its "Latest Past Events" widget --
    e.g. tribe-events-calendar-latest-past__event (the actual per-event
    container), tribe-events-calendar-latest-past__event-title,
    ...__event-date-tag, ...__event-featured-image, etc. (each event's
    internal sub-pieces). A substring selector like [class*='tribe-event']
    matches ALL of these at once -- the real container AND every one of
    its own children -- since every BEM sub-element name also contains
    "tribe-event". Confirmed on Dzire Bar And Lounge: 145 elements matched
    for what was actually a much smaller number of real events, because
    each event contributed itself plus ~7-10 of its own nested pieces to
    the match count. Extracting title/date from one of those nested
    pieces in isolation (e.g. just the image-wrapper div) finds nothing,
    since the real title/date live in SIBLING pieces, not inside that one
    fragment -- hence 0 events despite well over 100 "matches."

    General fix, not a Dzire-specific patch: any sufficiently broad
    substring selector in _EVENT_SELECTORS is exposed to this same
    failure mode on any BEM-ish (or otherwise nested-naming) theme, so
    this runs for every selector match, not just the tribe-event one.
    """
    if len(elements) <= 1:
        return elements
    match_ids = set(id(e) for e in elements)
    kept = []
    for el in elements:
        if not any(id(ancestor) in match_ids for ancestor in el.parents):
            kept.append(el)
    return kept


def _nearby_sibling_anchor(tag, max_hops: int = 3):
    """
    For headings with no <a> of their own, look up to `max_hops` siblings
    in each direction for the nearest element containing a real link.

    Covers the "poster image links to the event's detail page, plain-text
    heading sits right after it, TICKETS link sits right after that"
    pattern -- confirmed on Graceland Live (Wix, no semantic event/card
    classes, no schema.org markup, heading is a bare <h1>/<h1>-equivalent
    with no inner link at all). Without this, _fallback_heading_containers
    silently produces zero candidates on markup shaped this way, since it
    only ever checked inside the heading tag itself.
    """
    for direction in (tag.find_previous_siblings, tag.find_next_siblings):
        hop = 0
        for sib in direction():
            hop += 1
            if hop > max_hops:
                break
            if getattr(sib, "name", None) in _HEADING_TAGS:
                break  # ran into the previous/next event's heading -- stop
            # The sibling can EITHER be the anchor itself (e.g. Graceland's
            # poster image is directly "<a href=...><img/></a>", a sibling
            # of the heading, not a container wrapping one) OR a container
            # that has an anchor somewhere inside it -- check both, anchor-
            # itself first since that's the more common real-world shape.
            if getattr(sib, "name", None) == "a" and sib.get("href"):
                return sib
            found = sib.find("a", href=True) if hasattr(sib, "find") else None
            if found:
                return found
    return None


def _fallback_heading_containers(soup) -> list:
    """
    Last-resort container detection for sites whose markup doesn't match
    any known event-card class pattern (e.g. Etix-rendered venue calendars
    like Hi Tone Cafe, which use plain h2/h3 headings linking to event
    detail pages with no 'event'/'card'-style class names at all; or
    Wix-built sites like Graceland Live, whose headings have no link
    inside them at all -- see _nearby_sibling_anchor).

    Heuristic: an event listing heading is an h1-h4 either wrapping (or
    directly followed by) a link, OR sitting immediately next to one
    (poster image / "TICKETS" button), repeated many times down the
    page, where most of the link hrefs share a common path segment (e.g.
    "/event/"). We use the heading's parent element as the container so
    date text and ticket links sitting alongside the title are still
    reachable by the normal extraction code below.
    """
    candidates = []  # (tag, href, is_orphan)
    for tag in soup.find_all(["h1", "h2", "h3", "h4"]):
        heading_text = tag.get_text(strip=True)
        if _MONTH_YEAR_RE.fullmatch(heading_text):
            continue  # month/year divider, not an event heading
        # Skip headings that live inside structural chrome (site nav,
        # footer, page <header>) rather than the actual page content.
        # Real-world sites can have dozens of these (mega-menus, footer
        # sitemaps, "Our Brands" link lists) -- left unfiltered, they
        # dilute the path-segment majority vote below threshold even when
        # every genuine event heading is being detected correctly
        # (confirmed on Ryman: 13/13 real event links found, but diluted
        # to 13/38 candidates by nav/footer headings, which fails a 50%
        # vote). Filtering structural chrome out up front fixes this at
        # the source rather than chasing a different magic threshold
        # number for every new site.
        if tag.find_parent(["nav", "footer", "header"]):
            continue
        a = tag.find("a", href=True)
        is_orphan = False
        if not a:
            # Orphan heading (no inner link) -- check nearby siblings
            # before giving up on this candidate entirely.
            a = _nearby_sibling_anchor(tag)
            is_orphan = True
        if not a:
            continue
        candidates.append((tag, a["href"], is_orphan))

    if len(candidates) < 3:
        return []

    # Find the most common path segment among hrefs (e.g. "event" from
    # "/event/foo" or "https://site.com/event/foo") to confirm these
    # headings are actually event links, not nav/footer links.
    #
    # IMPORTANT: must parse out the URL PATH before splitting on "/" --
    # splitting a raw absolute href like "https://www.ryman.com/event/foo"
    # on "/" and taking the first segment gives the literal string
    # "https:" for every single link, since that's always parts[0]
    # regardless of the actual page. That collapses every candidate into
    # one meaningless bucket and breaks detection entirely on any site
    # that uses absolute hrefs in its markup (confirmed on Ryman/AXS).
    # Relative hrefs ("/event/foo") aren't affected by this parsing change
    # either way, so this fix is safe for sites already working correctly
    # (e.g. Hi Tone).
    from urllib.parse import urlparse
    from collections import Counter
    seg_counts = Counter()
    for _, href, _is_orphan in candidates:
        path = urlparse(href).path
        parts = [p for p in path.split("/") if p]
        if parts:
            seg_counts[parts[0]] += 1

    threshold = max(3, len(candidates) * 0.5)
    top_seg, top_count = (seg_counts.most_common(1)[0] if seg_counts else (None, 0))

    if seg_counts and top_count >= threshold:
        selected = [
            (tag, href) for tag, href, _is_orphan in candidates
            if ([p for p in urlparse(href).path.split("/") if p] or [None])[0] == top_seg
        ]
    else:
        # Flat-slug sites (confirmed on Graceland Live, a Wix site whose
        # event hrefs are top-level -- "/elmiene", "/deanz" -- with no
        # shared path segment to vote on at all) can never clear the
        # majority-vote check above by design; there's nothing to share.
        #
        # For candidates found via the ORPHAN path specifically (a nearby
        # sibling anchor, not a link inside the heading itself), the
        # detection signal is already meaningful on its own: a heading
        # that isn't a month/year divider, isn't inside nav/footer/header,
        # and sits within 3 siblings of a real link -- repeated 3+ times
        # down the page. That's enough to trust without ALSO requiring a
        # shared URL path segment, since flat-slug sites structurally
        # can't provide one. Regular (non-orphan) candidates still need
        # to clear the segment vote as before -- this relaxation is
        # scoped to the orphan case only.
        orphan_only = [(tag, href) for tag, href, is_orphan in candidates if is_orphan]
        if len(orphan_only) < 3:
            return []
        selected = orphan_only

    containers = []
    for tag, href in selected:
        container = _synthetic_container(tag, soup, href=href)
        container._tlr_anchor = _nearest_preceding_anchor(tag)
        containers.append(container)
    return containers


def _nearest_preceding_anchor(tag):
    """
    Walk backwards from `tag` through all preceding elements in the
    original document to find the nearest "Month YYYY" heading above it.
    Used for synthetic fallback containers, since they're copies that no
    longer live in the original tree and can't be matched by id() against
    a forward walk over `soup`.
    """
    for el in tag.find_all_previous(_HEADING_TAGS):
        text = el.get_text(strip=True)
        m = _MONTH_YEAR_RE.search(text)
        if m:
            return _section_anchor_date(m.group(1), m.group(2))
    return None


def _synthetic_container(heading_tag, soup, href=""):
    """
    Build a lightweight synthetic container for a fallback event heading:
    a fresh <div> holding every PRECEDING sibling back to the previous
    heading boundary, the heading itself, and every FOLLOWING sibling up
    to (not including) the next heading of the same or higher level. This
    scopes date/price/ticket-link extraction to just this one event's
    nearby content, without bleeding into neighbors or pulling in the
    whole page like an oversized shared ancestor would.

    Etix-style pages (e.g. Hi Tone Cafe) render each event as a plain date-
    bearing link ("Title... Thu, Jun 18") immediately BEFORE a clean
    "## [Title](same url)" heading with no date in its own text at all.

    AXS-style pages (e.g. Ryman Auditorium) put date/venue text in plain
    <p> tags BEFORE an image-wrapping link, which is itself before the
    heading -- multiple preceding siblings deep, not just the one
    immediately before the heading.

    Wix-style pages (e.g. Graceland Live) put the poster-image link BEFORE
    the (linkless) heading and a "TICKETS" link AFTER it, with the date
    text as a following sibling too -- covered by the same "walk all
    preceding/following siblings to the next heading boundary" approach,
    no special-casing needed beyond _nearby_sibling_anchor finding the
    candidate href in the first place.
    """
    wrapper = soup.new_tag("div")

    preceding = []
    for sib in heading_tag.find_previous_siblings():
        if sib.name in _HEADING_TAGS:
            break
        preceding.append(sib)
    # find_previous_siblings() walks backwards (nearest first) -- reverse
    # so the synthetic container preserves original document order.
    for sib in reversed(preceding):
        wrapper.append(sib.__copy__())

    wrapper.append(heading_tag.__copy__())
    for sib in heading_tag.find_next_siblings():
        if sib.name in _HEADING_TAGS:
            break
        wrapper.append(sib.__copy__())
    return wrapper


def _parse_rhp(html: str, base_url: str, source: dict,
                city_slug: str, city_name: str,
                tz_name: str = "America/Chicago") -> list[dict]:
    """
    Dedicated parser for the "RHP" WordPress events plugin seen on Hi Tone
    Cafe's events page (and possibly other venues using the same plugin).

    Confirmed real markup (June 2026):
        <a id="eventTitle" class="url" href=".../event/<slug>/<venue>/<city>/"
           title="..." rel="bookmark">
            <h2 class="... rhp-event__title--list ...">Event Title</h2>
        </a>
        ...
        <div class="eventDateList rhp-event__date--list">
            <div id="eventDate" class="mb-0 eventMonth singleEventDate text-uppercase">
                Fri, Jun 19
            </div>
        </div>

    Two things make this different from the generic heading-fallback
    heuristic, which is why it gets its own tier instead of being folded
    into _fallback_heading_containers:
      1. The <a> WRAPS the <h2> (heading-fallback expects the opposite —
         an <a> nested inside the heading).
      2. The date lives in a sibling div with a stable, reusable
         `rhp-event__date--list` class, not loose text near the heading.

    The href itself (".../event/<slug>/<venue-slug>/<city-slug>/") is also
    a clean, stable dedup key per-event — used directly as external_id.
    """
    soup = BeautifulSoup(html, "html.parser")

    # IMPORTANT: the real anchor has BOTH id="eventTitle" AND class="url"
    # at once. A comma-separated CSS selector ("a#eventTitle, a.url") would
    # match it twice — once per clause — and double every event. Match on
    # id alone first; only fall back to class="url" if no id matches exist
    # at all (e.g. a future markup variant that drops the id).
    title_links = soup.select("a#eventTitle")
    if not title_links:
        title_links = soup.select("a.url")
    if not title_links:
        title_links = [
            a for a in soup.find_all("a", href=True)
            if a.find(["h1", "h2", "h3"], class_=lambda c: c and "rhp-event__title" in c)
        ]
    if not title_links:
        return []

    events = []
    seen   = set()  # keyed by href — unique per event in this markup

    for a_tag in title_links:
        heading = a_tag.find(["h1", "h2", "h3"])
        title   = heading.get_text(strip=True) if heading else a_tag.get("title", "").strip()
        if not title:
            continue

        href = a_tag.get("href", "")
        if not href:
            continue
        if href in seen:
            continue
        seen.add(href)
        ticket_url = _abs(href, base_url)

        # Date lookup: do NOT crawl upward through ancestors looking for
        # "any #eventDate nearby" — RHP's wrapper divs nest several events
        # deep, so an upward crawl frequently lands on a container that
        # holds MULTIPLE events' date divs, silently grabbing the wrong
        # one (this caused duplicate/mis-dated events in production).
        #
        # Instead, find the OTHER anchor on the page that shares this
        # exact href (the thumbnail-wrapping link seen in the confirmed
        # markup: <a href="same-url"><img/><div id="eventDate">...) and
        # read the date from inside THAT specific link only.
        date_str = ""
        date_link = None
        for cand in soup.find_all("a", href=True):
            if cand is a_tag:
                continue
            if cand.get("href") == href and cand.select_one("#eventDate, [class*='eventDate']"):
                date_link = cand
                break
        if date_link:
            date_el  = date_link.select_one("#eventDate, [class*='eventDate']")
            date_str = date_el.get_text(strip=True) if date_el else ""

        if not date_str:
            # Fallback: narrow search to the title link's own immediate
            # row/column ancestors only (max 4 levels), never the whole
            # page — keeps us from re-introducing the cross-event bleed
            # this whole rewrite is fixing.
            scope = a_tag
            for _ in range(4):
                if scope.parent is None:
                    break
                scope = scope.parent
                date_el = scope.select_one("#eventDate, [class*='eventDate']")
                if date_el:
                    date_str = date_el.get_text(strip=True)
                    break
        if not date_str:
            continue

        # Time + price: scoped narrowly to THIS event only. Climb from the
        # title link, capped at a few levels, and stop the moment the
        # ancestor would contain a SECOND #eventTitle — that's the signal
        # we've crossed into a sibling event's markup. This is what the
        # earlier broad upward-crawl got wrong (it climbed indiscriminately
        # and often landed on a wrapper holding several events at once).
        info_scope = a_tag.parent or a_tag
        for _ in range(5):
            parent = info_scope.parent
            if parent is None:
                break
            if len(parent.select("#eventTitle")) > 1:
                break
            info_scope = parent
        card_text = info_scope.get_text(" ", strip=True)

        time_match = _SHOW_TIME_RE.search(card_text) or _DOORS_TIME_RE.search(card_text)
        if time_match:
            date_str = f"{date_str} {time_match.group(1)}"

        # No year in "Fri, Jun 19" — anchor using the shared year-rollover
        # helper (infer next year if the month has already passed
        # relative to today).
        start_date = _parse_fuzzy_date(date_str, default=_year_inferred_default(date_str))
        if not start_date:
            continue

        img_el = date_link.find("img") if date_link else info_scope.find("img")
        image_url = ""
        if img_el:
            image_url = img_el.get("src") or img_el.get("data-src") or ""

        # Same og:image fallback as _parse_heuristic/_parse_ical -- RHP
        # markup doesn't always carry a thumbnail on the listing page even
        # when the event's own detail page has one.
        if not image_url and ticket_url:
            image_url = _fetch_og_image(ticket_url)

        price_match = _PRICE_RE.search(card_text)
        cost_text   = ""
        if price_match:
            low, high = price_match.group(1), price_match.group(2)
            cost_text = f"${low} to ${high}" if high else f"${low}"

        # "Description" on this plugin is actually the RHP subheader field
        # — confirmed real markup: <h4 id="evSubHead" class="eventSubHeader
        # rhp-event__subheader--list">. It's optional per-event (often
        # empty/d-none when unset), which is why some events show a blurb
        # and others don't.
        desc_el = info_scope.select_one("#evSubHead, [class*='subheader']")
        desc    = desc_el.get_text(strip=True) if desc_el else ""

        events.append(_ev(
            title=_clean_title(title), description=desc,
            start_date=start_date, end_date=start_date,
            venue_name=source.get("name", ""),
            ticket_url=ticket_url, source=source,
            city_slug=city_slug, city_name=city_name,
            image_url=image_url, cost=cost_text,
            external_id=ticket_url,
        ))

    return events


def _parse_heuristic(html: str, base_url: str, source: dict,
                     city_slug: str, city_name: str,
                     tz_name: str = "America/Chicago") -> list[dict]:
    """
    CSS heuristic scraper.
    Quality gate: if ≥50% of raw events fail date parsing, return []
    and let Tier 4 (Ollama) handle it instead.

    Section-aware: many calendar pages (e.g. Etix-powered venue sites)
    group events under month/year headings like "June 2026" and never
    repeat the year on individual events ("Thu, Jun 18"). We walk the
    page in document order, remember the most recent month/year heading
    seen, and use it as a default anchor when parsing dates for events
    that follow — instead of letting dateutil silently guess from
    today's date (which only looks right by coincidence).
    """
    soup       = BeautifulSoup(html, "html.parser")
    containers = []
    for sel in _EVENT_SELECTORS:
        found = soup.select(sel)
        if found:
            containers = _dedupe_nested_matches(found)
            break

    if not containers:
        containers = _fallback_heading_containers(soup)

    if not containers:
        return []

    # Map each container to the nearest preceding month/year heading by
    # walking all "interesting" elements (headings + event containers) in
    # document order. This works regardless of DOM nesting depth, since
    # BeautifulSoup's default find_all traversal is already document-order.
    container_set = set(id(c) for c in containers[:60])
    anchor_for_container = {}
    current_anchor = None
    for el in soup.find_all(True):
        if el.name in _HEADING_TAGS:
            text = el.get_text(strip=True)
            m = _MONTH_YEAR_RE.search(text)
            if m:
                anchor = _section_anchor_date(m.group(1), m.group(2))
                if anchor:
                    current_anchor = anchor
        if id(el) in container_set:
            anchor_for_container[id(el)] = current_anchor

    # Synthetic fallback containers carry their own precomputed anchor
    # (set in _fallback_heading_containers) since they're detached copies
    # that never appear in the soup.find_all(True) walk above.
    for c in containers[:60]:
        if hasattr(c, "_tlr_anchor"):
            anchor_for_container[id(c)] = c._tlr_anchor

    raw_events = []
    seen       = set()

    for c in containers[:60]:
        # Confirmed real-world gap: TicketWeb's "tw-widget-event" markup
        # (seen independently on Hop Springs - Outdoors AND Marathon Music
        # Works -- same third-party widget, different venues) puts the
        # title in <div class="tw-event-name">, which the original
        # title/summary-only selector never matched. Same root cause on
        # Nashville Superspeedway, a different platform entirely, whose
        # title sits in a bare <div class="event">. [class~='event'] is a
        # whole-token match (only fires when "event" is exactly one of the
        # element's class tokens, e.g. class="event" or class="event foo")
        # -- deliberately NOT a [class*='event'] substring match, which
        # would be far too broad and risk matching a container's own class
        # (many things are named "event-card", "event-item", etc.) instead
        # of the actual title inside it.
        title_el = c.select_one(
            "h1,h2,h3,[class*='title'],[class*='summary'],"
            "[class*='event-name'],[class*='event-title'],[class~='event']"
        )
        if not title_el:
            continue
        # get_text(" ", strip=True) here for the same reason as the date
        # extraction fix below -- confirmed real bug on Nashville
        # Superspeedway, whose title div has a nested suffix div
        # ("Grand Prix" + "presented by OnlyBulls" as separate text
        # nodes), which glued into "Grand Prixpresented by..." with no
        # separator.
        title = title_el.get_text(" ", strip=True)
        if not title:
            continue
        # Etix/Hi-Tone-style headings glue the date onto the title text
        # itself (e.g. "DIY Memphis Presents: [Big Room] Thu, Jun 18").
        # Strip it so the title field doesn't carry date junk — the real
        # date is recovered separately below from the same text or a
        # sibling link. Also strip common ticketing-platform boilerplate
        # suffixes ("... - Tickets", "... | Ticketmaster").
        title = _clean_title(_TRAILING_DATE_RE.sub("", title).strip())

        full_text = c.get_text(" ", strip=True)

        # Date range check FIRST ("June 26 - July 19, 2026") -- takes
        # priority over everything below except an explicit machine-
        # readable datetime="" attribute, since a naive text-scan against
        # range text has the same wrong-end-of-range risk whether it comes
        # from a <time>/[class*='date'] element or the loose full-text
        # scan further down.
        range_match = _DATE_RANGE_RE.search(full_text)

        # Date: a real <time> element is always more trustworthy than a
        # generic [class*='date'] match -- a <time datetime="..."> carries
        # a structured, ISO8601 value, while a class-matched span can just
        # as easily be a weekday abbreviation, a "date-tag" label, or some
        # other fragment that isn't a usable date on its own. Confirmed
        # real-world trigger: WordPress TEC's "Latest Past Events" widget
        # has a <span class="...__event-date-tag-weekday">Sun</span>
        # sitting BEFORE the actual <time datetime="..."> in document
        # order -- select_one() on a combined "time,[class*='date']"
        # selector returns whichever matches first, so it was picking the
        # bare weekday text over the real structured date every time.
        date_el  = c.select_one("time[datetime]") or c.select_one("time") or c.select_one("[class*='date']")
        date_str = ""
        if date_el:
            # get_text(" ", strip=True) -- NOT get_text(strip=True) --
            # deliberately, here. Confirmed real bug on TicketWeb's date
            # div, whose content is several separate text nodes across
            # multiple lines ("Sep Wed 16", "@", "7:30 pm"). Without an
            # explicit separator, BeautifulSoup concatenates adjacent text
            # nodes with nothing between them (e.g. "16@7:30pm"), which
            # dateutil's fuzzy parser can easily fail to split correctly.
            date_str = date_el.get("datetime") or date_el.get_text(" ", strip=True)

        if range_match and not (date_el and date_el.get("datetime")):
            date_str = f"{range_match.group(1)}, {range_match.group(2)}"

        if not date_str:
            # No <time>/date-class element and no range match — scan the
            # container's text for a date-shaped fragment (e.g. "Thu, Jun
            # 18") and append the event's actual start time so the fuzzy
            # parser has both a date and a time to work with. Prefer
            # "Show:" over "Doors:" since the show time is what people
            # actually want to know — doors is just when the venue opens.
            #
            # Try the year-bearing pattern first (e.g. "June 19, 2026 7:00
            # PM") -- it's typically already a complete, parseable
            # date+time string on its own, no Show:/Doors: time-appending
            # or year-inference needed. Fall back to the day-of-week
            # pattern (e.g. "Thu, Jun 18") for Etix-style sites that omit
            # the year and need a separate time fragment appended.
            date_match = _LOOSE_DATE_WITH_YEAR_RE.search(full_text)
            if date_match:
                date_str = date_match.group(0)
            else:
                date_match = _LOOSE_DATE_RE.search(full_text)
                if date_match:
                    date_str = date_match.group(0)
                    time_match = _SHOW_TIME_RE.search(full_text) or _DOORS_TIME_RE.search(full_text)
                    if time_match:
                        date_str = f"{date_str} {time_match.group(1)}"

        # Image: src/data-src/srcset, or a CSS background-image as a last
        # resort (some sites -- confirmed pattern on a handful of venue
        # sites using CSS-driven poster grids -- render the poster as a
        # div background rather than a real <img> at all). Strip
        # Squarespace CDN query params either way.
        img_el    = c.find("img")
        image_url = ""
        if img_el:
            image_url = (img_el.get("src") or img_el.get("data-src")
                         or img_el.get("data-lazy-src") or "")
            if not image_url:
                srcset = img_el.get("srcset") or img_el.get("data-srcset") or ""
                if srcset:
                    image_url = srcset.split(",")[0].strip().split(" ")[0]
        if not image_url:
            bg_el = c.select_one("[style*='background-image']")
            if bg_el:
                m = _BG_IMAGE_RE.search(bg_el.get("style", ""))
                if m:
                    image_url = m.group(1)
        if "squarespace-cdn.com" in image_url and "?" in image_url:
            image_url = image_url.split("?")[0]

        # Ticket link: rank candidates by keyword priority so "Buy Tickets"/
        # "Free Show" always wins over "RSVP" (a Facebook event link, not a
        # ticketing link) regardless of document order within the container.
        best_rank, a_tag = len(_TICKET_LINK_PRIORITY), None
        for cand in c.find_all("a", href=True):
            link_text = cand.get_text(strip=True).lower()
            for rank, kw in enumerate(_TICKET_LINK_PRIORITY):
                if kw in link_text and rank < best_rank:
                    best_rank, a_tag = rank, cand
                    break
        if not a_tag:
            a_tag = title_el.find("a", href=True) or c.find("a", href=True)
        ticket_url = _abs(a_tag["href"], base_url) if a_tag else ""

        # Canonical "More Info" detail-page link, when present — used as a
        # stable dedup/source key since Etix ticket URLs carry a partner_id
        # query param that's noise for fingerprinting.
        more_info_a = next(
            (cand for cand in c.find_all("a", href=True)
             if "more info" in cand.get_text(strip=True).lower()),
            None,
        )
        source_url = _abs(more_info_a["href"], base_url) if more_info_a else ""

        p_tag = c.find("p")
        desc  = p_tag.get_text(strip=True) if p_tag else ""

        # Price: blank line = free, "$X" = flat, "$X to $Y" = range. Not
        # required for parsing — purely informational — so a miss here
        # never affects the date quality gate below.
        cost_text = ""
        price_match = _PRICE_RE.search(full_text)
        if price_match:
            low, high = price_match.group(1), price_match.group(2)
            cost_text = f"${low} to ${high}" if high else f"${low}"

        raw_events.append({
            "title":      title,
            "date_str":   date_str,
            "image_url":  image_url,
            "ticket_url": ticket_url,
            "source_url": source_url,
            "desc":       desc,
            "cost":       cost_text,
            "anchor":     anchor_for_container.get(id(c)),
        })

    if not raw_events:
        return []

    # Quality gate: bail to Ollama if too many dates fail to parse.
    # Anchor priority: an explicit month/year section-heading anchor (if
    # this container sat under one) wins; otherwise fall back to rolling
    # forward from the date string's own month name (e.g. "FRIDAY, JULY
    # 17" with no section heading at all, as on Graceland Live).
    parsed_dates = [
        _parse_fuzzy_date(r["date_str"], default=r["anchor"] or _year_inferred_default(r["date_str"]))
        for r in raw_events
    ]
    fail_count   = sum(1 for d in parsed_dates if not d)
    if len(raw_events) > 0 and fail_count / len(raw_events) >= 0.5:
        log.info("[%s] Heuristic quality gate: %d/%d dates failed — falling to Ollama",
                 source.get("name"), fail_count, len(raw_events))
        return []

    events = []
    for r, start_date in zip(raw_events, parsed_dates):
        if not start_date:
            continue
        key = r["title"].lower() + "|" + start_date[:10]
        if key in seen:
            continue
        seen.add(key)
        # Prefer the canonical "More Info" detail-page URL as the stable
        # external_id/dedup key over the ticket URL — Etix ticket links
        # carry a partner_id query param plus slug variations that make
        # them noisy for fingerprinting, while hitonecafe.com/event/...
        # links are stable per show.
        external_id = r["source_url"] or r["ticket_url"]

        # Fallback: heuristic DOM scraping frequently finds no <img> at all
        # (inconsistent site layouts) even though the event's own detail
        # page almost always has an og:image meta tag. Same pattern already
        # used for iCal events in _parse_ical -- generalized here since the
        # heuristic tier hits this gap far more often than iCal does.
        image_url = r["image_url"]
        if not image_url and r["ticket_url"]:
            image_url = _fetch_og_image(r["ticket_url"])

        events.append(_ev(
            title=r["title"], description=r["desc"],
            start_date=start_date, end_date=start_date,
            venue_name=source.get("name", ""),
            ticket_url=r["ticket_url"], source=source,
            city_slug=city_slug, city_name=city_name,
            image_url=image_url,
            cost=r["cost"],
            external_id=external_id,
        ))
    return events


def _ollama_extract(html: str, source: dict, city_slug: str, city_name: str) -> list[dict]:
    try:
        import json as _json
        from config import OLLAMA_HOST, OLLAMA_MODEL, OLLAMA_TIMEOUT
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()
        text = soup.get_text(separator="\n", strip=True)[:6000]

        # Today's date anchor: without this, the model has no way to
        # infer a year on sites whose visible text never states one
        # (e.g. Graceland Live's "FRIDAY, JULY 17") and is exposed to
        # exactly the same silent mis-dating risk that the sentinel-date
        # safety rail in _parse_fuzzy_date exists to catch in the
        # heuristic tier -- except tier 4 had no equivalent protection at
        # all before this.
        today_str = datetime.now().strftime("%Y-%m-%d")

        prompt = (
            f"Today's date is {today_str}. Extract all UPCOMING events "
            f"(on or after today's date) from this page for "
            f"{source.get('name')} in {city_name}.\n"
            "Return a JSON array. Each object must have exactly these keys:\n"
            "title, description, start_date (YYYY-MM-DD HH:MM:SS), end_date, "
            "venue_name, ticket_url, cost, image_url.\n"
            "If a date has no explicit year stated, assume the NEXT occurrence "
            "of that month/day on or after today's date -- never a date in "
            "the past relative to today.\n"
            "Use empty string for unknown fields. Return ONLY valid JSON.\n\n"
            f"{text}"
        )
        resp = requests.post(
            f"{OLLAMA_HOST}/api/generate",
            json={"model": OLLAMA_MODEL, "prompt": prompt, "stream": False, "format": "json"},
            timeout=OLLAMA_TIMEOUT,
        )
        resp.raise_for_status()
        raw = resp.json().get("response", "[]")
        raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
        extracted = _json.loads(raw)
        if not isinstance(extracted, list):
            extracted = extracted.get("events", [])

        events = []
        for item in extracted:
            title = _clean_title((item.get("title") or "").strip())
            if not title:
                continue
            events.append(_ev(
                title=title,
                description=item.get("description", ""),
                start_date=_normalize_dt(item.get("start_date", "")),
                end_date=_normalize_dt(item.get("end_date") or item.get("start_date", "")),
                venue_name=item.get("venue_name", source.get("name", "")),
                ticket_url=item.get("ticket_url", ""),
                source=source, city_slug=city_slug, city_name=city_name,
                image_url=item.get("image_url", ""),
                cost=item.get("cost", ""),
            ))
        return events
    except Exception as e:
        log.error("Ollama failed for %s: %s", source.get("name"), e, exc_info=True)
        return []


# ── SeeTickets ────────────────────────────────────────────────────────────────

def _scrape_seetickets(source: dict, city_slug: str, city_name: str) -> list[dict]:
    url = source["url"]
    try:
        resp = _request_get(url, headers=HEADERS, timeout=TIMEOUT, source_name=source.get("name", ""))
    except Exception as e:
        log.warning("SeeTickets fetch failed for %s: %s", source.get("name"), e)
        return []

    soup       = BeautifulSoup(resp.text, "html.parser")
    events     = []
    seen       = set()
    anchors    = soup.find_all("a", href=re.compile(r"seetickets\.us/event/"))
    event_urls = {}
    for a in anchors:
        canonical = a["href"].split("?")[0]
        event_urls.setdefault(canonical, []).append(a)

    for ticket_url, tags in event_urls.items():
        img_a     = next((a for a in tags if a.find("img")), None)
        image_url = (img_a.find("img").get("src", "") if img_a else "")

        title_a = next((a for a in tags if a.get_text(strip=True) and not a.find("img")), None)
        title   = title_a.get_text(strip=True) if title_a else ""
        if not title:
            continue

        block = None
        if title_a:
            for parent in title_a.parents:
                if parent.name in ("div", "li", "article", "section"):
                    if re.search(r"(Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+\w+\s+\d{1,2}", parent.get_text()):
                        block = parent
                        break

        date_text = ""
        if block:
            m = re.search(
                r"(Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+"
                r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d{1,2})",
                block.get_text(" ", strip=True)
            )
            if m:
                year      = _infer_year(m.group(2))
                date_text = f"{m.group(2)} {m.group(3)} {year}"

        if not date_text:
            continue

        time_text = ""
        if block:
            bt = block.get_text(" ", strip=True)
            tm = re.search(r"Show at (\d+:\d+\s*[AP]M)", bt, re.IGNORECASE) or \
                 re.search(r"Doors at (\d+:\d+\s*[AP]M)", bt, re.IGNORECASE)
            if tm:
                time_text = tm.group(1)

        start_date = _parse_fuzzy_date(f"{date_text} {time_text}".strip())
        if not start_date:
            continue

        desc_parts = []
        if block:
            bt   = block.get_text(" ", strip=True)
            supp = re.search(r"Supporting Talent:\s*(.+?)(?:\n|at Hernando)", bt, re.IGNORECASE)
            if supp:
                desc_parts.append(f"Supporting: {supp.group(1).strip()}")
            times = re.findall(r"(?:Doors|Show) at \d+:\d+\s*[AP]M", bt, re.IGNORECASE)
            if times:
                desc_parts.append(" / ".join(times))
            price = re.search(r"\$[\d\.]+-?\$?[\d\.]*", bt)
            if price:
                desc_parts.append(price.group(0))

        key = title.lower() + "|" + start_date[:10]
        if key in seen:
            continue
        seen.add(key)

        events.append(_ev(
            title=_clean_title(title),
            description=" | ".join(desc_parts),
            start_date=start_date, end_date=start_date,
            venue_name=source.get("venue_name", source.get("name", "")),
            ticket_url=ticket_url, source=source,
            city_slug=city_slug, city_name=city_name,
            image_url=image_url,
        ))

    log.info("[%s] SeeTickets: %d events", url, len(events))
    return events


def _infer_year(month_abbr: str) -> int:
    now    = datetime.now()
    months = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
    try:
        m = months.index(month_abbr) + 1
    except ValueError:
        return now.year
    return now.year + 1 if m < now.month else now.year


# ── TEC REST ──────────────────────────────────────────────────────────────────

def _tec_rendered_text(field) -> str:
    """
    TEC REST API title/description fields vary by site: some wrap them as
    {"rendered": "..."} (WP core REST convention), others (confirmed on
    bhamnow.com, 2026-07-02) return a plain string directly. Handle both
    without crashing.
    """
    if isinstance(field, dict):
        return field.get("rendered", "") or ""
    return field or ""


def _scrape_tec_rest(source: dict, city_slug: str, city_name: str) -> list[dict]:
    """
    WordPress TEC (The Events Calendar) REST API.

    IMPORTANT: TEC's /wp-json/tribe/events/v1/events endpoint does NOT
    reliably respect start_date/end_date query params on every site --
    confirmed 2026-07-01 on bhamnow.com, where total_pages stayed at 200
    (~10,000 events) regardless of start_date/end_date filters. Those
    100+ "pages" are overwhelmingly recurring weekly classes/camps each
    counted as a separate occurrence stretching months or years out --
    not a real data problem, just an unbounded feed.

    Without a cap, the old `while True` version here would sequentially
    fetch all 200 pages (100+ seconds minimum just in sleep time, plus
    real request latency) every single scrape cycle. Bounded the same
    way ticketmaster.py bounds its 90-day lookahead: a real date horizon
    checked against each page's results (stop once we're past it, since
    TEC returns events in chronological order) PLUS a hard page-count
    ceiling as a backstop in case a site's sort order is ever different
    than expected.
    """
    from urllib.parse import urlparse
    parsed  = urlparse(source["url"].rstrip("/"))
    api_url = f"{parsed.scheme}://{parsed.netloc}/wp-json/tribe/events/v1/events"

    HORIZON_DAYS = 35  # community/class-heavy TEC sites (e.g. Bham Now: ~30
                        # events/day) can hit the MAX_PAGES cap well before
                        # 90 days out. These are hyperlocal recurring events,
                        # not trip-planning material -- a complete 5-week
                        # window is more useful than a truncated 90-day one.
    MAX_PAGES    = 20  # backstop: 20 pages * 50/page = 1000 events max, regardless of dates

    today   = datetime.now()
    horizon = today + _timedelta(days=HORIZON_DAYS)
    today_str   = today.strftime("%Y-%m-%d")
    horizon_str = horizon.strftime("%Y-%m-%d")

    params = {
        "per_page":   50,
        "status":     "publish",
        "start_date": today_str,
        "end_date":   horizon_str,  # passed even though not confirmed effective on every site --
                                     # harmless if ignored, helpful if honored
        "page":       1,
    }
    events = []

    while params["page"] <= MAX_PAGES:
        try:
            resp = _request_get(api_url, params=params, headers=HEADERS, timeout=TIMEOUT,
                                 source_name=source.get("name", ""))
            data = resp.json()
        except Exception as e:
            log.warning("TEC REST error page %d for %s: %s", params["page"], source.get("name"), e)
            break

        page_events = data.get("events", [])
        if not page_events:
            break

        hit_horizon = False
        for item in page_events:
            start_raw = item.get("start_date", "")
            start_norm = _normalize_dt(start_raw)

            # Stop once we've crossed the horizon -- TEC returns events in
            # chronological order, so everything from here on this page
            # (and every subsequent page) is also beyond the window.
            if start_norm and start_norm[:10] > horizon_str:
                hit_horizon = True
                break

            title = BeautifulSoup(
                _tec_rendered_text(item.get("title", "")),
                "html.parser"
            ).get_text(strip=True)
            title = _clean_title(title)
            if not title:
                continue
            desc  = BeautifulSoup(
                _tec_rendered_text(item.get("description", "")),
                "html.parser"
            ).get_text(separator=" ", strip=True)
            venue = item.get("venue") or {}
            img   = item.get("image") or {}
            events.append(_ev(
                title=title, description=desc,
                start_date=start_norm,
                end_date=_normalize_dt(item.get("end_date") or start_raw),
                venue_name=venue.get("venue", "") if isinstance(venue, dict) else "",
                ticket_url=item.get("url", ""),
                source=source, city_slug=city_slug, city_name=city_name,
                image_url=img.get("url", "") if isinstance(img, dict) else "",
                cost=item.get("cost", ""),
                external_id=f"tec_{item.get('id', '')}",
            ))

        if hit_horizon:
            log.info("[%s] TEC REST: reached %d-day horizon at page %d, stopping",
                     source.get("name"), HORIZON_DAYS, params["page"])
            break
        if params["page"] >= data.get("total_pages", 1):
            break
        params["page"] += 1
        time.sleep(0.5)
    else:
        log.warning("[%s] TEC REST: hit %d-page safety cap before reaching horizon -- "
                    "site may have unusually dense listings, consider a smaller HORIZON_DAYS",
                    source.get("name"), MAX_PAGES)

    log.info("[%s] TEC REST: %d events (within %d-day horizon)",
             source.get("name"), len(events), HORIZON_DAYS)
    return events


# ── iCal URL ──────────────────────────────────────────────────────────────────

def _scrape_ical_url(source: dict, city_slug: str, city_name: str,
                     tz_name: str = "America/Chicago") -> list[dict]:
    return _parse_ical(source["url"], source, city_slug, city_name, tz_name)


# ── JSON API ──────────────────────────────────────────────────────────────────

def _scrape_json_api(source: dict, city_slug: str, city_name: str) -> list[dict]:
    api_url   = source.get("api_url", source["url"])
    paginate  = source.get("pagination", False)
    max_pages = source.get("max_pages", 1)
    events    = []

    for page in range(1, max_pages + 1):
        url = api_url.format(page=page)
        try:
            resp = _request_get(url, headers={**HEADERS, "Accept": "application/json"},
                                 timeout=TIMEOUT, source_name=source.get("name", ""))
            data = resp.json()
        except Exception as e:
            log.warning("JSON API page %d failed for %s: %s", page, source.get("name"), e)
            break

        raw_list = (
            data if isinstance(data, list)
            else next(
                (data[k] for k in ("events", "items", "data", "results")
                 if k in data and isinstance(data[k], list)),
                []
            )
        )
        if not raw_list:
            break

        for raw in raw_list:
            title = _clean_title((raw.get("title") or raw.get("name") or raw.get("event_name") or "").strip())
            if not title:
                continue
            desc = BeautifulSoup(
                raw.get("description") or raw.get("summary") or "",
                "html.parser"
            ).get_text(separator=" ", strip=True)
            venue_name = raw.get("venue") or raw.get("venue_name") or raw.get("location") or ""
            if isinstance(venue_name, dict):
                venue_name = venue_name.get("name", "")
            image_url = raw.get("image") or raw.get("image_url") or raw.get("thumbnail") or ""
            if isinstance(image_url, dict):
                image_url = image_url.get("url", "")
            start_raw = raw.get("start_date") or raw.get("startDate") or raw.get("date") or raw.get("start") or ""
            end_raw   = raw.get("end_date")   or raw.get("endDate")   or raw.get("end")   or start_raw
            events.append(_ev(
                title=title, description=desc,
                start_date=_normalize_dt(str(start_raw)),
                end_date=_normalize_dt(str(end_raw)),
                venue_name=str(venue_name),
                ticket_url=str(raw.get("url") or raw.get("ticket_url") or raw.get("link") or ""),
                source=source, city_slug=city_slug, city_name=city_name,
                image_url=str(image_url),
                cost=str(raw.get("cost") or raw.get("price") or ""),
                external_id=str(raw.get("id") or ""),
            ))
        if not paginate:
            break
        time.sleep(0.5)

    log.info("[%s] JSON API: %d events", source.get("name"), len(events))
    return events


# ── AJAX-paginated "infinite scroll" ─────────────────────────────────────────
#
# Confirmed real-world pattern: Ryman Auditorium (AXS platform). The visible
# page only server-renders a first batch of events; every subsequent batch
# loads via a numbered/offset AJAX endpoint as the user scrolls:
#
#   GET /events/events_ajax/<offset>?category=0&venue=0&team=0&exclude=
#       &per_page=12&came_from_page=event-list-page
#
# The response body is NOT structured JSON with event fields -- it's a
# JSON-encoded STRING containing a raw HTML fragment (the exact same
# "eventItem"/"h3.title"/"m-date__month" markup as the main page), meant
# to be decoded and injected straight into the DOM client-side.
#
# Because that fragment uses identical markup to what _parse_heuristic
# already handles via its fallback-heading-container path, this tier does
# NOT need its own title/date extraction logic at all -- it only needs to
# walk the offsets, unwrap the JSON-string-encoded HTML, and hand each
# chunk to the existing _parse_heuristic(). Any other site using this same
# "infinite scroll via numbered AJAX + JSON-string HTML" pattern (a fairly
# common approach, not unique to AXS) gets covered by the same tier.
#
# Deliberately NOT auto-detected -- the offset URL template and per-page
# count aren't safely guessable from the main page alone (found once via
# browser devtools Network tab, watching what fires while scrolling).
# Configured entirely via notes JSON:
#     {
#       "ajax_paginate": true,
#       "ajax_url_template": "https://www.ryman.com/events/events_ajax/{offset}?category=0&venue=0&team=0&exclude=&per_page=12&came_from_page=event-list-page",
#       "ajax_per_page": 12
#     }
# and source_type set to "ajax_paginate" (a free-text DB column -- no
# Monitor dropdown entry required to use it, though one could be added).

def _scrape_ajax_paginate(source: dict, city_slug: str, city_name: str) -> list[dict]:
    import json as _json

    template = source.get("ajax_url_template", "")
    if not template or "{offset}" not in template:
        log.warning("[%s] ajax_paginate configured but ajax_url_template is missing "
                    "or has no {offset} placeholder -- nothing to fetch", source.get("name"))
        return []

    try:
        per_page = int(source.get("ajax_per_page", 12))
    except (TypeError, ValueError):
        per_page = 12

    # Backstop, same philosophy as TEC REST/JSON API pagination elsewhere
    # in this file: don't hammer a site indefinitely. 20 pages * 12/page
    # = 240 events max per scrape cycle, regardless of how deep the real
    # listing goes.
    MAX_PAGES = 20

    events    = []
    seen_keys = set()

    for page_num in range(MAX_PAGES):
        offset = page_num * per_page
        url = template.format(offset=offset)
        try:
            resp = _request_get(url, headers=_ajax_headers_for(source), timeout=TIMEOUT,
                                source_name=source.get("name", ""))
        except Exception as e:
            log.info("[%s] ajax_paginate stopped at offset %d: %s",
                     source.get("name"), offset, e)
            break

        try:
            # The confirmed real response is a JSON-encoded STRING
            # containing HTML (e.g. the raw body is `"<div>...</div>\n"`,
            # quotes and all) -- json.loads() unwraps that back to a
            # plain HTML string.
            fragment_html = _json.loads(resp.text)
            if not isinstance(fragment_html, str):
                fragment_html = resp.text
        except Exception:
            # Some other ajax_paginate source might return raw HTML
            # directly rather than JSON-string-wrapped HTML like Ryman --
            # don't assume every site using this tier is wrapped the same
            # way, just fall back to the raw body.
            fragment_html = resp.text

        if not fragment_html or not fragment_html.strip():
            log.info("[%s] ajax_paginate: empty page at offset %d, stopping",
                     source.get("name"), offset)
            break

        page_events = _parse_heuristic(fragment_html, url, source, city_slug, city_name)

        new_count = 0
        for ev in page_events:
            key = ev["title"].lower() + "|" + ev["start_date"][:10]
            if key in seen_keys:
                continue
            seen_keys.add(key)
            events.append(ev)
            new_count += 1

        if new_count == 0:
            # Either genuinely exhausted the listing, or this offset
            # returned a repeat of events already seen -- either way,
            # no point requesting further pages.
            log.info("[%s] ajax_paginate: no new events at offset %d, stopping",
                     source.get("name"), offset)
            break

        time.sleep(0.5)

    log.info("[%s] ajax_paginate: %d total events", source.get("name"), len(events))
    return events


# ── Generic HTML ──────────────────────────────────────────────────────────────

def _scrape_generic_html(source: dict, city_slug: str, city_name: str) -> list[dict]:
    try:
        resp = _request_get(source["url"], headers=HEADERS, timeout=TIMEOUT,
                             source_name=source.get("name", ""))
    except Exception as e:
        log.warning("Generic HTML fetch failed for %s: %s", source.get("name"), e)
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    sel  = source.get("event_container_selector")
    if sel:
        containers = soup.select(sel)
        if containers:
            events = []
            for c in containers:
                te = c.select_one(source.get("title_selector", "h2,h3,.event-title"))
                de = c.select_one(source.get("date_selector", ".event-date,time"))
                de2 = c.select_one(source.get("description_selector", ".description,p"))
                le = c.select_one("a[href]")
                ie = c.select_one("img")
                if not te or not te.get_text(strip=True):
                    continue
                events.append(_ev(
                    title=_clean_title(te.get_text(strip=True)),
                    description=de2.get_text(separator=" ", strip=True) if de2 else "",
                    start_date=_parse_fuzzy_date(de.get_text(strip=True) if de else ""),
                    end_date="",
                    venue_name=source.get("name", ""),
                    ticket_url=le["href"] if le else "",
                    source=source, city_slug=city_slug, city_name=city_name,
                    image_url=ie.get("src", "") if ie else "",
                    needs_enrichment=True,
                ))
            return events
    return _ollama_extract(resp.text, source, city_slug, city_name)


# ── Date helpers ──────────────────────────────────────────────────────────────

def _normalize_dt(raw) -> str:
    """Normalize ISO/epoch dates to 'YYYY-MM-DD HH:MM:SS'. No timezone conversion."""
    if not raw:
        return ""
    if isinstance(raw, (int, float)):
        try:
            return datetime.fromtimestamp(raw).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return ""
    raw = str(raw).strip().replace("Z", "+00:00")
    # Already in target format
    if re.match(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", raw):
        return raw
    # ISO 8601 (with or without offset)
    try:
        dt = datetime.fromisoformat(raw)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        pass
    # Bare date
    m = re.search(r"(\d{4}-\d{2}-\d{2})", raw)
    if m:
        return m.group(1) + " 00:00:00"
    # Last resort
    return _parse_fuzzy_date(raw)


def _parse_fuzzy_date(text: str, default=None) -> str:
    """
    Parse human-readable date strings like 'June 21 2025 7:30 PM'.

    `default` lets a caller anchor missing components (most commonly:
    no year in the visible text, e.g. "Thu, Jun 18"). Without an explicit
    default, dateutil silently fills missing year/month/day from
    datetime.now() at parse time — which happens to look "correct" most
    of the year and then quietly mis-dates events once you cross a
    year/month boundary between scrape time and event time. Always pass
    an explicit default when one is available (see _section_anchor_date
    and _year_inferred_default above).

    SAFETY RULE (added 2026-06-30 — Larimer Lounge incident): if no
    `default` is supplied, this function refuses to let dateutil borrow
    ANY component from today's date. Confirmed root cause: Larimer
    Lounge's "RHP plugin" tier matched a selector that doesn't actually
    exist in the site's static HTML (the real markup is JS-rendered),
    so date_str ended up being something with no real date in it at
    all -- and the old default-to-now() behavior silently turned that
    into "today" for all 104 events instead of failing visibly. A date
    string needs to stand on its own: if it doesn't contain a real
    day/month/year, that's a parse failure, not "today."
    """
    if not text:
        return ""
    try:
        from dateutil import parser as dp

        if default:
            dt = dp.parse(text, fuzzy=True, default=default)
            if dt and dt.year > 2020:
                return dt.strftime("%Y-%m-%d %H:%M:%S")
            return ""

        # No default supplied — parse against an absurd sentinel date
        # instead of dateutil's implicit datetime.now(). If `text`
        # doesn't carry its own real day/month/year, the result lands
        # on (or very near) this sentinel, which we can detect and
        # reject — instead of dateutil silently substituting today's
        # date with no way for us to tell it happened.
        from datetime import datetime as _dt
        sentinel = _dt(1900, 1, 1)
        dt = dp.parse(text, fuzzy=True, default=sentinel)
        if dt and dt.year == 1900:
            log.warning(
                "Refusing fuzzy-parsed date with no real date components: %r",
                text
            )
            return ""
        if dt and dt.year > 2020:
            return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        pass
    return ""


def _abs(href: str, base_url: str) -> str:
    from urllib.parse import urljoin
    return urljoin(base_url, href)


# ── CLI debug entrypoint ────────────────────────────────────────────────────
#
# Run directly on the server to diagnose why a source isn't scraping:
#   venv/bin/python3 scraper.py <url>
#
# No quoting/heredoc gymnastics needed — just one argument. Prints raw HTML
# stats, which tier (if any) would fire and why, whether the known
# event-card CSS selectors matched anything, what _fallback_heading_
# containers found (including orphan-heading matches) or why it bailed,
# and the actual extracted events if any tier succeeds. Does NOT hit
# Ollama — this is for diagnosing tiers 1-3 only, since tier 4 is
# slow/expensive and not what's usually broken.

if __name__ == "__main__":
    import sys as _sys
    if len(_sys.argv) < 2:
        print("Usage: python3 scraper.py <url>")
        _sys.exit(1)

    debug_url = _sys.argv[1]
    print(f"=== Fetching {debug_url} ===")
    resp = requests.get(debug_url, headers=HEADERS, timeout=TIMEOUT)
    html = resp.text
    print(f"HTTP {resp.status_code}, {len(html)} bytes\n")

    print("=== Does '/event/' appear in raw HTML? ===")
    idx = html.find("/event/")
    print(f"First occurrence at index: {idx}")
    if idx == -1:
        print("NOT FOUND. This usually means the events are loaded via "
              "JavaScript/AJAX after page load, and requests.get() only "
              "sees an empty shell — no amount of CSS-selector tuning will "
              "fix that; the source needs a different source_type (e.g. "
              "finding the underlying JSON/API endpoint the JS calls).\n")
    else:
        print("Surrounding raw HTML (1000 chars before, 2000 after):\n")
        print(html[max(0, idx - 1000): idx + 2000])
        print()

    print("=== Tier 1: single calendar-level iCal feed ===")
    ical_hit = _detect_ical(html, debug_url)
    print(f"_detect_ical: {ical_hit!r}\n")

    print("=== Tier 1.5: multiple per-event iCal exports (Squarespace-style) ===")
    multi_ical_hits = _detect_multi_ical(html, debug_url)
    print(f"_detect_multi_ical: {len(multi_ical_hits)} links found")
    for link in multi_ical_hits[:5]:
        print(f"  {link}")
    if len(multi_ical_hits) > 5:
        print(f"  ... and {len(multi_ical_hits) - 5} more")
    print()

    print("=== Known CSS selector tiers (_EVENT_SELECTORS) ===")
    soup_dbg = BeautifulSoup(html, "html.parser")
    for sel in _EVENT_SELECTORS:
        found = soup_dbg.select(sel)
        if found:
            deduped = _dedupe_nested_matches(found)
            if len(deduped) != len(found):
                print(f"  MATCHED '{sel}': {len(found)} raw elements -> "
                      f"{len(deduped)} after dropping nested sub-element matches "
                      f"(BEM-style class naming -- see _dedupe_nested_matches)")
            else:
                print(f"  MATCHED '{sel}': {len(found)} elements")
    else_matched = any(soup_dbg.select(sel) for sel in _EVENT_SELECTORS)
    if not else_matched:
        print("  No known selector matched anything — falling to heading-based detection.\n")

    print("=== _fallback_heading_containers diagnostic ===")
    soup_dbg2 = BeautifulSoup(html, "html.parser")
    headings = soup_dbg2.find_all(["h1", "h2", "h3", "h4"])
    print(f"Total h1-h4 tags on page: {len(headings)}")

    candidates = []
    orphan_count = 0
    for tag in headings:
        heading_text = tag.get_text(strip=True)
        if _MONTH_YEAR_RE.fullmatch(heading_text):
            continue
        if tag.find_parent(["nav", "footer", "header"]):
            continue
        a = tag.find("a", href=True)
        if not a:
            a = _nearby_sibling_anchor(tag)
            if a:
                orphan_count += 1
        if not a:
            continue
        candidates.append((tag, a["href"]))
    print(f"Headings with a usable link (inner OR nearby-sibling) -- excluding month/year "
          f"dividers and nav/footer/header: {len(candidates)}")
    print(f"  of which found via nearby-sibling fallback (no link inside the heading itself): {orphan_count}")
    if candidates[:5]:
        print("First few candidate hrefs:")
        for _, href in candidates[:5]:
            print(f"  {href}")

    if len(candidates) < 3:
        print("\nBAILED: fewer than 3 heading+link candidates found (even counting nearby-"
              "sibling matches). Real markup likely doesn't put a link inside the h1-h4 tag "
              "OR within 3 siblings of it either — inspect the snippet above for the actual "
              "structure.")
    else:
        from collections import Counter
        from urllib.parse import urlparse
        seg_counts = Counter()
        for _, href in candidates:
            path = urlparse(href).path
            parts = [p for p in path.split("/") if p]
            if parts:
                seg_counts[parts[0]] += 1
        print(f"\nPath segment counts (looking for a dominant '/event/'-style prefix): {dict(seg_counts)}")
        if seg_counts:
            top_seg, top_count = seg_counts.most_common(1)[0]
            threshold = max(3, len(candidates) * 0.5)
            print(f"Top segment: '{top_seg}' with {top_count} occurrences (need >= {threshold:.1f})")
            if top_count < threshold:
                print("BAILED: top segment doesn't clear the 50% threshold.")
            else:
                print("PASSED threshold check — containers should be built.")

    print("\n=== Running full _parse_heuristic ===")
    fake_source = {"name": "DEBUG", "url": debug_url}
    heuristic_events = _parse_heuristic(html, debug_url, fake_source, "debug", "Debug", "America/Chicago")
    print(f"_parse_heuristic returned {len(heuristic_events)} events")
    for e in heuristic_events[:5]:
        print(f"  - {e['title']!r} | {e['start_date']} | {e['cost']!r}")

    print("\n=== Running _parse_jsonld (tier 2) ===")
    jsonld_events = _parse_jsonld(html, fake_source, "debug", "Debug")
    print(f"_parse_jsonld returned {len(jsonld_events)} events")

    print("\n=== Running _parse_rhp (tier 2.5) ===")
    rhp_events = _parse_rhp(html, debug_url, fake_source, "debug", "Debug", "America/Chicago")
    print(f"_parse_rhp returned {len(rhp_events)} events")
    no_image_count = sum(1 for e in rhp_events if not e["image_url"])
    print(f"Events missing an image_url: {no_image_count} / {len(rhp_events)}")
    for e in rhp_events[:10]:
        print(f"  - {e['title']!r} | {e['start_date']} | {e['cost']!r} | img={e['image_url']!r}")
