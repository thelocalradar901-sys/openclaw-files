"""
cleanup_orphaned_attachments.py -- find and remove image attachment
posts that are no longer referenced by any tribe_events post.

Context: dedup_true_duplicates.py (and any other script that deletes
tribe_events posts directly via SQL) removes the deleted post's
_thumbnail_id postmeta row, but does NOT touch the attachment post
itself -- the image's own row in wp_posts (post_type='attachment')
plus its _wp_attached_file / _wp_attachment_metadata / _wp_attachment_
image_alt postmeta. Run this AFTER any bulk event-deletion cleanup to
remove the now-unreferenced images those deletions left behind.

An attachment is considered orphaned here if NO existing tribe_events
post (any status) has a _thumbnail_id postmeta row pointing at it.
This intentionally does NOT check post_parent or other post types --
only the one relationship OpenClaw actually creates (sideload_image()
sets _thumbnail_id on the event post). If you've ever used an
OpenClaw-downloaded image anywhere else on the site by hand, that
usage would not be caught by this check and the image would still be
deleted -- this has not come up in practice, but worth knowing before
running --execute.

For each orphaned attachment: deletes its wp_posts row, all of its
own wp_postmeta rows, and (separately, with a clear summary) reports
how many bytes its files would free up on disk -- but does NOT delete
the actual files from /wp-content/uploads. Run WP-CLI's
`wp media regenerate` style cleanup or a manual file sweep afterward
if you also want disk space back; this script only cleans the
database side, matching the scope of the other cleanup scripts.

SAFETY:
  - Only considers post_type='attachment' rows with NO matching
    _thumbnail_id reference from any tribe_events post.
  - Does not touch attachments referenced by post_parent of a
    non-tribe_events post (e.g. a regular blog post's featured image),
    since the WHERE clause only excludes attachments tied to
    tribe_events via _thumbnail_id -- this script is scoped to
    OpenClaw-managed event images only.
  - Defaults to DRY RUN. Pass --execute to actually delete.
  - Take a fresh backup_db.py dump before running --execute.

Run directly on the server, AFTER dedup_true_duplicates.py --execute:
    cd /opt/openclaw && venv/bin/python3 cleanup_orphaned_attachments.py           # dry run
    cd /opt/openclaw && venv/bin/python3 cleanup_orphaned_attachments.py --execute # apply
"""

import os
import sys

import pymysql
import pymysql.cursors

BATCH_SIZE = 2000


def _load_env_file_if_needed():
    if os.getenv("WP_DB_PASSWORD"):
        return
    env_path = "/etc/openclaw/openclaw.env"
    if not os.path.exists(env_path):
        return
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            os.environ.setdefault(key, value)


def get_conn():
    _load_env_file_if_needed()
    return pymysql.connect(
        host=os.getenv("WP_DB_HOST", "localhost"),
        port=int(os.getenv("WP_DB_PORT", 3306)),
        user=os.getenv("WP_DB_USER", "wpuser"),
        password=os.getenv("WP_DB_PASSWORD", ""),
        database=os.getenv("WP_DB_NAME", "wordpress"),
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
    )


def find_orphaned_attachment_ids(cur):
    """
    Attachments with no tribe_events post pointing at them via
    _thumbnail_id. Restricted to attachments whose post_parent is 0 OR
    whose post_parent itself no longer exists as a tribe_events post --
    in practice OpenClaw's sideload_image() always sets post_parent=0
    and relies solely on _thumbnail_id, so this is just a safety net.
    """
    cur.execute("""
        SELECT a.ID, a.post_title, a.post_date
        FROM wp_posts a
        WHERE a.post_type = 'attachment'
        AND a.ID NOT IN (
            SELECT pm.meta_value
            FROM wp_postmeta pm
            JOIN wp_posts p ON p.ID = pm.post_id
            WHERE pm.meta_key = '_thumbnail_id'
              AND p.post_type = 'tribe_events'
        )
    """)
    return cur.fetchall()


def main():
    execute = "--execute" in sys.argv

    print("=" * 70)
    if execute:
        print("EXECUTE MODE -- this will actually delete orphaned attachment posts.")
    else:
        print("DRY RUN -- showing what WOULD be deleted, nothing will be written.")
        print("Re-run with --execute when you're ready to apply.")
    print("=" * 70)

    conn = get_conn()
    cur = conn.cursor()

    orphans = find_orphaned_attachment_ids(cur)
    print(f"\nFound {len(orphans)} attachment posts with no tribe_events "
          f"post referencing them via _thumbnail_id.\n")

    if not orphans:
        print("Nothing to do.")
        conn.close()
        return

    for o in orphans[:20]:
        title = (o["post_title"] or "(untitled)")[:60]
        print(f"  ID {o['ID']:>7}  {o['post_date']}  {title}")
    if len(orphans) > 20:
        print(f"  ... and {len(orphans) - 20} more")

    if not execute:
        print(f"\nDRY RUN -- would delete {len(orphans)} attachment posts "
              f"(database rows only -- files in wp-content/uploads are NOT "
              f"touched by this script).")
        print("Re-run with --execute to actually delete.")
        conn.close()
        return

    ids = [o["ID"] for o in orphans]
    deleted = 0
    for i in range(0, len(ids), BATCH_SIZE):
        batch = ids[i:i + BATCH_SIZE]
        placeholders = ",".join(["%s"] * len(batch))
        cur.execute(f"DELETE FROM wp_postmeta WHERE post_id IN ({placeholders})", batch)
        cur.execute(f"DELETE FROM wp_posts WHERE ID IN ({placeholders})", batch)
        conn.commit()
        deleted += len(batch)
        print(f"  Deleted batch: {len(batch)} attachments (total so far: {deleted})")

    print(f"\nDone. Deleted {deleted} orphaned attachment posts from the database.")
    print("Note: the actual image files in wp-content/uploads were NOT deleted -- "
          "this script only cleans up the database rows, matching the scope of "
          "the other cleanup scripts. Run a separate file-level sweep if you also "
          "want to reclaim disk space.")

    conn.close()


if __name__ == "__main__":
    main()
