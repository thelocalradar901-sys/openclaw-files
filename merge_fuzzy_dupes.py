"""
merge_fuzzy_dupes.py — merge cross-source duplicate PAIRS (found by
find_fuzzy_dupes.py's logic) into a single post, field by field, instead
of blindly keeping one and deleting the other.

Field-level merge rules:
  - Ticket URL (_EventURL): winner decided by the SAME AFFILIATE_PRIORITY
    ranking used in update_event() (ticketmaster > eventim > none).
    A post with a lower-priority/no affiliate link never overrides a
    higher-priority one, but if the picked "primary" post has no
    affiliate link and the other one does, the affiliate link is pulled
    IN rather than lost.
    EXCEPTION -- Choose901: if either side of the pair is sourced from
    "Choose 901", its URL is never eligible to become _EventURL, no
    matter which post wins primary (see _resolve_ticket_url()). Falls
    back to the other side's URL if it has one, else left blank. Every
    OTHER field (title/thumbnail/description) still follows the normal
    rules above -- Choose901 can win primary and contribute those.
  - Image (_thumbnail_id): keep it if primary already has one; otherwise
    pull in the other post's image reference.
  - Description (post_content): keep primary's if non-empty and
    reasonably substantial; otherwise pull in the other's if longer.
  - Primary post chosen as whichever of the pair has the affiliate URL;
    if neither/both do, whichever has the image; final tie-break is
    earliest post_id (most likely to have existing traffic/links to it).

The LOSING post is fully deleted (posts, postmeta, tec_events,
tec_occurrences, term_relationships, openclaw_fingerprints row) same as
the cancelled-event delete path in insert_event(). The winning post's fp
row is left as-is; the losing post's fp (if any) is deleted so it can't
be food for a stale future lookup.

Run with no flags = dry run (prints planned merges, writes nothing).
Run with --apply to execute.
"""
import sys
import argparse
sys.path.insert(0, "/opt/openclaw")
import difflib
import re

_NUMBERED_OCCURRENCE_RE = re.compile(
    r"\((?:night|day|part|show|set)\s*\d+\)", re.IGNORECASE
)

# "vs" / "vs." / "v." as a whole word -- see the fixture guard comment
# below for why the period-abbreviated forms had to be added on top of
# the plain " vs " check.
_FIXTURE_RE = re.compile(r"\bvs?\.?\s", re.IGNORECASE)

_LINEUP_SHOW_RE = re.compile(
    r"\bfeaturing\b|\bfeat\.|\blineup\b|&\s*friends\b|\bin the round\b", re.IGNORECASE
)

_TOKEN_STOPWORDS = {
    "the", "a", "an", "of", "with", "and", "or", "in", "at", "w",
    "presents", "performs", "live", "tour",
}
_TOKEN_RE = re.compile(r"[a-z0-9]+")
_MIN_CONTAINMENT_TOKENS = 3  # guard against short/generic titles matching by chance

def _token_set(norm_title: str) -> set:
    return {w for w in _TOKEN_RE.findall(norm_title) if w not in _TOKEN_STOPWORDS and len(w) > 1}

def _is_token_containment_match(norm_a: str, norm_b: str) -> bool:
    """
    True if every significant word in the SHORTER title also appears in
    the longer one, regardless of order. Catches titles SequenceMatcher's
    character-ratio scores low on purely because of word reordering or
    connective filler -- e.g. "The Colorado Symphony performs the Music
    of Hans Zimmer & John Williams" vs "Colorado Symphony: Hans Zimmer &
    John Williams" scored 0.769 (below TITLE_SIMILARITY_THRESHOLD) even
    though every content word of the second title is present in the
    first. Confirmed 2026-07-17. Full containment (not partial-overlap/
    Jaccard) keeps this conservative: "Colorado Symphony: Beethoven's
    9th" vs "Colorado Symphony: Mozart's Requiem" shares only 2 of 4
    tokens and correctly stays unmatched. The guards above (fixture,
    numbered-occurrence, lineup) all run independently of this and
    still apply regardless of what this returns.
    """
    ta, tb = _token_set(norm_a), _token_set(norm_b)
    if len(ta) < _MIN_CONTAINMENT_TOKENS or len(tb) < _MIN_CONTAINMENT_TOKENS:
        return False
    shorter, longer = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    return shorter <= longer

