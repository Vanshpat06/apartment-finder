"""RentCast: licensed nationwide rental listings API (free Developer plan: 50 requests/month).

Get a free key at https://app.rentcast.io/app/api (no credit card), then either
    export RENTCAST_API_KEY=your_key
or put the key alone in cache/../rentcast_key.txt (the project folder).

To stay on the free plan, this module counts its own requests per calendar
month and stops at MONTHLY_BUDGET, and caches each area for CACHE_HOURS.
"""

import json
import os
import threading
import time
from datetime import date
from pathlib import Path
from urllib.parse import quote_plus

import requests

from .. import CACHE_DIR
from ..geo import haversine_mi

NAME = "RentCast"
LICENSED = True
PRIORITY = 0

API = "https://api.rentcast.io/v1/listings/rental/long-term"
PAGE_SIZE = 500          # API maximum
MONTHLY_BUDGET = 45      # free plan allows 50; keep a few spare
MAX_PAGES_PER_SEARCH = 6
CACHE_HOURS = 24
KEY_FILE = Path(__file__).resolve().parents[2] / "rentcast_key.txt"
USAGE_FILE = CACHE_DIR / "rentcast_usage.json"

_lock = threading.Lock()


def _key():
    key = os.environ.get("RENTCAST_API_KEY", "").strip()
    if not key and KEY_FILE.exists():
        key = KEY_FILE.read_text().strip()
    return key


def _usage():
    try:
        u = json.loads(USAGE_FILE.read_text())
    except (FileNotFoundError, ValueError):
        u = {}
    month = date.today().strftime("%Y-%m")
    return u if u.get("month") == month else {"month": month, "requests": 0}


def requests_left():
    return MONTHLY_BUDGET - _usage()["requests"]


def available():
    if not _key():
        return False, "no API key (free at rentcast.io/api)"
    if requests_left() <= 0:
        return False, "free monthly request budget used up"
    return True, ""


def _get(params):
    with _lock:
        u = _usage()
        if u["requests"] >= MONTHLY_BUDGET:
            raise ValueError("RentCast free monthly budget used up")
        u["requests"] += 1
        USAGE_FILE.write_text(json.dumps(u))
    r = requests.get(API, params=params, headers={"X-Api-Key": _key(), "Accept": "application/json"}, timeout=40)
    r.raise_for_status()
    return r.json()


def fetch(center, radius_mi):
    cache = CACHE_DIR / f"rentcast_{center[0]:.3f}_{center[1]:.3f}_{radius_mi:g}.json"
    if cache.exists() and time.time() - cache.stat().st_mtime < CACHE_HOURS * 3600:
        return json.loads(cache.read_text())

    rows = []
    for page in range(MAX_PAGES_PER_SEARCH):
        batch = _get({"latitude": center[0], "longitude": center[1], "radius": radius_mi,
                      "status": "Active", "limit": PAGE_SIZE, "offset": page * PAGE_SIZE})
        rows += batch
        if len(batch) < PAGE_SIZE:
            break

    out = []
    for x in rows:
        if x.get("price") is None or x.get("bedrooms") is None or x.get("latitude") is None:
            continue
        pt = (x["latitude"], x["longitude"])
        if haversine_mi(center, pt) > radius_mi:
            continue
        full = x.get("formattedAddress") or ""
        street = " ".join(p for p in (x.get("addressLine1"), x.get("addressLine2")) if p)
        out.append({
            "source": "rentcast",
            "source_id": str(x.get("id")),
            # RentCast has no public listing page, so link to a web search for the address
            "url": "https://www.google.com/search?q=" + quote_plus(f"{full} for rent"),
            "address": street or full.split(",")[0],
            "unit": x.get("addressLine2"),
            "city": x.get("city", ""),
            "state": x.get("state", ""),
            "zip": x.get("zipCode", ""),
            "lat": pt[0],
            "lon": pt[1],
            "beds": int(x["bedrooms"]),
            "baths": x.get("bathrooms"),
            "price": int(x["price"]),
            "sqft": x.get("squareFootage"),
            "unit_type": x.get("propertyType") or "",
            "updated": (x.get("lastSeenDate") or x.get("listedDate") or "")[:10] or None,
            "description": "",
            "income_restricted": False,
            "is_room": x.get("propertyType") == "Room",
        })
    cache.write_text(json.dumps(out))
    return out
