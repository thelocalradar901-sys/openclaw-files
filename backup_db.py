"""
backup_db.py — dump the wordpress DB to a timestamped .sql file before
running the orphaned-postmeta cleanup.

Run directly on the server:
    cd /opt/openclaw && venv/bin/python3 backup_db.py

Writes to /opt/openclaw/backups/wordpress_<timestamp>.sql
No arguments needed. Self-loads /etc/openclaw/openclaw.env if the DB
password isn't already in the environment (same trick as diagnose_bloat.py).
"""

import os
import subprocess
import sys
from datetime import datetime


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


def main():
    _load_env_file_if_needed()

    host = os.getenv("WP_DB_HOST", "localhost")
    port = os.getenv("WP_DB_PORT", "3306")
    user = os.getenv("WP_DB_USER", "wpuser")
    password = os.getenv("WP_DB_PASSWORD", "")
    database = os.getenv("WP_DB_NAME", "wordpress")

    if not password:
        print("ERROR: WP_DB_PASSWORD is empty — refusing to run mysqldump "
              "with no password. Check /etc/openclaw/openclaw.env.")
        sys.exit(1)

    backup_dir = "/opt/openclaw/backups"
    os.makedirs(backup_dir, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = os.path.join(backup_dir, f"wordpress_{timestamp}.sql")

    cmd = [
        "mysqldump",
        f"--host={host}",
        f"--port={port}",
        f"--user={user}",
        f"--password={password}",
        "--single-transaction",   # consistent snapshot without locking tables
        "--quick",                # stream rows instead of buffering — important at this scale
        "--routines",
        "--triggers",
        database,
    ]

    print(f"Dumping '{database}' to {out_path} ...")
    print("(this can take a few minutes given the postmeta row count — let it run)")

    with open(out_path, "wb") as out_file:
        result = subprocess.run(cmd, stdout=out_file, stderr=subprocess.PIPE)

    if result.returncode != 0:
        print("mysqldump FAILED:")
        print(result.stderr.decode(errors="replace"))
        if os.path.exists(out_path):
            os.remove(out_path)
        sys.exit(1)

    size_mb = os.path.getsize(out_path) / (1024 * 1024)
    print(f"\nBackup complete: {out_path} ({size_mb:.1f} MB)")
    print("Verify it's non-empty and looks sane before proceeding with cleanup:")
    print(f"  ls -la {out_path}")
    print(f"  head -50 {out_path}")


if __name__ == "__main__":
    main()
