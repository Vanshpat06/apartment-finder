"""Per-source listing cache keyed by map tile.

The map is cut into standard web-map tiles (zoom 15, about 0.6 miles across). Each
source's units are stored by the tile they sit in, and a tile counts as covered once
a fetch's circle has contained all of it. A search reads every tile its circle
touches; only the part no fresh fetch has covered is fetched, with one request per
source, so overlapping searches share work and a preloaded area serves any search
inside it.

Tiles only track what's cached. Fetching stays one circle per source and search,
because several sources are rate-limited or have a monthly quota, and splitting a
search into many small fetches would multiply their requests.
"""

import json
import math
from contextlib import contextmanager
import sqlite3
import threading
import time

import requests

from . import CACHE_DIR
from .geo import haversine_mi

DB_PATH = CACHE_DIR / "tiles.sqlite"
ZOOM = 15
# Tiles younger than FRESH are used as is. Older ones, up to MAX_AGE, are still served
# but refreshed in the background; past MAX_AGE they are fetched again before use.
FRESH = 6 * 3600
MAX_AGE = 48 * 3600
# After a failed fetch, the same source and area aren't retried for this long
ERROR_TTL = 10 * 60
# Fetches reach this far past what's missing, so a later search a little way off is already cached
SPARE_MI = 2
REPEAT_WINDOW = 30 * 60

_db_lock = threading.Lock()
_inflight = {}      # source name -> [(center, radius_mi, threading.Event)]
_inflight_lock = threading.Lock()
_errors = {}        # (source name, rounded center, radius) -> (time, message)


# ---------- tile math ----------

def tile_of(lat, lon):
    n = 2 ** ZOOM
    x = int((lon + 180) / 360 * n)
    y = int((1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n)
    return x, y


def tile_bounds(x, y):
    """(south, west, north, east) of a tile."""
    n = 2 ** ZOOM
    lat = lambda yy: math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * yy / n))))
    return lat(y + 1), x / n * 360 - 180, lat(y), (x + 1) / n * 360 - 180


def _corners(t):
    s, w, n, e = tile_bounds(*t)
    return ((s, w), (s, e), (n, w), (n, e))


def _tile_range(center, radius_mi):
    dlat = radius_mi / 69.0
    dlon = radius_mi / (69.172 * max(math.cos(math.radians(center[0])), 1e-6))
    x0, y0 = tile_of(center[0] + dlat, center[1] - dlon)  # north-west corner: smallest x and y
    x1, y1 = tile_of(center[0] - dlat, center[1] + dlon)
    return x0, x1, y0, y1


def tiles_touching(center, radius_mi):
    """Tiles that overlap the circle."""
    x0, x1, y0, y1 = _tile_range(center, radius_mi)
    out = []
    for x in range(x0, x1 + 1):
        for y in range(y0, y1 + 1):
            s, w, n, e = tile_bounds(x, y)
            nearest = (min(max(center[0], s), n), min(max(center[1], w), e))
            if haversine_mi(center, nearest) <= radius_mi:
                out.append((x, y))
    return out


def tiles_inside(center, radius_mi):
    """Tiles that lie entirely within the circle."""
    return [t for t in tiles_touching(center, radius_mi)
            if all(haversine_mi(center, c) <= radius_mi for c in _corners(t))]


def circle_around(center, tiles):
    """A (center, radius) circle containing every tile: around the search center, or around the
    tiles' own bounding box when that's smaller (e.g. only one corner of a search is missing)."""
    corners = [c for t in tiles for c in _corners(t)]
    lats, lons = [c[0] for c in corners], [c[1] for c in corners]
    mid = ((min(lats) + max(lats)) / 2, (min(lons) + max(lons)) / 2)
    options = [(p, max(haversine_mi(p, c) for c in corners) * 1.001) for p in (center, mid)]
    return min(options, key=lambda o: o[1])


# ---------- storage ----------

