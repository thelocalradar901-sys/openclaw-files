import pymysql

conn = pymysql.connect(
    host='localhost', user='wpuser', password='wpDB_pass789',
    database='wordpress', charset='utf8mb4'
)

with conn.cursor() as cur:
    # Get all TM events without a ticket URL
    cur.execute("""
        SELECT p.ID, pm.meta_value
        FROM wp_posts p
        JOIN wp_postmeta pm ON p.ID = pm.post_id
        WHERE p.post_type = 'tribe_events'
        AND p.post_status = 'publish'
        AND pm.meta_key = '_openclaw_external_id'
        AND pm.meta_value LIKE 'tm_%'
        AND p.ID NOT IN (
            SELECT post_id FROM wp_postmeta
            WHERE meta_key = '_EventURL' AND meta_value != ''
        )
    """)
    rows = cur.fetchall()
    print(f"Found {len(rows)} TM events without ticket URL")

    count = 0
    for post_id, external_id in rows:
        tm_id = external_id[3:]  # strip 'tm_'
        url = f"https://www.ticketmaster.com/event/{tm_id}?aaid=7097599"
        cur.execute(
            "INSERT INTO wp_postmeta (post_id, meta_key, meta_value) VALUES (%s, '_EventURL', %s)",
            (post_id, url)
        )
        count += 1

conn.commit()
conn.close()
print(f"Done — set ticket URLs on {count} events")
