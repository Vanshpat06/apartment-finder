# Apartment Finder

Searches rentals around any US location across several listing sites, merges
listings that appear on more than one site, and ranks them by value (price,
neighborhood safety, distance, space).

## Run

    python3 server.py              # web app at http://localhost:8765
    python3 find_apartments.py     # command-line version (see --help)
    python3 preload.py             # fill the cache for popular areas ahead of time (see --help)

Needs Python 3 with `requests`.

## Sources

| Source | Type | Coverage | Notes |
|---|---|---|---|
| RentCast | Licensed API | US | Free key (no card) at https://app.rentcast.io/app/api, then `export RENTCAST_API_KEY=...` or save it in `rentcast_key.txt` here. Free plan = 50 requests/month; the app stops at 45 and caches each area for a day. |
| Apartment List | Listing site, robots.txt allows | US | Fast |
| Redfin | Listing site, robots.txt disallows | US | Fast; lowest priority, so it only fills gaps and breaks no ties |
| ApartmentGuide | Listing site, robots.txt allows | US | Rate-limited: loads in the background, ~12 min for 10 miles. Same RentPath listings as Rent.com, whose slower adapter (`apt/sources/rentcom.py`) is kept but unused |
| Rentable | Listing site, robots.txt allows | US | Slow: one page per building, loads in the background, cached 24 h |
| AvalonBay, Essex, UDR, Greystar | Landlords' own sites, robots.txt allows | Their markets (Essex: CA + Seattle) | Fast; preferred link on a price tie |

Not included: Zillow, Trulia, HotPads, Apartments.com, Zumper, Realtor.com, RentHop, Equity Residential and others block automated access, and this project doesn't work around that.

The unlicensed sources are for personal use: their terms of service don't allow
automated collection, and they can change without notice. Before offering this
to other people, run with only licensed sources:

    APT_LICENSED_ONLY=1 python3 server.py

## Layout

- `apt/sources/` - one module per listing site, all returning the same unit format
- `apt/listings.py` - runs every source, drops room shares and income-restricted units, merges duplicates, flags suspicious prices
- `apt/areas/` - per-city neighborhood names and safety data (San Francisco uses SFPD incident reports); cities without a module get ZIP codes and no safety grade
- `static/index.html` - the web app (filters, sorting, map)
- `apt/tilecache.py` - caches each source's listings by map tile in `cache/tiles.sqlite`, so overlapping searches only fetch what's missing; also logs searches for `preload.py`
- `preload.py` - fetches the most-searched areas (and optionally large metros) ahead of time, slow sources included; run it daily to keep them fresh
- `cache/` - cached listings, floor plans and crime data (safe to delete)
