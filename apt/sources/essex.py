"""Essex Property Trust (essexapartmenthomes.com) adapter.

Uses the site's own same-origin JSON endpoints (the ones its Next.js front end calls):

  1. GET /api/locations/communities
       Full community hierarchy (state > region > metro > city > community) with
       propertyId, displayName, address, zip, "lat,lon" coordinate and communityUrl.
       One request; ~250 unique communities (each is listed twice in the tree).
  2. GET /api/properties/<propertyId>/availability?start_date=YYYY-MM-DD
         &end_date=YYYY-MM-DD&format=spa
       Floor plans for one community with beds/baths/sqft/min rent,
       available_units_count and the available units (unit number, sqft, rent,
       availability date, amenities).  The site's own widget asks for today..+60 days;
       we do the same.

robots.txt (User-agent *) has no Disallow lines, so both paths are allowed.
No key, cookie or login.  The site sits behind Vercel's firewall: plain `curl`
got a "Vercel Security Checkpoint" (HTTP 429) page, python-requests with a
browser UA did not.  If the checkpoint is served we raise EssexBlocked instead
of trying to pass it.

Coverage: Essex only operates in California and Washington (Seattle area);
fetch() returns [] elsewhere.
"""
from __future__ import annotations

import datetime as _dt
import json
import math
import re
import time

import requests

BASE_URL = "https://www.essexapartmenthomes.com"
SOURCE = "essex"
NAME = "Essex"
LICENSED = False
PRIORITY = 0.5  # the landlord's own site: preferred over listing sites on a price tie


def available():
    return True, ""

WINDOW_DAYS = 60        # same move-in window the site's availability widget uses
REQUEST_DELAY = 0.5     # seconds between per-community requests
MAX_COMMUNITIES = 120   # safety cap on availability requests per fetch()
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept": "application/json, text/plain, */*",
           "Accept-Language": "en-US,en;q=0.9"}

_RESTRICT_RE = re.compile(
    r"(income[- ]restricted|affordable|below market rate|\bBMR\b|\bAMI\b|senior|"
    r"\b55\+|\b62\+|student housing)", re.I)

REQUEST_COUNT = 0


class EssexBlocked(requests.RequestException):
    """Vercel firewall challenge served; not bypassing it."""


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


def _get_json(session, path, params=None):
    global REQUEST_COUNT
    REQUEST_COUNT += 1
    r = session.get(BASE_URL + path, params=params, headers=HEADERS, timeout=45)
    if r.headers.get("x-vercel-mitigated") or "Vercel Security Checkpoint" in r.text[:3000]:
        raise EssexBlocked(f"Vercel security checkpoint on {path} (HTTP {r.status_code})",
                           response=r)
    r.raise_for_status()
    try:
        return r.json()
    except ValueError as e:
        raise ValueError(f"non-JSON response from {path}") from e


def _communities(session):
    data = _get_json(session, "/api/locations/communities")
    found = {}

    def walk(o):
        if isinstance(o, dict):
            if "propertyId" in o and "coordinate" in o:
                found.setdefault(str(o["propertyId"]), o)
                return
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(data)
    if not found:
        raise ValueError("Essex community list is empty / unexpected shape")
    return list(found.values())


def fetch(center: tuple[float, float], radius_mi: float) -> list[dict]:
    """One dict per available floor plan in Essex communities within radius_mi."""
    lat, lon = float(center[0]), float(center[1])
    if not (-90 <= lat <= 90 and -180 <= lon <= 180) or not radius_mi > 0:
        raise ValueError("bad center/radius")
    session = requests.Session()
    near = []
    for c in _communities(session):
        try:
            la, lo = (float(x) for x in str(c.get("coordinate") or "").split(","))
        except ValueError:
            continue
        d = _haversine_mi((lat, lon), (la, lo))
        if d <= radius_mi:
            near.append((d, la, lo, c))
    near.sort(key=lambda t: t[0])
    near = near[:MAX_COMMUNITIES]

    today = _dt.date.today()
    params = {"start_date": today.isoformat(),
              "end_date": (today + _dt.timedelta(days=WINDOW_DAYS)).isoformat(),
              "format": "spa"}
    out = []
    for i, (_, la, lo, c) in enumerate(near):
        if i:
            time.sleep(REQUEST_DELAY)
        pid = str(c["propertyId"])
        res = (_get_json(session, f"/api/properties/{pid}/availability", params) or {})
        res = res.get("result") or {}
        name = c.get("displayName") or c.get("name") or ""
        for fp in res.get("floorplans") or []:
            units = [u for u in (fp.get("units") or []) if (_int(u.get("minimum_rent")) or 0) > 0]
            n_avail = _int(fp.get("available_units_count")) or len(units)
            if not units and not n_avail:
                continue
            if units:
                best = min(units, key=lambda u: _int(u.get("minimum_rent")))
                price, sqft = _int(best.get("minimum_rent")), _int(best.get("sqft"))
                unit_no = best.get("name") if len(units) == 1 else None
                dates = sorted((u.get("availability_date") or "")[:10] for u in units
                               if u.get("availability_date"))
                amen = " ".join(a for u in units for a in (u.get("amenities") or [])
                                if isinstance(a, str))
            else:
                price = _int(fp.get("minimum_rent"))
                sqft, unit_no, dates, amen = _int(fp.get("minimum_sqft")), None, [], ""
            if not price or price <= 0:
                continue
            fname = (fp.get("name") or "").strip()
            specials = "; ".join(
                str(s.get("name") or s.get("title") or s) for u in units
                for s in (u.get("specials") or []))[:150]
            desc = (f"{name}: {n_avail} available {fname} unit(s)"
                    + (f", earliest {max(dates[0], today.isoformat())}" if dates else "")
                    + (f"; {specials}" if specials else ""))
            out.append({
                "source": SOURCE,
                "source_id": f"{pid}:{fp.get('floorplan_id')}",
                "url": BASE_URL + (c.get("communityUrl") or ""),
                "address": c.get("address"),
                "unit": unit_no,
                "city": c.get("city"),
                "state": c.get("stateAbbr"),
                "zip": c.get("zipCode"),
                "lat": la, "lon": lo,
                "beds": _int(fp.get("beds")),
                "baths": _float(fp.get("baths")),
                "price": price,
                "sqft": sqft or _int(fp.get("minimum_sqft")),
                "unit_type": fname,
                "updated": today.isoformat(),
                "description": desc[:500],
                "income_restricted": bool(_RESTRICT_RE.search(f"{name} {fname} {amen}")),
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
