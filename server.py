#!/usr/bin/env python3
"""Local web UI for the apartment finder.

    python3 server.py            # then open http://localhost:8765

Search any US location in the page; it fetches rentals (studio to 3BR) within
SEARCH_RADIUS_MI of it from every enabled source (see apt/sources) and merges
listings that appear on more than one site. Filtering, sorting and scoring happen in the
page. Source results are cached by map tile (apt/tilecache.py), so nearby and overlapping
searches reuse them; Refresh re-fetches. While running it preloads the most-searched
areas in the background (see preload.py); --no-preload turns that off.
"""

import json
import sys
import threading
import time
import webbrowser
from datetime import datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests

from apt import CACHE_DIR
from apt.geo import geocode
from apt.listings import search
from apt.tilecache import record_search
import preload

PORT = 8765
STATIC = Path(__file__).resolve().parent / "static"
SEARCH_RADIUS_MI = 10
MAX_RADIUS_MI = 15
# On start, preload this many of the most-searched areas in the background (0 = off)
PRELOAD_AREAS = 3
CACHE_HOURS = 6
BEDS = [0, 1, 2, 3]
# Upper price limits for what gets fetched; the page filters further
FETCH_CAPS = {0: 8000, 1: 10000, 2: 15000, 3: 20000}

_locks = {}
_locks_guard = threading.Lock()


def get_listings(lat, lon, radius, refresh=False):
    # Round the center to ~100 m so nearby searches share a cache entry
    key = f"{lat:.3f}_{lon:.3f}_{radius:g}"
    path = CACHE_DIR / f"listings_{key}.json"
    with _locks_guard:
        lock = _locks.setdefault(key, threading.Lock())
    with lock:
        if not refresh and path.exists() and time.time() - path.stat().st_mtime < CACHE_HOURS * 3600:
            data = json.loads(path.read_text())
            if not data.get("pending"):
                return data
            # A background source was still loading; re-merge in case it has finished
        listings, sources = search((round(lat, 3), round(lon, 3)), radius, BEDS, FETCH_CAPS, refresh=refresh)
        data = {
            "center": [lat, lon],
            "radius_mi": radius,
            "fetched_at": datetime.now().isoformat(timespec="minutes"),
            "sources": sources,
            "pending": any(s.get("pending") for s in sources),
            "listings": listings,
        }
        path.write_text(json.dumps(data))
        return data


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(STATIC), **kwargs)

    def send_json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlparse(self.path)
        qs = {k: v[0] for k, v in parse_qs(url.query).items()}
        try:
            if url.path == "/api/listings":
                lat, lon = float(qs["lat"]), float(qs["lon"])
                radius = min(float(qs.get("radius", SEARCH_RADIUS_MI)), MAX_RADIUS_MI)
                record_search((lat, lon), radius)
                return self.send_json(get_listings(lat, lon, radius, refresh="refresh" in qs))
            if url.path == "/api/geocode":
                q = qs.get("q", "").strip()
                return self.send_json(geocode(q) if q else [])
        except (KeyError, ValueError) as e:
            return self.send_json({"error": f"bad request: {e}"}, status=400)
        except requests.RequestException as e:
            return self.send_json({"error": str(e)}, status=502)
        return super().do_GET()

    def log_message(self, fmt, *args):
        if "/api/" in str(args[0] if args else ""):
            sys.stderr.write(f"{self.log_date_time_string()} {fmt % args}\n")


def preload_popular():
    """Refresh the most-searched areas while the app is open; stops when the app does."""
    areas = preload.pick_areas(top=PRELOAD_AREAS)
    if not areas:
        return
    print(f"Preloading {len(areas)} popular area(s) in the background")
    preload.run(areas, preload.pick_sources(), log=lambda msg: print(f"  [preload] {msg}", flush=True))
    print("Preloading finished", flush=True)


def main():
    url = f"http://localhost:{PORT}"
    try:
        server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError:
        sys.exit(f"Port {PORT} is busy - the app is probably already running. Open {url}, "
                 f"or stop the other copy with: pkill -f server.py")
    print(f"Apartment finder running at {url}  (Ctrl+C to stop)")
    if PRELOAD_AREAS and "--no-preload" not in sys.argv:
        threading.Thread(target=preload_popular, daemon=True).start()
    if "--no-browser" not in sys.argv:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
