"""
backfill_ticketweb_images.py — one-off backfill for TicketWeb event images

Problem: _apply_image() in db.py only sideloads/sets a thumbnail if the post
has NO _thumbnail_id yet. Every event already published before the
_fetch_ticketweb_image() fix (bot-blocking headers + silent-debug logging)
got stuck with TM's generic placeholder image as its permanent thumbnail --
normal scrape re-runs will never touch it again.

This script:
  1. Finds every published, Ticketmaster-sourced event (_openclaw_source
     meta = 'Ticketmaster').
  2. Unwraps its stored _EventURL (the affiliate-decorated ticket link) to
     find the real ticketweb.com URL, if any.
  3. Re-fetches the real flyer via the FIXED _fetch_ticketweb_image()
     (proper browser headers).
  4. If a real image comes back, sideloads it as a new attachment and
     OVERWRITES _thumbnail_id -- this is the one place we deliberately
     bypass _apply_image()'s "skip if thumbnail exists" guard.

Safe to re-run: events with no TicketWeb URL, or where the real fetch still
fails, are simply skipped and left untouched.

Usage:
  python3 backfill_ticketweb_images.py            # dry run -- report only
  python3 backfill_ticketweb_images.py --apply     # actually update DB
"""

import argparse
import logging
import os
import sys
from pathlib import Path

from config import WP_PREFIX
from db import get_connection, sideload_image
from ticketmaster import _extract_ticketweb_url, _fetch_ticketweb_image

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("openclaw.backfill_ticketweb_images")


def find_candidates(conn) -> list[dict]:
    """
    Published Ticketmaster-sourced events with their current _EventURL and
    (if any) current _thumbnail_id.
    """
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT p.ID AS post_id,
                   p.post_title AS title,
                   url_meta.meta_value AS ticket_url,
                   thumb_meta.meta_value AS thumbnail_id
            FROM {WP_PREFIX}posts p
            JOIN {WP_PREFIX}postmeta src_meta
              ON src_meta.post_id = p.ID
             AND src_meta.meta_key = '_openclaw_source'
             AND src_meta.meta_value = 'Ticketmaster'
            JOIN {WP_PREFIX}postmeta url_meta
              ON url_meta.post_id = p.ID
             AND url_meta.meta_key = '_EventURL'
            LEFT JOIN {WP_PREFIX}postmeta thumb_meta
              ON thumb_meta.post_id = p.ID
             AND thumb_meta.meta_key = '_thumbnail_id'
            WHERE p.post_type = 'tribe_events'
              AND p.post_status = 'publish'
            """
        )
        return cur.fetchall()


def delete_attachment(conn, attachment_id: int) -> bool:
    """
    Delete an attachment post entirely: its file on disk, its postmeta rows,
    and the wp_posts row itself. Best-effort -- a failure here never rolls
    back the thumbnail swap that already succeeded, since a stray orphaned
    file is a cosmetic cleanup issue, not a correctness one.
    """
    try:
        upload_dir = os.getenv("WP_UPLOAD_DIR", "/var/www/html/wp-content/uploads")
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT meta_value FROM {WP_PREFIX}postmeta "
                f"WHERE post_id=%s AND meta_key='_wp_attached_file' LIMIT 1",
                (attachment_id,)
            )
            row = cur.fetchone()
        if row and row["meta_value"]:
            file_path = Path(upload_dir) / row["meta_value"]
            try:
                file_path.unlink(missing_ok=True)
            except Exception as e:
                log.warning("Couldn't delete old image file %s: %s", file_path, e)

        with conn.cursor() as cur:
            cur.execute(f"DELETE FROM {WP_PREFIX}postmeta WHERE post_id=%s", (attachment_id,))
            cur.execute(f"DELETE FROM {WP_PREFIX}posts WHERE ID=%s", (attachment_id,))
        return True
    except Exception as e:
        log.warning("Failed to clean up old attachment %d: %s", attachment_id, e)
        return False


def replace_thumbnail(conn, post_id: int, title: str, new_image_url: str,
                       old_thumbnail_id) -> int | None:
    """
    Sideload the real image, overwrite _thumbnail_id, and delete the old
    attachment (file + postmeta + post row) so nothing orphaned is left
    behind. Returns the new attachment id.
    """
    att = sideload_image(conn, new_image_url, post_id, title)
    if not att:
        return None
    with conn.cursor() as cur:
        if old_thumbnail_id:
            cur.execute(
                f"UPDATE {WP_PREFIX}postmeta SET meta_value=%s "
                f"WHERE post_id=%s AND meta_key='_thumbnail_id'",
                (att, post_id)
            )
        else:
            cur.execute(
                f"INSERT INTO {WP_PREFIX}postmeta (post_id,meta_key,meta_value) "
                f"VALUES (%s,'_thumbnail_id',%s)",
                (post_id, att)
            )
    # New thumbnail is live -- now safe to clean up the old one.
    if old_thumbnail_id:
        try:
            old_id = int(old_thumbnail_id)
            delete_attachment(conn, old_id)
        except (TypeError, ValueError):
            log.warning("Old thumbnail id %r not a valid int -- skipping cleanup", old_thumbnail_id)
    return att


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true",
                        help="Actually write changes. Without this flag, dry-run only.")
    args = parser.parse_args()

    conn = get_connection()
    try:
        candidates = find_candidates(conn)
        log.info("Found %d published Ticketmaster events to check", len(candidates))

        n_no_tw_url   = 0
        n_fetch_fail  = 0
        n_updated     = 0

        for row in candidates:
            post_id      = row["post_id"]
            title        = row["title"]
            ticket_url   = row["ticket_url"] or ""
            thumbnail_id = row["thumbnail_id"]

            tw_url = _extract_ticketweb_url(ticket_url)
            if not tw_url:
                n_no_tw_url += 1
                continue

            real_image = _fetch_ticketweb_image(tw_url)
            if not real_image:
                n_fetch_fail += 1
                log.warning("[%d] '%s' -- no real image found at %s", post_id, title, tw_url)
                continue

            if not args.apply:
                log.info("[DRY RUN] [%d] '%s' -- would replace thumbnail (old att=%s, "
                          "would be deleted) with %s",
                          post_id, title, thumbnail_id, real_image)
                n_updated += 1
                continue

            att = replace_thumbnail(conn, post_id, title, real_image, thumbnail_id)
            if att:
                conn.commit()
                n_updated += 1
                log.info("[%d] '%s' -- thumbnail replaced (old=%s, new att=%d)",
                          post_id, title, thumbnail_id, att)
            else:
                conn.rollback()
                n_fetch_fail += 1
                log.warning("[%d] '%s' -- sideload failed for %s", post_id, title, real_image)

        log.info(
            "Done. candidates=%d, no_ticketweb_url=%d, fetch_or_sideload_failed=%d, %s=%d",
            len(candidates), n_no_tw_url, n_fetch_fail,
            "would_update" if not args.apply else "updated",
            n_updated,
        )
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
