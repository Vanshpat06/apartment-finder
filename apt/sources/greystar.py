"""Greystar (greystar.com) adapter.

Greystar's property search (/s/...) is a Next.js app backed by Coveo Commerce.
The server-rendered search page already contains the first page of Coveo
results in its React Server Component payload (self.__next_f), including every
property's lat/lon and, as "children", each currently available unit with beds,
baths, floor-plan label (ec_category) and min/max base rent.

The map view encodes its viewport in a `bb` query parameter:
    https://www.greystar.com/s/us?bb=<base64 of
        {"sw":{"lat":..,"lng":..},"ne":{"lat":..,"lng":..},"centerLat":..,"centerLng":..}>
and the server honours it, so one GET returns properties inside that box.

Only page 1 (21 properties) is server-rendered; later pages are fetched by the
browser from Coveo with a public API key.  We stay on greystar.com instead and
split the box into quadrants whenever the page reports more than 21 results
(adaptive quadtree), so every box we keep fits on one page.

robots.txt (greystar.com, User-agent *): "Allow: *", Disallow /example-pages/*
and /api/*.  /s/us is allowed (urllib.robotparser: True).  We never call /api/.
(For reference: the Coveo host platform.cloud.coveo.com explicitly Allows
/rest/organizations/*/commerce/v2/search for *, but urllib.robotparser cannot
evaluate that wildcard rule, so we don't use it.)

Gaps: the search data has no square footage (sqft=None) and no per-unit
availability date.  Properties shown without unit children ("property://"
results: leasing handled off-platform) are skipped because no availability or
price is published for them.
"""
from __future__ import annotations

import base64
import datetime as _dt
import json
import math
import re
import time

import requests

BASE_URL = "https://www.greystar.com"
SOURCE = "greystar"
NAME = "Greystar"
LICENSED = False
PRIORITY = 0.5  # the landlord's own site: preferred over listing sites on a price tie


def available():
    return True, ""

PAGE_SIZE = 21            # fixed by the site
MAX_REQUESTS = 60         # per fetch() safety cap
MAX_DEPTH = 7
REQUEST_DELAY = 1.0
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")
HEADERS = {"User-Agent": UA,
           "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
           "Accept-Language": "en-US,en;q=0.9"}

_FLIGHT_RE = re.compile(r"self\.__next_f\.push\((\[.*?\])\)</script>", re.S)
_HREF_RE = re.compile(r'href="(/[a-z0-9-]+/p_(\d+))"')
_RESTRICT_RE = re.compile(
    r"(income[- ]restricted|affordable|below market rate|\bBMR\b|senior|\b55\+|\b62\+|"
    r"student|\bLIHTC\b)", re.I)
_ROOM_RE = re.compile(r"\b(private room|shared room|by the bed|per bed|co-?living)\b", re.I)

REQUEST_COUNT = 0


class GreystarBlocked(requests.RequestException):
    pass


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


def _flight(html):
    parts = []
    for c in _FLIGHT_RE.findall(html):
        try:
            a = json.loads(c)
        except ValueError:
            continue
        if len(a) > 1 and isinstance(a[1], str):
            parts.append(a[1])
    return "".join(parts)


def _state(text, key):
    k = f'"{key}":{{"state":'
    i = text.find(k)
    if i < 0:
        return None
    obj, _ = json.JSONDecoder().raw_decode(text[i + len(k):])
    return obj


def _search_box(session, s, w, n, e):
    """Return (total, products, {property_id: path}) for one bounding box."""
    global REQUEST_COUNT
    bb = {"sw": {"lat": round(s, 6), "lng": round(w, 6)},
          "ne": {"lat": round(n, 6), "lng": round(e, 6)},
          "centerLat": round((s + n) / 2, 6), "centerLng": round((w + e) / 2, 6)}
    token = base64.b64encode(json.dumps(bb, separators=(",", ":")).encode()).decode()
    if REQUEST_COUNT:
        time.sleep(REQUEST_DELAY)
    REQUEST_COUNT += 1
    r = session.get(BASE_URL + "/s/us", params={"bb": token}, headers=HEADERS, timeout=60)
    if r.status_code in (403, 429) or "captcha" in r.text[:3000].lower():
        raise GreystarBlocked(f"greystar.com refused search (HTTP {r.status_code})", response=r)
    r.raise_for_status()
    text = _flight(r.text)
    pl, pg = _state(text, "productList"), _state(text, "pagination")
    if pl is None or pg is None:
        raise ValueError("Greystar search page had no productList/pagination state")
    links = {pid: path for path, pid in _HREF_RE.findall(r.text)}
    return int(pg.get("totalEntries") or 0), pl.get("products") or [], links


