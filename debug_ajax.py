import sys, json
sys.path.insert(0, '/opt/openclaw')
import scraper

source = {
    "name": "Ryman Auditorium",
    "ajax_url_template": "https://www.ryman.com/events/events_ajax/{offset}?category=0&venue=0&team=0&exclude=&per_page=12&came_from_page=event-list-page",
    "ajax_per_page": 12,
}
url = source["ajax_url_template"].format(offset=0)
print("Fetching:", url)

resp = scraper._request_get(url, headers=scraper._ajax_headers_for(source), timeout=scraper.TIMEOUT, source_name="debug")
print("HTTP status-ish OK, raw text length:", len(resp.text))
print("First 200 chars of raw response:")
print(repr(resp.text[:200]))
print()

try:
    fragment_html = json.loads(resp.text)
    print("json.loads SUCCEEDED. Decoded HTML length:", len(fragment_html))
    print("First 200 chars of decoded HTML:")
    print(fragment_html[:200])
except Exception as e:
    print("json.loads FAILED:", type(e).__name__, e)
    fragment_html = resp.text
    print("Falling back to raw text, length:", len(fragment_html))

print()
print("=== Running _parse_heuristic on the fragment ===")
events = scraper._parse_heuristic(fragment_html, url, source, "nashville", "Nashville")
print(f"_parse_heuristic returned {len(events)} events")
for e in events[:5]:
    print(" ->", e["title"], "|", e["start_date"])
