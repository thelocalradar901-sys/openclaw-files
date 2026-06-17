"""
enricher.py – Ollama/Qwen3 AI description enricher for OpenClaw

Called by the scheduler after scraping. Only processes events that have
_needs_enrichment=True and an empty description. Safe to call on every batch.
"""

import logging
import re

import requests

from config import OLLAMA_HOST, OLLAMA_MODEL, OLLAMA_TIMEOUT

log = logging.getLogger("openclaw.enricher")


def enrich_events(events: list[dict]) -> list[dict]:
    """
    Fill in descriptions for events that need enrichment.
    Returns the same list (mutated in-place for efficiency).
    """
    needs = [e for e in events if e.get("_needs_enrichment") and not e.get("description")]
    if not needs:
        return events

    log.info("Enriching %d events with Ollama (%s)", len(needs), OLLAMA_MODEL)
    for event in needs:
        desc = _generate_description(event)
        if desc:
            event["description"] = desc
            event["_needs_enrichment"] = False

    return events


def _generate_description(event: dict) -> str:
    title      = event.get("title", "")
    venue      = event.get("venue_name", "")
    city       = (event.get("city_slug") or "").replace("-", " ").title()
    start_date = event.get("start_local") or event.get("start_date", "")
    cost       = event.get("cost", "")
    categories = ", ".join(event.get("categories") or [])

    parts = [f"Event: {title}"]
    if venue:      parts.append(f"Venue: {venue}")
    if city:       parts.append(f"City: {city}")
    if start_date: parts.append(f"Date: {start_date[:10]}")
    if cost:       parts.append(f"Price: {cost}")
    if categories: parts.append(f"Type: {categories}")

    prompt = (
        "Write a concise, engaging 2-3 sentence event description for a local events website. "
        "Use an energetic, local voice. Don't include the date, price, or ticket link. "
        "Don't start with 'Join us'. Return only the description text, nothing else.\n\n"
        + "\n".join(parts)
    )

    try:
        resp = requests.post(
            f"{OLLAMA_HOST}/api/generate",
            json={"model": OLLAMA_MODEL, "prompt": prompt, "stream": False},
            timeout=OLLAMA_TIMEOUT,
        )
        resp.raise_for_status()
        text = resp.json().get("response", "").strip()
        # Strip <think> blocks Qwen3 sometimes emits
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
        return text
    except Exception as e:
        log.warning("Ollama enrichment failed for '%s': %s", event.get("title"), e)
        return ""
