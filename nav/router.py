"""Walking routes from FOSSGIS Valhalla, normalised for a tiny display.

Valhalla is used because it needs no API key and no credit card, has the
richest pedestrian cost model of any option, and returns `street_names` as a
clean field rather than buried in English prose (which is what rules out
Google's Routes API for a HUD).

Rate limit is 1 request/sec/user, and the maintainers ask for a real
User-Agent and X-Client-Id. We make ONE request per walk, so that is ample:
distance-to-turn and off-route detection are local geometry against the
returned shape.

GOTCHA: Valhalla reports lengths in KILOMETRES even when you ask for
`units: meters`. Everything here converts to metres at the boundary.
"""

from __future__ import annotations

import json
import math
import urllib.error
import urllib.request
from dataclasses import dataclass, field

VALHALLA_URL = "https://valhalla1.openstreetmap.de/route"
USER_AGENT = "frame-nav-hud/0.1 (personal project; Brilliant Labs Frame)"

# Valhalla emits 44 maneuver types; a pedestrian route uses a small subset.
# Collapse them to the icons we can actually draw and read at 20 degrees FOV.
_ICON_BY_TYPE = {
    0: "straight", 1: "depart", 2: "depart", 3: "depart",
    4: "arrive", 5: "arrive", 6: "arrive",
    7: "straight", 8: "straight",
    9: "slight-right", 10: "right", 11: "sharp-right",
    12: "uturn", 13: "uturn",
    14: "sharp-left", 15: "left", 16: "slight-left",
    17: "straight", 18: "right", 19: "left",
    20: "right", 21: "left",
    22: "straight", 23: "slight-right", 24: "slight-left",
    25: "straight",
    26: "roundabout", 27: "roundabout",
    37: "right", 38: "left",
    39: "steps", 40: "steps", 41: "steps",
    42: "straight", 43: "straight",
}


_ABBREV = (("Street", "St"), ("Road", "Rd"), ("Avenue", "Ave"),
           ("Lane", "Ln"), ("Drive", "Dr"), ("Place", "Pl"),
           ("Square", "Sq"), ("Crescent", "Cres"), ("Gardens", "Gdns"),
           ("Terrace", "Terr"), ("Boulevard", "Blvd"))


def _shorten(name: str) -> str:
    for long, short in _ABBREV:
        name = name.replace(long, short)
    return name.strip()


@dataclass
class Step:
    icon: str
    street: str
    metres: float            # length of THIS step
    seconds: float
    instruction: str
    verbal: str
    begin_index: int         # index into the decoded shape
    end_index: int
    raw_type: int

    @property
    def label(self) -> str:
        """What to print beside the arrow.

        Roughly a third of the maneuvers on a real city walk have no
        `street_names` at all -- crosswalks, walkways, stairs, plazas. An
        empty label next to a turn arrow looks broken, so fall back to the
        object Valhalla names in its own instruction ("Turn left onto the
        walkway." -> "the walkway").
        """
        if self.short_street:
            return self.short_street
        text = self.instruction.rstrip(".").strip()
        for sep in (" onto ", " on ", " to take "):
            if sep in text:
                return _shorten(text.rsplit(sep, 1)[1])[:24]
        return _shorten(text)[:24]

    @property
    def short_street(self) -> str:
        """Street name trimmed for a 26-character panel."""
        return _shorten(self.street)[:24]


@dataclass
class Route:
    steps: list[Step]
    shape: list[tuple[float, float]] = field(default_factory=list)
    total_metres: float = 0.0
    total_seconds: float = 0.0


def decode_polyline6(encoded: str) -> list[tuple[float, float]]:
    """Valhalla uses precision 6, not Google's 5."""
    coords, index, lat, lng = [], 0, 0, 0
    while index < len(encoded):
        for target in ("lat", "lng"):
            shift, result = 0, 0
            while True:
                byte = ord(encoded[index]) - 63
                index += 1
                result |= (byte & 0x1F) << shift
                shift += 5
                if byte < 0x20:
                    break
            delta = ~(result >> 1) if result & 1 else (result >> 1)
            if target == "lat":
                lat += delta
            else:
                lng += delta
        coords.append((lat / 1e6, lng / 1e6))
    return coords


