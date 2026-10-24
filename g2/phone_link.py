"""Phone link: the Android companion forwards phone notifications and GPS fixes to this laptop.
The laptop keeps the glasses' Bluetooth link (gateway on 127.0.0.1:8765); the phone never talks
to the glasses.  See g2/PHONE.md.

    python g2/phone_link.py [--port 8768] [--no-push]   # standalone (pushes need the gateway running)

Transport: aiohttp WebSocket on ``ws://127.0.0.1:<phone.port>/phone`` (default 8768).  The phone
reaches it over USB with ``adb reverse tcp:8768 tcp:8768`` (phone's localhost -> laptop's), so
nothing listens on the LAN.  ``GET /phone/status`` returns a JSON summary.

Phone -> laptop (JSON text frames)::

    {"t":"hello","device":"Pixel"}
    {"t":"notif","app":"com.whatsapp","app_name":"WhatsApp","title":"Ann","text":"hi","time":1729760000000,
     "key":"0|com.whatsapp|1|...","ongoing":false,"silent":false}
    {"t":"notif_removed","key":"..."}
    {"t":"loc","lat":51.5,"lon":-0.12,"acc":5.0,"speed":1.2,"bearing":90.0,"time":1729760000000}

    {"t":"set","key":"height"|"distance"|"brightness"|"auto","value":n}

Laptop -> phone: ``{"t":"ack","for":<t>}`` after every valid message, ``{"t":"error","msg":...}`` for a
malformed one (the socket stays open), ``{"t":"config","notif_allow":[...],"loc_interval_s":n}``
after hello, and ``{"t":"display","height":h,"distance":d,"brightness":b,"auto":0|1,"battery":pct,
"charging":bool}`` on connect, every ``display_poll_s`` and right after a change.

Display settings (synced both ways, like Even's app): the glasses' settings snapshot is read with
``hub.glasses.read_settings()`` under ``hub.lock`` (absent proto3 fields = 0) and broadcast to every
phone.  A phone "set" is validated (height/distance 0..20, brightness 1..50, auto 0/1), coalesced
(applied at most every ``set_coalesce_s``, last value per key wins), applied with
``hub.glasses.apply_setting(settings.display_height/display_distance/brightness/auto_brightness)``,
re-read and broadcast.  Changes made elsewhere (g2/position.py, settings_cli.py) show up through the
poll.

Notifications: filtered (allow/deny lists, ongoing/silent dropped, empty dropped, the companion
itself and com.even.sg never), de-duplicated (same key + same content within ``dedupe_s``),
rate-limited to one card per ``min_interval_s`` with coalescing (a same-key update replaces the
queued one; several queued cards become one "N notifications" card), then pushed with
``hub.glasses.notify(...)`` under ``hub.lock``.  ``enable_notifications()`` runs once per phone
connection (and again after the glasses reconnect).  Every accepted notification is appended to
``g2/logs/notifications.jsonl`` (gitignored) for a later Notifications screen.

Location: the latest fix is ``PhoneLink.location`` (a ``nav.position.Fix``), the last
``ring_size`` fixes are in ``PhoneLink.ring``, ``on_location(cb)`` registers a listener, and
``latest()`` / ``start()`` / ``stop()`` make the link usable as a nav position source.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import sys
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Callable

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from nav.position import Fix  # noqa: E402

log = logging.getLogger("g2.phone")

#: Never forwarded, whatever the config says: the companion itself and Even's own app.
ALWAYS_DENY = ("com.kunsh.frame", "com.even.sg")


@dataclass
class PhoneConfig:
    enabled: bool = True
    host: str = "127.0.0.1"            # loopback only; the phone comes in through adb reverse
    port: int = 8768
    push: bool = True                  # False = log only, never touch the glasses
    form: str = "hub"                  # Glasses.notify form: hub | community
    allow: list[str] = field(default_factory=list)   # packages; empty = every app not denied
    deny: list[str] = field(default_factory=list)    # packages never forwarded
    drop_ongoing: bool = True
    drop_silent: bool = True
    dedupe_s: float = 10.0             # same key + same content within this window = duplicate
    min_interval_s: float = 3.0        # at most one card per this many seconds
    max_coalesce: int = 5              # lines in an "N notifications" card
    app_identifier: str | None = None  # card app_identifier; null = the phone package
    loc_interval_s: float = 5.0        # sent to the phone in the config message
    ring_size: int = 512               # location fixes kept in memory
    display_poll_s: float = 10.0       # glasses settings re-read + sent to the phone this often
    set_coalesce_s: float = 0.3        # slider drags: apply at most this often, last value wins
    log_path: str = "g2/logs/notifications.jsonl"

    @classmethod
    def from_dict(cls, d: "dict | PhoneConfig | None") -> "PhoneConfig":
        if isinstance(d, PhoneConfig):
            return d
        known = {f.name for f in fields(cls)}
        kw = {}
        for k, v in (d or {}).items():
            if k in known:
                kw[k] = v
            else:
                log.warning("hub config: unknown key phone.%s ignored", k)
        return cls(**kw)


@dataclass
class Notif:
    app: str
    app_name: str
    title: str
    text: str
    key: str
    time_ms: int
    received: float                    # PhoneLink clock

    def as_dict(self) -> dict:
        return {"app": self.app, "app_name": self.app_name, "title": self.title, "text": self.text,
                "key": self.key, "time": self.time_ms}


class BadMessage(ValueError):
    pass


#: phone "set" key -> (min, max) accepted values
SET_RANGES = {"height": (0, 20), "distance": (0, 20), "brightness": (1, 50), "auto": (0, 1)}


def validate_set(m: dict) -> tuple[str, int]:
    key = m.get("key")
    if key not in SET_RANGES:
        raise BadMessage(f"key must be one of {', '.join(SET_RANGES)}")
    v = m.get("value")
    if isinstance(v, bool):
        v = int(v)
    if not isinstance(v, (int, float)) or not math.isfinite(v) or int(v) != v:
        raise BadMessage("value must be an integer")
    lo, hi = SET_RANGES[key]
    if not lo <= int(v) <= hi:
        raise BadMessage(f"{key} must be {lo}..{hi}")
    return key, int(v)


def display_message(snap) -> dict:
    """settings.Snapshot -> the phone's "display" message (absent proto3 fields mean 0)."""
    g = lambda name: int(getattr(snap, name, None) or 0)  # noqa: E731
    return {"t": "display", "height": g("display_height"), "distance": g("display_distance"),
            "brightness": g("brightness_level"), "auto": 1 if g("brightness_auto") else 0,
            "battery": g("battery"), "charging": bool(g("charging"))}


