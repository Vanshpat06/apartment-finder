#!/usr/bin/env python3
"""Find apartments near a place and rank them by value (command-line version).

Pulls rentals from every enabled source (see apt/sources) within --radius
miles of --near, merges listings found on more than one site, keeps units within your
budget (1BR <= $3,000, 2BR <= $6,000 by default), and scores each on price,
neighborhood safety, distance and space. 1-bedrooms get a small bonus.
For the map-and-filters version, run server.py instead.

Usage:
    python3 find_apartments.py                          # near Salesforce Tower, SF
    python3 find_apartments.py --near "UCSF Mission Bay" --radius 3
    python3 find_apartments.py --beds 1                 # only 1-bedrooms
    python3 find_apartments.py --new                    # only listings not seen before
    python3 find_apartments.py --top 50
"""

import argparse
import csv
import json
import sys
from pathlib import Path

import requests

from apt.geo import geocode, haversine_mi, short_label
from apt.listings import search

DEFAULT_NEAR = "Salesforce Tower, San Francisco"
BUDGETS = {1: 3000, 2: 6000}
FETCH_CAPS = {0: 8000, 1: 10000, 2: 15000, 3: 20000}  # wider than budgets so local medians are fair
PREFERRED_BEDS, PREFERRED_BONUS = 1, 5

# Weights for the 0-100 value score; safety is left out where there's no data
W_PRICE, W_HOOD, W_LOCATION, W_SPACE = 0.30, 0.26, 0.24, 0.20
# Location score is 1 at NEAR_MI, 0 at FAR_MI, and keeps falling (down to -1) beyond it
NEAR_MI, FAR_MI = 0.5, 3.5
# Walking routes run ~25% longer than straight-line; ~20 min per mile
ROUTE_FACTOR, WALK_MIN_PER_MI = 1.25, 20

HERE = Path(__file__).resolve().parent
SEEN_FILE = HERE / "seen.json"
CSV_FILE = HERE / "results.csv"


def clamp(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


def score(listings, center):
    typical = {}
    for b in BUDGETS:
        sizes = sorted(l["sqft"] for l in listings if l["beds"] == b and not l["sqft_est"])
        typical[b] = sizes[len(sizes) // 2] if sizes else {1: 600, 2: 900}[b]

    for l in listings:
        dist = haversine_mi(center, (l["lat"], l["lon"]))
        l["dist_mi"] = dist
        l["walk_min"] = round(dist * ROUTE_FACTOR * WALK_MIN_PER_MI)
        l["commute"] = "walk" if l["walk_min"] <= 25 else "bus/transit"
        t = typical[l["beds"]]

        parts = {
            "price": (W_PRICE, clamp((BUDGETS[l["beds"]] - l["price"]) / (BUDGETS[l["beds"]] * 0.5))),
            "loc": (W_LOCATION, clamp((FAR_MI - dist) / (FAR_MI - NEAR_MI), -1)),
            # typical size = 0.5; unknown size is a small penalty, not a big one
            "space": (W_SPACE, 0.4 if l["sqft_est"] else clamp(0.5 + (l["sqft"] - t) / t)),
        }
        if l["hood_s"] is not None:
            parts["hood"] = (W_HOOD, l["hood_s"])
        s = 100 * sum(w * v for w, v in parts.values()) / sum(w for w, _ in parts.values())
        if l["beds"] == PREFERRED_BEDS:
            s += PREFERRED_BONUS
        l["score"] = round(s, 1)
        l["ppsf"] = None if l["sqft_est"] else round(l["price"] / l["sqft"], 2)
    # Flagged listings go to the bottom no matter how they score
    return sorted(listings, key=lambda l: (not l["flags"], l["score"]), reverse=True)


def load_seen():
    try:
        return set(json.loads(SEEN_FILE.read_text()))
    except (FileNotFoundError, ValueError):
        return set()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--near", default=DEFAULT_NEAR, help=f'place to measure distance from (default "{DEFAULT_NEAR}")')
    ap.add_argument("--radius", type=float, default=4, help="search radius in miles (default 4)")
    ap.add_argument("--beds", type=int, choices=[1, 2], help="only this bedroom count")
    ap.add_argument("--top", type=int, default=25, help="how many to show (default 25)")
    ap.add_argument("--new", action="store_true", help="only show listings not seen on a previous run")
    args = ap.parse_args()

    try:
        places = geocode(args.near, limit=1)
    except requests.RequestException as e:
        sys.exit(f"Could not look up {args.near!r}: {e}")
    if not places:
        sys.exit(f"Could not find {args.near!r}")
    center = (places[0]["lat"], places[0]["lon"])

    wanted = [args.beds] if args.beds else list(BUDGETS)
    try:
        listings, status = search(center, args.radius, list(FETCH_CAPS), FETCH_CAPS, background=False)
    except (requests.RequestException, ValueError) as e:
        sys.exit(f"Could not fetch listings: {e}")
    for s in status:
        print(f"  {s['name']}: {s['count']} units" if s["ok"] else f"  {s['name']}: skipped ({s['note']})")
    listings = [l for l in listings if l["beds"] in wanted and l["price"] <= BUDGETS[l["beds"]]]
    listings = score(listings, center)

    seen = load_seen()
    for l in listings:
        l["new"] = l["id"] not in seen
    SEEN_FILE.write_text(json.dumps(sorted(seen | {l["id"] for l in listings})))
    shown = [l for l in listings if l["new"]] if args.new else listings

    with CSV_FILE.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["rank", "score", "new", "price", "beds", "sqft", "$/sqft", "miles", "walk_min", "commute",
                    "neighborhood", "safety_grade", "incidents_90d", "flags", "address", "unit_type", "updated", "url"])
        for i, l in enumerate(shown, 1):
            w.writerow([i, l["score"], "yes" if l["new"] else "", l["price"], l["beds"], "" if l["sqft_est"] else l["sqft"],
                        l["ppsf"] or "", round(l["dist_mi"], 2), l["walk_min"], l["commute"], l["hood"], l["grade"],
                        "" if l["incidents"] is None else round(l["incidents"]), "; ".join(l["flags"]),
                        l["address"], l["unit_type"], l["updated"], l["url"]])

    print(f"\n{len(shown)} listings ({sum(l['new'] for l in listings)} new) within {args.radius:g} mi of "
          f"{short_label(places[0]['label'])}, best value first. Full list: {CSV_FILE.name}\n")
    for i, l in enumerate(shown[: args.top], 1):
        sqft = "size ?" if l["sqft_est"] else f"{l['sqft']} sqft"
        tag = " NEW" if l["new"] else ""
        more = f" ({l['units_fit']} units in budget)" if l["units_fit"] > 1 else ""
        safety = "no safety data" if l["incidents"] is None else \
            f"area grade {l['grade']} ({round(l['incidents'])} weighted incidents nearby, 90d)"
        print(f"{i:>2}. [{l['score']:>5}]{tag}  ${l['price']:,}  {l['beds']}BR  {sqft}  "
              f"{l['dist_mi']:.1f} mi (~{l['walk_min']} min walk, {l['commute']})")
        print(f"    {l['address']}, {l['hood']} - {safety}{more}")
        for flag in l["flags"]:
            print(f"    ! {flag}")
        others = [f"{s['source']} ${s['price']:,}" for s in l["sources"][1:]]
        print(f"    {l['url']}" + (f"\n    also on: {', '.join(others)}" if others else "") + "\n")


if __name__ == "__main__":
    main()
