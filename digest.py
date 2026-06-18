"""
digest.py — Weekly email digest for The Local Radar

Sends one email per city every Monday morning with the top 8 upcoming events.
Uses Ollama/Qwen3 for a dynamic intro blurb per city.
Delivers via Brevo Email Campaigns API — handles unsubscribes, open/click
tracking, and list management automatically. No SMTP needed.

Cron: 0 7 * * 1  (7am every Monday)
  /opt/openclaw/venv/bin/python /opt/openclaw/digest.py

Env vars (add to /etc/openclaw/openclaw.env):
  BREVO_API_KEY     — Brevo API key (Settings → API Keys in Brevo dashboard)
  DIGEST_FROM_EMAIL — e.g. hello@thelocalradar.com
  DIGEST_FROM_NAME  — e.g. The Local Radar
  BREVO_LIST_IDS    — JSON map of city_slug -> Brevo list ID
                      e.g. '{"memphis":3,"denver":4,"nashville":5,"birmingham":6}'
"""

import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pymysql
import pymysql.cursors
import requests

# ── Bootstrap logging ─────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("openclaw.digest")

# ── Load env ──────────────────────────────────────────────────────────────────
env_file = "/etc/openclaw/openclaw.env"
if os.path.exists(env_file):
    with open(env_file) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

from config import DB, WP_PREFIX, OLLAMA_HOST, OLLAMA_MODEL, OLLAMA_TIMEOUT, load_cities

# ── Config ────────────────────────────────────────────────────────────────────
BREVO_API_KEY    = os.getenv("BREVO_API_KEY", "")
BREVO_API_BASE   = "https://api.brevo.com/v3"
FROM_EMAIL       = os.getenv("DIGEST_FROM_EMAIL", "hello@thelocalradar.com")
FROM_NAME        = os.getenv("DIGEST_FROM_NAME", "The Local Radar")
EVENTS_PER_CITY  = 8
TLR_BASE_URL     = "https://thelocalradar.com"

# Brevo list IDs per city slug — set via env or edit here
_list_ids_raw    = os.getenv("BREVO_LIST_IDS", "{}")
try:
    BREVO_LIST_IDS = json.loads(_list_ids_raw)
except Exception:
    BREVO_LIST_IDS = {}

# City display config
CITY_CONFIG = {
    "memphis":    {"label": "Memphis",    "color": "#00E5FF", "url_slug": "memphis"},
    "denver":     {"label": "Denver",     "color": "#00E5FF", "url_slug": "denver"},
    "nashville":  {"label": "Nashville",  "color": "#00E5FF", "url_slug": "nashville"},
    "birmingham": {"label": "Birmingham", "color": "#00E5FF", "url_slug": "birmingham"},
}


# ── DB ────────────────────────────────────────────────────────────────────────

def get_conn():
    return pymysql.connect(
        host=DB["host"], port=DB["port"], user=DB["user"],
        password=DB["password"], database=DB["database"],
        charset=DB["charset"], cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
    )


