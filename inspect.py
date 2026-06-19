import requests
from bs4 import BeautifulSoup

r = requests.get(
    "https://hitonecafe.com/events/",
    headers={"User-Agent": "Mozilla/5.0 (compatible; OpenClaw/1.0)"},
    timeout=20,
)
soup = BeautifulSoup(r.text, "html.parser")

classes = set()
for tag in soup.find_all(class_=True):
    for c in tag.get("class", []):
        cl = c.lower()
        if "event" in cl or "card" in cl or "list" in cl:
            classes.add((tag.name, c))

for tag, c in sorted(classes):
    print(tag, c)
