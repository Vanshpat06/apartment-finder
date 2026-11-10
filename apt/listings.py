"""Search every source around a point, merge duplicates, and add neighborhood/safety/warning info."""

import re
from concurrent.futures import ThreadPoolExecutor
from statistics import median

from . import sources, tilecache
from .areas import area_for, grade
from .geo import haversine_mi

# Sizes outside these are usually the whole building's footage, so they're ignored
PLAUSIBLE_SQFT = {0: (200, 800), 1: (300, 1200), 2: (450, 2000), 3: (650, 3000)}
# Rent below this share of the local median for the same bedroom count gets flagged.
# "Local" = same-bedroom listings within the first of these radii that has enough of them.
SUSPICIOUS_SHARE = 0.45
LOCAL_RADII_MI = (1.5, 3, 6)
LOCAL_MIN_COUNT = 8
# Listings at the same normalized address farther apart than this are treated as different places
SAME_PLACE_MI = 0.15

# Listings that are really a room in a shared place
SHARED = re.compile(
    r"\b(room for rent|private room|shared (bath|bathroom|kitchen|living)|share (the |a )?(house|home|apartment|apt|living)|"
    r"roommates?|housemates?|sublet|sublease|co-?living|master bedroom (in|of)|bedroom in a|single room|furnished room)\b",
    re.I,
)

STREET_WORDS = {
    "street": "st", "avenue": "ave", "av": "ave", "boulevard": "blvd", "drive": "dr", "place": "pl",
    "road": "rd", "terrace": "ter", "court": "ct", "lane": "ln", "parkway": "pkwy", "highway": "hwy",
    "square": "sq", "circle": "cir", "alley": "aly", "plaza": "plz",
    "north": "n", "south": "s", "east": "e", "west": "w",
    "first": "1st", "second": "2nd", "third": "3rd", "fourth": "4th", "fifth": "5th",
}
UNIT_MARK = re.compile(r"\s*(#|\bapt\b|\bapartment\b|\bunit\b|\bste\b|\bsuite\b|\bfl\b|\bfloor\b).*$", re.I)


def building_key(address, city):
    """Normalized 'number street|city' used to match the same building across sites, or None."""
    street = (address or "").lower().split(",")[0]  # drop ", City, ST 12345" if present
    street = UNIT_MARK.sub("", street)
    words = re.sub(r"[^a-z0-9 -]", " ", street).split()
    if not words or not words[0][0].isdigit():
        return None
    number = words[0].split("-")[0]  # "101-103" -> "101"
    rest = [STREET_WORDS.get(w, w) for w in (w.strip("-") for w in words[1:]) if w]
    return f"{number} {' '.join(rest)}|{(city or '').lower()}"


def search(center, radius_mi, beds, caps, refresh=False, background=True):
    """Merged listings within radius_mi of center, plus a status entry per source.

    Each source's units come from the tile cache (apt/tilecache.py), which fetches
    only the part of the circle it doesn't already have. Sources marked SLOW are
    fetched in a background thread; until they finish, their status has
    "pending": True and a later call picks up their results. With background=False
    (e.g. a one-shot command-line run) they only return what's already cached.
    """
    status, available = [], []
    for s in sources.enabled():
        ok, reason = s.available()
        if ok:
            available.append(s)
        else:
            status.append({"name": s.NAME, "licensed": s.LICENSED, "ok": False, "count": 0, "note": reason})

    slow = "background" if background else "skip"
    with ThreadPoolExecutor(max_workers=max(1, len(available))) as pool:
        results = list(pool.map(lambda s: (s, tilecache.get(s, center, radius_mi, refresh=refresh, slow=slow)), available))

    units = []
    for s, data in results:
        rows = data["units"]
        entry = {"name": s.NAME, "licensed": s.LICENSED, "ok": not data["error"], "count": len(rows),
                 "note": data["error"] or data["note"]}
        if data["pending"]:
            entry["pending"] = True
        status.append(entry)
        for u in rows:
            u["priority"] = s.PRIORITY
        units += rows

    units = [u for u in units
             if u["beds"] in beds and u["lat"] is not None and u["lon"] is not None and u["price"] and u["price"] <= caps[u["beds"]]
             and not u["income_restricted"] and not u["is_room"]
             and not SHARED.search(u.get("description") or "")
             and not SHARED.search(u.get("unit_type") or "")]
    listings = merge(units)
    describe(listings)
    return listings, status