def _is_lineup_show(title: str) -> bool:
    """
    True if a title looks like a multi-performer showcase/lineup listing
    (contains "featuring", "feat.", "lineup", or "& friends"). These
    recurring-series shows (songwriter rounds, tribute nights, cabaret
    nights) share so much generic boilerplate across genuinely DIFFERENT
    real shows that no similarity-ratio or prefix-comparison heuristic
    reliably tells them apart -- confirmed 2026-07-07: "Gay Ole Opry
    Presents: Lilith Feral lineup TBA" (a cabaret night) scored 0.82
    against BOTH "BACKSTAGE NASHVILLE! ... featuring Aaron Raitiere..."
    (a songwriter showcase) and "The Long Players performing Fleetwood
    Mac's 'Rumours' featuring Todd Sharp..." (a tribute band) -- three
    unrelated shows. Rather than chase an ever-more-specific heuristic,
    ANY pair where either side is a lineup-style show is pulled out of
    auto-merge entirely and flagged for manual review instead.

    "& friends" added 2026-07-17: "In The Round with Zach Henard &
    friends" scored 0.81 against "In The Round with Kim Richey &
    friends" -- same recurring songwriter-round series, different
    real headliner each night. Same failure mode as the lineup case
    above (shared template boilerplate, different actual performer).

    "in the round" added 2026-07-17 (same day, second occurrence):
    "In The Round with Dylan Altman, Marshall Altman & Brice Long"
    scored 0.88 against "...& Terry McBride" -- two shared names (the
    recurring hosts) plus one differing guest name is exactly as
    ambiguous as the single-name-swap case above (could be one show
    with a data-entry error on the guest, could be two genuinely
    different nights) -- not confident enough to auto-merge either way,
    so the whole "In The Round" format is excluded like the others.
    """
    return bool(_LINEUP_SHOW_RE.search(title or ""))
from collections import defaultdict
from db import (
    normalize_title_for_matching, _is_headliner_prefix_match,
    TITLE_SIMILARITY_THRESHOLD, make_fingerprint, get_connection
)

WP_PREFIX = "wp_"

AFFILIATE_PRIORITY = ["ticketmaster", "eventim"]
AFFILIATE_MARKERS = {
    "ticketmaster": lambda u: "aaid=" in u,
    "eventim":      lambda u: ("seeticketsusa.us" in u or
                                "eventim.us" in u or
                                "nts_trk=" in u),
}

def affiliate_rank(url: str):
    url = url or ""
    for i, src in enumerate(AFFILIATE_PRIORITY):
        if AFFILIATE_MARKERS[src](url):
            return i
    return None  # no known affiliate link


def _is_choose901(source_name: str) -> bool:
    return (source_name or "").strip().lower().replace(" ", "") == "choose901"


def _resolve_ticket_url(primary: dict, secondary: dict) -> str | None:
    """
    Only call this when Choose901 is on either side of the pair. Returns
    the value updates["_EventURL"] should be set to, or None if
    primary's existing value should be left untouched.

    Choose901 (source name "Choose 901") is scraped from a city events
    calendar -- confirmed 2026-07-17: its "ticket_url" is inconsistent
    quality, sometimes its own choose901.com listing page, sometimes a
    real external link, sometimes something unrelated entirely (e.g. a
    generic Microsoft Forms link some organizer used for RSVPs). Not
    reliable as an outbound link under any circumstance, regardless of
    what domain it happens to point to on a given event -- so this is a
    blanket per-SOURCE ban, not a URL-pattern check. Choose901 can still
    win primary via the normal thumbnail/post_id tie-break and supply
    title/description/image as usual; only _EventURL is restricted.
    """
    p_choose901 = _is_choose901(primary.get("source_name"))
    s_choose901 = _is_choose901(secondary.get("source_name"))

    if not p_choose901 and primary.get("ticket_url"):
        return None  # primary already has a legitimate (non-Choose901) URL -- keep it
    if not s_choose901 and secondary.get("ticket_url"):
        return secondary["ticket_url"]  # fall back to the other source's URL
    if p_choose901 and primary.get("ticket_url"):
        return ""  # only Choose901's own URL exists anywhere in this pair -- blank it
    return None  # nothing usable on either side, and nothing to blank


