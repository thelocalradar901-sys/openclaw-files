# db.py — Future‑proof, layered architecture for OpenClaw → WordPress ingestion
# Cleaned, PEP‑8‑consistent, draft‑first publish flow (fixes TEC “Update to publish” bug)

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
    DB,
    WP_PREFIX,
    SIDELOAD_IMAGES,
    IMAGE_TIMEOUT,
    TLR_CATEGORIES,
    TM_TO_TLR,
)

log = logging.getLogger("openclaw.db")

DEFAULT_TZ = "America/Chicago"
UTC = ZoneInfo("UTC")

TLR_VALID_SLUGS = {
    "comedy",
    "family-community",
    "festivals",
    "live-music-concerts",
    "more-to-do",
    "performing-visual-arts",
    "sports-fitness",
}

CAT_NAMES = {
    "comedy": "Comedy",
    "family-community": "Family & Community",
    "festivals": "Festivals",
    "live-music-concerts": "Live Music & Concerts",
    "more-to-do": "More To Do",
    "performing-visual-arts": "Performing & Visual Arts",
    "sports-fitness": "Sports & Fitness",
}


# ============================================================================
# LAYER 1 — DB CONNECTION
# ============================================================================

def get_connection():
    return pymysql.connect(
        host=DB["host"],
        port=DB["port"],
        user=DB["user"],
        password=DB["password"],
        database=DB["database"],
        charset=DB["charset"],
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
    )


# ============================================================================
# LAYER 2 — UTILITIES (slugify, fingerprint, time resolution)
# ============================================================================

def slugify(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_-]+", "-", text)
    return text[:200]


