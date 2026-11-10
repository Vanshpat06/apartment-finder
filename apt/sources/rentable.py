"""Rentable (rentable.co, formerly ABODO) listing-source adapter.

fetch(center, radius_mi) -> list[dict], one dict per currently available floor plan.

How it works (all paths checked against https://www.rentable.co/robots.txt, User-agent *):
  1. /list-search?lat=..&lng=..&zoom=..&ne_bounds=LAT,LNG&sw_bounds=LAT,LNG&page=N
     Server-rendered AMP HTML, 30 property tiles per page, "next_page" link until the end.
     This is the bounding-box search the site's own map "list view" links to. (The map's JSON
     endpoint /search/get_property_results is Disallowed in robots.txt, so it is NOT used.)
  2. One property page per tile (e.g. /san-francisco-ca/azure): JSON-LD (address, zip, geo,
     description) + server-rendered floor-plan accordion (beds, baths, price, sqft, available
     units, "Managed By"). The page's JSON state endpoint /properties/<id>/listing-details is
     Disallowed in robots.txt, so it is NOT used.
Coverage: nationwide US (Rentable lists in thousands of US cities).
"""
from __future__ import annotations

import html
import json
import math
import re
import time
from datetime import date, timedelta
from urllib.parse import urlencode, urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests

SOURCE = "rentable"
NAME = "Rentable"
LICENSED = False
PRIORITY = 1   # robots.txt allows the list-search and property pages used here
SLOW = True    # one property page per building: a 10-mile search takes ~30 minutes the first time

# Parsed property pages are cached so repeat and nearby searches don't re-download them
PROPERTY_CACHE_HOURS = 24
SAVE_EVERY = 25


def available():
    return True, ""

BASE = "https://www.rentable.co"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")
MAX_PAGES = 400            # safety cap on search pages (30 properties each)
KEYS = ("source", "source_id", "url", "address", "unit", "city", "state", "zip", "lat", "lon",
        "beds", "baths", "price", "sqft", "unit_type", "updated", "description",
        "income_restricted", "is_room")


class RentableBlocked(requests.RequestException):
    """Raised when the site answers with a bot challenge / block instead of content."""


class RobotsDisallowed(ValueError):
    pass


# --------------------------------------------------------------------------- robots.txt
class _Robots:
    """urllib.robotparser plus Google-style '*' / '$' wildcard rules (robotparser only
    does prefix matching, so rules like 'Disallow: /properties/*/listing-details' would be
    silently ignored by it)."""

    def __init__(self, text: str):
        self.rp = RobotFileParser()
        self.rp.parse(text.splitlines())
        self.rules: list[tuple[bool, str]] = []
        applies = False
        for line in text.splitlines():
            line = line.split("#", 1)[0].strip()
            if ":" not in line:
                continue
            k, v = (s.strip() for s in line.split(":", 1))
            k = k.lower()
            if k == "user-agent":
                applies = v == "*"
            elif applies and k in ("allow", "disallow") and v:
                self.rules.append((k == "allow", v))

    @staticmethod
    def _match(pattern: str, path: str) -> bool:
        rx = re.escape(pattern).replace(r"\*", ".*")
        if rx.endswith(r"\$"):
            rx = rx[:-2] + "$"
        if not pattern.startswith(("/", "*")):
            rx = ".*" + rx
        return re.match(rx, path) is not None

    def allowed(self, url: str) -> bool:
        if not self.rp.can_fetch("*", url):
            return False
        p = urlparse(url)
        path = (p.path or "/") + (("?" + p.query) if p.query else "")
        best = None  # longest matching rule wins; Allow wins ties
        for allow, pat in self.rules:
            if self._match(pat, path):
                if best is None or len(pat) > len(best[1]) or (len(pat) == len(best[1]) and allow):
                    best = (allow, pat)
        return best is None or best[0]


