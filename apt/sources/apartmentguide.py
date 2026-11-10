"""ApartmentGuide (RentPath) adapter -- robots.txt-compliant.

ApartmentGuide is the same RentPath Next.js app / listing index as Rent.com, but
it serves 50 listings per search page (Rent.com: 30).  We only fetch public
search-results pages that robots.txt allows for User-agent *:

    https://www.apartmentguide.com/<type>/<State>/<City>/[page-N/]?bbox=minLng,minLat,maxLng,maxLat

and parse their embedded __NEXT_DATA__ JSON (pageData.location.listingSearch),
exactly the same shape as Rent.com.  The `bbox` parameter is the site's own
map-search parameter; it replaces the city filter (any valid city slug works,
results come from the box), so one slug covers any US area.  Property types
are separate slugs: apartments, condos, houses (incl. duplex/triplex),
townhomes.  Pagination via /page-N/; `total` in the payload tells us when to stop.

NOT used (disallowed by robots.txt): /graphql/, /api/maps/*, /api/rift*,
/rentals/, /*?limit, /*?*min_price=*, /*?*max_price=* ...  Consequence: floor
plans come from the search-card data (beds, baths, price range, sqft range per
plan, plus plan name when present); no free-text description on search cards.

The site sits behind CloudFront.  If it ever answers with an AWS WAF challenge
(HTTP 202 + x-amzn-waf-action) or 403/405/429 we raise ApartmentGuideBlocked
rather than trying to solve or retry through it.
"""
from __future__ import annotations

import json
import math
import re
import time

import requests

SOURCE = "apartmentguide"
BASE_URL = "https://www.apartmentguide.com"
# Any valid city slug works because bbox overrides the city filter.
SLUG_PATH = "California/San-Francisco"
PROPERTY_SLUGS = ["apartments", "condos", "houses", "townhomes"]
PAGE_SIZE = 50            # fixed by the site (resultsPerPage)
MAX_PAGES_PER_TYPE = 100  # safety cap (5000 listings per type)
REQUEST_DELAY = 1.0       # seconds between consecutive requests
# The site's AWS WAF (same rule as Rent.com) served a challenge (HTTP 202,
# x-amzn-waf-action: challenge) after ~37 requests in ~4 min / 26 in ~85 s.
# We never retry through it; instead we keep at most MAX_IN_WINDOW requests in
# any trailing WINDOW seconds (shared across fetch() calls in one process).
MAX_IN_WINDOW = 18
WINDOW = 360.0
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")
HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

_NEXT_RE = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)
_ROOM_RE = re.compile(
    r"\b(rooms? for rent|private room|shared (?:room|house|home|apartment|unit|kitchen|"
    r"bath(?:room)?)|roommates?|co-?living|room in (?:a|an|the)|single room|"
    r"furnished room)\b", re.I)
_RESTRICT_RE = re.compile(
    r"(income[- ]restricted|income[- ]qualified|affordable housing|below market rate|"
    r"\bBMR\b|section 8|tax credit|\bLIHTC\b|% AMI|area median income|senior|\b55\+|\b62\+|"
    r"age[- ]restricted|student housing|students only)", re.I)
_UNIT_RE = re.compile(r"\s*(?:#|\b(?:apt|unit|ste|suite)\.?\s*#?)\s*([\w-]+)\s*$", re.I)
_KEYS = ["source", "source_id", "url", "address", "unit", "city", "state", "zip",
         "lat", "lon", "beds", "baths", "price", "sqft", "unit_type", "updated",
         "description", "income_restricted", "is_room"]

# Updated by fetch() for reporting.
stats = {"requests": 0}


NAME = "ApartmentGuide"
LICENSED = False
PRIORITY = 1   # robots.txt allows the search pages used here
SLOW = True    # paced under the site's rate limit: ~20 s for 3 miles, ~12 minutes for 10


def available():
    return True, ""


class ApartmentGuideBlocked(requests.RequestException):
    """The site's WAF returned a bot challenge; we do not attempt to solve it."""


def _bbox(center, radius_mi):
    lat, lon = center
    dlat = radius_mi / 69.0
    dlon = radius_mi / (69.172 * max(math.cos(math.radians(lat)), 1e-6))
    return [lon - dlon, lat - dlat, lon + dlon, lat + dlat]