def make_fingerprint(event: dict) -> str:
    canonical = (
        event.get("start_utc")
        or event.get("start_local")
        or event.get("start_date")
        or ""
    ).strip()

    raw = "|".join([
        (event.get("title") or "").strip().lower(),
        canonical,
        (event.get("city_slug") or "").strip().lower(),
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def resolve_event_times(event: dict, fallback_now: str) -> dict:
    tz_name = event.get("timezone") or DEFAULT_TZ
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo(DEFAULT_TZ)
        tz_name = DEFAULT_TZ

    sl = (event.get("start_local") or "").strip()
    su = (event.get("start_utc") or "").strip()
    el = (event.get("end_local") or "").strip()
    eu = (event.get("end_utc") or "").strip()

    if not sl and not su:
        sl = (event.get("start_date") or fallback_now).strip()
    if not el and not eu:
        el = (event.get("end_date") or "").strip()

    def to_utc(local_s: str) -> str:
        try:
            dt = datetime.strptime(local_s, "%Y-%m-%d %H:%M:%S")
            return dt.replace(tzinfo=tz).astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return local_s

    def to_local(utc_s: str) -> str:
        try:
            dt = datetime.strptime(utc_s, "%Y-%m-%d %H:%M:%S")
            return dt.replace(tzinfo=UTC).astimezone(tz).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return utc_s

    if sl and not su:
        su = to_utc(sl)
    elif su and not sl:
        sl = to_local(su)

    if not el and not eu:
        try:
            el = (
                datetime.strptime(sl, "%Y-%m-%d %H:%M:%S")
                + timedelta(hours=2)
            ).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            el = sl
        eu = to_utc(el)
    else:
        if el and not eu:
            eu = to_utc(el)
        elif eu and not el:
            el = to_local(eu)

    return {
        "start_local": sl,
        "start_utc": su,
        "end_local": el,
        "end_utc": eu,
        "timezone": tz_name,
        "all_day": "yes" if event.get("all_day") else "",
    }


def get_fingerprint_post_id(conn, fp: str) -> int | None:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT post_id
            FROM {WP_PREFIX}postmeta
            WHERE meta_key = '_openclaw_fp'
              AND meta_value = %s
            LIMIT 1
            """,
            (fp,),
        )
        row = cur.fetchone()
        return row["post_id"] if row else None


# ============================================================================
# LAYER 3 — TAXONOMY (categories, tags)
# ============================================================================

def map_to_tlr_categories(event: dict) -> list[str]:
    matched = set()

    for raw_cat in (event.get("categories") or []):
        slug = TM_TO_TLR.get(raw_cat.lower().strip())
        if slug:
            matched.add(slug)

    text = " ".join([
        (event.get("title") or "").lower(),
        (event.get("description") or "").lower(),
    ])

    for cat_slug, keywords in TLR_CATEGORIES:
        if not keywords:
            continue
        if any(kw in text for kw in keywords):
            matched.add(cat_slug)

    matched = {s for s in matched if s in TLR_VALID_SLUGS}

    return list(matched) or ["more-to-do"]


def get_or_create_term_by_slug(conn, slug: str, name: str, taxonomy: str) -> int:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT tt.term_taxonomy_id
            FROM {WP_PREFIX}terms t
            JOIN {WP_PREFIX}term_taxonomy tt ON t.term_id = tt.term_id
            WHERE t.slug = %s
              AND tt.taxonomy = %s
            LIMIT 1
            """,
            (slug, taxonomy),
        )
        row = cur.fetchone()
        if row:
            return row["term_taxonomy_id"]

        cur.execute(
            f"""
            INSERT INTO {WP_PREFIX}terms (name, slug, term_group)
            VALUES (%s, %s, 0)
            """,
            (name, slug),
        )
        term_id = cur.lastrowid

        cur.execute(
            f"""
            INSERT INTO {WP_PREFIX}term_taxonomy
            (term_id, taxonomy, description, parent, count)
            VALUES (%s, %s, '', 0, 0)
            """,
            (term_id, taxonomy),
        )
        return cur.lastrowid


def assign_terms(conn, post_id: int, tt_ids: list[int]):
    with conn.cursor() as cur:
        for tt_id in tt_ids:
            cur.execute(
                f"""
                INSERT IGNORE INTO {WP_PREFIX}term_relationships
                (object_id, term_taxonomy_id, term_order)
                VALUES (%s, %s, 0)
                """,
                (post_id, tt_id),
            )
            if cur.rowcount > 0:
                cur.execute(
                    f"""
                    UPDATE {WP_PREFIX}term_taxonomy
                    SET count = count + 1
                    WHERE term_taxonomy_id = %s
                    """,
                    (tt_id,),
                )


def apply_categories(conn, post_id: int, event: dict, city_config: dict | None):
    cat_slugs = map_to_tlr_categories(event)
    tt_ids = []

    for slug in cat_slugs:
        name = CAT_NAMES.get(slug, slug.replace("-", " ").title())
        tt_ids.append(get_or_create_term_by_slug(conn, slug, name, "tribe_events_cat"))

    city_slug = city_config["slug"] if city_config else event.get("city_slug", "")
    if city_slug:
        city_name = city_slug.replace("-", " ").title()
        tt_ids.append(get_or_create_term_by_slug(conn, city_slug, city_name, "post_tag"))

    if tt_ids:
        assign_terms(conn, post_id, tt_ids)


# ============================================================================
# LAYER 4 — VENUE & ORGANIZER
# ============================================================================

def get_or_create_venue(conn, event: dict) -> int | None:
    name = (event.get("venue_name") or "").strip()
    if not name:
        return None

    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT p.ID
            FROM {WP_PREFIX}posts p
            JOIN {WP_PREFIX}postmeta pm ON p.ID = pm.post_id
            WHERE p.post_type = 'tribe_venue'
              AND p.post_status = 'publish'
              AND pm.meta_key = '_VenueName'
              AND pm.meta_value = %s
            LIMIT 1
            """,
            (name,),
        )
        row = cur.fetchone()
        if row:
            return row["ID"]

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    slug = slugify(name)

    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO {WP_PREFIX}posts
            (post_author, post_date, post_date_gmt, post_content, post_excerpt,
             post_title, post_status, post_name, post_modified, post_modified_gmt,
             post_type, to_ping, pinged, post_content_filtered)
            VALUES (1, %s, %s, '', '', %s, 'publish', %s, %s, %s,
                    'tribe_venue', '', '', '')
            """,
            (now, now, name, slug, now, now),
        )
        vid = cur.lastrowid

        cur.executemany(
            f"""
            INSERT INTO {WP_PREFIX}postmeta (post_id, meta_key, meta_value)
            VALUES (%s, %s, %s)
            """,
            [
                (vid, "_VenueName", name),
                (vid, "_VenueAddress", event.get("venue_address") or ""),
                (vid, "_VenueCity", event.get("venue_city") or ""),
                (vid, "_VenueState", event.get("venue_state") or ""),
                (vid, "_VenueZip", event.get("venue_zip") or ""),
                (vid, "_VenueCountry", "United States"),
            ],
        )

    return vid


def get_or_create_organizer(conn, name: str) -> int | None:
    name = (name or "").strip()
    if not name:
        return None

    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT p.ID
            FROM {WP_PREFIX}posts p
            JOIN {WP_PREFIX}postmeta pm ON p.ID = pm.post_id
            WHERE p.post_type = 'tribe_organizer'
              AND p.post_status = 'publish'
              AND pm.meta_key = '_OrganizerOrganizer'
              AND pm.meta_value = %s
            LIMIT 1
            """,
            (name,),
        )
        row = cur.fetchone()
        if row:
            return row["ID"]

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    slug = slugify(name)

    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO {WP_PREFIX}posts
            (post_author, post_date, post_date_gmt, post_content, post_excerpt,
             post_title, post_status, post_name, post_modified, post_modified_gmt,
             post_type, to_ping, pinged, post_content_filtered)
            VALUES (1, %s, %s, '', '', %s, 'publish', %s, %s, %s,
                    'tribe_organizer', '', '', '')
            """,
            (now, now, name, slug, now, now),
        )
        oid = cur.lastrowid

        cur.execute(
            f"""
            INSERT INTO {WP_PREFIX}postmeta (post_id, meta_key, meta_value)
            VALUES (%s, '_OrganizerOrganizer', %s)
            """,
            (oid, name),
        )

    return oid


