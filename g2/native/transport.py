"""Transport abstraction, the local gateway raw transport, a fake for tests, and ``Glasses``,
the high-level async helper.  Standard library only; nothing here opens a connection until
``GatewayRawTransport.frames()`` is iterated or ``write`` is awaited.

Transports see raw frames only (``AA 21 ...`` out, ``AA 12 ...`` in).  Re-assembly, ack
matching on (sid, magic) and timing live in :class:`Glasses`.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import struct
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable, Protocol, Sequence, runtime_checkable

from . import dashboard, even_ai, menu as menu_mod, notifications, session, settings
from .framing import FrameAssembler, MagicAllocator, ParsedFrame, frame_pb, parse_frame, read_ack_magic

LEFT, RIGHT, BOTH = session.LEFT, session.RIGHT, session.BOTH


@runtime_checkable
class Transport(Protocol):
    async def write(self, side: str, frame: bytes, response: bool = False) -> None:
        """Write one raw frame to characteristic 0x5401 of ``side`` ('left' | 'right' | 'both')."""

    def frames(self) -> AsyncIterator[tuple[str, bytes]]:
        """Inbound raw frames as ``(side, bytes)`` ('left' | 'right'); never ends by itself."""


# ═══════════════════════════════════════════════════════════════════════════════════════
# minimal WebSocket client (RFC 6455, text frames) over asyncio streams
# ═══════════════════════════════════════════════════════════════════════════════════════


class _WebSocket:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.r, self.w = reader, writer

    @classmethod
    async def connect(cls, url: str, timeout: float = 10.0) -> "_WebSocket":
        from urllib.parse import urlsplit
        u = urlsplit(url)
        host, port = u.hostname or "127.0.0.1", u.port or (443 if u.scheme == "wss" else 80)
        path = (u.path or "/") + (f"?{u.query}" if u.query else "")
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port, ssl=(u.scheme == "wss")), timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
        writer.write(req.encode())
        await writer.drain()
        status = await asyncio.wait_for(reader.readline(), timeout)
        if b" 101 " not in status:
            writer.close()
            raise ConnectionError(f"websocket handshake failed: {status!r}")
        while True:
            line = await reader.readline()
            if line in (b"\r\n", b"\n", b""):
                break
        return cls(reader, writer)

    async def _send(self, opcode: int, data: bytes) -> None:
        mask = os.urandom(4)
        head = bytearray([0x80 | opcode])
        n = len(data)
        if n < 126:
            head.append(0x80 | n)
        elif n < 65536:
            head += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            head += bytes([0x80 | 127]) + struct.pack(">Q", n)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        self.w.write(bytes(head) + mask + masked)
        await self.w.drain()

    async def send_text(self, text: str) -> None:
        await self._send(0x1, text.encode())

    async def recv(self) -> tuple[int, bytes]:
        """Next complete message as (opcode, payload); pings are answered, close raises."""
        buf = bytearray()
        op0 = None
        while True:
            h = await self.r.readexactly(2)
            fin, opcode = h[0] & 0x80, h[0] & 0x0F
            masked, n = h[1] & 0x80, h[1] & 0x7F
            if n == 126:
                n = struct.unpack(">H", await self.r.readexactly(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", await self.r.readexactly(8))[0]
            mask = await self.r.readexactly(4) if masked else None
            data = await self.r.readexactly(n) if n else b""
            if mask:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            if opcode == 0x8:
                raise ConnectionError("websocket closed")
            if opcode == 0x9:
                await self._send(0xA, data)
                continue
            if opcode == 0xA:
                continue
            if opcode in (0x1, 0x2):
                op0 = opcode
                buf = bytearray(data)
            else:
                buf += data
            if fin:
                return op0 or 0x1, bytes(buf)

    async def close(self) -> None:
        try:
            await self._send(0x8, b"\x03\xe8")
        except Exception:
            pass
        self.w.close()


# ═══════════════════════════════════════════════════════════════════════════════════════
# gateway transport
# ═══════════════════════════════════════════════════════════════════════════════════════


class GatewayRawTransport:
    """Raw frames through the local gateway (men-g2-ble-gateway) that owns the Bluetooth link.

    * ``write`` -> ``POST {base}/api/raw`` with ``{"side": "left"|"right"|"both", "hex": "...", "response": false}``;
      ``write_many`` sends ``{"side": ..., "frames": ["hex", ...]}`` in one request.
    * ``write_file`` targets the EvenFile characteristic (``"char": "7401"``) for notification JSON.
    * ``frames()`` reads ``glasses.raw_packet`` events from ``{base}/ws`` (``{"kind": "glasses.raw_packet",
      "data": {"source": "L"|"R", "hex": "..."}}``; a flat ``{source, hex}`` is accepted too) and
      reconnects with back-off.  Nothing connects until ``frames()`` is iterated.
    """

    def __init__(self, base: str = "http://127.0.0.1:8765", timeout: float = 10.0) -> None:
        self.base = base.rstrip("/")
        self.ws_url = self.base.replace("http", "ws", 1) + "/ws"
        self.timeout = timeout
        self.connected = False

    # -- HTTP ----------------------------------------------------------------------------
    def _post_sync(self, path: str, body: dict) -> dict:
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:  # noqa: S310 - local gateway
                raw = r.read()
        except urllib.error.HTTPError as e:
            raise ConnectionError(f"{path} -> {e.code}: {e.read()[:200]!r}") from e
        except urllib.error.URLError as e:
            raise ConnectionError(f"{path}: {e.reason}") from e
        try:
            return json.loads(raw) if raw else {}
        except ValueError:
            return {"raw": raw.decode("utf-8", "replace")}

    async def post(self, path: str, body: dict) -> dict:
        return await asyncio.to_thread(self._post_sync, path, body)

    async def write(self, side: str, frame: bytes, response: bool = False) -> None:
        await self.post("/api/raw", {"side": side, "hex": bytes(frame).hex(), "response": bool(response)})

    async def write_many(self, side: str, frames: Sequence[bytes], response: bool = False) -> None:
        await self.post("/api/raw", {"side": side, "frames": [bytes(f).hex() for f in frames], "response": bool(response)})

    async def write_file(self, side: str, frame: bytes, response: bool = False) -> None:
        await self.post("/api/raw", {"side": side, "hex": bytes(frame).hex(), "response": bool(response), "char": "7401"})

    # -- WebSocket -----------------------------------------------------------------------
    @staticmethod
    def parse_event(e: dict) -> tuple[str, bytes] | None:
        kind = e.get("kind") or e.get("type")
        d = e.get("data") if isinstance(e.get("data"), dict) else e
        if kind not in (None, "glasses.raw_packet", "raw_packet") or "hex" not in d:
            return None
        src = str(d.get("source") or d.get("side") or "").upper()
        side = LEFT if src.startswith("L") else RIGHT if src.startswith("R") else None
        if side is None:
            return None
        try:
            return side, bytes.fromhex(d["hex"])
        except ValueError:
            return None

    async def frames(self) -> AsyncIterator[tuple[str, bytes]]:
        delay = 1.0
        while True:
            ws = None
            try:
                ws = await _WebSocket.connect(self.ws_url, self.timeout)
                self.connected = True
                delay = 1.0
                while True:
                    op, data = await ws.recv()
                    if op != 0x1:
                        continue
                    try:
                        ev = json.loads(data)
                    except ValueError:
                        continue
                    item = self.parse_event(ev) if isinstance(ev, dict) else None
                    if item is not None:
                        yield item
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            finally:
                self.connected = False
                if ws is not None:
                    await ws.close()
            await asyncio.sleep(delay)
            delay = min(delay * 2, 10.0)


# ═══════════════════════════════════════════════════════════════════════════════════════
# fake transport for tests
# ═══════════════════════════════════════════════════════════════════════════════════════

Responder = Callable[[str, ParsedFrame, bytes], "list[tuple[str, bytes]] | None"]


def rx_frame(sid: int, payload: bytes, seq: int = 1, flag: int = 0x00) -> bytes:
    """Build a glasses -> app frame (``AA 12 ...``) for fakes."""
    f = bytearray(frame_pb(payload, sid, flag, seq)[0])
    f[1] = 0x12
    return bytes(f)


class FakeTransport:
    """Records every write and feeds scripted replies.

    ``responder(side, parsed, raw) -> [(side, rx_frame), ...] | None`` is called for every
    written frame; returned frames are queued for ``frames()``.  ``push(side, frame)`` injects
    unsolicited frames.  ``writes`` is ``[(side, frame, response)]``; ``file_writes`` likewise for
    :meth:`write_file`.
    """

    def __init__(self, responder: Responder | None = None, reply_delay: float = 0.0) -> None:
        self.responder = responder
        self.reply_delay = reply_delay
        self.writes: list[tuple[str, bytes, bool]] = []
        self.file_writes: list[tuple[str, bytes, bool]] = []
        self._q: asyncio.Queue[tuple[str, bytes]] = asyncio.Queue()

    def push(self, side: str, frame: bytes) -> None:
        self._q.put_nowait((side, bytes(frame)))

    async def _respond(self, side: str, frame: bytes) -> None:
        if self.responder is None:
            return
        parsed = parse_frame(frame)
        if parsed is None:
            return
        out = self.responder(side, parsed, frame)
        if not out:
            return
        if self.reply_delay:
            await asyncio.sleep(self.reply_delay)
        for s, f in out:
            self.push(s, f)

    async def write(self, side: str, frame: bytes, response: bool = False) -> None:
        self.writes.append((side, bytes(frame), response))
        await self._respond(side, frame)

    async def write_many(self, side: str, frames: Sequence[bytes], response: bool = False) -> None:
        for f in frames:
            await self.write(side, f, response)

    async def write_file(self, side: str, frame: bytes, response: bool = False) -> None:
        self.file_writes.append((side, bytes(frame), response))
        await self._respond(side, frame)

    async def frames(self) -> AsyncIterator[tuple[str, bytes]]:
        while True:
            yield await self._q.get()

    # -- helpers for assertions ----------------------------------------------------------
    def payloads(self, side: str | None = None, sid: int | None = None) -> list[bytes]:
        out = []
        asm = {LEFT: FrameAssembler(), RIGHT: FrameAssembler()}
        for s, f, _ in self.writes:
            data = asm.get(s, asm[RIGHT]).feed(f)
            if data is None:
                continue
            p = parse_frame(data)
            if p is None or (side and s != side) or (sid is not None and p.sid != sid):
                continue
            out.append(p.payload)
        return out


def fake_glasses_responder(battery: int = 87, charging: int = 0, worn: int = 1, case_battery: int = 60,
                           fw: str = "1.2.3", news_slots: int = 5) -> Responder:
    """A responder that behaves like bonded glasses: auth reply ending ``1a 00``, dashboard acks
    flag 0, settings snapshot, wear/case replies, keep-alive echo on the right lens, news FIFO
    count / status replies, menu + quicklist echoes."""
    from .pb import Writer, message_field, varint_field

    def respond(side: str, p: ParsedFrame, raw: bytes) -> list[tuple[str, bytes]] | None:
        cmd, magic = dashboard.read_cmd(p.payload), read_ack_magic(p.payload)
        if magic is None:
            return None
        head = varint_field(1, cmd) + varint_field(2, magic)
        if p.sid == 0x80 and cmd == 4:
            return [(side, rx_frame(0x80, head + message_field(3, b"")))]
        if p.sid == 0x80 and cmd == 14:
            return [(side, rx_frame(0x80, head + message_field(13, b"")))] if side == RIGHT else None
        if side != RIGHT:
            return None
        if p.sid == 0x01:
            if cmd == 2:
                return [(side, rx_frame(0x01, head + message_field(3, varint_field(1, 1) + varint_field(2, 0))))]
            if cmd == 13:
                return [(side, rx_frame(0x01, varint_field(1, 14) + varint_field(2, magic) + message_field(16, b"")))]
            if cmd == 7:
                return [(side, rx_frame(0x01, varint_field(1, 8) + varint_field(2, magic) + message_field(10, varint_field(1, news_slots))))]
            if cmd == 9:
                return [(side, rx_frame(0x01, varint_field(1, 10) + varint_field(2, magic) + message_field(12, varint_field(5, 0))))]
        if p.sid == 0x09 and cmd == 2:
            snap = (Writer().varint(1, 1).varint(2, 30).varint(3, 10).varint(4, 10).string(5, fw).string(6, fw).varint(7, 1)
                    .varint(8, 25).varint(10, 1).varint(12, battery).varint(13, charging).varint(14, 0).varint(18, 1).varint(19, 2))
            return [(side, rx_frame(0x09, head + message_field(4, snap.to_bytes())))]
        if p.sid == 0x09:
            return [(side, rx_frame(0x09, head + message_field(6, varint_field(1, 1) + varint_field(2, 0))))]
        if p.sid == 0x10 and cmd == 3:
            return [(side, rx_frame(0x10, head + message_field(5, varint_field(1, 1) + varint_field(2, worn))))]
        if p.sid == 0x81:
            return [(side, rx_frame(0x81, head + message_field(3, varint_field(1, case_battery) + varint_field(2, 0))))]
        if p.sid == 0x03:
            return [(side, rx_frame(0x03, varint_field(1, 1) + varint_field(2, magic)))]
        if p.sid == 0x0C:
            return [(side, rx_frame(0x0C, p.payload))]
        if p.sid in (0x04, 0x05, 0x06, 0x07, 0x08, 0x0D, 0x10, 0x20):
            return [(side, rx_frame(p.sid, head))]
        return None

    return respond


# ═══════════════════════════════════════════════════════════════════════════════════════
# Glasses: high-level async helper
# ═══════════════════════════════════════════════════════════════════════════════════════

Expect = "str | int | Callable[[ParsedFrame], bool] | None"


@dataclass
class Inbound:
    side: str
    frame: ParsedFrame
    at: float


class Glasses:
    """Drive the native firmware through any :class:`Transport`.

    ``start()`` begins reading frames (re-assembled per lens, routed to waiters and ``on`` listeners).
    ``send`` writes and optionally waits for the matching reply; ``run_plan`` executes
    ``session.MinimalDashboardPlan``; the ``set_*``/``push_*`` helpers reproduce the hub's push
    patterns (heartbeat on, push, heartbeat off).  Timing constants come from ``session``.
    """

    def __init__(self, transport: Transport, alloc: MagicAllocator | None = None, clock: Callable[[], float] = time.time,
                 tz_units: int | None = None, log: Callable[[str], None] | None = None) -> None:
        self.t = transport
        self.alloc = alloc or MagicAllocator()
        self.clock = clock
        self.tz_units = tz_units
        self.log = log or (lambda m: None)
        self._asm = {LEFT: FrameAssembler(), RIGHT: FrameAssembler()}
        self._waiters: list[tuple[Callable[[str, ParsedFrame], bool], asyncio.Future]] = []
        self._listeners: list[Callable[[str, ParsedFrame], Any]] = []
        self._reader: asyncio.Task | None = None
        self._hb: asyncio.Task | None = None
        self._ka: asyncio.Task | None = None
        self.hb_count = 0
        self.last_keepalive_echo: float | None = None
        self.missed_keepalives = 0
        self.status: dict[str, Any] = {}
        self.snapshot: settings.Snapshot | None = None
        self.inbound: list[Inbound] = []
        self.keep_inbound = 256
        self.on_link_lost: Callable[[str], Any] | None = None
        #: multiply every protocol wait (tests set it small); 1.0 = the hub's real timing
        self.time_scale = 1.0

    async def _sleep(self, seconds: float) -> None:
        if seconds > 0:
            await asyncio.sleep(seconds * self.time_scale)

    # -- lifecycle ---------------------------------------------------------------------
    async def start(self) -> None:
        if self._reader is None:
            self._reader = asyncio.create_task(self._read_loop(), name="g2-native-reader")

    async def stop(self) -> None:
        self.stop_heartbeat()
        for t in (self._ka, self._reader):
            if t is not None:
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass
        self._ka = self._reader = None

    async def __aenter__(self) -> "Glasses":
        await self.start()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.stop()

    def on(self, fn: Callable[[str, ParsedFrame], Any]) -> None:
        """Register ``fn(side, parsed_frame)`` for every inbound frame (after waiters)."""
        self._listeners.append(fn)

    async def _read_loop(self) -> None:
        async for side, raw in self.t.frames():
            data = self._asm.get(side, self._asm[RIGHT]).feed(raw)
            if data is None:
                continue
            p = parse_frame(data)
            if p is None:
                continue
            self._dispatch(side, p)

    def _dispatch(self, side: str, p: ParsedFrame) -> None:
        self.inbound.append(Inbound(side, p, self.clock()))
        del self.inbound[:-self.keep_inbound]
        self._track(side, p)
        for pred, fut in list(self._waiters):
            if fut.done():
                self._waiters.remove((pred, fut))
                continue
            try:
                hit = pred(side, p)
            except Exception:
                hit = False
            if hit:
                self._waiters.remove((pred, fut))
                fut.set_result(p)
        for fn in list(self._listeners):
            try:
                r = fn(side, p)
                if asyncio.iscoroutine(r):
                    asyncio.ensure_future(r)
            except Exception as e:  # pragma: no cover - listener bugs must not kill the reader
                self.log(f"listener error: {e!r}")

    def _track(self, side: str, p: ParsedFrame) -> None:
        try:
            if p.sid == session.SID_SETTINGS:
                m = settings.parse(p.payload)
                if m.snapshot is not None:
                    self.snapshot = m.snapshot
                    for k, v in m.snapshot.as_dict().items():
                        self.status[k] = v
            elif p.sid == session.SID_CASE:
                c = session.parse_case(p.payload)
                if c and c.battery is not None:
                    self.status["case_battery"] = c.battery
            elif p.sid == session.SID_ONBOARDING:
                w = session.parse_wear_reply(p.payload)
                if w is not None:
                    self.status["worn"] = w
            elif p.sid == session.SID_APP_SYNC:
                a = session.parse_app_sync(p.payload)
                if a.foreground is not None:
                    self.status["foreground"] = a.foreground
            elif p.sid == session.SID_DEVICE:
                if side == RIGHT and session.parse_device(p.payload).is_keepalive_echo:
                    self.last_keepalive_echo = self.clock()
            elif p.sid == settings.SID_MODULE:
                mm = settings.parse_module(p.payload)
                if mm.timeout is not None:
                    self.status["display_timeout"] = mm.timeout
        except Exception as e:
            self.log(f"decode error sid=0x{p.sid:02x}: {e!r}")

    # -- waiting -----------------------------------------------------------------------
    def _wait(self, pred: Callable[[str, ParsedFrame], bool]) -> asyncio.Future:
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._waiters.append((pred, fut))
        return fut

    @staticmethod
    def _predicate(expect, side: str, sid: int | None, magic: int | None) -> Callable[[str, ParsedFrame], bool]:
        want_side = RIGHT if side == BOTH else side
        if callable(expect):
            return lambda s, p: bool(expect(p))
        if isinstance(expect, int):
            return lambda s, p: p.sid == expect
        if expect == "auth":
            return lambda s, p: s == want_side and p.sid == session.SID_DEVICE and session.parse_auth_reply(p.payload)
        if expect in ("ack", "echo", "snapshot", "reply"):
            def pred(s: str, p: ParsedFrame) -> bool:
                if s != want_side or p.sid != sid:
                    return False
                if magic is not None and read_ack_magic(p.payload) != magic:
                    return False
                if expect == "snapshot":
                    return settings.parse(p.payload).snapshot is not None
                return True
            return pred
        raise ValueError(f"unknown expect {expect!r}")

    async def send(self, side: str, frame: bytes | Sequence[bytes], expect=None, timeout: float = 3.0,
                   sid: int | None = None, magic: int | None = None) -> ParsedFrame | None:
        """Write ``frame`` (or a list of fragments, 50 ms apart) and, when ``expect`` is given, wait up
        to ``timeout`` s for the matching reply: 'auth' | 'ack' | 'echo' | 'snapshot' | 'reply' (matched on
        ``sid``+``magic``, taken from the frame when not given), an int SID, or a predicate on
        ParsedFrame.  Returns the reply's ParsedFrame or None on timeout."""
        frames = [frame] if isinstance(frame, (bytes, bytearray)) else list(frame)
        first = parse_frame(frames[0]) if frames else None
        if first is not None:
            sid = first.sid if sid is None else sid
            if magic is None and first.total <= 1:
                magic = read_ack_magic(first.payload)
        fut = self._wait(self._predicate(expect, side, sid, magic)) if expect is not None else None
        try:
            for n, f in enumerate(frames):
                if n:
                    await self._sleep(session.PACKET_GAP_S)
                await self.t.write(side, f, False)
            if fut is None:
                return None
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            self.log(f"no reply (sid=0x{sid or 0:02x} magic={magic} expect={expect})")
            return None
        finally:
            if fut is not None and not fut.done():
                fut.cancel()

    async def send_pb(self, sid: int, payload: bytes, side: str = RIGHT, expect=None, timeout: float = 3.0,
                      flag: int | None = None) -> ParsedFrame | None:
        """Frame ``payload`` for ``sid`` with the app's flag rule and send; see :meth:`send` for ``expect``."""
        f = session.flag_for(sid, payload) if flag is None else flag
        if side == BOTH:
            await self.send(RIGHT, frame_pb(payload, sid, f, self.alloc.next_seq()))
            return await self.send(LEFT, frame_pb(payload, sid, f, self.alloc.next_seq()), expect, timeout, sid)
        return await self.send(side, frame_pb(payload, sid, f, self.alloc.next_seq()), expect, timeout, sid)

    async def request(self, sid: int, payload: bytes, timeout: float = 4.0) -> ParsedFrame | None:
        """Send to the right lens and wait for the reply echoing (sid, magic)."""
        return await self.send_pb(sid, payload, RIGHT, "ack", timeout)

    # -- EvenHub heartbeat ---------------------------------------------------------------
    def start_heartbeat(self) -> bool:
        """(Re)start the SID 0xE0 heartbeat every 4 s. Returns True if started now."""
        if self._hb is not None and not self._hb.done():
            return False
        self._hb = asyncio.create_task(self._hb_loop(), name="g2-evenhub-heartbeat")
        return True

    def stop_heartbeat(self) -> bool:
        """Stop it: the native dashboard takes the screen back ~5 s later."""
        t, self._hb = self._hb, None
        if t is None or t.done():
            return False
        t.cancel()
        return True

    @property
    def heartbeat_running(self) -> bool:
        return self._hb is not None and not self._hb.done()

    async def _hb_loop(self) -> None:
        while True:
            try:
                await self.send_pb(session.SID_EVENHUB, session.evenhub_heartbeat(self.alloc.next_magic()))
                self.hb_count += 1
            except Exception as e:
                self.log(f"heartbeat write failed: {e!r}")
            await self._sleep(session.HEARTBEAT_INTERVAL_S)

    async def ensure_heartbeat(self, settle: float = 2.0) -> None:
        """Start the heartbeat if needed and give the glasses 2 s to see it (features.dart:98-105)."""
        if self.start_heartbeat():
            await self._sleep(settle)

    # -- plans -------------------------------------------------------------------------
    async def run_step(self, st: session.Step) -> ParsedFrame | None:
        if st.action == "start_heartbeat":
            self.start_heartbeat()
            if st.wait_ms:
                await self._sleep(st.wait_ms / 1000)
            return None
        if st.action == "stop_heartbeat":
            self.stop_heartbeat()
            if st.wait_ms:
                await self._sleep(st.wait_ms / 1000)
            return None
        reply = await self.send(st.side, st.frames, st.expect, st.timeout_ms / 1000, st.sid, st.magic)
        if st.wait_ms:
            await self._sleep(st.wait_ms / 1000)
        return reply

    async def run_plan(self, plan, stop_on_failure: bool = False) -> list[tuple[session.Step, ParsedFrame | None]]:
        """Execute a plan (``session.MinimalDashboardPlan`` or any iterable of Steps).  Returns
        ``[(step, reply)]``; a step that expected a reply and got none has ``reply is None``."""
        results = []
        for st in plan:
            reply = await self.run_step(st)
            results.append((st, reply))
            self.log(f"step {st.number} {st.label}: {'reply' if reply is not None else ('no reply' if st.expect else 'sent')}")
            if stop_on_failure and st.expect and reply is None:
                break
        return results

    async def connect_sequence(self, full: bool = False) -> None:
        """The captured post-auth housekeeping (session.connect_sequence), 150 ms apart."""
        now = int(self.clock())
        for sid, payload, side in session.connect_sequence(self.alloc.next_magic, now, self.tz_units, full):
            await self.send_pb(sid, payload, side)
            await self._sleep(session.CONNECT_SEQUENCE_GAP_S)

    # -- settings ----------------------------------------------------------------------
    async def read_settings(self, timeout: float = 3.0) -> settings.Snapshot | None:
        """Settings READ basic -> Snapshot (battery, charging, height, distance, fw, head-up, ...)."""
        r = await self.send_pb(session.SID_SETTINGS, settings.read_settings(self.alloc.next_magic()), RIGHT, "snapshot", timeout)
        return settings.parse(r.payload).snapshot if r is not None else None

    async def apply_setting(self, payload: bytes, sid: int = session.SID_SETTINGS, timeout: float = 3.0) -> ParsedFrame | None:
        """Send one SET/module payload (built with ``settings.*``) and wait for its ack/echo."""
        return await self.send_pb(sid, payload, RIGHT, "ack", timeout)

    async def refresh_status(self) -> dict[str, Any]:
        """Hub "refresh status" burst: settings READ, wear, case, timeout READ, 150 ms apart."""
        for sid, build in ((session.SID_SETTINGS, settings.read_settings), (session.SID_ONBOARDING, session.wear_query),
                           (session.SID_CASE, session.case_query), (settings.SID_MODULE, settings.read_display_timeout)):
            await self.send_pb(sid, build(self.alloc.next_magic()))
            await self._sleep(session.CONNECT_SEQUENCE_GAP_S)
        await self._sleep(0.5)
        return dict(self.status)

    # -- dashboard pushes (heartbeat on -> push -> heartbeat off) -------------------------
    async def _dash(self, payload: bytes, timeout: float = 4.0) -> int | None:
        r = await self.request(session.SID_DASHBOARD, payload, timeout)
        return None if r is None else (dashboard.ack_flag(r.payload) if dashboard.ack_flag(r.payload) is not None else 0)

    async def set_weather(self, temperature_c: float, weather_type: int = 0, status: str = "", fahrenheit_display: bool = False,
                          show: bool = True, **extra) -> int | None:
        """Weather status bar (LIVE).  Returns the ack flag (0 = success) or None."""
        await self.ensure_heartbeat()
        unit = dashboard.TEMP_FAHRENHEIT if fahrenheit_display else dashboard.TEMP_CELSIUS
        flag = await self._dash(dashboard.weather(self.alloc.next_magic(), temperature_c, unit, weather_type,
                                                  int(self.clock() * 1000), status, **extra))
        if show:
            self.stop_heartbeat()
        return flag

    async def set_calendar(self, schedules: Sequence[dashboard.Schedule], show: bool = True) -> list[int | None]:
        """One message per event (or the "none" message), each awaited (LIVE)."""
        await self.ensure_heartbeat()
        flags = [await self._dash(p) for p in dashboard.calendar_sequence(self.alloc.next_magic, schedules)]
        if show:
            self.stop_heartbeat()
        return flags

    async def push_news(self, items: Sequence[dashboard.NewsItem], show: bool = True) -> list[int | None]:
        """News V3 FIFO: reset -> count -> per slot (LIVE).  Returns the newsStatus per slot."""
        if not items:
            return []
        await self.ensure_heartbeat()
        await self.request(session.SID_DASHBOARD, dashboard.news_reset(self.alloc.next_magic()))
        await self._sleep(0.5)
        r = await self.request(session.SID_DASHBOARD, dashboard.news_fifo_count_request(self.alloc.next_magic()))
        count = dashboard.news_fifo_count(r.payload if r else None)
        await self._sleep(0.2)
        now = int(self.clock())
        tz = session.local_tz_units(now) if self.tz_units is None else self.tz_units
        report_ms = (now + tz * 900) * 1000
        out = []
        for slot in range(count):
            it = items[slot % len(items)]
            r = await self.request(session.SID_DASHBOARD, dashboard.news_push(self.alloc.next_magic(), slot, it, report_ms, count))
            out.append(dashboard.news_status(r.payload if r else None))
            await self._sleep(0.15)
        if show:
            self.stop_heartbeat()
        return out

    async def set_tasks(self, tasks: Sequence[dashboard.Task], show: bool = True) -> bool:
        """Quicklist: init -> 300 ms -> FULL_UPDATE (echo) -> display-enable (LIVE)."""
        await self.ensure_heartbeat()
        ok = True
        for i, (sid, payload, expect) in enumerate(dashboard.tasks_sequence(self.alloc.next_magic, tasks)):
            r = await self.send_pb(sid, payload, RIGHT, expect, 4.0)
            ok = ok and r is not None and (expect != "ack" or (dashboard.ack_flag(r.payload) or 0) == 0)
            if i == 0:
                await self._sleep(0.3)
        if show:
            self.stop_heartbeat()
        return ok

    async def set_menu(self, builtin_ids=menu_mod.DEFAULT_MENU, plugins=(), timeout: float = 4.0) -> bool:
        """Whole-menu replacement (LIVE for built-ins); True when the cmd-1 reply echoed the magic."""
        r = await self.request(menu_mod.SID_MENU, menu_mod.menu_push(self.alloc.next_magic(), builtin_ids, plugins, strict=True), timeout)
        return r is not None

    # -- notifications -------------------------------------------------------------------
    async def enable_notifications(self, disp_time: int = 5) -> None:
        await self.send_pb(session.SID_NOTIFICATION, notifications.notification_ctrl(self.alloc.next_magic(), disp_time=disp_time))
        await self._sleep(session.CONNECT_SEQUENCE_GAP_S)
        await self.send_pb(session.SID_NOTIFICATION, notifications.whitelist_disable(self.alloc.next_magic()))

    async def _write_file(self, frame: bytes) -> None:
        wf = getattr(self.t, "write_file", None)
        if wf is not None:
            await wf(RIGHT, frame, False)
        else:
            await self.t.write(RIGHT, frame, False)

    async def notify(self, title: str, message: str, subtitle: str = "", form: str = "hub", timeout: float = 8.0, **kw) -> bool:
        """Send a notification card.  ``form='hub'`` = START/DATA/RESULT_CHECK with awaited ``[cid, rsp]``
        replies (DECODED/UNTESTED); ``form='community'`` = the Jan-2026 community sequence, fire and
        forget with 0.3 s gaps (COMMUNITY).  Frames go to the EvenFile characteristic through
        ``transport.write_file`` when available."""
        data = notifications.notification_json(title, message, subtitle, now=int(self.clock()), **kw)
        if form == "community":
            frames = notifications.community_notification_frames(data)
            gaps = [0.3, 0.1] + [0.05] * (len(frames) - 3) + [0.3]
            for f, g in zip(frames, gaps):
                await self._write_file(f)
                await self._sleep(g)
            await self.send_pb(session.SID_DEVICE, session.keepalive(self.alloc.next_magic()), LEFT)
            return True
        for st in notifications.hub_notification_steps(self.alloc.next_seq, data):
            for attempt in range(4):
                fut = self._wait(lambda s, p, cid=st.expect_cid: s == RIGHT and p.sid in (0xC4, 0xC5)
                                 and notifications.parse_file_reply(p.payload) is not None
                                 and notifications.parse_file_reply(p.payload)[0] == cid)
                for f in st.frames:
                    await self._write_file(f)
                    await self._sleep(0.01)
                try:
                    reply = await asyncio.wait_for(fut, timeout)
                except asyncio.TimeoutError:
                    rsp = None
                else:
                    rsp = notifications.parse_file_reply(reply.payload)[1]
                if rsp == 0:
                    break
                if st.name != "data" or (rsp is not None and rsp not in notifications.RSP_RETRY):
                    self.log(f"EFS {st.name}: {notifications.RSP_NAMES.get(rsp, rsp)}")
                    return False
                if rsp == 5:
                    await self._sleep(5)
            else:
                return False
        return True

    # -- keep-alive ----------------------------------------------------------------------
    async def keepalive_once(self) -> bool:
        """LEFT keep-alive, 2 s, RIGHT keep-alive; True when the right lens echoed within 5 s."""
        await self.send_pb(session.SID_DEVICE, session.keepalive(self.alloc.next_magic()), LEFT)
        await self._sleep(session.KEEPALIVE_LEFT_LEAD_S)
        r = await self.send_pb(session.SID_DEVICE, session.keepalive(self.alloc.next_magic()), RIGHT, "echo", session.KEEPALIVE_ECHO_WAIT_S)
        return r is not None

    async def keepalive_loop(self, interval: float = session.KEEPALIVE_INTERVAL_S, left_lead: float = session.KEEPALIVE_LEFT_LEAD_S,
                             echo_wait: float = session.KEEPALIVE_ECHO_WAIT_S, time_sync_every: float = session.TIME_SYNC_INTERVAL_S) -> None:
        """Every ``interval`` s: keep-alive to both lenses (left first); hourly time re-sync; after two
        missed right-lens echoes call ``on_link_lost`` and return (glasses_session.dart:245-284)."""
        last_sync = self.clock()
        self.missed_keepalives = 0
        while True:
            await self._sleep(interval)
            await self.send_pb(session.SID_DEVICE, session.keepalive(self.alloc.next_magic()), LEFT)
            await self._sleep(left_lead)
            r = await self.send_pb(session.SID_DEVICE, session.keepalive(self.alloc.next_magic()), RIGHT, "echo", echo_wait)
            if r is not None:
                self.missed_keepalives = 0
            else:
                self.missed_keepalives += 1
                self.log(f"keep-alive: no echo from the right lens ({self.missed_keepalives}/2)")
                if self.missed_keepalives >= 2:
                    if self.on_link_lost:
                        self.on_link_lost("keep-alive watchdog")
                    return
            if self.clock() - last_sync > time_sync_every:
                await self.send_pb(session.SID_DEVICE, session.time_sync(self.alloc.next_magic(), int(self.clock()), self.tz_units))
                last_sync = self.clock()

    def start_keepalive(self, **kw) -> None:
        if self._ka is None or self._ka.done():
            self._ka = asyncio.create_task(self.keepalive_loop(**kw), name="g2-keepalive")

    # -- Even AI convenience -------------------------------------------------------------
    async def even_ai_answer(self, question: str, answer: str, community: bool = False) -> None:
        """ENTER -> ASK -> ANALYSE -> REPLY chunks -> EXIT (or the community minimal path)."""
        m = self.alloc.next_magic
        await self.send_pb(even_ai.SID_EVEN_AI, even_ai.enter(m()))
        await self._sleep(0.3)
        await self.send_pb(even_ai.SID_EVEN_AI, even_ai.ask(m(), question, community))
        if community:
            await self._sleep(1.0)
            await self.send_pb(even_ai.SID_EVEN_AI, even_ai.reply(m(), answer, True, community=True))
            return
        await self._sleep(0.3)
        await self.send_pb(even_ai.SID_EVEN_AI, even_ai.analyse(m()))
        for p in even_ai.reply_sequence(m, answer):
            await self.send_pb(even_ai.SID_EVEN_AI, p)
            await self._sleep(even_ai.REPLY_PACE_MS / 1000)
