"""
db.py – WordPress MySQL writer for OpenClaw

Responsibilities:
- Deduplication via SHA-256 fingerprint (title + start_utc + city_slug)
- Insert new tribe_events posts with all TEC meta
- Upsert path: if fingerprint exists, update times/image only
- Write wp_tec_events + wp_tec_occurrences so TEC Views V2 shows events
- Sideload images into wp_content/uploads
- Assign TLR categories (exactly 7 slugs)
- Create/reuse venue records
- Store source URL on every event (_EventURL)
- Clean up images when events are trashed (via WP-side mu-plugin; see notes)

Connection model: insert_event() opens + closes its own connection per call.
All helper functions receive the connection and never close it.
"""

import hashlib
import logging
import os
import re
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pymysql
import pymysql.cursors

from config import (
    DB, WP_PREFIX, SIDELOAD_IMAGES, IMAGE_TIMEOUT,
    TLR_CATEGORIES, TM_TO_TLR,
)

log = logging.getLogger("openclaw.db")

DEFAULT_TZ = "America/Chicago"

# The 7 valid TLR category slugs. Anything else is rejected.
TLR_VALID_SLUGS = {
    "comedy", "family-community", "festivals",
    "live-music-concerts", "more-to-do",
    "performing-visual-arts", "sports-fitness",
}


# ── Connection ────────────────────────────────────────────────────────────────

def get_connection():
    return pymysql.connect(
        host=DB["host"], port=DB["port"], user=DB["user"],
        password=DB["password"], database=DB["database"],
        charset=DB["charset"], cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
    )


# ── Date helpers ──────────────────────────────────────────────────────────────

def _fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def resolve_event_times(event: dict, fallback_now: str) -> dict:
    """
    Normalize event times to a consistent dict with start/end in both local
    and UTC. Accepts start_local/start_utc (Ticketmaster) or start_date
    (scrapers, treated as local).
    """
    tz_name = event.get("timezone") or DEFAULT_TZ
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo(DEFAULT_TZ)
        tz_name = DEFAULT_TZ
    utc = ZoneInfo("UTC")

    start_local = event.get("start_local") or ""
    start_utc   = event.get("start_utc")   or ""
    end_local   = event.get("end_local")   or ""
    end_utc     = event.get("end_utc")     or ""

    # Legacy scraper path: only start_date present → treat as local
    if not start_local and not start_utc:
        start_local = event.get("start_date") or fallback_now
    if not end_local and not end_utc:
        end_local = event.get("end_date") or ""

    def to_utc(local_s):
        try:
            return _fmt(datetime.strptime(local_s, "%Y-%m-%d %H:%M:%S")
                        .replace(tzinfo=tz).astimezone(utc))
        except Exception:
            return local_s

    def to_local(utc_s):
        try:
            return _fmt(datetime.strptime(utc_s, "%Y-%m-%d %H:%M:%S")
                        .replace(tzinfo=utc).astimezone(tz))
        except Exception:
            return utc_s

    if start_local and not start_utc:
        start_utc = to_utc(start_local)
    elif start_utc and not start_local:
        start_local = to_local(start_utc)

    if not end_local and not end_utc:
        try:
            end_local = _fmt(datetime.strptime(start_local, "%Y-%m-%d %H:%M:%S") + timedelta(hours=2))
        except Exception:
            end_local = start_local
        end_utc = to_utc(end_local)
    else:
        if end_local and not end_utc:
            end_utc = to_utc(end_local)
        elif end_utc and not end_local:
            end_local = to_local(end_utc)

    return {
        "start_local": start_local,
        "start_utc":   start_utc,
        "end_local":   end_local,
        "end_utc":     end_utc,
        "timezone":    tz_name,
        "all_day":     "yes" if event.get("all_day") else "",
    }


# ── Deduplication ─────────────────────────────────────────────────────────────

