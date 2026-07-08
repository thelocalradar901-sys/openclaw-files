"""
scheduler.py – OpenClaw APScheduler job coordinator

Jobs:
  - Ticketmaster pull: every TICKETMASTER_INTERVAL seconds (default 1h), one job per city
  - Scraper run:       every SCRAPER_INTERVAL seconds (default 2h), one job per source
  - City/source refresh: every 1h, reloads from DB so new cities/sources take effect
    without restarting the daemon

All jobs are fire-and-forget. Errors are caught and logged; the scheduler
continues regardless.
"""

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.cron import CronTrigger

from config import TICKETMASTER_INTERVAL, SCRAPER_INTERVAL, load_cities, load_dynamic_sources, update_source_stats

log = logging.getLogger("openclaw.scheduler")

# Module-level state so the refresh job can update it
_cities  = []
_sources = {}   # { city_slug: [source_dict, ...] }


def start_scheduler() -> BackgroundScheduler:
    """Build and start the APScheduler. Returns the running scheduler."""
    global _cities, _sources
    _cities  = load_cities()
    _sources = load_dynamic_sources()

    scheduler = BackgroundScheduler(timezone="UTC")

    # ── Ticketmaster jobs ─────────────────────────────────────────────────────
    for city in _cities:
        scheduler.add_job(
            _run_ticketmaster,
            trigger=IntervalTrigger(seconds=TICKETMASTER_INTERVAL),
            args=[city],
            id=f"tm_{city['slug']}",
            name=f"Ticketmaster - {city['name']}",
            replace_existing=True,
            max_instances=1,
            # APScheduler's default misfire_grace_time is 1 second -- if
            # more than 1s passes between a job's scheduled run time and
            # the executor actually getting to it, APScheduler silently
            # SKIPS the run entirely (logged only as a WARNING, easy to
            # miss). Confirmed 2026-07-08: every TM job for all 4 cities
            # missed its immediate on-startup fire this way -- with
            # dozens of jobs (4 TM + every scraper + Eventim) all queued
            # at once on restart, the thread pool routinely takes more
            # than 1s to reach each one. 300s of slack costs nothing
            # (a TM pull running up to 5 minutes "late" is irrelevant at
            # a 1-hour interval) and stops this class of silent no-op.
            misfire_grace_time=300,
        )
        log.info("Scheduled Ticketmaster job for %s (every %ds)", city["name"], TICKETMASTER_INTERVAL)

    # ── Scraper jobs ──────────────────────────────────────────────────────────
    for city_slug, source_list in _sources.items():
        for source in source_list:
            job_id = f"scraper_{city_slug}_{source['_db_id']}"
            scheduler.add_job(
                _run_scraper,
                trigger=IntervalTrigger(seconds=SCRAPER_INTERVAL),
                args=[source, _get_city(city_slug)],
                id=job_id,
                name=f"{source['name']}/{city_slug}",
                replace_existing=True,
                max_instances=1,
                # Same misfire reasoning as the TM jobs above -- see that
                # comment for the full explanation.
                misfire_grace_time=300,
            )
    log.info("Scheduled %d scraper jobs", sum(len(v) for v in _sources.values()))

    # ── Refresh job ───────────────────────────────────────────────────────────
    scheduler.add_job(
        _refresh_cities_and_sources,
        trigger=IntervalTrigger(seconds=3600),
        args=[scheduler],
        id="refresh_db",
        name="Refresh cities + sources from WP DB",
        replace_existing=True,
        misfire_grace_time=300,
    )

    # ── Fuzzy duplicate merge job ─────────────────────────────────────────────
    # Runs merge_fuzzy_dupes.py's logic once a day. The advisory lock added
    # to insert_event() (db.py, 2026-07-08) closes the specific race that
    # caused cross-source dupes to slip through, but it's not a hard
    # guarantee against every possible path to a duplicate (e.g. the lock
    # timing out under heavy load and insert_event() proceeding without
    # it) -- this is the automated safety net for whatever occasionally
    # gets through anyway. 9:00 UTC = ~3-4am Central, picked as an
    # off-peak hour so a burst of deletes doesn't coincide with a TM/
    # scraper cycle also writing to the same tables. --apply runs
    # unattended (no separate dry-run-then-review step) -- merge_fuzzy_
    # dupes.py already excludes the risky cases (fixture titles, numbered
    # multi-night occurrences, lineup/showcase shows) from auto-merge, so
    # this is scoped to the same high-confidence pairs a human reviewed
    # manually before turning this on.
    scheduler.add_job(
        _run_dupe_merge,
        trigger=CronTrigger(hour=9, minute=0),
        id="dupe_merge",
        name="Fuzzy duplicate merge (daily)",
        replace_existing=True,
        misfire_grace_time=3600,
    )

    scheduler.start()

    # Fire Ticketmaster and scrapers immediately on startup
    for city in _cities:
        scheduler.get_job(f"tm_{city['slug']}").modify(next_run_time=_now())
    for city_slug, source_list in _sources.items():
        for source in source_list:
            job_id = f"scraper_{city_slug}_{source['_db_id']}"
            job = scheduler.get_job(job_id)
            if job:
                job.modify(next_run_time=_now())

    log.info("Scheduler started with %d cities, %d sources",
             len(_cities), sum(len(v) for v in _sources.values()))
    return scheduler


