"""
scheduler.py — APScheduler job coordinator

Jobs:
  - Ticketmaster: one per city, every TICKETMASTER_INTERVAL seconds (default 1h)
  - Scraper:      one per source, every SCRAPER_INTERVAL seconds (default 2h)
  - DB refresh:   every hour — picks up new cities/sources without daemon restart
  - Discovery:    one per city, every DISCOVERY_INTERVAL seconds (default 7 days).
                  Finds new candidate event sources via OSM Overpass and adds
                  them to wp_openclaw_sources as status='probation'. Does NOT
                  fire immediately on startup (see _fire_all) since a single
                  run can take ~30-40 minutes -- we don't want every daemon
                  restart kicking off a long discovery run.

All jobs run immediately on startup, then on their interval -- EXCEPT
discovery jobs, which only ever run on their normal weekly interval.
"""

import logging
from datetime import datetime, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from config import (
    TICKETMASTER_INTERVAL, SCRAPER_INTERVAL, EVENTIM_INTERVAL,
    load_cities, load_dynamic_sources, update_source_stats,
)

DISCOVERY_INTERVAL = 7 * 24 * 3600  # weekly

log = logging.getLogger("openclaw.scheduler")

_cities  = []
_sources = {}


def start_scheduler() -> BackgroundScheduler:
    global _cities, _sources
    _cities  = load_cities()
    _sources = load_dynamic_sources()

    scheduler = BackgroundScheduler(timezone="UTC")

    # Ticketmaster jobs
    for city in _cities:
        scheduler.add_job(
            _run_ticketmaster,
            trigger=IntervalTrigger(seconds=TICKETMASTER_INTERVAL),
            args=[city],
            id=f"tm_{city['slug']}",
            name=f"Ticketmaster – {city['name']}",
            replace_existing=True,
            max_instances=1,
        )

    # Eventim/See Tickets Affiliate job -- ONE job total, not one per city.
    # Unlike TM, Eventim's API returns the whole national feed in a single
    # call regardless of city, so it's registered once here and handles
    # all 4 cities internally via pull_all_cities().
    scheduler.add_job(
        _run_eventim,
        trigger=IntervalTrigger(seconds=EVENTIM_INTERVAL),
        args=[],
        id="eventim",
        name="Eventim/See Tickets Affiliate",
        replace_existing=True,
        max_instances=1,
    )

    # Scraper jobs
    for city_slug, sources in _sources.items():
        for source in sources:
            jid = f"scraper_{city_slug}_{source['_db_id']}"
            scheduler.add_job(
                _run_scraper,
                trigger=IntervalTrigger(seconds=SCRAPER_INTERVAL),
                args=[source, _city_dict(city_slug)],
                id=jid,
                name=f"{source['name']} / {city_slug}",
                replace_existing=True,
                max_instances=1,
            )

    # Discovery jobs -- one per city, weekly. Intentionally NOT fired
    # immediately on startup (see _fire_all) since a single run takes
    # ~30-40 minutes against the public OSM Overpass mirror.
    for city in _cities:
        scheduler.add_job(
            _run_discovery,
            trigger=IntervalTrigger(seconds=DISCOVERY_INTERVAL),
            args=[city],
            id=f"discovery_{city['slug']}",
            name=f"Source Discovery – {city['name']}",
            replace_existing=True,
            max_instances=1,
        )

    # DB refresh
    scheduler.add_job(
        _refresh,
        trigger=IntervalTrigger(seconds=3600),
        args=[scheduler],
        id="db_refresh",
        name="Refresh cities + sources",
        replace_existing=True,
    )

    scheduler.start()

    # Fire everything immediately
    _fire_all(scheduler)

    log.info("Scheduler started — %d cities, %d sources",
             len(_cities), sum(len(v) for v in _sources.values()))
    return scheduler


def _fire_all(scheduler: BackgroundScheduler):
    now = datetime.now(timezone.utc)
    for job in scheduler.get_jobs():
        if job.id == "db_refresh" or job.id.startswith("discovery_"):
            continue
        try:
            job.modify(next_run_time=now)
        except Exception:
            pass


def _run_discovery(city: dict):
    from discover_sources import discover_for_city, send_summary_email
    from config import _get_conn
    conn = _get_conn()
    try:
        found = discover_for_city(city, conn, dry_run=False)
        log.info("Discovery %s: %d new probation sources added", city["name"], len(found))
        if found:
            send_summary_email(found)
    except Exception as e:
        log.error("Discovery job failed for %s: %s", city["name"], e, exc_info=True)
    finally:
        conn.close()


