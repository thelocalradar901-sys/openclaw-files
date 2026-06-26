"""
delete_cancelled_events.py -- one-time cleanup to remove all currently
published/draft tribe_events posts whose title indicates the event was
cancelled (e.g. "*Cancelled* Sonido Gallo Negro"). Going forward,
db.py's insert_event() detects and deletes these automatically as they
get re-scraped -- this script just clears out anything already live
on the site before that fix was deployed.

SAFETY:
  - Only matches clear cancellation title patterns: "*Cancelled*",
    "**CANCELLED**", "Cancelled: ...", "Event Cancelled". Does NOT
    match "(SOLD OUT)" or promotional suffixes -- those are not
    cancellations and are left alone.
  - Defaults to DRY RUN. Pass --execute to actually delete.

Run directly on the server:
    cd /opt/openclaw && venv/bin/python3 delete_cancelled_events.py           # dry run
    cd /opt/openclaw && venv/bin/python3 delete_cancelled_events.py --execute # apply
"""

import os
import re
import sys

import pymysql
import pymysql.cursors

_CANCELLED_RE = re.compile(
    r"\*+\s*cancelled\s*\*+|^\s*cancelled\s*[:\-]|\bevent\s+cancelled\b",
    re.IGNORECASE
)


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


def delete_post_completely(cur, post_id):
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
        print("EXECUTE MODE -- this will actually delete cancelled events.")
    else:
        print("DRY RUN -- showing what WOULD be deleted, nothing will be written.")
        print("Re-run with --execute when you're ready to apply.")
    print("=" * 70)

    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        SELECT ID, post_title
        FROM wp_posts
        WHERE post_type = 'tribe_events'
          AND post_status IN ('publish', 'draft')
    """)
    rows = cur.fetchall()

    cancelled = [r for r in rows if _CANCELLED_RE.search(r["post_title"] or "")]
    print(f"\nFound {len(cancelled)} cancelled events out of {len(rows)} total.\n")

    for r in cancelled:
        print(f"  ID {r['ID']:>7}  {r['post_title']}")

    if not cancelled:
        print("\nNothing to do.")
        conn.close()
        return

    if not execute:
        print(f"\nDRY RUN -- would delete {len(cancelled)} cancelled events.")
        print("Re-run with --execute to actually delete.")
        conn.close()
        return

    for r in cancelled:
        delete_post_completely(cur, r["ID"])
    conn.commit()

    print(f"\nDone. Deleted {len(cancelled)} cancelled events.")
    conn.close()


if __name__ == "__main__":
    main()
