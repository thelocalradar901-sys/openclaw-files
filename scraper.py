"""
scraper.py — OpenClaw HTML/API event scraper

Source types (configured in openclaw-monitor WP plugin):
  html_auto    — Universal: tries iCal → JSON-LD → heuristic CSS → Ollama AI
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
"""

import logging
import re
import time
from datetime import datetime, date, timezone as _timezone
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
            return _scrape_html_auto(source, city_slug, city_name, tz_name)
        elif stype == "seetickets":
            return _scrape_seetickets(source, city_slug, city_name)
        elif stype == "tec_rest":
            return _scrape_tec_rest(source, city_slug, city_name)
        elif stype == "ical_url":
            return _scrape_ical_url(source, city_slug, city_name, tz_name)
        elif stype == "json_api":
            return _scrape_json_api(source, city_slug, city_name)
        elif stype == "generic_html":
            return _scrape_generic_html(source, city_slug, city_name)
        else:
            log.warning("Unknown source_type '%s' — trying html_auto", stype)
            return _scrape_html_auto(source, city_slug, city_name, tz_name)
    except Exception as e:
        log.error("scrape_source crashed for '%s': %s", source.get("url"), e, exc_info=True)
        return []


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


# ── html_auto: 4-tier universal ───────────────────────────────────────────────

def _scrape_html_auto(source: dict, city_slug: str, city_name: str,
                      tz_name: str = "America/Chicago") -> list[dict]:
    url = source["url"]
    try:
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
    except Exception as e:
        log.warning("Fetch failed for %s: %s", source.get("name"), e)
        return []

    html    = resp.text
    no_ical = source.get("no_ical", False)

    # Tier 1: iCal
    ical_url = _detect_ical(html, url, no_ical=no_ical)
    if ical_url:
        events = _parse_ical(ical_url, source, city_slug, city_name, tz_name)
        if events:
            log.info("[%s] iCal: %d events", source.get("name"), len(events))
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
    """Find a calendar-level iCal feed. Ignores individual event ICS links."""
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


def _parse_ical(ical_url: str, source: dict, city_slug: str, city_name: str,
                tz_name: str = None) -> list[dict]:
    """Parse an iCal feed, converting UTC datetimes to local city time."""
    try:
        from icalendar import Calendar
        try:
            local_tz = ZoneInfo(tz_name) if tz_name else ZoneInfo("UTC")
        except Exception:
            local_tz = ZoneInfo("UTC")

        resp = requests.get(ical_url, headers=HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
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
                title=title,
                description=str(comp.get("DESCRIPTION", "")).strip(),
                start_date=start, end_date=end,
                venue_name=str(comp.get("LOCATION", source.get("name", ""))).strip(),
                ticket_url=str(comp.get("URL", "")).strip(),
                source=source, city_slug=city_slug, city_name=city_name,
            ))
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
        for item in items:
            if not isinstance(item, dict):
                continue
            if "Event" not in str(item.get("@type", "")):
                continue
            title     = item.get("name", "").strip()
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


def _section_anchor_date(month_name: str, year: str):
    """Build a default datetime anchored to the 1st of the given month/year,
    used to fill in missing year (and month) components when an event's
    visible date text has no year, e.g. 'Thu, Jun 18'."""
    try:
        return dp_parser.parse(f"1 {month_name} {year}")
    except Exception:
        return None