def _haversine_mi(a, b):
    lat1, lon1 = map(math.radians, a)
    lat2, lon2 = map(math.radians, b)
    h = (math.sin((lat2 - lat1) / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2)
    return 2 * 3958.8 * math.asin(math.sqrt(h))


_recent: list[float] = []   # monotonic timestamps of our recent requests


def _pace():
    now = time.monotonic()
    _recent[:] = [t for t in _recent if now - t < WINDOW]
    wait = 0.0
    if _recent:
        wait = max(wait, _recent[-1] + REQUEST_DELAY - now)
    if len(_recent) >= MAX_IN_WINDOW:
        wait = max(wait, _recent[-MAX_IN_WINDOW] + WINDOW - now)
    if wait > 0:
        time.sleep(wait)
    _recent.append(time.monotonic())


def _get_page(session, ptype, page, bbox_str):
    path = f"/{ptype}/{SLUG_PATH}/" + (f"page-{page}/" if page > 1 else "")
    _pace()
    r = session.get(BASE_URL + path, params={"bbox": bbox_str}, headers=HEADERS,
                    timeout=60, allow_redirects=True)
    stats["requests"] += 1
    if r.headers.get("x-amzn-waf-action") or r.status_code in (202, 403, 405, 429):
        raise ApartmentGuideBlocked(
            f"apartmentguide.com bot protection responded (HTTP {r.status_code}, "
            f"x-amzn-waf-action={r.headers.get('x-amzn-waf-action')}) after "
            f"{stats['requests']} requests; not bypassing", response=r)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    m = _NEXT_RE.search(r.text)
    if not m:
        raise ValueError(f"no __NEXT_DATA__ on {r.url}")
    try:
        data = json.loads(m.group(1))
        loc = data["props"]["pageProps"]["pageData"]["location"]
    except (ValueError, KeyError, TypeError) as e:
        raise ValueError(f"unexpected __NEXT_DATA__ shape on {r.url}") from e
    return (loc or {}).get("listingSearch") or {}


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


def _to_rows(l):
    """Search-card listing -> one row per unit type.  Same logic as the Rent.com
    adapter (identical RentPath data shape), plus floor-plan names when present."""
    loc = l.get("location") or {}
    address = (l.get("address") or (l.get("addressFull") or "").split(",")[0]).strip() or None
    unit = None
    if address:
        m = _UNIT_RE.search(address)
        if m:
            unit = m.group(1)
    highlights = [h for h in (l.get("uniqueHighlights") or []) if h]
    amen = [a for a in (l.get("amenitiesHighlighted") or []) if a]
    badges = [b for b in (l.get("categoryBadges") or []) if isinstance(b, str)]
    desc_parts = [l.get("name") or ""] + highlights
    desc = "; ".join(p for p in desc_parts if p) or None
    flag_text = " ".join(badges + amen + highlights + [l.get("name") or ""])
    income_restricted = bool(l.get("incomeRestrictions")) or bool(_RESTRICT_RE.search(flag_text))
    is_room = bool(_ROOM_RE.search(" ".join([l.get("name") or ""] + highlights)))
    updated = (l.get("updatedAt") or "")[:10] or None
    path = l.get("urlPathname")
    url = BASE_URL + "/" + path.lstrip("/") if path else None
    base = {
        "source": SOURCE, "url": url, "address": address, "unit": unit,
        "city": loc.get("city"), "state": loc.get("stateAbbr"),
        "zip": loc.get("zip") or l.get("zipCode"),
        "lat": _float(loc.get("lat")), "lon": _float(loc.get("lng")),
        "updated": updated, "description": desc[:500] if desc else None,
        "income_restricted": income_restricted, "is_room": is_room,
    }
    plans = l.get("floorPlans") or []
    if l.get("__typename") == "Building" and not plans:
        # Single unit in a building: text summaries only (as on Rent.com).
        disp = ((l.get("photosWithAttribution") or {}).get("displayName") or "").strip()
        addr = address
        if not unit and disp and re.search(r"\d", disp) and len(disp) <= 16:
            unit = re.sub(r"^(?:#|apt\.?|apartment|unit)\s*", "", disp, flags=re.I).strip() or None
            if unit and address:
                addr = f"{address} #{unit}"
        bed_text = l.get("bedText") or ""
        bed_m = re.search(r"(\d+)", bed_text)
        beds = int(bed_m.group(1)) if bed_m else (0 if "studio" in bed_text.lower() else None)
        bath_m = re.search(r"(\d+(?:\.\d+)?)", l.get("bathText") or "")
        sq_m = re.search(r"([\d,]+)", l.get("squareFeetText") or "")
        price = _int(l.get("price"))
        row = {**base, "address": addr, "unit": unit, "source_id": str(l["id"]),
               "beds": beds, "baths": float(bath_m.group(1)) if bath_m else None,
               "price": price if price and price > 0 else None,
               "sqft": int(sq_m.group(1).replace(",", "")) if sq_m else None,
               "unit_type": ""}
        return [{k: row[k] for k in _KEYS}]
    # Plans without any price are typically not currently offered; drop them when the
    # listing has at least one priced plan.
    priced = [fp for fp in plans if _int((fp.get("priceRange") or {}).get("min"))
              or _int((fp.get("priceRange") or {}).get("max"))]
    if priced:
        plans = priced
    best = {}
    for fp in plans:
        pr = fp.get("priceRange") or {}
        sq = fp.get("sqFtRange") or {}
        beds, baths = _int(fp.get("bedCount")), _float(fp.get("bathCount"))
        sqft = _int(sq.get("min")) or _int(sq.get("max"))
        price = _int(pr.get("min")) or _int(pr.get("max"))
        if price is not None and price <= 0:
            price = None
        key = (beds, baths, sqft)
        if key not in best or (price or 10**9) < (best[key]["price"] or 10**9):
            best[key] = {"beds": beds, "baths": baths, "price": price, "sqft": sqft,
                         "unit_type": (fp.get("name") or "").strip()}
    rows = []
    for (beds, baths, sqft), v in best.items():
        sid = f"{l['id']}:{beds}bd_{baths}ba_{sqft or 0}sf"
        rows.append({**base, "source_id": sid, **v})
    if not rows:
        for bc in l.get("bedCountData") or []:
            b = _int(bc.get("beds"))
            p = _int((bc.get("prices") or {}).get("low"))
            rows.append({**base, "source_id": f"{l['id']}:{b}bd", "beds": b, "baths": None,
                         "price": p if p and p > 0 else None, "sqft": None, "unit_type": ""})
    if not rows:
        p = _int(l.get("price"))
        rows.append({**base, "source_id": str(l["id"]), "beds": None, "baths": None,
                     "price": p if p and p > 0 else None, "sqft": None, "unit_type": ""})
    return [{k: r[k] for k in _KEYS} for r in rows]


def fetch(center: tuple[float, float], radius_mi: float) -> list[dict]:
    """Return one dict per unit type for every ApartmentGuide rental within
    radius_mi of center (lat, lon)."""
    lat, lon = float(center[0]), float(center[1])
    if not (-90 <= lat <= 90 and -180 <= lon <= 180) or not radius_mi > 0:
        raise ValueError("bad center/radius")
    bbox_str = ",".join(f"{v:.5f}" for v in _bbox((lat, lon), radius_mi))
    session = requests.Session()
    listings: dict[str, dict] = {}
    stats["requests"] = 0
    for ptype in PROPERTY_SLUGS:
        total = None
        for page in range(1, MAX_PAGES_PER_TYPE + 1):
            ls = _get_page(session, ptype, page, bbox_str)
            if ls is None:
                break
            if total is None:
                total = ls.get("total") or 0
            batch = [x for x in (ls.get("listings") or []) if x and x.get("id")]
            for x in batch:
                listings.setdefault(str(x["id"]), x)
            if not batch or page * PAGE_SIZE >= total:
                break

    out = []
    for l in listings.values():
        loc = l.get("location") or {}
        la, lo = _float(loc.get("lat")), _float(loc.get("lng"))
        if la is None or lo is None or _haversine_mi((lat, lon), (la, lo)) > radius_mi:
            continue
        if l.get("offMarket"):
            continue
        out.extend(_to_rows(l))
    return out


if __name__ == "__main__":
    import collections
    import sys

    for rad in [float(x) for x in sys.argv[1:]] or [3.0]:
        t0 = time.time()
        rows = fetch((37.78977, -122.39693), rad)
        dt = time.time() - t0
        print(f"radius={rad} mi  count={len(rows)}  properties={len({r['url'] for r in rows})}"
              f"  requests={stats['requests']}  elapsed={dt:.1f}s")
        print("by beds:", dict(sorted(collections.Counter(r["beds"] for r in rows).items(),
                                      key=lambda kv: (kv[0] is None, kv[0] or 0))))
        print("income_restricted:", sum(r["income_restricted"] for r in rows),
              " is_room:", sum(r["is_room"] for r in rows),
              " no price:", sum(r["price"] is None for r in rows),
              " named plans:", sum(bool(r["unit_type"]) for r in rows))
        for r in rows[:3]:
            print(json.dumps(r, indent=1))
