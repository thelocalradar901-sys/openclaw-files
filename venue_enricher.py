"""
venue_enricher.py — Automatic venue enrichment for OpenClaw

For each TM event with a weak image or description, this script:
  1. Looks up the venue in wp_openclaw_venues cache
  2. If no URL cached: asks Qwen3 for the venue's official events page URL
  3. Scrapes that URL for a matching event (fuzzy title + date)
  4. If matched: upgrades image and/or description in WordPress

Run after ticketmaster pulls, or as a standalone daily script.

Cron (daily at 3am):
  0 3 * * * /opt/openclaw/venv/bin/python /opt/openclaw/venue_enricher.py >> /var/log/openclaw-enricher.log 2>&1

Schema (auto-created on first run):
  wp_openclaw_venues:
    id, venue_name, city_slug, events_url, status, last_checked, notes
    status: 'active' | 'skip' | 'pending'
"""

import hashlib
import logging
import os
import re
import time
import urllib.request
from datetime import datetime, timedelta
from difflib import SequenceMatcher

import pymysql
import pymysql.cursors
import requests
from bs4 import BeautifulSoup

# ── Bootstrap ─────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("openclaw.venue_enricher")

env_file = "/etc/openclaw/openclaw.env"
if os.path.exists(env_file):
    with open(env_file) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

from config import DB, WP_PREFIX, OLLAMA_HOST, OLLAMA_MODEL, OLLAMA_TIMEOUT, SIDELOAD_IMAGES

# ── Settings ──────────────────────────────────────────────────────────────────
TITLE_MATCH_THRESHOLD  = 0.60   # fuzzy title similarity required (0-1)
DATE_WINDOW_DAYS       = 2      # how many days either side to consider a date match
MAX_VENUES_PER_RUN     = 50     # cap to avoid runaway Ollama calls
MAX_EVENTS_PER_RUN     = 200    # cap on events to attempt enrichment for
RECHECK_DAYS           = 30     # re-ask Ollama for venue URL after this many days
REQUESTS_TIMEOUT       = 15
REQUESTS_HEADERS       = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


# ── DB ────────────────────────────────────────────────────────────────────────

def get_conn():
    return pymysql.connect(
        host=DB["host"], port=DB["port"], user=DB["user"],
        password=DB["password"], database=DB["database"],
        charset=DB["charset"], cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
    )