# --------------------------------------------------------------------------- HTTP
def _looks_blocked(body: str) -> bool:
    head = body[:5000]
    return bool(re.search(r"px-captcha|cf-chl|/cdn-cgi/challenge|g-recaptcha|hcaptcha|Access Denied|"
                          r"verify you are (a )?human", head, re.I)) and "floor-plan" not in body and "serp-item" not in body


class _Client:
    def __init__(self, delay: float):
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA,
                               "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                               "Accept-Language": "en-US,en;q=0.9"})
        self.delay = delay
        self.count = 0
        self._last = 0.0
        r = self._raw(BASE + "/robots.txt")
        self.robots = _Robots(r.text if r.status_code == 200 else "")

    def _raw(self, url: str) -> requests.Response:
        wait = self._last + self.delay - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()
        self.count += 1
        return self.s.get(url, timeout=30)

    def get(self, url: str) -> str:
        if not self.robots.allowed(url):
            raise RobotsDisallowed(f"robots.txt disallows {url}")
        for attempt in range(4):
            r = self._raw(url)
            if r.status_code == 200:
                if _looks_blocked(r.text):
                    self.last_blocked = r.text
                    raise RentableBlocked(f"bot challenge at {url}")
                return r.text
            if r.status_code in (429, 500, 502, 503, 504) and attempt < 3:
                # pace down (honour Retry-After), never try to get around a challenge
                ra = r.headers.get("Retry-After", "")
                time.sleep(float(ra) if ra.isdigit() else 5 * (attempt + 1))
                continue
            if r.status_code in (401, 403):
                raise RentableBlocked(f"HTTP {r.status_code} at {url}")
            r.raise_for_status()
        raise requests.HTTPError(f"gave up on {url}")


# --------------------------------------------------------------------------- helpers
def _text(s: str) -> str:
    s = re.sub(r"<(svg|style|script|template)\b.*?</\1>", " ", s, flags=re.S | re.I)
    s = re.sub(r"<sup>.*?</sup>", "", s, flags=re.S)
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s))).strip()


def _int(s) -> int | None:
    m = re.search(r"\d[\d,]*", s or "")
    return int(m.group().replace(",", "")) if m else None


def _haversine_mi(a, b) -> float:
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 3958.8 * 2 * math.asin(math.sqrt(h))


def _rel_date(s: str | None) -> str | None:
    """'Updated 6 minutes ago' / 'Updated over 1 month ago' -> YYYY-MM-DD."""
    if not s:
        return None
    m = re.search(r"(\d+|an?|one)\s+(minute|hour|day|week|month|year)s?\s+ago", s, re.I)
    if not m:
        return None
    n = 1 if not m.group(1).isdigit() else int(m.group(1))
    days = {"minute": 0, "hour": 0, "day": 1, "week": 7, "month": 30, "year": 365}[m.group(2).lower()] * n
    if m.group(2).lower() == "hour" and n >= 24:
        days = n // 24
    return (date.today() - timedelta(days=days)).isoformat()


_RESTRICTED_RX = re.compile(
    r"income[- ]restricted|income[- ]qualified|income limits?|affordable housing|\bBMR\b|below[- ]market[- ]rate|"
    r"\bLIHTC\b|tax credit|section 42|\bsenior(s)? (community|living|housing|apartments)|\b55\s*\+|\b62\s*\+|"
    r"\bage[- ]restricted|student (housing|living|community)|\bper[- ]bed\b|by the bed", re.I)
_ROOM_RX = re.compile(
    r"private room|shared room|room for rent|rooms for rent|\bSRO\b|roommates?\b|co-?living|shared (house|home|housing|"
    r"apartment|bath|bathroom|kitchen)|room in (a|an|our|shared)|bedroom in (a|an|shared)|single room|furnished room", re.I)


# --------------------------------------------------------------------------- search pages
def _bbox(center, radius_mi):
    lat, lon = center
    dlat = radius_mi / 69.05
    dlon = radius_mi / (69.17 * max(math.cos(math.radians(lat)), 0.01))
    return (lat + dlat, lon + dlon), (lat - dlat, lon - dlon)


