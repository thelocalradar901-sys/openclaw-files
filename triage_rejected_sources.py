"""
triage_rejected_sources.py — macro re-test + root-cause classifier for
OpenClaw sources

Run directly on the server:

    venv/bin/python3 triage_rejected_sources.py
    venv/bin/python3 triage_rejected_sources.py --status rejected,probation
    venv/bin/python3 triage_rejected_sources.py --limit 20        # test on a subset first
    venv/bin/python3 triage_rejected_sources.py --city Birmingham # one city at a time

WHY THIS EXISTS: after landing real fixes in scraper.py (multi-iCal
harvesting, the date-range parsing fix, the orphan-heading fallback),
the actual question worth asking isn't "what does this source's markup
look like" one at a time -- it's "does the CURRENT code already produce
events for this source, right now, across everything marked rejected/
probation, in one pass." That's what this script answers first.

TWO PASSES PER SOURCE:
  1. REAL RE-SCRAPE: runs the source through the actual current tier
     functions (_detect_ical, _detect_multi_ical, _parse_jsonld,
     _parse_rhp, _parse_heuristic, or the dedicated seetickets/tec_rest/
     ical_url/json_api function for non-html_auto types), respecting any
     browser_ua/html_relay/no_ical flags already configured in the
     source's notes field. This is the real production logic -- not a
     guess about it. Ollama (tier 4) is deliberately skipped here so a
     run across hundreds of sources stays bounded in time; anything that
     would only be caught by Ollama shows up as 0 in this pass and gets
     a root-cause hint from pass 2 instead.
  2. ROOT-CAUSE CLASSIFICATION: only for sources that are STILL at 0
     after pass 1. Buckets by no-URL, blocked-by-UA, blocked-by-ASN,
     JS-rendered-empty-shell, or genuinely unmatched markup -- same as
     before, so what's left over is a short, categorized punch list
     instead of "everything is still just rejected."

BATCH DIAGNOSTIC DUMP (the point of this section): every source landing
in markup_not_matched gets its raw fetched HTML saved to
/opt/openclaw/triage_html_dump/<city>__<name>.html, plus an expanded
diagnostic line covering known-selector hits, fallback-container counts,
JSON-LD script counts, AND multi-iCal link counts (even below the
min_links threshold, so a near-miss is visible instead of silently
looking identical to "found nothing at all"). This exists specifically
so that finding the next generalizable pattern doesn't require a
one-at-a-time "run the debug tool on this one URL" round trip -- run this
script once across a whole city (or everything), then hand over the
WHOLE dump directory in one shot:

    tar czf /tmp/triage_dump.tar.gz -C /opt/openclaw triage_html_dump
    (then upload /tmp/triage_dump.tar.gz)

That's what turns "investigate 19 sites one at a time" into "investigate
19 sites' real markup in one pass, looking for the two or three shared
patterns worth fixing generally."

Reads DB credentials straight out of /etc/openclaw/openclaw.env and
shells out to the `mysql` CLI -- no assumptions about which Python MySQL
driver (if any) is installed in the venv.

Does not touch the database. Read-only: fetches sources, re-scrapes,
classifies, prints a report. No status/count/notes fields are updated by
this script -- once you've seen which sources are "NOW WORKING," you
still re-fire (or let the next scheduled cycle pick them up) to actually
post/persist those events; this script's job is telling you which ones
that's worth doing for.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from urllib.parse import quote

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scraper  # noqa: E402  (must come after sys.path insert)

ENV_PATH = "/etc/openclaw/openclaw.env"


def _load_env(path=ENV_PATH):
    env = {}
    if not os.path.exists(path):
        return env
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def _fetch_sources(env, statuses, city=None, limit=None):
    """Shell out to the mysql CLI to pull sources -- avoids guessing which
    Python MySQL driver (if any) is available in this venv."""
    prefix = env.get("WP_PREFIX", "wp_")
    table = f"{prefix}openclaw_sources"

    status_list = ",".join(f"'{s.strip()}'" for s in statuses)
    where = f"status IN ({status_list})"
    if city:
        where += f" AND city_slug = '{city.lower()}'"
    limit_clause = f" LIMIT {int(limit)}" if limit else ""

    # Tab-separated, no column header (-N -B), NULLs come through as the
    # literal string "NULL" -- filtered out below. notes included now so
    # any already-configured browser_ua/html_relay/no_ical flags are
    # respected during the real re-scrape pass, not just displayed.
    query = (
        f"SELECT id, name, city_slug, url, source_type, status, notes "
        f"FROM {table} WHERE {where} ORDER BY city_slug, name{limit_clause};"
    )
    cmd = [
        "mysql", "-N", "-B",
        "-h", env.get("WP_DB_HOST", "localhost"),
        "-P", env.get("WP_DB_PORT", "3306"),
        "-u", env.get("WP_DB_USER", "root"),
        f"-p{env.get('WP_DB_PASSWORD', '')}",
        env.get("WP_DB_NAME", "wordpress"),
        "-e", query,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print("MySQL query failed:", result.stderr, file=sys.stderr)
        sys.exit(1)

    rows = []
    for line in result.stdout.strip().split("\n"):
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        _id, name, city_slug, url, source_type, status, notes = parts[:7]
        rows.append({
            "id": _id, "name": name, "city_slug": city_slug,
            "url": url if url != "NULL" else "",
            "source_type": source_type, "status": status,
            "notes": notes if notes != "NULL" else "",
        })
    return rows


def _build_source_dict(row: dict) -> dict:
    """Merge any JSON config in the notes field into the source dict, the
    same way the (now-fixed) Monitor PHP does before calling scrape_source
    -- so a source already flagged browser_ua/html_relay/no_ical in the DB
    gets re-tested WITH those flags, not without them."""
    source = {"name": row["name"], "url": row["url"], "source_type": row["source_type"]}
    try:
        flags = json.loads(row["notes"]) if row["notes"] else {}
        if isinstance(flags, dict):
            source.update(flags)
    except Exception:
        pass  # non-JSON notes (a plain free-text note) -- ignore, same as the PHP side
    return source


# Known-good dropdown values (see openclaw-monitor.php stypes()) -- anything
# else stored in source_type is a stale/orphaned label that silently falls
# through to html_auto regardless of what it says.
_KNOWN_TYPES = {"html_auto", "seetickets", "tec_rest", "ical_url", "json_api",
                "generic_html", "auto", "squarespace"}


def _revisit_source(source: dict, city_slug: str) -> int:
    """
    PASS 1: run the source through the REAL current scraper.py tier logic
    (respecting browser_ua/html_relay/no_ical from its notes), skipping
    only the slow Ollama tier 4 fallback so a run across hundreds of
    sources stays bounded in time. Returns the event count actually
    produced right now, with the current code.

    Raises on a hard fetch failure (connection error, DNS failure, etc.)
    so the caller can distinguish "ran clean, found 0" from "couldn't
    even fetch it" -- the latter should NOT be reported as "still
    genuinely has no matching markup," it's a different problem.
    """
    stype = (source.get("source_type") or "html_auto").lower().strip()
    if stype not in _KNOWN_TYPES:
        stype = "html_auto"  # stale/orphaned label -- this IS what scrape_source() does today
    if stype in ("auto", "squarespace"):
        stype = "html_auto"

    city_name = city_slug.title()

    if stype == "html_auto":
        url = source["url"]
        resp = scraper._fetch_source_page(source, url, timeout=15)
        html = resp.text
        no_ical = source.get("no_ical", False)

        ical_url = scraper._detect_ical(html, url, no_ical=no_ical)
        if ical_url:
            events = scraper._parse_ical(ical_url, source, city_slug, city_name)
            if events:
                return len(events)

        if not no_ical:
            multi = scraper._detect_multi_ical(html, url)
            if multi:
                events = scraper._parse_multi_ical(multi, source, city_slug, city_name)
                if events:
                    return len(events)

        events = scraper._parse_jsonld(html, source, city_slug, city_name)
        if events:
            return len(events)

        events = scraper._parse_rhp(html, url, source, city_slug, city_name)
        if events:
            return len(events)

        events = scraper._parse_heuristic(html, url, source, city_slug, city_name)
        return len(events)

    elif stype == "seetickets":
        return len(scraper._scrape_seetickets(source, city_slug, city_name))
    elif stype == "tec_rest":
        return len(scraper._scrape_tec_rest(source, city_slug, city_name))
    elif stype == "ical_url":
        return len(scraper._scrape_ical_url(source, city_slug, city_name))
    elif stype == "json_api":
        return len(scraper._scrape_json_api(source, city_slug, city_name))
    elif stype == "generic_html":
        # generic_html itself falls through to Ollama internally when its
        # selector is missing or matches nothing -- can't safely call the
        # real function here without risking the same slow-tier-4 cost
        # this script is designed to avoid. Only test it if a selector is
        # actually configured AND that selector matches something, via
        # the same direct fetch+select the real function would use.
        if not source.get("event_container_selector"):
            return 0
        resp = scraper._request_get(source["url"], headers=scraper.HEADERS, timeout=15,
                                     source_name=source.get("name", ""))
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(resp.text, "html.parser")
        return len(soup.select(source["event_container_selector"]))

    return 0


DUMP_DIR = "/opt/openclaw/triage_html_dump"


def _dump_html(source: dict, html: str):
    """Save the raw fetched HTML for a still-broken source to disk, so a
    full batch run produces a directory of real markup samples that can
    be reviewed and tarred up in one shot -- instead of asking for a
    live re-fetch of one URL at a time every time a new pattern needs
    investigating. Best-effort: a write failure here should never break
    the triage run itself."""
    try:
        os.makedirs(DUMP_DIR, exist_ok=True)
        safe_city = re.sub(r"[^a-zA-Z0-9_-]", "_", source.get("__city_slug", "unknown"))
        safe_name = re.sub(r"[^a-zA-Z0-9_-]", "_", source["name"])[:80]
        path = os.path.join(DUMP_DIR, f"{safe_city}__{safe_name}.html")
        with open(path, "w", encoding="utf-8") as f:
            f.write(html)
    except Exception:
        pass  # diagnostic convenience only -- never let this break classification



def _classify(source: dict) -> tuple:
    """PASS 2 (only reached if pass 1 found 0): returns (bucket, detail)
    explaining WHY. Never raises -- a classification failure is itself
    just recorded as a bucket.

    Deliberately uses requests.get() directly here, NOT scraper._request_get().
    _request_get() raises immediately on a plain 403/404/etc (the right
    behavior for production scraping, where a 4xx genuinely means "give
    up"), but this function's entire job is to READ the status code, not
    have it thrown away as an exception before we get a chance to look at
    it -- that was a real bug caught while testing this script (a 403
    during classification was being mislabeled as
    dead_domain_or_connection_error instead of getting the browser_ua/
    html_relay check it needed).
    """
    from bs4 import BeautifulSoup

    name, url = source["name"], source["url"]

    if not url:
        return ("no_url_configured", "no fix possible without adding a URL")

    try:
        resp = requests.get(url, headers=scraper.HEADERS, timeout=15)
        plain_status, plain_len, plain_html = resp.status_code, len(resp.content), resp.text
    except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
        return ("dead_domain_or_connection_error", f"{type(e).__name__}: {e}")

    if plain_status == 200:
        if len(plain_html) < 3000 and "/event" not in plain_html.lower():
            return ("likely_js_rendered_empty_shell", f"{plain_len} bytes, no event links in raw HTML")

        # Lightweight diagnostic so a whole pile of markup_not_matched
        # sources can be scanned for common patterns in one report,
        # instead of running the CLI debug tool on each one individually.
        # Not a full re-implementation of _parse_heuristic's logic -- just
        # enough signal to see, at a glance, whether ANY known selector
        # hit, how many fallback heading candidates exist, and whether
        # this looks like a near-miss multi-iCal case (some per-event
        # ical links present, just not enough to clear the min_links
        # threshold -- distinguishing that from "zero ical links at all"
        # matters: a near-miss might just need the threshold tuned, while
        # zero means this site genuinely doesn't expose that pattern).
        try:
            soup = BeautifulSoup(plain_html, "html.parser")
            selector_hit = next((sel for sel in scraper._EVENT_SELECTORS if soup.select(sel)), None)
            if selector_hit:
                containers = scraper._dedupe_nested_matches(soup.select(selector_hit))
            else:
                containers = scraper._fallback_heading_containers(soup)
            jsonld_count = len(soup.find_all("script", {"type": "application/ld+json"}))

            # min_links=1 here on purpose -- we want the RAW count for
            # diagnostic visibility, not scraper.py's production threshold
            # (3). A count of 1-2 still tells us something useful (a near
            # miss) that a plain "multi_ical=0" would hide.
            multi_ical_links = scraper._detect_multi_ical(plain_html, url, min_links=1)

            diag = (f"selector_hit={selector_hit!r} fallback_containers={len(containers)} "
                    f"jsonld_scripts={jsonld_count} multi_ical_links={len(multi_ical_links)}")
        except Exception as e:
            diag = f"diagnostic failed: {type(e).__name__}: {e}"

        _dump_html(source, plain_html)

        return ("markup_not_matched",
                 f"{plain_len} bytes fetched OK, no tier matched -- {diag}")

    # 202 included alongside the usual bot-block statuses -- seen
    # recurring across multiple otherwise-unrelated sources in this run
    # (Eastside Bowl, Alabama Theatre, Red Mountain Theatre, The Lyric
    # Theatre). A 202 on a plain GET for a static event listing page is
    # not a normal "accepted, processing" response -- that status code
    # doesn't mean anything for a synchronous page load. Far more likely
    # explanation: a WAF/CDN handing non-browser clients a placeholder
    # "202" instead of a clean block, the same underlying pattern as the
    # 403 cases, just a different status code for it.
    if plain_status in (403, 406, 429, 202):
        try:
            resp2 = requests.get(url, headers=scraper.BROWSER_HEADERS, timeout=15)
            if resp2.status_code == 200:
                return ("needs_browser_ua", f"plain UA={plain_status}, browser UA=200")
            return ("needs_html_relay", f"blocked on both plain ({plain_status}) "
                                         f"and browser UA ({resp2.status_code}) -- ASN/IP block")
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            return ("needs_html_relay", f"plain UA={plain_status}, browser UA fetch also failed: {e}")

    return ("other_http_status", f"HTTP {plain_status}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", default="rejected,probation",
                     help="comma-separated status values to triage (default: rejected,probation)")
    ap.add_argument("--city", default=None, help="limit to one city_slug")
    ap.add_argument("--limit", type=int, default=None, help="limit row count (for a quick test run)")
    ap.add_argument("--delay", type=float, default=0.5,
                     help="seconds to sleep between sources (default 0.5 -- be a little polite)")
    args = ap.parse_args()

    env = _load_env()
    if not env:
        print(f"Could not read {ENV_PATH} -- are you running this as a user "
              f"with read access, and does that file exist on this box?", file=sys.stderr)
        sys.exit(1)

    statuses = [s.strip() for s in args.status.split(",")]
    rows = _fetch_sources(env, statuses, city=args.city, limit=args.limit)
    print(f"Re-testing {len(rows)} sources against the CURRENT scraper.py "
          f"(status in {statuses})...\n")

    now_working = []
    buckets = {}

    for i, row in enumerate(rows, 1):
        source = _build_source_dict(row)
        source["__city_slug"] = row["city_slug"]  # used only for the HTML dump filename
        label = f"[{i}/{len(rows)}] {row['city_slug']:12s} {row['name']:45s}"

        if not row["url"]:
            buckets.setdefault("no_url_configured", []).append(
                (row["name"], row["city_slug"], "no fix possible without adding a URL"))
            print(f"{label} -> no_url_configured")
            time.sleep(args.delay)
            continue

        try:
            count = _revisit_source(source, row["city_slug"])
        except Exception as e:
            # IMPORTANT: an HTTPError means we got a real response from a
            # real server, just a non-2xx one (403 blocked, 404 moved,
            # etc.) -- that's exactly the case _classify() knows how to
            # investigate further (browser_ua vs html_relay vs genuine
            # other-status). Only a true connection-level failure (DNS
            # resolution, refused connection, timeout with no response at
            # all) means the domain itself is actually unreachable. Catching
            # both the same way here previously buried real 403-blocked
            # sources in "dead_domain_or_connection_error" instead of
            # giving them the same shot at needs_browser_ua/needs_html_relay
            # that Parker Arts got.
            if isinstance(e, requests.exceptions.HTTPError):
                bucket, detail = _classify(source)
                buckets.setdefault(bucket, []).append((row["name"], row["city_slug"], detail))
                print(f"{label} -> still 0 (HTTP error on re-scrape), classified as: {bucket}")
            else:
                buckets.setdefault("dead_domain_or_connection_error", []).append(
                    (row["name"], row["city_slug"], f"{type(e).__name__}: {e}"))
                print(f"{label} -> dead_domain_or_connection_error")
            time.sleep(args.delay)
            continue

        if count > 0:
            now_working.append((row["name"], row["city_slug"], row["id"], count))
            print(f"{label} -> NOW WORKING ({count} events)")
        else:
            bucket, detail = _classify(source)
            buckets.setdefault(bucket, []).append((row["name"], row["city_slug"], detail))
            print(f"{label} -> still 0, classified as: {bucket}")

        time.sleep(args.delay)

    print("\n" + "=" * 78)
    print(f"NOW WORKING WITH CURRENT CODE  ({len(now_working)})")
    print("=" * 78)
    print("These already produce events right now -- no manual flag changes")
    print("needed. Re-fire them (or let the next scheduled cycle pick them up).\n")
    for name, city_slug, source_id, count in now_working:
        print(f"    - [{city_slug}] {name} (id={source_id}): {count} events")

    print("\n" + "=" * 78)
    print("STILL AT ZERO -- ROOT CAUSE BREAKDOWN")
    print("=" * 78)

    _BUCKET_ORDER = [
        "no_url_configured", "needs_browser_ua", "needs_html_relay",
        "likely_js_rendered_empty_shell", "markup_not_matched",
        "dead_domain_or_connection_error", "other_http_status",
    ]
    _BUCKET_NOTES = {
        "no_url_configured": "Add a URL, or delete the row -- no scraper fix possible.",
        "needs_browser_ua": 'One MySQL UPDATE per source: notes = \'{"browser_ua":true}\'',
        "needs_html_relay": 'Add domain to ALLOWED_HOSTS in the relay Worker, then '
                             'notes = \'{"browser_ua":true,"html_relay":true}\'',
        "likely_js_rendered_empty_shell": "Needs a real browser render (headless), not a CSS/regex fix.",
        "markup_not_matched": "Genuinely needs per-site scraper work -- send me the debug CLI output for these.",
        "dead_domain_or_connection_error": "DNS/connection failure -- check the URL is still correct.",
        "other_http_status": "Inspect individually -- not one of the common patterns.",
    }

    for bucket in _BUCKET_ORDER:
        items = buckets.pop(bucket, [])
        if not items:
            continue
        print(f"\n{bucket}  ({len(items)})")
        print(f"  → {_BUCKET_NOTES.get(bucket, '')}")
        for name, city_slug, detail in items:
            print(f"    - [{city_slug}] {name}: {detail}")

    for bucket, items in buckets.items():  # any unexpected leftover buckets
        print(f"\n{bucket}  ({len(items)})")
        for name, city_slug, detail in items:
            print(f"    - [{city_slug}] {name}: {detail}")

    if os.path.isdir(DUMP_DIR) and os.listdir(DUMP_DIR):
        print("\n" + "=" * 78)
        print(f"Raw HTML saved for every markup_not_matched source -> {DUMP_DIR}")
        print("To review all of them in one batch instead of one URL at a time:")
        print(f"    tar czf /tmp/triage_dump.tar.gz -C {os.path.dirname(DUMP_DIR)} "
              f"{os.path.basename(DUMP_DIR)}")
        print("    (then upload /tmp/triage_dump.tar.gz)")


if __name__ == "__main__":
    main()