def ensure_venues_table(conn):
    """Create wp_openclaw_venues if it doesn't exist."""
    with conn.cursor() as cur:
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {WP_PREFIX}openclaw_venues (
                id           INT AUTO_INCREMENT PRIMARY KEY,
                venue_name   VARCHAR(255) NOT NULL,
                city_slug    VARCHAR(64)  NOT NULL,
                events_url   VARCHAR(500) DEFAULT '',
                status       VARCHAR(20)  DEFAULT 'pending',
                last_checked DATETIME     DEFAULT NULL,
                notes        TEXT         DEFAULT '',
                UNIQUE KEY venue_city (venue_name(191), city_slug)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
        """)
    conn.commit()
    log.info("wp_openclaw_venues table ready")


def get_venue_record(conn, venue_name: str, city_slug: str) -> dict | None:
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT * FROM {WP_PREFIX}openclaw_venues "
            f"WHERE venue_name=%s AND city_slug=%s LIMIT 1",
            (venue_name, city_slug)
        )
        return cur.fetchone()


def upsert_venue(conn, venue_name: str, city_slug: str,
                 events_url: str, status: str, notes: str = ""):
    with conn.cursor() as cur:
        cur.execute(f"""
            INSERT INTO {WP_PREFIX}openclaw_venues
                (venue_name, city_slug, events_url, status, last_checked, notes)
            VALUES (%s, %s, %s, %s, NOW(), %s)
            ON DUPLICATE KEY UPDATE
                events_url   = VALUES(events_url),
                status       = VALUES(status),
                last_checked = NOW(),
                notes        = VALUES(notes)
        """, (venue_name, city_slug, events_url, status, notes))
    conn.commit()


def fetch_weak_tm_events(conn, limit: int = MAX_EVENTS_PER_RUN) -> list[dict]:
    """
    Find published TM events with missing/weak image OR short/missing description.
    Only looks at future events within 180 days.
    """
    now     = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    future  = (datetime.now() + timedelta(days=180)).strftime("%Y-%m-%d %H:%M:%S")

    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT
                p.ID,
                p.post_title                                         AS title,
                MAX(CASE WHEN pm.meta_key='_openclaw_source'  THEN pm.meta_value END) AS source,
                MAX(CASE WHEN pm.meta_key='_openclaw_city'    THEN pm.meta_value END) AS city_slug,
                MAX(CASE WHEN pm.meta_key='_EventStartDate'   THEN pm.meta_value END) AS start_date,
                MAX(CASE WHEN pm.meta_key='_EventVenueID'     THEN pm.meta_value END) AS venue_id,
                MAX(CASE WHEN pm.meta_key='_EventDescription' THEN pm.meta_value END) AS description,
                p.post_content,
                MAX(CASE WHEN pm.meta_key='_thumbnail_id'     THEN pm.meta_value END) AS thumbnail_id
            FROM {WP_PREFIX}posts p
            JOIN {WP_PREFIX}postmeta pm ON p.ID = pm.post_id
            WHERE p.post_type   = 'tribe_events'
              AND p.post_status = 'publish'
            GROUP BY p.ID
            HAVING
                source     = 'Ticketmaster'
                AND city_slug IS NOT NULL
                AND start_date >= %s
                AND start_date <= %s
                AND (
                    thumbnail_id IS NULL
                    OR CHAR_LENGTH(COALESCE(description, post_content, '')) < 100
                )
            ORDER BY start_date ASC
            LIMIT %s
        """, (now, future, limit))
        rows = cur.fetchall()

    # Enrich with venue name
    events = []
    for row in rows:
        venue_name = ""
        if row.get("venue_id"):
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT post_title FROM {WP_PREFIX}posts WHERE ID=%s LIMIT 1",
                    (row["venue_id"],)
                )
                v = cur.fetchone()
                if v:
                    venue_name = v["post_title"]
        if not venue_name:
            continue
        events.append({
            "post_id":     row["ID"],
            "title":       row["title"],
            "city_slug":   row["city_slug"] or "",
            "start_date":  row["start_date"] or "",
            "venue_name":  venue_name,
            "description": (row["description"] or row["post_content"] or "").strip(),
            "has_image":   bool(row.get("thumbnail_id")),
        })

    log.info("Found %d weak TM events to enrich", len(events))
    return events


# ── Ollama venue URL lookup ───────────────────────────────────────────────────

def ask_ollama_for_venue_url(venue_name: str, city_slug: str) -> str:
    """
    Ask Qwen3 for the official events page URL of a venue.
    Returns a URL string or '' if not found/confident.
    """
    city_label = city_slug.replace("-", " ").title()
    prompt = (
        f"What is the official events calendar URL for '{venue_name}' in {city_label}? "
        f"Return ONLY the full URL (starting with https://) of the page that lists their "
        f"upcoming events. If you are not confident, return exactly: UNKNOWN. "
        f"Do not explain. Do not add any other text."
    )

    try:
        resp = requests.post(
            f"{OLLAMA_HOST}/api/generate",
            json={
                "model":  OLLAMA_MODEL,
                "prompt": prompt,
                "stream": False,
                "options": {"num_predict": 60, "temperature": 0.1},
            },
            timeout=OLLAMA_TIMEOUT,
        )
        resp.raise_for_status()
        text = resp.json().get("response", "").strip()

        # Strip <think> blocks
        if "<think>" in text:
            text = text.split("</think>")[-1].strip()

        # Extract URL
        urls = re.findall(r'https?://[^\s\'"<>]+', text)
        if urls:
            url = urls[0].rstrip(".,;)")
            log.info("Qwen3 suggests URL for '%s': %s", venue_name, url)
            return url

        log.info("Qwen3 returned no URL for '%s': %s", venue_name, text[:80])
        return ""

    except Exception as e:
        log.warning("Ollama error for venue '%s': %s", venue_name, e)
        return ""


# ── Scraping ──────────────────────────────────────────────────────────────────

def _similarity(a: str, b: str) -> float:
    """Fuzzy string similarity 0-1."""
    a = re.sub(r'[^\w\s]', '', a.lower().strip())
    b = re.sub(r'[^\w\s]', '', b.lower().strip())
    return SequenceMatcher(None, a, b).ratio()


