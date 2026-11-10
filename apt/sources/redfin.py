"""Redfin rentals, read from the map search its website uses, with per-unit prices from floor plans."""

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import requests

from .. import CACHE_DIR, HEADERS
from ..geo import box_around, haversine_mi

NAME = "Redfin"
LICENSED = False
# Redfin's robots.txt disallows /stingray/, so it only supplements the other sources
PRIORITY = 2

BASE = "https://www.redfin.com"
SEARCH_API = BASE + "/stingray/api/v1/search/rentals"
FLOORPLAN_API = BASE + "/stingray/api/v1/rentals/{}/floorPlans"
SEARCH_CAP = 350  # Redfin returns at most this many buildings per search
MAX_SPLITS = 6    # a 20-mile box split 6 times gives ~0.3-mile tiles, enough for dense downtowns
WORKERS = 6

FLOORPLAN_CACHE = CACHE_DIR / "redfin_floorplans.json"
FLOORPLAN_TTL = 12 * 3600


def available():
    return True, ""


def search_box(south, west, north, east, depth=0):
    """All rental buildings in a lat/lon box, splitting it into quarters while Redfin caps the results."""
    poly = f"{west} {south},{east} {south},{east} {north},{west} {north},{west} {south}"
    params = {
        "al": 1, "isRentals": "true", "num_homes": SEARCH_CAP, "ord": "redfin-recommended-asc",
        "page_number": 1, "poly": poly, "status": 9, "uipt": "1,2,3,4", "use_map_home_api": "true", "v": 8,
    }
    r = requests.get(SEARCH_API, params=params, headers=HEADERS, timeout=40)
    r.raise_for_status()
    data = r.json()
    homes = data.get("homes", [])
    if data.get("numMatchedHomes", 0) <= len(homes) or depth >= MAX_SPLITS:
        return homes
    mid_lat, mid_lon = (south + north) / 2, (west + east) / 2
    quarters = [(south, west, mid_lat, mid_lon), (south, mid_lon, mid_lat, east),
                (mid_lat, west, north, mid_lon), (mid_lat, mid_lon, north, east)]
    out = []
    for q in quarters:
        out += search_box(*q, depth + 1)
    return out


def _point(home):
    c = home["homeData"]["addressInfo"]["centroid"]["centroid"]
    return c["latitude"], c["longitude"]


class _FloorPlanCache:
    def __init__(self):
        self.lock = threading.Lock()
        try:
            self.data = json.loads(FLOORPLAN_CACHE.read_text())
        except (FileNotFoundError, ValueError):
            self.data = {}

    def get(self, rental_id):
        hit = self.data.get(rental_id)
        return hit[1] if hit and time.time() - hit[0] < FLOORPLAN_TTL else None

    def put(self, rental_id, units):
        with self.lock:
            self.data[rental_id] = [time.time(), units]

    def save(self):
        with self.lock:
            now = time.time()
            self.data = {k: v for k, v in self.data.items() if now - v[0] < FLOORPLAN_TTL}
            FLOORPLAN_CACHE.write_text(json.dumps(self.data))


_cache = _FloorPlanCache()


def floor_plans(rental_id):
    """[beds, price, sqft, name, baths] for each available unit type in a building (cached)."""
    cached = _cache.get(rental_id)
    if cached is not None:
        return cached
    r = requests.get(FLOORPLAN_API.format(rental_id), headers=HEADERS, timeout=20)
    if r.status_code != 200:
        return []
    groups = r.json().get("unitTypesByBedroom", [])
    types = next((g.get("availableUnitTypes", []) for g in groups if g.get("bedroomTitle") == "All"), [])
    out = []
    for t in types:
        if not t.get("rentPriceMin"):
            continue
        units = [u for u in t.get("units", []) if u.get("rentPrice")]
        cheapest = min(units, key=lambda u: u["rentPrice"]) if units else {}
        baths = (t.get("fullBaths") or 0) + 0.5 * (t.get("halfBaths") or 0) or None
        out.append([t.get("bedrooms"), t["rentPriceMin"], cheapest.get("sqft") or t.get("sqftMin"), t.get("name", ""), baths])
    _cache.put(rental_id, out)
    return out


def fetch(center, radius_mi):
    homes = search_box(*box_around(center, radius_mi))
    buildings = {h["homeData"]["propertyId"]: h for h in homes}.values()
    buildings = [h for h in buildings
                 if h.get("rentalExtension", {}).get("rentPriceRange") and haversine_mi(center, _point(h)) <= radius_mi]

    def units_for(h):
        rent = h["rentalExtension"]
        beds, price, sqft = rent.get("bedRange", {}), rent["rentPriceRange"], rent.get("sqftRange", {})
        if beds.get("min") == beds.get("max") and price["min"] == price["max"]:
            return h, [[beds.get("min"), price["min"], sqft.get("min"), "", rent.get("bathRange", {}).get("min")]]
        return h, floor_plans(rent["rentalId"])

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        results = list(pool.map(units_for, buildings))
    _cache.save()

    out = []
    for h, units in results:
        home, rent = h["homeData"], h["rentalExtension"]
        addr = home["addressInfo"]
        lat, lon = _point(h)
        restricted = bool(rent.get("isIncomeRestricted") or rent.get("isSeniorLiving") or rent.get("isStudent"))
        for beds, price, sqft, name, baths in units:
            if beds is None or not price:
                continue
            out.append({
                "source": "redfin",
                "source_id": f"{home['propertyId']}:{beds}:{name}",
                "url": BASE + home["url"],
                "address": addr.get("formattedStreetLine", ""),
                "unit": addr.get("unitNumber"),
                "city": addr.get("city", ""),
                "state": addr.get("state", ""),
                "zip": addr.get("zip", ""),
                "lat": lat,
                "lon": lon,
                "beds": int(beds),
                "baths": baths,
                "price": int(price),
                "sqft": int(sqft) if sqft else None,
                "unit_type": name,
                "updated": rent.get("lastUpdated", "")[:10] or None,
                "description": rent.get("description", ""),
                "income_restricted": restricted,
                "is_room": False,
            })
    return out
