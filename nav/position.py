"""Where the wearer is, from one of three sources.

The glasses have no GPS -- the nRF52840 has Bluetooth and an IMU, nothing
that knows latitude. So position comes from somewhere else, and the rest of
the navigator is written against one small interface so it does not care
which:

  SimulatedSource  walks the route on its own, for developing and testing
                   the whole stack with no phone and no walking
  HttpSource       listens for fixes pushed from a phone over the LAN
  FixedSource      one constant position, for planning a route from a
                   place you are not standing in yet

HttpSource is stdlib `http.server` on a worker thread rather than aiohttp,
which is not installed and would be a dependency for about forty lines of
code. The thread publishes into a lock-guarded slot; the asyncio side only
ever reads it, so there is no cross-loop machinery to get wrong.

Phone side, the zero-install option is GPSLogger for Android (F-Droid or
Play), Log to custom URL:

    http://<laptop-ip>:8900/gps?lat=%LAT&lon=%LON&acc=%ACC&dir=%DIR&spd=%SPD

Browser geolocation is deliberately NOT used: Chrome refuses it on a plain
http:// LAN origin, so it would need TLS to work at all.
"""

from __future__ import annotations

import json
import math
import socket
import threading
import time
import urllib.parse
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from nav.router import bearing_deg, haversine_m

# A fix older than this is stale: we stop trusting it rather than guide from
# a position the wearer left several streets ago.
MAX_FIX_AGE = 20.0


@dataclass
class Fix:
    lat: float
    lon: float
    accuracy_m: float = 0.0
    heading_deg: float | None = None
    speed_mps: float | None = None
    t: float = 0.0                       # time.monotonic() when received
    source: str = ""

    @property
    def coord(self) -> tuple[float, float]:
        return (self.lat, self.lon)

    @property
    def age(self) -> float:
        return time.monotonic() - self.t

    @property
    def is_stale(self) -> bool:
        return self.age > MAX_FIX_AGE


def _now_fix(lat, lon, **kw) -> Fix:
    return Fix(lat=lat, lon=lon, t=time.monotonic(), **kw)


# ------------------------------------------------------------------ fixed

class FixedSource:
    """One constant position that never goes stale."""

    name = "fixed"

    def __init__(self, pos: tuple[float, float]) -> None:
        self._fix = _now_fix(pos[0], pos[1], accuracy_m=5.0, source="fixed")

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def latest(self) -> Fix | None:
        self._fix.t = time.monotonic()
        return self._fix


# -------------------------------------------------------------- simulated

class SimulatedSource:
    """Walks along a polyline at a constant speed.

    `lateral_m` can be set at any time to shove the walker sideways off the
    line, which is how the off-route and reroute paths get exercised without
    anyone actually walking into a side street.
    """

    name = "simulated"

    def __init__(self, shape: list[tuple[float, float]],
                 speed_mps: float = 1.4, start_offset_m: float = 0.0) -> None:
        self.shape = shape
        self.speed = speed_mps
        self.lateral_m = 0.0
        self._travelled = start_offset_m
        self._t0 = time.monotonic()
        self._paused_at: float | None = None
        self._seg_lengths = [haversine_m(shape[i], shape[i + 1])
                             for i in range(len(shape) - 1)] or [0.0]
        self.total_m = sum(self._seg_lengths)

    def start(self) -> None:
        self._t0 = time.monotonic()

    def stop(self) -> None:
        pass

    def pause(self) -> None:
        if self._paused_at is None:
            self._paused_at = time.monotonic()

    def resume(self) -> None:
        if self._paused_at is not None:
            self._t0 += time.monotonic() - self._paused_at
            self._paused_at = None

    def advance(self, metres: float) -> None:
        """Jump forward along the route without waiting for the clock.

        Used to replay a whole walk in a fraction of a second, and to skip
        ahead while debugging.
        """
        self._travelled += metres

    @property
    def distance_travelled(self) -> float:
        now = (self._paused_at if self._paused_at is not None
               else time.monotonic())
        return self._travelled + (now - self._t0) * self.speed

    def _point_at(self, d: float) -> tuple[tuple[float, float], float]:
        """(position, bearing) `d` metres along the shape."""
        if len(self.shape) < 2:
            return (self.shape[0] if self.shape else (0.0, 0.0)), 0.0
        d = max(0.0, min(d, self.total_m))
        for i, seg in enumerate(self._seg_lengths):
            if d <= seg or i == len(self._seg_lengths) - 1:
                f = (d / seg) if seg > 1e-6 else 0.0
                f = min(f, 1.0)
                a, b = self.shape[i], self.shape[i + 1]
                return ((a[0] + (b[0] - a[0]) * f,
                         a[1] + (b[1] - a[1]) * f), bearing_deg(a, b))
            d -= seg
        return self.shape[-1], 0.0

    def latest(self) -> Fix | None:
        pos, brg = self._point_at(self.distance_travelled)
        if self.lateral_m:
            # push perpendicular to travel, in a local flat-earth frame
            rad = math.radians(brg + 90.0)
            dn = math.cos(rad) * self.lateral_m / 111132.0
            de = (math.sin(rad) * self.lateral_m
                  / max(111320.0 * math.cos(math.radians(pos[0])), 1e-6))
            pos = (pos[0] + dn, pos[1] + de)
        return _now_fix(pos[0], pos[1], accuracy_m=6.0, heading_deg=brg,
                        speed_mps=self.speed, source="simulated")

    @property
    def finished(self) -> bool:
        return self.distance_travelled >= self.total_m