def _parse_date(date_str: str) -> datetime | None:
    """Try to parse a date string into a datetime."""
    formats = [
        "%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%B %d, %Y",
        "%b %d, %Y", "%m/%d/%Y", "%d %B %Y",
    ]
    for fmt in formats:
        try:
            return datetime.strptime(date_str.strip(), fmt)
        except Exception:
            continue
    # Try extracting year-month-day pattern
    m = re.search(r'(\d{4})-(\d{2})-(\d{2})', date_str)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except Exception:
            pass
    return None


def _dates_close(date_a: str, date_b: str, window: int = DATE_WINDOW_DAYS) -> bool:
    """Check if two date strings are within window days of each other."""
    da = _parse_date(date_a)
    db = _parse_date(date_b)
    if not da or not db:
        return False
    return abs((da - db).days) <= window


def scrape_venue_events(url: str) -> list[dict]:
    """
    Scrape a venue events page. Returns list of dicts with:
      title, date_str, description, image_url, event_url
    Uses BeautifulSoup heuristics + og: meta tags.
    """
    try:
        resp = requests.get(url, headers=REQUESTS_HEADERS, timeout=REQUESTS_TIMEOUT)
        resp.raise_for_status()
        html = resp.text
    except Exception as e:
        log.warning("Failed to fetch venue URL %s: %s", url, e)
        return []

    soup = BeautifulSoup(html, "html.parser")
    events = []

    # Strategy 1: JSON-LD structured data
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            import json
            data = json.loads(script.string or "")
            items = data if isinstance(data, list) else [data]
            for item in items:
                if item.get("@type") in ("Event", "MusicEvent", "TheaterEvent",
                                          "SportsEvent", "ScreeningEvent"):
                    title    = item.get("name", "")
                    date_str = item.get("startDate", "")
                    desc     = item.get("description", "")
                    img      = ""
                    if isinstance(item.get("image"), str):
                        img = item["image"]
                    elif isinstance(item.get("image"), dict):
                        img = item["image"].get("url", "")
                    elif isinstance(item.get("image"), list) and item["image"]:
                        img = item["image"][0] if isinstance(item["image"][0], str) \
                              else item["image"][0].get("url", "")
                    ev_url = item.get("url", url)
                    if title:
                        events.append({
                            "title":       title,
                            "date_str":    date_str,
                            "description": desc,
                            "image_url":   img,
                            "event_url":   ev_url,
                            "source":      "json-ld",
                        })
        except Exception:
            continue

    if events:
        log.debug("JSON-LD found %d events at %s", len(events), url)
        return events

    # Strategy 2: OG tags (single event page)
    og_title = soup.find("meta", property="og:title")
    og_desc  = soup.find("meta", property="og:description")
    og_img   = soup.find("meta", property="og:image")
    og_url   = soup.find("meta", property="og:url")

    if og_title and og_title.get("content"):
        events.append({
            "title":       og_title["content"],
            "date_str":    "",
            "description": og_desc["content"] if og_desc else "",
            "image_url":   og_img["content"]  if og_img  else "",
            "event_url":   og_url["content"]  if og_url  else url,
            "source":      "og-tags",
        })
        log.debug("OG tags found event at %s", url)
        return events

    # Strategy 3: Heuristic — find event-like blocks
    # Look for article/li/div elements containing a title-like heading + date
    candidates = soup.find_all(["article", "li", "div"],
                               class_=re.compile(
                                   r'event|show|concert|performance|listing',
                                   re.I))
    for el in candidates[:30]:
        heading = el.find(["h1", "h2", "h3", "h4", "a"])
        title   = heading.get_text(strip=True) if heading else ""
        if not title or len(title) < 3:
            continue

        # Date: look for time tag or date-like text
        time_el  = el.find("time")
        date_str = ""
        if time_el:
            date_str = time_el.get("datetime", "") or time_el.get_text(strip=True)

        # Image
        img_el    = el.find("img")
        image_url = ""
        if img_el:
            image_url = img_el.get("src", "") or img_el.get("data-src", "")

        # Description
        p_els = el.find_all("p")
        desc  = " ".join(p.get_text(strip=True) for p in p_els[:2])

        # Link
        a_el    = el.find("a", href=True)
        ev_url  = a_el["href"] if a_el else url
        if ev_url.startswith("/"):
            from urllib.parse import urlparse
            parsed  = urlparse(url)
            ev_url  = f"{parsed.scheme}://{parsed.netloc}{ev_url}"

        events.append({
            "title":       title,
            "date_str":    date_str,
            "description": desc,
            "image_url":   image_url,
            "event_url":   ev_url,
            "source":      "heuristic",
        })

    log.debug("Heuristic found %d events at %s", len(events), url)
    return events


