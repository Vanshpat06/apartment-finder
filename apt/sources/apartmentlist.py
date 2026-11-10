"""Apartment List (apartmentlist.com), read through the same search calls its website makes.

Allowed by apartmentlist.com/robots.txt (checked 2026-09-29), but not a licensed
API: the site's terms don't grant automated access, and these internal routes
can change without notice.

Uses the site's own public tRPC endpoints (the same calls the search page makes,
no login, no bot challenge encountered):

  1. POST https://www.apartmentlist.com/api/trpc/search.postCategories?batch=1
     body {"0":{"json":{"searchOptions":{"location_polygon":<GeoJSON Polygon [lon,lat]>,
                                          "categories":["E0"], "fields":[...]}}}}
     -> every matching property in the polygon in ONE response (no pagination,
        no cap observed: 339 props @3mi, 770 @10mi); each has rental_id, lat/lon,
        min price per bed count, slug.
  2. GET  https://www.apartmentlist.com/api/trpc/listings.fetchListingCardsById?batch=1
          &input={"0":{"json":{"rentalIds":[...<=100...],"preferences":{}}}}
     -> per-property card: formatted_address, floorplans (bed/bath/sqft/price,
        units with unit numbers and updated_at), occupancy_types, summary text.
"""
from __future__ import annotations

import json
import math
import re
import time

import requests

NAME = "Apartment List"
LICENSED = False
PRIORITY = 1


def available():
    return True, ""


UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")
BASE = "https://www.apartmentlist.com"
SEARCH_FIELDS = ["lat", "lon", "prices", "display_name", "slug", "show_price", "contract"]
CARD_BATCH = 100
MIN_INTERVAL = 1.0  # seconds between requests (politeness)
TIMEOUT = 30

_RESTRICTED_OCC = {"affordable", "senior", "student", "military"}
_RESTRICTED_RE = re.compile(
    r"income[- ]restrict|income[- ]qualif|affordable housing|below[- ]market[- ]rate|\bBMR\b|"
    r"\bsection 8\b|\bLIHTC\b|\bsenior (?:living|housing|community|apartments|residence)|"
    r"\b(?:55|62)\s*\+|\b(?:55|62) and (?:over|older)|age[- ]restricted|"
    r"student (?:housing|living|apartments)|off[- ]campus housing|\bper bed\b|by the bed",
    re.I)
_ROOM_RE = re.compile(
    r"\b(?:private|furnished|shared|single|master)\s+(?:bed)?room\b|(?:^|\b(?:a|one|1)\s+)bedroom for rent\b|"
    r"\brooms? for rent\b|\broommates?\b|\bco-?living\b|\bhousemates?\b|"
    r"\bshared (?:house|home|apartment|housing|living)\b|\bby the room\b|\bper room\b",
    re.I)
_UNIT_RE = re.compile(r"(?:\bunit|\bapt\.?|\bapartment|\bsuite|#)\s*([A-Za-z0-9-]{1,8})\s*$", re.I)


class _Client:
    def __init__(self):
        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": UA,
            "Accept": "application/json",
            "x-trpc-source": "nextjs-react",
            "Origin": BASE,
            "Referer": BASE + "/",
        })
        self._last = 0.0

    def _wait(self):
        dt = time.monotonic() - self._last
        if dt < MIN_INTERVAL:
            time.sleep(MIN_INTERVAL - dt)
        self._last = time.monotonic()

    def _parse(self, r: requests.Response, proc: str):
        r.raise_for_status()
        try:
            j = r.json()[0]
        except (ValueError, IndexError, KeyError) as e:
            raise ValueError(f"{proc}: non-JSON/unexpected response ({r.text[:200]!r})") from e
        if "error" in j:
            raise ValueError(f"{proc}: tRPC error {json.dumps(j['error'])[:300]}")
        data = j["result"]["data"]["json"]
        if isinstance(data, dict) and data.get("status") not in (None, "success"):
            raise ValueError(f"{proc}: status {data.get('status')}")
        return data

    def mutate(self, proc: str, inp: dict):
        self._wait()
        r = self.s.post(f"{BASE}/api/trpc/{proc}", params={"batch": 1},
                        data=json.dumps({"0": {"json": inp}}),
                        headers={"Content-Type": "application/json"}, timeout=TIMEOUT)
        return self._parse(r, proc)

    def query(self, proc: str, inp: dict):
        self._wait()
        r = self.s.get(f"{BASE}/api/trpc/{proc}",
                       params={"batch": 1, "input": json.dumps({"0": {"json": inp}}, separators=(",", ":"))},
                       timeout=TIMEOUT)
        return self._parse(r, proc)


