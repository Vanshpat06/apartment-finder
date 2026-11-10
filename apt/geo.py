"""Distances and place search."""

import math

import requests

GEOCODE_API = "https://nominatim.openstreetmap.org/search"
GEOCODE_HEADERS = {"User-Agent": "apartment-finder/1.0 (personal use)"}


def haversine_mi(a, b):
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 3958.8 * 2 * math.asin(math.sqrt(h))


def box_around(center, radius_mi):
    """(south, west, north, east) of the square that contains a circle of radius_mi."""
    dlat = radius_mi / 69.0
    dlon = radius_mi / (69.0 * math.cos(math.radians(center[0])))
    return center[0] - dlat, center[1] - dlon, center[0] + dlat, center[1] + dlon


def geocode(query, limit=6):
    """US places matching query, best first: [{label, lat, lon}]."""
    params = {"q": query, "format": "jsonv2", "limit": limit, "countrycodes": "us"}
    r = requests.get(GEOCODE_API, params=params, headers=GEOCODE_HEADERS, timeout=15)
    r.raise_for_status()
    return [{"label": x["display_name"], "lat": float(x["lat"]), "lon": float(x["lon"])} for x in r.json()]


def short_label(label):
    return ",".join(label.split(",")[:2])
