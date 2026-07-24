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
import queue
import threading
from datetime import timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.cron import CronTrigger

from config import TICKETMASTER_INTERVAL, SCRAPER_INTERVAL, load_cities, load_dynamic_sources, update_source_stats

log = logging.getLogger("openclaw.scheduler")

# Module-level state so the refresh job can update it
_cities  = []
_sources = {}   # { city_slug: [source_dict, ...] }

# Spacing (seconds) between each job's forced initial next_run_time, so ~75
# jobs don't all fire in the same instant -- see the staggering comment in
# start_scheduler() for why an unstaggered pile-up here recreates itself
# every future cycle, not just on startup.
TM_STAGGER_SECONDS      = 15
SCRAPER_STAGGER_SECONDS = 8

# Events awaiting an Ollama-generated description, drained by a single
# dedicated background thread (see _enrichment_worker) instead of being
# generated inline on a scraper job's own pool thread. Ollama only
# meaningfully processes ~1 generation request at a time, so scraper jobs
# that called it inline were tying up a pool worker for the full duration
# of every serial Ollama call they needed -- confirmed 2026-07-23: all 25
# pool workers were simultaneously occupied at the exact moment 11
# Nashville jobs missed their dispatch window, because earlier-staggered
# jobs elsewhere in the cycle were each sitting on a worker for 20-40+
# minutes waiting on Ollama. Queueing enrichment separately means
# insert_event() finishes in seconds again regardless of how deep the
# enrichment backlog gets, so pool workers free up promptly and later
# jobs in the same cycle still get dispatched on time.
_enrichment_queue = queue.Queue()


def start_scheduler() -> BackgroundScheduler:
    """Build and start the APScheduler. Returns the running scheduler."""
    global _cities, _sources
    _cities  = load_cities()
    _sources = load_dynamic_sources()

    # Default executor is a 10-worker pool. All ~75 TM + scraper jobs fire
    # simultaneously every cycle (see the on-startup next_run_time reset
    # below), and 10 workers can't drain that backlog before misfire_grace_
    # time expires -- confirmed 2026-07-22: Larimer Lounge, Colorado
    # Railroad Museum, and Highlands Ranch Mansion were silently dropped
    # every single cycle for 5+ days because they consistently landed in a
    # queue position the pool didn't reach in time. 25 workers gives enough
    # headroom to drain the full job set well inside the grace window.
    scheduler = BackgroundScheduler(
        timezone="UTC",
        executors={"default": ThreadPoolExecutor(25)},
    )

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
            # than 1s to reach each one. Bumped 300 -> 900 on 2026-07-22
            # after the 10-worker pool (now 25, see start_scheduler) still
            # wasn't draining the full ~75-job backlog inside 300s, which
            # was silently dropping the same few jobs every cycle. 900s of
            # slack costs nothing (a TM pull running up to 15 minutes
            # "late" is irrelevant at a 1-hour interval) and gives real
            # margin on top of the larger pool.
            misfire_grace_time=900,
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
                misfire_grace_time=900,
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
        misfire_grace_time=900,
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

    # Fire Ticketmaster and scrapers on startup, staggered rather than all
    # at once. Forcing every job's next_run_time to the identical instant
    # recreates the exact thundering-herd/Ollama-contention pile-up the
    # pool-size and misfire_grace_time changes above were compensating for
    # -- and since IntervalTrigger just keeps adding its own interval to
    # whatever anchor it's given, an unstaggered pile-up here resyncs every
    # job back onto the same instant for every future cycle too, not just
    # this one startup. Spreading the initial fire by a few seconds per job
    # keeps that spread permanently without changing any job's own cadence.
    now = _now()
    for i, city in enumerate(_cities):
        scheduler.get_job(f"tm_{city['slug']}").modify(
            next_run_time=now + timedelta(seconds=i * TM_STAGGER_SECONDS)
        )
    i = 0
    for city_slug, source_list in _sources.items():
        for source in source_list:
            job_id = f"scraper_{city_slug}_{source['_db_id']}"
            job = scheduler.get_job(job_id)
            if job:
                job.modify(next_run_time=now + timedelta(seconds=i * SCRAPER_STAGGER_SECONDS))
                i += 1

    threading.Thread(target=_enrichment_worker, name="enrichment-worker", daemon=True).start()

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
    from db import insert_event

    try:
        events   = scrape_source(source, city)
        inserted = skipped = 0
        for event in events:
            if insert_event(event, city):
                inserted += 1
            else:
                skipped += 1
            if not (event.get("description") or "").strip() or event.get("_needs_enrichment"):
                _enrichment_queue.put((event, city))
        log.info("Scraper '%s' %s: %d inserted, %d skipped",
                 source["name"], city["name"], inserted, skipped)

        db_id = source.get("_db_id")
        if db_id:
            update_source_stats(db_id, inserted)

    except Exception as e:
        log.error("Scraper failed for %s/%s: %s",
                  source.get("name"), city.get("name"), e, exc_info=True)