def _str(m: dict, k: str, cap: int = 2000) -> str:
    v = m.get(k)
    if v is None:
        return ""
    if not isinstance(v, (str, int, float)):
        raise BadMessage(f"{k} must be a string")
    return str(v)[:cap]


def _num(m: dict, k: str, required: bool = False) -> float | None:
    v = m.get(k)
    if v is None:
        if required:
            raise BadMessage(f"{k} missing")
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        raise BadMessage(f"{k} must be a number")
    return float(v)


class PhoneLink:
    """WebSocket server for the phone companion.  ``hub`` may be None (log only)."""

    def __init__(self, hub=None, config: PhoneConfig | None = None, *,
                 clock: Callable[[], float] = time.monotonic, wall: Callable[[], float] = time.time,
                 log_path: str | Path | None = None) -> None:
        self.hub = hub
        self.config = config or PhoneConfig()
        self.clock = clock
        self.wall = wall
        p = Path(log_path) if log_path is not None else Path(self.config.log_path)
        self.log_path = p if p.is_absolute() else ROOT / p
        self.clients: set = set()
        self.device: str | None = None
        self.connections = 0               # phone connections accepted since start
        self.location: Fix | None = None
        self.ring: deque[Fix] = deque(maxlen=max(1, int(self.config.ring_size)))
        self.last_notif: Notif | None = None
        self.last_push: dict | None = None
        self.pushed = 0
        self.dropped: dict[str, int] = {}
        self.recent: deque[dict] = deque(maxlen=50)
        self._loc_cbs: list[Callable[[Fix], Any]] = []
        self._seen: dict[str, tuple[float, str]] = {}     # key -> (clock, content hash)
        self._pending: OrderedDict[str, Notif] = OrderedDict()
        self._wake = asyncio.Event()
        self._last_push_at: float | None = None
        self._notif_enabled = False
        self._sender: asyncio.Task | None = None
        self._runner = None
        self._msg_id = int(wall()) & 0x7FFFFF00
        self.display: dict | None = None          # last "display" message sent
        self.applied: list[tuple[str, int]] = []  # settings applied (most recent last, capped)
        self._pending_set: dict[str, int] = {}
        self._set_wake = asyncio.Event()
        self._display_wake = asyncio.Event()
        self._last_apply_at: float | None = None
        self._display_task: asyncio.Task | None = None
        self._setter: asyncio.Task | None = None

    # -- lifecycle ----------------------------------------------------------------------
    def make_app(self):
        from aiohttp import web
        app = web.Application()
        app.router.add_get("/phone", self._ws_handler)
        app.router.add_get("/phone/status", self._status_handler)
        app.on_startup.append(self._on_startup)
        app.on_cleanup.append(self._on_cleanup)
        return app

    async def _on_startup(self, app) -> None:
        self._ensure_sender()

    async def _on_cleanup(self, app) -> None:
        await self._stop_sender()

    def _ensure_sender(self) -> None:
        """Start the notification sender, the display poll and the settings applier (idempotent)."""
        if self._sender is None or self._sender.done():
            self._sender = asyncio.create_task(self._send_loop(), name="g2-phone-sender")
        if self._display_task is None or self._display_task.done():
            self._display_task = asyncio.create_task(self._display_loop(), name="g2-phone-display")
        if self._setter is None or self._setter.done():
            self._setter = asyncio.create_task(self._set_loop(), name="g2-phone-settings")

    async def _stop_sender(self) -> None:
        tasks = [t for t in (self._sender, self._display_task, self._setter) if t is not None]
        self._sender = self._display_task = self._setter = None
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass

    async def start(self) -> None:
        """Listen on ``config.host:config.port``."""
        if self._runner is not None:
            return
        from aiohttp import web
        runner = web.AppRunner(self.make_app(), access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, self.config.host, int(self.config.port))
        await site.start()
        self._runner = runner
        log.info("phone link listening on ws://%s:%d/phone", self.config.host, int(self.config.port))

    async def stop(self) -> None:
        for ws in list(self.clients):
            try:
                await ws.close()
            except Exception:
                pass
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
        await self._stop_sender()

    def on_glasses_connection(self, ready: bool) -> None:
        """The glasses (re)connected: the notification enable has to be sent again."""
        if ready:
            self._notif_enabled = False

    # -- position-source interface (nav/position.py) --------------------------------------
    name = "phone"

    def latest(self) -> Fix | None:
        return self.location

    def on_location(self, cb: Callable[[Fix], Any]) -> None:
        """``cb(fix)`` (sync or async) for every accepted fix."""
        self._loc_cbs.append(cb)

    def fix_age_s(self) -> float | None:
        return None if self.location is None else max(0.0, self.clock() - self.location.t)

    # -- HTTP ---------------------------------------------------------------------------
    def status(self) -> dict:
        f = self.location
        return {
            "connected": bool(self.clients), "clients": len(self.clients), "device": self.device,
            "connections": self.connections,
            "last_notif": self.last_notif.as_dict() if self.last_notif else None,
            "last_push": self.last_push,
            "pushed": self.pushed, "dropped": dict(self.dropped), "pending": len(self._pending),
            "last_fix": None if f is None else {"lat": f.lat, "lon": f.lon, "acc": f.accuracy_m,
                                                 "speed": f.speed_mps, "bearing": f.heading_deg},
            "last_fix_age_s": None if f is None else round(self.fix_age_s(), 1),
            "fixes": len(self.ring),
            "display": self.display,
        }

    async def _status_handler(self, request):
        from aiohttp import web
        return web.json_response(self.status())

    async def _ws_handler(self, request):
        from aiohttp import WSMsgType, web
        ws = web.WebSocketResponse(heartbeat=30.0, max_msg_size=256 * 1024)
        await ws.prepare(request)
        self._ensure_sender()
        self.clients.add(ws)
        self.connections += 1
        self._notif_enabled = False            # once per connection
        self._display_wake.set()               # read + send the display settings now
        log.info("phone connected (%d open)", len(self.clients))
        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    reply = await self.handle_text(msg.data)
                    for r in reply:
                        await ws.send_json(r)
                elif msg.type == WSMsgType.BINARY:
                    await ws.send_json({"t": "error", "msg": "binary frames not supported"})
                elif msg.type == WSMsgType.ERROR:
                    break
        finally:
            self.clients.discard(ws)
            log.info("phone disconnected (%d open)", len(self.clients))
        return ws

    # -- messages -----------------------------------------------------------------------
    async def handle_text(self, text: str) -> list[dict]:
        """One phone message -> the replies to send back.  Never raises."""
        try:
            m = json.loads(text)
        except (ValueError, TypeError):
            return [{"t": "error", "msg": "not JSON"}]
        if not isinstance(m, dict) or not isinstance(m.get("t"), str):
            return [{"t": "error", "msg": "expected an object with a string 't'"}]
        t = m["t"]
        try:
            if t == "hello":
                self.device = _str(m, "device", 64) or None
                log.info("phone hello: %s", self.device or "?")
                return [{"t": "ack", "for": t}, self.config_message()]
            if t == "notif":
                self.on_notif(m)
            elif t == "notif_removed":
                self.on_notif_removed(_str(m, "key", 512))
            elif t == "loc":
                await self.on_loc(m)
            elif t == "set":
                self.on_set(m)
            elif t == "ping":
                pass
            else:
                return [{"t": "error", "msg": f"unknown type {t!r}"}]
        except BadMessage as e:
            return [{"t": "error", "msg": f"{t}: {e}"}]
        except Exception as e:  # a bug here must not drop the phone
            log.exception("phone %s handling failed", t)
            return [{"t": "error", "msg": f"{t}: {type(e).__name__}"}]
        return [{"t": "ack", "for": t}]

    def config_message(self) -> dict:
        return {"t": "config", "notif_allow": list(self.config.allow), "loc_interval_s": self.config.loc_interval_s}

    # -- notifications ------------------------------------------------------------------
    def _drop(self, why: str) -> str:
        self.dropped[why] = self.dropped.get(why, 0) + 1
        return why

    def filter_reason(self, n: Notif, m: dict) -> str | None:
        c = self.config
        if n.app in ALWAYS_DENY or n.app in c.deny:
            return "denied"
        if c.allow and n.app not in c.allow:
            return "not_allowed"
        if c.drop_ongoing and m.get("ongoing") is True:
            return "ongoing"
        if c.drop_silent and m.get("silent") is True:
            return "silent"
        if not n.title.strip() and not n.text.strip():
            return "empty"
        return None

    def on_notif(self, m: dict) -> str | None:
        """Accept or drop one notification; returns the drop reason or None when queued."""
        app = _str(m, "app", 128)
        if not app:
            raise BadMessage("app missing")
        tm = _num(m, "time")
        now = self.clock()
        n = Notif(app=app, app_name=_str(m, "app_name", 64) or app, title=_str(m, "title", 256),
                  text=_str(m, "text", 2000), key=_str(m, "key", 512), time_ms=int(tm) if tm is not None else int(self.wall() * 1000),
                  received=now)
        why = self.filter_reason(n, m)
        if why:
            log.debug("notif from %s dropped: %s", app, why)
            return self._drop(why)
        dkey = n.key or f"{n.app}|{n.title}|{n.text}"
        content = f"{n.title}\x00{n.text}"
        for k, (at, _) in list(self._seen.items()):
            if now - at > self.config.dedupe_s:
                del self._seen[k]
        prev = self._seen.get(dkey)
        if prev is not None and prev[1] == content:
            return self._drop("duplicate")
        self._seen[dkey] = (now, content)
        self.last_notif = n
        self.recent.append(n.as_dict())
        self._log({"t": "notif", **n.as_dict(), "received": int(self.wall() * 1000)})
        self._pending.pop(dkey, None)          # a same-key update replaces the queued card
        self._pending[dkey] = n
        self._wake.set()
        return None

    def on_notif_removed(self, key: str) -> None:
        if not key:
            raise BadMessage("key missing")
        if self._pending.pop(key, None) is not None:
            log.debug("queued notif %s removed on the phone", key)
        self._log({"t": "removed", "key": key, "received": int(self.wall() * 1000)})

    def _log(self, rec: dict) -> None:
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.log_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError as e:
            log.warning("notification log: %s", e)

    def build_card(self, batch: list[Notif]) -> dict:
        """One queued notification -> its own card; several -> one coalesced card."""
        if len(batch) == 1:
            n = batch[0]
            title = n.title or n.app_name
            return {"title": title, "subtitle": n.app_name if n.title else "", "message": n.text or n.title,
                    "display_name": n.app_name, "app_identifier": self.config.app_identifier or n.app}
        apps = list(OrderedDict.fromkeys(n.app_name for n in batch))
        lines = [f"{n.app_name}: {n.title}" + (f" - {n.text}" if n.text else "") for n in batch[-self.config.max_coalesce:]]
        if len(batch) > self.config.max_coalesce:
            lines.insert(0, f"(+{len(batch) - self.config.max_coalesce} more)")
        return {"title": f"{len(batch)} notifications", "subtitle": ", ".join(apps), "message": "\n".join(lines),
                "display_name": apps[0] if len(apps) == 1 else "Phone",
                "app_identifier": self.config.app_identifier or (batch[-1].app if len(apps) == 1 else "com.even.sg")}

    async def _send_loop(self) -> None:
        while True:
            await self._wake.wait()
            self._wake.clear()
            if not self._pending:
                continue
            if self._last_push_at is not None:
                wait = self._last_push_at + self.config.min_interval_s - self.clock()
                if wait > 0:
                    await asyncio.sleep(wait)      # later arrivals coalesce into this batch
            if not self._pending:
                continue
            batch = list(self._pending.values())
            self._pending.clear()
            self._last_push_at = self.clock()
            try:
                await self.push_card(self.build_card(batch), len(batch))
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("notification push failed")

    async def push_card(self, card: dict, count: int = 1) -> bool:
        glasses = getattr(self.hub, "glasses", None)
        ok = False
        if not self.config.push or glasses is None:
            log.info("notif (not pushed): %s / %s", card["title"], card["message"][:60])
        else:
            lock = getattr(self.hub, "lock", None) or asyncio.Lock()
            async with lock:
                try:
                    if not self._notif_enabled:
                        await glasses.enable_notifications()
                        self._notif_enabled = True
                    self._msg_id = (self._msg_id + 1) & 0x7FFFFFFF
                    ok = bool(await glasses.notify(card["title"], card["message"], card["subtitle"], form=self.config.form,
                                                   display_name=card["display_name"], app_identifier=card["app_identifier"],
                                                   msg_id=self._msg_id))
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    log.warning("notify failed: %s", e)
                    ok = False
            (log.info if ok else log.warning)("notif %s: %s (%d coalesced)", "pushed" if ok else "NOT acked", card["title"], count)
        self.pushed += 1 if ok else 0
        self.last_push = {"title": card["title"], "count": count, "ok": ok, "at": int(self.wall() * 1000)}
        return ok

    # -- display settings ---------------------------------------------------------------
    def on_set(self, m: dict) -> tuple[str, int]:
        """Queue one validated phone "set"; a later value for the same key replaces it (slider drags)."""
        key, value = validate_set(m)
        self._pending_set[key] = value
        self._set_wake.set()
        return key, value

    async def broadcast(self, msg: dict) -> int:
        n = 0
        for ws in list(self.clients):
            try:
                await ws.send_json(msg)
                n += 1
            except Exception as e:
                log.debug("phone send failed: %s", e)
        return n

    async def refresh_display(self) -> dict | None:
        """Read the glasses' settings snapshot (under hub.lock) and send it to every phone."""
        glasses = getattr(self.hub, "glasses", None)
        if glasses is None or not hasattr(glasses, "read_settings"):
            return None
        lock = getattr(self.hub, "lock", None) or asyncio.Lock()
        try:
            async with lock:
                snap = await glasses.read_settings()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.debug("settings read failed: %s", e)
            return None
        if snap is None:
            return None
        self.display = display_message(snap)
        await self.broadcast(self.display)
        return self.display

    async def apply_pending(self) -> list[tuple[str, int]]:
        """Apply every queued "set" (last value per key), then re-read and broadcast."""
        from g2.native import settings
        batch, self._pending_set = self._pending_set, {}
        if not batch:
            return []
        glasses = getattr(self.hub, "glasses", None)
        if glasses is None:
            log.info("display set %s ignored: no glasses", batch)
            return []
        build = {"height": settings.display_height, "distance": settings.display_distance,
                 "brightness": settings.brightness, "auto": lambda mg, v: settings.auto_brightness(mg, bool(v))}
        done: list[tuple[str, int]] = []
        lock = getattr(self.hub, "lock", None) or asyncio.Lock()
        async with lock:
            for key, value in batch.items():
                try:
                    await glasses.apply_setting(build[key](glasses.alloc.next_magic(), value))
                    done.append((key, value))
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    log.warning("display set %s=%s failed: %s", key, value, e)
        self.applied.extend(done)
        del self.applied[:-50]
        log.info("display set %s", ", ".join(f"{k}={v}" for k, v in done) or "nothing")
        await self.refresh_display()
        return done

    async def _set_loop(self) -> None:
        while True:
            await self._set_wake.wait()
            self._set_wake.clear()
            if self._last_apply_at is not None:
                wait = self._last_apply_at + self.config.set_coalesce_s - self.clock()
                if wait > 0:
                    await asyncio.sleep(wait)      # more drag values arrive meanwhile; the last one wins
            if not self._pending_set:
                continue
            self._last_apply_at = self.clock()
            try:
                await self.apply_pending()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("display set failed")
            self._last_apply_at = self.clock()

    async def _display_loop(self) -> None:
        while True:
            try:
                await asyncio.wait_for(self._display_wake.wait(), self.config.display_poll_s)
            except asyncio.TimeoutError:
                pass
            self._display_wake.clear()
            if not self.clients:
                continue
            try:
                await self.refresh_display()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("display refresh failed")

    # -- location -----------------------------------------------------------------------
    async def on_loc(self, m: dict) -> Fix:
        lat, lon = _num(m, "lat", True), _num(m, "lon", True)
        if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
            raise BadMessage("lat/lon out of range")
        acc, speed, bearing = _num(m, "acc"), _num(m, "speed"), _num(m, "bearing")
        fix = Fix(lat=lat, lon=lon, accuracy_m=acc or 0.0, heading_deg=bearing, speed_mps=speed,
                  t=self.clock(), source="phone")
        self.location = fix
        self.ring.append(fix)
        for cb in list(self._loc_cbs):
            try:
                r = cb(fix)
                if asyncio.iscoroutine(r):
                    await r
            except Exception:
                log.exception("on_location callback failed")
        return fix