# ============================================================================
# LAYER 5 — IMAGE HANDLING
# ============================================================================

def sideload_image(conn, image_url: str, post_id: int, title: str) -> int | None:
    if not image_url or not SIDELOAD_IMAGES:
        return None

    try:
        upload_dir = os.getenv("WP_UPLOAD_DIR", "/var/www/html/wp-content/uploads")
        upload_url = os.getenv("WP_UPLOAD_URL", "https://thelocalradar.com/wp-content/uploads")

        ym = datetime.now().strftime("%Y/%m")
        dest_dir = Path(upload_dir) / ym
        dest_dir.mkdir(parents=True, exist_ok=True)

        ext = image_url.split("?")[0].rsplit(".", 1)[-1].lower()
        if ext not in ("jpg", "jpeg", "png", "gif", "webp"):
            ext = "jpg"

        fname = f"{slugify(title)[:60]}-{int(time.time())}.{ext}"
        dest = dest_dir / fname

        req = urllib.request.Request(image_url, headers={"User-Agent": "OpenClaw/1.0"})
        with urllib.request.urlopen(req, timeout=IMAGE_TIMEOUT) as resp:
            dest.write_bytes(resp.read())

        mime = {
            "jpg": "image/jpeg",
            "jpeg": "image/jpeg",
            "png": "image/png",
            "gif": "image/gif",
            "webp": "image/webp",
        }.get(ext, "image/jpeg")

        rel = f"{ym}/{fname}"
        url = f"{upload_url}/{ym}/{fname}"
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        with conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO {WP_PREFIX}posts
                (post_author, post_date, post_date_gmt, post_content, post_excerpt,
                 post_title, post_status, post_name, post_modified, post_modified_gmt,
                 post_type, post_mime_type, guid, to_ping, pinged, post_content_filtered)
                VALUES (1, %s, %s, '', '', %s, 'inherit', %s, %s, %s,
                        'attachment', %s, %s, '', '', '')
                """,
                (now, now, title, slugify(fname), now, now, mime, url),
            )
            att = cur.lastrowid

            cur.executemany(
                f"""
                INSERT INTO {WP_PREFIX}postmeta (post_id, meta_key, meta_value)
                VALUES (%s, %s, %s)
                """,
                [
                    (att, "_wp_attached_file", rel),
                    (att, "_wp_attachment_metadata", f'a:1:{{s:4:"file";s:{len(rel)}:"{rel}";}}'),
                    (att, "_wp_attachment_image_alt", title),
                ],
            )

            cur.execute(
                f"""
                UPDATE {WP_PREFIX}posts
                SET post_parent = %s
                WHERE ID = %s
                """,
                (post_id, att),
            )

        return att

    except Exception as e:
        log.warning("Image sideload failed for %s: %s", image_url, e)
        return None


def apply_image(conn, post_id: int, image_url: str, title: str):
    if not image_url:
        return

    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT meta_value
            FROM {WP_PREFIX}postmeta
            WHERE post_id = %s
              AND meta_key = '_thumbnail_id'
            LIMIT 1
            """,
            (post_id,),
        )
        if cur.fetchone():
            return

    att = sideload_image(conn, image_url, post_id, title)
    if att:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO {WP_PREFIX}postmeta (post_id, meta_key, meta_value)
                VALUES (%s, '_thumbnail_id', %s)
                """,
                (post_id, att),
            )


# ============================================================================
# LAYER 6 — TEC INDEX (wp_tec_events + wp_tec_occurrences)
# ============================================================================

def write_tec_index(conn, post_id: int, t: dict):
    try:
        sl = t["start_local"]
        el = t["end_local"]
        su = t["start_utc"]
        eu = t["end_utc"]
        tz = t["timezone"]

        try:
            dur = int(
                (
                    datetime.strptime(el, "%Y-%m-%d %H:%M:%S")
                    - datetime.strptime(sl, "%Y-%m-%d %H:%M:%S")
                ).total_seconds()
            )
            if dur <= 0:
                dur = 7200
        except Exception:
            dur = 7200

        h = hashlib.sha1(f"{sl}{el}".encode()).hexdigest()

        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO wp_tec_events
                (post_id, start_date, end_date, timezone,
                 start_date_utc, end_date_utc, duration, hash)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    start_date = %s,
                    end_date = %s,
                    timezone = %s,
                    start_date_utc = %s,
                    end_date_utc = %s,
                    duration = %s,
                    hash = %s
                ""