def _fallback_heading_containers(soup) -> list:
    """
    Last-resort container detection for sites whose markup doesn't match
    any known event-card class pattern (e.g. Etix-rendered venue calendars
    like Hi Tone Cafe, which use plain h2/h3 headings linking to event
    detail pages with no 'event'/'card'-style class names at all).

    Heuristic: an event listing heading is an h1-h4 wrapping (or directly
    followed by) a link, repeated many times down the page, where most of
    the link hrefs share a common path segment (e.g. "/event/"). We use the
    heading's parent element as the container so date text and ticket
    links sitting alongside the title are still reachable by the normal
    extraction code below.
    """
    candidates = []
    for tag in soup.find_all(["h1", "h2", "h3", "h4"]):
        heading_text = tag.get_text(strip=True)
        if _MONTH_YEAR_RE.fullmatch(heading_text):
            continue  # month/year divider, not an event heading
        a = tag.find("a", href=True)
        if not a:
            continue  # only trust a link INSIDE the heading itself
        candidates.append((tag, a["href"]))

    if len(candidates) < 3:
        return []

    # Find the most common path segment among hrefs (e.g. "/event/") to
    # confirm these headings are actually event links, not nav/footer links.
    from collections import Counter
    seg_counts = Counter()
    for _, href in candidates:
        parts = [p for p in href.split("/") if p]
        if parts:
            seg_counts[parts[0]] += 1
    if not seg_counts:
        return []
    top_seg, top_count = seg_counts.most_common(1)[0]
    if top_count < max(3, len(candidates) * 0.5):
        return []

    containers = []
    for tag, href in candidates:
        parts = [p for p in href.split("/") if p]
        if not parts or parts[0] != top_seg:
            continue
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
    a fresh <div> holding the heading itself plus every following sibling
    in document order up to (not including) the next heading of the same
    or higher level. This scopes date/price/ticket-link extraction to just
    this one event's nearby content, without bleeding into neighbors or
    pulling in the whole page like an oversized shared ancestor would.

    Etix-style pages (e.g. Hi Tone Cafe) render each event as a plain date-
    bearing link ("Title... Thu, Jun 18") immediately BEFORE a clean
    "## [Title](same url)" heading with no date in its own text at all. If
    a preceding sibling link shares the heading's href, we prepend a copy
    of it too, since that's the only place the date text actually lives.
    """
    wrapper = soup.new_tag("div")

    if href:
        prev = heading_tag.find_previous_sibling("a", href=True)
        if prev and prev.get("href") == href:
            wrapper.append(prev.__copy__())

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

    title_links = soup.select("a#eventTitle, a.url")
    if not title_links:
        title_links = [
            a for a in soup.find_all("a", href=True)
            if a.find(["h1", "h2", "h3"], class_=lambda c: c and "rhp-event__title" in c)
        ]
    if not title_links:
        return []

    events = []
    seen   = set()

    for a_tag in title_links:
        heading = a_tag.find(["h1", "h2", "h3"])
        title   = heading.get_text(strip=True) if heading else a_tag.get("title", "").strip()
        if not title:
            continue

        href       = a_tag.get("href", "")
        ticket_url = _abs(href, base_url) if href else ""

        # Card boundary: walk up to a reasonably-scoped ancestor so date/
        # price/image lookups stay within THIS event, not bleed into
        # neighboring cards. RHP nests several wrapper divs deep, so climb
        # until we find one that actually contains an eventDate div, capped
        # so a malformed page can't walk us up to <body>.
        card = a_tag
        for _ in range(8):
            if card.parent is None:
                break
            card = card.parent
            if card.select_one("[class*='eventDate'], #eventDate"):
                break

        date_el  = card.select_one("#eventDate, [class*='eventDate']")
        date_str = date_el.get_text(strip=True) if date_el else ""
        if not date_str:
            continue

        # Time, if present anywhere in the card ("Doors:"/"Show:" pattern
        # seen on other venue sites using similar plugins) — Show preferred
        # over Doors since that's the actual performance start time.
        card_text  = card.get_text(" ", strip=True)
        time_match = _SHOW_TIME_RE.search(card_text) or _DOORS_TIME_RE.search(card_text)
        if time_match:
            date_str = f"{date_str} {time_match.group(1)}"

        # No year in "Fri, Jun 19" — anchor using the same forward-rolling
        # logic as the rest of the file (infer next year if the month has
        # already passed relative to today).
        month_match = re.search(r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)", date_str, re.IGNORECASE)
        default_dt  = None
        if month_match and not re.search(r"\b\d{4}\b", date_str):
            year = _infer_year(month_match.group(1)[:3].title())
            default_dt = datetime(year, 1, 1)

        start_date = _parse_fuzzy_date(date_str, default=default_dt)
        if not start_date:
            continue

        key = title.lower() + "|" + start_date[:10]
        if key in seen:
            continue
        seen.add(key)

        img_el    = card.find("img")
        image_url = ""
        if img_el:
            image_url = img_el.get("src") or img_el.get("data-src") or ""

        price_match = _PRICE_RE.search(card_text)
        cost_text   = ""
        if price_match:
            low, high = price_match.group(1), price_match.group(2)
            cost_text = f"${low} to ${high}" if high else f"${low}"

        desc_el = card.select_one("[class*='description'], [class*='excerpt']")
        desc    = desc_el.get_text(strip=True) if desc_el else ""

        events.append(_ev(
            title=title, description=desc,
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
            containers = found
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
        title_el = c.select_one("h1,h2,h3,[class*='title'],[class*='summary']")
        if not title_el:
            continue
        title = title_el.get_text(strip=True)
        if not title:
            continue
        # Etix/Hi-Tone-style headings glue the date onto the title text
        # itself (e.g. "DIY Memphis Presents: [Big Room] Thu, Jun 18").
        # Strip it so the title field doesn't carry date junk — the real
        # date is recovered separately below from the same text or a
        # sibling link.
        title = _TRAILING_DATE_RE.sub("", title).strip()

        # Date: prefer <time datetime="..."> attribute, fall back to visible text
        date_el  = c.select_one("time,[class*='date']")
        date_str = ""
        if date_el:
            date_str = date_el.get("datetime") or date_el.get_text(strip=True)
        if not date_str:
            # No <time>/date-class element — scan the container's text for
            # a date-shaped fragment (e.g. "Thu, Jun 18") and append the
            # event's actual start time so the fuzzy parser has both a
            # date and a time to work with. Prefer "Show:" over "Doors:"
            # since the show time is what people actually want to know —
            # doors is just when the venue opens.
            full_text = c.get_text(" ", strip=True)
            date_match = _LOOSE_DATE_RE.search(full_text)
            if date_match:
                date_str = date_match.group(0)
                time_match = _SHOW_TIME_RE.search(full_text) or _DOORS_TIME_RE.search(full_text)
                if time_match:
                    date_str = f"{date_str} {time_match.group(1)}"

        # Image: src or data-src; strip Squarespace CDN query params
        img_el    = c.find("img")
        image_url = ""
        if img_el:
            image_url = img_el.get("src") or img_el.get("data-src") or ""
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
        price_match = _PRICE_RE.search(c.get_text(" ", strip=True))
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

    # Quality gate: bail to Ollama if too many dates fail to parse
    parsed_dates = [_parse_fuzzy_date(r["date_str"], default=r["anchor"]) for r in raw_events]
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
        events.append(_ev(
            title=r["title"], description=r["desc"],
            start_date=start_date, end_date=start_date,
            venue_name=source.get("name", ""),
            ticket_url=r["ticket_url"], source=source,
            city_slug=city_slug, city_name=city_name,
            image_url=r["image_url"],
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

        prompt = (
            f"Extract all upcoming events from this page for {source.get('name')} in {city_name}.\n"
            "Return a JSON array. Each object must have exactly these keys:\n"
            "title, description, start_date (YYYY-MM-DD HH:MM:SS), end_date, "
            "venue_name, ticket_url, cost, image_url.\n"
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
            title = (item.get("title") or "").strip()
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
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
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
            title=title,
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

def _scrape_tec_rest(source: dict, city_slug: str, city_name: str) -> list[dict]:
    from urllib.parse import urlparse
    parsed  = urlparse(source["url"].rstrip("/"))
    api_url = f"{parsed.scheme}://{parsed.netloc}/wp-json/tribe/events/v1/events"
    today   = datetime.now().strftime("%Y-%m-%d")
    params  = {"per_page": 50, "status": "publish", "start_date": today, "page": 1}
    events  = []

    while True:
        try:
            resp = requests.get(api_url, params=params, headers=HEADERS, timeout=TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            log.warning("TEC REST error page %d for %s: %s", params["page"], source.get("name"), e)
            break

        for item in data.get("events", []):
            title = BeautifulSoup(
                (item.get("title") or {}).get("rendered", "") or item.get("title", ""),
                "html.parser"
            ).get_text(strip=True)
            if not title:
                continue
            desc  = BeautifulSoup(
                (item.get("description") or {}).get("rendered", "") or item.get("description", ""),
                "html.parser"
            ).get_text(separator=" ", strip=True)
            venue = item.get("venue") or {}
            img   = item.get("image") or {}
            events.append(_ev(
                title=title, description=desc,
                start_date=_normalize_dt(item.get("start_date", "")),
                end_date=_normalize_dt(item.get("end_date") or item.get("start_date", "")),
                venue_name=venue.get("venue", "") if isinstance(venue, dict) else "",
                ticket_url=item.get("url", ""),
                source=source, city_slug=city_slug, city_name=city_name,
                image_url=img.get("url", "") if isinstance(img, dict) else "",
                cost=item.get("cost", ""),
                external_id=f"tec_{item.get('id', '')}",
            ))

        if params["page"] >= data.get("total_pages", 1):
            break
        params["page"] += 1
        time.sleep(0.5)

    log.info("[%s] TEC REST: %d events", source.get("name"), len(events))
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
            resp = requests.get(url, headers={**HEADERS, "Accept": "application/json"}, timeout=TIMEOUT)
            resp.raise_for_status()
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
            title = (raw.get("title") or raw.get("name") or raw.get("event_name") or "").strip()
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


# ── Generic HTML ──────────────────────────────────────────────────────────────

def _scrape_generic_html(source: dict, city_slug: str, city_name: str) -> list[dict]:
    try:
        resp = requests.get(source["url"], headers=HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
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
                    title=te.get_text(strip=True),
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
    in _parse_heuristic).
    """
    if not text:
        return ""
    try:
        from dateutil import parser as dp
        dt = dp.parse(text, fuzzy=True, default=default) if default else dp.parse(text, fuzzy=True)
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
# stats, whether the known event-card CSS selectors matched anything, what
# _fallback_heading_containers found (or why it bailed), and the actual
# extracted events if any tier succeeds. Does NOT hit Ollama — this is for
# diagnosing tiers 1-3 only, since tier 4 is slow/expensive and not what's
# usually broken.

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

    print("\n=== Known CSS selector tiers (_EVENT_SELECTORS) ===")
    soup_dbg = BeautifulSoup(html, "html.parser")
    for sel in _EVENT_SELECTORS:
        found = soup_dbg.select(sel)
        if found:
            print(f"  MATCHED '{sel}': {len(found)} elements")
    else_matched = any(soup_dbg.select(sel) for sel in _EVENT_SELECTORS)
    if not else_matched:
        print("  No known selector matched anything — falling to heading-based detection.\n")

    print("=== _fallback_heading_containers diagnostic ===")
    soup_dbg2 = BeautifulSoup(html, "html.parser")
    headings = soup_dbg2.find_all(["h1", "h2", "h3", "h4"])
    print(f"Total h1-h4 tags on page: {len(headings)}")

    candidates = []
    for tag in headings:
        heading_text = tag.get_text(strip=True)
        if _MONTH_YEAR_RE.fullmatch(heading_text):
            continue
        a = tag.find("a", href=True)
        if not a:
            continue
        candidates.append((tag, a["href"]))
    print(f"Headings containing a direct <a href> (and not a month/year divider): {len(candidates)}")
    if candidates[:5]:
        print("First few candidate hrefs:")
        for _, href in candidates[:5]:
            print(f"  {href}")

    if len(candidates) < 3:
        print("\nBAILED: fewer than 3 heading+link candidates found. "
              "Real markup likely doesn't put an <a> directly inside the "
              "h1-h4 tag — e.g. the link might wrap the heading instead of "
              "containing it, or event titles might be in a different tag "
              "(div, span) entirely. Inspect the snippet above to see the "
              "actual structure.")
    else:
        from collections import Counter
        seg_counts = Counter()
        for _, href in candidates:
            parts = [p for p in href.split("/") if p]
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
