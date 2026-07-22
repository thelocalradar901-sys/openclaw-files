"""
source_scout.py — automated source discovery agent for OpenClaw.

Answers the two questions discover_sources.py alone doesn't: not just
"is this scrapeable" but "is this WORTH scraping." discover_sources.py
inserts any candidate with a detectable feed straight into probation
sight-unseen; every candidate here gets a real trial scrape and a scored
verdict BEFORE it's ever written to wp_openclaw_sources, so nothing
lands in probation that a human would look at and immediately reject.

Pipeline per candidate:
  1. DISCOVER  — same Google Places search + feed-probing
                 discover_sources.py already does (reused directly,
                 not reimplemented).
  2. TRIAL SCRAPE — actually runs scrape_source() against the candidate
                 right now, same as vet_probation_sources.py does for
                 EXISTING probation sources — just done a step earlier,
                 before insertion, not after.
  3. SCORE     — event count, calendar freshness (is anything upcoming),
                 image completeness, on-topic keyword check, and a
                 fuzzy check against venue names already covered by an
                 existing source in the same city (skip if this is
                 probably an aggregator page for a venue that already
                 has its own direct source — direct-venue sources
                 already outrank aggregators throughout this codebase,
                 e.g. merge_fuzzy_dupes.py's affiliate-rank logic; no
                 reason to let a redundant aggregator source in at the
                 discovery stage when the same preference already
                 governs everything downstream).
  4. WRITE     — only WORTH_IT candidates are inserted, into
                 'probation' (never 'active' — same promote/reject
                 lifecycle vet_probation_sources.py already owns).
                 REVIEW candidates are logged but NOT inserted, so a
                 borderline call doesn't silently become a live source;
                 SKIP candidates are logged and dropped.

Run with no flags = dry run (prints the full scored report, writes
nothing). Run with --apply to actually insert WORTH_IT sources.

    venv/bin/python3 source_scout.py                # dry run
    venv/bin/python3 source_scout.py --apply         # write + summary
    venv/bin/python3 source_scout.py --city denver   # one city at a time
"""

import argparse
import difflib
import logging
import re
import sys
import time
from urllib.parse import urlparse

sys.path.insert(0, "/opt/openclaw")

from config import load_cities, _get_conn as get_connection
from scraper import scrape_source
from discover_sources import (
    places_search, probe_for_feed, existing_domains,
    PLACES_TYPE_QUERIES, SEARCH_RADIUS_METERS, WP_PREFIX,
)

log = logging.getLogger("openclaw.source_scout")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# ── Scoring thresholds ──────────────────────────────────────────────────────
MIN_EVENTS_FOR_TRIAL       = 3    # below this, not worth a scrape slot at all
MIN_UPCOMING_EVENTS        = 1    # at least one event actually in the future
MIN_IMAGE_RATE             = 0.30 # fraction of trial events with a real image
VENUE_NAME_DUPLICATE_RATIO = 0.85 # difflib ratio against an existing source's name

# Off-topic keyword denylist. Deliberately narrow and conservative --
# false-negatives (an off-topic source slipping through) get caught
# later by vet_probation_sources.py's human review anyway; a false-
# positive here silently throws away a legitimate venue before a human
# ever sees it, which is the worse failure mode. Only flags a candidate
# when MOST of its trial titles look like this, never on a single hit
# (a museum can host one private-rental "corporate event" and still be
# a perfectly good public events source).
_OFF_TOPIC_RE = re.compile(
    r"\b(staff meeting|board meeting|private rental|closed for private event|"
    r"employee training|hoa meeting|city council meeting)\b",
    re.IGNORECASE,
)
OFF_TOPIC_FRACTION_THRESHOLD = 0.5

FRESHNESS_LOOKAHEAD_DAYS = 90  # candidate must have something within this window


