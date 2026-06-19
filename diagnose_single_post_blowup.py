"""
diagnose_single_post_blowup.py -- the wp-admin Events list is showing the
SAME real post (same edit link / same post ID) as many duplicate-looking
rows, not actually duplicate posts. This points at a join fanning out
per-meta-row or per-term-row rather than per-post.

Run directly on the server:
    cd /opt/openclaw && venv/bin/python3 diagnose_single_post_blowup.py
"""

import os

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
    )


def main():
    conn = get_conn()
    cur = conn.cursor()

    print("=== 1. Find the 'Reindeer Run 5K and Diaper Drive' post(s) by title ===")
    cur.execute("""
        SELECT ID, post_title, post_status
        FROM wp_posts
        WHERE post_type = 'tribe_events' AND post_title LIKE 'Reindeer Run%'
    """)
    posts = cur.fetchall()
    for p in posts:
        print(f"  ID {p['ID']}  [{p['post_status']}]  {p['post_title']}")

    if not posts:
        print("  No matching post found -- title may differ slightly, adjust the LIKE pattern.")
        conn.close()
        return

    print(f"\n  Found {len(posts)} real distinct post row(s) with this title.")
    print("  (If this is 1, the post itself is NOT duplicated in wp_posts --")
    print("   confirming the admin list is multiplying rows via a join, not real dupes.)")

    print("\n=== 2. For each matching post, count postmeta rows and term relationships ===")
    for p in posts:
        pid = p["ID"]
        cur.execute("SELECT COUNT(*) AS n FROM wp_postmeta WHERE post_id=%s", (pid,))
        meta_count = cur.fetchone()["n"]

        cur.execute("SELECT COUNT(*) AS n FROM wp_term_relationships WHERE object_id=%s", (pid,))
        term_count = cur.fetchone()["n"]

        cur.execute("""
            SELECT meta_key, COUNT(*) AS n
            FROM wp_postmeta
            WHERE post_id=%s
            GROUP BY meta_key
            HAVING n > 1
            ORDER BY n DESC
        """, (pid,))
        repeated_keys = cur.fetchall()

        print(f"\n  Post ID {pid}:")
        print(f"    postmeta rows total:        {meta_count}")
        print(f"    term_relationships rows:    {term_count}  (categories/tags)")
        if repeated_keys:
            print("    meta_keys appearing MORE THAN ONCE for this single post (should never happen):")
            for rk in repeated_keys:
                print(f"      {rk['n']:>4}x  {rk['meta_key']}")
        else:
            print("    No meta_key appears more than once for this post -- postmeta is clean.")

    print("\n=== 3. Simulate the actual wp-admin list query shape (post LEFT JOIN postmeta/terms) ===")
    print("    Mimics how a naive WP_Query/custom query joining one-to-many tables")
    print("    without DISTINCT can multiply rows, to see if it reproduces the blowup.")
    pid_list = ",".join(str(p["ID"]) for p in posts)
    cur.execute(f"""
        SELECT p.ID, COUNT(*) AS row_count_if_joined
        FROM wp_posts p
        LEFT JOIN wp_postmeta pm ON pm.post_id = p.ID
        LEFT JOIN wp_term_relationships tr ON tr.object_id = p.ID
        WHERE p.ID IN ({pid_list})
        GROUP BY p.ID
    """)
    for r in cur.fetchall():
        print(f"  Post ID {r['ID']}: a naive JOIN across postmeta+terms with no DISTINCT "
              f"would produce {r['row_count_if_joined']} rows for this ONE post.")

    conn.close()
    print("\nDone.")
    print("\nIf step 3's number is large (hundreds/thousands), that's very likely the")
    print("multiplier behind '2,449,257 items' -- postmeta_count * term_count per post,")
    print("summed across ~3,240 real posts, can reach into the millions.")
    print("Caused by a plugin/theme/custom query joining wp_postmeta or")
    print("wp_term_relationships without GROUP BY/DISTINCT on the events list screen.")
