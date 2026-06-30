"""
test_places.py — quick standalone test of the Google Places lookup chain
(Find Place -> Place Details -> website) before wiring it into the
OpenClaw pipeline as a real tertiary-source auto-creation step.

Usage:
  python3 test_places.py "Nissan Stadium" "Nashville"
"""

import sys
import requests

API_KEY = "AIzaSyD-SSMuBHWqOgi4NAH9GufQ6ZGqwr3GmGk"

FIND_PLACE_URL = "https://maps.googleapis.com/maps/api/place/findplacefromtext/json"
DETAILS_URL    = "https://maps.googleapis.com/maps/api/place/details/json"


def find_place_id(venue_name: str, city: str) -> str | None:
    params = {
        "input": f"{venue_name} {city}",
        "inputtype": "textquery",
        "fields": "place_id",
        "key": API_KEY,
    }
    resp = requests.get(FIND_PLACE_URL, params=params, timeout=10)
    data = resp.json()
    print("Find Place response:", data)
    candidates = data.get("candidates", [])
    if not candidates:
        return None
    return candidates[0].get("place_id")


def get_website(place_id: str) -> str | None:
    params = {
        "place_id": place_id,
        "fields": "website,name",
        "key": API_KEY,
    }
    resp = requests.get(DETAILS_URL, params=params, timeout=10)
    data = resp.json()
    print("Place Details response:", data)
    result = data.get("result", {})
    return result.get("website")


if __name__ == "__main__":
    venue_name = sys.argv[1] if len(sys.argv) > 1 else "Nissan Stadium"
    city       = sys.argv[2] if len(sys.argv) > 2 else "Nashville"

    pid = find_place_id(venue_name, city)
    if not pid:
        print(f"No place_id found for '{venue_name}, {city}'")
        sys.exit(1)

    print(f"place_id: {pid}")

    website = get_website(pid)
    if website:
        print(f"Website: {website}")
    else:
        print("No website on file for this place.")
