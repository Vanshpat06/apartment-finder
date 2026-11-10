"""AvalonBay (avaloncommunities.com) adapter.

The site is an Arc XP "Fusion" app.  Its apartment-search pages (e.g.
/california/san-francisco-apartments/) render from the Fusion content source
"search", which the browser also calls directly when the map is moved:

    GET https://www.avaloncommunities.com/pf/api/v3/content/fetch/search
        ?query={"locationId":"<id>","topLeftCoords":"<lat>,<lon>","bottomRightCoords":"<lat>,<lon>"}
        &_website=avalon-communities

(robots.txt for User-agent * only disallows */contact-us, */reset-password and
*/onlineapplication/*, so /pf/api/v3/content/fetch/search is allowed.)

One request returns, for the bounding box, every community (name, address,
lat/lon, per-bedroom "starting at" prices) plus every available unit in the
AvalonBay-operated communities (unit number, floor plan, beds, baths, sqft,
availability date, lowest price).  No pagination or cap was observed (a
~100x80 mile box around Boston returned 59 communities / 646 units in one
response).  No key, cookie or login is needed.

The search also lists partner "EQR" (Equity Residential) communities
(communityType == "EQR", isExpansionCommunity).  For those the response has no
unit list, only per-bedroom starting prices, and the link goes to
equityapartments.com.  We emit one row per bedroom count for them (baths/sqft
unknown); set INCLUDE_PARTNER_COMMUNITIES = False to drop them.
"""
from __future__ import annotations

import datetime as _dt
import json
import math
import re
import urllib.parse

import requests

BASE_URL = "https://www.avaloncommunities.com"
SEARCH_URL = BASE_URL + "/pf/api/v3/content/fetch/search"
SITE = "avalon-communities"
SOURCE = "avalonbay"
NAME = "AvalonBay"
LICENSED = False
PRIORITY = 0.5  # the landlord's own site: preferred over listing sites on a price tie


def available():
    return True, ""

