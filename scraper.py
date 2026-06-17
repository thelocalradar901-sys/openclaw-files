"""
scraper.py – OpenClaw multi-tier event scraper

Source types (set in openclaw-monitor plugin):
  html_auto        — Universal auto-detector: iCal → JSON-LD → heuristic CSS → Ollama AI
  seetickets       — SeeTickets embedded widget (Hernando's Hideaway, etc.)
  tec_rest         — TEC REST API (/wp-json/tribe/events/v1/events)
  ical_url         — Bare .ics URL
  json_api         — Generic paginated JSON endpoint
  generic_html     — CSS-selector based with Ollama fallback

All fetchers return a list of event dicts with these keys:
  title, description, start_date, end_date, image_url, ticket_url,
  venue_name, venue_address, venue_city, venue_state, venue_zip,
  organizer_name, cost, source_name, city_slug, categories, tags,
  external_id, _needs_enrichment (optional)
"""

import logging
import re
import time
from datetime import datetime, date

import requests
from bs4 import BeautifulSoup

log = logging.getLogger("openclaw.scraper")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; OpenClaw/1.0; +https://thelocalradar.com)",
    "Accept":     "text/html,application/xhtml+xml,*/*;q=0.9",
}
TIMEOUT = 20


# ── Public entry point ────────────────────────────────────────────────────────

def scrape_source(source: dict, city) -> list[dict]:
    """Route to the correct fetcher based on source_type."""
    if isinstance(city, dict):
        city_slug = city.get("slug", "")
        city_name = city.get("name", "")
    else:
        city_slug = str(city)
        city_name = str(city).title()

    stype = (source.get("source_type") or "html_auto").lower().strip()
    # Normalize legacy aliases
    if stype in ("auto", "squarespace"):
        stype = "html_auto"

    log.info("Scraping '%s' (%s) for %s", source.get("name", source.get("url")), stype, city_name or city_slug)

    try:
        if stype == "html_auto":
            return _scrape_html_auto(source, city_slug, city_name)
        elif stype == "seetickets":
            return _scrape_seetickets(source, city_slug, city_name)
        elif stype == "tec_rest":
            return _scrape_tec_rest(source, city_slug, city_name)
        elif stype == "ical_url":
            return _scrape_ical_url(source, city_slug, city_name)
        elif stype == "json_api":
            return _scrape_json_api(source, city_slug, city_name)
        elif stype == "generic_html":
            return _scrape_generic_html(source, city_slug, city_name)
        else:
            log.warning("Unknown source_type '%s' for %s — trying html_auto", stype, source.get("url"))
            return _scrape_html_auto(source, city_slug, city_name)
    except Exception as e:
        log.error("scrape_source crashed for '%s': %s", source.get("url"), e, exc_info=True)
        return []


# ── Event dict builder ────────────────────────────────────────────────────────

def _make_event(title, description, start_date, end_date, venue_name,
                ticket_url, source, city_slug, city_name,
                image_url="", cost="", external_id="", needs_enrichment=False) -> dict:
    return {
        "title":            title,
        "description":      description,
        "start_date":       start_date,
        "end_date":         end_date or start_date,
        "venue_name":       venue_name,
        "venue_address":    "",
        "venue_city":       city_name,
        "venue_state":      "",
        "venue_zip":        "",
        "organizer_name":   "",
        "image_url":        image_url,
        "ticket_url":       ticket_url,
        "cost":             cost,
        "source_name":      source.get("name", source.get("url", "")),
        "city_slug":        city_slug,
        "categories":       [],
        "tags":             [],
        "external_id":      external_id,
        "_needs_enrichment": needs_enrichment,
    }


# ── html_auto: 4-tier universal scraper ───────────────────────────────────────

