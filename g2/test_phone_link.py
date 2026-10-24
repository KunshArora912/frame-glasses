"""Phone link tests: filtering, dedupe, rate limit + coalescing, location ring, reconnect,
enable_notifications once per connection, malformed messages.  aiohttp test client against a fake
Glasses and a recording hub lock; no hardware, no gateway, no phone.

    python -m unittest g2.test_phone_link
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from aiohttp import WSMsgType  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

from g2.phone_link import PhoneConfig, PhoneLink, attach  # noqa: E402


class FakeGlasses:
    def __init__(self, lock=None, ok=True) -> None:
        self.lock = lock
        self.ok = ok
        self.enables = 0
        self.notes: list[dict] = []
        self.calls: list[str] = []
        self.locked_during: list[bool] = []

    async def enable_notifications(self, disp_time: int = 5) -> None:
        self.enables += 1
        self.calls.append("enable")

    async def notify(self, title, message, subtitle="", form="hub", **kw) -> bool:
        self.locked_during.append(bool(self.lock and self.lock.locked()))
        self.notes.append({"title": title, "message": message, "subtitle": subtitle, "form": form, **kw})
        self.calls.append("notify")
        return self.ok


class RecordingLock:
    """asyncio.Lock that counts acquisitions (the hub's push lock)."""

    def __init__(self) -> None:
        self._l = asyncio.Lock()
        self.acquired = 0

    def locked(self) -> bool:
        return self._l.locked()

    async def __aenter__(self):
        await self._l.acquire()
        self.acquired += 1
        return self

    async def __aexit__(self, *exc):
        self._l.release()


class Clock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def notif(key="k1", app="com.whatsapp", title="Ann", text="hi", **kw) -> dict:
    return {"t": "notif", "app": app, "app_name": "WhatsApp", "title": title, "text": text,
            "time": 1729760000000, "key": key, **kw}


class Base(unittest.IsolatedAsyncioTestCase):
    def make(self, clock=None, **cfg) -> PhoneLink:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.lock = RecordingLock()
        self.glasses = FakeGlasses(self.lock)
        self.hub = SimpleNamespace(glasses=self.glasses, lock=self.lock, config=SimpleNamespace(phone={}))
        kw = {"clock": clock} if clock else {}
        self.link = attach(self.hub, PhoneConfig(**{"min_interval_s": 0.25, **cfg}),
                           log_path=Path(self.tmp.name) / "notifications.jsonl", **kw)
        return self.link

    async def client(self) -> TestClient:
        c = TestClient(TestServer(self.link.make_app()))
        await c.start_server()
        self.addAsyncCleanup(c.close)
        return c

    async def settle(self, n_notes: int, timeout: float = 2.0) -> None:
        t = 0.0
        while len(self.glasses.notes) < n_notes and t < timeout:
            await asyncio.sleep(0.02)
            t += 0.02


class FilterTests(Base):
    def test_deny_allow_and_always_denied(self):
        link = self.make(deny=["com.spam"], allow=[])
        self.assertEqual(link.on_notif(notif(app="com.spam")), "denied")
        self.assertEqual(link.on_notif(notif(app="com.kunsh.frame", key="a")), "denied")
        self.assertEqual(link.on_notif(notif(app="com.even.sg", key="b")), "denied")
        self.assertIsNone(link.on_notif(notif(app="com.whatsapp", key="c")))
        link.config.allow = ["com.signal"]
        self.assertEqual(link.on_notif(notif(app="com.whatsapp", key="d")), "not_allowed")
        self.assertIsNone(link.on_notif(notif(app="com.signal", key="e")))
        self.assertEqual(link.dropped, {"denied": 3, "not_allowed": 1})

    def test_ongoing_silent_empty_dropped(self):
        link = self.make()
        self.assertEqual(link.on_notif(notif(key="a", ongoing=True)), "ongoing")
        self.assertEqual(link.on_notif(notif(key="b", silent=True)), "silent")
        self.assertEqual(link.on_notif(notif(key="c", title=" ", text="")), "empty")
        link.config.drop_silent = False
        self.assertIsNone(link.on_notif(notif(key="d", silent=True)))

    def test_dedupe_same_key_same_content_within_window(self):
        clk = Clock()
        link = self.make(clock=clk)
        self.assertIsNone(link.on_notif(notif()))
        clk.t += 5
        self.assertEqual(link.on_notif(notif()), "duplicate")
        clk.t += 11                                   # outside the 10 s window
        self.assertIsNone(link.on_notif(notif()))

    def test_same_key_update_replaces_queued_card(self):
        link = self.make(clock=Clock())
        link.on_notif(notif(text="1 new message"))
        link.on_notif(notif(text="2 new messages"))
        link.on_notif(notif(key="other", title="Bob"))
        self.assertEqual([n.text for n in link._pending.values()], ["2 new messages", "hi"])

    def test_notif_removed_drops_queued(self):
        link = self.make()
        link.on_notif(notif(key="gone"))
        link.on_notif_removed("gone")
        self.assertEqual(len(link._pending), 0)

    def test_log_file_written(self):
        link = self.make()
        link.on_notif(notif(title="Ann", text="hello"))
        link.on_notif(notif(key="x", ongoing=True))   # dropped: not logged
        link.on_notif_removed("k1")
        rows = [json.loads(x) for x in link.log_path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([r["t"] for r in rows], ["notif", "removed"])
        self.assertEqual(rows[0]["text"], "hello")
        self.assertEqual(rows[0]["app"], "com.whatsapp")

    def test_coalesced_card(self):
        link = self.make(max_coalesce=2)
        link.on_notif(notif(key="a", title="Ann", text="1"))
        link.on_notif(notif(key="b", title="Bob", text="2"))
        link.on_notif(notif(key="c", title="Cy", text="3"))
        card = link.build_card(list(link._pending.values()))
        self.assertEqual(card["title"], "3 notifications")
        self.assertIn("(+1 more)", card["message"])
        self.assertIn("WhatsApp: Cy - 3", card["message"])
        self.assertNotIn("Ann", card["message"])


class WebSocketTests(Base):
    async def test_hello_gets_ack_and_config(self):
        self.make(allow=["com.signal"], loc_interval_s=7)
        c = await self.client()
        ws = await c.ws_connect("/phone")
        await ws.send_json({"t": "hello", "device": "Phone"})
        self.assertEqual(await ws.receive_json(), {"t": "ack", "for": "hello"})
        self.assertEqual(await ws.receive_json(), {"t": "config", "notif_allow": ["com.signal"], "loc_interval_s": 7})
        r = await c.get("/phone/status")
        st = await r.json()
        self.assertTrue(st["connected"])
        self.assertEqual(st["device"], "Phone")
        await ws.close()

    async def test_notif_pushed_under_lock_and_enable_once(self):
        self.make()
        c = await self.client()
        ws = await c.ws_connect("/phone")
        await ws.send_json(notif(key="a", title="Ann", text="hi"))
        self.assertEqual(await ws.receive_json(), {"t": "ack", "for": "notif"})
        await self.settle(1)
        await asyncio.sleep(0.3)                      # past the rate window
        await ws.send_json(notif(key="b", title="Bob", text="yo"))
        await ws.receive_json()
        await self.settle(2)
        self.assertEqual(self.glasses.enables, 1)
        self.assertEqual(self.glasses.calls, ["enable", "notify", "notify"])
        self.assertEqual(self.glasses.locked_during, [True, True])
        n = self.glasses.notes[0]
        self.assertEqual((n["title"], n["subtitle"], n["message"]), ("Ann", "WhatsApp", "hi"))
        self.assertEqual(n["display_name"], "WhatsApp")
        self.assertEqual(n["app_identifier"], "com.whatsapp")
        self.assertNotEqual(self.glasses.notes[0]["msg_id"], self.glasses.notes[1]["msg_id"])
        self.assertEqual(self.link.pushed, 2)
        await ws.close()

    async def test_rate_limit_coalesces(self):
        self.make(min_interval_s=0.4)
        c = await self.client()
        ws = await c.ws_connect("/phone")
        for k in ("a", "b", "c"):
            await ws.send_json(notif(key=k, title=k.upper(), text=k))
            await ws.receive_json()
        await self.settle(1)
        await asyncio.sleep(0.1)
        self.assertEqual(len(self.glasses.notes), 1)  # first one at once, the rest wait for the window
        await self.settle(2)
        self.assertEqual(len(self.glasses.notes), 2)
        self.assertEqual(self.glasses.notes[0]["title"], "A")
        self.assertEqual(self.glasses.notes[1]["title"], "2 notifications")
        await asyncio.sleep(0.5)
        self.assertEqual(len(self.glasses.notes), 2)  # nothing more queued
        await ws.close()

    async def test_reconnect_reenables_notifications(self):
        self.make(min_interval_s=0.0)
        c = await self.client()
        ws = await c.ws_connect("/phone")
        await ws.send_json(notif(key="a"))
        await ws.receive_json()
        await self.settle(1)
        await ws.close()
        await asyncio.sleep(0.05)
        self.assertFalse((await (await c.get("/phone/status")).json())["connected"])
        ws2 = await c.ws_connect("/phone")
        await ws2.send_json(notif(key="b", text="again"))
        await ws2.receive_json()
        await self.settle(2)
        self.assertEqual(self.glasses.enables, 2)
        self.assertEqual(self.link.connections, 2)
        self.assertTrue(self.link.status()["connected"])
        # glasses reconnect: enable again on the next push
        self.link.on_glasses_connection(True)
        await ws2.send_json(notif(key="c", text="third"))
        await ws2.receive_json()
        await self.settle(3)
        self.assertEqual(self.glasses.enables, 3)
        await ws2.close()

    async def test_malformed_messages_keep_socket_open(self):
        self.make()
        c = await self.client()
        ws = await c.ws_connect("/phone")
        bad = ["not json", "[1,2]", json.dumps({"x": 1}), json.dumps({"t": "dance"}),
               json.dumps({"t": "notif", "title": "no app"}), json.dumps({"t": "loc", "lat": "51", "lon": 0}),
               json.dumps({"t": "loc", "lat": 95, "lon": 0}), json.dumps({"t": "notif_removed"}),
               json.dumps({"t": "notif", "app": "a", "title": {"x": 1}})]
        for b in bad:
            await ws.send_str(b)
            r = await ws.receive_json()
            self.assertEqual(r["t"], "error", b)
        await ws.send_bytes(b"\x00\x01")
        self.assertEqual((await ws.receive_json())["t"], "error")
        await ws.send_json({"t": "hello", "device": "still here"})
        self.assertEqual(await ws.receive_json(), {"t": "ack", "for": "hello"})
        self.assertEqual(self.glasses.notes, [])
        await ws.close()

    async def test_push_disabled_logs_only(self):
        self.make(push=False, min_interval_s=0.0)
        c = await self.client()
        ws = await c.ws_connect("/phone")
        await ws.send_json(notif())
        await ws.receive_json()
        await asyncio.sleep(0.1)
        self.assertEqual(self.glasses.calls, [])
        self.assertEqual(self.link.last_push["ok"], False)
        self.assertTrue(self.link.log_path.exists())
        await ws.close()

    async def test_failed_notify_does_not_kill_sender(self):
        self.make(min_interval_s=0.0)
        self.glasses.ok = False
        c = await self.client()
        ws = await c.ws_connect("/phone")
        await ws.send_json(notif(key="a"))
        await ws.receive_json()
        await self.settle(1)
        self.glasses.ok = True
        await ws.send_json(notif(key="b", text="later"))
        await ws.receive_json()
        await self.settle(2)
        self.assertEqual(self.link.pushed, 1)
        self.assertTrue(self.link.last_push["ok"])
        await ws.close()


class LocationTests(Base):
    async def test_location_ring_and_callbacks(self):
        clk = Clock()
        link = self.make(clock=clk, ring_size=3)
        seen_sync, seen_async = [], []
        link.on_location(seen_sync.append)

        async def acb(fix):
            seen_async.append(fix.lat)
        link.on_location(acb)
        for i in range(5):
            r = await link.handle_text(json.dumps({"t": "loc", "lat": 51.0 + i, "lon": -0.1, "acc": 4.5,
                                                   "speed": 1.0, "bearing": 90, "time": 1}))
            self.assertEqual(r, [{"t": "ack", "for": "loc"}])
        self.assertEqual([f.lat for f in link.ring], [53.0, 54.0, 55.0])
        self.assertEqual(link.location.lat, 55.0)
        self.assertEqual(link.latest().heading_deg, 90.0)
        self.assertEqual(link.location.accuracy_m, 4.5)
        self.assertEqual(len(seen_sync), 5)
        self.assertEqual(seen_async[-1], 55.0)
        clk.t += 12
        st = link.status()
        self.assertEqual(st["last_fix_age_s"], 12.0)
        self.assertEqual(st["fixes"], 3)

    async def test_location_over_websocket(self):
        self.make()
        c = await self.client()
        ws = await c.ws_connect("/phone")
        await ws.send_json({"t": "loc", "lat": 48.85, "lon": 2.35, "acc": 10})
        self.assertEqual(await ws.receive_json(), {"t": "ack", "for": "loc"})
        st = await (await c.get("/phone/status")).json()
        self.assertEqual(st["last_fix"]["lat"], 48.85)
        self.assertIsNotNone(st["last_fix_age_s"])
        await ws.close()


class SettingsGlasses(FakeGlasses):
    """FakeGlasses + settings READ/SET: SETs are decoded and change the reported snapshot."""

    def __init__(self, lock=None) -> None:
        super().__init__(lock)
        from g2.native.framing import MagicAllocator
        self.alloc = MagicAllocator()
        self.state = {"display_height": 4, "display_distance": None, "brightness_level": 20,
                      "brightness_auto": None, "battery": 87, "charging": 1}
        self.reads = 0
        self.sets: list[dict] = []

    async def read_settings(self, timeout: float = 3.0):
        from g2.native import settings
        self.locked_during.append(bool(self.lock and self.lock.locked()))
        self.reads += 1
        return settings.Snapshot(setting_type=1, **self.state)

    async def apply_setting(self, payload: bytes, sid: int = 0x09, timeout: float = 3.0):
        from g2.native import settings
        self.locked_during.append(bool(self.lock and self.lock.locked()))
        st = settings.parse(payload).set
        self.sets.append(st)
        if "display_height" in st:
            self.state["display_height"] = st["display_height"]
        if "display_distance" in st:
            self.state["display_distance"] = st["display_distance"]
        if "brightness" in st:
            b = st["brightness"]
            if b["level"]:
                self.state["brightness_level"] = b["level"]
            else:
                self.state["brightness_auto"] = b["auto"]
        return True


class DisplayTests(Base):
    def make_display(self, **cfg) -> PhoneLink:
        link = self.make(**cfg)
        self.glasses = SettingsGlasses(self.lock)
        self.hub.glasses = self.glasses
        return link

    async def next_of(self, ws, t: str, pred=lambda m: True, timeout=2.0) -> dict:
        """Next message of type ``t`` matching ``pred`` (display broadcasts interleave with acks)."""
        while True:
            m = await asyncio.wait_for(ws.receive_json(), timeout)
            if m.get("t") == t and pred(m):
                return m

    async def next_display(self, ws, pred=lambda m: True, timeout=2.0) -> dict:
        return await self.next_of(ws, "display", pred, timeout)

    def test_set_validation(self):
        from g2.phone_link import BadMessage, validate_set
        self.assertEqual(validate_set({"key": "height", "value": 20}), ("height", 20))
        self.assertEqual(validate_set({"key": "brightness", "value": 1}), ("brightness", 1))
        self.assertEqual(validate_set({"key": "auto", "value": True}), ("auto", 1))
        self.assertEqual(validate_set({"key": "distance", "value": 7.0}), ("distance", 7))
        for bad in ({"key": "height", "value": 21}, {"key": "distance", "value": -1}, {"key": "brightness", "value": 0},
                    {"key": "brightness", "value": 51}, {"key": "auto", "value": 2}, {"key": "height", "value": 2.5},
                    {"key": "height", "value": "5"}, {"key": "volume", "value": 1}, {"value": 1}, {"key": "height"}):
            with self.assertRaises(BadMessage, msg=bad):
                validate_set(bad)

    async def test_display_sent_on_connect_absent_fields_zero(self):
        self.make_display()
        c = await self.client()
        ws = await c.ws_connect("/phone")
        d = await self.next_display(ws)
        self.assertEqual(d, {"t": "display", "height": 4, "distance": 0, "brightness": 20, "auto": 0,
                             "battery": 87, "charging": True})
        self.assertTrue(all(self.glasses.locked_during))
        await ws.close()

    async def test_poll_picks_up_changes_made_elsewhere(self):
        self.make_display(display_poll_s=0.2)
        c = await self.client()
        ws = await c.ws_connect("/phone")
        await self.next_display(ws)
        self.glasses.state["display_height"] = 15          # e.g. g2/position.py moved it
        d = await self.next_display(ws)
        self.assertEqual(d["height"], 15)
        await ws.close()

    async def test_set_applies_rereads_and_broadcasts_to_every_phone(self):
        self.make_display(display_poll_s=60)
        c = await self.client()
        ws1 = await c.ws_connect("/phone")
        ws2 = await c.ws_connect("/phone")
        await self.next_display(ws1)
        await self.next_display(ws2)
        await ws1.send_json({"t": "set", "key": "distance", "value": 12})
        self.assertEqual(await self.next_of(ws1, "ack"), {"t": "ack", "for": "set"})
        d1 = await self.next_display(ws1, lambda m: m["distance"] == 12)
        d2 = await self.next_display(ws2, lambda m: m["distance"] == 12)
        self.assertEqual((d1["distance"], d2["distance"]), (12, 12))
        self.assertEqual(self.glasses.sets, [{"display_distance": 12}])
        await ws1.send_json({"t": "set", "key": "auto", "value": 1})
        self.assertEqual((await self.next_display(ws2, lambda m: m["auto"] == 1))["auto"], 1)
        self.assertTrue(all(self.glasses.locked_during))
        await ws1.close()
        await ws2.close()

    async def test_slider_drag_coalesces_last_value_wins(self):
        self.make_display(display_poll_s=60, set_coalesce_s=0.3)
        c = await self.client()
        ws = await c.ws_connect("/phone")
        await self.next_display(ws)
        for v in range(1, 11):                               # a fast drag: 10 values in ~0.1 s
            await ws.send_json({"t": "set", "key": "height", "value": v})
            self.assertEqual(await self.next_of(ws, "ack"), {"t": "ack", "for": "set"})
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.8)
        heights = [st["display_height"] for st in self.glasses.sets]
        self.assertLessEqual(len(heights), 3)                # not one SET per drag value
        self.assertEqual(heights[-1], 10)                    # the last value wins
        self.assertEqual(self.link.display["height"], 10)
        await ws.close()

    async def test_bad_set_is_an_error_and_nothing_applied(self):
        self.make_display()
        c = await self.client()
        ws = await c.ws_connect("/phone")
        await self.next_display(ws)
        await ws.send_json({"t": "set", "key": "height", "value": 99})
        self.assertIn("height must be 0..20", (await self.next_of(ws, "error"))["msg"])
        await asyncio.sleep(0.4)
        self.assertEqual(self.glasses.sets, [])
        await ws.close()


class WiringTests(unittest.IsolatedAsyncioTestCase):
    def test_hub_config_accepts_phone_key(self):
        from g2.hub import HubConfig
        with self.assertNoLogs("g2.hub", level="WARNING"):
            cfg = HubConfig.from_dict({"phone": {"port": 9999, "allow": ["a"]}})
        p = cfg.phone
        self.assertEqual((p.port, p.allow), (9999, ["a"]))
        self.assertEqual(PhoneConfig.from_dict(None).port, 8768)

    async def test_launcher_starts_and_stops_phone(self):
        from g2.fakes import FakeGateway
        from g2.launcher import Launcher

        class FakePhone:
            started = stopped = 0
            ready = []

            async def start(self):
                self.started += 1

            async def stop(self):
                self.stopped += 1

            def on_glasses_connection(self, ready):
                self.ready.append(ready)

        phone = FakePhone()
        launcher = Launcher(FakeGateway(), apps=[], phone=phone)
        self.assertIs(launcher.ctx.phone, phone)
        await launcher.start()
        await launcher.on_connection(True, "test")
        await launcher.stop()
        self.assertEqual((phone.started, phone.stopped), (1, 1))
        self.assertIn(True, phone.ready)



if __name__ == "__main__":
    unittest.main()