INCLUDE_PARTNER_COMMUNITIES = True
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept": "application/json, text/plain, */*",
           "Accept-Language": "en-US,en;q=0.9"}

_RESTRICT_RE = re.compile(
    r"(income[- ]restricted|affordable|below market|\bBMR\b|senior|\b55\+|\b62\+|student)", re.I)

REQUEST_COUNT = 0


def _bbox(center, radius_mi):
    lat, lon = center
    dlat = radius_mi / 69.0
    dlon = radius_mi / (69.172 * max(math.cos(math.radians(lat)), 1e-6))
    return lat + dlat, lon - dlon, lat - dlat, lon + dlon  # top, left, bottom, right


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


def _abs_url(u):
    if not u:
        return None
    u = urllib.parse.urljoin(BASE_URL + "/", u)
    # drop tracking params on partner links
    p = urllib.parse.urlsplit(u)
    q = [(k, v) for k, v in urllib.parse.parse_qsl(p.query) if not k.startswith("utm_")]
    return urllib.parse.urlunsplit((p.scheme, p.netloc, p.path, urllib.parse.urlencode(q), ""))


def _search(session, top, left, bottom, right):
    global REQUEST_COUNT
    tl, br = f"{top:.5f},{left:.5f}", f"{bottom:.5f},{right:.5f}"
    # The browser always sends the page's locationId alongside the map box.  The
    # site's content cache (max-age 120 s) appears to key on a coarse form of the
    # query, so bbox-only queries for nearby boxes can come back with another box's
    # cached result.  A locationId naming the exact box keeps results box-specific;
    # when coordinates are present the backend filters by the box, not the id.
    q = {"locationId": f"bbox_{tl}_{br}", "topLeftCoords": tl, "bottomRightCoords": br}
    params = {"query": json.dumps(q, separators=(",", ":")), "_website": SITE}
    REQUEST_COUNT += 1
    r = session.get(SEARCH_URL, params=params, headers=HEADERS, timeout=60)
    r.raise_for_status()
    try:
        data = r.json()
        comms = data["communityResults"]["communities"].get("items") or []
        units = (data.get("unitResults") or {}).get("items") or []
    except (ValueError, KeyError, TypeError, AttributeError) as e:
        raise ValueError("unexpected AvalonBay search response shape") from e
    return comms, units


def _unit_price(u):
    for key in ("startingAtPricesUnfurnished", "startingAtPricesFurnished"):
        p = ((u.get(key) or {}).get("prices") or {})
        v = _int(p.get("price"))
        if v and v > 0:
            return v, _int(p.get("netEffectivePrice")), key == "startingAtPricesFurnished"
    return None, None, False


def fetch(center: tuple[float, float], radius_mi: float) -> list[dict]:
    """One dict per available unit type (community x floor plan x beds/baths) for
    AvalonBay communities within radius_mi of center (lat, lon)."""
    lat, lon = float(center[0]), float(center[1])
    if not (-90 <= lat <= 90 and -180 <= lon <= 180) or not radius_mi > 0:
        raise ValueError("bad center/radius")
    session = requests.Session()
    comms, units = _search(session, *_bbox((lat, lon), radius_mi))

    today = _dt.date.today().isoformat()
    by_id = {}
    for c in comms:
        co = c.get("coordinates") or {}
        la, lo = _float(co.get("latitude")), _float(co.get("longitude"))
        if la is None or lo is None or _haversine_mi((lat, lon), (la, lo)) > radius_mi:
            continue
        by_id[c.get("communityId")] = c

    out = []
    # --- AvalonBay-operated communities: group available units by type -------------
    groups: dict[tuple, dict] = {}
    comm_with_units = set()
    for u in units:
        cid = u.get("communityId")
        c = by_id.get(cid)
        if c is None:
            continue
        price, net, furnished_only = _unit_price(u)
        if price is None:
            continue
        comm_with_units.add(cid)
        beds, baths = _int(u.get("bedroomNumber")), _float(u.get("bathroomNumber"))
        plan = ((u.get("floorPlan") or {}).get("name") or "").strip()
        key = (cid, plan, beds, baths)
        g = groups.get(key)
        if g is None or price < g["price"]:
            groups[key] = {"u": u, "price": price, "net": net, "furnished_only": furnished_only,
                           "n": (g["n"] if g else 0)}
        groups[key]["n"] += 1

    for (cid, plan, beds, baths), g in groups.items():
        c, u = by_id[cid], g["u"]
        addr = c.get("address") or {}
        uaddr = (u.get("address") or {})
        avail = (u.get("availableDateUnfurnished") or u.get("availableDateFurnished") or "")[:10]
        chars = ", ".join(x for x in (u.get("characteristics") or []) if isinstance(x, str))
        promos = "; ".join(p.get("promotionTitle", "") for p in (u.get("promotions") or [])
                           if isinstance(p, dict) and p.get("promotionTitle"))
        desc = (f"{c.get('name')}: {g['n']} available {plan or ''} unit(s)"
                + (f", earliest {avail}" if avail else "")
                + (f" ({chars})" if chars else "")
                + (f"; net effective ${g['net']}" if g["net"] and g["net"] < g["price"] else "")
                + ("; furnished" if g["furnished_only"] else "")
                + (f"; {promos}" if promos else ""))
        name_txt = f"{c.get('name')} {chars}"
        out.append({
            "source": SOURCE,
            "source_id": f"{cid}:{plan or 'na'}:{beds}bd{baths}ba",
            "url": _abs_url(c.get("url")),
            "address": addr.get("addressLine1"),
            "unit": u.get("unitName") if g["n"] == 1 else None,
            "city": addr.get("city") or uaddr.get("city"),
            "state": addr.get("state") or uaddr.get("state"),
            "zip": addr.get("zip") or uaddr.get("zip"),
            "lat": _float((c.get("coordinates") or {}).get("latitude")),
            "lon": _float((c.get("coordinates") or {}).get("longitude")),
            "beds": beds, "baths": baths, "price": g["price"],
            "sqft": _int(u.get("squareFeet")),
            "unit_type": plan,
            "updated": today,
            "description": desc[:500],
            "income_restricted": bool(_RESTRICT_RE.search(name_txt)),
            "is_room": False,
        })

    # --- partner (EQR) communities / any community without a unit list ----------------
    if INCLUDE_PARTNER_COMMUNITIES:
        for cid, c in by_id.items():
            if cid in comm_with_units or not c.get("apartmentsAvailable"):
                continue
            if c.get("communityType") == "AVB":
                continue  # AVB community whose units were all filtered out: nothing to report
            addr = c.get("address") or {}
            per_bed = ((c.get("prices") or {}).get("perBedroom") or {})
            for b, p in per_bed.items():
                b, p = _int(b), _int(p)
                if b is None or not p or p <= 0:
                    continue
                out.append({
                    "source": SOURCE,
                    "source_id": f"{cid}:{b}bd",
                    "url": _abs_url(c.get("url")),
                    "address": addr.get("addressLine1"), "unit": None,
                    "city": addr.get("city"), "state": addr.get("state"), "zip": addr.get("zip"),
                    "lat": _float((c.get("coordinates") or {}).get("latitude")),
                    "lon": _float((c.get("coordinates") or {}).get("longitude")),
                    "beds": b, "baths": None, "price": p, "sqft": None, "unit_type": "",
                    "updated": today,
                    "description": (f"{c.get('name')} ({c.get('communityType')} partner community "
                                    f"listed on avaloncommunities.com; {c.get('apartmentsAvailable')} "
                                    f"available; starting-at price for {b} bed)"),
                    "income_restricted": bool(_RESTRICT_RE.search(c.get("name") or "")),
                    "is_room": False,
                })
    return out


if __name__ == "__main__":
    import collections
    import sys
    import time

    for rad in [float(x) for x in sys.argv[1:]] or [3.0]:
        REQUEST_COUNT = 0
        t0 = time.time()
        rows = fetch((37.78977, -122.39693), rad)
        dt = time.time() - t0
        print(f"radius={rad} mi count={len(rows)} communities={len({r['url'] for r in rows})} "
              f"elapsed={dt:.1f}s requests={REQUEST_COUNT}")
        print("by beds:", dict(sorted(collections.Counter(r["beds"] for r in rows).items(),
                                      key=lambda kv: (kv[0] is None, kv[0] or 0))))
        for r in rows[:2] + rows[-1:]:
            print(json.dumps(r, indent=1))
