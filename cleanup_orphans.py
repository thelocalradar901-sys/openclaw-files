"""
cleanup_orphans.py — delete orphaned wp_postmeta / wp_tec_events /
wp_tec_occurrences rows left behind by posts that were deleted directly
via SQL (bypassing WordPress's own wp_delete_post(), which normally
cleans up postmeta automatically).

SAFETY:
  - Only deletes rows whose post_id has NO matching row in wp_posts.
    Never touches a row attached to a real, existing post.
  - Runs in batches (default 5,000 rows at a time) rather than one giant
    DELETE, so it doesn't hold a long table lock or risk a query timeout.
  - Prints before/after counts for every table it touches.
  - Defaults to DRY RUN (counts only, no deletes). Pass --execute to
    actually delete.

Run directly on the server:
    cd /opt/openclaw && venv/bin/python3 cleanup_orphans.py           # dry run
    cd /opt/openclaw && venv/bin/python3 cleanup_orphans.py --execute # actually delete

Make sure you have a recent backup before running with --execute.
"""

import os
import sys
import time

import pymysql
import pymysql.cursors

BATCH_SIZE = 5000


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


def count_orphans(cur, table, id_col="post_id"):
    cur.execute(f"""
        SELECT COUNT(*) AS n
        FROM {table} t
        LEFT JOIN wp_posts p ON p.ID = t.{id_col}
        WHERE p.ID IS NULL
    """)
    return cur.fetchone()["n"]


def delete_orphans_in_batches(conn, cur, table, id_col, execute):
    before = count_orphans(cur, table, id_col)
    print(f"\n=== {table} ===")
    print(f"  Orphaned rows before: {before}")

    if before == 0:
        print("  Nothing to do.")
        return

    if not execute:
        print(f"  DRY RUN — would delete {before} rows in batches of {BATCH_SIZE}. "
              f"Re-run with --execute to actually delete.")
        return

    deleted_total = 0
    start = time.time()
    while True:
        # Delete a bounded batch at a time. Using a subquery with LIMIT
        # because MySQL doesn't allow LIMIT directly on a multi-table
        # DELETE...JOIN in all configurations — this form is safe and
        # portable.
        cur.execute(f"""
            DELETE FROM {table}
            WHERE {id_col} IN (
                SELECT t_id FROM (
                    SELECT t.{id_col} AS t_id
                    FROM {table} t
                    LEFT JOIN wp_posts p ON p.ID = t.{id_col}
                    WHERE p.ID IS NULL
                    LIMIT {BATCH_SIZE}
                ) AS batch
            )
        """)
        batch_deleted = cur.rowcount
        conn.commit()
        deleted_total += batch_deleted
        print(f"  Deleted batch: {batch_deleted} rows (total so far: {deleted_total})")
        if batch_deleted == 0:
            break

    elapsed = time.time() - start
    after = count_orphans(cur, table, id_col)
    print(f"  Done in {elapsed:.1f}s. Orphaned rows after: {after} "
          f"(deleted {deleted_total} total)")


def main():
    execute = "--execute" in sys.argv

    print("=" * 70)
    if execute:
        print("EXECUTE MODE — this will actually delete rows.")
    else:
        print("DRY RUN — counts only, nothing will be deleted.")
        print("Re-run with --execute when you're ready to actually clean up.")
    print("=" * 70)

    conn = get_conn()
    cur = conn.cursor()

    # wp_postmeta uses post_id; wp_tec_events / wp_tec_occurrences also
    # key on post_id per the schema confirmed in diagnose_bloat.py.
    delete_orphans_in_batches(conn, cur, "wp_postmeta", "post_id", execute)
    delete_orphans_in_batches(conn, cur, "wp_tec_events", "post_id", execute)
    delete_orphans_in_batches(conn, cur, "wp_tec_occurrences", "post_id", execute)

    conn.close()
    print("\nDone.")
    if not execute:
        print("\nNothing was deleted. Run again with --execute to apply the cleanup.")


if __name__ == "__main__":
    main()
