"""
backfill_categories.py -- re-run the FIXED categorization logic against
every existing tribe_events post, correcting any mis-tagged events that
were categorized before the map_to_tlr_categories() fix went in.

For each post: rebuilds the same {title, description, categories} shape
map_to_tlr_categories() expects, gets the (now correct) single category,
and replaces whatever wp_term_relationships rows currently exist for the
TLR category taxonomy with just that one.

SAFETY:
  - Only touches the TLR category taxonomy's term relationships -- never
    touches tags, post_tag, or any other taxonomy a post might have.
  - Defaults to DRY RUN (prints what WOULD change, no writes).
    Pass --execute to actually apply changes.
  - Take a backup_db.py dump before running --execute.

Run directly on the server:
    cd /opt/openclaw && venv/bin/python3 backfill_categories.py           # dry run
    cd /opt/openclaw && venv/bin/python3 backfill_categories.py --execute # apply
"""

import json
import os
import sys

import pymysql
import pymysql.cursors

# Same taxonomy name TEC/your plugin suite uses for event categories.
# Adjust here if your install uses a different taxonomy slug.
CATEGORY_TAXONOMY = "tribe_events_cat"


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


def get_or_create_term_taxonomy_id(cur, slug, name, taxonomy):
    cur.execute(
        "SELECT tt.term_taxonomy_id FROM wp_terms t "
        "JOIN wp_term_taxonomy tt ON t.term_id=tt.term_id "
        "WHERE t.slug=%s AND tt.taxonomy=%s LIMIT 1",
        (slug, taxonomy)
    )
    row = cur.fetchone()
    if row:
        return row["term_taxonomy_id"]
    cur.execute(
        "INSERT INTO wp_terms (name, slug, term_group) VALUES (%s,%s,0)",
        (name, slug)
    )
    term_id = cur.lastrowid
    cur.execute(
        "INSERT INTO wp_term_taxonomy (term_id, taxonomy, description, parent, count) "
        "VALUES (%s,%s,'',0,0)",
        (term_id, taxonomy)
    )
    return cur.lastrowid


def main():
    execute = "--execute" in sys.argv

    print("=" * 70)
    if execute:
        print("EXECUTE MODE -- this will actually update category assignments.")
    else:
        print("DRY RUN -- showing what WOULD change, nothing will be written.")
        print("Re-run with --execute when you're ready to apply.")
    print("=" * 70)

    # Import the fixed logic directly from db.py rather than re-implementing
    # it here, so this script can never drift out of sync with the real
    # categorization rules.
    sys.path.insert(0, "/opt/openclaw")
    from db import map_to_tlr_categories
    from config import TLR_CATEGORIES

    slug_to_name = {slug: slug.replace("-", " ").title() for slug, _ in TLR_CATEGORIES}

    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        SELECT p.ID, p.post_title, p.post_content AS description,
               (SELECT meta_value FROM wp_postmeta
                WHERE post_id = p.ID AND meta_key = '_openclaw_raw_categories' LIMIT 1) AS raw_categories_json
        FROM wp_posts p
        WHERE p.post_type = 'tribe_events' AND p.post_status IN ('publish', 'draft')
    """)
    posts = cur.fetchall()
    print(f"\nFound {len(posts)} events to check.\n")

    changed_count = 0
    unchanged_count = 0
    error_count = 0

    for post in posts:
        pid = post["ID"]
        title = post["post_title"] or ""
        description = post.get("description") or ""

        raw_categories = []
        raw_categories_json = post.get("raw_categories_json")
        if raw_categories_json:
            try:
                raw_categories = json.loads(raw_categories_json)
            except Exception:
                raw_categories = []

        try:
            new_cats = map_to_tlr_categories({
                "title": title,
                "description": description,
                "categories": raw_categories,
            })
        except Exception as e:
            print(f"  ERROR computing category for post {pid} ({title!r}): {e}")
            error_count += 1
            continue

        # Current category assignment for this taxonomy only.
        cur.execute("""
            SELECT t.slug
            FROM wp_term_relationships tr
            JOIN wp_term_taxonomy tt ON tt.term_taxonomy_id = tr.term_taxonomy_id
            JOIN wp_terms t ON t.term_id = tt.term_id
            WHERE tr.object_id = %s AND tt.taxonomy = %s
        """, (pid, CATEGORY_TAXONOMY))
        current_cats = sorted(r["slug"] for r in cur.fetchall())

        if current_cats == sorted(new_cats):
            unchanged_count += 1
            continue

        changed_count += 1
        print(f"  Post {pid} ({title[:60]!r}): {current_cats} -> {new_cats}")

        if not execute:
            continue

        # Remove existing relationships for this taxonomy, then add the
        # new (single) category. Scoped to ONLY this taxonomy's term_ids
        # so tags/other taxonomies on the same post are never touched.
        cur.execute("""
            DELETE tr FROM wp_term_relationships tr
            JOIN wp_term_taxonomy tt ON tt.term_taxonomy_id = tr.term_taxonomy_id
            WHERE tr.object_id = %s AND tt.taxonomy = %s
        """, (pid, CATEGORY_TAXONOMY))

        for slug in new_cats:
            tt_id = get_or_create_term_taxonomy_id(cur, slug, slug_to_name.get(slug, slug), CATEGORY_TAXONOMY)
            cur.execute(
                "INSERT IGNORE INTO wp_term_relationships (object_id, term_taxonomy_id, term_order) "
                "VALUES (%s,%s,0)",
                (pid, tt_id)
            )

        conn.commit()

    print(f"\n{'=' * 70}")
    print(f"Checked:   {len(posts)}")
    print(f"Changed:   {changed_count}{'  (dry run -- not actually applied)' if not execute else ''}")
    print(f"Unchanged: {unchanged_count}")
    print(f"Errors:    {error_count}")

    conn.close()


if __name__ == "__main__":
    main()