@contextmanager
def _db():
    """A connection, used under the module lock and committed on success."""
    with _db_lock:
        db = _connect()
        try:
            yield db
            db.commit()
        finally:
            db.close()


def _connect():
    db = sqlite3.connect(DB_PATH, timeout=30)
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS coverage (source TEXT, x INT, y INT, fetched_at REAL,
                                             PRIMARY KEY (source, x, y)) WITHOUT ROWID;
        CREATE TABLE IF NOT EXISTS units (source TEXT, x INT, y INT, data TEXT);
        CREATE INDEX IF NOT EXISTS units_tile ON units (source, x, y);
        CREATE TABLE IF NOT EXISTS searches (lat REAL, lon REAL, radius_mi REAL, at REAL);
    """)
    return db


def _source_key(s):
    return s.__name__.rsplit(".", 1)[-1]


def _store(db, key, center, radius_mi, units, fetched_at):
    """Save a fetch's units into every tile its circle fully covers, replacing what was there."""
    covered = set(tiles_inside(center, radius_mi))
    by_tile = {t: [] for t in covered}
    for u in units:
        if u.get("lat") is None or u.get("lon") is None:
            continue
        t = tile_of(u["lat"], u["lon"])
        if t in by_tile:
            by_tile[t].append(u)
    db.executemany("DELETE FROM units WHERE source=? AND x=? AND y=?", [(key, *t) for t in covered])
    db.executemany("INSERT INTO units VALUES (?,?,?,?)",
                   [(key, *t, json.dumps(u)) for t, us in by_tile.items() for u in us])
    db.executemany("INSERT OR REPLACE INTO coverage VALUES (?,?,?,?)", [(key, *t, fetched_at) for t in covered])


def _ages(key, center, radius_mi):
    """{tile: seconds since fetched} for the tiles touching the circle that have been covered."""
    x0, x1, y0, y1 = _tile_range(center, radius_mi)
    with _db() as db:
        rows = db.execute("SELECT x, y, fetched_at FROM coverage WHERE source=? AND x BETWEEN ? AND ? "
                          "AND y BETWEEN ? AND ?", (key, x0, x1, y0, y1)).fetchall()
    now = time.time()
    return {(x, y): now - at for x, y, at in rows}


def _read_units(key, center, radius_mi, tiles):
    x0, x1, y0, y1 = _tile_range(center, radius_mi)
    with _db() as db:
        rows = db.execute("SELECT x, y, data FROM units WHERE source=? AND x BETWEEN ? AND ? AND y BETWEEN ? AND ?",
                          (key, x0, x1, y0, y1)).fetchall()
    out = []
    for x, y, data in rows:
        if (x, y) in tiles:
            u = json.loads(data)
            if haversine_mi(center, (u["lat"], u["lon"])) <= radius_mi:
                out.append(u)
    return out


def record_search(center, radius_mi):
    """Log a search; preload.py uses the log to find popular areas. The page re-requests
    while slow sources load, so a repeat of the same area within REPEAT_WINDOW isn't logged."""
    now = time.time()
    with _db() as db:
        recent = db.execute("SELECT lat, lon FROM searches WHERE at > ?", (now - REPEAT_WINDOW,)).fetchall()
        if not any(haversine_mi(center, r) <= 1 for r in recent):
            db.execute("INSERT INTO searches VALUES (?,?,?,?)", (center[0], center[1], radius_mi, now))


def popular_areas(days=30, limit=10):
    """[(center, searches)] for the most-searched areas, grouping searches within ~10 miles."""
    with _db() as db:
        rows = db.execute("SELECT lat, lon FROM searches WHERE at > ?", (time.time() - days * 86400,)).fetchall()
    groups = []  # [center, count]
    for lat, lon in rows:
        g = next((g for g in groups if haversine_mi(g[0], (lat, lon)) <= 10), None)
        if g:
            g[1] += 1
        else:
            groups.append([(lat, lon), 1])
    return [tuple(g) for g in sorted(groups, key=lambda g: -g[1])[:limit]]


