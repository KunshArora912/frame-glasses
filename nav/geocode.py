"""Turning what you say into a coordinate, via Nominatim.

Keyless, like the router. Nominatim's usage policy is strict about two
things and we honour both: a real User-Agent identifying the application,
and at most one request per second. We only geocode when the wearer names a
destination, so the rate limit never binds in practice.

`search` is deliberately biased toward where you are standing: a bare
"pharmacy" should find the one on this street, not the most famous one on
Earth. Two measured findings shape how that is done.

First, a viewbox alone is only a hint -- unbounded, "pharmacy" near
Westminster returns bus stops in Toronto. `bounded=1` fixes that, but then
"Heathrow Airport" returns NOTHING because it falls outside the box. So the
local pass runs first and an empty result falls through to a global one.

Second, and less obvious: do NOT re-sort by distance. Nominatim already
ranks by name match, and sorting purely on proximity turns "Trafalgar
Square" into a pub 100 m nearer that happens to have the square in its
address. Relevance leads; distance only breaks ties between results of
equal importance (which is exactly the category-search case, where every
hit comes back with importance 0).
"""

from __future__ import annotations

import json
import math
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass

from nav.router import haversine_m

NOMINATIM = "https://nominatim.openstreetmap.org"
USER_AGENT = "frame-nav-hud/0.1 (personal project; Brilliant Labs Frame)"

_last_call = 0.0
MIN_INTERVAL = 1.1          # Nominatim allows 1/s; leave a margin

# Spoken category words mapped onto Nominatim "special phrases", which are
# queried with brackets: [cafe] finds cafes near you, while the bare word
# "coffee" finds Coffee County, Georgia. The map only covers what someone
# actually says out loud; anything else is tried as [the words themselves].
_CATEGORY = {
    "coffee": "cafe", "cafe": "cafe", "espresso": "cafe",
    "food": "restaurant", "eat": "restaurant", "lunch": "restaurant",
    "dinner": "restaurant", "restaurant": "restaurant",
    "bathroom": "toilets", "restroom": "toilets", "toilet": "toilets",
    "toilets": "toilets", "loo": "toilets",
    "gas": "fuel", "petrol": "fuel", "fuel": "fuel",
    "cash": "atm", "atm": "atm", "cashpoint": "atm",
    "groceries": "supermarket", "grocery": "supermarket",
    "supermarket": "supermarket",
    "chemist": "pharmacy", "drugstore": "pharmacy", "pharmacy": "pharmacy",
    "beer": "pub", "pub": "pub", "bar": "bar",
    "hospital": "hospital", "bank": "bank", "hotel": "hotel",
    "park": "park", "parking": "parking", "station": "station",
    "water": "drinking water", "charger": "charging station",
}


@dataclass
class Place:
    name: str
    lat: float
    lon: float
    kind: str = ""
    distance_m: float | None = None
    importance: float = 0.0

    @property
    def coord(self) -> tuple[float, float]:
        return (self.lat, self.lon)

    @property
    def short_name(self) -> str:
        """First meaningful part of a long OSM display_name."""
        return self.name.split(",")[0].strip()[:24]


def _throttle() -> None:
    global _last_call
    wait = MIN_INTERVAL - (time.monotonic() - _last_call)
    if wait > 0:
        time.sleep(wait)
    _last_call = time.monotonic()


def _get(path: str, params: dict, timeout: float) -> object:
    _throttle()
    url = f"{NOMINATIM}/{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def _viewbox(near: tuple[float, float], radius_km: float) -> str:
    dlat = radius_km / 111.0
    dlon = radius_km / max(111.0 * math.cos(math.radians(near[0])), 1e-6)
    return (f"{near[1] - dlon},{near[0] + dlat},"
            f"{near[1] + dlon},{near[0] - dlat}")


def search(query: str, near: tuple[float, float] | None = None,
           limit: int = 5, radius_km: float = 2.5,
           timeout: float = 20.0) -> list[Place]:
    """Candidates for a spoken or typed destination, best match first."""
    def run(q: str, bounded: bool) -> list[Place]:
        params = {"q": q, "format": "jsonv2",
                  "limit": str(max(limit, 1)), "addressdetails": "0"}
        if near:
            params["viewbox"] = _viewbox(near, radius_km)
            params["bounded"] = "1" if bounded else "0"
        out: list[Place] = []
        for r in _get("search", params, timeout) or []:
            try:
                p = Place(name=r.get("display_name", query),
                          lat=float(r["lat"]), lon=float(r["lon"]),
                          kind=r.get("type", ""),
                          importance=float(r.get("importance") or 0.0))
            except (KeyError, TypeError, ValueError):
                continue
            if near:
                p.distance_m = haversine_m(near, p.coord)
            out.append(p)
        return out

    # Three passes, cheapest-and-most-likely first. Each only runs if the
    # one before it came back empty, so the common case is a single request.
    places: list[Place] = []
    if near:
        known = _CATEGORY.get(query.strip().lower())
        if known:
            # An unambiguous category word means the category, not a shop
            # that happens to contain it -- "bathroom" is a toilet, not
            # Bathroom Merchant Services Ltd.
            places = run(f"[{known}]", bounded=True)
        if not places:
            places = run(query, bounded=True)              # named place nearby
        if not places:                                     # unlisted category
            places = run(f"[{query.strip()}]", bounded=True)
    if not places:
        places = run(query, bounded=False)                 # anywhere on earth

    # Relevance leads; proximity only separates equally-good matches.
    places.sort(key=lambda p: (-round(p.importance, 2),
                               p.distance_m if p.distance_m is not None else 0.0))
    return places


def reverse(pos: tuple[float, float], timeout: float = 20.0) -> str:
    """What street are we on? Used for the 'from' label, never for routing."""
    try:
        r = _get("reverse", {"lat": f"{pos[0]:.6f}", "lon": f"{pos[1]:.6f}",
                             "format": "jsonv2", "zoom": "17"}, timeout)
        return str(r.get("display_name", "")) if isinstance(r, dict) else ""
    except Exception:
        return ""


def parse_coord(text: str) -> tuple[float, float] | None:
    """Accept 'lat,lon' so a destination can bypass geocoding entirely."""
    parts = [p.strip() for p in (text or "").replace(";", ",").split(",")]
    if len(parts) != 2:
        return None
    try:
        lat, lon = float(parts[0]), float(parts[1])
    except ValueError:
        return None
    if -90 <= lat <= 90 and -180 <= lon <= 180:
        return (lat, lon)
    return None


def resolve(text: str, near: tuple[float, float] | None = None) -> Place:
    """A destination from either a coordinate or a name. Raises if neither."""
    coord = parse_coord(text)
    if coord:
        return Place(name=f"{coord[0]:.5f},{coord[1]:.5f}",
                     lat=coord[0], lon=coord[1], kind="coordinate")
    hits = search(text, near=near)
    if not hits:
        raise LookupError(f"no place matching {text!r}")
    return hits[0]
