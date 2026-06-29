#!/usr/bin/env python3
"""Quick one-off test: confirm the Google Places API (New) key works."""

import requests

api_key = "AIzaSyD4yOOnxiBGFgUgRQzflmLtLk3Ysgapnts"

url = "https://places.googleapis.com/v1/places:searchNearby"
headers = {
    "Content-Type": "application/json",
    "X-Goog-Api-Key": api_key,
    "X-Goog-FieldMask": "places.displayName,places.websiteUri",
}
body = {
    "includedTypes": ["museum"],
    "maxResultCount": 10,
    "locationRestriction": {
        "circle": {
            "center": {"latitude": 33.5186, "longitude": -86.8104},
            "radius": 20000.0,
        }
    },
}

resp = requests.post(url, headers=headers, json=body, timeout=30)
print("STATUS:", resp.status_code)
print(resp.text)