def _collect(session, box, depth, products, links):
    total, prods, lk = _search_box(session, *box)
    links.update(lk)
    if total <= PAGE_SIZE or depth >= MAX_DEPTH or REQUEST_COUNT + 4 > MAX_REQUESTS:
        for p in prods:
            products.setdefault(p.get("permanentid") or p.get("clickUri"), p)
        return total > PAGE_SIZE  # True -> truncated
    s, w, n, e = box
    ml, mg = (s + n) / 2, (w + e) / 2
    truncated = False
    for q in ((s, w, ml, mg), (s, mg, ml, e), (ml, w, n, mg), (ml, mg, n, e)):
        truncated |= _collect(session, q, depth + 1, products, links)
    return truncated


def fetch(center: tuple[float, float], radius_mi: float) -> list[dict]:
    """One dict per available unit type (property x floor-plan label x beds/baths)."""
    lat, lon = float(center[0]), float(center[1])
    if not (-90 <= lat <= 90 and -180 <= lon <= 180) or not radius_mi > 0:
        raise ValueError("bad center/radius")
    dlat = radius_mi / 69.0
    dlon = radius_mi / (69.172 * max(math.cos(math.radians(lat)), 1e-6))
    session = requests.Session()
    products, links = {}, {}
    _collect(session, (lat - dlat, lon - dlon, lat + dlat, lon + dlon), 0, products, links)

    today = _dt.date.today().isoformat()
    groups = {}
    for p in products.values():
        if not str(p.get("clickUri", "")).startswith("unit://"):
            continue  # property with no published availability
        units = p.get("children") or [p]
        pid = str(p.get("ec_item_group_id") or p["clickUri"].split("/")[2])
        for u in units:
            a = u.get("additionalFields") or {}
            la, lo = _float(a.get("latitude")), _float(a.get("longitude"))
            if la is None or lo is None or _haversine_mi((lat, lon), (la, lo)) > radius_mi:
                continue
            price = _int(a.get("min_price")) or _int(u.get("ec_price"))
            if not price or price <= 0:
                continue
            cat = (u.get("ec_category") or [""])[0] or ""
            beds, baths = _int(a.get("bedrooms")), _float(a.get("bathrooms"))
            key = (pid, cat, beds, baths)
            g = groups.get(key)
            if g is None:
                groups[key] = g = {"u": u, "a": a, "price": price, "n": 0, "brand": p.get("ec_brand")}
            g["n"] += 1
            if price < g["price"]:
                g.update(u=u, a=a, price=price)

    out = []
    for (pid, cat, beds, baths), g in groups.items():
        a, u = g["a"], g["u"]
        brand = (g["brand"] or "").strip()
        path = links.get(pid)
        banner = (a.get("image_banner_text") or "").strip()
        desc = (f"{brand}: {g['n']} available {cat or 'unit'}(s)"
                + (f"; {banner}" if banner else "")
                + (f"; min lease {a.get('min_base_rent_lease_term')} mo"
                   if a.get("min_base_rent_lease_term") else ""))
        out.append({
            "source": SOURCE,
            "source_id": f"{pid}:{cat or 'na'}:{beds}bd{baths}ba",
            "url": BASE_URL + path if path else f"{BASE_URL}/s/us",
            "address": (a.get("address") or "").strip() or None,
            "unit": (u.get("ec_name") or None) if g["n"] == 1 else None,
            "city": (a.get("city") or "").strip() or None,
            "state": a.get("state"),
            "zip": a.get("postal_code"),
            "lat": _float(a.get("latitude")), "lon": _float(a.get("longitude")),
            "beds": beds, "baths": baths, "price": g["price"], "sqft": None,
            "unit_type": cat,
            "updated": today,
            "description": desc[:500],
            "income_restricted": bool(_RESTRICT_RE.search(f"{brand} {cat} {banner}")),
            "is_room": bool(_ROOM_RE.search(f"{brand} {cat}")),
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
        print(f"radius={rad} mi count={len(rows)} properties={len({r['source_id'].split(':')[0] for r in rows})} "
              f"elapsed={dt:.1f}s requests={REQUEST_COUNT}")
        print("by beds:", dict(sorted(collections.Counter(r["beds"] for r in rows).items(),
                                      key=lambda kv: (kv[0] is None, kv[0] or 0))))
        print("no url:", sum(r["url"].endswith("/s/us") for r in rows),
              " income_restricted:", sum(r["income_restricted"] for r in rows))
        for r in rows[:3]:
            print(json.dumps(r, indent=1))
