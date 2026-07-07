"""
config.py – OpenClaw configuration
All runtime settings. Cities and sources are loaded from the DB (managed via
the openclaw-monitor WP plugin). Static CITIES list is a fallback only.
"""

import logging
import os

import pymysql
import pymysql.cursors

log = logging.getLogger("openclaw.config")

# ── Logging ───────────────────────────────────────────────────────────────────
LOG_LEVEL = os.getenv("OPENCLAW_LOG_LEVEL", "INFO")

# ── Database ──────────────────────────────────────────────────────────────────
DB = {
    "host":     os.getenv("WP_DB_HOST",     "localhost"),
    "port":     int(os.getenv("WP_DB_PORT", 3306)),
    "user":     os.getenv("WP_DB_USER",     "wpuser"),
    "password": os.getenv("WP_DB_PASSWORD", ""),
    "database": os.getenv("WP_DB_NAME",     "wordpress"),
    "charset":  "utf8mb4",
}

WP_PREFIX = os.getenv("WP_PREFIX", "wp_")

# ── Ticketmaster ──────────────────────────────────────────────────────────────
TICKETMASTER_API_KEY = os.getenv("TICKETMASTER_API_KEY", "")
TM_RADIUS       = "50"
TM_UNIT         = "miles"
TM_SIZE         = 200
TM_AFFILIATE_ID = os.getenv("TM_AFFILIATE_ID", "7097599")
TM_SEGMENTS     = ["Music", "Arts & Theatre", "Sports", "Family", "Miscellaneous"]

# ── Eventim / See Tickets Affiliate API ───────────────────────────────────────
# Official Affiliates API (national feed, affId-scoped) -- a different data
# path from any existing source_type="seetickets" entries in
# wp_openclaw_sources (those scrape individual venue HTML pages directly,
# no affiliate tracking). EVENTIM_ACCOUNT_PASSWORD is intentionally NOT
# loaded here -- it's only used for the affiliate web portal login, never
# sent by the API client in eventim.py.
EVENTIM_API_KEY    = os.getenv("EVENTIM_API_KEY", "")
EVENTIM_API_SECRET = os.getenv("EVENTIM_API_SECRET", "")
EVENTIM_AFF_ID     = os.getenv("EVENTIM_AFF_ID", "40")

# ── Ollama / Qwen3 ────────────────────────────────────────────────────────────
OLLAMA_HOST    = os.getenv("OLLAMA_HOST",    "http://localhost:11434")
OLLAMA_MODEL   = os.getenv("OLLAMA_MODEL",   "qwen3:14b")
OLLAMA_TIMEOUT = int(os.getenv("OLLAMA_TIMEOUT", 60))

# ── Scheduler intervals (seconds) ─────────────────────────────────────────────
TICKETMASTER_INTERVAL = int(os.getenv("TM_INTERVAL",      3600))
SCRAPER_INTERVAL      = int(os.getenv("SCRAPER_INTERVAL", 7200))
EVENTIM_INTERVAL      = int(os.getenv("EVENTIM_INTERVAL", 3600))

# ── Image sideloading ─────────────────────────────────────────────────────────
SIDELOAD_IMAGES = True
IMAGE_TIMEOUT   = 15

# ── Static city fallback (used only if DB is unreachable) ─────────────────────
CITIES = [
    {"name": "Memphis",    "slug": "memphis",    "ticketmaster_dma_id": "322", "lat": 35.1495, "lng": -90.0490, "radius_miles": 35},
    {"name": "Denver",     "slug": "denver",     "ticketmaster_dma_id": "302", "lat": 39.7392, "lng": -104.9903, "radius_miles": 35},
    {"name": "Nashville",  "slug": "nashville",  "ticketmaster_dma_id": "324", "lat": 36.1627, "lng": -86.7816, "radius_miles": 35},
    {"name": "Birmingham", "slug": "birmingham", "ticketmaster_dma_id": "630", "lat": 33.5186, "lng": -86.8104, "radius_miles": 35},
]