def find_matching_event(scraped: list[dict], target_title: str,
                        target_date: str) -> dict | None:
    """
    Find the best matching event from scraped results.
    Requires title similarity >= TITLE_MATCH_THRESHOLD AND date within window.
    """
    best       = None
    best_score = 0.0

    for ev in scraped:
        score = _similarity(ev["title"], target_title)
        if score < TITLE_MATCH_THRESHOLD:
            continue
        # Date check — if we have dates, require them to be close
        if ev.get("date_str") and target_date:
            if not _dates_close(ev["date_str"], target_date):
                continue
        if score > best_score:
            best_score = score
            best       = ev

    if best:
        log.info("Matched '%.40s' → '%.40s' (score=%.2f)",
                 target_title, best["title"], best_score)
    return best


# ── WordPress updater ─────────────────────────────────────────────────────────

def _sideload_image(image_url: str, post_id: int, title: str,
                    wp_upload_dir: str, wp_upload_url: str) -> int | None:
    """Download image and attach to post. Returns attachment ID or None."""
    if not image_url or not SIDELOAD_IMAGES:
        return None
    try:
        ext      = re.search(r'\.(jpg|jpeg|png|webp|gif)', image_url, re.I)
        ext      = ext.group(0).lower() if ext else ".jpg"
        fname    = f"ve_{post_id}_{hashlib.md5(image_url.encode()).hexdigest()[:8]}{ext}"
        fpath    = os.path.join(wp_upload_dir, fname)
        furl     = f"{wp_upload_url.rstrip('/')}/{fname}"

        if not os.path.exists(fpath):
            req = urllib.request.Request(image_url, headers=REQUESTS_HEADERS)
            with urllib.request.urlopen(req, timeout=15) as r, open(fpath, "wb") as f:
                f.write(r.read())
            log.info("Downloaded image → %s", fpath)

        # Insert attachment post
        conn  = get_conn()
        now   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        mtype = "image/jpeg"
        if ext == ".png":  mtype = "image/png"
        if ext == ".webp": mtype = "image/webp"

        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"INSERT INTO {WP_PREFIX}posts "
                    f"(post_author,post_date,post_date_gmt,post_content,post_title,"
                    f"post_status,post_name,post_modified,post_modified_gmt,"
                    f"post_type,post_mime_type,post_parent,guid,to_ping,pinged,"
                    f"post_content_filtered,post_excerpt) "
                    f"VALUES (1,%s,%s,'',%s,'inherit',%s,%s,%s,"
                    f"'attachment',%s,%s,%s,'','','','')",
                    (now, now, title, fname, now, now, mtype, post_id, furl)
                )
                att_id = cur.lastrowid
                cur.execute(
                    f"INSERT INTO {WP_PREFIX}postmeta (post_id,meta_key,meta_value) "
                    f"VALUES (%s,'_wp_attached_file',%s)",
                    (att_id, fname)
                )
            conn.commit()
            return att_id
        finally:
            conn.close()

    except Exception as e:
        log.warning("Image sideload failed for post %d: %s", post_id, e)
        return None


