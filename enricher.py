"""
enricher.py — Ollama/Qwen3 AI description enricher

Generates descriptions for events missing them. Passes rich context to Ollama
so it can draw on its training data for known artists, teams, venues, etc.
"""

import logging
import re

from config import OLLAMA_MODEL
from ollama_client import generate as _ollama_generate

log = logging.getLogger("openclaw.enricher")


def _build_context(event: dict) -> str:
    """Build rich context so Ollama can use its training knowledge."""
    title      = event.get("title", "")
    venue      = event.get("venue_name", "")
    city       = (event.get("city_slug") or "").replace("-", " ").title()
    start      = event.get("start_local") or event.get("start_date", "")
    cost       = event.get("cost", "")
    categories = event.get("categories") or []
    source     = event.get("source_name", "")

    title_lower = title.lower()
    is_sports = any(k in title_lower for k in [
        " vs ", " vs. ", " v ", "game", "match", "tournament",
        "fc", "legion", "barons", "redbirds", "grizzlies", "hustle", "tigers",
        "nba", "nfl", "mlb", "nhl", "mls",
    ])
    is_concert = (
        any(k in title_lower for k in ["tour", "live", "concert", "presents", "performing"])
        or source == "Ticketmaster"
    ) and not is_sports

    lines = [f"Event: {title}"]
    if venue:      lines.append(f"Venue: {venue}")
    if city:       lines.append(f"City: {city}")
    if start:      lines.append(f"Date: {start[:10]}")
    if cost:       lines.append(f"Price: {cost}")
    if categories: lines.append(f"Category: {', '.join(categories)}")

    if is_sports:
        lines.append("Type: Professional or semi-professional sports match")
        lines.append("Note: Use your knowledge of these teams to write an exciting match preview.")
    elif is_concert:
        lines.append("Type: Live music or entertainment event")
        lines.append("Note: Use your knowledge of this artist to write an engaging description.")
    else:
        lines.append("Note: Write an engaging local events description based on the event details.")

    return "\n".join(lines)


def _generate(event: dict) -> str:
    context = _build_context(event)

    prompt = (
        "You are a writer for a local events website with an energetic, local voice.\n"
        "Write a 2-3 sentence event description based on the details below.\n"
        "Rules:\n"
        "- Do NOT include the date, price, or venue address\n"
        "- Do NOT start with 'Join us'\n"
        "- DO use your knowledge of the artist, team, or event if you recognize it\n"
        "- Make it exciting and specific, not generic\n"
        "- Return ONLY the description text, nothing else\n\n"
        f"{context}"
    )

    try:
        data = _ollama_generate({"model": OLLAMA_MODEL, "prompt": prompt, "stream": False})
        text = data.get("response", "").strip()
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
        text = text.strip('"\'')
        return text
    except Exception as e:
        log.warning("Enrichment failed for '%s': %s", event.get("title"), e)
        return ""
