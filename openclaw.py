#!/usr/bin/env python3
"""
openclaw.py – OpenClaw event aggregation daemon
Runs as a systemd service. Starts APScheduler, then sleeps.
"""

import logging
import signal
import sys
import time

from config import LOG_LEVEL

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s,%(msecs)03d [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("/var/log/openclaw/openclaw.log"),
    ],
)
log = logging.getLogger("openclaw")


def handle_shutdown(signum, frame):
    log.info("Shutdown signal received (%d). Exiting.", signum)
    sys.exit(0)


def main():
    signal.signal(signal.SIGTERM, handle_shutdown)
    signal.signal(signal.SIGINT,  handle_shutdown)

    log.info("OpenClaw starting up")

    from scheduler import start_scheduler
    scheduler = start_scheduler()

    try:
        while True:
            time.sleep(30)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown(wait=False)
        log.info("OpenClaw stopped.")


if __name__ == "__main__":
    main()
