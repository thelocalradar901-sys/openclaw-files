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