def _scrape_html_auto(source: dict, city_slug: str, city_name: str) -> list[dict]:
    url = source["url"]
    try:
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
    except Exception as e:
        log.warning("html_auto fetch failed for %s: %s", source.get("name"), e)
        return []

    html = resp.text

    # Tier 1: iCal feed link on page
    ical_url = _detect_ical(html, url)
    if ical_url:
        events = _parse_ical(ical_url, source, city_slug, city_name)
        if events:
            log.info("[%s] iCal tier: %d events", source.get("name"), len(events))
            return events

    # Tier 2: JSON-LD structured data
    events = _parse_jsonld(html, source, city_slug, city_name)
    if events:
        log.info("[%s] JSON-LD tier: %d events", source.get("name"), len(events))
        return events

    # Tier 3: Heuristic CSS pattern matching
    events = _parse_heuristic(html, url, source, city_slug, city_name)
    if events:
        log.info("[%s] Heuristic tier: %d events", source.get("name"), len(events))
        return events

    # Tier 4: Ollama AI extraction
    log.info("[%s] Falling back to Ollama", source.get("name"))
    events = _ollama_extract(html, source, city_slug, city_name)
    if events:
        log.info("[%s] Ollama tier: %d events", source.get("name"), len(events))
    else:
        log.warning("[%s] All tiers failed — 0 events", source.get("name"))
    return events


def _detect_ical(html: str, base_url: str):
    soup = BeautifulSoup(html, "html.parser")
    tag  = soup.find("link", {"type": "text/calendar"})
    if tag and tag.get("href"):
        return _abs(tag["href"], base_url)
    for a in soup.find_all("a", href=True):
        if ".ics" in a["href"] or "format=ical" in a["href"]:
            return _abs(a["href"], base_url)
    return None


