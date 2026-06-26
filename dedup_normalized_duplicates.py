"""
dedup_normalized_duplicates.py -- catches duplicates that
dedup_true_duplicates.py misses because it only matches on EXACT
title strings. This script matches on the SAME normalized title
(after stripping known promotional/reseller suffixes, via the same
normalize_title_for_matching() logic added to db.py) + the SAME exact
start date/time.

Example this catches that the exact-match script doesn't:
  'Vienna Light Orchestra' @ 2026-12-06
  'Vienna Light Orchestra | Official BJCC Ticket + Hotel Packages.' @ 2026-12-06
These are two different raw title strings (so the exact-match dedup
correctly left them alone) but normalize to the identical comparison
key 'vienna light orchestra', and share the exact same date/time --
they're the same real event, duplicated before the suffix-normalization
fix was deployed to db.py.

Which post survives a duplicate group: prefers the post whose title
does NOT have a promotional suffix stripped from it (i.e. prefers
"Vienna Light Orchestra" over "Vienna Light Orchestra | Official BJCC
Ticket + Hotel Packages.") since the plain title is what you'd
actually want displayed on the site. If neither or both have a
suffix, falls back to keeping the lowest post ID, same as
dedup_true_duplicates.py.

SAFETY:
  - Only groups posts that share BOTH the same normalized title AND
    the same exact full _EventStartDate -- never merges different
    dates or meaningfully different titles.
  - Defaults to DRY RUN. Pass --execute to actually delete.
  - Take a fresh backup_db.py dump before running --execute.

Run directly on the server:
    cd /opt/openclaw && venv/bin/python3 dedup_normalized_duplicates.py           # dry run
    cd /opt/openclaw && venv/bin/python3 dedup_normalized_duplicates.py --execute # apply
"""

import os
import re
import sys
from collections import defaultdict

import pymysql
import pymysql.cursors

# Mirrors normalize_title_for_matching() / _PROMO_SUFFIX_PATTERNS in
# db.py -- kept as a self-contained copy here so this script doesn't
# need to import db.py (which requires the full env/config setup) just
# to run a read-mostly cleanup pass.
_PROMO_SUFFIX_PATTERNS = [
    r"\s*\|\s*official\s+\w+\s+ticket\s*\+\s*hotel\s+packages\.?\s*$",
    r"\s*\|\s*official\s+ticket\s*\+\s*hotel\s+packages\.?\s*$",
    r"\s*\(sold\s+out\)\s*$",
    r"\s*\*+\s*cancelled\s*\*+\s*$",
]
_PROMO_SUFFIX_RE = re.compile("|".join(_PROMO_SUFFIX_PATTERNS), re.IGNORECASE)


def normalize_title(title: str) -> str:
    cleaned = _PROMO_SUFFIX_RE.sub("", (title or "").strip())
    return cleaned.strip().lower()


def has_promo_suffix(title: str) -> bool:
    return bool(_PROMO_SUFFIX_RE.search((title or "").strip()))


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
    cur.execute("""
        SELECT p.ID, p.post_title, pm.meta_value AS start_date
        FROM wp_posts p
        JOIN wp_postmeta pm ON pm.post_id = p.ID AND pm.meta_key = '_EventStartDate'
        WHERE p.post_type = 'tribe_events'
          AND p.post_status IN ('publish', 'draft')
    """)
    rows = cur.fetchall()

    buckets = defaultdict(list)
    for r in rows:
        key = (normalize_title(r["post_title"]), r["start_date"])
        buckets[key].append(r)

    groups = []
    for (norm_title, start_date), posts in buckets.items():
        if len(posts) > 1:
            groups.append({
                "norm_title": norm_title,
                "start_date": start_date,
                "posts": posts,  # each: {ID, post_title, start_date}
            })
    return groups


def choose_keeper(posts):
    """
    Prefer a post whose title has NO promotional suffix. If multiple
    (or none) qualify, fall back to the lowest post ID.
    """
    clean_candidates = [p for p in posts if not has_promo_suffix(p["post_title"])]
    pool = clean_candidates if clean_candidates else posts
    return min(pool, key=lambda p: p["ID"])


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
        print("EXECUTE MODE -- this will actually delete duplicate posts.")
    else:
        print("DRY RUN -- showing what WOULD be deleted, nothing will be written.")
        print("Re-run with --execute when you're ready to apply.")
    print("=" * 70)

    conn = get_conn()
    cur = conn.cursor()

    groups = find_duplicate_groups(cur)
    print(f"\nFound {len(groups)} (normalized title, start_date) pairs with "
          f"duplicate posts (matched after stripping promotional suffixes).\n")

    if not groups:
        print("Nothing to do -- no normalized-title duplicates found.")
        conn.close()
        return

    total_to_delete = 0
    plan = []  # (keep_id, [delete_ids])
    for g in groups:
        keeper = choose_keeper(g["posts"])
        remove = [p for p in g["posts"] if p["ID"] != keeper["ID"]]
        total_to_delete += len(remove)
        plan.append((keeper["ID"], [p["ID"] for p in remove]))

        print(f"  '{g['norm_title'][:60]}' @ {g['start_date']}")
        for p in g["posts"]:
            marker = "KEEP " if p["ID"] == keeper["ID"] else "DELETE"
            print(f"    [{marker}] ID {p['ID']:>7}  {p['post_title']!r}")

    print(f"\nTotal posts that would be deleted: {total_to_delete}")

    if not execute:
        print("\nDRY RUN -- nothing deleted. Re-run with --execute to apply.")
        conn.close()
        return

    deleted = 0
    for keep_id, delete_ids in plan:
        for post_id in delete_ids:
            delete_post_completely(cur, post_id)
            deleted += 1
        conn.commit()

    print(f"\nDone. Deleted {deleted} duplicate posts (kept the non-suffixed "
          f"title where one existed, otherwise the lowest-ID post, in each group).")
    conn.close()


if __name__ == "__main__":
    main()