def _enrichment_worker():
    """
    Runs continuously in its own dedicated thread for the life of the
    process, one event at a time -- matching Ollama's real throughput
    instead of however many scraper jobs happen to be concurrently
    dispatched. See the _enrichment_queue comment above for why this is
    decoupled from the scraper thread pool at all.

    Writes the generated description with a minimal, targeted UPDATE --
    deliberately NOT full update_event(). First version of this called
    update_event(), which redundantly re-runs _apply_categories(),
    _apply_image(), and _write_tec_index() on every backfill; those do
    DELETE+INSERT churn on term_relationships/postmeta, and because this
    worker's write now happens asynchronously and LATER than the
    original scrape (rather than inline in the same writer, like before
    this queue existed), it frequently landed at the same moment as that
    same post's own next 2-hour scrape cycle also calling update_event()
    -- two independent writers racing on the same rows. Confirmed
    2026-07-24: deadlock rate went from ~0.27/hour to ~220/hour within
    hours of deploying the queued version. Categories/image/TEC-index
    are already handled correctly by the original insert/update call;
    this only ever needs to fill in post_content.
    """
    from datetime import datetime
    from config import WP_PREFIX
    from db import get_connection, resolve_event_times, make_fingerprint, get_fingerprint_post_id
    from enricher import _generate

    while True:
        event, city_config = _enrichment_queue.get()
        try:
            desc = _generate(event)
            if not desc:
                continue
            enriched = {**event, "description": desc}
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            t   = resolve_event_times(enriched, now)
            fp  = make_fingerprint(enriched, resolved_times=t)

            conn = get_connection()
            try:
                post_id = get_fingerprint_post_id(conn, fp)
                if post_id:
                    with conn.cursor() as cur:
                        cur.execute(
                            f"UPDATE {WP_PREFIX}posts SET post_content=%s, post_modified=%s, post_modified_gmt=%s "
                            f"WHERE ID=%s AND (post_content='' OR post_content IS NULL)",
                            (desc, now, now, post_id)
                        )
                    conn.commit()
            finally:
                conn.close()
        except Exception:
            log.error("Enrichment worker failed for '%s'", event.get("title"), exc_info=True)
        finally:
            _enrichment_queue.task_done()


def _run_dupe_merge():
    """
    Daily safety-net job -- finds and merges the same high-confidence
    fuzzy duplicate pairs merge_fuzzy_dupes.py's CLI would find, using
    the exact same find_pairs()/plan_merge()/apply_merge()/merge_all()
    functions (imported directly, not shelled out to) so this can never
    drift out of sync with the manually-run version. Deliberately does
    NOT call merge_fuzzy_dupes.main() -- that function does its own
    argparse.parse_args(), which would try to parse this daemon's own
    argv instead of a real --apply flag.

    Uses merge_all() rather than looping over pairs directly so a 3+-way
    duplicate cluster (multiple pairs that don't agree on one primary)
    can't leave a later pair updating/merging into a post an earlier
    pair in this same run already deleted -- see merge_all()'s docstring.
    """
    from db import get_connection
    from merge_fuzzy_dupes import find_pairs, merge_all

    def _log_errors_only(line):
        if line.startswith("        ERROR"):
            log.error("Dupe merge: %s", line.strip())

    conn = get_connection()
    try:
        pairs = find_pairs(conn)
        _, merged, skipped_stale = merge_all(conn, pairs, apply=True, log_fn=_log_errors_only)
        log.info("Dupe merge: %d/%d duplicate pairs merged (%d stale-skipped)",
                  merged, len(pairs), skipped_stale)
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

    # New jobs get staggered next_run_times too (see start_scheduler) --
    # otherwise a refresh that picks up several new sources at once fires
    # all of them the instant they're added.
    now = _now()

    # Add any new Ticketmaster jobs
    tm_added = 0
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
                misfire_grace_time=900,
            )
            scheduler.get_job(job_id).modify(
                next_run_time=now + timedelta(seconds=tm_added * TM_STAGGER_SECONDS)
            )
            tm_added += 1
            log.info("Added new Ticketmaster job for %s", city["name"])

    # Add any new scraper jobs
    scraper_added = 0
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
                    misfire_grace_time=900,
                )
                scheduler.get_job(job_id).modify(
                    next_run_time=now + timedelta(seconds=scraper_added * SCRAPER_STAGGER_SECONDS)
                )
                scraper_added += 1
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
