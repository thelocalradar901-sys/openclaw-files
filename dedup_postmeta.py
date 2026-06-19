"""
dedup_postmeta.py -- remove duplicate (post_id, meta_key) rows in
wp_postmeta left behind by the old update_event() bug (fixed in db.py),
which inserted a brand-new row every scrape cycle instead of updating
the existing one in place.

For every (post_id, meta_key) pair with more than one row, keeps the
row with the HIGHEST meta_id (the most recently inserted -- since rows
were never updated in place under the old bug, the highest meta_id is
also the most recently scraped, correct value) and deletes the rest.

SAFETY:
  - Only touches rows where a real duplicate (post_id, meta_key) pair
    exists. A post with exactly one row per key is left completely
    untouched.
  - Runs in batches, not one giant DELETE.
  - Defaults to DRY RUN (counts only). Pass --execute to actually delete.
  - Take a fresh backup_db.py dump before running --execute, in addition
    to the one from the orphan cleanup -- the data has changed since then.

Run directly on the server:
    cd /opt/openclaw && venv/bin/python3 dedup_postmeta.py           # dry run
    cd /opt/openclaw && venv/bin/python3 dedup_postmeta.py --execute # actually delete
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


def count_excess_duplicates(cur):
    """
    Total number of EXCESS rows -- i.e. rows beyond the one we'll keep
    for each (post_id, meta_key) pair. This is what we expect to delete.
    """
    cur.execute("""
        SELECT SUM(n - 1) AS excess
        FROM (
            SELECT post_id, meta_key, COUNT(*) AS n
            FROM wp_postmeta
            GROUP BY post_id, meta_key
            HAVING n > 1
        ) AS grouped
    """)
    return cur.fetchone()["excess"] or 0


def main():
    execute = "--execute" in sys.argv

    print("=" * 70)
    if execute:
        print("EXECUTE MODE -- this will actually delete rows.")
    else:
        print("DRY RUN -- counts only, nothing will be deleted.")
        print("Re-run with --execute when you're ready to actually clean up.")
    print("=" * 70)

    conn = get_conn()
    cur = conn.cursor()

    before = count_excess_duplicates(cur)
    print(f"\nExcess duplicate postmeta rows (beyond one-per-key): {before}")

    if before == 0:
        print("Nothing to do -- postmeta is already clean.")
        conn.close()
        return

    if not execute:
        print(f"\nDRY RUN -- would delete {before} rows in batches of {BATCH_SIZE}.")
        print("Re-run with --execute to actually delete.")
        conn.close()
        return

    deleted_total = 0
    start = time.time()
    while True:
        # For each (post_id, meta_key) pair with duplicates, delete every
        # row EXCEPT the one with the highest meta_id (most recent value
        # under the old bug, since rows were never updated in place).
        # Wrapped in a derived table because MySQL won't allow directly
        # referencing the table being deleted from inside its own
        # subquery.
        cur.execute(f"""
            DELETE FROM wp_postmeta
            WHERE meta_id IN (
                SELECT meta_id FROM (
                    SELECT pm.meta_id
                    FROM wp_postmeta pm
                    JOIN (
                        SELECT post_id, meta_key, MAX(meta_id) AS keep_id
                        FROM wp_postmeta
                        GROUP BY post_id, meta_key
                        HAVING COUNT(*) > 1
                    ) dupes ON dupes.post_id = pm.post_id AND dupes.meta_key = pm.meta_key
                    WHERE pm.meta_id != dupes.keep_id
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
    after = count_excess_duplicates(cur)
    print(f"\nDone in {elapsed:.1f}s.")
    print(f"Excess duplicate rows after: {after} (deleted {deleted_total} total)")

    conn.close()


if __name__ == "__main__":
    main()