def attach(hub, config: dict | PhoneConfig | None = None, **kw) -> PhoneLink:
    """Build the link for ``hub`` (config: ``hub.config.phone`` when not given) and set ``hub.phone``."""
    if config is None:
        config = getattr(getattr(hub, "config", None), "phone", None)
    cfg = PhoneConfig.from_dict(config)
    link = PhoneLink(hub, cfg, **kw)
    if hub is not None:
        hub.phone = link
    return link


async def _main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--port", type=int)
    ap.add_argument("--no-push", action="store_true", help="log only; do not open the gateway")
    ap.add_argument("-v", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.v else logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    from g2.hub import HubConfig
    hcfg = HubConfig.load()
    cfg = hcfg.phone
    if a.port:
        cfg.port = a.port
    hub = None
    if a.no_push:
        cfg.push = False
    else:
        from g2.hub import Hub
        from g2.native.transport import GatewayRawTransport, Glasses
        hub = Hub(Glasses(GatewayRawTransport(hcfg.gateway), log=logging.getLogger("g2.native").info),
                  HubConfig.from_dict({"menu": {"enabled": False}, "keepalive": {"enabled": False},
                                       "time": {"enabled": False}, "calendar": {"enabled": False},
                                       "tasks": {"enabled": False}}))
        await hub.glasses.start()
    link = attach(hub, cfg)
    await link.start()
    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await link.stop()
        if hub is not None:
            await hub.glasses.stop()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(_main()))
    except KeyboardInterrupt:
        pass
