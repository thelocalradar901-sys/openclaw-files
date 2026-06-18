import sys, os, pymysql

with open("/etc/openclaw/openclaw.env") as f:
    for line in f:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

conn = pymysql.connect(
    host=os.environ["WP_DB_HOST"],
    user=os.environ["WP_DB_USER"],
    password=os.environ["WP_DB_PASSWORD"],
    database=os.environ["WP_DB_NAME"],
    charset="utf8mb4",
)
cur = conn.cursor()
cur.execute("SELECT meta_key, meta_value FROM wp_postmeta WHERE post_id = 170222")
for row in cur.fetchall():
    print(f"{row[0]}: {row[1][:80]}")
conn.close()
