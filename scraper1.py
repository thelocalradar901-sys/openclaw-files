"""
OpenClaw scraper — purpose-built fetchers per source type.

source_type values:
  squarespace   — S2F, Overton Park Shell (iCal listing page)
  seetickets    — Hernando's Hideaway (SeeTickets embedded widget)
  tec_rest      — Crosstown Concourse (WordPress TEC REST API)
  ical_url      — any bare .ics URL

Each fetcher returns a list of event dicts:
  title, description, start_date, end_date,
  image_url, ticket_url, venue_name, source_name, city_slug
"""

import logging
import re
import requests
from bs4 import BeautifulSoup
from datetime import datetime, date
from icalendar import Calendar
from dateutil import parser as dateparser

log = logging.getLogger("openclaw.scraper")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}
TIMEOUT = 15


# ── Helpers ────────────────────────────────────────────────────────────────────

def _fmt(dt):
    if isinstance(dt, datetime):
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(dt, date):
        return datetime(dt.year, dt.month, dt.day).strftime("%Y-%m-%d %H:%M:%S")
    return str(dt)


def _parse_date(text):
    """Parse any human-readable date string into YYYY-MM-DD HH:MM:SS. Returns '' on failure."""
    if not text:
        return ""
    text = str(text).strip()
    # Already formatted
    if re.match(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", text):
        return text
    try:
        dt = dateparser.parse(text, fuzzy=True)
        if dt and dt.year > 2020:
            return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        pass
    return ""


def _get(url, **kwargs):
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT, **kwargs)
        r.raise_for_status()
        return r
    except Exception as e:
        log.warning("GET %s failed: %s", url, e)
        return None


# ── Squarespace listing-page scraper (S2F, Overton Park Shell) ────────────────
#
# Scrapes the event listing page directly — no iCal fetches, no detail page
# fetches. Everything we need (title, date/time, image, description, URL) is
# already rendered in the listing HTML. This avoids server-side 403s on detail
# pages and cuts per-source requests down to 1.
#
# Squarespace event block structure (both sites match this pattern):
#   <a href="..."><img src="squarespace-cdn.com/..."></a>   ← image anchor
#   <h1 class="..."><a href="...">EVENT TITLE</a></h1>
#   <li>Day, Month D, YYYY</li>
#   <li>H:MM AM – H:MM AM</li>
#   <li>Venue / Address</li>
#   <a href="...?format=ical">ICS</a>                       ← per-event ical link
#   <p>Description text...</p>

