"""
refetch_tm_categories.py -- recover Ticketmaster genre classifications
for events already in the DB that came from Ticketmaster (external_id
starts with "tm_") but never had their genre data persisted, since
_apply_categories() only started saving it to postmeta as of today's fix.

For each tm_* event: extracts the real Ticketmaster event ID from
external_id, calls the TM Discovery API's single-event lookup endpoint
to get its classifications fresh, and writes the recovered categories
to _openclaw_raw_categories postmeta -- the same field the live pipeline
now saves going forward. Does NOT change category term assignments
itself; that's what backfill_categories.py does afterward, once this
has restored the real signal for it to read.

SAFETY:
  - Read + postmeta-write only. Never touches wp_term_relationships.
  - Respects Ticketmaster's rate limits with a delay between calls.
  - Defaults to DRY RUN. Pass --execute to actually write.
  - A single failed/missing TM lookup (event since removed from TM,
    API hiccup, etc.) is skipped and logged, never crashes the run.

Run directly on the server:
    cd /opt/openclaw && venv/bin/python3 refetch_tm_categories.py           # dry run
    cd /opt/openclaw && venv/bin/python3 refetch_tm_categories.py --execute # apply
"""

import json
import os
import sys
import time

import pymysql
import pymysql.cursors
import requests

TM_EVENT_BASE = "https://app.ticketmaster.com/discovery/v2/events/"
REQUEST_DELAY_SECONDS = 0.3  # be polite to TM's rate limits


def _load_env_file_if_needed():
    if os.getenv("WP_DB_PASSWORD") and os.getenv("TICKETMASTER_API_KEY"):
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


def fetch_tm_categories(tm_event_id: str, api_key: str):
    """Look up one TM event by ID, return its segment/genre/subGenre names."""
    resp = requests.get(
        f"{TM_EVENT_BASE}{tm_event_id}",
        params={"apikey": api_key},
        timeout=15,
    )
    if resp.status_code == 404:
        return None  # event no longer exists on TM -- not an error, just gone
    resp.raise_for_status()
    data = resp.json()

    categories = []
    for cl in data.get("classifications", []):
        for key in ("segment", "genre", "subGenre"):
            val = (cl.get(key) or {}).get("name", "")
            if val and val.lower() not in ("undefined", "") and val not in categories:
                categories.append(val)
    return categories


def main():
    execute = "--execute" in sys.argv
    _load_env_file_if_needed()

    api_key = os.getenv("TICKETMASTER_API_KEY", "")
    if not api_key:
        print("ERROR: TICKETMASTER_API_KEY not set. Check /etc/openclaw/openclaw.env.")
        sys.exit(1)

    print("=" * 70)
    if execute:
        print("EXECUTE MODE -- will write recovered categories to postmeta.")
    else:
        print("DRY RUN -- showing what WOULD be recovered, nothing will be written.")
        print("Re-run with --execute when ready to apply.")
    print("=" * 70)

    conn = get_conn()
    cur = conn.cursor()

    # Find tm_* events that don't already have _openclaw_raw_categories
    # saved (i.e. ones from before today's persistence fix).
    cur.execute("""
        SELECT pm.post_id, pm.meta_value AS external_id, p.post_title
        FROM wp_postmeta pm
        JOIN wp_posts p ON p.ID = pm.post_id
        WHERE pm.meta_key = '_openclaw_external_id'
          AND pm.meta_value LIKE 'tm\\_%'
          AND p.ID NOT IN (
              SELECT post_id FROM wp_postmeta WHERE meta_key = '_openclaw_raw_categories'
          )
    """)
    rows = cur.fetchall()
    print(f"\nFound {len(rows)} Ticketmaster events missing saved category data.\n")

    recovered = 0
    not_found = 0
    errors = 0

    for i, row in enumerate(rows, 1):
        post_id = row["post_id"]
        external_id = row["external_id"] or ""
        title = row["post_title"] or ""

        if not external_id.startswith("tm_"):
            continue
        tm_id = external_id[len("tm_"):]
        if not tm_id:
            continue

        try:
            categories = fetch_tm_categories(tm_id, api_key)
        except Exception as e:
            print(f"  [{i}/{len(rows)}] ERROR fetching {tm_id} ({title[:50]!r}): {e}")
            errors += 1
            time.sleep(REQUEST_DELAY_SECONDS)
            continue

        if categories is None:
            not_found += 1
            time.sleep(REQUEST_DELAY_SECONDS)
            continue

        if categories:
            recovered += 1
            print(f"  [{i}/{len(rows)}] Post {post_id} ({title[:50]!r}): recovered {categories}")
            if execute:
                cur.execute(
                    "DELETE FROM wp_postmeta WHERE post_id=%s AND meta_key=%s",
                    (post_id, "_openclaw_raw_categories")
                )
                cur.execute(
                    "INSERT INTO wp_postmeta (post_id,meta_key,meta_value) VALUES (%s,%s,%s)",
                    (post_id, "_openclaw_raw_categories", json.dumps(categories))
                )
                conn.commit()

        time.sleep(REQUEST_DELAY_SECONDS)

    print(f"\n{'=' * 70}")
    print(f"Checked:   {len(rows)}")
    print(f"Recovered: {recovered}{'  (dry run -- not actually saved)' if not execute else ''}")
    print(f"Not found on TM (event removed/expired): {not_found}")
    print(f"Errors:    {errors}")

    conn.close()


if __name__ == "__main__":
    main()