def make_fingerprint(event: dict) -> str:
    """SHA-256 of title + start_utc + city_slug. Excludes venue (inconsistent across sources)."""
    canonical_start = event.get("start_utc") or event.get("start_date") or ""
    raw = "|".join([
        (event.get("title")     or "").strip().lower(),
        canonical_start,
        (event.get("city_slug") or "").strip().lower(),
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def get_fingerprint_post_id(conn, fingerprint: str):
    """Return existing post_id if fingerprint found, else None."""
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT post_id FROM {WP_PREFIX}postmeta "
            f"WHERE meta_key='_openclaw_fp' AND meta_value=%s LIMIT 1",
            (fingerprint,)
        )
        row = cur.fetchone()
        return row["post_id"] if row else None


# ── Slug helpers ──────────────────────────────────────────────────────────────

def slugify(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_-]+", "-", text)
    return text[:200]


def unique_post_slug(conn, base_slug: str) -> str:
    slug = base_slug
    suffix = 1
    while True:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT ID FROM {WP_PREFIX}posts "
                f"WHERE post_name=%s AND post_status!='trash' LIMIT 1",
                (slug,)
            )
            if not cur.fetchone():
                return slug
        slug = f"{base_slug}-{suffix}"
        suffix += 1


# ── Category mapping ──────────────────────────────────────────────────────────

def map_to_tlr_categories(event: dict) -> list:
    """
    Map an event to exactly the TLR category slugs that exist on the site.
    Priority: TM segment/genre mapping first, then keyword scan of title+desc.
    Returns a list with at least one slug (always falls back to 'more-to-do').
    """
    matched = set()

    # 1. TM classification mapping
    for raw_cat in (event.get("categories") or []):
        key = raw_cat.lower().strip()
        if key in TM_TO_TLR:
            matched.add(TM_TO_TLR[key])

    # 2. Keyword scan — uses ordered TLR_CATEGORIES list; stops at first match per category
    text = " ".join([
        (event.get("title")       or "").lower(),
        (event.get("description") or "").lower(),
    ])

    for slug, keywords in TLR_CATEGORIES:
        if slug == "more-to-do":
            continue
        for kw in keywords:
            if kw in text:
                matched.add(slug)
                break

    # 3. Validate — only keep real TLR slugs
    matched = {c for c in matched if c in TLR_VALID_SLUGS}

    # 4. Fallback
    if not matched:
        matched.add("more-to-do")

    return list(matched)


# ── Taxonomy ──────────────────────────────────────────────────────────────────

def get_or_create_term_by_slug(conn, slug: str, name: str, taxonomy: str) -> int:
    """Look up by slug first (preserves existing WP terms), create if missing."""
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT tt.term_taxonomy_id FROM {WP_PREFIX}terms t "
            f"JOIN {WP_PREFIX}term_taxonomy tt ON t.term_id=tt.term_id "
            f"WHERE t.slug=%s AND tt.taxonomy=%s LIMIT 1",
            (slug, taxonomy)
        )
        row = cur.fetchone()
        if row:
            return row["term_taxonomy_id"]
        cur.execute(
            f"INSERT INTO {WP_PREFIX}terms (name, slug, term_group) VALUES (%s,%s,0)",
            (name, slug)
        )
        term_id = cur.lastrowid
        cur.execute(
            f"INSERT INTO {WP_PREFIX}term_taxonomy (term_id, taxonomy, description, parent, count) "
            f"VALUES (%s,%s,'',0,0)",
            (term_id, taxonomy)
        )
        return cur.lastrowid


def get_or_create_term(conn, name: str, taxonomy: str) -> int:
    return get_or_create_term_by_slug(conn, slugify(name), name, taxonomy)


def assign_terms(conn, post_id: int, term_taxonomy_ids: list):
    with conn.cursor() as cur:
        for tt_id in term_taxonomy_ids:
            cur.execute(
                f"INSERT IGNORE INTO {WP_PREFIX}term_relationships "
                f"(object_id, term_taxonomy_id, term_order) VALUES (%s,%s,0)",
                (post_id, tt_id)
            )
            cur.execute(
                f"UPDATE {WP_PREFIX}term_taxonomy SET count=count+1 "
                f"WHERE term_taxonomy_id=%s",
                (tt_id,)
            )