def scrape_squarespace(source, city):
    """
    Fetch a Squarespace event listing page and parse events directly from HTML.
    No detail page or iCal fetches — everything is on the listing page.
    """
    from urllib.parse import urlparse

    url = source["url"]
    log.info("Scraping squarespace (listing) '%s' for %s", url, city)

    r = _get(url)
    if not r:
        return []

    soup = BeautifulSoup(r.text, "html.parser")
    base_url = f"{urlparse(url).scheme}://{urlparse(url).netloc}"

    events = []
    seen = set()

    # Each event on a Squarespace events page is wrapped in an <article> or a
    # list item with class containing "eventlist-event". Both S2F and Overton
    # use this pattern. Fall back to scanning all h1/h2 with event links.
    blocks = soup.select("article.eventlist-event, li.eventlist-event, div.eventlist-event")
    if not blocks:
        # Fallback: find all iCal links and use their parent container
        ical_anchors = soup.find_all("a", href=re.compile(r"format=ical"))
        blocks = [a.find_parent(["article", "li", "div"]) for a in ical_anchors if a.find_parent(["article", "li", "div"])]
        blocks = [b for b in blocks if b]  # remove None

    log.info("[%s] Found %d event blocks", url, len(blocks))

    for block in blocks:
        # ── Title ──────────────────────────────────────────────────────────────
        title_tag = block.find(["h1", "h2", "h3"], class_=re.compile(r"event.*title|title.*event", re.I))
        if not title_tag:
            title_tag = block.find(["h1", "h2", "h3"])
        if not title_tag:
            continue
        title = title_tag.get_text(strip=True)
        if not title:
            continue

        # ── Ticket/detail URL ──────────────────────────────────────────────────
        ticket_url = ""
        title_a = title_tag.find("a", href=True)
        if title_a:
            href = title_a["href"]
            if href.startswith("/"):
                href = base_url + href
            ticket_url = href

        # ── Image ──────────────────────────────────────────────────────────────
        image_url = ""
        img = block.find("img", src=re.compile(r"squarespace-cdn\.com|cdn\.squarespace"))
        if img:
            image_url = img.get("src") or img.get("data-src", "")
        if not image_url:
            # Try any img in block
            img = block.find("img")
            if img:
                image_url = img.get("src") or img.get("data-src", "")

        # ── Date / Time ────────────────────────────────────────────────────────
        # Squarespace renders dates in <time> tags with datetime attr, or in
        # <li> text. Prefer <time datetime="..."> for reliability.
        start_date = ""
        end_date = ""

        time_tags = block.find_all("time")
        if time_tags:
            dts = [t.get("datetime", "") for t in time_tags if t.get("datetime")]
            if dts:
                start_date = _parse_date(dts[0])
                end_date = _parse_date(dts[-1]) if len(dts) > 1 else start_date

        if not start_date:
            # Fall back: find the iCal link for this block and parse its datetime
            ical_a = block.find("a", href=re.compile(r"format=ical"))
            if ical_a:
                ical_href = ical_a["href"]
                if ical_href.startswith("/"):
                    ical_href = base_url + ical_href
                r2 = _get(ical_href)
                if r2:
                    try:
                        cal = Calendar.from_ical(r2.content)
                        for component in cal.walk():
                            if component.name == "VEVENT":
                                dtstart = component.get("DTSTART")
                                dtend = component.get("DTEND")
                                if dtstart:
                                    dt = dtstart.dt
                                    if isinstance(dt, date) and not isinstance(dt, datetime):
                                        dt = datetime(dt.year, dt.month, dt.day)
                                    start_date = _fmt(dt)
                                    end_dt = dtend.dt if dtend else dt
                                    if isinstance(end_dt, date) and not isinstance(end_dt, datetime):
                                        end_dt = datetime(end_dt.year, end_dt.month, end_dt.day)
                                    end_date = _fmt(end_dt)
                                break
                    except Exception as e:
                        log.warning("iCal fallback parse failed for %s: %s", ical_href, e)

        if not start_date:
            # Last resort: any date-like text in the block
            block_text = block.get_text(" ", strip=True)
            start_date = _parse_date(block_text)

        if not start_date:
            log.debug("No date found for '%s', skipping", title)
            continue

        if not end_date:
            end_date = start_date

        # ── Description ────────────────────────────────────────────────────────
        desc = ""
        for sel in [
            "div.eventlist-description",
            "div.sqs-block-content",
            "div.event-excerpt",
            "p.eventlist-excerpt",
        ]:
            block_desc = block.select_one(sel)
            if block_desc:
                desc = block_desc.get_text(" ", strip=True)[:1000]
                break
        if not desc:
            # Grab all <p> text in block
            paras = [p.get_text(" ", strip=True) for p in block.find_all("p") if p.get_text(strip=True)]
            desc = " ".join(paras)[:1000]

        # ── Venue ──────────────────────────────────────────────────────────────
        venue_name = source.get("name", "")
        loc_tag = block.find(class_=re.compile(r"event.*location|location.*event", re.I))
        if loc_tag:
            venue_name = loc_tag.get_text(strip=True) or venue_name

        # ── Dedupe ─────────────────────────────────────────────────────────────
        key = title.lower() + "|" + start_date[:10]
        if key in seen:
            continue
        seen.add(key)

        events.append({
            "title": title,
            "description": desc,
            "start_date": start_date,
            "end_date": end_date,
            "image_url": image_url,
            "ticket_url": ticket_url,
            "venue_name": venue_name,
            "source_name": source.get("name", url),
            "city_slug": city,
        })

    log.info("[%s] Parsed %d events", url, len(events))
    return events


# ── SeeTickets HTML scraper (Hernando's Hideaway) ─────────────────────────────

def _seetickets_infer_year(month_str):
    """
    Given a 3-letter month abbreviation (e.g. 'Jun'), return the most likely
    4-digit year. If the month is earlier than the current month, assume next
    year (handles Dec→Jan rollover cleanly).
    """
    now = datetime.now()
    months = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
    try:
        m_idx = months.index(month_str) + 1  # 1-based
    except ValueError:
        return now.year
    # If that month has already passed this year, it must be next year
    if m_idx < now.month:
        return now.year + 1
    return now.year