def _zoom(radius_mi: float) -> int:
    return max(8, min(15, int(round(14 - math.log2(max(radius_mi, 0.1))))))


def _parse_tiles(page: str) -> list[dict]:
    out = []
    for chunk in re.split(r'<amp-layout\s+id="serp-item-property-id-', page)[1:]:
        pid = chunk[:chunk.find('"')]
        href = re.search(r'class="prop-page-link serp-item-link"\s+href="([^"]+)"', chunk) or \
            re.search(r'class="property-name[^"]*">\s*<a href="([^"]+)"', chunk)
        if not href:
            continue
        kws = [html.unescape(k).strip() for k in re.findall(r'class="property-tile-keyword">([^<]*)<', chunk)]
        addr = re.search(r'class="property-address">(.*?)</div>', chunk, re.S)
        out.append({"tile_id": pid, "url": urljoin(BASE, html.unescape(href.group(1))),
                    "keywords": kws, "tile_address": _text(addr.group(1)) if addr else None})
    return out


def _search(cli: _Client, center, radius_mi) -> list[dict]:
    ne, sw = _bbox(center, radius_mi)
    params = {"lat": f"{center[0]:.5f}", "lng": f"{center[1]:.5f}", "zoom": _zoom(radius_mi),
              "ne_bounds": f"{ne[0]:.5f},{ne[1]:.5f}", "sw_bounds": f"{sw[0]:.5f},{sw[1]:.5f}"}
    tiles, seen = [], set()
    for page_no in range(1, MAX_PAGES + 1):
        q = dict(params, page=page_no) if page_no > 1 else params
        page = cli.get(f"{BASE}/list-search?{urlencode(q)}")
        new = [t for t in _parse_tiles(page) if t["url"] not in seen]
        for t in new:
            seen.add(t["url"])
        tiles.extend(new)
        nxt = re.search(r'class="next_page(?! disabled)[^"]*"[^>]*href=', page)
        if not new or not nxt:
            break
    return tiles


# --------------------------------------------------------------------------- property page
def _jsonld_itempage(page: str) -> dict:
    for m in re.finditer(r'<script type="application/ld\+json">(.*?)</script>', page, re.S):
        s = m.group(1)
        if '"ItemPage"' not in s:
            continue
        try:
            return json.loads(s)
        except ValueError:
            try:
                return json.loads(re.sub(r"[\x00-\x1f]", " ", s))
            except ValueError:
                pass
    return {}


def _parse_floorplans(page: str) -> list[dict]:
    fps = []
    for m in re.finditer(r'<section\s+id="floorPlansAccordionSection\d+"\s+class="floor-plan-group-item([^"]*)"', page):
        if "inactive" in m.group(1):
            continue
        body = page[m.end(): page.find("</section>", m.end())]
        hdr_end = body.find("</header>")
        header, units_html = body[:hdr_end], body[hdr_end:]
        title = re.search(r'floor-plan-title">(.*?)</div>', header, re.S)
        title = _text(title.group(1)) if title else ""
        rows = re.findall(r'<div class="floor-plan-info-row[^"]*">(.*?)</div>', header, re.S)
        info = [_text(r) for r in rows]
        price_txt = next((t for t in info if t.startswith("$")), "")
        sqft_m = re.search(r"([\d,]+)\s*ft", _text(header))
        avail = re.search(r"(\d+)\s+Available", _text(header))
        mgr = re.search(r"Managed By:\s*(.*?)\s*</div>", header, re.S)
        img = re.search(r'floor-plan-img-container">\s*<amp-img src="https://images\.rentable\.co/\d+/(\d+)/', header)
        units = []
        for um in re.finditer(r'class="flex-row floor-plan-table-unit-row([^"]*)"', units_html):
            if "inactive" in um.group(1):
                continue
            seg = units_html[um.end(): um.end() + 3500]
            lid = re.search(r"selectedListingId: '(\d+)'", seg)
            cols = [_text(c) for c in re.findall(r'<div class="floor-plan-table-unit-col[^"]*">(.*?)</div>', seg, re.S)]
            if len(cols) >= 3 and cols[2].lower().startswith("rented"):
                continue
            units.append({"listing_id": lid.group(1) if lid else None,
                          "label": cols[0] if cols else "", "price": _int(cols[1]) if len(cols) > 1 else None,
                          "avail": cols[2] if len(cols) > 2 else ""})
        n_avail = int(avail.group(1)) if avail else len(units)
        if n_avail == 0 and not units:
            continue
        beds = 0 if re.search(r"studio", title, re.I) else _int(re.search(r"(\d+)\s*Bed", title).group(1)) \
            if re.search(r"(\d+)\s*Bed", title) else None
        bm = re.search(r"([\d.]+)\s*Bath", title)
        prices = [_int(p) for p in re.findall(r"\$\s*[\d,]+", price_txt)]
        prices += [u["price"] for u in units if u["price"]]
        fps.append({"title": title, "beds": beds, "baths": float(bm.group(1)) if bm else None,
                    "price": min(prices) if prices else None,
                    "sqft": _int(sqft_m.group(1)) if sqft_m else None,
                    "managed_by": _text(mgr.group(1)) if mgr else None,
                    "img_id": img.group(1) if img else None, "units": units})
    return fps