# ── Image sideloading ─────────────────────────────────────────────────────────

def sideload_image(conn, image_url: str, post_id: int, post_title: str) -> int | None:
    """Download image to wp-content/uploads and create an attachment post."""
    if not image_url or not SIDELOAD_IMAGES:
        return None
    try:
        upload_dir = os.getenv("WP_UPLOAD_DIR", "/var/www/html/wp-content/uploads")
        upload_url = os.getenv("WP_UPLOAD_URL", "https://thelocalradar.com/wp-content/uploads")
        year_month = datetime.now().strftime("%Y/%m")
        dest_dir   = Path(upload_dir) / year_month
        dest_dir.mkdir(parents=True, exist_ok=True)

        ext = image_url.split("?")[0].rsplit(".", 1)[-1].lower()
        if ext not in ("jpg", "jpeg", "png", "gif", "webp"):
            ext = "jpg"
        fname     = slugify(post_title)[:60] + f"-{int(time.time())}.{ext}"
        dest_path = dest_dir / fname

        req = urllib.request.Request(image_url, headers={"User-Agent": "OpenClaw/1.0"})
        with urllib.request.urlopen(req, timeout=IMAGE_TIMEOUT) as resp:
            dest_path.write_bytes(resp.read())

        mime_map = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
                    "gif": "image/gif", "webp": "image/webp"}
        mime          = mime_map.get(ext, "image/jpeg")
        relative_path = f"{year_month}/{fname}"
        wp_url        = f"{upload_url}/{year_month}/{fname}"

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO {WP_PREFIX}posts "
                f"(post_author, post_date, post_date_gmt, post_content, post_excerpt, post_title, "
                f"post_status, post_name, post_modified, post_modified_gmt, post_type, "
                f"post_mime_type, guid, to_ping, pinged, post_content_filtered) "
                f"VALUES (1,%s,%s,'','',%s,'inherit',%s,%s,%s,'attachment',%s,%s,'','','')",
                (now, now, post_title, slugify(fname), now, now, mime, wp_url)
            )
            att_id = cur.lastrowid
            cur.execute(
                f"INSERT INTO {WP_PREFIX}postmeta (post_id, meta_key, meta_value) "
                f"VALUES (%s,'_wp_attached_file',%s)",
                (att_id, relative_path)
            )
            cur.execute(
                f"INSERT INTO {WP_PREFIX}postmeta (post_id, meta_key, meta_value) "
                f"VALUES (%s,'_wp_attachment_metadata',%s)",
                (att_id, f'a:1:{{s:4:"file";s:{len(relative_path)}:"{relative_path}";}}'  )
            )
            cur.execute(
                f"UPDATE {WP_PREFIX}posts SET post_parent=%s WHERE ID=%s",
                (post_id, att_id)
            )
            cur.execute(
                f"INSERT INTO {WP_PREFIX}postmeta (post_id, meta_key, meta_value) "
                f"VALUES (%s,'_wp_attachment_image_alt',%s)",
                (att_id, post_title)
            )
        log.debug("Sideloaded image %s → attachment %d", image_url, att_id)
        return att_id
    except Exception as e:
        log.warning("Image sideload failed for %s: %s", image_url, e)
        return None


# ── TEC index writer ──────────────────────────────────────────────────────────