def scrape_seetickets(source, city):
    """
    Parse SeeTickets embedded calendar HTML directly from the venue page.
    Each event is a self-contained block containing:
      - an <a href="wl.seetickets.us/event/..."> wrapping an <img>  (image anchor)
      - a separate <a href="wl.seetickets.us/event/..."> with title text
      - plain text nodes with date ("Wed Jun 17"), time ("Doors at 6:00PM / Show at 8:00PM"),
        venue, genre, price

    Strategy: find all unique seetickets event URLs on the page, then for each
    URL collect ALL anchors sharing that href — one will be the image anchor,
    one will be the title anchor. Grab the nearest common ancestor as the block.
    """
    url = source["url"]
    log.info("Scraping seetickets '%s' for %s", url, city)

    r = _get(url)
    if not r:
        return []

    soup = BeautifulSoup(r.text, "html.parser")
    events = []
    seen = set()

    # Collect all unique SeeTickets event URLs on the page
    all_st_anchors = soup.find_all("a", href=re.compile(r"seetickets\.us/event/"))
    event_urls = {}  # canonical_url -> list of <a> tags
    for a in all_st_anchors:
        canonical = a["href"].split("?")[0]
        event_urls.setdefault(canonical, []).append(a)

    for ticket_url, anchors in event_urls.items():
        # ── Image ──────────────────────────────────────────────────────────────
        image_url = ""
        img_anchor = next((a for a in anchors if a.find("img")), None)
        if img_anchor:
            img = img_anchor.find("img")
            image_url = img.get("src", "") if img else ""

        # ── Title ──────────────────────────────────────────────────────────────
        # The title anchor has visible text and no img child
        title = ""
        title_anchor = next(
            (a for a in anchors if a.get_text(strip=True) and not a.find("img")),
            None
        )
        if title_anchor:
            title = title_anchor.get_text(strip=True)

        if not title:
            continue

        # ── Block: nearest common ancestor of all anchors for this event ───────
        # Walk up from the title anchor to find a block-level container that
        # also contains the date text.
        block = None
        if title_anchor:
            for parent in title_anchor.parents:
                if parent.name in ("div", "li", "article", "section"):
                    # Check it contains a date pattern
                    if re.search(r"(Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+\w+\s+\d{1,2}", parent.get_text()):
                        block = parent
                        break

        # ── Date ───────────────────────────────────────────────────────────────
        date_text = ""
        month_str = ""
        if block:
            text = block.get_text(" ", strip=True)
            m = re.search(
                r"(Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d{1,2})",
                text
            )
            if m:
                month_str = m.group(2)
                year = _seetickets_infer_year(month_str)
                date_text = f"{m.group(2)} {m.group(3)} {year}"

        if not date_text:
            continue

        # ── Time ───────────────────────────────────────────────────────────────
        # Prefer "Show at H:MMpm", fall back to "Doors at H:MMpm"
        time_text = ""
        if block:
            block_text = block.get_text(" ", strip=True)
            tm = re.search(r"Show at (\d+:\d+\s*[AP]M)", block_text, re.IGNORECASE)
            if not tm:
                tm = re.search(r"Doors at (\d+:\d+\s*[AP]M)", block_text, re.IGNORECASE)
            if tm:
                time_text = tm.group(1)

        start_date = _parse_date(f"{date_text} {time_text}".strip())
        if not start_date:
            continue

        # ── Description ────────────────────────────────────────────────────────
        # Build a clean description from: supporting talent, genre, price
        desc_parts = []
        if block:
            block_text = block.get_text(" ", strip=True)
            # Supporting talent line
            supp = re.search(r"Supporting Talent:\s*(.+?)(?:\n|at Hernando)", block_text, re.IGNORECASE)
            if supp:
                desc_parts.append(f"Supporting: {supp.group(1).strip()}")
            # Genre
            genre_m = re.search(r"\b(Country|Rock|Blues|Soul|Jazz|Pop|Folk|R&B|Hip-?Hop|Metal|Indie)\b", block_text, re.IGNORECASE)
            if genre_m:
                desc_parts.append(genre_m.group(1))
            # Price
            price_m = re.search(r"\$[\d\.]+-?\$?[\d\.]*", block_text)
            if price_m:
                desc_parts.append(price_m.group(0))
            # Doors/show times
            time_info = re.findall(r"(?:Doors|Show) at \d+:\d+\s*[AP]M", block_text, re.IGNORECASE)
            if time_info:
                desc_parts.append(" / ".join(time_info))

        desc = " | ".join(desc_parts) if desc_parts else title

        # ── Dedupe ─────────────────────────────────────────────────────────────
        key = title.lower() + "|" + start_date[:10]
        if key in seen:
            continue
        seen.add(key)

        events.append({
            "title": title,
            "description": desc,
            "start_date": start_date,
            "end_date": start_date,
            "image_url": image_url,
            "ticket_url": ticket_url,
            "venue_name": source.get("venue_name", "Hernando's Hideaway"),
            "source_name": source.get("name", "Hernando's Hideaway"),
            "city_slug": city,
        })

    log.info("[%s] Parsed %d events", url, len(events))
    return events