def _existing_venue_names(conn, city_slug: str) -> list[str]:
    cur = conn.cursor()
    cur.execute(
        f"SELECT name FROM {WP_PREFIX}openclaw_sources WHERE city_slug = %s",
        (city_slug,),
    )
    rows = cur.fetchall()
    cur.close()
    # Works whether rows are dicts (DictCursor) or plain tuples.
    return [r["name"] if isinstance(r, dict) else r[0] for r in rows if r]


def _likely_duplicate_venue(candidate_name: str, existing_names: list[str]) -> str | None:
    """
    Returns the matching existing source name if candidate_name is a
    close fuzzy match to a venue already covered, else None. Catches
    the case where Places surfaces an aggregator's own page for a venue
    ("Nashville Live Music Guide: The Ryman") that's really just a
    re-listing of a venue that already has its own direct source.
    """
    candidate_norm = candidate_name.lower().strip()
    for existing in existing_names:
        ratio = difflib.SequenceMatcher(None, candidate_norm, existing.lower().strip()).ratio()
        if ratio >= VENUE_NAME_DUPLICATE_RATIO:
            return existing
    return None


def score_candidate(events: list[dict], candidate_name: str, existing_names: list[str]) -> dict:
    """
    Runs every check and returns a verdict dict:
      {"verdict": "WORTH_IT" | "REVIEW" | "SKIP", "reasons": [...], "stats": {...}}
    First disqualifying check short-circuits with SKIP; anything that
    clears every hard check but looks borderline lands in REVIEW instead
    of being auto-approved.
    """
    reasons = []
    count = len(events)
    stats = {"event_count": count}

    dup = _likely_duplicate_venue(candidate_name, existing_names)
    if dup:
        return {
            "verdict": "SKIP", "stats": stats,
            "reasons": [f"Likely duplicate of existing source '{dup}' (name similarity)"],
        }

    if count < MIN_EVENTS_FOR_TRIAL:
        return {
            "verdict": "SKIP", "stats": stats,
            "reasons": [f"Trial scrape returned only {count} events (min {MIN_EVENTS_FOR_TRIAL})"],
        }

    today = time.strftime("%Y-%m-%d")
    upcoming = [e for e in events if (e.get("start_date") or "")[:10] >= today]
    stats["upcoming_count"] = len(upcoming)
    if len(upcoming) < MIN_UPCOMING_EVENTS:
        return {
            "verdict": "SKIP", "stats": stats,
            "reasons": ["No upcoming events -- calendar looks stale/dead"],
        }

    with_image = sum(1 for e in events if e.get("image_url"))
    image_rate = with_image / count
    stats["image_rate"] = round(image_rate, 2)
    if image_rate < MIN_IMAGE_RATE:
        reasons.append(f"Low image coverage ({image_rate:.0%} of trial events)")

    off_topic = sum(1 for e in events if _OFF_TOPIC_RE.search(e.get("title") or ""))
    off_topic_fraction = off_topic / count
    stats["off_topic_fraction"] = round(off_topic_fraction, 2)
    if off_topic_fraction >= OFF_TOPIC_FRACTION_THRESHOLD:
        return {
            "verdict": "SKIP", "stats": stats,
            "reasons": [f"{off_topic_fraction:.0%} of trial titles look off-topic (private/internal events)"],
        }

    if reasons:
        return {"verdict": "REVIEW", "stats": stats, "reasons": reasons}

    return {"verdict": "WORTH_IT", "stats": stats, "reasons": ["Clean pass on all checks"]}