def _write_tec_index(conn, post_id: int, t: dict):
    """
    Populate wp_tec_events + wp_tec_occurrences.
    TEC Views V2 requires these rows; without them events won't show on the
    calendar unless manually saved in the WP editor.
    """
    try:
        start_local = t["start_local"]
        end_local   = t["end_local"]
        start_utc   = t["start_utc"]
        end_utc     = t["end_utc"]
        tz          = t["timezone"]

        try:
            dur = int((datetime.strptime(end_local,   "%Y-%m-%d %H:%M:%S") -
                       datetime.strptime(start_local, "%Y-%m-%d %H:%M:%S")).total_seconds())
        except Exception:
            dur = 7200

        h = hashlib.sha1(f"{start_local}{end_local}".encode()).hexdigest()

        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO `wp_tec_events` "
                "(post_id, start_date, end_date, timezone, start_date_utc, end_date_utc, duration, hash) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON DUPLICATE KEY UPDATE "
                "start_date=%s, end_date=%s, timezone=%s, "
                "start_date_utc=%s, end_date_utc=%s, duration=%s, hash=%s",
                (post_id, start_local, end_local, tz, start_utc, end_utc, dur, h,
                 start_local, end_local, tz, start_utc, end_utc, dur, h)
            )
            event_id = cur.lastrowid or post_id

            cur.execute(
                "INSERT INTO `wp_tec_occurrences` "
                "(event_id, post_id, start_date, start_date_utc, end_date, end_date_utc, duration, hash) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON DUPLICATE KEY UPDATE "
                "start_date=%s, start_date_utc=%s, end_date=%s, end_date_utc=%s, duration=%s, hash=%s",
                (event_id, post_id, start_local, start_utc, end_local, end_utc, dur, h,
                 start_local, start_utc, end_local, end_utc, dur, h)
            )
    except Exception as e:
        log.warning("TEC index write failed for post %d: %s", post_id, e)


# ── Venue / Organizer ─────────────────────────────────────────────────────────

def _get_or_create_venue(conn, event: dict) -> int | None:
    name = (event.get("venue_name") or "").strip()
    if not name:
        return None

    with conn.cursor() as cur:
        cur.execute(
            f"SELECT p.ID FROM {WP_PREFIX}posts p "
            f"JOIN {WP_PREFIX}postmeta pm ON p.ID=pm.post_id "
            f"WHERE p.post_type='tribe_venue' AND p.post_status='publish' "
            f"AND pm.meta_key='_VenueName' AND pm.meta_value=%s LIMIT 1",
            (name,)
        )
        row = cur.fetchone()
        if row:
            return row["ID"]

    now  = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    slug = unique_post_slug(conn, slugify(name))
    with conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO {WP_PREFIX}posts "
            f"(post_author,post_date,post_date_gmt,post_content,post_excerpt,post_title,"
            f"post_status,post_name,post_modified,post_modified_gmt,post_type,"
            f"to_ping,pinged,post_content_filtered) "
            f"VALUES (1,%s,%s,'','',%s,'publish',%s,%s,%s,'tribe_venue','','','')",
            (now, now, name, slug, now, now)
        )
        venue_id = cur.lastrowid
        cur.executemany(
            f"INSERT INTO {WP_PREFIX}postmeta (post_id,meta_key,meta_value) VALUES (%s,%s,%s)",
            [
                (venue_id, "_VenueName",    name),
                (venue_id, "_VenueAddress", event.get("venue_address") or ""),
                (venue_id, "_VenueCity",    event.get("venue_city")    or ""),
                (venue_id, "_VenueState",   event.get("venue_state")   or ""),
                (venue_id, "_VenueZip",     event.get("venue_zip")     or ""),
                (venue_id, "_VenueCountry", "United States"),
            ]
        )
    return venue_id


