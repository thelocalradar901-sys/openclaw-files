"""
cleanup_ticketweb_images.py — one-off cleanup for events that were saved
BEFORE the TicketWeb image relay existed.

_apply_image() (db.py) only ever sets a post's featured image if it
doesn't already have one -- so any TicketWeb-sourced event that was
scraped before the relay went live is stuck with whatever generic image
it got back then (TM's own placeholder image, or a venue's site-wide
og:image fallback), forever, even though ticketmaster.py would now fetch
the real flyer via the relay on a fresh scrape.

This script does NOT fetch any images itself. It only:
  1. Finds every 'Ticketmaster'-sourced post
  2. Re-derives whether it's TicketWeb-linked, using the exact same
     _extract_ticketweb_url() logic ticketmaster.py itself uses (so this
     stays in sync with that logic automatically -- no separate
     "is this a TicketWeb URL" heuristic to maintain here)
  3. Clears that post's _thumbnail_id (and deletes the old attachment
     row) so it looks, to _apply_image(), exactly like a post that has
     never had an image set

The very next time this event gets pulled (normal TM_INTERVAL schedule,
no manual trigger needed) update_event() -> _apply_image() will see no
existing thumbnail and sideload whatever image_url ticketmaster.py's
_normalize() produces this time -- which now goes through the relay.

Run with no flags = dry run (prints what it WOULD clear, writes nothing).
Run with --apply to execute.
"""
import sys
import argparse
sys.path.insert(0, "/opt/openclaw")

from db import get_connection
from ticketmaster import _extract_ticketweb_url

WP_PREFIX = "wp_"


def find_candidates(conn):
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT p.ID AS post_id, p.post_title,
                MAX(CASE WHEN pm.meta_key='_EventURL'     THEN pm.meta_value END) AS ticket_url,
                MAX(CASE WHEN pm.meta_key='_thumbnail_id' THEN pm.meta_value END) AS thumb_id,
                MAX(CASE WHEN pm.meta_key='_openclaw_source' THEN pm.meta_value END) AS source_name
            FROM {WP_PREFIX}posts p
            LEFT JOIN {WP_PREFIX}postmeta pm ON pm.post_id = p.ID
            WHERE p.post_type = 'tribe_events' AND p.post_status IN ('publish','draft')
            GROUP BY p.ID, p.post_title
            HAVING source_name = 'Ticketmaster' AND thumb_id IS NOT NULL AND thumb_id != ''
        """)
        rows = cur.fetchall()

    candidates = []
    for r in rows:
        if _extract_ticketweb_url(r["ticket_url"] or ""):
            candidates.append(r)
    return candidates


def clear_thumbnail(conn, post_id: int, thumb_id: str):
    with conn.cursor() as cur:
        # Delete the old attachment's own post row + its meta -- sideload_image()
        # creates a fresh attachment post every time it's called, so this
        # attachment isn't shared with anything else and is safe to remove
        # outright rather than just orphaning it.
        cur.execute(f"DELETE FROM {WP_PREFIX}postmeta WHERE post_id=%s", (thumb_id,))
        cur.execute(f"DELETE FROM {WP_PREFIX}posts WHERE ID=%s AND post_type='attachment'", (thumb_id,))
        # Clear the event post's pointer to it
        cur.execute(
            f"DELETE FROM {WP_PREFIX}postmeta WHERE post_id=%s AND meta_key='_thumbnail_id'",
            (post_id,)
        )
    conn.commit()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    conn = get_connection()
    candidates = find_candidates(conn)
    print(f"TicketWeb-sourced posts with a pre-relay thumbnail: {len(candidates)}\n")

    for c in candidates:
        print(f"[{c['post_id']}] '{c['post_title']}' -- thumb_id={c['thumb_id']}")
        if args.apply:
            clear_thumbnail(conn, c["post_id"], c["thumb_id"])

    if args.apply:
        print(f"\nAPPLIED: cleared {len(candidates)} thumbnails. "
              f"Each will get its real image on its next scheduled TM pull.")
    else:
        print("\nDRY RUN -- no changes written. Re-run with --apply to execute.")
    conn.close()


if __name__ == "__main__":
    main()