def upgrade_event(conn, post_id: int, matched: dict,
                  has_image: bool, current_desc: str) -> bool:
    """
    Upgrade a WP event post with better image and/or description from venue site.
    Only upgrades fields that are weak/missing.
    """
    updated = False
    now     = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Upgrade description if current is short/empty and matched has one
    new_desc = (matched.get("description") or "").strip()
    if new_desc and len(new_desc) > 80 and len(current_desc) < 100:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE {WP_PREFIX}posts SET post_content=%s, post_modified=%s "
                f"WHERE ID=%s",
                (new_desc, now, post_id)
            )
            cur.execute(
                f"INSERT INTO {WP_PREFIX}postmeta (post_id,meta_key,meta_value) "
                f"VALUES (%s,'_EventDescription',%s) "
                f"ON DUPLICATE KEY UPDATE meta_value=VALUES(meta_value)",
                (post_id, new_desc)
            )
        log.info("Upgraded description for post %d", post_id)
        updated = True

    # Upgrade image if missing
    if not has_image and matched.get("image_url"):
        wp_upload_dir = os.getenv("WP_UPLOAD_DIR", "/var/www/html/wp-content/uploads")
        wp_upload_url = os.getenv("WP_UPLOAD_URL", "https://thelocalradar.com/wp-content/uploads")
        att_id = _sideload_image(
            matched["image_url"], post_id,
            matched.get("title", ""), wp_upload_dir, wp_upload_url
        )
        if att_id:
            with conn.cursor() as cur:
                cur.execute(
                    f"INSERT INTO {WP_PREFIX}postmeta (post_id,meta_key,meta_value) "
                    f"VALUES (%s,'_thumbnail_id',%s) "
                    f"ON DUPLICATE KEY UPDATE meta_value=VALUES(meta_value)",
                    (post_id, att_id)
                )
            log.info("Upgraded image for post %d (att=%d)", post_id, att_id)
            updated = True

    if updated:
        conn.commit()

    return updated


# ── Main ──────────────────────────────────────────────────────────────────────

def run():
    conn = get_conn()
    try:
        ensure_venues_table(conn)
        events = fetch_weak_tm_events(conn)
    finally:
        conn.close()

    if not events:
        log.info("No weak TM events found — nothing to enrich")
        return

    # Group events by (venue_name, city_slug) to minimize Ollama + scrape calls
    from collections import defaultdict
    venue_groups = defaultdict(list)
    for ev in events:
        key = (ev["venue_name"], ev["city_slug"])
        venue_groups[key].append(ev)

    log.info("Processing %d unique venues", len(venue_groups))

    enriched = 0
    skipped  = 0
    venues_checked = 0

    for (venue_name, city_slug), venue_events in venue_groups.items():
        if venues_checked >= MAX_VENUES_PER_RUN:
            log.info("Hit MAX_VENUES_PER_RUN=%d — stopping", MAX_VENUES_PER_RUN)
            break

        conn = get_conn()
        try:
            record = get_venue_record(conn, venue_name, city_slug)

            # Skip venues marked as skip
            if record and record["status"] == "skip":
                skipped += len(venue_events)
                continue

            # Check if we need to (re)look up the URL
            needs_lookup = (
                not record or
                not record.get("events_url") or
                (record.get("last_checked") and
                 (datetime.now() - record["last_checked"]).days > RECHECK_DAYS)
            )

            events_url = (record or {}).get("events_url", "")

            if needs_lookup:
                log.info("Looking up URL for venue: %s (%s)", venue_name, city_slug)
                events_url = ask_ollama_for_venue_url(venue_name, city_slug)
                time.sleep(0.5)  # be nice to Ollama

                status = "active" if events_url else "skip"
                upsert_venue(conn, venue_name, city_slug, events_url, status)
                venues_checked += 1

                if not events_url:
                    log.info("No URL found for '%s' — marking skip", venue_name)
                    skipped += len(venue_events)
                    continue

            # Scrape the venue's events page
            log.info("Scraping %s for %d events", events_url, len(venue_events))
            scraped = scrape_venue_events(events_url)

            if not scraped:
                log.info("No events scraped from %s", events_url)
                skipped += len(venue_events)
                continue

            # Match and upgrade each event
            for ev in venue_events:
                matched = find_matching_event(scraped, ev["title"], ev["start_date"])
                if not matched:
                    log.debug("No match for '%s' at %s", ev["title"][:40], venue_name)
                    continue

                conn2 = get_conn()
                try:
                    ok = upgrade_event(
                        conn2, ev["post_id"], matched,
                        ev["has_image"], ev["description"]
                    )
                    if ok:
                        enriched += 1
                finally:
                    conn2.close()

            time.sleep(0.25)  # polite scraping

        finally:
            conn.close()

    log.info("Venue enrichment complete — enriched: %d, skipped: %d", enriched, skipped)


if __name__ == "__main__":
    run()
