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
import json
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
    conn = pymysql.connect(
        host=DB["host"], port=DB["port"], user=DB["user"],
        password=DB["password"], database=DB["database"],
        charset=DB["charset"], cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
    )
    # MySQL's default isolation level, REPEATABLE READ, freezes this
    # connection's view of the database as of its FIRST query in each
    # transaction. That broke the fingerprint-claim race-recovery logic
    # in insert_event(): a thread that lost the race would retry
    # get_fingerprint_post_id() many times waiting for the winner's
    # post_id to appear, but every retry saw the same frozen snapshot
    # from before the winner's commit, so it could NEVER see the
    # winner's row no matter how long it waited -- guaranteeing a
    # "Fingerprint claim race unresolved" skip every time two workers
    # raced, and occasionally a true duplicate post on a later cycle.
    # READ COMMITTED makes each individual statement see the latest
    # committed data at the moment it runs, which is what's actually
    # needed here.
    with conn.cursor() as cur:
        cur.execute("SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED")
    ensure_schema(conn)
    return conn


_SCHEMA_CHECKED = False


def ensure_schema(conn) -> None:
    """
    Creates wp_openclaw_fingerprints if it doesn't exist yet.

    This table exists ONLY to give the fingerprint a real UNIQUE
    constraint at the database level. wp_postmeta has no unique
    constraint on (meta_key, meta_value) in stock WordPress -- two
    concurrent scrape workers can both run get_fingerprint_post_id(),
    both see "not found" (because neither has committed yet), and
    both proceed to insert_event(). That race is what produced the
    731-row Bodies/Denver flood: a tight burst of near-simultaneous
    Ticketmaster showtime pulls all lost the same race at once.

    A UNIQUE column makes the race impossible to lose silently: the
    second writer's INSERT throws IntegrityError instead of quietly
    succeeding, so insert_event() can catch that and fall back to
    update_event() against whichever row actually won. Checked once
    per process via _SCHEMA_CHECKED, not once per connection.
    """
    global _SCHEMA_CHECKED
    if _SCHEMA_CHECKED:
        return
    with conn.cursor() as cur:
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {WP_PREFIX}openclaw_fingerprints (
                fp        CHAR(64)     NOT NULL,
                post_id   BIGINT       NOT NULL,
                created   DATETIME     NOT NULL,
                PRIMARY KEY (fp),
                KEY post_id_idx (post_id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )
    conn.commit()
    _SCHEMA_CHECKED = True


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

def make_fingerprint(event: dict, resolved_times: dict | None = None) -> str:
    """
    SHA-256(title | canonical_date | city_slug).

    canonical_date priority: resolved_times["start_utc"] → resolved_times
    ["start_local"] → raw event start_utc/start_local/start_date,
    TRUNCATED TO THE DATE (YYYY-MM-DD) -- time-of-day is intentionally
    dropped here.

    IMPORTANT: pass resolved_times (the output of resolve_event_times())
    whenever you have it. Hashing straight off the raw event dict is
    what caused the 2026-06-26 duplicate flood: a source can hand back
    the same logical event across repeated parses with start_utc set on
    one pass and only start_local (or only start_date) set on another,
    or with a near-midnight local time that resolve_event_times() would
    convert across a UTC date boundary. Each raw variant hashed to a
    DIFFERENT fingerprint, so the UNIQUE-constrained claim table never
    even got a chance to dedupe them -- 20+ distinct posts got created
    for "Open Mic Comedy Night" in under two seconds, with no warning
    logged, because every single one looked like a brand-new event.
    Hashing the post-resolve_event_times() values instead guarantees
    the SAME logical start instant always produces the SAME fingerprint
    no matter which raw fields the source happened to populate on a
    given pull.

    Why date-only at all: long-running exhibits/touring shows (e.g.
    "Bodies -- The Science Within") sell each daily timeslot as its own
    Ticketmaster event, and some calendar plugins (e.g. MEC on
    Choose901, for SciPlay-style recurring listings) render one
    calendar tile per day an ongoing event spans. Both produce many
    same-title rows on the same day that differ only by time-of-day or
    by which day's tile rendered them. Keeping time-of-day in the
    fingerprint made every one of those a "new" event instead of an
    update -- this is what caused the Bodies/Denver and
    SciPlay/Choose901 duplicate floods.

    Truncating to date-only means: same title + same day + same city
    = same post, regardless of which specific showtime or which day's
    tile produced the data. The first one scraped creates the post;
    every later one that day runs through update_event() instead of
    creating a sibling post.

    Trade-off accepted: if a venue genuinely hosts two DIFFERENT
    events that happen to share an exact title on the same day, they
    will collapse into one post. This is considered acceptable -- it
    has not come up in practice, and it is far less common than the
    showtime/day-tile duplication this fixes.
    """
    if resolved_times:
        canonical_full = (
            resolved_times.get("start_utc") or
            resolved_times.get("start_local") or ""
        ).strip()
    else:
        canonical_full = (
            event.get("start_utc")   or
            event.get("start_local") or
            event.get("start_date")  or ""
        ).strip()
    canonical_date = canonical_full[:10]  # "YYYY-MM-DD" prefix only

    raw = "|".join([
        (event.get("title")     or "").strip().lower(),
        canonical_date,
        (event.get("city_slug") or "").strip().lower(),
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def get_fingerprint_post_id(conn, fp: str) -> int | None:
    """
    Looks up the post for a fingerprint via the unique-constrained
    wp_openclaw_fingerprints table first. Falls back to scanning
    wp_postmeta for legacy rows written before that table existed
    (so old posts aren't mistaken for "new" and re-inserted).
    """
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT post_id FROM {WP_PREFIX}openclaw_fingerprints WHERE fp=%s LIMIT 1",
            (fp,)
        )
        row = cur.fetchone()
        if row:
            return row["post_id"]

    with conn.cursor() as cur:
        cur.execute(
            f"SELECT post_id FROM {WP_PREFIX}postmeta "
            f"WHERE meta_key='_openclaw_fp' AND meta_value=%s LIMIT 1",
            (fp,)
        )
        row = cur.fetchone()
        if row:
            # Backfill into the new table so future lookups for this
            # fingerprint hit the fast/atomic path instead of repeating
            # this postmeta scan every single time.
            try:
                with conn.cursor() as cur2:
                    cur2.execute(
                        f"INSERT IGNORE INTO {WP_PREFIX}openclaw_fingerprints "
                        f"(fp, post_id, created) VALUES (%s, %s, %s)",
                        (fp, row["post_id"], datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
                    )
                conn.commit()
            except Exception:
                conn.rollback()
            return row["post_id"]

    return None


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
    Map event to exactly ONE of the 7 TLR category slugs (plus the
    more-to-do catch-all). TLR_CATEGORIES in config.py is deliberately
    ORDERED -- most specific/confident categories first, "more-to-do"
    always last with no keywords -- and the rule is "first match wins,"
    not "union of every category that happens to match."

    BUG THIS FIXES: the previous version's inner "break" only exited the
    per-category keyword loop, not the outer category loop, so every
    event was checked against ALL 7 categories regardless of an earlier
    match, and every hit got added to a set -- silently turning "first
    match wins" into "union everything that matches." That's how a
    concert ended up tagged Family & Community: it correctly matched
    live-music-concerts on a music keyword, but the outer loop kept
    going and ALSO matched family-community on an unrelated incidental
    word elsewhere in the title/description, and both got kept.

    Priority order, stopping at the first hit:
      1. Ticketmaster's own segment/genre classification, if present --
         a structured signal from TM is more reliable than incidental
         keyword overlap in free-text title/description.
      2. Keyword scan of title + description, checked in TLR_CATEGORIES
         order, stopping at the first category with any keyword hit.
      3. Fallback to more-to-do if nothing matched.
    """
    # 1. TM segment/genre mapping -- trust this first if present, since
    # it's a structured classification rather than a free-text guess.
    # IMPORTANT: TM often returns MULTIPLE classifications together for
    # one event (e.g. segment="Arts & Theatre" + genre="Comedy" for a
    # stand-up special) with no guaranteed specificity ordering in the
    # array itself. Collect every TM-mapped slug this event matches, then
    # pick whichever one appears EARLIEST in TLR_CATEGORIES' own order --
    # that list is already deliberately sequenced most-specific-first
    # (comedy before performing-visual-arts), so this respects the
    # intended priority instead of just taking whichever TM classification
    # happened to be listed first.
    tlr_order = [slug for slug, _ in TLR_CATEGORIES]
    tm_matched_slugs = set()
    for raw_cat in (event.get("categories") or []):
        slug = TM_TO_TLR.get(raw_cat.lower().strip())
        if slug and slug in TLR_VALID_SLUGS:
            tm_matched_slugs.add(slug)
    if tm_matched_slugs:
        best = min(tm_matched_slugs, key=lambda s: tlr_order.index(s))
        return [best]

    # 2. Keyword scan of title + description, first category match wins.
    # Word-boundary matching, not raw substring search -- several real
    # keywords are short enough (jazz, rap, r&b, soul, folk, punk, 5k,
    # trot, baby, etc.) to falsely match as substrings of unrelated words
    # otherwise: "trot" inside "Trotsky", "rap" inside "Trapeze", "soul"
    # inside "Console", "baby" inside "Babylon".
    text = " ".join([
        (event.get("title")       or "").lower(),
        (event.get("description") or "").lower(),
    ])
    for cat_slug, keywords in TLR_CATEGORIES:
        if not keywords:
            continue
        for kw in keywords:
            pattern = r"\b" + re.escape(kw) + r"\b"
            if re.search(pattern, text):
                return [cat_slug]

    # 3. Nothing matched (or matched a slug that's somehow not valid) --
    # fall back to the catch-all.
    return ["more-to-do"]


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

    # IMPORTANT: clear any EXISTING tribe_events_cat relationships for
    # this post before assigning the new one. assign_terms() below only
    # ever ADDS relationships (via INSERT IGNORE, which dedupes identical
    # rows but does nothing about a DIFFERENT stale category from a
    # previous scrape). Without this, every re-scrape of an existing
    # event just piled today's correct category on top of whatever
    # category it had last time -- including ones assigned under the
    # categorization bug fixed earlier today -- so events kept
    # accumulating multiple categories indefinitely, surviving even a
    # full backfill pass, since the very next live re-scrape would
    # silently re-add the stale one right back.
    with conn.cursor() as cur:
        # Find which term_taxonomy_ids are about to be removed so their
        # counts can be decremented correctly -- mirrors the count+1 that
        # assign_terms() does on insert, so counts stay accurate instead
        # of drifting upward forever as stale relationships get cleared.
        cur.execute(
            f"SELECT tr.term_taxonomy_id FROM {WP_PREFIX}term_relationships tr "
            f"JOIN {WP_PREFIX}term_taxonomy tt ON tt.term_taxonomy_id = tr.term_taxonomy_id "
            f"WHERE tr.object_id=%s AND tt.taxonomy=%s",
            (post_id, "tribe_events_cat")
        )
        stale_tt_ids = [r["term_taxonomy_id"] for r in cur.fetchall()]

        cur.execute(
            f"DELETE tr FROM {WP_PREFIX}term_relationships tr "
            f"JOIN {WP_PREFIX}term_taxonomy tt ON tt.term_taxonomy_id = tr.term_taxonomy_id "
            f"WHERE tr.object_id=%s AND tt.taxonomy=%s",
            (post_id, "tribe_events_cat")
        )

        for tt_id in stale_tt_ids:
            cur.execute(
                f"UPDATE {WP_PREFIX}term_taxonomy SET count=GREATEST(count-1,0) "
                f"WHERE term_taxonomy_id=%s",
                (tt_id,)
            )

    tt_ids = []
    for slug in cat_slugs:
        name = CAT_NAMES.get(slug, slug.replace("-", " ").title())
        tt_ids.append(get_or_create_term_by_slug(conn, slug, name, "tribe_events_cat"))

    # City tag (post_tag taxonomy) is intentionally left alone here --
    # unlike category, a post legitimately keeps the same city tag for
    # its whole life and there's no "stale city" problem to clean up.
    city_slug = city_config["slug"] if city_config else event.get("city_slug", "")
    if city_slug:
        city_name = city_slug.replace("-", " ").title()
        tt_ids.append(get_or_create_term_by_slug(conn, city_slug, city_name, "post_tag"))

    if tt_ids:
        assign_terms(conn, post_id, tt_ids)

    # Persist the raw signal categorization was actually computed from --
    # e.g. Ticketmaster's "Music"/"Rock"/"Sports" classifications -- so a
    # future re-categorization pass or backfill can recover it. Without
    # this, that signal only ever existed in the transient event dict for
    # the duration of one scrape cycle and was lost forever once the
    # event was saved -- which is exactly why the categories backfill
    # couldn't correctly re-tag old Ticketmaster events: the genre data
    # was never written down anywhere.
    raw_categories = event.get("categories") or []
    with conn.cursor() as cur:
        cur.execute(
            f"DELETE FROM {WP_PREFIX}postmeta WHERE post_id=%s AND meta_key=%s",
            (post_id, "_openclaw_raw_categories")
        )
        if raw_categories:
            cur.execute(
                f"INSERT INTO {WP_PREFIX}postmeta (post_id,meta_key,meta_value) VALUES (%s,%s,%s)",
                (post_id, "_openclaw_raw_categories", json.dumps(raw_categories))
            )


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

        h = hashlib.sha1(f"{post_id}{sl}{el}".encode()).hexdigest()

        with conn.cursor() as cur:
            # Upsert tec_events row (unique key on post_id)
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
            # Get the actual event_id
            cur.execute("SELECT event_id FROM `wp_tec_events` WHERE post_id=%s LIMIT 1", (post_id,))
            row = cur.fetchone()
            event_id = row["event_id"] if row else post_id

            # DELETE then INSERT for occurrences — avoids duplicate rows when
            # hash changes slightly between scraper runs (ON DUPLICATE KEY won't
            # fire if the hash changed, causing a new row instead of an update)
            cur.execute("DELETE FROM `wp_tec_occurrences` WHERE post_id=%s", (post_id,))
            cur.execute(
                "INSERT INTO `wp_tec_occurrences` "
                "(event_id,post_id,start_date,start_date_utc,end_date,end_date_utc,duration,hash) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                (event_id, post_id, sl, su, el, eu, dur, h)
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

        # make_fingerprint() now collapses to date-only, so every showtime
        # of a multi-showtime-per-day listing (e.g. a touring exhibit
        # selling hourly Ticketmaster slots, or an MEC calendar rendering
        # one tile per day of a long-running event) lands on this same
        # post via this same update_event() call. Scrape order across
        # those showtimes is not guaranteed, so blindly overwriting
        # _EventStartDate/_EventStartDateUTC with whatever happened to be
        # scraped most recently would make the displayed start time
        # flicker between runs (9am one run, 7pm the next).
        #
        # Instead: only move the start time EARLIER than what's already
        # stored, never later. The post ends up showing the first
        # available showtime of the day, which stays stable regardless
        # of scrape order, and is also just the more useful time to show
        # for "when does this start."
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT meta_value FROM {WP_PREFIX}postmeta "
                f"WHERE post_id=%s AND meta_key='_EventStartDateUTC' LIMIT 1",
                (post_id,)
            )
            row = cur.fetchone()
        existing_start_utc = row["meta_value"] if row else None

        keep_existing_time = bool(
            existing_start_utc and t["start_utc"] and
            t["start_utc"] > existing_start_utc
        )

        # Update time meta
        if keep_existing_time:
            # New scrape's showtime is later in the day than what's
            # already stored -- keep the existing (earlier) time meta
            # untouched, but still let ticket_url/description/category
            # fields below refresh normally.
            time_meta = {}
        else:
            time_meta = {
                "_EventStartDate":    t["start_local"],
                "_EventEndDate":      t["end_local"],
                "_EventStartDateUTC": t["start_utc"],
                "_EventEndDateUTC":   t["end_utc"],
                "_EventAllDay":       t["all_day"],
                "_EventTimezone":     t["timezone"],
            }
        ticket_url = (event.get("ticket_url") or "").strip()
        if ticket_url and not keep_existing_time:
            time_meta["_EventURL"] = ticket_url

        with conn.cursor() as cur:
            for key, val in time_meta.items():
                # IMPORTANT: wp_postmeta has NO unique constraint on
                # (post_id, meta_key) in stock WordPress schema -- only an
                # autoincrement primary key on meta_id. That means
                # "UPDATE ... ; INSERT IGNORE ..." (the previous approach)
                # has nothing for INSERT IGNORE to collide against, so it
                # silently inserted a brand-new duplicate row on every
                # single update cycle regardless of whether the UPDATE
                # above it just succeeded. Over enough re-scrapes this
                # multiplied postmeta rows 10-50x per post (confirmed via
                # diagnose_single_post_blowup.py: one post had 14 separate
                # _EventStartDate rows after 14 update cycles).
                #
                # DELETE-then-INSERT is unconditionally correct here: it
                # doesn't matter whether a row existed before, the result
                # is always exactly one row per (post_id, meta_key).
                cur.execute(
                    f"DELETE FROM {WP_PREFIX}postmeta WHERE post_id=%s AND meta_key=%s",
                    (post_id, key)
                )
                cur.execute(
                    f"INSERT INTO {WP_PREFIX}postmeta (post_id,meta_key,meta_value) "
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


def _clean_title(t: str) -> str:
    """Normalize smart quotes and unicode punctuation to plain ASCII."""
    replacements = [
        ('’', "'"), ('‘', "'"), ('“', '"'), ('”', '"'),
        ('–', '-'), ('—', '-'), ('…', '...'), ('â', "'"),
    ]
    for old_c, new_c in replacements:
        t = t.replace(old_c, new_c)
    return t


def insert_event(event: dict, city_config: dict = None) -> bool:
    """
    Insert or update an event. Opens its own DB connection.
    Never leaves the connection open regardless of outcome.

    Race-safety: the fingerprint is claimed via an atomic INSERT into
    wp_openclaw_fingerprints (UNIQUE on fp) BEFORE any post row is
    created. If two workers race for the same fingerprint at the same
    moment, exactly one INSERT succeeds; the other hits a duplicate-key
    error and falls back to update_event() against the winner's post.
    This is what the old check-then-insert flow lacked -- it checked
    "does this exist," got "no" from both racing workers, and let both
    proceed to insert, which is how the 731-row Bodies/Denver flood and
    the smaller cross-venue duplicates happened.

    Times are resolved via resolve_event_times() FIRST, before the
    fingerprint is computed, so the fingerprint always hashes the same
    normalized start instant regardless of which raw date fields the
    source happened to populate on a given pull. See make_fingerprint()
    docstring for why this ordering matters -- hashing the raw event
    dict directly is what caused the 2026-06-26 "Open Mic Comedy Night"
    flood (20+ posts created in under two seconds for one logical
    event, no warning logged, because each pull's raw fields hashed
    differently).

    ISOLATION-LEVEL BUG (found 2026-06-26, second flood the same day):
    MySQL's default REPEATABLE READ isolation means this whole
    function runs inside ONE transaction with a snapshot fixed at its
    first SELECT. The old retry loop re-ran get_fingerprint_post_id()
    against that SAME connection/transaction -- so even though another
    thread's winning INSERT had fully committed in the database, this
    transaction's snapshot was taken before that commit and could never
    see it, no matter how many times or how long it retried. Every race
    was guaranteed to "time out" and skip that cycle (logged as
    "Fingerprint claim race unresolved"), and on a LATER scrape cycle
    (a fresh connection/transaction with a fresh snapshot), the
    existence check at the top could itself lose a fresh race and
    repeat the same dance -- occasionally landing on a true duplicate
    post hours after the original race, which is what was still
    happening even after the post_id-backfill-timing fix earlier today.

    Fix: the retry loop now opens a SEPARATE short-lived connection for
    each re-check, so every retry attempt gets its own fresh snapshot
    and can actually see newly committed data instead of being frozen
    at the moment this function started.
    """
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    t   = resolve_event_times(event, now)
    fp  = make_fingerprint(event, resolved_times=t)
    conn = get_connection()
    try:
        # Existing post already mapped to this fingerprint? Just update it.
        existing_id = get_fingerprint_post_id(conn, fp)
        if existing_id:
            return update_event(conn, existing_id, event, city_config)

        # Claim the fingerprint atomically. Reserve post_id=0 as a
        # placeholder row while we build the real post below, then
        # update it to the real post_id once we have one. The UNIQUE
        # constraint on fp is what actually prevents the race -- only
        # one worker's INSERT here can ever succeed for a given fp.
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"INSERT INTO {WP_PREFIX}openclaw_fingerprints (fp, post_id, created) "
                    f"VALUES (%s, 0, %s)",
                    (fp, now)
                )
            conn.commit()
        except pymysql.err.IntegrityError:
            # Lost the race -- another worker claimed this fingerprint
            # between our get_fingerprint_post_id() check and now.
            # Roll back our half-open transaction, then find and update
            # whichever post the winner created.
            conn.rollback()

            # IMPORTANT: from here on, use a FRESH connection for every
            # lookup attempt instead of reusing `conn`. `conn`'s
            # transaction snapshot was established back at the
            # existence check above (REPEATABLE READ) and will never
            # see the winner's commit no matter how many times we
            # query it on this same connection. A new connection means
            # a new snapshot taken at query time, which can see
            # anything already committed by another thread.
            for attempt in range(25):
                lookup_conn = get_connection()
                try:
                    winner_id = get_fingerprint_post_id(lookup_conn, fp)
                finally:
                    lookup_conn.close()
                if winner_id and winner_id != 0:
                    # Use a fresh connection for the update too, rather
                    # than the original `conn` whose snapshot may also
                    # be stale for the post's own row state.
                    update_conn = get_connection()
                    try:
                        return update_event(update_conn, winner_id, event, city_config)
                    finally:
                        update_conn.close()
                time.sleep(0.2)
            log.warning("Fingerprint claim race unresolved for '%s' -- skipping this cycle", event.get("title"))
            return False

        # New insert — we now hold the only claim on this fingerprint.
        title   = _clean_title((event.get("title") or "Untitled Event").strip())
        content = (event.get("description") or "").strip()
        slug    = unique_post_slug(conn, slugify(title))

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

            # Fill in the real post_id on our fingerprint claim row right
            # away -- BEFORE the slower category/image/TEC-index writes
            # below. A racing second worker's wait-and-retry loop (above)
            # polls this row, so the sooner it's filled in, the sooner
            # that worker finds it instead of timing out and skipping.
            cur.execute(
                f"UPDATE {WP_PREFIX}openclaw_fingerprints SET post_id=%s WHERE fp=%s",
                (post_id, fp)
            )
            conn.commit()

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
