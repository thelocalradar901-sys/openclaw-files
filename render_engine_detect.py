"""
render_engine_detect.py

Fingerprints a source's HTML to determine which "render engine" it uses,
so scraper.py can route straight to the correct adapter tier instead of
falling through iCal -> JSON-LD -> RHP -> heuristic -> Playwright every
single scrape cycle.

Usage pattern:
    1. Run detect_render_engine() ONCE per source (e.g. during vetting /
       probation, in vet_probation_sources.py, or on first scrape attempt).
    2. Persist the result on the source record as `render_engine`.
    3. In scraper.py's tier dispatch, check `render_engine` before falling
       back to the generic tier chain:

        if source.render_engine == "eventon":
            events = eventon.fetch_events(source)
        elif source.render_engine == "mec":
            events = mec.fetch_events(source)  # future
        elif source.render_engine == "playwright":
            events = render_with_playwright(source)  # last resort
        else:
            events = run_standard_tier_chain(source)  # iCal/JSON-LD/RHP/heuristic

Known engines are matched by generator meta tag first (cheap, reliable),
then by CSS/script fingerprints as a fallback for sites that strip the
generator tag.
"""

import re
from dataclasses import dataclass
from typing import Optional

# (engine_id, [meta-generator substrings], [html/css/script substrings])
# Add new plugins here as you encounter them. Keep substrings lowercase.
KNOWN_ENGINES = [
    (
        "eventon",
        ["eventon"],
        ["evo_calendar", "eventon_events_list", "eventon_script", "evcal_"],
    ),
    (
        "mec",  # Modern Events Calendar
        ["modern events calendar", "webnus"],
        ["mec-event", "mec_skin", "mec-calendar"],
    ),
    (
        "tec_block",  # The Events Calendar, newer block-based views (JS-hydrated)
        ["the events calendar"],
        ["tribe-events-view", "tribe-events-pro"],
    ),
]


@dataclass
class RenderEngineResult:
    engine: Optional[str]  # e.g. "eventon", "mec", None if unrecognized
    confidence: str        # "meta" (high) or "fingerprint" (medium) or "none"
    matched_on: Optional[str] = None


def detect_render_engine(html: str) -> RenderEngineResult:
    """
    Inspect raw HTML (pre-JS, as fetched by requests/wget) and identify
    known JS-rendered calendar plugins.

    Returns RenderEngineResult(engine=None, confidence="none") if nothing
    matches -- caller should fall through to the standard tier chain, and
    only escalate to Playwright if that chain also comes up empty.
    """
    if not html:
        return RenderEngineResult(engine=None, confidence="none")

    lower = html.lower()

    # Pass 1: meta generator tag -- cheap and reliable when present
    generator_match = re.search(
        r'<meta[^>]+name=["\']generator["\'][^>]+content=["\']([^"\']+)["\']',
        lower,
    )
    generator_content = generator_match.group(1) if generator_match else ""

    for engine_id, meta_needles, _ in KNOWN_ENGINES:
        for needle in meta_needles:
            if needle in generator_content:
                return RenderEngineResult(
                    engine=engine_id, confidence="meta", matched_on=needle
                )

    # Pass 2: CSS class / script handle fingerprints (site stripped generator tag,
    # or fingerprint appears in a second <meta> block some themes inject)
    for engine_id, _, html_needles in KNOWN_ENGINES:
        for needle in html_needles:
            if needle in lower:
                return RenderEngineResult(
                    engine=engine_id, confidence="fingerprint", matched_on=needle
                )

    return RenderEngineResult(engine=None, confidence="none")


def has_no_static_event_content(html: str, min_event_keywords: int = 2) -> bool:
    """
    Secondary signal: does this page look event-related (nav/labels present)
    but contain none of the usual static content markers (JSON-LD, iCal link,
    repeated date/time patterns)? If true alongside a known JS engine match,
    that's strong confirmation the standard tiers will fail and we should
    route directly to the adapter (or Playwright if no adapter exists yet).

    This is intentionally cheap/heuristic -- it's a routing signal, not a
    scrape method.
    """
    if not html:
        return True

    lower = html.lower()
    has_jsonld_event = '"@type":"event"' in lower.replace(" ", "")
    has_ical_link = ".ics" in lower
    # crude date pattern check (e.g. "July 12" / "07/12/2026")
    has_date_pattern = bool(
        re.search(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+\d{1,2}", lower)
    ) or bool(re.search(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b", lower))

    return not (has_jsonld_event or has_ical_link or has_date_pattern)


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python render_engine_detect.py <path_to_saved_html>")
        sys.exit(1)

    with open(sys.argv[1], "r", encoding="utf-8", errors="ignore") as f:
        html = f.read()

    result = detect_render_engine(html)
    static_empty = has_no_static_event_content(html)
    print(f"engine={result.engine} confidence={result.confidence} matched_on={result.matched_on}")
    print(f"no_static_event_content={static_empty}")