def fetch_week_events(city_slug: str, tz_name: str) -> list[dict]:
    """
    Pull up to EVENTS_PER_CITY published tribe_events for the coming 7 days,
    ordered by start date, for a given city slug.
    """
    tz       = ZoneInfo(tz_name)
    now_local = datetime.now(tz)
    week_end  = now_local + timedelta(days=7)

    now_str  = now_local.strftime("%Y-%m-%d %H:%M:%S")
    end_str  = week_end.strftime("%Y-%m-%d %H:%M:%S")

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT
                    p.ID,
                    p.post_title                                    AS title,
                    p.post_name                                     AS slug,
                    MAX(CASE WHEN pm.meta_key='_EventStartDate'     THEN pm.meta_value END) AS start_date,
                    MAX(CASE WHEN pm.meta_key='_EventEndDate'       THEN pm.meta_value END) AS end_date,
                    MAX(CASE WHEN pm.meta_key='_EventDescription'   THEN pm.meta_value END) AS description,
                    MAX(CASE WHEN pm.meta_key='_EventURL'           THEN pm.meta_value END) AS ticket_url,
                    MAX(CASE WHEN pm.meta_key='_EventCost'          THEN pm.meta_value END) AS cost,
                    MAX(CASE WHEN pm.meta_key='_EventVenueID'       THEN pm.meta_value END) AS venue_id,
                    MAX(CASE WHEN pm.meta_key='_openclaw_city'      THEN pm.meta_value END) AS city_slug,
                    MAX(CASE WHEN pm.meta_key='_thumbnail_id'       THEN pm.meta_value END) AS thumbnail_id
                FROM {WP_PREFIX}posts p
                JOIN {WP_PREFIX}postmeta pm ON p.ID = pm.post_id
                WHERE p.post_type   = 'tribe_events'
                  AND p.post_status = 'publish'
                GROUP BY p.ID
                HAVING
                    city_slug   = %s
                    AND start_date >= %s
                    AND start_date <= %s
                ORDER BY start_date ASC
                LIMIT %s
            """, (city_slug, now_str, end_str, EVENTS_PER_CITY))
            rows = cur.fetchall()

        # Enrich with venue name
        events = []
        for row in rows:
            venue_name = ""
            if row.get("venue_id"):
                with conn.cursor() as cur:
                    cur.execute(
                        f"SELECT post_title FROM {WP_PREFIX}posts WHERE ID=%s LIMIT 1",
                        (row["venue_id"],)
                    )
                    v = cur.fetchone()
                    if v:
                        venue_name = v["post_title"]

            # Resolve thumbnail URL
            image_url = ""
            if row.get("thumbnail_id"):
                with conn.cursor() as cur:
                    cur.execute(
                        f"SELECT meta_value FROM {WP_PREFIX}postmeta "
                        f"WHERE post_id=%s AND meta_key='_wp_attachment_metadata' LIMIT 1",
                        (row["thumbnail_id"],)
                    )
                    att = cur.fetchone()
                    # Fall back to guid
                    cur.execute(
                        f"SELECT guid FROM {WP_PREFIX}posts WHERE ID=%s LIMIT 1",
                        (row["thumbnail_id"],)
                    )
                    gd = cur.fetchone()
                    if gd:
                        image_url = gd["guid"]

            events.append({
                "id":          row["ID"],
                "title":       row["title"],
                "slug":        row["slug"],
                "start_date":  row["start_date"] or "",
                "end_date":    row["end_date"]   or "",
                "description": (row["description"] or row.get("post_content") or "").strip(),
                "ticket_url":  row["ticket_url"] or "",
                "cost":        row["cost"]       or "",
                "venue_name":  venue_name,
                "image_url":   image_url,
                "city_slug":   city_slug,
            })

        return events

    finally:
        conn.close()


# ── Ollama intro ──────────────────────────────────────────────────────────────

def generate_intro(city_label: str, events: list[dict]) -> str:
    """Ask Qwen3 for a 2-sentence punchy intro for the city digest."""
    if not events:
        return f"Here's what's happening in {city_label} this week."

    titles = ", ".join(e["title"] for e in events[:5])
    prompt = (
        f"You are the voice of The Local Radar, a cool local events guide for {city_label}. "
        f"Write exactly 2 short punchy sentences (under 40 words total) hyping this week's events. "
        f"Be enthusiastic and local. Do not list the events — just set the vibe. "
        f"Events this week include: {titles}. "
        f"No hashtags, no emojis, no quotes around your response."
    )

    try:
        resp = requests.post(
            f"{OLLAMA_HOST}/api/generate",
            json={
                "model":  OLLAMA_MODEL,
                "prompt": prompt,
                "stream": False,
                "options": {"num_predict": 80, "temperature": 0.8},
            },
            timeout=OLLAMA_TIMEOUT,
        )
        resp.raise_for_status()
        text = resp.json().get("response", "").strip()
        # Strip any <think> blocks Qwen3 sometimes emits
        if "<think>" in text:
            text = text.split("</think>")[-1].strip()
        return text or f"Here's what's happening in {city_label} this week."
    except Exception as e:
        log.warning("Ollama intro failed for %s: %s", city_label, e)
        return f"Here's what's happening in {city_label} this week."


# ── HTML builder ──────────────────────────────────────────────────────────────

def _fmt_date(date_str: str) -> str:
    """Format '2026-06-21 19:00:00' → 'Saturday, June 21 at 7:00 PM'"""
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d %H:%M:%S")
        time_part = dt.strftime("%-I:%M %p").lstrip("0")
        if time_part in ("12:00 AM", "00:00 AM"):
            return dt.strftime("%A, %B %-d")
        return dt.strftime("%A, %B %-d") + f" at {time_part}"
    except Exception:
        return date_str


def _event_url(event: dict, city_slug: str) -> str:
    if event.get("ticket_url"):
        return event["ticket_url"]
    return f"{TLR_BASE_URL}/events/{event['slug']}/"


def build_html(city_slug: str, city_label: str, intro: str,
               events: list[dict], week_start: str, week_end: str) -> str:
    city_url = f"{TLR_BASE_URL}/{city_slug}/"

    # Build event cards
    cards_html = ""
    for ev in events:
        url        = _event_url(ev, city_slug)
        date_str   = _fmt_date(ev["start_date"])
        venue_str  = ev["venue_name"] or ""
        cost_str   = ev["cost"] or ""
        desc       = ev["description"]
        desc_short = (desc[:120] + "…") if len(desc) > 120 else desc

        # Image block
        if ev.get("image_url"):
            img_block = f"""
            <tr>
              <td style="padding:0 0 12px 0;">
                <a href="{url}" style="display:block;">
                  <img src="{ev['image_url']}" alt="{ev['title']}"
                       width="560" style="width:100%;max-width:560px;height:auto;
                       border-radius:6px;display:block;" />
                </a>
              </td>
            </tr>"""
        else:
            img_block = ""

        cost_badge = ""
        if cost_str:
            cost_badge = f'<span style="color:#FF2D78;font-weight:600;">{cost_str}</span> &nbsp;·&nbsp; '

        cards_html += f"""
        <!-- EVENT CARD -->
        <tr>
          <td style="padding:0 0 28px 0;">
            <table width="100%" cellpadding="0" cellspacing="0" border="0"
                   style="background:#1a1a1a;border-radius:8px;overflow:hidden;">
              <tr><td style="padding:20px 24px 20px 24px;">
                <table width="100%" cellpadding="0" cellspacing="0" border="0">
                  {img_block}
                  <tr>
                    <td>
                      <a href="{url}"
                         style="font-family:'Space Grotesk',Arial,sans-serif;
                                font-size:18px;font-weight:700;color:#ffffff;
                                text-decoration:none;line-height:1.3;">
                        {ev['title']}
                      </a>
                    </td>
                  </tr>
                  <tr>
                    <td style="padding:6px 0 0 0;
                               font-family:Arial,sans-serif;font-size:13px;
                               color:#00E5FF;line-height:1.5;">
                      📅 {date_str}
                      {"<br>📍 " + venue_str if venue_str else ""}
                    </td>
                  </tr>
                  {"<tr><td style='padding:8px 0 0 0;font-family:Arial,sans-serif;font-size:13px;color:#aaaaaa;line-height:1.5;'>" + cost_badge + desc_short + "</td></tr>" if desc_short else ""}
                  <tr>
                    <td style="padding:12px 0 0 0;">
                      <a href="{url}"
                         style="display:inline-block;background:#FF2D78;color:#ffffff;
                                font-family:Arial,sans-serif;font-size:13px;font-weight:700;
                                padding:8px 18px;border-radius:4px;text-decoration:none;">
                        {"Get Tickets" if ev.get("ticket_url") else "More Info"}
                      </a>
                    </td>
                  </tr>
                </table>
              </td></tr>
            </table>
          </td>
        </tr>"""

    if not cards_html:
        cards_html = """
        <tr><td style="padding:20px 0;font-family:Arial,sans-serif;font-size:15px;
                       color:#aaaaaa;text-align:center;">
          No events found for this week. Check back soon!
        </td></tr>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>The Local Radar — {city_label} | {week_start}</title>