def merge(units):
    """Group units into one listing per building and bedroom count, keeping every site's offer."""
    groups = {}
    for u in units:
        key = building_key(u["address"], u["city"]) or f"{u['lat']:.4f},{u['lon']:.4f}"
        gkey = (key, u["beds"])
        g = groups.get(gkey)
        # Guard against the same street address in different places (e.g. a bad map pin)
        if g and haversine_mi((g[0]["lat"], g[0]["lon"]), (u["lat"], u["lon"])) > SAME_PLACE_MI:
            gkey = (f"{key}@{u['lat']:.3f},{u['lon']:.3f}", u["beds"])
        groups.setdefault(gkey, []).append(u)

    listings = []
    for (key, beds), group in groups.items():
        # Cheapest wins; on a tie the higher-priority source's listing is the main one
        group.sort(key=lambda u: (u["price"], u["priority"]))
        best = group[0]
        by_priority = sorted(group, key=lambda u: u["priority"])

        def fill(field):
            """best's value, else the first one from the most-preferred source that has it."""
            return best[field] or next((u[field] for u in by_priority if u[field]), None)

        offers = {}
        for u in group:  # cheapest offer per source
            offers.setdefault(u["source"], u)
        listings.append({
            "id": f"{key}:{beds}",
            "beds": beds,
            "baths": fill("baths"),
            "price": best["price"],
            "sqft": fill("sqft"),
            "lat": best["lat"],
            "lon": best["lon"],
            "address": best["address"],
            "city": best["city"],
            "state": best["state"],
            "zip": best["zip"],
            "unit_type": fill("unit_type") or "",
            "units_fit": len(group),
            "updated": max((u["updated"] for u in group if u["updated"]), default=None),
            "url": best["url"],
            "sources": [{"source": u["source"], "price": u["price"], "url": u["url"]}
                        for u in sorted(offers.values(), key=lambda u: (u["price"], u["priority"]))],
        })
    return listings


def describe(listings):
    """Add neighborhood, safety grade, usable sqft and warning flags to each listing."""
    for l in listings:
        pt = (l["lat"], l["lon"])
        area = area_for(l)
        if area:
            l["area"] = area.NAME
            l["hood"] = area.neighborhood(pt)
            l["hood_s"], l["incidents"] = area.safety(pt)
            l["safety_note"] = area.SAFETY_NOTE
        else:
            l["area"] = l["city"]
            l["hood"] = f"{l['city']} {l['zip']}".strip()
            l["hood_s"], l["incidents"], l["safety_note"] = None, None, None
        l["grade"] = grade(l["hood_s"])
        lo, hi = PLAUSIBLE_SQFT.get(l["beds"], (150, 5000))
        l["sqft_est"] = not (l["sqft"] and lo <= l["sqft"] <= hi)

    for l in listings:
        m = local_median(l, listings)
        l["flags"] = []
        if m and l["price"] < SUSPICIOUS_SHARE * m:
            l["flags"].append(f"unusually cheap for the area (median ${round(m):,}) - check it isn't a room, "
                              "income-restricted unit or typo")
    return listings


def local_median(listing, listings):
    """Median rent of same-bedroom listings near this one, or None if there are too few."""
    pt = (listing["lat"], listing["lon"])
    same = [l for l in listings if l["beds"] == listing["beds"] and l is not listing]
    for r in LOCAL_RADII_MI:
        deg = r / 69.0 * 1.5  # cheap box check before the exact distance
        prices = [l["price"] for l in same
                  if abs(l["lat"] - pt[0]) < deg and abs(l["lon"] - pt[1]) < deg
                  and haversine_mi(pt, (l["lat"], l["lon"])) <= r]
        if len(prices) >= LOCAL_MIN_COUNT:
            return median(prices)
    return None
