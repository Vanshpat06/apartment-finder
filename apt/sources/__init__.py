"""Listing sources.

Each source module provides:
    NAME          display name, e.g. "Redfin"
    LICENSED      True if the source's terms allow this use (an official API);
                  False for sites read the way their own web pages read them
    PRIORITY      tie-breaker when several sources list the same unit at the same
                  price, and order for filling in missing details (lower = preferred):
                  0 licensed APIs, 0.5 landlords' own sites, 1 listing sites whose
                  robots.txt allows this, 2 the rest
    available()   -> (bool, reason) whether it can run right now (e.g. has an API key)
    SLOW          optional; True if a search takes minutes, so it runs in the background
    fetch(center, radius_mi) -> list of unit dicts with these keys (None when unknown):
        source, source_id, url, address, unit, city, state, zip, lat, lon,
        beds, baths, price, sqft, unit_type, updated, description,
        income_restricted, is_room

One dict per available unit type: a building with studios and 1BRs yields two.
To add a source, add a module here and list it in SOURCES. Set the environment
variable APT_LICENSED_ONLY=1 to run only licensed sources.
"""

import os

from . import apartmentguide, apartmentlist, avalonbay, essex, greystar, redfin, rentable, rentcast, udr

# Listing sites, then landlords' own sites (AvalonBay, Essex, UDR, Greystar)
# Rent.com (rentcom.py) has the same RentPath listings as ApartmentGuide but is slower, so it isn't listed.
SOURCES = [rentcast, apartmentlist, apartmentguide, rentable, redfin, avalonbay, essex, udr, greystar]


def enabled():
    licensed_only = os.environ.get("APT_LICENSED_ONLY") == "1"
    return [s for s in SOURCES if s.LICENSED or not licensed_only]