</head>
<body style="margin:0;padding:0;background:#0D0D0D;">
<table width="100%" cellpadding="0" cellspacing="0" border="0"
       style="background:#0D0D0D;min-height:100vh;">
  <tr><td align="center" style="padding:24px 16px;">

    <!-- CONTAINER -->
    <table width="600" cellpadding="0" cellspacing="0" border="0"
           style="max-width:600px;width:100%;">

      <!-- HEADER -->
      <tr>
        <td style="padding:0 0 28px 0;text-align:center;">
          <a href="{city_url}" style="text-decoration:none;">
            <span style="font-family:'Space Grotesk',Arial,sans-serif;
                         font-size:28px;font-weight:900;color:#00E5FF;
                         letter-spacing:-0.5px;">
              THE LOCAL RADAR
            </span>
            <br>
            <span style="font-family:Arial,sans-serif;font-size:13px;
                         color:#FF2D78;font-weight:600;letter-spacing:2px;
                         text-transform:uppercase;">
              {city_label}
            </span>
          </a>
          <div style="margin:12px auto 0;width:48px;height:2px;background:#00E5FF;"></div>
        </td>
      </tr>

      <!-- INTRO BAND -->
      <tr>
        <td style="background:#111111;border-radius:8px;padding:20px 24px;
                   margin-bottom:24px;">
          <p style="margin:0 0 4px 0;font-family:Arial,sans-serif;font-size:12px;
                    color:#FF2D78;font-weight:700;letter-spacing:2px;
                    text-transform:uppercase;">
            This Week in {city_label} &nbsp;·&nbsp; {week_start}–{week_end}
          </p>
          <p style="margin:8px 0 0 0;font-family:'Space Grotesk',Arial,sans-serif;
                    font-size:16px;color:#eeeeee;line-height:1.6;">
            {intro}
          </p>
        </td>
      </tr>

      <!-- SPACER -->
      <tr><td style="height:24px;"></td></tr>

      <!-- EVENT CARDS -->
      {cards_html}

      <!-- FOOTER -->
      <tr>
        <td style="padding:12px 0 32px 0;border-top:1px solid #222222;">
          <table width="100%" cellpadding="0" cellspacing="0" border="0">
            <tr>
              <td style="padding:20px 0 0 0;text-align:center;
                         font-family:Arial,sans-serif;font-size:12px;color:#555555;">
                <a href="{city_url}" style="color:#00E5FF;text-decoration:none;">
                  View full {city_label} calendar
                </a>
                &nbsp;·&nbsp;
                <a href="{TLR_BASE_URL}" style="color:#00E5FF;text-decoration:none;">
                  thelocalradar.com
                </a>
                <br><br>
                You're receiving this because you signed up for The Local Radar {city_label} digest.
              </td>
            </tr>
          </table>
        </td>
      </tr>

    </table>
    <!-- /CONTAINER -->

  </td></tr>
