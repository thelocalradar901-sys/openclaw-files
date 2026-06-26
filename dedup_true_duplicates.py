"""
dedup_true_duplicates.py -- find and remove TRUE duplicate tribe_events
posts: same post_title AND same exact _EventStartDate (full datetime,
not just date). This is the safe, narrow definition of "duplicate" --
it does NOT touch posts that share a title but have different dates
(e.g. a 5-game series like "Birmingham Barons vs. Knoxville Smokies"
playing June 24-28 is five legitimate separate posts, not duplicates).

Root cause (confirmed 2026-06-26): make_fingerprint() was hashing the
RAW event dict before resolve_event_times() normalized it, so the same
logical event could get a different fingerprint hash across separate
scrape runs and create a second post instead of matching the first.
Fixed in db.py going forward -- this script cleans up what the bug
already created.

For each (post_title, _EventStartDate) pair with more than one post:
  - Keeps the LOWEST post ID (the original/first-scraped copy)
  - Deletes every other post in the group: wp_posts row, all wp_postmeta
    rows, and wp_tec_events / wp_tec_occurrences rows for that post_id.
  - Also removes the deleted post's row from wp_openclaw_fingerprints
    (matched by post_id) so a stale claim row doesn't linger.

SAFETY:
  - Only matches on EXACT same title + EXACT same full _EventStartDate
    string (including time). Same title with a different date/time is
    left completely untouched.
  - Never deletes the lowest (oldest) ID in any duplicate group.
  - Defaults to DRY RUN -- prints what WOULD be deleted, no writes.
    Pass --execute to actually delete.
  - Take a fresh backup_db.py dump before running --execute.

Run directly on the server:
    cd /opt/openclaw && venv/bin/python3 dedup_true_duplicates.py           # dry run
    cd /opt/openclaw && venv/bin/python3 dedup_true_duplicates.py --execute # actually delete
"""

import os
import sys

import pymysql
import pymysql.cursors


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


def find_duplicate_groups(cur):
    """
    Returns a list of dicts: {title, start_date, post_ids: [sorted ascending]}
    for every (title, start_date) pair with more than one tribe_events post.
    Only considers publish/draft posts -- already-trashed posts are
    ignored so we don't "clean up" something you already trashed on
    purpose.
    """
    cur.execute("""
        SELECT p.post_title AS title, pm.meta_value AS start_date,
               GROUP_CONCAT(p.ID ORDER BY p.ID ASC) AS ids
        FROM wp_posts p
        JOIN wp_postmeta pm ON pm.post_id = p.ID AND pm.meta_key = '_EventStartDate'
        WHERE p.post_type = 'tribe_events'
          AND p.post_status IN ('publish', 'draft')
        GROUP BY p.post_title, pm.meta_value
        HAVING COUNT(*) > 1
    """)
    groups = []
    for row in cur.fetchall():
        ids = [int(x) for x in row["ids"].split(",")]
        groups.append({
            "title": row["title"],
            "start_date": row["start_date"],
            "post_ids": sorted(ids),
        })
    return groups


def delete_post_completely(cur, post_id):
    """Delete one post and every table row that references it."""
    cur.execute("DELETE FROM wp_postmeta WHERE post_id=%s", (post_id,))
    cur.execute("DELETE FROM wp_tec_events WHERE post_id=%s", (post_id,))
    cur.execute("DELETE FROM wp_tec_occurrences WHERE post_id=%s", (post_id,))
    cur.execute("DELETE FROM wp_term_relationships WHERE object_id=%s", (post_id,))
    cur.execute("DELETE FROM wp_openclaw_fingerprints WHERE post_id=%s", (post_id,))
    cur.execute("DELETE FROM wp_posts WHERE ID=%s", (post_id,))


def main():
    execute = "--execute" in sys.argv

    print("=" * 70)
    if execute:
        print("EXECUTE MODE -- this will actually delete duplicate posts.")
    else:
        print("DRY RUN -- showing what WOULD be deleted, nothing will be written.")
        print("Re-run with --execute when you're ready to apply.")
    print("=" * 70)

    conn = get_conn()
    cur = conn.cursor()

    groups = find_duplicate_groups(cur)
    print(f"\nFound {len(groups)} (title, start_date) pairs with duplicate posts.\n")

    if not groups:
        print("Nothing to do -- no true duplicates found.")
        conn.close()
        return

    total_to_delete = 0
    for g in groups:
        keep = g["post_ids"][0]
        remove = g["post_ids"][1:]
        total_to_delete += len(remove)
        title_short = (g["title"] or "")[:60]
        print(f"  '{title_short}' @ {g['start_date']}")
        print(f"    keep ID {keep}, delete {remove}")

    print(f"\nTotal posts that would be deleted: {total_to_delete}")

    if not execute:
        print("\nDRY RUN -- nothing deleted. Re-run with --execute to apply.")
        conn.close()
        return

    deleted = 0
    for g in groups:
        for post_id in g["post_ids"][1:]:
            delete_post_completely(cur, post_id)
            deleted += 1
        conn.commit()

    print(f"\nDone. Deleted {deleted} duplicate posts (kept the original/lowest-ID "
          f"post in each group).")
    conn.close()


if __name__ == "__main__":
    main()
