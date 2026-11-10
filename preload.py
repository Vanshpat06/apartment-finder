#!/usr/bin/env python3
"""Fill the tile cache for popular areas ahead of time, so searches there load instantly.
The web app (server.py) also runs this for the top few areas each time it starts.

    python3 preload.py                 # the most-searched areas from the last 30 days
    python3 preload.py --metros 10     # also the 10 largest US metros
    python3 preload.py --skip-slow     # quick run without the slow sources
    python3 preload.py --dry-run       # list the areas without fetching

Popular areas come from the search log the web app keeps (apt/tilecache.py). Only
tiles that are missing or older than the cache's freshness window are fetched, so
running this daily keeps those areas fresh at the cost of one fetch per source and
area. Slow sources are fetched too, since nobody is waiting on them. RentCast is
left out unless --rentcast is given: its free plan allows 45 requests a month.
"""

import argparse
import time
from concurrent.futures import ThreadPoolExecutor

from apt import sources, tilecache
from apt.geo import haversine_mi

# Wider than the web app's 10-mile search, so searches centered up to 5 miles away are covered
RADIUS_MI = 15

# Downtowns of the largest US metro areas, by population
METROS = [
    ("New York", 40.7128, -74.0060), ("Los Angeles", 34.0522, -118.2437), ("Chicago", 41.8781, -87.6298),
    ("Dallas", 32.7767, -96.7970), ("Houston", 29.7604, -95.3698), ("Washington", 38.9072, -77.0369),
    ("Philadelphia", 39.9526, -75.1652), ("Atlanta", 33.7490, -84.3880), ("Miami", 25.7617, -80.1918),
    ("Phoenix", 33.4484, -112.0740), ("Boston", 42.3601, -71.0589), ("San Francisco", 37.7749, -122.4194),
    ("Detroit", 42.3314, -83.0458), ("Seattle", 47.6062, -122.3321), ("Minneapolis", 44.9778, -93.2650),
    ("San Diego", 32.7157, -117.1611), ("Tampa", 27.9506, -82.4572), ("Denver", 39.7392, -104.9903),
    ("Baltimore", 39.2904, -76.6122), ("St. Louis", 38.6270, -90.1994), ("Orlando", 28.5383, -81.3792),
    ("Charlotte", 35.2271, -80.8431), ("San Antonio", 29.4241, -98.4936), ("Portland", 45.5152, -122.6784),
    ("Austin", 30.2672, -97.7431), ("Sacramento", 38.5816, -121.4944), ("Pittsburgh", 40.4406, -79.9959),
    ("Las Vegas", 36.1699, -115.1398), ("San Jose", 37.3382, -121.8863), ("Nashville", 36.1627, -86.7816),
]


def pick_areas(top=10, days=30, metros=0):
    """[(name, center)]: the most-searched areas, then large metros not already among them."""
    areas = [(f"{n} searches near {c[0]:.3f}, {c[1]:.3f}", c) for c, n in tilecache.popular_areas(days, top)]
    for name, lat, lon in METROS[:metros]:
        if not any(haversine_mi((lat, lon), c) <= 5 for _, c in areas):  # already covered by a search area
            areas.append((name, (lat, lon)))
    return areas


def pick_sources(skip_slow=False, rentcast=False):
    return [s for s in sources.enabled() if s.available()[0] and (rentcast or s is not sources.rentcast)
            and not (skip_slow and getattr(s, "SLOW", False))]


def run(areas, srcs, log=print):
    """Bring every area up to date in the tile cache. Each source works through the areas on
    its own, so fast sources finish them all without waiting on slow ones."""
    def work(s):
        for name, center in areas:
            t = time.time()
            r = tilecache.get(s, center, RADIUS_MI, slow="wait")
            log(f"{name}: {s.NAME} {len(r['units'])} units in {time.time() - t:.0f} s"
                + (f" ({r['error']})" if r["error"] else ""))

    with ThreadPoolExecutor(max_workers=max(1, len(srcs))) as pool:
        list(pool.map(work, srcs))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--top", type=int, default=10, help="how many of the most-searched areas (default 10)")
    ap.add_argument("--days", type=int, default=30, help="search history to count, in days (default 30)")
    ap.add_argument("--metros", type=int, default=0, help=f"also preload this many large metros (max {len(METROS)})")
    ap.add_argument("--skip-slow", action="store_true", help="leave out slow sources (ApartmentGuide, Rentable) for a quick run")
    ap.add_argument("--rentcast", action="store_true", help="include RentCast, which uses the monthly quota")
    ap.add_argument("--dry-run", action="store_true", help="list the areas and stop")
    args = ap.parse_args()

    areas = pick_areas(args.top, args.days, args.metros)
    if not areas:
        print("No areas: nothing has been searched yet. Use --metros N to preload large metros.")
        return
    srcs = pick_sources(args.skip_slow, args.rentcast)
    print(f"{len(areas)} areas, {RADIUS_MI} miles each, from {', '.join(s.NAME for s in srcs)}")
    for name, _ in areas:
        print(f"  {name}")
    if not args.dry_run:
        t = time.time()
        run(areas, srcs)
        print(f"done in {time.time() - t:.0f} s")


if __name__ == "__main__":
    main()