</table>
</body>
</html>"""


# ── Brevo Campaign API ────────────────────────────────────────────────────────

def _brevo_headers() -> dict:
    return {
        "api-key":      BREVO_API_KEY,
        "Content-Type": "application/json",
        "Accept":       "application/json",
    }


def send_campaign(city_slug: str, city_label: str, list_id: int,
                  subject: str, html_body: str) -> bool:
    """
    Create and immediately send a Brevo email campaign to a list.
    Brevo handles unsubscribes, open/click tracking automatically.
    The campaign is scheduled for immediate send (scheduledAt = now).
    """
    if not BREVO_API_KEY:
        log.error("BREVO_API_KEY not set — cannot send campaign")
        return False

    # Step 1: Create the campaign
    campaign_name = f"TLR {city_label} Weekly — {datetime.now().strftime('%Y-%m-%d')}"
    payload = {
        "name":   campaign_name,
        "subject": subject,
        "sender": {"name": FROM_NAME, "email": FROM_EMAIL},
        "type":   "classic",
        "htmlContent": html_body,
        "recipients": {"listIds": [list_id]},
    }

    try:
        resp = requests.post(
            f"{BREVO_API_BASE}/emailCampaigns",
            headers=_brevo_headers(),
            json=payload,
            timeout=20,
        )
        resp.raise_for_status()
        campaign_id = resp.json().get("id")
        if not campaign_id:
            log.error("Brevo did not return campaign ID for %s", city_label)
            return False
        log.info("Created Brevo campaign %d for %s", campaign_id, city_label)
    except Exception as e:
        log.error("Failed to create Brevo campaign for %s: %s", city_label, e)
        return False

    # Brief pause — Brevo needs a moment before send is accepted
    time.sleep(1)

    # Step 2: Send immediately
    try:
        resp = requests.post(
            f"{BREVO_API_BASE}/emailCampaigns/{campaign_id}/sendNow",
            headers=_brevo_headers(),
            timeout=20,
        )
        resp.raise_for_status()
        log.info("Sent Brevo campaign %d for %s", campaign_id, city_label)
        return True
    except Exception as e:
        log.error("Failed to send Brevo campaign %d for %s: %s", campaign_id, city_label, e)
        return False


# ── Main ──────────────────────────────────────────────────────────────────────

def run():
    cities = load_cities()
    if not cities:
        log.error("No cities loaded — aborting digest")
        sys.exit(1)

    now        = datetime.now()
    week_start = now.strftime("%b %-d")
    week_end   = (now + timedelta(days=6)).strftime("%b %-d")

    sent = 0
    fail = 0

    for city in cities:
        slug  = city["slug"]
        cfg   = CITY_CONFIG.get(slug, {"label": city["name"], "url_slug": slug})
        label = cfg["label"]
        tz    = city.get("timezone", "America/Chicago")

        log.info("Processing digest for %s", label)

        list_id = BREVO_LIST_IDS.get(slug)
        if not list_id:
            log.warning("No Brevo list ID for %s — skipping", slug)
            continue

        events = fetch_week_events(slug, tz)
        log.info("%s — %d events this week", label, len(events))

        intro   = generate_intro(label, events)
        log.info("%s intro: %s", label, intro[:80])

        html    = build_html(slug, label, intro, events, week_start, week_end)
        subject = f"🎯 {label} This Week — {week_start}"

        ok = send_campaign(slug, label, list_id, subject, html)
        if ok:
            sent += 1
        else:
            fail += 1

    log.info("Digest complete — campaigns sent: %d, failed: %d", sent, fail)


if __name__ == "__main__":
    run()
