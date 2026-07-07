"""
config.py – OpenClaw configuration

Cities are defined here in CITIES.
Scrape sources are loaded dynamically from wp_tlr_event_sources DB table.
Add new venues via the WP admin plugin UI or direct DB insert.
"""

import logging
import os

import mysql.connector

log = logging.getLogger("openclaw.config")

# ─── Logging ────────────────────────────────────────────────────────────────
LOG_LEVEL = os.getenv("OPENCLAW_LOG_LEVEL", "INFO")

# ─── Database ───────────────────────────────────────────────────────────────
DB = {
    "host":     os.getenv("WP_DB_HOST", "localhost"),
    "port":     int(os.getenv("WP_DB_PORT", 3306)),
    "user":     os.getenv("WP_DB_USER", "wpuser"),
    "password": os.getenv("WP_DB_PASSWORD", ""),
    "database": os.getenv("WP_DB_NAME", "wordpress"),
    "charset":  "utf8mb4",
}

WP_PREFIX = os.getenv("WP_PREFIX", "wp_")

# ─── Ticketmaster ───────────────────────────────────────────────────────────
TICKETMASTER_API_KEY = os.getenv("TICKETMASTER_API_KEY", "")

# ─── Eventim / See Tickets Affiliate API ────────────────────────────────────
# NOTE: this is the official Affiliates API (national feed, affId-scoped),
# a different data path from any existing source_type="seetickets" entries
# in wp_openclaw_sources (those scrape individual venue HTML pages directly,
# no affiliate tracking). EVENTIM_ACCOUNT_PASSWORD is intentionally NOT
# loaded here -- it's only used for the affiliate web portal login, never
# sent by the API client in eventim.py.
EVENTIM_API_KEY    = os.getenv("EVENTIM_API_KEY", "")
EVENTIM_API_SECRET = os.getenv("EVENTIM_API_SECRET", "")
EVENTIM_AFF_ID     = os.getenv("EVENTIM_AFF_ID", "40")