def haversine_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    r = 6371000.0
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dp = p2 - p1
    dl = math.radians(b[1] - a[1])
    h = (math.sin(dp / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2)
    return 2 * r * math.asin(math.sqrt(h))


def bearing_deg(a: tuple[float, float], b: tuple[float, float]) -> float:
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dl = math.radians(b[1] - a[1])
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def route(start: tuple[float, float], end: tuple[float, float],
          timeout: float = 25.0) -> Route:
    """One walking route. Raises on failure -- the caller decides what to do."""
    body = json.dumps({
        "locations": [{"lat": start[0], "lon": start[1]},
                      {"lat": end[0], "lon": end[1]}],
        "costing": "pedestrian",
        "directions_options": {"units": "kilometers"},
    }).encode()

    req = urllib.request.Request(
        VALHALLA_URL, data=body,
        headers={"User-Agent": USER_AGENT,
                 "X-Client-Id": "frame-nav-hud",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read())

    trip = data["trip"]
    if trip.get("status") != 0:
        raise RuntimeError(trip.get("status_message", "no route"))

    leg = trip["legs"][0]
    shape = decode_polyline6(leg["shape"])

    steps: list[Step] = []
    for m in leg["maneuvers"]:
        names = m.get("street_names") or m.get("begin_street_names") or []
        street = names[0] if names and names[0] != "-" else ""
        steps.append(Step(
            icon=_ICON_BY_TYPE.get(m["type"], "straight"),
            street=street,
            metres=float(m.get("length", 0.0)) * 1000.0,   # km -> m
            seconds=float(m.get("time", 0.0)),
            instruction=m.get("instruction", "").strip(),
            verbal=(m.get("verbal_pre_transition_instruction")
                    or m.get("instruction", "")).strip(),
            begin_index=int(m.get("begin_shape_index", 0)),
            end_index=int(m.get("end_shape_index", 0)),
            raw_type=int(m["type"]),
        ))

    summary = trip["summary"]
    return Route(steps=steps, shape=shape,
                 total_metres=float(summary["length"]) * 1000.0,
                 total_seconds=float(summary["time"]))


def distance_along(shape: list[tuple[float, float]],
                   i: int, j: int) -> float:
    return sum(haversine_m(shape[k], shape[k + 1])
               for k in range(max(i, 0), min(j, len(shape) - 1)))


def nearest_point(shape: list[tuple[float, float]],
                  pos: tuple[float, float]) -> tuple[int, float]:
    """Closest shape index to a position, and the distance to it in metres."""
    best_i, best_d = 0, float("inf")
    for i, pt in enumerate(shape):
        d = haversine_m(pos, pt)
        if d < best_d:
            best_i, best_d = i, d
    return best_i, best_d


# ---------------------------------------------------------------- snapping
#
# Nearest-vertex snapping is too coarse for guidance: Valhalla's shape has
# vertices tens of metres apart on a straight road, so "distance to the turn"
# would jump in steps instead of counting down. These project onto the
# SEGMENTS instead, in a local flat-earth frame -- over the few hundred
# metres that matter, the error is far below GPS noise.


def _metres_per_degree(lat: float) -> tuple[float, float]:
    """(per degree latitude, per degree longitude) at this latitude."""
    return 111132.0, 111320.0 * math.cos(math.radians(lat))


@dataclass
class Snap:
    seg_i: int                      # shape[seg_i] -> shape[seg_i + 1]
    t: float                        # 0..1 along that segment
    point: tuple[float, float]
    lateral_m: float                # how far off the line we are
    bearing_deg: float              # direction of travel along the route here


def _project(a: tuple[float, float], b: tuple[float, float],
             p: tuple[float, float]) -> tuple[float, tuple[float, float], float]:
    mlat, mlon = _metres_per_degree(p[0])
    ax, ay = (a[1] - p[1]) * mlon, (a[0] - p[0]) * mlat
    bx, by = (b[1] - p[1]) * mlon, (b[0] - p[0]) * mlat
    dx, dy = bx - ax, by - ay
    denom = dx * dx + dy * dy
    if denom < 1e-9:                       # degenerate segment
        return 0.0, a, math.hypot(ax, ay)
    t = -(ax * dx + ay * dy) / denom
    t = max(0.0, min(1.0, t))
    qx, qy = ax + t * dx, ay + t * dy
    point = (p[0] + qy / mlat, p[1] + qx / mlon)
    return t, point, math.hypot(qx, qy)


def project_segment(a: tuple[float, float], b: tuple[float, float],
                    p: tuple[float, float]) -> tuple[float, tuple[float, float], float]:
    """Project p onto one segment: (fraction along, point, metres off it).

    Public because snapping POLICY belongs in guidance -- which candidate
    wins is about route continuity, not geometry -- while the geometry
    itself belongs here.
    """
    return _project(a, b, p)


def project_on_route(shape: list[tuple[float, float]],
                     pos: tuple[float, float],
                     from_i: int = 0,
                     look_ahead: int | None = None) -> Snap:
    """Closest point on the route polyline.

    `from_i` and `look_ahead` bound the search so a route that doubles back
    on itself cannot snap us backwards onto a leg we already walked.
    """
    if len(shape) < 2:
        return Snap(0, 0.0, shape[0] if shape else pos, 0.0, 0.0)

    lo = max(0, min(from_i, len(shape) - 2))
    hi = len(shape) - 1 if look_ahead is None else min(len(shape) - 1,
                                                       lo + look_ahead)
    best: Snap | None = None
    for i in range(lo, max(hi, lo + 1)):
        t, point, d = _project(shape[i], shape[i + 1], pos)
        if best is None or d < best.lateral_m:
            best = Snap(i, t, point, d, bearing_deg(shape[i], shape[i + 1]))
    return best                               # type: ignore[return-value]


def distance_to_index(shape: list[tuple[float, float]], snap: Snap,
                      j: int) -> float:
    """Metres from the snapped position forward along the route to shape[j]."""
    j = max(0, min(j, len(shape) - 1))
    if j <= snap.seg_i:
        return 0.0
    rest = haversine_m(snap.point, shape[snap.seg_i + 1])
    return rest + distance_along(shape, snap.seg_i + 1, j)