# ── TLR Category slugs (ONLY these 7 exist on the site) ──────────────────────
# Map keywords → category slug. Evaluated in order; first match wins per event.
# Fallback is always "more-to-do".
TLR_CATEGORIES = [
    ("live-music-concerts",   ["concert", "live music", "band", "dj set", "dj ", "jazz", "blues",
                                "hip hop", "hip-hop", "country music", "rock show", "indie",
                                "rap", "r&b", "soul", "folk", "metal", "punk", "singer"]),
    ("comedy",                ["comedy", "stand-up", "standup", "stand up", "improv", "open mic",
                                "comedian", "laughs"]),
    ("performing-visual-arts",["theater", "theatre", "dance", "opera", "ballet", "symphony",
                                "orchestra", "gallery", "exhibit", "museum", "film", "cinema",
                                "art show", "art walk", "drag show", "burlesque", "magic show",
                                "spoken word", "poetry"]),
    ("sports-fitness",        ["game", "match", "tournament", "grizzlies", "hustle", "tigers",
                                "nba", "nfl", "mlb", "nhl", "mls", "soccer", "football",
                                "basketball", "baseball", "hockey", "run", "race", "5k",
                                "marathon", "half marathon", "yoga", "cycling", "bike ride",
                                "triathlon", "crossfit", "fitness", "workout", "gym"]),
    ("festivals",             ["festival", "fair", "carnival", "flea market", "holiday market",
                                "street fest", "block party", "pride", "oktoberfest", "mardi gras"]),
    ("family-community",      ["family", "kids", "children", "youth", "parent", "toddler", "baby",
                                "community", "volunteer", "charity", "fundraiser", "neighborhood",
                                "civic", "town hall", "free event", "all ages"]),
    ("more-to-do",            []),   # catch-all — always matches
]

# ── Ticketmaster segment → TLR slug ──────────────────────────────────────────
TM_TO_TLR = {
    "music":           "live-music-concerts",
    "arts & theatre":  "performing-visual-arts",
    "sports":          "sports-fitness",
    "family":          "family-community",
    "miscellaneous":   "more-to-do",
    # genres
    "classical":       "performing-visual-arts",
    "comedy":          "comedy",
    "dance/electronic":"live-music-concerts",
    "jazz":            "live-music-concerts",
    "blues":           "live-music-concerts",
    "r&b":             "live-music-concerts",
    "hip-hop/rap":     "live-music-concerts",
    "rock":            "live-music-concerts",
    "country":         "live-music-concerts",
    "pop":             "live-music-concerts",
    "folk":            "live-music-concerts",
    "metal":           "live-music-concerts",
    "theatre":         "performing-visual-arts",
    "dance":           "performing-visual-arts",
    "film":            "performing-visual-arts",
}

# ── DB helpers ────────────────────────────────────────────────────────────────

def _get_conn():
    return pymysql.connect(
        host=DB["host"], port=DB["port"], user=DB["user"],
        password=DB["password"], database=DB["database"],
        charset=DB["charset"], cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
    )


def load_cities() -> list[dict]:
    """Load active cities from wp_openclaw_cities. Falls back to static CITIES."""
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT name, slug, tm_dma_id, lat, lng, radius_miles
                FROM wp_openclaw_cities
                WHERE status = 'active'
                ORDER BY name
            """)
            rows = cur.fetchall()
        conn.close()

        if not rows:
            log.warning("No active cities in DB, using static fallback")
            return CITIES

        cities = []
        for row in rows:
            cities.append({
                "name":                row["name"],
                "slug":                row["slug"],
                "ticketmaster_dma_id": row["tm_dma_id"] or "",
                "lat":                 float(row["lat"] or 0),
                "lng":                 float(row["lng"] or 0),
                "radius_miles":        int(row["radius_miles"] or 35),
            })
        log.info("Loaded %d cities from DB", len(cities))
        return cities

    except Exception as e:
        log.error("Failed to load cities from DB: %s — using static fallback", e)
        return CITIES


def load_dynamic_sources() -> dict:
    """
    Load active scrape sources from wp_openclaw_sources grouped by city_slug.
    Returns { "memphis": [source_dict, ...], ... }
    source_type 'auto' is aliased to 'html_auto'.
    """
    import json as _json
    cities = load_cities()
    by_city = {c["slug"]: [] for c in cities}

    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, url, city_slug, name, source_type, notes
                FROM wp_openclaw_sources
                WHERE status = 'active'
                ORDER BY city_slug, id
            """)
            rows = cur.fetchall()
        conn.close()

        for row in rows:
            slug = row["city_slug"]
            if slug not in by_city:
                by_city[slug] = []

            extra = {}
            try:
                if row.get("notes"):
                    extra = _json.loads(row["notes"])
            except Exception:
                pass

            stype = (row["source_type"] or "html_auto").strip()
            if stype in ("auto", "squarespace"):
                stype = "html_auto"

            by_city[slug].append({
                "_db_id":      row["id"],
                "name":        row["name"] or row["url"],
                "url":         row["url"],
                "source_type": stype,
                "city_slug":   slug,
                **extra,
            })

        total = sum(len(v) for v in by_city.values())
        log.info("Loaded %d dynamic sources from DB", total)

    except Exception as e:
        log.error("Failed to load dynamic sources: %s", e)

    return by_city


def update_source_stats(db_id: int, event_count: int):
    """Update last_run and last_count for a scrape source."""
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE wp_openclaw_sources SET last_run=NOW(), last_count=%s WHERE id=%s",
                (event_count, db_id)
            )
        conn.commit()
        conn.close()
    except Exception as e:
        log.error("Failed to update source stats for id %s: %s", db_id, e)
