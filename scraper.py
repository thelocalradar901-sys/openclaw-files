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
    else:
        city_slug = str(city)
        city_name = str(city).title()

    stype = (source.get("source_type") or "html_auto").lower().strip()
    if stype in ("auto", "squarespace"):
        stype = "html_auto"

    tz_name = city.get("timezone", "America/Chicago") if isinstance(city, dict) else "America/Chicago"

    log.info("Scraping '%s' (%s) for %s", source.get("name", source.get("url")), stype, city_name)

    try:
        if stype == "html_auto":
            return _scrape_html_auto(source, city_slug, city_name, tz_name)
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

def _scrape_html_auto(source: dict, city_slug: str, city_name: str, tz_name: str = "America/Chicago") -> list[dict]:
    url = source["url"]
    try:
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
    except Exception as e:
        log.warning("Fetch failed for %s: %s", source.get("name"), e)
        return []

    html     = resp.text
    no_ical  = source.get("no_ical", False)

    # Tier 1: iCal
    ical_url = _detect_ical(html, url, no_ical=no_ical)
    if ical_url:
        events = _parse_ical(ical_url, source, city_slug, city_name, city.get('timezone') if isinstance(city, dict) else None)
        if events:
            log.info("[%s] iCal: %d events", source.get("name"), len(events))
            return events

    # Tier 2: JSON-LD
    events = _parse_jsonld(html, source, city_slug, city_name)
    if events:
        log.info("[%s] JSON-LD: %d events", source.get("name"), len(events))
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
    base_parts = urlparse(base_url)
    soup = BeautifulSoup(html, "html.parser")
    tag  = soup.find("link", {"type": "text/calendar"})
    if tag and tag.get("href"):
        return _abs(tag["href"], base_url)
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if ".ics" in href or "format=ical" in href:
            abs_href  = _abs(href, base_url)
            path      = urlparse(abs_href).path.rstrip("/")
            # Only use calendar-level feeds — skip individual event pages
            # (individual event paths have 3+ segments after domain)
            depth = len([s for s in path.split("/") if s])
            if depth <= 1:
                return abs_href
    return None


def _parse_ical(ical_url: str, source: dict, city_slug: str, city_name: str,
                tz_name: str = None) -> list[dict]:
    """Parse an iCal feed, converting UTC datetimes to local city time."""
    try:
        from icalendar import Calendar
        # Resolve timezone — use city tz, fall back to UTC
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

            # Skip past events
            now_local = datetime.now(local_tz)
            if dt.replace(tzinfo=local_tz if dt.tzinfo is None else dt.tzinfo) < now_local.replace(tzinfo=None if dt.tzinfo is None else now_local.tzinfo):
                pass  # let db.py handle dedup/expiry

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
            venue_name = loc.get("name", source.get("name", "")) if isinstance(loc, dict) else source.get("name", "")
            img        = item.get("image")
            image_url  = (img if isinstance(img, str)
                          else img.get("url", "") if isinstance(img, dict)
                          else (img[0] if isinstance(img, list) and img and isinstance(img[0], str)
                                else img[0].get("url", "") if isinstance(img, list) and img else ""))
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
    for c in containers[:60]:
        title_el = c.select_one("h1,h2,h3,[class*='title'],[class*='summary']")
        if not title_el:
            continue
        title = title_el.get_text(strip=True)
        if not title:
            continue

        date_el    = c.select_one("time,[class*='date']")
        date_str   = (date_el.get("datetime") or date_el.get_text(strip=True)) if date_el else ""
        start_date = ""

        # Squarespace: extract UTC datetime from Google Calendar link
        gcal_a = c.find("a", href=True)
        if gcal_a:
            for a in c.find_all("a", href=True):
                if "google.com/calendar" in a["href"] and "dates=" in a["href"]:
                    import re as _re
                    m = _re.search(r"dates=(\d{8}T\d{6}Z)", a["href"])
                    if m:
                        try:
                            raw_dt   = datetime.strptime(m.group(1), "%Y%m%dT%H%M%SZ")
                            utc_dt   = raw_dt.replace(tzinfo=ZoneInfo("UTC"))
                            local_dt = utc_dt.astimezone(ZoneInfo(tz_name))
                            start_date = local_dt.strftime("%Y-%m-%d %H:%M:%S")
                        except Exception:
                            pass
                    break

        if not start_date:
            start_date = _normalize_dt(date_str, tz_name) if date_str else _parse_fuzzy_date(date_str)


        img_el    = c.find("img")
        image_url = ""
        if img_el:
            image_url = img_el.get("src") or img_el.get("data-src") or ""
            # Squarespace CDN URLs — strip format param for cleaner URL
            if "squarespace-cdn.com" in image_url and "?" in image_url:
                image_url = image_url.split("?")[0]

        a_tag      = title_el.find("a", href=True) or c.find("a", href=True)
        ticket_url = _abs(a_tag["href"], base_url) if a_tag else ""

        p_tag = c.find("p")
        desc  = p_tag.get_text(strip=True) if p_tag else ""

        key = title.lower() + "|" + (start_date or "")[:10]
        if key in seen:
            continue
        seen.add(key)

        events.append(_ev(
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

def _scrape_ical_url(source: dict, city_slug: str, city_name: str) -> list[dict]:
    return _parse_ical(source["url"], source, city_slug, city_name)


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

        raw_list = (data if isinstance(data, list)
                    else next((data[k] for k in ("events","items","data","results") if k in data and isinstance(data[k], list)), []))
        if not raw_list:
            break

        for raw in raw_list:
            title = (raw.get("title") or raw.get("name") or raw.get("event_name") or "").strip()
            if not title:
                continue
            desc       = BeautifulSoup(raw.get("description") or raw.get("summary") or "", "html.parser").get_text(separator=" ", strip=True)
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
                de2= c.select_one(source.get("description_selector", ".description,p"))
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
        dt = datetime.fromisoformat(raw)
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
        from dateutil import parser as dp
        dt = dp.parse(text, fuzzy=True)
        if dt and dt.year > 2020:
            return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        pass
    return ""


def _abs(href: str, base_url: str) -> str:
    from urllib.parse import urljoin
    return urljoin(base_url, href)