def find_pairs(conn):
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT p.ID AS post_id, p.post_title, p.post_content,
                MAX(CASE WHEN pm.meta_key='_EventStartDateUTC' THEN pm.meta_value END) AS start_utc,
                MAX(CASE WHEN pm.meta_key='_EventStartDate'    THEN pm.meta_value END) AS start_local,
                MAX(CASE WHEN pm.meta_key='_openclaw_city'     THEN pm.meta_value END) AS city_slug,
                MAX(CASE WHEN pm.meta_key='_openclaw_source'   THEN pm.meta_value END) AS source_name,
                MAX(CASE WHEN pm.meta_key='_thumbnail_id'      THEN pm.meta_value END) AS thumb_id,
                MAX(CASE WHEN pm.meta_key='_EventURL'          THEN pm.meta_value END) AS ticket_url,
                MAX(CASE WHEN pm.meta_key='_openclaw_fp'       THEN pm.meta_value END) AS fp
            FROM {WP_PREFIX}posts p
            LEFT JOIN {WP_PREFIX}postmeta pm ON pm.post_id = p.ID
            WHERE p.post_type = 'tribe_events' AND p.post_status IN ('publish','draft')
            GROUP BY p.ID, p.post_title, p.post_content
        """)
        rows = cur.fetchall()

    buckets = defaultdict(list)
    for r in rows:
        # Bucket by LOCAL calendar date, not UTC. Evening shows in
        # Mountain/Central/etc time are early-morning UTC the NEXT day,
        # so a difference of as little as 30-90 minutes in the resolved
        # start time between two scrapes of the SAME real-world show --
        # from source data drift or a timezone-offset inconsistency --
        # is enough to push one copy's start_utc across midnight while
        # the other stays put. Bucketing on UTC then splits the pair
        # into two different (city, date) buckets that are never
        # compared at all. Confirmed 2026-07-17: Bach's Brandenburg
        # Concertos, Maren Morris w/ Colorado Symphony, JINJER, String
        # Cheese Incident, and Hans Zimmer & John Williams all had
        # matching local dates but UTC dates one day apart, so they
        # were silently skipped -- not a similarity-threshold miss, a
        # bucketing miss. Local date is stable across that drift since
        # it's what "same day" actually means to the humans reading it.
        canonical = (r["start_local"] or r["start_utc"] or "").strip()[:10]
        city = (r["city_slug"] or "").strip().lower()
        if not canonical or not city:
            continue
        r["_norm_title"] = normalize_title_for_matching(r["post_title"] or "")
        r["_fp"] = make_fingerprint({"title": r["post_title"], "start_utc": r["start_utc"],
                                      "start_local": r["start_local"], "city_slug": r["city_slug"]})
        buckets[(city, canonical)].append(r)

    pairs = []
    for (city, date), posts in buckets.items():
        n = len(posts)
        for i in range(n):
            for j in range(i + 1, n):
                a, b = posts[i], posts[j]
                if a["_fp"] == b["_fp"]:
                    # Identical title+date+city fingerprint -- this IS the
                    # duplicate, not a similarity false-positive risk (the
                    # guards below exist to protect against titles that are
                    # merely SIMILAR, which doesn't apply when the
                    # fingerprint match is exact). insert_event()'s own
                    # dedup occasionally misses these when two scrape
                    # passes resolve start times slightly differently at
                    # insert time, so the stored fingerprints differ even
                    # though title/date/city are now identical. This was
                    # a silent `continue` before -- confirmed 2026-07-17:
                    # 24 exact-duplicate pairs sat unmerged indefinitely
                    # because of it.
                    pairs.append((1.0, a, b))
                    continue
                # Fixture/matchup guard: titles like "Team A vs Team B" must
                # never be fuzzy-matched against each other. Confirmed
                # 2026-07-07: "Liverpool FC vs Sunderland" scored 0.86
                # against "Birmingham Legion FC vs Brooklyn FC" -- two
                # completely unrelated real-world games -- because shared
                # boilerplate ("FC", "vs", "Womens Volleyball") inflates
                # character-overlap similarity regardless of which teams
                # are actually playing. Also caught same-tournament
                # different-opponent pairs (Buffs Classic vs Central
                # Arkansas vs. vs Denver Pioneers). Fixture titles are
                # only ever safe to treat as the same event via the EXACT
                # fingerprint path (make_fingerprint()), never via fuzzy
                # similarity here.
                #
                # Broadened 2026-07-21: the literal " vs " (space-bounded,
                # no period) check missed "Auburn v. UCF" vs "Auburn vs.
                # Florida State" -- two genuinely different games -- since
                # both use a period-abbreviated "v."/"vs." that never
                # produces a bare " vs " substring. Confirmed these two
                # scored 0.81, ABOVE threshold, and would have deleted a
                # real, separately-ticketed game. Now matches "vs", "vs.",
                # or "v." as a whole token (word-boundary before, so this
                # never fires on a title that merely contains a word
                # ending in "v" or "vs" as a substring, e.g. "Vs." can't
                # match inside "Elvis").
                a_is_fixture = bool(_FIXTURE_RE.search(a["_norm_title"]))
                b_is_fixture = bool(_FIXTURE_RE.search(b["_norm_title"]))
                if a_is_fixture or b_is_fixture:
                    continue

                # Numbered-occurrence guard: "(Night 2)", "(Night 3)",
                # "(Day 1)", "(Part 2)" etc mark genuinely DIFFERENT real
                # performances in a multi-night residency, not duplicate
                # listings of the same show. Confirmed 2026-07-07: Eric
                # Church (Night 2) vs (Night 3), The Avett Brothers
                # (Night 2) vs (Night 3), Tedeschi Trucks Band, and Andrea
                # Bocelli (Night 2) all scored above threshold against
                # either each other or the base title -- would have
                # deleted real, separately-ticketed shows. If EITHER title
                # has a numbered-occurrence marker at all, skip the pair;
                # too risky to try to compare the numbers and allow a
                # "same number" case through, since a stray marker on only
                # one side is itself a sign these are different listings.
                if _NUMBERED_OCCURRENCE_RE.search(a["post_title"] or "") or \
                   _NUMBERED_OCCURRENCE_RE.search(b["post_title"] or ""):
                    continue

                ratio = difflib.SequenceMatcher(None, a["_norm_title"], b["_norm_title"]).ratio()
                is_prefix = (_is_headliner_prefix_match(a["_norm_title"], b["_norm_title"]) or
                             _is_headliner_prefix_match(b["_norm_title"], a["_norm_title"]))
                if is_prefix:
                    ratio = max(ratio, TITLE_SIMILARITY_THRESHOLD)
                if _is_token_containment_match(a["_norm_title"], b["_norm_title"]):
                    ratio = max(ratio, TITLE_SIMILARITY_THRESHOLD)

                a_is_lineup = _is_lineup_show(a["post_title"] or "")
                b_is_lineup = _is_lineup_show(b["post_title"] or "")
                if a_is_lineup and b_is_lineup:
                    # Both sides are full lineup-style titles -- this is the
                    # confirmed 2026-07-07 collision case (Gay Ole Opry vs
                    # BACKSTAGE NASHVILLE vs Long Players), where similarity
                    # alone can't tell genuinely different shows apart from
                    # dupes. Never auto-merge two lineup titles.
                    continue
                if (a_is_lineup or b_is_lineup) and not is_prefix:
                    # One side is a bare/generic series title, the other a
                    # specific lineup title, but they're not a prefix match
                    # of each other -- not confident this is the same show
                    # (could be a different night's lineup for the same
                    # recurring series). Skip.
                    continue
                # Remaining case: one side is a bare series title (e.g.
                # "Bluebird On 3rd") and it's a prefix of the other's
                # specific lineup title (e.g. "Bluebird On 3rd featuring
                # Gabe Dixon...") on the SAME venue+date -- this is the
                # generic listing + specific listing of the identical show,
                # safe to merge.
                if ratio >= TITLE_SIMILARITY_THRESHOLD:
                    pairs.append((ratio, a, b))
    return pairs


_STATUS_FLAG_RE = re.compile(
    r"\bcancell?ed\b|\bsold\s*out\b|\bpostponed\b", re.IGNORECASE
)

def _has_status_flag(title: str) -> bool:
    return bool(_STATUS_FLAG_RE.search(title or ""))


def plan_merge(a, b):
    """Decide primary/secondary and the field values the primary should end up with."""
    a_flagged = _has_status_flag(a["post_title"])
    b_flagged = _has_status_flag(b["post_title"])
    if a_flagged != b_flagged:
        # CANCELLED / SOLD OUT / POSTPONED status is live, time-sensitive
        # info -- confirmed 2026-07-13: normal primary selection (affiliate
        # rank -> thumbnail -> earliest post_id) would delete the flagged
        # post and keep the stale unflagged one, silently un-cancelling a
        # show on the site. The flagged post always wins primary so its
        # title (and thus status) survives the merge.
        primary, secondary = (a, b) if a_flagged else (b, a)
        updates = {}
        if _is_choose901(a["source_name"]) or _is_choose901(b["source_name"]):
            url = _resolve_ticket_url(primary, secondary)
            if url is not None:
                updates["_EventURL"] = url
        elif not primary["ticket_url"] and secondary["ticket_url"]:
            updates["_EventURL"] = secondary["ticket_url"]
        if not primary["thumb_id"] and secondary["thumb_id"]:
            updates["_thumbnail_id"] = secondary["thumb_id"]
        p_len = len((primary["post_content"] or "").strip())
        s_len = len((secondary["post_content"] or "").strip())
        if s_len > p_len:
            updates["post_content"] = secondary["post_content"]
        return primary, secondary, updates

    a_rank = affiliate_rank(a["ticket_url"])
    b_rank = affiliate_rank(b["ticket_url"])

    # Lower rank number = higher priority. None = no affiliate link.
    if a_rank is not None and (b_rank is None or a_rank <= b_rank):
        primary, secondary = a, b
    elif b_rank is not None and (a_rank is None or b_rank < a_rank):
        primary, secondary = b, a
    elif a["thumb_id"] and not b["thumb_id"]:
        primary, secondary = a, b
    elif b["thumb_id"] and not a["thumb_id"]:
        primary, secondary = b, a
    else:
        primary, secondary = (a, b) if a["post_id"] < b["post_id"] else (b, a)

    updates = {}
    if _is_choose901(a["source_name"]) or _is_choose901(b["source_name"]):
        url = _resolve_ticket_url(primary, secondary)
        if url is not None:
            updates["_EventURL"] = url
    # Pull in affiliate URL if primary lacks one but secondary has one
    elif affiliate_rank(primary["ticket_url"]) is None and affiliate_rank(secondary["ticket_url"]) is not None:
        updates["_EventURL"] = secondary["ticket_url"]
    # Pull in image if primary lacks one
    if not primary["thumb_id"] and secondary["thumb_id"]:
        updates["_thumbnail_id"] = secondary["thumb_id"]
    # Pull in description if primary's is empty/short and secondary's is longer
    p_len = len((primary["post_content"] or "").strip())
    s_len = len((secondary["post_content"] or "").strip())
    if s_len > p_len:
        updates["post_content"] = secondary["post_content"]

    return primary, secondary, updates


def apply_merge(conn, primary, secondary, updates):
    with conn.cursor() as cur:
        for key, val in updates.items():
            if key == "post_content":
                cur.execute(f"UPDATE {WP_PREFIX}posts SET post_content=%s WHERE ID=%s",
                            (val, primary["post_id"]))
            else:
                cur.execute(f"DELETE FROM {WP_PREFIX}postmeta WHERE post_id=%s AND meta_key=%s",
                            (primary["post_id"], key))
                cur.execute(f"INSERT INTO {WP_PREFIX}postmeta (post_id,meta_key,meta_value) VALUES (%s,%s,%s)",
                            (primary["post_id"], key, val))

        sid = secondary["post_id"]
        cur.execute(f"DELETE FROM {WP_PREFIX}tec_events WHERE post_id=%s", (sid,))
        cur.execute(f"DELETE FROM {WP_PREFIX}tec_occurrences WHERE post_id=%s", (sid,))
        cur.execute(f"DELETE FROM {WP_PREFIX}term_relationships WHERE object_id=%s", (sid,))
        cur.execute(f"DELETE FROM {WP_PREFIX}openclaw_fingerprints WHERE post_id=%s", (sid,))
        cur.execute(f"DELETE FROM {WP_PREFIX}postmeta WHERE post_id=%s", (sid,))
        cur.execute(f"DELETE FROM {WP_PREFIX}posts WHERE ID=%s", (sid,))
    conn.commit()


def merge_all(conn, pairs, apply=True, log_fn=print):
    """
    Walk find_pairs()' output in order, planning (and optionally
    applying) each merge, while skipping any pair whose primary or
    secondary was already deleted earlier in THIS SAME run.

    Needed because find_pairs() bucket-compares every post against
    every other post in its (city, date) bucket independently -- a
    3+-way cluster (e.g. several reseller-name variants of the same
    show) produces multiple overlapping pairs that don't necessarily
    agree on a single primary. Confirmed 2026-07-17: a 4-post Don
    Toliver/Birmingham cluster produced (A,B)->keep A, (A,C)->keep A,
    (B,C)->keep B. Applying all three in sequence deletes B in the
    first pair, then the third pair tries to pull fields into the
    now-deleted B as if it were still the primary -- the UPDATE/INSERT
    silently succeeds against a post_id that no longer exists in
    {WP_PREFIX}posts, orphaning postmeta rows that never get cleaned
    up. Tracking deleted ids and skipping any pair that references one
    closes this off entirely: only the first pair to touch a given
    post in a run gets to act on it, every later pair referencing it
    is stale by definition once it's gone.

    Both the manual CLI (main(), below) and the daily automated job
    (scheduler.py's _run_dupe_merge()) call this so the same safety
    holds unattended, not just when someone's watching the dry-run
    output.

    Returns (planned, merged, skipped_stale) where `planned` is the
    full list of (ratio, primary, secondary, updates) tuples actually
    acted on (or that would be, in dry-run).
    """
    planned = []
    merged = 0
    skipped_stale = 0
    deleted_ids = set()
    for ratio, a, b in pairs:
        if a["post_id"] in deleted_ids or b["post_id"] in deleted_ids:
            skipped_stale += 1
            continue
        primary, secondary, updates = plan_merge(a, b)
        planned.append((ratio, primary, secondary, updates))
        log_fn(f"[{ratio:.2f}] KEEP {primary['post_id']} '{primary['post_title']}' "
               f"(src={primary['source_name']})")
        log_fn(f"        MERGE-FROM {secondary['post_id']} '{secondary['post_title']}' "
               f"(src={secondary['source_name']}) -- then DELETE it")
        if updates:
            for k in updates:
                log_fn(f"        pulling in: {k}")
        else:
            log_fn(f"        (no fields needed from secondary)")
        log_fn("")

        if apply:
            try:
                apply_merge(conn, primary, secondary, updates)
            except Exception as e:
                # One pair failing (e.g. a stale row deleted out from
                # under us by something else entirely) shouldn't abort
                # every other pair in the run -- matches the daily job's
                # original per-pair try/except behavior.
                log_fn(f"        ERROR applying this pair: {e}")
                continue
            merged += 1
        # Tracked even in dry-run (apply=False) so the preview matches
        # what --apply will actually do -- otherwise a dry run over-reports
        # pairs that a 3+-way cluster will really skip as stale.
        deleted_ids.add(secondary["post_id"])
    return planned, merged, skipped_stale


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    conn = get_connection()
    pairs = find_pairs(conn)
    print(f"Fuzzy cross-source duplicate pairs found: {len(pairs)}\n")

    planned, merged, skipped_stale = merge_all(conn, pairs, apply=args.apply)

    if skipped_stale:
        print(f"(skipped {skipped_stale} pair(s) referencing a post already "
              f"merged earlier in this run -- part of a 3+-way cluster)\n")

    if args.apply:
        print(f"APPLIED: merged and deleted {merged} duplicate posts.")
    else:
        print("DRY RUN -- no changes written. Re-run with --apply to execute.")
    conn.close()


if __name__ == "__main__":
    main()
