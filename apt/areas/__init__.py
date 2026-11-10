"""Per-city area data: neighborhood names and safety scores.

Each area module provides:
    NAME                       display name
    covers(listing) -> bool    whether this module knows the listing's location
    neighborhood((lat, lon))   neighborhood name
    safety((lat, lon))         (0-1 score where 1 = safest, raw count) or (None, None)
    SAFETY_NOTE                one-line description of the safety data source

To support a new city, add a module here and list it in AREAS. Listings in a
city with no module get their ZIP code as the neighborhood and no safety grade.
"""

from . import san_francisco

AREAS = [san_francisco]


def area_for(listing):
    return next((a for a in AREAS if a.covers(listing)), None)


def grade(score):
    if score is None:
        return "?"
    return "A" if score >= 0.8 else "B" if score >= 0.6 else "C" if score >= 0.4 else "D" if score >= 0.2 else "F"