_UNIT_RX = re.compile(r"^(?:unit|apt\.?|apartment|suite|#)\s*([A-Za-z0-9-]+)$|#\s*([A-Za-z0-9-]+)\s*$", re.I)


def _unit_of(label: str) -> str | None:
    m = _UNIT_RX.search(label.strip())
    return (m.group(1) or m.group(2)) if m else None


def _property_rows(cli: _Client, tile: dict) -> list[dict]:
    page = cli.get(tile["url"])
    ld = _jsonld_itempage(page)
    about = ld.get("about") or {}
    ent = (ld.get("mainEntity") or [{}])[0] if isinstance(ld.get("mainEntity"), list) else (ld.get("mainEntity") or {})
    addr = ent.get("address") or {}
    geo = ent.get("geo") or {}
    canon = re.search(r'<link rel="canonical" href="([^"]+)"', page)
    url = canon.group(1) if canon else tile["url"]
    pid = re.search(r'src="/properties/(\d+)/listing-details"', page)
    pid = pid.group(1) if pid else tile["tile_id"]
    name = html.unescape(about.get("name") or ent.get("name") or "")
    prop_desc = _text(html.unescape(about.get("description") or ""))
    upd = re.search(r"Last Updated:</span>\s*([^<]+)", page)
    updated = _rel_date(upd.group(1) if upd else None) or ld.get("dateModified")
    try:
        lat, lon = float(geo["latitude"]), float(geo["longitude"])
    except (KeyError, TypeError, ValueError):
        lat = lon = None
    street = html.unescape(addr.get("streetAddress") or "") or None
    kw = " ".join(tile.get("keywords") or [])
    fps = _parse_floorplans(page)
    img_counts: dict = {}
    for fp in fps:
        img_counts[fp["img_id"]] = img_counts.get(fp["img_id"], 0) + 1
    rows, used = [], set()
    for fp in fps:
        labels = [u["label"] for u in fp["units"] if u["label"]]
        unit = _unit_of(labels[0]) if len(fp["units"]) == 1 and labels else None
        lids = sorted((u["listing_id"] for u in fp["units"] if u["listing_id"]), key=lambda x: (len(x), x))
        if fp["img_id"] and img_counts[fp["img_id"]] == 1:
            key = f"fp{fp['img_id']}"          # floor-plan image id: stable per floor plan
        elif lids:
            key = f"u{lids[0]}"                # shared/missing image: lowest available unit listing id
        else:
            key = f"{fp['beds']}b{fp['baths']}ba{fp['sqft']}"
        sid, n = f"{pid}:{key}", 2
        while sid in used:
            sid, n = f"{pid}:{key}-{n}", n + 1
        used.add(sid)
        titles = [l for l in labels if not _unit_of(l)]
        desc_parts = [name] + titles[:3] + ([prop_desc] if prop_desc and prop_desc != street else [])
        if fp["managed_by"]:
            desc_parts.append(f"Managed by {fp['managed_by']}")
        desc = " | ".join(p for p in desc_parts if p)[:500] or None
        blob = " ".join([kw, name, " ".join(titles), prop_desc])
        rows.append({
            "source": SOURCE, "source_id": sid, "url": url,
            "address": (f"{street} Unit {unit}" if street and unit else street),
            "unit": unit, "city": html.unescape(addr.get("addressLocality") or "") or None,
            "state": (addr.get("addressRegion") or None), "zip": (addr.get("postalCode") or None),
            "lat": lat, "lon": lon, "beds": fp["beds"], "baths": fp["baths"], "price": fp["price"],
            "sqft": fp["sqft"], "unit_type": "", "updated": updated, "description": desc,
            "income_restricted": bool(_RESTRICTED_RX.search(blob) or re.search(r"Income Restricted|Senior Community|Student", kw)),
            "is_room": bool(_ROOM_RX.search(" ".join([name, " ".join(titles)])) or (fp["sqft"] is not None and fp["sqft"] < 200)),
        })
    return rows


