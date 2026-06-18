"""
db.py — WordPress MySQL writer for OpenClaw

Rules:
  - insert_event() opens and closes its own connection. Never leaks.
  - update_event() receives an open connection, never closes it.
  - All helper functions receive a connection, never close it.
  - Fingerprint uses start_utc OR start_local OR start_date — whichever exists.
  - update_event() re-applies categories every run (backfills old events).
  - assign_terms() uses INSERT IGNORE — safe to call repeatedly.
  - Times: _EventStartDate = local time, _EventStartDateUTC = UTC.
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

from config import DB, WP_PREFIX, SIDELOAD_IMAGES, IMAGE_TIMEOUT, TLR_CATEGORIES, TM_TO_TLR

log = logging.getLogger("openclaw.db")

DEFAULT_TZ = "America/Chicago"
UTC        = ZoneInfo("UTC")

TLR_VALID_SLUGS = {
    "comedy", "family-community", "festivals",
    "live-music-concerts", "more-to-do",
    "performing-visual-arts", "sports-fitness",
}

CAT_NAMES = {
    "comedy":                "Comedy",
    "family-community":      "Family & Community",
    "festivals":             "Festivals",
    "live-music-concerts":   "Live Music & Concerts",
    "more-to-do":            "More To Do",
    "performing-visual-arts":"Performing & Visual Arts",
    "sports-fitness":        "Sports & Fitness",
}


# ── Connection ────────────────────────────────────────────────────────────────

def get_connection():
    return pymysql.connect(
        host=DB["host"], port=DB["port"], user=DB["user"],
        password=DB["password"], database=DB["database"],
        charset=DB["charset"], cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
    )


# ── Date handling ─────────────────────────────────────────────────────────────

def _fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def resolve_event_times(event: dict, fallback_now: str) -> dict:
    """
    Produce a consistent set of local+UTC times from whatever the event provides.

    Priority:
      1. start_utc + start_local both set  → use as-is
      2. start_utc set, start_local empty  → derive local from UTC (TM path after fix)
      3. start_local set, start_utc empty  → derive UTC from local
      4. neither → fall back to start_date (treated as local)
    """
    tz_name = event.get("timezone") or DEFAULT_TZ
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz      = ZoneInfo(DEFAULT_TZ)
        tz_name = DEFAULT_TZ

    sl = (event.get("start_local") or "").strip()
    su = (event.get("start_utc")   or "").strip()
    el = (event.get("end_local")   or "").strip()
    eu = (event.get("end_utc")     or "").strip()

    # Legacy scraper path: only start_date
    if not sl and not su:
        sl = (event.get("start_date") or fallback_now).strip()
    if not el and not eu:
        el = (event.get("end_date") or "").strip()

    def to_utc(local_s: str) -> str:
        try:
            return _fmt(datetime.strptime(local_s, "%Y-%m-%d %H:%M:%S")
                        .replace(tzinfo=tz).astimezone(UTC))
        except Exception:
            return local_s

    def to_local(utc_s: str) -> str:
        try:
            return _fmt(datetime.strptime(utc_s, "%Y-%m-%d %H:%M:%S")
                        .replace(tzinfo=UTC).astimezone(tz))
        except Exception:
            return utc_s

    # Fill missing side
    if sl and not su:
        su = to_utc(sl)
    elif su and not sl:
        sl = to_local(su)

    # End times
    if not el and not eu:
        try:
            el = _fmt(datetime.strptime(sl, "%Y-%m-%d %H:%M:%S") + timedelta(hours=2))
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
        "start_utc":   su,
        "end_local":   el,
        "end_utc":     eu,
        "timezone":    tz_name,
        "all_day":     "yes" if event.get("all_day") else "",
    }


# ── Deduplication ─────────────────────────────────────────────────────────────

def make_fingerprint(event: dict) -> str:
    """
    SHA-256(title | canonical_date | city_slug).

    canonical_date priority: start_utc → start_local → start_date
    TM events always have start_utc populated.
    Scraper events always have start_date populated.
    This guarantees every unique event gets a unique fingerprint.
    """
    canonical = (
        event.get("start_utc")   or
        event.get("start_local") or
        event.get("start_date")  or ""
    ).strip()

    raw = "|".join([
        (event.get("title")     or "").strip().lower(),
        canonical,
        (event.get("city_slug") or "").strip().lower(),
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def get_fingerprint_post_id(conn, fp: str) -> int | None:
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT post_id FROM {WP_PREFIX}postmeta "
            f"WHERE meta_key='_openclaw_fp' AND meta_value=%s LIMIT 1",
            (fp,)
        )
        row = cur.fetchone()
        return row["post_id"] if row else None


# ── Slugs ─────────────────────────────────────────────────────────────────────

def slugify(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_-]+", "-", text)
    return text[:200]


def unique_post_slug(conn, base: str) -> str:
    slug = base
    n    = 1
    while True:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT ID FROM {WP_PREFIX}posts "
                f"WHERE post_name=%s AND post_status!='trash' LIMIT 1",
                (slug,)
            )
            if not cur.fetchone():
                return slug
        slug = f"{base}-{n}"
        n   += 1


# ── Category mapping ──────────────────────────────────────────────────────────

def map_to_tlr_categories(event: dict) -> list[str]:
    """
    Map event to one or more of the 7 TLR category slugs.
    Always returns at least ['more-to-do'].
    """
    matched = set()

    # 1. TM segment/genre mapping
    for raw_cat in (event.get("categories") or []):
        slug = TM_TO_TLR.get(raw_cat.lower().strip())
        if slug:
            matched.add(slug)

    # 2. Keyword scan of title + description
    text = " ".join([
        (event.get("title")       or "").lower(),
        (event.get("description") or "").lower(),
    ])
    for cat_slug, keywords in TLR_CATEGORIES:
        if not keywords:
            continue
        for kw in keywords:
            if kw in text:
                matched.add(cat_slug)
                break

    # 3. Validate against known slugs
    matched = {s for s in matched if s in TLR_VALID_SLUGS}

    if not matched:
        matched.add("more-to-do")

    return list(matched)


# ── Taxonomy ──────────────────────────────────────────────────────────────────

def get_or_create_term_by_slug(conn, slug: str, name: str, taxonomy: str) -> int:
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
            f"INSERT INTO {WP_PREFIX}term_taxonomy "
            f"(term_id, taxonomy, description, parent, count) VALUES (%s,%s,'',0,0)",
            (term_id, taxonomy)
        )
        return cur.lastrowid


def assign_terms(conn, post_id: int, tt_ids: list):
    """Assign taxonomy terms. INSERT IGNORE makes this safe to call repeatedly."""
    with conn.cursor() as cur:
        for tt_id in tt_ids:
            cur.execute(
                f"INSERT IGNORE INTO {WP_PREFIX}term_relationships "
                f"(object_id, term_taxonomy_id, term_order) VALUES (%s,%s,0)",
                (post_id, tt_id)
            )
            if cur.rowcount > 0:
                cur.execute(
                    f"UPDATE {WP_PREFIX}term_taxonomy SET count=count+1 "
                    f"WHERE term_taxonomy_id=%s",
                    (tt_id,)
                )


def _apply_categories(conn, post_id: int, event: dict, city_config: dict = None):
    """Assign TLR categories and city tag. Safe to call on insert or update."""
    cat_slugs = map_to_tlr_categories(event)
    tt_ids    = []
    for slug in cat_slugs:
        name = CAT_NAMES.get(slug, slug.replace("-", " ").title())
        tt_ids.append(get_or_create_term_by_slug(conn, slug, name, "tribe_events_cat"))

    city_slug = city_config["slug"] if city_config else event.get("city_slug", "")
    if city_slug:
        city_name = city_slug.replace("-", " ").title()
        tt_ids.append(get_or_create_term_by_slug(conn, city_slug, city_name, "post_tag"))

    if tt_ids:
        assign_terms(conn, post_id, tt_ids)


# ── Image sideloading ─────────────────────────────────────────────────────────

def sideload_image(conn, image_url: str, post_id: int, title: str) -> int | None:
    if not image_url or not SIDELOAD_IMAGES:
        return None
    try:
        upload_dir = os.getenv("WP_UPLOAD_DIR", "/var/www/html/wp-content/uploads")
        upload_url = os.getenv("WP_UPLOAD_URL", "https://thelocalradar.com/wp-content/uploads")
        ym         = datetime.now().strftime("%Y/%m")
        dest_dir   = Path(upload_dir) / ym
        dest_dir.mkdir(parents=True, exist_ok=True)

        ext = image_url.split("?")[0].rsplit(".", 1)[-1].lower()
        if ext not in ("jpg", "jpeg", "png", "gif", "webp"):
            ext = "jpg"
        fname = slugify(title)[:60] + f"-{int(time.time())}.{ext}"
        dest  = dest_dir / fname

        req = urllib.request.Request(image_url, headers={"User-Agent": "OpenClaw/1.0"})
        with urllib.request.urlopen(req, timeout=IMAGE_TIMEOUT) as resp:
            dest.write_bytes(resp.read())

        mime = {"jpg":"image/jpeg","jpeg":"image/jpeg","png":"image/png",
                "gif":"image/gif","webp":"image/webp"}.get(ext, "image/jpeg")
        rel  = f"{ym}/{fname}"
        url  = f"{upload_url}/{ym}/{fname}"
        now  = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        with conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO {WP_PREFIX}posts "
                f"(post_author,post_date,post_date_gmt,post_content,post_excerpt,post_title,"
                f"post_status,post_name,post_modified,post_modified_gmt,post_type,"
                f"post_mime_type,guid,to_ping,pinged,post_content_filtered) "
                f"VALUES (1,%s,%s,'','',%s,'inherit',%s,%s,%s,'attachment',%s,%s,'','','')",
                (now, now, title, slugify(fname), now, now, mime, url)
            )
            att = cur.lastrowid
            cur.executemany(
                f"INSERT INTO {WP_PREFIX}postmeta (post_id,meta_key,meta_value) VALUES (%s,%s,%s)",
                [
                    (att, "_wp_attached_file",        rel),
                    (att, "_wp_attachment_metadata",  f'a:1:{{s:4:"file";s:{len(rel)}:"{rel}";}}'  ),
                    (att, "_wp_attachment_image_alt", title),
                ]
            )
            cur.execute(
                f"UPDATE {WP_PREFIX}posts SET post_parent=%s WHERE ID=%s",
                (post_id, att)
            )
        log.debug("Sideloaded %s → att %d", image_url, att)
        return att
    except Exception as e:
        log.warning("Image sideload failed for %s: %s", image_url, e)
        return None


def _apply_image(conn, post_id: int, image_url: str, title: str):
    """Sideload and set thumbnail only if post has no thumbnail yet."""
    if not image_url:
        return
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT meta_value FROM {WP_PREFIX}postmeta "
            f"WHERE post_id=%s AND meta_key='_thumbnail_id' LIMIT 1",
            (post_id,)
        )
        if cur.fetchone():
            return
    att = sideload_image(conn, image_url, post_id, title)
    if att:
        with conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO {WP_PREFIX}postmeta (post_id,meta_key,meta_value) "
                f"VALUES (%s,'_thumbnail_id',%s)",
                (post_id, att)
            )


# ── TEC index ─────────────────────────────────────────────────────────────────

def _write_tec_index(conn, post_id: int, t: dict):
    """
    Write wp_tec_events + wp_tec_occurrences.
    TEC Views V2 needs these rows; without them events need a manual WP save to appear.
    """
    try:
        sl = t["start_local"]
        el = t["end_local"]
        su = t["start_utc"]
        eu = t["end_utc"]
        tz = t["timezone"]

        try:
            dur = int((
                datetime.strptime(el, "%Y-%m-%d %H:%M:%S") -
                datetime.strptime(sl, "%Y-%m-%d %H:%M:%S")
            ).total_seconds())
            if dur <= 0:
                dur = 7200
        except Exception:
            dur = 7200

        h = hashlib.sha1(f"{sl}{el}".encode()).hexdigest()

        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO `wp_tec_events` "
                "(post_id,start_date,end_date,timezone,start_date_utc,end_date_utc,duration,hash) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON DUPLICATE KEY UPDATE "
                "start_date=%s,end_date=%s,timezone=%s,"
                "start_date_utc=%s,end_date_utc=%s,duration=%s,hash=%s",
                (post_id, sl, el, tz, su, eu, dur, h,
                           sl, el, tz, su, eu, dur, h)
            )
            # Always look up the actual tec_events.id — lastrowid returns 0 on UPDATE
            cur.execute("SELECT event_id FROM `wp_tec_events` WHERE post_id=%s LIMIT 1", (post_id,))
            row = cur.fetchone()
            event_id = row["event_id"] if row else post_id

            cur.execute(
                "INSERT INTO `wp_tec_occurrences` "
                "(event_id,post_id,start_date,start_date_utc,end_date,end_date_utc,duration,hash) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON DUPLICATE KEY UPDATE "
                "start_date=%s,start_date_utc=%s,end_date=%s,end_date_utc=%s,duration=%s,hash=%s",
                (event_id, post_id, sl, su, el, eu, dur, h,
                                    sl, su, el, eu, dur, h)
            )
    except Exception as e:
        log.warning("TEC index failed for post %d: %s", post_id, e)


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
        vid = cur.lastrowid
        cur.executemany(
            f"INSERT INTO {WP_PREFIX}postmeta (post_id,meta_key,meta_value) VALUES (%s,%s,%s)",
            [
                (vid, "_VenueName",    name),
                (vid, "_VenueAddress", event.get("venue_address") or ""),
                (vid, "_VenueCity",    event.get("venue_city")    or ""),
                (vid, "_VenueState",   event.get("venue_state")   or ""),
                (vid, "_VenueZip",     event.get("venue_zip")     or ""),
                (vid, "_VenueCountry", "United States"),
            ]
        )
    return vid


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
        oid = cur.lastrowid
        cur.execute(
            f"INSERT INTO {WP_PREFIX}postmeta (post_id,meta_key,meta_value) VALUES (%s,%s,%s)",
            (oid, "_OrganizerOrganizer", name)
        )
    return oid


# ── Main writers ──────────────────────────────────────────────────────────────

def update_event(conn, post_id: int, event: dict, city_config: dict = None) -> bool:
    """
    Update an existing event. Re-applies times, categories, image, TEC index.
    This backfills categories on events that were inserted before the category fix.
    """
    try:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        t   = resolve_event_times(event, now)

        # Update time meta
        time_meta = {
            "_EventStartDate":    t["start_local"],
            "_EventEndDate":      t["end_local"],
            "_EventStartDateUTC": t["start_utc"],
            "_EventEndDateUTC":   t["end_utc"],
            "_EventAllDay":       t["all_day"],
            "_EventTimezone":     t["timezone"],
        }
        ticket_url = (event.get("ticket_url") or "").strip()
        if ticket_url:
            time_meta["_EventURL"] = ticket_url

        with conn.cursor() as cur:
            for key, val in time_meta.items():
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

        # Update post_content if description changed
        content = (event.get("description") or "").strip()
        if content:
            with conn.cursor() as cur:
                cur.execute(
                    f"UPDATE {WP_PREFIX}posts SET post_content=%s, post_modified=%s "
                    f"WHERE ID=%s AND (post_content='' OR post_content IS NULL)",
                    (content, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), post_id)
                )

        # Always re-apply categories — handles events inserted before the fix
        _apply_categories(conn, post_id, event, city_config)
        _apply_image(conn, post_id, event.get("image_url", ""), event.get("title", ""))
        _write_tec_index(conn, post_id, t)
        conn.commit()
        log.info("Updated [%d] '%s'", post_id, event.get("title"))
        return True
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        log.error("Update failed [%d]: %s", post_id, e, exc_info=True)
        return False


def insert_event(event: dict, city_config: dict = None) -> bool:
    """
    Insert or update an event. Opens its own DB connection.
    Never leaves the connection open regardless of outcome.
    """
    fp   = make_fingerprint(event)
    conn = get_connection()
    try:
        # Upsert check
        existing_id = get_fingerprint_post_id(conn, fp)
        if existing_id:
            return update_event(conn, existing_id, event, city_config)

        # New insert
        now     = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        title   = (event.get("title") or "Untitled Event").strip()
        content = (event.get("description") or "").strip()
        slug    = unique_post_slug(conn, slugify(title))
        t       = resolve_event_times(event, now)

        with conn.cursor() as cur:
            # Insert as 'draft' first — we'll flip to 'publish' after all meta and
            # TEC index rows are written. This triggers TEC's own save hooks on the
            # status transition, which is what makes events appear without a manual Update.
            cur.execute(
                f"INSERT INTO {WP_PREFIX}posts "
                f"(post_author,post_date,post_date_gmt,post_content,post_excerpt,post_title,"
                f"post_status,comment_status,ping_status,post_name,"
                f"post_modified,post_modified_gmt,post_type,"
                f"to_ping,pinged,post_content_filtered) "
                f"VALUES (1,%s,%s,%s,'',%s,'draft','closed','closed',%s,%s,%s,"
                f"'tribe_events','','','')",
                (now, now, content, title, slug, now, now)
            )
            post_id = cur.lastrowid

            meta = [
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
                (post_id, "_EventURL",              event.get("ticket_url") or ""),
                (post_id, "_openclaw_fp",           fp),
                (post_id, "_openclaw_source",       event.get("source_name") or ""),
                (post_id, "_openclaw_city",         event.get("city_slug") or ""),
                (post_id, "_openclaw_external_id",  event.get("external_id") or ""),
            ]

            vid = _get_or_create_venue(conn, event)
            if vid:
                meta.append((post_id, "_EventVenueID", vid))

            if event.get("organizer_name"):
                oid = _get_or_create_organizer(conn, event["organizer_name"])
                if oid:
                    meta.append((post_id, "_EventOrganizerID", oid))

            cur.executemany(
                f"INSERT INTO {WP_PREFIX}postmeta (post_id,meta_key,meta_value) "
                f"VALUES (%s,%s,%s)",
                meta
            )

        _apply_categories(conn, post_id, event, city_config)
        _apply_image(conn, post_id, event.get("image_url", ""), title)
        _write_tec_index(conn, post_id, t)

        # Flip from draft → publish now that all meta and TEC index are ready
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE {WP_PREFIX}posts SET post_status='publish', "
                f"post_modified=%s, post_modified_gmt=%s WHERE ID=%s",
                (now, now, post_id)
            )

        conn.commit()
        log.info("Inserted [%d] '%s' (%s)", post_id, title, event.get("city_slug"))
        return True

    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        log.error("Insert failed '%s': %s", event.get("title"), e, exc_info=True)
        return False
    finally:
        try:
            conn.close()
        except Exception:
            pass
