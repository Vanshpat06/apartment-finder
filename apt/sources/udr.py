"""UDR (udr.com) adapter.

UDR's site is server-rendered (EPiServer/IIS); the data we need is embedded in
two kinds of public pages, so no JSON API is called:

  1. Market pages, e.g. /san-francisco-bay-area-apartments/
       One <div class="community-card" data-cid data-name data-lat data-long
       data-rent-min ...> per community in the market, with the community link,
       street address and "City, ST ZIP".
  2. Community pricing pages, e.g.
       /san-francisco-bay-area-apartments/san-francisco/399-fremont/apartments-pricing/
       embed `window.udr.jsonObjPropertyViewModel = {...}` whose `floorPlans`
       list holds every floor plan with available units (beds, baths, sqft,
       rent min/max, available date, per-unit rent, isAffordable flag).  Plans
       with nothing available are in a separate `unavailableFloorPlans` list.

The list of markets comes from /search-apartments/ (links like /austin-apartments/).
Market pages don't expose a market centre, so MARKET_CENTERS below gives an
approximate centre per known market; we only open market pages whose centre is
within radius + MARKET_REACH_MI of the query (unknown new markets are always
opened).  Pricing pages are ~1 MB each, fetched only for communities inside
the radius whose card shows a rent (data-rent-min > 0).

robots.txt: https://www.udr.com/robots.txt answers HTTP 406 (an IIS MIME error
page) for every Accept header tried.  Under RFC 9309 a 4xx robots.txt means "no
restrictions", and Python's urllib.robotparser treats it the same way
(allow_all), so all paths used are allowed.  Nothing needs login or a key.

Coverage: UDR's ~21 US metro markets only.
"""
from __future__ import annotations

import datetime as _dt
import html as _html
import json
import math
import re
import time

import requests

BASE_URL = "https://www.udr.com"
SOURCE = "udr"
NAME = "UDR"
LICENSED = False
PRIORITY = 0.5  # the landlord's own site: preferred over listing sites on a price tie


def available():
    return True, ""

REQUEST_DELAY = 0.7
MARKET_REACH_MI = 70     # communities can sit this far from our market centre point
MAX_COMMUNITIES = 80
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")
HEADERS = {"User-Agent": UA,
           "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
           "Accept-Language": "en-US,en;q=0.9"}

# approximate centres of UDR markets (slug of /<slug>-apartments/)
MARKET_CENTERS = {
    "austin": (30.27, -97.74), "baltimore": (39.29, -76.61), "boston": (42.36, -71.06),
    "dallas": (32.85, -96.85), "denver": (39.74, -104.99), "inland-empire": (34.00, -117.40),
    "los-angeles": (34.05, -118.30), "nashville": (36.16, -86.78),
    "new-york-city": (40.73, -73.99), "orange-county": (33.70, -117.83),
    "orlando": (28.54, -81.38), "philadelphia": (39.95, -75.17), "portland": (45.52, -122.68),
    "richmond": (37.54, -77.44), "salinas": (36.68, -121.66), "san-diego": (32.80, -117.15),
    "san-francisco-bay-area": (37.60, -122.20), "seattle": (47.61, -122.25),
    "tampa": (27.95, -82.46), "washington-dc": (38.90, -77.04),
    "west-palm-beach": (26.71, -80.06),
}

_RESTRICT_RE = re.compile(
    r"(income[- ]restricted|affordable|below market rate|\bBMR\b|senior|\b55\+|\b62\+|"
    r"student housing)", re.I)
_CARD_RE = re.compile(r'<div class="community-card community-card--\d+"(.*?)(?=<div class="community-card community-card--|<footer|\Z)', re.S)
_PVM_KEY = "window.udr.jsonObjPropertyViewModel = "

REQUEST_COUNT = 0


