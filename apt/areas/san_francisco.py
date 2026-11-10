"""San Francisco: neighborhood names and safety grades from SFPD incident reports."""

import json
import math
import time
from datetime import datetime, timedelta

import requests

from .. import CACHE_DIR, HEADERS
from ..geo import haversine_mi

NAME = "San Francisco"
CRIME_API = "https://data.sf.gov/resource/wg3w-h783.json"
CRIME_CACHE = CACHE_DIR / "crime_sf.json"

# SFPD incidents within RADIUS_MI of a listing over the last DAYS, weighted by
# how much they affect living there. Other categories (warrants, lost property,
# fraud...) are ignored.
DAYS = 90
RADIUS_MI = 0.125  # ~2 blocks
WEIGHTS = {
    "Assault": 3, "Robbery": 3, "Weapons Offense": 3, "Weapons Offence": 3,
    "Weapons Carrying Etc": 3, "Homicide": 3, "Sex Offense": 3, "Arson": 2,
    "Drug Offense": 2, "Drug Violation": 2, "Disorderly Conduct": 2, "Prostitution": 2,
    "Burglary": 1, "Motor Vehicle Theft": 1, "Malicious Mischief": 1, "Vandalism": 1,
    "Larceny Theft": 0.5,  # mostly shoplifting, so it counts less
}
# Weighted totals that map to the best and worst score (log scale in between).
# For reference: Russian Hill ~6, Nob Hill ~15, Rincon Hill ~37,
# Union Square ~220, Tenderloin core / 6th & Mission ~1000+.
BEST, WORST = 10, 1000
CELL = 0.002  # degrees; grid cell a bit bigger than RADIUS_MI

# Rough neighborhood centers; each listing gets the nearest one
HOODS = {
    "Financial District": (37.7946, -122.3999), "SoMa": (37.7785, -122.4056),
    "Rincon Hill / South Beach": (37.7852, -122.3900), "Mission Bay": (37.7706, -122.3915),
    "Chinatown": (37.7941, -122.4078), "North Beach": (37.8061, -122.4103),
    "Telegraph Hill": (37.8025, -122.4058), "Nob Hill": (37.7930, -122.4161),
    "Russian Hill": (37.8011, -122.4194), "Tenderloin": (37.7847, -122.4141),
    "Union Square": (37.7880, -122.4075), "Civic Center": (37.7793, -122.4193),
    "Hayes Valley": (37.7759, -122.4245), "Western Addition": (37.7810, -122.4330),
    "Lower Haight / Duboce": (37.7717, -122.4310), "Mission": (37.7599, -122.4148),
    "Potrero Hill": (37.7587, -122.4010), "Dogpatch": (37.7580, -122.3886),
    "Castro": (37.7609, -122.4350), "Pacific Heights": (37.7925, -122.4382),
    "Marina": (37.8037, -122.4368), "Haight-Ashbury": (37.7692, -122.4481),
    "Noe Valley": (37.7502, -122.4337), "Bernal Heights": (37.7389, -122.4152),
    "Japantown": (37.7854, -122.4294), "Laurel Heights": (37.7860, -122.4500),
    "Presidio Heights": (37.7880, -122.4560), "Inner Richmond": (37.7800, -122.4640),
    "Cole Valley": (37.7650, -122.4500), "Treasure Island": (37.8235, -122.3706),
    "Cow Hollow": (37.7980, -122.4370), "Presidio": (37.7980, -122.4660),
    "Sea Cliff": (37.7870, -122.4900), "Outer Richmond": (37.7780, -122.4940),
    "Inner Sunset": (37.7610, -122.4680), "Outer Sunset": (37.7550, -122.4950),
    "Parkside": (37.7420, -122.4930), "West Portal": (37.7400, -122.4650),
    "Twin Peaks": (37.7540, -122.4470), "Diamond Heights": (37.7430, -122.4420),
    "Glen Park": (37.7340, -122.4340), "Ingleside": (37.7200, -122.4550),
    "Lakeshore / Stonestown": (37.7270, -122.4800), "Excelsior": (37.7250, -122.4300),
    "Portola": (37.7270, -122.4070), "Visitacion Valley": (37.7150, -122.4090),
    "Bayview": (37.7300, -122.3900), "Hunters Point": (37.7280, -122.3730),
}

_index = None


def covers(listing):
    return listing.get("city") == "San Francisco" and listing.get("state") == "CA"


def neighborhood(pt):
    return min(HOODS, key=lambda h: haversine_mi(HOODS[h], pt))


def _load_incidents():
    """[lat, lon, weight] for the last DAYS of citywide incidents, cached for a day."""
    if CRIME_CACHE.exists() and time.time() - CRIME_CACHE.stat().st_mtime < 86400:
        return json.loads(CRIME_CACHE.read_text())
    since = (datetime.now() - timedelta(days=DAYS)).strftime("%Y-%m-%d")
    cats = ",".join(f"'{c}'" for c in WEIGHTS)
    params = {
        "$select": "incident_category,latitude,longitude",
        "$where": f"incident_date > '{since}' AND incident_category in ({cats}) AND latitude IS NOT NULL",
        "$limit": 200000,
    }
    r = requests.get(CRIME_API, params=params, headers=HEADERS, timeout=90)
    r.raise_for_status()
    rows = [[float(x["latitude"]), float(x["longitude"]), WEIGHTS[x["incident_category"]]] for x in r.json()]
    CRIME_CACHE.write_text(json.dumps(rows))
    return rows


def _crime_index():
    """Incidents bucketed into a lat/lon grid so lookups only scan nearby cells."""
    global _index
    if _index is None or time.time() - _index[0] > 86400:
        idx = {}
        for la, lo, w in _load_incidents():
            idx.setdefault((int(la // CELL), int(lo // CELL)), []).append((la, lo, w))
        _index = (time.time(), idx)
    return _index[1]


def safety(pt):
    """(0-1 score where 1 = quiet, weighted incident total), or (None, None) if data is unavailable."""
    try:
        idx = _crime_index()
    except (requests.RequestException, ValueError):
        return None, None
    ci, cj = int(pt[0] // CELL), int(pt[1] // CELL)
    total = sum(w for di in (-1, 0, 1) for dj in (-2, -1, 0, 1, 2)
                for la, lo, w in idx.get((ci + di, cj + dj), ())
                if haversine_mi(pt, (la, lo)) < RADIUS_MI)
    s = 1 - math.log10(max(total, BEST) / BEST) / math.log10(WORST / BEST)
    return max(0.0, min(1.0, s)), total


SAFETY_NOTE = "SFPD incidents within ~2 blocks over the last 90 days"