# ------------------------------------------------------------------- http

# Senders disagree about spelling, so accept every form anyone actually
# uses rather than making the phone match us.
_KEYS = {
    "lat": ("lat", "latitude"),
    "lon": ("lon", "lng", "long", "longitude"),
    "acc": ("acc", "accuracy", "hdop"),
    "dir": ("dir", "heading", "bearing", "course"),
    "spd": ("spd", "speed", "velocity"),
}


def _pick(params: dict, which: str) -> float | None:
    for key in _KEYS[which]:
        for spelling in (key, key.upper(), key.capitalize()):
            if spelling in params and params[spelling] not in ("", None):
                try:
                    return float(params[spelling])
                except (TypeError, ValueError):
                    return None
    return None


def fix_from_params(params: dict, source: str = "http") -> Fix | None:
    """Build a Fix from whatever key spelling the sender used."""
    lat, lon = _pick(params, "lat"), _pick(params, "lon")
    if lat is None or lon is None:
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    if lat == 0.0 and lon == 0.0:        # GPSLogger's "no fix yet" value
        return None
    return _now_fix(lat, lon, accuracy_m=_pick(params, "acc") or 0.0,
                    heading_deg=_pick(params, "dir"),
                    speed_mps=_pick(params, "spd"), source=source)


def lan_ip() -> str:
    """This machine's address on the LAN, as the phone would reach it."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))    # UDP: no packet is actually sent
        return sock.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        sock.close()


class HttpSource:
    name = "http"

    def __init__(self, port: int = 8900, host: str = "0.0.0.0",
                 on_fix=None) -> None:
        self.port, self.host = port, host
        self._lock = threading.Lock()
        self._fix: Fix | None = None
        self._count = 0
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._on_fix = on_fix

    @property
    def url(self) -> str:
        return (f"http://{lan_ip()}:{self.port}/gps"
                "?lat=%LAT&lon=%LON&acc=%ACC&dir=%DIR&spd=%SPD")

    @property
    def count(self) -> int:
        with self._lock:
            return self._count

    def _publish(self, fix: Fix) -> None:
        with self._lock:
            self._fix = fix
            self._count += 1
        if self._on_fix:
            try:
                self._on_fix(fix)
            except Exception:
                pass

    def start(self) -> None:
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args) -> None:    # keep the console clean
                pass

            def _reply(self, code: int, body: str,
                       ctype: str = "text/plain") -> None:
                raw = body.encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", ctype + "; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self) -> None:
                parsed = urllib.parse.urlparse(self.path)
                if parsed.path in ("/", "/help"):
                    self._reply(200, _HELP.format(url=outer.url, n=outer.count),
                                "text/html")
                    return
                params = {k: v[0] for k, v in
                          urllib.parse.parse_qs(parsed.query).items()}
                fix = fix_from_params(params)
                if fix is None:
                    self._reply(400, "need lat and lon\n")
                    return
                outer._publish(fix)
                self._reply(200, "ok\n")

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length).decode("utf-8", "replace")
                ctype = (self.headers.get("Content-Type") or "").lower()
                try:
                    if "json" in ctype:
                        params = json.loads(raw)
                    else:
                        params = {k: v[0] for k, v in
                                  urllib.parse.parse_qs(raw).items()}
                except Exception:
                    self._reply(400, "unparseable body\n")
                    return
                if not isinstance(params, dict):
                    self._reply(400, "expected an object\n")
                    return
                fix = fix_from_params(params)
                if fix is None:
                    self._reply(400, "need lat and lon\n")
                    return
                outer._publish(fix)
                self._reply(200, "ok\n")

        self._server = ThreadingHTTPServer((self.host, self.port), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        name="gps-http", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    def latest(self) -> Fix | None:
        with self._lock:
            return self._fix


_HELP = """<!doctype html><meta name=viewport content="width=device-width">
<style>body{{font:16px system-ui;margin:2rem;line-height:1.5;max-width:34rem}}
code{{background:#eee;padding:.2em .4em;word-break:break-all}}</style>
<h2>Frame navigation &mdash; GPS feed</h2>
<p>Fixes received: <b>{n}</b></p>
<p>Point GPSLogger (Android) at this URL, under
<i>Logging details &rarr; Log to custom URL</i>:</p>
<p><code>{url}</code></p>
<p>Or POST JSON <code>{{"lat":51.5,"lon":-0.12,"accuracy":8}}</code>
to <code>/gps</code>.</p>
"""