def _parse_ical(ical_url: str, source: dict, city_slug: str, city_name: str) -> list[dict]:
    try:
        from icalendar import Calendar
        resp = requests.get(ical_url, headers=HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
        cal    = Calendar.from_ical(resp.content)
        events = []
        for component in cal.walk():
            if component.name != "VEVENT":
                continue
            title = str(component.get("SUMMARY", "")).strip()
            dtstart = component.get("DTSTART")
            if not title or not dtstart:
                continue
            dt = dtstart.dt
            if isinstance(dt, date) and not isinstance(dt, datetime):
                dt = datetime(dt.year, dt.month, dt.day)
            start_date = dt.strftime("%Y-%m-%d %H:%M:%S")
            dtend  = component.get("DTEND")
            end_dt = dtend.dt if dtend else dt
            if isinstance(end_dt, date) and not isinstance(end_dt, datetime):
                end_dt = datetime(end_dt.year, end_dt.month, end_dt.day)
            end_date = end_dt.strftime("%Y-%m-%d %H:%M:%S")
            events.append(_make_event(
                title=title,
                description=str(component.get("DESCRIPTION", "")).strip(),
                start_date=start_date, end_date=end_date,
                venue_name=str(component.get("LOCATION", source.get("name", ""))).strip(),
                ticket_url=str(component.get("URL", "")).strip(),
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
            location  = item.get("location", {})
            venue_name = location.get("name", source.get("name", "")) if isinstance(location, dict) else source.get("name", "")
            image_url  = ""
            img = item.get("image")
            if isinstance(img, str):
                image_url = img
            elif isinstance(img, dict):
                image_url = img.get("url", "")
            elif isinstance(img, list) and img:
                first = img[0]
                image_url = first if isinstance(first, str) else first.get("url", "")
            events.append(_make_event(
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
    "[class*='event-item']", "[class*='event_item']", "[class*='eventCard']",
    "[class*='event-card']", "[class*='event-list-item']",
    "[class*='tribe-event']", "[class*='tribe_event']",
    "article[class*='event']", "li[class*='event']", ".vevent",
    "article.eventlist-event", "li.eventlist-event",
]


def _parse_heuristic(html: str, base_url: str, source: dict,
                     city_slug: str, city_name: str) -> list[dict]:
    soup       = BeautifulSoup(html, "html.parser")
    containers = []
    for sel in _EVENT_SELECTORS:
        found = soup.select(sel)
        if found:
            containers = found
            break
    if not containers:
        return []

    events = []
    seen   = set()
    for container in containers[:60]:
        title_el = container.select_one("h1,h2,h3,[class*='title'],[class*='summary']")
        if not title_el:
            continue
        title = title_el.get_text(strip=True)
        if not title:
            continue

        date_el  = container.select_one("time,[class*='date'],[datetime]")
        date_str = ""
        if date_el:
            date_str = date_el.get("datetime", "") or date_el.get_text(strip=True)
        start_date = _parse_fuzzy_date(date_str)

        # Image
        img_el    = container.find("img")
        image_url = ""
        if img_el:
            image_url = img_el.get("src") or img_el.get("data-src") or ""

        # Ticket URL
        a_tag      = title_el.find("a", href=True) or container.find("a", href=True)
        ticket_url = _abs(a_tag["href"], base_url) if a_tag else ""

        # Description
        p_tag = container.find("p")
        desc  = p_tag.get_text(strip=True) if p_tag else ""

        key = title.lower() + "|" + start_date[:10]
        if key in seen:
            continue
        seen.add(key)

        events.append(_make_event(
            title=title, description=desc,
            start_date=start_date, end_date=start_date,
            venue_name=source.get("name", ""),
            ticket_url=ticket_url, source=source,
            city_slug=city_slug, city_name=city_name,
            image_url=image_url,
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
            f"Return a JSON array. Each object must have these exact keys:\n"
            f"title, description, start_date (YYYY-MM-DD HH:MM:SS), end_date, "
            f"venue_name, ticket_url, cost, image_url.\n"
            f"If unknown, use empty string. Return ONLY valid JSON, no other text.\n\n"
            f"{text}"
        )
        resp = requests.post(
            f"{OLLAMA_HOST}/api/generate",
            json={"model": OLLAMA_MODEL, "prompt": prompt, "stream": False, "format": "json"},
            timeout=OLLAMA_TIMEOUT,
        )
        resp.raise_for_status()
        raw     = resp.json().get("response", "[]")
        # Strip <think> tags that Qwen3 sometimes emits
        raw     = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
        extracted = _json.loads(raw)
        if not isinstance(extracted, list):
            extracted = extracted.get("events", [])

        events = []
        for item in extracted:
            title = (item.get("title") or "").strip()
            if not title:
                continue
            events.append(_make_event(
                title=title,
                description=item.get("description", ""),
                start_date=_normalize_dt(item.get("start_date", "")),
                end_date=_normalize_dt(item.get("end_date", "") or item.get("start_date", "")),
                venue_name=item.get("venue_name", source.get("name", "")),
                ticket_url=item.get("ticket_url", ""),
                source=source, city_slug=city_slug, city_name=city_name,
                image_url=item.get("image_url", ""),
                cost=item.get("cost", ""),
            ))
        return events
    except Exception as e:
        log.error("Ollama extraction failed for %s: %s", source.get("name"), e, exc_info=True)
        return []


# ── SeeTickets widget ─────────────────────────────────────────────────────────

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

    # All unique SeeTickets event URLs on the page
    anchors    = soup.find_all("a", href=re.compile(r"seetickets\.us/event/"))
    event_urls = {}
    for a in anchors:
        canonical = a["href"].split("?")[0]
        event_urls.setdefault(canonical, []).append(a)

    for ticket_url, tags in event_urls.items():
        # Image anchor has an <img> child
        img_anchor = next((a for a in tags if a.find("img")), None)
        image_url  = ""
        if img_anchor:
            img = img_anchor.find("img")
            image_url = img.get("src", "") if img else ""

        # Title anchor has text and no img
        title_anchor = next((a for a in tags if a.get_text(strip=True) and not a.find("img")), None)
        title = title_anchor.get_text(strip=True) if title_anchor else ""
        if not title:
            continue

        # Walk up to find block containing date text
        block = None
        if title_anchor:
            for parent in title_anchor.parents:
                if parent.name in ("div", "li", "article", "section"):
                    if re.search(r"(Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+\w+\s+\d{1,2}", parent.get_text()):
                        block = parent
                        break

        # Date
        date_text = ""
        if block:
            text = block.get_text(" ", strip=True)
            m    = re.search(
                r"(Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d{1,2})",
                text
            )
            if m:
                year  = _infer_year(m.group(2))
                date_text = f"{m.group(2)} {m.group(3)} {year}"

        if not date_text:
            continue

        # Time — prefer Show at, fall back to Doors at
        time_text = ""
        if block:
            bt = block.get_text(" ", strip=True)
            tm = re.search(r"Show at (\d+:\d+\s*[AP]M)", bt, re.IGNORECASE)
            if not tm:
                tm = re.search(r"Doors at (\d+:\d+\s*[AP]M)", bt, re.IGNORECASE)
            if tm:
                time_text = tm.group(1)

        start_date = _parse_fuzzy_date(f"{date_text} {time_text}".strip())
        if not start_date:
            continue

        # Description
        desc_parts = []
        if block:
            bt = block.get_text(" ", strip=True)
            supp = re.search(r"Supporting Talent:\s*(.+?)(?:\n|at Hernando)", bt, re.IGNORECASE)
            if supp:
                desc_parts.append(f"Supporting: {supp.group(1).strip()}")
            time_info = re.findall(r"(?:Doors|Show) at \d+:\d+\s*[AP]M", bt, re.IGNORECASE)
            if time_info:
                desc_parts.append(" / ".join(time_info))
            price = re.search(r"\$[\d\.]+-?\$?[\d\.]*", bt)
            if price:
                desc_parts.append(price.group(0))
        desc = " | ".join(desc_parts) if desc_parts else ""

        key = title.lower() + "|" + start_date[:10]
        if key in seen:
            continue
        seen.add(key)

        events.append(_make_event(
            title=title, description=desc,
            start_date=start_date, end_date=start_date,
            venue_name=source.get("venue_name", source.get("name", "")),
            ticket_url=ticket_url, source=source,
            city_slug=city_slug, city_name=city_name,
            image_url=image_url,
        ))

    log.info("[%s] Parsed %d events", url, len(events))
    return events


def _infer_year(month_abbr: str) -> int:
    now    = datetime.now()
    months = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
    try:
        m_idx = months.index(month_abbr) + 1
    except ValueError:
        return now.year
    return now.year + 1 if m_idx < now.month else now.year


# ── TEC REST API ──────────────────────────────────────────────────────────────

def _scrape_tec_rest(source: dict, city_slug: str, city_name: str) -> list[dict]:
    from urllib.parse import urlparse
    base     = source["url"].rstrip("/")
    parsed   = urlparse(base)
    api_url  = f"{parsed.scheme}://{parsed.netloc}/wp-json/tribe/events/v1/events"
    today    = datetime.now().strftime("%Y-%m-%d")
    params   = {"per_page": 50, "status": "publish", "start_date": today, "page": 1}
    events   = []

    while True:
        try:
            resp = requests.get(api_url, params=params, headers=HEADERS, timeout=TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            log.warning("TEC REST error page %d for %s: %s", params["page"], source.get("name"), e)
            break

        items = data.get("events", [])
        if not items:
            break

        for item in items:
            title = (item.get("title") or {}).get("rendered", "") or item.get("title", "")
            title = BeautifulSoup(title, "html.parser").get_text(strip=True)
            if not title:
                continue

            desc = BeautifulSoup(
                (item.get("description") or {}).get("rendered", "") or item.get("description", ""),
                "html.parser"
            ).get_text(separator=" ", strip=True)

            start_date = _normalize_dt(item.get("start_date", ""))
            end_date   = _normalize_dt(item.get("end_date", "") or item.get("start_date", ""))
            venue      = item.get("venue") or {}
            image_url  = ""
            img = item.get("image")
            if isinstance(img, dict):
                image_url = img.get("url", "")

            events.append(_make_event(
                title=title, description=desc,
                start_date=start_date, end_date=end_date,
                venue_name=venue.get("venue", "") if isinstance(venue, dict) else "",
                ticket_url=item.get("url", ""),
                source=source, city_slug=city_slug, city_name=city_name,
                image_url=image_url,
                cost=item.get("cost", ""),
                external_id=f"tec_{item.get('id', '')}",
            ))

        if params["page"] >= data.get("total_pages", 1):
            break
        params["page"] += 1
        time.sleep(0.5)

    log.info("[%s] TEC REST: %d events", source.get("name"), len(events))
    return events


# ── Direct iCal URL ───────────────────────────────────────────────────────────

def _scrape_ical_url(source: dict, city_slug: str, city_name: str) -> list[dict]:
    return _parse_ical(source["url"], source, city_slug, city_name)


# ── Generic JSON API ──────────────────────────────────────────────────────────

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

        raw_events = _extract_json_list(data)
        if not raw_events:
            break

        for raw in raw_events:
            title = (raw.get("title") or raw.get("name") or raw.get("event_name") or "").strip()
            if not title:
                continue
            desc = BeautifulSoup(
                raw.get("description") or raw.get("summary") or raw.get("body") or "",
                "html.parser"
            ).get_text(separator=" ", strip=True)
            start_raw   = (raw.get("start_date") or raw.get("startDate") or raw.get("date") or raw.get("start") or "")
            end_raw     = (raw.get("end_date")   or raw.get("endDate")   or raw.get("end")   or start_raw)
            venue_name  = raw.get("venue") or raw.get("venue_name") or raw.get("location") or ""
            if isinstance(venue_name, dict):
                venue_name = venue_name.get("name", "")
            image_url   = raw.get("image") or raw.get("image_url") or raw.get("thumbnail") or ""
            if isinstance(image_url, dict):
                image_url = image_url.get("url", "")
            events.append(_make_event(
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


def _extract_json_list(data) -> list:
    if isinstance(data, list):
        return data
    for key in ("events", "items", "data", "results", "event_list"):
        if key in data and isinstance(data[key], list):
            return data[key]
    return []


# ── Generic HTML with Ollama fallback ─────────────────────────────────────────

def _scrape_generic_html(source: dict, city_slug: str, city_name: str) -> list[dict]:
    try:
        resp = requests.get(source["url"], headers=HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
    except Exception as e:
        log.warning("Generic HTML fetch failed for %s: %s", source.get("name"), e)
        return []

    soup    = BeautifulSoup(resp.text, "html.parser")
    sel     = source.get("event_container_selector")
    if sel:
        containers = soup.select(sel)
        if containers:
            events = []
            for c in containers:
                title_el = c.select_one(source.get("title_selector", "h2,h3,.event-title"))
                date_el  = c.select_one(source.get("date_selector",  ".event-date,time"))
                desc_el  = c.select_one(source.get("description_selector", ".description,p"))
                link_el  = c.select_one("a[href]")
                img_el   = c.select_one("img")
                title    = title_el.get_text(strip=True) if title_el else ""
                if not title:
                    continue
                events.append(_make_event(
                    title=title,
                    description=desc_el.get_text(separator=" ", strip=True) if desc_el else "",
                    start_date=_parse_fuzzy_date(date_el.get_text(strip=True) if date_el else ""),
                    end_date="",
                    venue_name=source.get("name", ""),
                    ticket_url=link_el["href"] if link_el else "",
                    source=source, city_slug=city_slug, city_name=city_name,
                    image_url=img_el.get("src", "") if img_el else "",
                    needs_enrichment=True,
                ))
            return events

    return _ollama_extract(resp.text, source, city_slug, city_name)


# ── Date helpers ──────────────────────────────────────────────────────────────

def _normalize_dt(raw) -> str:
    if not raw:
        return ""
    if isinstance(raw, (int, float)):
        try:
            return datetime.fromtimestamp(raw).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return ""
    raw = str(raw).strip().replace("Z", "+00:00")
    if re.match(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", raw):
        return raw
    try:
        from datetime import datetime as _dt
        dt = _dt.fromisoformat(raw)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        pass
    m = re.search(r"(\d{4}-\d{2}-\d{2})", raw)
    if m:
        return m.group(1) + " 00:00:00"
    return _parse_fuzzy_date(raw)


def _parse_fuzzy_date(text: str) -> str:
    if not text:
        return ""
    try:
        from dateutil import parser as dateparser
        dt = dateparser.parse(text, fuzzy=True)
        if dt and dt.year > 2020:
            return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        pass
    return ""


def _abs(href: str, base_url: str) -> str:
    from urllib.parse import urljoin
    return urljoin(base_url, href)