def _get_or_create_organizer(conn, name: str) -> int | None:
    name = (name or "").strip()
    if not name:
        return None
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT p.ID FROM {WP_PREFIX}posts p "
            f"JOIN {WP_PREFIX}postmeta pm ON p.ID=pm.post_id "
            f"WHERE p.post_type='tribe_organizer' AND p.post_status='publish' "
            f"AND pm.meta_key='_OrganizerOrganizer' AND pm.meta_value=%s LIMIT 1",
            (name,)
        )
        row = cur.fetchone()
        if row:
            return row["ID"]

    now  = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    slug = unique_post_slug(conn, slugify(name))
    with conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO {WP_PREFIX}posts "
            f"(post_author,post_date,post_date_gmt,post_content,post_excerpt,post_title,"
            f"post_status,post_name,post_modified,post_modified_gmt,post_type,"
            f"to_ping,pinged,post_content_filtered) "
            f"VALUES (1,%s,%s,'','',%s,'publish',%s,%s,%s,'tribe_organizer','','','')",
            (now, now, name, slug, now, now)
        )
        org_id = cur.lastrowid
        cur.execute(
            f"INSERT INTO {WP_PREFIX}postmeta (post_id,meta_key,meta_value) VALUES (%s,%s,%s)",
            (org_id, "_OrganizerOrganizer", name)
        )
    return org_id


# ── City tagging ──────────────────────────────────────────────────────────────

def resolve_metro_city(event: dict, city_config: dict) -> str:
    """Always return the city slug — metro sub-city handling if needed later."""
    return city_config["slug"]


# ── Core writer ───────────────────────────────────────────────────────────────

def _apply_image(conn, post_id: int, image_url: str, title: str):
    """Sideload image and set as thumbnail, only if not already set."""
    if not image_url:
        return
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT meta_value FROM {WP_PREFIX}postmeta "
            f"WHERE post_id=%s AND meta_key='_thumbnail_id' LIMIT 1",
            (post_id,)
        )
        if cur.fetchone():
            return  # already has thumbnail
    att_id = sideload_image(conn, image_url, post_id, title)
    if att_id:
        with conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO {WP_PREFIX}postmeta (post_id,meta_key,meta_value) "
                f"VALUES (%s,'_thumbnail_id',%s)",
                (post_id, att_id)
            )


def update_event(conn, post_id: int, event: dict) -> bool:
    """Update times, source URL, and image on an existing event. Never creates dupes."""
    try:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        t   = resolve_event_times(event, now)

        updates = {
            "_EventStartDate":    t["start_local"],
            "_EventEndDate":      t["end_local"],
            "_EventStartDateUTC": t["start_utc"],
            "_EventEndDateUTC":   t["end_utc"],
            "_EventAllDay":       t["all_day"],
            "_EventTimezone":     t["timezone"],
        }
        # Always keep source URL updated
        ticket_url = event.get("ticket_url") or ""
        if ticket_url:
            updates["_EventURL"] = ticket_url

        with conn.cursor() as cur:
            for key, val in updates.items():
                cur.execute(
                    f"UPDATE {WP_PREFIX}postmeta SET meta_value=%s "
                    f"WHERE post_id=%s AND meta_key=%s",
                    (val, post_id, key)
                )
                cur.execute(
                    f"INSERT IGNORE INTO {WP_PREFIX}postmeta (post_id,meta_key,meta_value) "
                    f"VALUES (%s,%s,%s)",
                    (post_id, key, val)
                )

        _apply_image(conn, post_id, event.get("image_url", ""),
                     event.get("title", ""))
        _write_tec_index(conn, post_id, t)
        conn.commit()
        log.info("Updated event [%d] '%s'", post_id, event.get("title"))
        return True
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        log.error("Failed to update event [%d]: %s", post_id, e, exc_info=True)
        return False


