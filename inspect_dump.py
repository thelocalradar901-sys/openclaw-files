"""
inspect_dump.py — prints a compact sample of matched container markup
from every file in the triage HTML dump, straight to stdout.

Run on the server (no file transfer needed):

    venv/bin/python3 inspect_dump.py

Or for just one file:

    venv/bin/python3 inspect_dump.py triage_html_dump/nashville__Ryman_Auditorium.html

For each dumped page, this shows:
  - which detection path found something (known selector vs fallback heading)
  - the first matched container's own HTML, truncated to ~1500 chars

That's enough to spot the actual markup shape without dumping full
150KB-2MB files into the terminal one at a time.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scraper  # noqa: E402
from bs4 import BeautifulSoup  # noqa: E402

DUMP_DIR = "triage_html_dump"
MAX_SNIPPET = 1500


def inspect_one(path):
    name = os.path.basename(path)
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            html = f.read()
    except Exception as e:
        print(f"\n### {name}\nCould not read file: {e}")
        return

    soup = BeautifulSoup(html, "html.parser")
    selector_hit = next((sel for sel in scraper._EVENT_SELECTORS if soup.select(sel)), None)

    if selector_hit:
        containers = scraper._dedupe_nested_matches(soup.select(selector_hit))
        via = f"selector '{selector_hit}'"
    else:
        containers = scraper._fallback_heading_containers(soup)
        via = "fallback heading containers"

    print(f"\n{'=' * 78}\n### {name}  ({len(containers)} containers via {via})\n{'=' * 78}")
    if not containers:
        print("(no containers found by either path)")
        return

    snippet = str(containers[0])
    if len(snippet) > MAX_SNIPPET:
        snippet = snippet[:MAX_SNIPPET] + f"\n...[truncated, {len(snippet)} chars total]"
    print(snippet)


def main():
    if len(sys.argv) > 1:
        inspect_one(sys.argv[1])
        return

    if not os.path.isdir(DUMP_DIR):
        print(f"No {DUMP_DIR}/ directory found here -- run this from /opt/openclaw, "
              f"or pass a specific file path as an argument.")
        sys.exit(1)

    files = sorted(f for f in os.listdir(DUMP_DIR) if f.endswith(".html"))
    print(f"Inspecting {len(files)} dumped files...")
    for fname in files:
        inspect_one(os.path.join(DUMP_DIR, fname))


if __name__ == "__main__":
    main()