def _run_ticketmaster(city: dict):
    from ticketmaster import pull_city
    from db import insert_event
    try:
        events   = pull_city(city)
        inserted = skipped = 0
        for ev in events:
            if insert_event(ev, city):
                inserted += 1
            else:
                skipped += 1
        log.info("TM %s: %d inserted, %d skipped", city["name"], inserted, skipped)
    except Exception as e:
        log.error("TM job failed for %s: %s", city["name"], e, exc_info=True)


def _run_eventim():
    from eventim import pull_all_cities
    from db import insert_event
    try:
        events_by_city = pull_all_cities()
        total_inserted = total_skipped = 0
        for city_slug, events in events_by_city.items():
            city = _city_dict(city_slug)
            inserted = skipped = 0
            for ev in events:
                if insert_event(ev, city):
                    inserted += 1
                else:
                    skipped += 1
            log.info("Eventim %s: %d inserted, %d skipped", city["name"], inserted, skipped)
            total_inserted += inserted
            total_skipped += skipped
        log.info("Eventim total: %d inserted, %d skipped", total_inserted, total_skipped)
    except Exception as e:
        log.error("Eventim job failed: %s", e, exc_info=True)


def _run_scraper(source: dict, city: dict):
    from scraper import scrape_source
    from enricher import enrich_events
    from db import insert_event
    try:
        events   = scrape_source(source, city)
        events   = enrich_events(events)
        inserted = skipped = 0
        for ev in events:
            if insert_event(ev, city):
                inserted += 1
            else:
                skipped += 1
        log.info("Scraper '%s' %s: %d inserted, %d skipped",
                 source["name"], city["name"], inserted, skipped)
        if source.get("_db_id"):
            update_source_stats(source["_db_id"], inserted)
    except Exception as e:
        log.error("Scraper failed %s/%s: %s",
                  source.get("name"), city.get("name"), e, exc_info=True)


def _refresh(scheduler: BackgroundScheduler):
    global _cities, _sources
    log.info("Refreshing cities and sources from DB")
    try:
        new_cities  = load_cities()
        new_sources = load_dynamic_sources()
    except Exception as e:
        log.error("Refresh failed: %s", e)
        return

    _cities  = new_cities
    _sources = new_sources

    now = datetime.now(timezone.utc)

    for city in _cities:
        jid = f"tm_{city['slug']}"
        if not scheduler.get_job(jid):
            scheduler.add_job(
                _run_ticketmaster,
                trigger=IntervalTrigger(seconds=TICKETMASTER_INTERVAL),
                args=[city], id=jid,
                name=f"Ticketmaster – {city['name']}",
                replace_existing=True, max_instances=1,
            )
            scheduler.get_job(jid).modify(next_run_time=now)
            log.info("Added TM job for new city: %s", city["name"])

        disc_jid = f"discovery_{city['slug']}"
        if not scheduler.get_job(disc_jid):
            scheduler.add_job(
                _run_discovery,
                trigger=IntervalTrigger(seconds=DISCOVERY_INTERVAL),
                args=[city], id=disc_jid,
                name=f"Source Discovery – {city['name']}",
                replace_existing=True, max_instances=1,
            )
            # Deliberately NOT firing immediately -- a single discovery
            # run takes ~30-40 min against the public OSM Overpass
            # mirror. New cities wait for their normal weekly slot,
            # same as every other city.
            log.info("Added Discovery job for new city: %s (first run in ~%dh)",
                      city["name"], DISCOVERY_INTERVAL // 3600)

    for city_slug, sources in _sources.items():
        for source in sources:
            jid = f"scraper_{city_slug}_{source['_db_id']}"
            if not scheduler.get_job(jid):
                scheduler.add_job(
                    _run_scraper,
                    trigger=IntervalTrigger(seconds=SCRAPER_INTERVAL),
                    args=[source, _city_dict(city_slug)],
                    id=jid, name=f"{source['name']} / {city_slug}",
                    replace_existing=True, max_instances=1,
                )
                scheduler.get_job(jid).modify(next_run_time=now)
                log.info("Added scraper job: %s / %s", source["name"], city_slug)

    log.info("Refresh done — %d cities, %d sources",
             len(_cities), sum(len(v) for v in _sources.values()))


def _city_dict(slug: str) -> dict:
    for c in _cities:
        if c["slug"] == slug:
            return c
    return {"slug": slug, "name": slug.title(), "lat": 0, "lng": 0, "timezone": "America/Chicago"}