def _haversine_mi(lat1, lon1, lat2, lon2):
    R = 3958.7613
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def _circle_polygon(lat, lon, radius_mi, n=36):
    # circumscribe the circle (vertices at r / cos(pi/n)) so nothing inside is cut off;
    # exact radius is enforced afterwards with haversine.
    r = radius_mi / math.cos(math.pi / n) * 1.01
    dlat = r / 69.0
    dlon = r / (69.17 * max(math.cos(math.radians(lat)), 1e-6))
    pts = [[round(lon + dlon * math.sin(2 * math.pi * i / n), 6),
            round(lat + dlat * math.cos(2 * math.pi * i / n), 6)] for i in range(n)]
    pts.append(pts[0])
    return {"type": "Polygon", "coordinates": [pts]}


def _parse_address(fa: str | None):
    """'255 King Street, San Francisco, CA 94107' -> (street, city, state, zip)."""
    if not fa:
        return None, None, None, None
    parts = [p.strip() for p in fa.split(",") if p.strip()]
    state = zip_ = city = None
    if parts:
        m = re.match(r"^([A-Z]{2})\s*(\d{5})?(?:-\d{4})?$", parts[-1])
        if m:
            state, zip_ = m.group(1), m.group(2)
            parts = parts[:-1]
    if len(parts) >= 2:
        city = parts[-1]
        parts = parts[:-1]
    street = ", ".join(parts) or None
    return street, city, state, zip_


def _with_unit(street, unit):
    if not street or not unit:
        return street
    if re.search(r"(?:#|\b(?:unit|apt|suite|ste)\.?)\s*" + re.escape(unit) + r"\b", street, re.I):
        return street
    return f"{street} #{unit}"


def _int(x):
    try:
        v = int(round(float(x)))
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


