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

HEADERS = {"User-Agent": "Mozilla/5.0 (OpenClaw/1.0)"}
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


# ── Squarespace iCal scraper (S2F, Overton Park Shell) ────────────────────────

def scrape_squarespace(source, city):
    """
    Fetch a Squarespace event listing page, collect all ?format=ical links,
    then parse each ICS for clean title/date/description/image.
    """
    url = source["url"]
    log.info("Scraping squarespace '%s' for %s", url, city)

    r = _get(url)
    if not r:
        return []

    soup = BeautifulSoup(r.text, "html.parser")

    # Collect all individual event ICS URLs from the page
    ical_links = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "format=ical" in href:
            if href.startswith("/"):
                from urllib.parse import urlparse
                base = urlparse(url)
                href = f"{base.scheme}://{base.netloc}{href}"
            ical_links.append(href)

    log.info("[%s] Found %d iCal links", url, len(ical_links))

    events = []
    seen_titles = set()

    for ical_url in ical_links:
        r2 = _get(ical_url)
        if not r2:
            continue
        try:
            cal = Calendar.from_ical(r2.content)
        except Exception as e:
            log.warning("iCal parse failed for %s: %s", ical_url, e)
            continue

        for component in cal.walk():
            if component.name != "VEVENT":
                continue

            title = str(component.get("SUMMARY", "")).strip()
            if not title or title in seen_titles:
                continue
            seen_titles.add(title)

            dtstart = component.get("DTSTART")
            dtend = component.get("DTEND")
            if not dtstart:
                continue

            dt = dtstart.dt
            if isinstance(dt, date) and not isinstance(dt, datetime):
                dt = datetime(dt.year, dt.month, dt.day)
            start_date = _fmt(dt)

            end_dt = dtend.dt if dtend else dt
            if isinstance(end_dt, date) and not isinstance(end_dt, datetime):
                end_dt = datetime(end_dt.year, end_dt.month, end_dt.day)
            end_date = _fmt(end_dt)

            desc = str(component.get("DESCRIPTION", "")).strip()
            location = str(component.get("LOCATION", "")).strip()
            event_url = str(component.get("URL", "")).strip()

            # Image: look for the event detail page and scrape og:image
            image_url = _squarespace_og_image(event_url) if event_url else ""

            events.append({
                "title": title,
                "description": desc,
                "start_date": start_date,
                "end_date": end_date,
                "image_url": image_url,
                "ticket_url": event_url,
                "venue_name": location or source.get("name", ""),
                "source_name": source.get("name", url),
                "city_slug": city,
            })

    log.info("[%s] Parsed %d events", url, len(events))
    return events


def _squarespace_og_image(event_url):
    """Fetch event detail page and return og:image."""
    r = _get(event_url)
    if not r:
        return ""
    soup = BeautifulSoup(r.text, "html.parser")
    tag = soup.find("meta", property="og:image") or soup.find("meta", attrs={"name": "og:image"})
    if tag and tag.get("content"):
        return tag["content"]
    # fallback: first img with squarespace CDN
    img = soup.find("img", src=re.compile(r"squarespace-cdn\.com"))
    return img["src"] if img else ""


# ── SeeTickets HTML scraper (Hernando's Hideaway) ─────────────────────────────

def scrape_seetickets(source, city):
    """
    Parse SeeTickets embedded calendar HTML directly from the venue page.
    Events appear as article/div blocks with title, date, image, ticket link.
    """
    url = source["url"]
    log.info("Scraping seetickets '%s' for %s", url, city)

    r = _get(url)
    if not r:
        return []

    soup = BeautifulSoup(r.text, "html.parser")
    events = []

    # SeeTickets renders each event as an <a> wrapping an image + text block
    # Pattern: anchor with href to wl.seetickets.us, containing img + event name + date
    for a in soup.find_all("a", href=re.compile(r"seetickets\.us/event/")):
        ticket_url = a["href"].split("?")[0]  # strip affiliate params

        # Title: the text link right after the image anchor
        title_tag = soup.find("a", href=a["href"])
        # Walk siblings to find the event name text node
        title = ""
        parent = a.find_parent()
        if parent:
            # Find the next anchor with same href that has text
            for sibling_a in parent.find_all("a", href=True):
                if "seetickets.us/event/" in sibling_a["href"] and sibling_a.get_text(strip=True):
                    title = sibling_a.get_text(strip=True)
                    break

        # Image inside the anchor
        img = a.find("img")
        image_url = img["src"] if img else ""

        if not title:
            continue

        # Date: look in surrounding block for text matching date pattern
        # SeeTickets format: "Wed Jun 17" or "Fri Jun 19"
        block = a.find_parent("div") or a.find_parent("li") or a.find_parent()
        date_text = ""
        if block:
            text = block.get_text(" ", strip=True)
            m = re.search(r"(Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2}", text)
            if m:
                date_text = m.group(0)

        start_date = _parse_date(date_text + " 2026") if date_text else ""
        if not start_date:
            continue

        # Time: look for "Doors at H:MMpm / Show at H:MMpm" or similar
        time_text = ""
        if block:
            tm = re.search(r"Show at (\d+:\d+\s*[AP]M)", block.get_text(), re.IGNORECASE)
            if tm:
                time_text = tm.group(1)
                start_date = _parse_date(date_text + " 2026 " + time_text)

        # Description: any remaining text in the block
        desc = ""
        if block:
            desc = block.get_text(" ", strip=True)

        events.append({
            "title": title,
            "description": desc,
            "start_date": start_date,
            "end_date": start_date,
            "image_url": image_url,
            "ticket_url": ticket_url,
            "venue_name": "Hernando's Hideaway",
            "source_name": source.get("name", "Hernando's Hideaway"),
            "city_slug": city,
        })

    # Dedupe by title+date
    seen = set()
    deduped = []
    for e in events:
        key = e["title"].lower() + "|" + e["start_date"][:10]
        if key not in seen:
            seen.add(key)
            deduped.append(e)

    log.info("[%s] Parsed %d events", url, len(deduped))
    return deduped


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