def _haversine_mi(a, b):
    lat1, lon1 = map(math.radians, a)
    lat2, lon2 = map(math.radians, b)
    h = (math.sin((lat2 - lat1) / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2)
    return 2 * 3958.8 * math.asin(math.sqrt(h))


def _int(x):
    try:
        return None if x is None else int(round(float(x)))
    except (TypeError, ValueError):
        return None


def _float(x):
    try:
        return None if x is None else float(x)
    except (TypeError, ValueError):
        return None


def _get(session, path):
    global REQUEST_COUNT
    if REQUEST_COUNT:
        time.sleep(REQUEST_DELAY)
    REQUEST_COUNT += 1
    r = session.get(BASE_URL + path, headers=HEADERS, timeout=60)
    if r.status_code in (403, 429) or "cf-chl" in r.text[:5000]:
        raise requests.HTTPError(f"UDR refused {path} (HTTP {r.status_code}); not retrying",
                                 response=r)
    r.raise_for_status()
    return r.text


def _markets(session):
    page = _get(session, "/search-apartments/")
    slugs = sorted(set(re.findall(r'href="(?:https://www\.udr\.com)?/([a-z0-9-]+)-apartments/"', page)))
    slugs = [s for s in slugs if s not in ("search", "why-choose-udr")]
    if not slugs:
        raise ValueError("no UDR markets found on /search-apartments/")
    return slugs


def _attr(block, name):
    m = re.search(r'\b' + re.escape(name) + r'="([^"]*)"', block)
    return _html.unescape(m.group(1)) if m else None


def _cards(page):
    out = []
    for m in _CARD_RE.finditer(page):
        b = m.group(1)
        link = re.search(r'class="community-card__title" href="([^"]+)"', b)
        street = re.search(r'community-card__number-street">([^<]*)<', b)
        csz = re.search(r'community-card__city-state">([^<]*)<', b)
        city = state = zc = None
        if csz:
            mm = re.match(r"\s*(.*?),\s*([A-Z]{2})\s*(\d{5})?", _html.unescape(csz.group(1)))
            if mm:
                city, state, zc = mm.group(1), mm.group(2), mm.group(3)
        out.append({
            "cid": _attr(b, "data-cid"), "name": _attr(b, "data-name"),
            "lat": _float(_attr(b, "data-lat")), "lon": _float(_attr(b, "data-long")),
            "rent_min": _int(_attr(b, "data-rent-min")),
            "coming_soon": (_attr(b, "data-coming-soon") or "").lower() == "true",
            "path": _html.unescape(link.group(1)) if link else None,
            "street": _html.unescape(street.group(1)).strip() if street else None,
            "city": city, "state": state, "zip": zc,
        })
    return out


def _ms_date(s):
    m = re.search(r"/Date\((-?\d+)", s or "")
    if not m:
        return None
    return _dt.datetime.fromtimestamp(int(m.group(1)) / 1000, _dt.timezone.utc).date().isoformat()


def _property_vm(page):
    i = page.find(_PVM_KEY)
    if i < 0:
        raise ValueError("pricing page has no jsonObjPropertyViewModel")
    obj, _ = json.JSONDecoder().raw_decode(page[i + len(_PVM_KEY):])
    return obj


def fetch(center: tuple[float, float], radius_mi: float) -> list[dict]:
    """One dict per available floor plan in UDR communities within radius_mi."""
    lat, lon = float(center[0]), float(center[1])
    if not (-90 <= lat <= 90 and -180 <= lon <= 180) or not radius_mi > 0:
        raise ValueError("bad center/radius")
    session = requests.Session()
    comms = {}
    for slug in _markets(session):
        mc = MARKET_CENTERS.get(slug)
        if mc and _haversine_mi((lat, lon), mc) > radius_mi + MARKET_REACH_MI:
            continue
        for c in _cards(_get(session, f"/{slug}-apartments/")):
            if c["lat"] is None or c["lon"] is None or not c["path"]:
                continue
            d = _haversine_mi((lat, lon), (c["lat"], c["lon"]))
            if d <= radius_mi and c["cid"] not in comms:
                comms[c["cid"]] = (d, c)

    today = _dt.date.today().isoformat()
    out = []
    for _, c in sorted(comms.values(), key=lambda t: t[0])[:MAX_COMMUNITIES]:
        if c["coming_soon"] or not c["rent_min"]:
            continue  # card shows no available apartments
        path = c["path"].rstrip("/") + "/apartments-pricing/"
        vm = _property_vm(_get(session, path))
        for fp in vm.get("floorPlans") or []:
            units = [u for u in (fp.get("units") or [])
                     if u.get("isAvailable", True) and (_int(u.get("rentMin") or u.get("rent")) or 0) > 0]
            if not units:
                continue
            best = min(units, key=lambda u: _int(u.get("rentMin") or u.get("rent")))
            price = _int(best.get("rentMin") or best.get("rent"))
            dates = sorted(d for d in (_ms_date(u.get("availableDate")) for u in units) if d)
            affordable = any(u.get("isAffordable") for u in units)
            name = (fp.get("Name") or "").strip()
            desc = (f"{c['name']}: {len(units)} available {name} unit(s)"
                    + (f", earliest {max(dates[0], today)}" if dates else "")
                    + ("; income-restricted (affordable) unit" if affordable else ""))
            out.append({
                "source": SOURCE,
                "source_id": f"{c['cid']}:{fp.get('id')}",
                "url": BASE_URL + path,
                "address": c["street"],
                "unit": (best.get("marketingName") or None) if len(units) == 1 else None,
                "city": c["city"], "state": c["state"], "zip": c["zip"],
                "lat": c["lat"], "lon": c["lon"],
                "beds": _int(fp.get("bedRooms")),
                "baths": _float(fp.get("bathRooms")),
                "price": price,
                "sqft": _int(best.get("sqFt")) or _int(fp.get("sqFtMin")),
                "unit_type": name,
                "updated": today,
                "description": desc[:500],
                "income_restricted": affordable or bool(_RESTRICT_RE.search(c["name"] or "")),
                "is_room": False,
            })
    return out


if __name__ == "__main__":
    import collections
    import sys

    for rad in [float(x) for x in sys.argv[1:]] or [3.0]:
        REQUEST_COUNT = 0
        t0 = time.time()
        rows = fetch((37.78977, -122.39693), rad)
        dt = time.time() - t0
        print(f"radius={rad} mi count={len(rows)} communities={len({r['url'] for r in rows})} "
              f"elapsed={dt:.1f}s requests={REQUEST_COUNT}")
        print("by beds:", dict(sorted(collections.Counter(r["beds"] for r in rows).items(),
                                      key=lambda kv: (kv[0] is None, kv[0] or 0))))
        print("income_restricted:", sum(r["income_restricted"] for r in rows))
        for r in rows[:3]:
            print(json.dumps(r, indent=1))