def _float(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _date(s):
    return s[:10] if isinstance(s, str) and re.match(r"\d{4}-\d{2}-\d{2}", s) else None


def _search(client: _Client, lat, lon, radius_mi):
    so = {"location_polygon": _circle_polygon(lat, lon, radius_mi),
          "categories": ["E0"], "fields": SEARCH_FIELDS}
    data = client.mutate("search.postCategories", {"searchOptions": so})
    cats = data.get("data") or []
    listings = []
    for c in cats:
        got = c.get("listings") or []
        tc = c.get("total_count")
        if tc is not None and tc > len(got):
            # Not observed in testing (no cap up to 770 props); surface it rather than silently truncate.
            raise ValueError(f"search returned {len(got)} of {tc} listings (server cap); use a smaller radius")
        listings.extend(got)
    return listings


def _cards(client: _Client, ids):
    out = {}
    for i in range(0, len(ids), CARD_BATCH):
        chunk = ids[i:i + CARD_BATCH]
        data = client.query("listings.fetchListingCardsById", {"rentalIds": chunk, "preferences": {}})
        for c in (data.get("data") or {}).get("listings") or []:
            out[c.get("rental_id")] = c
    return out


def _rows_for(sl: dict, card: dict | None, lat0, lon0):
    rid = sl["rental_id"]
    slug = (card or {}).get("slug") or sl.get("slug") or ""
    url = BASE + "/" + slug.lstrip("/") if slug else None
    name = (card or {}).get("display_name") or sl.get("display_name") or ""
    lat = _float((card or {}).get("lat")) or _float(sl.get("lat"))
    lon = _float((card or {}).get("lon")) or _float(sl.get("lon"))
    street, city, state, zip_ = _parse_address((card or {}).get("formatted_address"))
    contract = (card or {}).get("contract") or sl.get("contract")

    # Individual-owner ("free" contract) listings put the unit in the title, e.g. "181 O'Farrell Street - 313Unit 313".
    unit = None
    if contract == "free":
        m = _UNIT_RE.search(name)
        if m:
            unit = m.group(1)
    address = _with_unit(street, unit)

    summ = ((card or {}).get("sampleSummary") or {}).get("text") or ""
    # For individual listings the display name is the landlord's ad title -> most useful text.
    description = (name + (" - " + summ if summ else "")) if contract == "free" else (summ or name)
    description = (description[:400].rstrip() or None)
    occ = {str(o).lower() for o in ((card or {}).get("occupancy_types") or [])}
    text = f"{name} {summ}"
    income_restricted = bool(occ & _RESTRICTED_OCC) or bool(_RESTRICTED_RE.search(text))
    is_room_listing = "co-living" in occ or bool(_ROOM_RE.search(name))
    listing_updated = _date((card or {}).get("updated_at"))

    base = dict(source="apartmentlist", url=url, address=address, unit=unit, city=city, state=state,
                zip=zip_, lat=lat, lon=lon, description=description,
                income_restricted=income_restricted)

    rows = []
    covered_beds = set()
    fps = list(((card or {}).get("priceToFloorplans") or {}).values())
    for fp in fps:
        units = fp.get("units") or []
        prices = [p for p in [_int(fp.get("price"))] + [_int(u.get("price")) for u in units] if p]
        if not prices:  # price 0 & no units => not currently available
            continue
        beds = fp.get("bed")
        beds = int(beds) if isinstance(beds, (int, float)) else None
        sqft = _int(fp.get("sqft")) or _int(fp.get("sqft_max"))
        if not sqft:
            sq = [_int(u.get("sqft")) for u in units if _int(u.get("sqft"))]
            sqft = min(sq) if sq else None
        upd = max((_date(u.get("updated_at")) for u in units if _date(u.get("updated_at"))), default=None)
        fp_unit = unit
        if fp_unit is None and contract != "free" and len(units) == 1 and len(fps) == 1:
            cand = (units[0].get("display_name") or "").strip()
            fp_unit = cand if re.fullmatch(r"[A-Za-z0-9-]{1,8}", cand) else None
        row = dict(base)
        row.update(source_id=f"{rid}:{fp.get('id')}" if fp.get("id") is not None else f"{rid}:b{beds}",
                   unit=fp_unit,
                   address=_with_unit(street, fp_unit),
                   beds=beds, baths=_float(fp.get("bath")), price=min(prices), sqft=sqft,
                   unit_type=fp.get("name") or "", updated=upd or listing_updated,
                   is_room=is_room_listing)
        rows.append(row)
        if beds is not None:
            covered_beds.add(beds)

    # Fallback / gap-fill: the search result carries min price per bed count ("0".."3"; 3 = 3+).
    # Used when no card came back, or a bed count's floorplan was lost (floorplans are keyed by price).
    by_bed = ((card or {}).get("priceRange") or {}).get("byBedCount") or {}
    fallback = {}
    for k, v in by_bed.items():
        p = _int((v or {}).get("minPrice"))
        if p:
            fallback[int(k)] = p
    if not card:
        for k, p in (sl.get("prices") or {}).items():
            if _int(p) and sl.get("show_price", True):
                fallback.setdefault(int(k), _int(p))
    for beds, price in sorted(fallback.items()):
        if beds in covered_beds or (beds == 3 and any(b >= 3 for b in covered_beds)):
            continue
        row = dict(base)
        row.update(source_id=f"{rid}:b{beds}", beds=beds, baths=None, price=price, sqft=None,
                   unit_type="", updated=listing_updated, is_room=is_room_listing)
        rows.append(row)

    keys = ["source", "source_id", "url", "address", "unit", "city", "state", "zip", "lat", "lon",
            "beds", "baths", "price", "sqft", "unit_type", "updated", "description",
            "income_restricted", "is_room"]
    return [{k: r.get(k) for k in keys} for r in rows]


def fetch(center: tuple[float, float], radius_mi: float) -> list[dict]:
    lat0, lon0 = float(center[0]), float(center[1])
    if not (-90 <= lat0 <= 90 and -180 <= lon0 <= 180) or not radius_mi or radius_mi <= 0:
        raise ValueError("bad center/radius")
    client = _Client()
    found = _search(client, lat0, lon0, radius_mi)
    # exact radius filter (polygon is a slightly larger circumscribed 36-gon)
    seen, props = set(), []
    for sl in found:
        rid = sl.get("rental_id")
        la, lo = _float(sl.get("lat")), _float(sl.get("lon"))
        if not rid or rid in seen or la is None or lo is None:
            continue
        if _haversine_mi(lat0, lon0, la, lo) <= radius_mi:
            seen.add(rid)
            props.append(sl)
    cards = _cards(client, [p["rental_id"] for p in props])
    out = []
    for sl in props:
        out.extend(_rows_for(sl, cards.get(sl["rental_id"]), lat0, lon0))
    return out


if __name__ == "__main__":
    import collections
    import sys

    radii = [float(a) for a in sys.argv[1:]] or [3.0]
    for rad in radii:
        t = time.time()
        rows = fetch((37.78977, -122.39693), rad)
        el = time.time() - t
        print(f"radius={rad}mi rows={len(rows)} properties={len({r['source_id'].split(':')[0] for r in rows})} "
              f"elapsed={el:.1f}s")
        print("by beds:", dict(sorted(collections.Counter(r["beds"] for r in rows).items(),
                                      key=lambda kv: (kv[0] is None, kv[0] or 0))))
        print("income_restricted:", sum(r["income_restricted"] for r in rows),
              "is_room:", sum(r["is_room"] for r in rows),
              "missing sqft:", sum(r["sqft"] is None for r in rows),
              "missing zip:", sum(r["zip"] is None for r in rows))
        for r in rows[:1] + rows[len(rows) // 2:len(rows) // 2 + 1] + [x for x in rows if x["is_room"]][:1]:
            print(json.dumps(r, indent=1))