# ── Job runners ───────────────────────────────────────────────────────────────

def _run_ticketmaster(city: dict):
    from ticketmaster import pull_city
    from db import insert_event

    try:
        events   = pull_city(city)
        inserted = skipped = 0
        for event in events:
            if insert_event(event, city):
                inserted += 1
            else:
                skipped += 1
        log.info("Ticketmaster %s: %d inserted, %d skipped", city["name"], inserted, skipped)
    except Exception as e:
        log.error("Ticketmaster job failed for %s: %s", city["name"], e, exc_info=True)


def _run_scraper(source: dict, city: dict):
    from scraper import scrape_source
    from enricher import enrich_events
    from db import insert_event

    try:
        events   = scrape_source(source, city)
        events   = enrich_events(events)
        inserted = skipped = 0
        for event in events:
            if insert_event(event, city):
                inserted += 1
            else:
                skipped += 1
        log.info("Scraper '%s' %s: %d inserted, %d skipped",
                 source["name"], city["name"], inserted, skipped)

        db_id = source.get("_db_id")
        if db_id:
            update_source_stats(db_id, inserted)

    except Exception as e:
        log.error("Scraper failed for %s/%s: %s",
                  source.get("name"), city.get("name"), e, exc_info=True)


def _run_dupe_merge():
    """
    Daily safety-net job -- finds and merges the same high-confidence
    fuzzy duplicate pairs merge_fuzzy_dupes.py's CLI would find, using
    the exact same find_pairs()/plan_merge()/apply_merge() functions
    (imported directly, not shelled out to) so this can never drift out
    of sync with the manually-run version. Deliberately does NOT call
    merge_fuzzy_dupes.main() -- that function does its own
    argparse.parse_args(), which would try to parse this daemon's own
    argv instead of a real --apply flag.
    """
    from db import get_connection
    from merge_fuzzy_dupes import find_pairs, plan_merge, apply_merge

    conn = get_connection()
    try:
        pairs = find_pairs(conn)
        merged = 0
        for ratio, a, b in pairs:
            primary, secondary, updates = plan_merge(a, b)
            try:
                apply_merge(conn, primary, secondary, updates)
                merged += 1
            except Exception as e:
                log.error("Dupe merge failed for pair (%d, %d): %s",
                          a["post_id"], b["post_id"], e, exc_info=True)
        log.info("Dupe merge: %d/%d duplicate pairs merged", merged, len(pairs))
    except Exception as e:
        log.error("Dupe merge job failed: %s", e, exc_info=True)
    finally:
        conn.close()


def _refresh_cities_and_sources(scheduler: BackgroundScheduler):
    """Reload cities and sources from DB. Adds new jobs for anything not already scheduled."""
    global _cities, _sources
    log.info("Scheduler: Refreshing cities and sources from WP DB...")

    try:
        new_cities  = load_cities()
        new_sources = load_dynamic_sources()
    except Exception as e:
        log.error("Refresh failed: %s", e)
        return

    _cities  = new_cities
    _sources = new_sources

    # Add any new Ticketmaster jobs
    for city in _cities:
        job_id = f"tm_{city['slug']}"
        if not scheduler.get_job(job_id):
            scheduler.add_job(
                _run_ticketmaster,
                trigger=IntervalTrigger(seconds=TICKETMASTER_INTERVAL),
                args=[city],
                id=job_id,
                name=f"Ticketmaster - {city['name']}",
                replace_existing=True,
                max_instances=1,
                misfire_grace_time=300,
            )
            log.info("Added new Ticketmaster job for %s", city["name"])

    # Add any new scraper jobs
    for city_slug, source_list in _sources.items():
        for source in source_list:
            job_id = f"scraper_{city_slug}_{source['_db_id']}"
            if not scheduler.get_job(job_id):
                scheduler.add_job(
                    _run_scraper,
                    trigger=IntervalTrigger(seconds=SCRAPER_INTERVAL),
                    args=[source, _get_city(city_slug)],
                    id=job_id,
                    name=f"{source['name']}/{city_slug}",
                    replace_existing=True,
                    max_instances=1,
                    misfire_grace_time=300,
                )
                log.info("Added new scraper job: %s / %s", source["name"], city_slug)

    log.info("Refresh complete: %d cities, %d sources",
             len(_cities), sum(len(v) for v in _sources.values()))


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_city(slug: str) -> dict:
    """Return the city dict for a given slug, or a minimal fallback."""
    for c in _cities:
        if c["slug"] == slug:
            return c
    return {"slug": slug, "name": slug.title(), "lat": 0, "lng": 0}


def _now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc)
