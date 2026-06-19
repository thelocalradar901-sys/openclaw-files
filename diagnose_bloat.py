"""
diagnose_bloat.py — figure out why wp-admin Events list shows way more
rows than real events exist.

Run directly on the server:
    cd /opt/openclaw && venv/bin/python3 diagnose_bloat.py

No arguments, no quoting gymnastics. Reads DB credentials from the same
env vars config.py uses (WP_DB_HOST/PORT/USER/PASSWORD/NAME), so source
the env file first if they're not already in your shell:
    set -a && . /etc/openclaw/openclaw.env && set +a
"""

import os
import sys

import pymysql
import pymysql.cursors


def _load_env_file_if_needed():
    """
    If WP_DB_PASSWORD isn't already in the environment (e.g. running this
    script directly rather than through systemd, which normally injects it
    via EnvironmentFile=), read /etc/openclaw/openclaw.env by hand and
    populate os.environ from it. Does nothing if the var is already set,
    so sourcing it yourself first still works fine too.
    """
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
            # Strip matching surrounding quotes if present (common in .env files)
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
    )


def main():
    conn = get_conn()
    cur = conn.cursor()

    print("=== 1. Total tribe_events posts, by status ===")
    cur.execute("""
        SELECT post_status, COUNT(*) AS n
        FROM wp_posts
        WHERE post_type = 'tribe_events'
        GROUP BY post_status
        ORDER BY n DESC
    """)
    rows = cur.fetchall()
    total = sum(r["n"] for r in rows)
    for r in rows:
        print(f"  {r['post_status']:>10}: {r['n']}")
    print(f"  {'TOTAL':>10}: {total}")

    print("\n=== 2. Duplicate titles (same title appearing many times) ===")
    cur.execute("""
        SELECT post_title, COUNT(*) AS n
        FROM wp_posts
        WHERE post_type = 'tribe_events'
        GROUP BY post_title
        HAVING n > 1
        ORDER BY n DESC
        LIMIT 20
    """)
    dupes = cur.fetchall()
    if dupes:
        for d in dupes:
            title = (d["post_title"] or "(empty)")[:70]
            print(f"  {d['n']:>5}x  {title}")
    else:
        print("  None found — duplicates are not by exact title match.")

    print("\n=== 3. Duplicate _openclaw_fp fingerprints (should be unique per event) ===")
    cur.execute("""
        SELECT meta_value AS fp, COUNT(*) AS n
        FROM wp_postmeta
        WHERE meta_key = '_openclaw_fp'
        GROUP BY meta_value
        HAVING n > 1
        ORDER BY n DESC
        LIMIT 20
    """)
    fp_dupes = cur.fetchall()
    if fp_dupes:
        print(f"  Found {len(fp_dupes)} fingerprints with duplicates (showing top 20):")
        for d in fp_dupes:
            print(f"  {d['n']:>5}x  fp={d['fp']}")
    else:
        print("  None found — fingerprints are all unique. Bloat is NOT from fingerprint collisions.")

    print("\n=== 4. Total fingerprinted rows vs total tribe_events posts ===")
    cur.execute("SELECT COUNT(*) AS n FROM wp_postmeta WHERE meta_key = '_openclaw_fp'")
    fp_count = cur.fetchone()["n"]
    print(f"  tribe_events posts: {total}")
    print(f"  _openclaw_fp rows:  {fp_count}")
    if fp_count > total * 1.5:
        print("  ⚠ Far more fingerprint rows than posts — possible orphaned postmeta "
              "(meta rows left behind after posts were deleted without cleaning up meta).")

    print("\n=== 5. wp_postmeta row count by meta_key for tribe_events posts (top 20) ===")
    cur.execute("""
        SELECT pm.meta_key, COUNT(*) AS n
        FROM wp_postmeta pm
        GROUP BY pm.meta_key
        ORDER BY n DESC
        LIMIT 20
    """)
    for r in cur.fetchall():
        print(f"  {r['n']:>10}  {r['meta_key']}")

    print("\n=== 6. Are there orphaned postmeta rows (post_id with no matching wp_posts row)? ===")
    cur.execute("""
        SELECT COUNT(*) AS n
        FROM wp_postmeta pm
        LEFT JOIN wp_posts p ON p.ID = pm.post_id
        WHERE p.ID IS NULL
    """)
    orphan_count = cur.fetchone()["n"]
    print(f"  Orphaned postmeta rows (no matching post): {orphan_count}")
    if orphan_count > 100000:
        print("  ⚠ This is almost certainly your '2M items' — postmeta rows left behind "
              "after posts were deleted (e.g. a nuke/trash operation that didn't clean up "
              "wp_postmeta, or wp_tec_events/wp_tec_occurrences) rather than 2M real events.")

    print("\n=== 7. Same orphan check against wp_tec_events / wp_tec_occurrences ===")
    for table in ("wp_tec_events", "wp_tec_occurrences"):
        try:
            cur.execute(f"SELECT COUNT(*) AS n FROM {table}")
            tbl_total = cur.fetchone()["n"]
            cur.execute(f"""
                SELECT COUNT(*) AS n
                FROM {table} t
                LEFT JOIN wp_posts p ON p.ID = t.post_id
                WHERE p.ID IS NULL
            """)
            tbl_orphan = cur.fetchone()["n"]
            print(f"  {table}: {tbl_total} total rows, {tbl_orphan} orphaned (no matching post)")
        except Exception as e:
            print(f"  {table}: error checking — {e}")

    print("\n=== 8. wp_posts table total row count (ALL post types, for scale) ===")
    cur.execute("SELECT COUNT(*) AS n FROM wp_posts")
    print(f"  {cur.fetchone()['n']} total rows in wp_posts")

    conn.close()
    print("\nDone.")


if __name__ == "__main__":
    main()