# ---------- fetching ----------

def _covering_fetch(key, center, radius_mi):
    """The Event of an in-progress fetch whose circle contains this one, if any."""
    with _inflight_lock:
        for c, r, ev in _inflight.get(key, []):
            if haversine_mi(c, center) + radius_mi <= r:
                return ev
    return None


def _fetch(s, center, radius_mi, ev=None):
    """Fetch one circle from a source and store it; returns an error message or None.
    ev is the in-flight entry when the caller already registered one."""
    key = _source_key(s)
    if ev is None:
        ev = threading.Event()
        with _inflight_lock:
            _inflight.setdefault(key, []).append((center, radius_mi, ev))
    try:
        fetched_at = time.time()
        units = s.fetch(center, radius_mi)
        with _db() as db:
            _store(db, key, center, radius_mi, units, fetched_at)
        return None
    except (requests.RequestException, ValueError, KeyError) as e:
        msg = f"{type(e).__name__}: {e}"[:200]
        _errors[(key, (round(center[0], 2), round(center[1], 2)), round(radius_mi, 1))] = (time.time(), msg)
        return msg
    finally:
        with _inflight_lock:
            _inflight[key] = [f for f in _inflight[key] if f[2] is not ev]
        ev.set()


def _recent_error(key, center, radius_mi):
    err = _errors.get((key, (round(center[0], 2), round(center[1], 2)), round(radius_mi, 1)))
    return err[1] if err and time.time() - err[0] < ERROR_TTL else None


def _in_background(s, center, radius_mi):
    """Start a background fetch unless one already running overlaps this circle; what it
    leaves uncovered is picked up by the next search once it finishes."""
    key = _source_key(s)
    with _inflight_lock:
        if any(haversine_mi(c, center) < r + radius_mi for c, r, _ in _inflight.get(key, [])):
            return
        ev = threading.Event()
        _inflight.setdefault(key, []).append((center, radius_mi, ev))
    threading.Thread(target=_fetch, args=(s, center, radius_mi, ev), daemon=True).start()


def get(s, center, radius_mi, refresh=False, slow="background"):
    """Units from source s within radius_mi of center, fetching only tiles the cache lacks.

    slow says what to do when a SLOW source has missing tiles: "background" starts the
    fetch and returns what's cached with pending=True, "skip" returns what's cached, and
    "wait" fetches before returning, and also refreshes stale tiles before returning
    instead of in the background. Returns {"units", "error", "pending", "note"}.
    """
    key = _source_key(s)
    needed = tiles_touching(center, radius_mi)
    ages = {} if refresh else _ages(key, center, radius_mi)
    missing = [t for t in needed if ages.get(t, MAX_AGE) >= MAX_AGE]
    stale = [t for t in needed if FRESH <= ages.get(t, MAX_AGE) < MAX_AGE]
    error, pending, note = None, False, ""

    if missing:
        c, r = circle_around(center, missing)
        r += SPARE_MI
        is_slow = getattr(s, "SLOW", False)
        error = _recent_error(key, c, r)
        if error:
            pass
        elif is_slow and slow == "skip":
            note = "slow source; only used by the web app" if not ages else "slow source; showing cached area only"
        elif is_slow and slow == "background":
            _in_background(s, c, r)
            pending = True
            note = "still loading in the background; listings appear when it finishes"
        else:
            ev = _covering_fetch(key, c, r)
            if ev:
                ev.wait()  # someone else is already fetching this area
            else:
                error = _fetch(s, c, r)
    elif stale and slow == "wait":
        error = _fetch(s, *circle_around(center, stale))
    elif stale:
        _in_background(s, *circle_around(center, stale))

    covered = set(needed) - (set(missing) if (error or pending or note) else set())
    return {"units": _read_units(key, center, radius_mi, covered), "error": error, "pending": pending, "note": note}