# --------------------------------------------------------------------------- public API
def fetch(center: tuple[float, float], radius_mi: float, *, delay: float = 1.0,
          max_properties: int | None = None, stats: dict | None = None) -> list[dict]:
    """All currently available Rentable floor plans within radius_mi of center=(lat, lon).

    delay: seconds between requests (politeness). max_properties: optional cap on property
    pages fetched (each property = 1 request of 0.3-1.2 MB). stats: optional dict filled with
    request count / tile count."""
    if not (-90 <= center[0] <= 90 and -180 <= center[1] <= 180) or radius_mi <= 0:
        raise ValueError("bad center/radius")
    cli = _Client(delay)
    tiles = _search(cli, center, radius_mi)
    n_tiles = len(tiles)
    if max_properties is not None:
        tiles = tiles[:max_properties]
    cache = _PropertyCache()
    out, seen = [], set()
    for i, t in enumerate(tiles, 1):
        rows = cache.get(t["url"])
        if rows is None:
            try:
                rows = _property_rows(cli, t)
            except requests.HTTPError as e:  # one dead property page shouldn't sink the run
                if e.response is not None and e.response.status_code in (404, 410):
                    continue
                raise
            finally:
                if i % SAVE_EVERY == 0:
                    cache.save()
            cache.put(t["url"], rows)
        for r in rows:
            if r["source_id"] in seen:
                continue
            if r["lat"] is not None and _haversine_mi(center, (r["lat"], r["lon"])) > radius_mi:
                continue
            seen.add(r["source_id"])
            out.append({k: r[k] for k in KEYS})
    cache.save()
    if stats is not None:
        stats.update(requests=cli.count, tiles=n_tiles, fetched_properties=len(tiles))
    return out


class _PropertyCache:
    """Parsed rows per property URL, kept for PROPERTY_CACHE_HOURS."""

    def __init__(self):
        from .. import CACHE_DIR
        self.path = CACHE_DIR / "rentable_properties.json"
        try:
            self.data = json.loads(self.path.read_text())
        except (FileNotFoundError, ValueError):
            self.data = {}

    def get(self, url):
        hit = self.data.get(url)
        return hit[1] if hit and time.time() - hit[0] < PROPERTY_CACHE_HOURS * 3600 else None

    def put(self, url, rows):
        self.data[url] = [time.time(), rows]

    def save(self):
        now = time.time()
        self.data = {k: v for k, v in self.data.items() if now - v[0] < PROPERTY_CACHE_HOURS * 3600}
        self.path.write_text(json.dumps(self.data))