def insert_event(event: dict, city_config: dict = None) -> bool:
    """
    Main entry point called by scheduler for each scraped event.
    - Deduplicates by fingerprint
    - Inserts new post with all TEC meta, categories, venue, image, source URL
    - Updates existing post if fingerprint found
    - Never leaves the connection open on any exit path
    """
    fingerprint = make_fingerprint(event)
    conn = get_connection()
    try:
        # ── Upsert check ──────────────────────────────────────────────────────
        existing_id = get_fingerprint_post_id(conn, fingerprint)
        if existing_id:
            return update_event(conn, existing_id, event)

        # ── New insert ────────────────────────────────────────────────────────
        now     = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        title   = (event.get("title") or "Untitled Event").strip()
        content = (event.get("description") or "").strip()
        slug    = unique_post_slug(conn, slugify(title))
        t       = resolve_event_times(event, now)

        with conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO {WP_PREFIX}posts "
                f"(post_author,post_date,post_date_gmt,post_content,post_excerpt,post_title,"
                f"post_status,comment_status,ping_status,post_name,post_modified,"
                f"post_modified_gmt,post_type,to_ping,pinged,post_content_filtered) "
                f"VALUES (1,%s,%s,%s,'',%s,'publish','closed','closed',%s,%s,%s,"
                f"'tribe_events','','','')",
                (now, now, content, title, slug, now, now)
            )
            post_id = cur.lastrowid

            meta_rows = [
                (post_id, "_EventStartDate",       t["start_local"]),
                (post_id, "_EventEndDate",          t["end_local"]),
                (post_id, "_EventStartDateUTC",     t["start_utc"]),
                (post_id, "_EventEndDateUTC",       t["end_utc"]),
                (post_id, "_EventAllDay",           t["all_day"]),
                (post_id, "_EventTimezone",         t["timezone"]),
                (post_id, "_EventCurrencySymbol",   "$"),
                (post_id, "_EventCurrencyPosition", "prefix"),
                (post_id, "_EventCost",             event.get("cost") or ""),
                (post_id, "_EventDescription",      content),
                # Source URL — the actual page/ticket link for this event
                (post_id, "_EventURL",              event.get("ticket_url") or ""),
                # OpenClaw tracking meta
                (post_id, "_openclaw_fp",           fingerprint),
                (post_id, "_openclaw_source",       event.get("source_name") or ""),
                (post_id, "_openclaw_city",         event.get("city_slug") or ""),
                (post_id, "_openclaw_external_id",  event.get("external_id") or ""),
            ]

            venue_id = _get_or_create_venue(conn, event)
            if venue_id:
                meta_rows.append((post_id, "_EventVenueID", venue_id))

            if event.get("organizer_name"):
                org_id = _get_or_create_organizer(conn, event["organizer_name"])
                if org_id:
                    meta_rows.append((post_id, "_EventOrganizerID", org_id))

            cur.executemany(
                f"INSERT INTO {WP_PREFIX}postmeta (post_id,meta_key,meta_value) "
                f"VALUES (%s,%s,%s)",
                meta_rows
            )

        # ── Categories ────────────────────────────────────────────────────────
        cat_slugs = map_to_tlr_categories(event)
        tt_ids    = []
        for slug in cat_slugs:
            cat_name = slug.replace("-", " ").title()
            # Fix display names for our slugs
            name_map = {
                "live-music-concerts":   "Live Music & Concerts",
                "performing-visual-arts": "Performing & Visual Arts",
                "sports-fitness":        "Sports & Fitness",
                "family-community":      "Family & Community",
                "more-to-do":            "More To Do",
                "comedy":                "Comedy",
                "festivals":             "Festivals",
            }
            cat_name = name_map.get(slug, cat_name)
            tt_ids.append(get_or_create_term_by_slug(conn, slug, cat_name, "tribe_events_cat"))

        # City tag
        city_slug = (resolve_metro_city(event, city_config)
                     if city_config else event.get("city_slug", ""))
        if city_slug:
            tt_ids.append(get_or_create_term(conn, city_slug.title(), "post_tag"))

        if tt_ids:
            assign_terms(conn, post_id, tt_ids)

        # ── Image ─────────────────────────────────────────────────────────────
        _apply_image(conn, post_id, event.get("image_url", ""), title)

        # ── TEC index ─────────────────────────────────────────────────────────
        _write_tec_index(conn, post_id, t)

        conn.commit()
        log.info("Inserted event [%d] '%s' (%s)", post_id, title, event.get("city_slug"))
        return True

    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        log.error("Failed to insert event '%s': %s", event.get("title"), e, exc_info=True)
        return False
    finally:
        try:
            conn.close()
        except Exception:
            pass