# ── TEC REST API scraper (Crosstown Concourse) ────────────────────────────────

def scrape_tec_rest(source, city):
    """
    Fetch events from a WordPress site running The Events Calendar REST API.
    Endpoint: /wp-json/tribe/events/v1/events
    """
    url = source["url"].rstrip("/")
    from urllib.parse import urlparse
    base = urlparse(url)
    api_base = f"{base.scheme}://{base.netloc}"
    api_url = f"{api_base}/wp-json/tribe/events/v1/events"

    today = datetime.now().strftime("%Y-%m-%d")
    params = {"per_page": 50, "status": "publish", "start_date": today, "page": 1}

    log.info("Scraping TEC REST '%s' for %s", api_url, city)

    events = []
    while True:
        r = _get(api_url, params=params)
        if not r:
            break
        try:
            data = r.json()
        except Exception:
            break

        items = data.get("events", [])
        if not items:
            break

        for item in items:
            title = item.get("title", "").strip()
            if not title:
                continue

            start_date = _parse_date(item.get("start_date", ""))
            end_date = _parse_date(item.get("end_date", "")) or start_date
            desc = BeautifulSoup(item.get("description", ""), "html.parser").get_text(" ", strip=True)
            ticket_url = item.get("url", "")

            image_url = ""
            img = item.get("image", {})
            if isinstance(img, dict):
                image_url = img.get("url", "")

            venue_name = ""
            venue = item.get("venue", {})
            if isinstance(venue, dict):
                venue_name = venue.get("venue", "")

            events.append({
                "title": title,
                "description": desc,
                "start_date": start_date,
                "end_date": end_date,
                "image_url": image_url,
                "ticket_url": ticket_url,
                "venue_name": venue_name or source.get("name", ""),
                "source_name": source.get("name", url),
                "city_slug": city,
            })

        total_pages = data.get("total_pages", 1)
        if params["page"] >= total_pages:
            break
        params["page"] += 1

    log.info("[%s] Parsed %d events", api_url, len(events))
    return events


# ── Bare iCal URL ──────────────────────────────────────────────────────────────

def scrape_ical_url(source, city):
    """Fetch a bare .ics URL and parse all VEVENTs."""
    url = source["url"]
    log.info("Scraping iCal URL '%s' for %s", url, city)
    r = _get(url)
    if not r:
        return []
    try:
        cal = Calendar.from_ical(r.content)
    except Exception as e:
        log.warning("iCal parse failed: %s", e)
        return []

    events = []
    for component in cal.walk():
        if component.name != "VEVENT":
            continue
        title = str(component.get("SUMMARY", "")).strip()
        if not title:
            continue
        dtstart = component.get("DTSTART")
        if not dtstart:
            continue
        dt = dtstart.dt
        if isinstance(dt, date) and not isinstance(dt, datetime):
            dt = datetime(dt.year, dt.month, dt.day)
        start_date = _fmt(dt)
        dtend = component.get("DTEND")
        end_dt = dtend.dt if dtend else dt
        if isinstance(end_dt, date) and not isinstance(end_dt, datetime):
            end_dt = datetime(end_dt.year, end_dt.month, end_dt.day)
        end_date = _fmt(end_dt)
        desc = str(component.get("DESCRIPTION", "")).strip()
        event_url = str(component.get("URL", "")).strip()
        events.append({
            "title": title,
            "description": desc,
            "start_date": start_date,
            "end_date": end_date,
            "image_url": "",
            "ticket_url": event_url,
            "venue_name": str(component.get("LOCATION", "")).strip() or source.get("name", ""),
            "source_name": source.get("name", url),
            "city_slug": city,
        })
    log.info("[%s] Parsed %d events", url, len(events))
    return events


# ── Dispatcher ─────────────────────────────────────────────────────────────────

def scrape_source(source, city):
    if isinstance(city, dict): city = city.get("slug", "")
    """
    Main entry point. Routes to the right fetcher based on source_type.
    Returns list of event dicts ready for db.insert_event().
    """
    stype = source.get("source_type", "squarespace").lower()

    try:
        if stype == "squarespace":
            return scrape_squarespace(source, city)
        elif stype == "seetickets":
            return scrape_seetickets(source, city)
        elif stype == "tec_rest":
            return scrape_tec_rest(source, city)
        elif stype == "ical_url":
            return scrape_ical_url(source, city)
        else:
            log.warning("Unknown source_type '%s' for %s — trying squarespace", stype, source["url"])
            return scrape_squarespace(source, city)
    except Exception as e:
        log.error("scrape_source failed for %s: %s", source.get("url"), e, exc_info=True)
        return []