# ─── Ollama / Qwen3 ─────────────────────────────────────────────────────────
OLLAMA_HOST    = os.getenv("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_MODEL   = os.getenv("OLLAMA_MODEL", "qwen3:14b")
OLLAMA_TIMEOUT = int(os.getenv("OLLAMA_TIMEOUT", 60))

# ─── Scheduler intervals (seconds) ──────────────────────────────────────────
TICKETMASTER_INTERVAL = int(os.getenv("TM_INTERVAL", 3600))
SCRAPER_INTERVAL      = int(os.getenv("SCRAPER_INTERVAL", 7200))
EVENTIM_INTERVAL      = int(os.getenv("EVENTIM_INTERVAL", 3600))

# ─── Cities (static — Ticketmaster DMA config) ───────────────────────────────
CITIES = [
    {
        "name": "Memphis",
        "slug": "memphis",
        "ticketmaster_dma_id": "322",
        "wp_site_id": 1,
        "default_category_slug": "memphis-events",
    },
    {
        "name": "Denver",
        "slug": "denver",
        "ticketmaster_dma_id": "302",
        "wp_site_id": 1,
        "default_category_slug": "denver-events",
    },
    {
        "name": "Nashville",
        "slug": "nashville",
        "ticketmaster_dma_id": "324",
        "wp_site_id": 1,
        "default_category_slug": "nashville-events",
    },
    {
        "name": "Birmingham",
        "slug": "birmingham",
        "ticketmaster_dma_id": "630",
        "wp_site_id": 1,
        "default_category_slug": "birmingham-events",
    },
]


def _get_conn():
    return mysql.connector.connect(**DB)


def load_cities() -> list[dict]:
    """
    Load active cities from wp_openclaw_cities DB table.
    Falls back to static CITIES list if DB is unavailable.
    Returns list of dicts with keys: name, slug, ticketmaster_dma_id, lat, lng,
    radius_miles, wp_site_id, default_category_slug.
    """
    try:
        conn = _get_conn()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("""
            SELECT name, slug, tm_dma_id, lat, lng, radius_miles, status
            FROM wp_openclaw_cities
            WHERE status = 'active'
            ORDER BY name
        """)
        rows = cursor.fetchall()
        cursor.close()
        conn.close()

        if not rows:
            log.warning("No cities found in DB, falling back to static CITIES list")
            return CITIES

        cities = []
        for row in rows:
            cities.append({
                "name":                  row["name"],
                "slug":                  row["slug"],
                "ticketmaster_dma_id":   row["tm_dma_id"] or "",
                "lat":                   float(row["lat"] or 0),
                "lng":                   float(row["lng"] or 0),
                "radius_miles":          int(row["radius_miles"] or 35),
                "wp_site_id":            1,
                "default_category_slug": f"{row['slug']}-events",
            })

        log.info("Loaded %d active cities from DB", len(cities))
        return cities

    except Exception as e:
        log.error("Failed to load cities from DB: %s — using static fallback", e)
        return CITIES


def load_dynamic_sources() -> dict:
    """
    Load active scrape sources from wp_openclaw_sources, grouped by city_slug.
    Returns dict: { "memphis": [source_dict, ...], "denver": [...], ... }
    """
    cities = load_cities()
    sources_by_city = {city["slug"]: [] for city in cities}
    try:
        conn = _get_conn()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("""
            SELECT id, url, city_slug, name, source_type, notes
            FROM wp_openclaw_sources
            WHERE status = 'active'
            ORDER BY city_slug, id
        """)
        rows = cursor.fetchall()
        cursor.close()
        conn.close()

        for row in rows:
            city_slug = row["city_slug"]
            if city_slug not in sources_by_city:
                sources_by_city[city_slug] = []

            # Parse any extra config from notes field (stored as JSON if present)
            import json
            extra = {}
            try:
                if row.get("notes"):
                    extra = json.loads(row["notes"])
            except Exception:
                pass

            # Map 'auto' and 'squarespace' source_type aliases
            stype = row["source_type"] or "squarespace"
            if stype == "auto":
                stype = "squarespace"

            source = {
                "_db_id":      row["id"],
                "name":        row["name"] or row["url"],
                "url":         row["url"],
                "source_type": stype,
                "city_slug":   city_slug,
                **extra,
            }
            sources_by_city[city_slug].append(source)

        log.info("Loaded %d dynamic sources from DB", sum(len(v) for v in sources_by_city.values()))
    except Exception as e:
        log.error("Failed to load dynamic sources from DB: %s", e)

    return sources_by_city


def update_source_stats(db_id: int, event_count: int):
    """Update last_run and last_count for a source."""
    try:
        conn = _get_conn()
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE wp_openclaw_sources
            SET last_run = NOW(),
                last_count = %s
            WHERE id = %s
        """, (event_count, db_id))
        conn.commit()
        cursor.close()
        conn.close()
    except Exception as e:
        log.error("Failed to update source stats for id %s: %s", db_id, e)


# ─── TEC Category Mapping ────────────────────────────────────────────────────
CATEGORY_KEYWORDS = {
    "music":       ["concert", "live music", "band", "show", "festival", "dj", "jazz", "blues", "hip hop", "country", "rock", "indie", "rap"],
    "arts":        ["art", "gallery", "exhibit", "museum", "theatre", "theater", "dance", "opera", "ballet", "film", "cinema", "comedy"],
    "food-drink":  ["food", "drink", "wine", "beer", "cocktail", "tasting", "dining", "restaurant", "brunch", "happy hour", "bbq"],
    "sports":      ["game", "match", "tournament", "grizzlies", "hustle", "tigers", "memphis", "nba", "nfl", "mlb", "nhl", "mls", "soccer", "football", "basketball", "baseball"],
    "fitness":     ["run", "race", "5k", "marathon", "yoga", "cycling", "bike", "hike", "triathlon", "crossfit", "fitness", "workout"],
    "community":   ["community", "volunteer", "charity", "fundraiser", "neighborhood", "civic", "meeting", "town hall"],
    "family":      ["family", "kids", "children", "youth", "parent", "toddler", "baby"],
    "nightlife":   ["nightlife", "club", "bar", "lounge", "karaoke", "trivia", "drag", "dance"],
    "outdoors":    ["outdoor", "park", "garden", "nature", "trail", "kayak", "canoe", "camping", "fishing"],
    "business":    ["networking", "conference", "seminar", "workshop", "professional", "startup", "entrepreneur"],
    "education":   ["lecture", "class", "course", "workshop", "learning", "tour", "talk", "panel"],
    "holiday":     ["holiday", "christmas", "halloween", "thanksgiving", "new year", "valentine", "easter", "fourth of july"],
}

# ─── Deduplication ───────────────────────────────────────────────────────────
DEDUP_FIELDS = ["title", "start_date", "venue_name"]

# ─── Image sideloading ───────────────────────────────────────────────────────
SIDELOAD_IMAGES = True
IMAGE_TIMEOUT   = 15

# ─── Ticketmaster filters ────────────────────────────────────────────────────
TM_SEGMENTS = ["Music", "Arts & Theatre", "Sports", "Family", "Miscellaneous"]
TM_RADIUS   = "50"
TM_UNIT     = "miles"
TM_SIZE     = 200
TM_AFFILIATE_ID = os.getenv("TM_AFFILIATE_ID", "7097599")
TM_TO_TLR   = {}