def scout_city(city: dict, conn, dry_run: bool) -> dict:
    """Returns {"worth_it": [...], "review": [...], "skip": [...]}"""
    results = {"worth_it": [], "review": [], "skip": []}
    known_domains = existing_domains(conn)
    existing_names = _existing_venue_names(conn, city["slug"])

    for category, included_types in PLACES_TYPE_QUERIES.items():
        places = places_search(included_types, city["lat"], city["lng"], SEARCH_RADIUS_METERS)
        log.info("'%s' near %s: %d results with a website", category, city["name"], len(places))

        for place in places:
            site_url, name = place.get("url"), place.get("name")
            if not site_url or not name:
                continue

            domain = urlparse(site_url).netloc.lower().lstrip("www.")
            if not domain or domain in known_domains:
                continue
            known_domains.add(domain)

            feed = probe_for_feed(site_url)
            time.sleep(0.5)
            if not feed:
                continue  # not scrapeable at all -- not even a candidate

            source = {
                "name": name, "url": feed["url"],
                "source_type": feed["source_type"], "city_slug": city["slug"],
            }
            try:
                events = scrape_source(source, city)
            except Exception as e:
                log.warning("Trial scrape crashed for %s: %s", name, e)
                events = []

            verdict = score_candidate(events, name, existing_names)
            entry = {**source, "category": category, **verdict}

            if verdict["verdict"] == "WORTH_IT":
                results["worth_it"].append(entry)
                existing_names.append(name)  # avoid a second near-duplicate this same run
                log.info("WORTH_IT: %s (%s) -- %s", name, feed["source_type"], verdict["reasons"][0])
            elif verdict["verdict"] == "REVIEW":
                results["review"].append(entry)
                log.info("REVIEW: %s -- %s", name, "; ".join(verdict["reasons"]))
            else:
                results["skip"].append(entry)
                log.info("SKIP: %s -- %s", name, "; ".join(verdict["reasons"]))

    if results["worth_it"] and not dry_run:
        cur = conn.cursor()
        for f in results["worth_it"]:
            note = (
                f"Auto-discovered via source_scout.py ({f['category']}). "
                f"Trial scrape: {f['stats']}"
            )
            cur.execute(
                f"INSERT INTO {WP_PREFIX}openclaw_sources "
                f"(city_slug, name, url, source_type, status, notes, browser_ua) "
                f"VALUES (%s, %s, %s, %s, 'probation', %s, 0)",
                (f["city_slug"], f["name"], f["url"], f["source_type"], note),
            )
        conn.commit()
        cur.close()

    return results


def _print_report(city_name: str, results: dict):
    print(f"\n=== {city_name} ===")
    print(f"  WORTH_IT: {len(results['worth_it'])}   "
          f"REVIEW: {len(results['review'])}   SKIP: {len(results['skip'])}")
    for f in results["worth_it"]:
        print(f"  [WORTH_IT] {f['name']} ({f['source_type']}) -> {f['url']}")
        print(f"             stats={f['stats']}")
    for f in results["review"]:
        print(f"  [REVIEW]   {f['name']} -- {'; '.join(f['reasons'])}")
    for f in results["skip"]:
        print(f"  [SKIP]     {f['name']} -- {'; '.join(f['reasons'])}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                     help="Actually insert WORTH_IT sources. Default is dry-run.")
    ap.add_argument("--city", help="Limit to one city slug.")
    args = ap.parse_args()
    dry_run = not args.apply

    conn = get_connection()
    try:
        cities = load_cities()
        if args.city:
            cities = [c for c in cities if c["slug"] == args.city]

        totals = {"worth_it": 0, "review": 0, "skip": 0}
        for city in cities:
            log.info("=== Scouting %s ===", city["name"])
            results = scout_city(city, conn, dry_run)
            _print_report(city["name"], results)
            for k in totals:
                totals[k] += len(results[k])

        print(f"\nTOTALS -- WORTH_IT: {totals['worth_it']}  "
              f"REVIEW: {totals['review']} (not inserted)  "
              f"SKIP: {totals['skip']} (not inserted)")

        if dry_run:
            print("\nDRY RUN -- nothing written to DB. Re-run with --apply.")
        else:
            print(f"\nInserted {totals['worth_it']} new probation sources. "
                  f"REVIEW candidates were logged but NOT inserted -- "
                  f"check the log/report above if you want to add any by hand.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
