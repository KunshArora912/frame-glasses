"""Settings (hidden fallback; use `python g2/settings_cli.py`): the glasses' own settings (brightness, head-up, wear, silent, units,
timeout, display height/distance) read and changed over the native protocol
(``ctx.native``, a g2.native.transport.Glasses). Swipe moves, tap edits, swipes
change the value, tap applies and re-reads the snapshot to confirm.

Builders used (g2/native/settings.py; confidence per g2/native/README.md):
  brightness, auto_brightness, head_up, wear_detection, silent_mode, units,
  display_timeout (SID 0x20)                         LIVE
  display_height, display_distance                   DECODED, never sent by a hub
Without ``ctx.native`` (no hub, preview) the app only shows a message.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from g2.apps.base import App, AppContext
from g2.native import settings as ns

TIMEOUTS = list(ns.TIMEOUT_CHOICES)          # 3, 5, 10, 15, 30, 65365 (never)
UNTESTED = "DECODED, untested"


@dataclass
class Item:
    key: str
    label: str
    kind: str                     # "int" | "flag" | "choice"
    lo: int = 0
    hi: int = 1
    step: int = 1
    choices: tuple = ()           # for "choice": (value, label) pairs
    note: str = ""
    snap: str | None = None       # Snapshot attribute that reports it back


ITEMS: list[Item] = [
    Item("brightness", "brightness", "int", ns.BRIGHTNESS_MIN, ns.BRIGHTNESS_MAX, 5, snap="brightness_level"),
    Item("auto_brightness", "auto brightness", "flag", snap="brightness_auto"),
    Item("display_height", "display height", "int", 0, ns.DISPLAY_LEVEL_MAX, 1, note=UNTESTED, snap="display_height"),
    Item("display_distance", "display distance", "int", 0, ns.DISPLAY_LEVEL_MAX, 1, note=UNTESTED, snap="display_distance"),
    Item("head_up", "head-up display", "flag", snap="head_up"),
    Item("head_up_angle", "head-up angle", "int", ns.HEAD_UP_ANGLE_MIN, ns.HEAD_UP_ANGLE_MAX, 5, snap="head_up_angle"),
    Item("wear_detection", "wear detection", "flag", snap="wear_detection"),
    Item("silent_mode", "silent mode", "flag", snap="silent_mode"),
    Item("time_format", "time format", "choice", choices=((0, "24h"), (1, "12h"))),
    Item("temperature_unit", "temperature", "choice", choices=((1, "C"), (2, "F"))),
    Item("display_timeout", "display timeout", "choice",
         choices=tuple((t, "never" if t == ns.TIMEOUT_NEVER else f"{t}s") for t in TIMEOUTS)),
]
BY_KEY = {it.key: it for it in ITEMS}

HINT_BROWSE = "swipe move  tap edit  2 taps exit"
HINT_EDIT = "swipe change  tap apply  hold cancel"


class Settings(App):
    name = "Settings"
    blurb = "brightness, head-up, units, timeout"
    order = 80
    hidden = True                 # fallback: the laptop CLI g2/settings_cli.py is the real tool

    def __init__(self) -> None:
        self.values: dict[str, Any] = {}          # current (read or last applied)
        self.cursor = 0
        self.editing = False
        self.edit_value: Any = None
        self.status = ""
        self.snapshot: ns.Snapshot | None = None
        self.timeout_s = 3.0                      # per native request; tests shrink it
        self._ctx: AppContext | None = None
        self._last = ""

    # ---- helpers ----------------------------------------------------------
    @staticmethod
    def native_of(ctx: AppContext):
        return getattr(ctx, "native", None)

    def fmt(self, it: Item, value: Any) -> str:
        if value is None:
            return "?"
        if it.kind == "flag":
            return "on" if value else "off"
        if it.kind == "choice":
            for v, label in it.choices:
                if v == value:
                    return label
            return str(value)
        return str(value)

    def row(self, i: int, it: Item, ctx: AppContext) -> str:
        cur = i == self.cursor
        value = self.edit_value if (cur and self.editing) else self.values.get(it.key)
        mark = ("* " if self.editing else "> ") if cur else "  "
        line = f"{mark}{it.label:<17}{self.fmt(it, value):>6}"
        if it.note:
            line += f"  {it.note}"
        return line[:ctx.COLS]

    def text(self, ctx: AppContext) -> str:
        rows = ctx.BODY_ROWS - 1
        self.cursor = max(0, min(self.cursor, len(ITEMS) - 1))
        top = max(0, self.cursor - rows + 1)
        body = [self.row(i, it, ctx) for i, it in enumerate(ITEMS) if top <= i < top + rows]
        body.append(self.status[:ctx.COLS])
        self._last = ctx.screen(self.name, body, HINT_EDIT if self.editing else HINT_BROWSE)
        return self._last

    def redraw_text(self):
        return self._last or None

    def _from_snapshot(self, snap: ns.Snapshot | None) -> None:
        if snap is None:
            return
        self.snapshot = snap
        for it in ITEMS:
            if it.snap is not None:
                v = getattr(snap, it.snap, None)
                if v is not None:
                    self.values[it.key] = bool(v) if it.kind == "flag" else v

    # ---- lifecycle --------------------------------------------------------
    async def open(self, ctx: AppContext) -> None:
        self._ctx = ctx
        self.cursor, self.editing, self.status = 0, False, ""
        self.values.setdefault("time_format", 1)          # the hub's connect defaults
        self.values.setdefault("temperature_unit", 1)
        if self.native_of(ctx) is None:
            self._last = ctx.screen(self.name, [
                "The native link is not up.",
                "",
                "Device settings need the hub that owns the",
                "glasses' raw channel: start g2/run.py and",
                "open Settings from the glasses menu.",
                "",
                "Nothing was changed.",
            ])
            await ctx.show(self._last)
            return
        self.status = "reading ..."
        await ctx.show(self.text(ctx))
        ctx.run(self._read(ctx, "read"), "g2-settings-read")

    async def _read(self, ctx: AppContext, label: str) -> None:
        g = self.native_of(ctx)
        try:
            snap = await g.read_settings(timeout=self.timeout_s)
            self._from_snapshot(snap)
            if snap is None:
                self.status = "no reply from the glasses"
            else:
                self.status = "" if label == "read" else label
            await self._read_timeout(ctx)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            ctx.log.exception("settings read failed")
            self.status = f"read failed: {type(exc).__name__}"
        await ctx.show(self.text(ctx))

    async def _read_timeout(self, ctx: AppContext) -> None:
        """SID 0x20 INQUIRE_AUTO_CLOSE (LIVE); the reply lands in Glasses.status."""
        g = self.native_of(ctx)
        try:
            r = await g.send_pb(ns.SID_MODULE, ns.read_display_timeout(g.alloc.next_magic()), expect="reply", timeout=min(2.0, self.timeout_s))
            t = ns.parse_module(r.payload).timeout if r is not None else None
            if t is None:
                t = getattr(g, "status", {}).get("display_timeout")
            if t is not None:
                self.values["display_timeout"] = ns.TIMEOUT_NEVER if t > 20 else t
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            ctx.log.warning("timeout read failed: %s", exc)

    async def touch(self, ctx: AppContext, gesture: str) -> None:
        if self.native_of(ctx) is None:
            return
        if ctx.busy and self.status.endswith("..."):
            return
        it = ITEMS[self.cursor]
        if not self.editing:
            if gesture == "swipe_down":
                self.cursor = min(self.cursor + 1, len(ITEMS) - 1)
            elif gesture == "swipe_up":
                self.cursor = max(self.cursor - 1, 0)
            elif gesture == "single_tap":
                self.editing = True
                self.edit_value = self.values.get(it.key)
                if self.edit_value is None:
                    self.edit_value = it.lo if it.kind == "int" else (False if it.kind == "flag" else it.choices[0][0])
                self.status = "editing " + it.label
            else:
                return
            await ctx.show(self.text(ctx))
            return
        # editing
        if gesture in ("swipe_down", "swipe_up"):
            self.edit_value = self.bump(it, self.edit_value, +1 if gesture == "swipe_down" else -1)
        elif gesture == "long_press_release":
            self.editing, self.status = False, "cancelled"
        elif gesture == "single_tap":
            self.editing = False
            self.status = "applying ..."
            await ctx.show(self.text(ctx))
            ctx.run(self._apply(ctx, it, self.edit_value), "g2-settings-apply")
            return
        else:
            return
        await ctx.show(self.text(ctx))

    @staticmethod
    def bump(it: Item, value: Any, direction: int) -> Any:
        if it.kind == "flag":
            return not value
        if it.kind == "choice":
            vals = [v for v, _ in it.choices]
            i = vals.index(value) if value in vals else 0
            return vals[(i + direction) % len(vals)]
        v = (value if isinstance(value, int) else it.lo) + direction * it.step
        return max(it.lo, min(it.hi, v))

    # ---- apply ------------------------------------------------------------
    def build(self, it: Item, value: Any, magic: int) -> tuple[bytes, int]:
        """(payload, sid) for one setting. Keep this the single place that names builders."""
        k = it.key
        if k == "brightness":
            return ns.brightness(magic, int(value)), ns.SID_SETTINGS
        if k == "auto_brightness":
            return ns.auto_brightness(magic, bool(value)), ns.SID_SETTINGS
        if k == "display_height":                                     # DECODED
            return ns.display_height(magic, int(value)), ns.SID_SETTINGS
        if k == "display_distance":                                   # DECODED
            return ns.display_distance(magic, int(value)), ns.SID_SETTINGS
        if k == "head_up":
            return ns.head_up(magic, bool(value), self.values.get("head_up_angle")), ns.SID_SETTINGS
        if k == "head_up_angle":
            return ns.head_up(magic, bool(self.values.get("head_up", True)), int(value)), ns.SID_SETTINGS
        if k == "wear_detection":
            return ns.wear_detection(magic, bool(value)), ns.SID_SETTINGS
        if k == "silent_mode":
            return ns.silent_mode(magic, bool(value)), ns.SID_SETTINGS
        if k in ("time_format", "temperature_unit"):
            tf = int(value) if k == "time_format" else int(self.values.get("time_format", 1))
            tu = int(value) if k == "temperature_unit" else int(self.values.get("temperature_unit", 1))
            return ns.units(magic, time_format=tf, temperature_unit=tu), ns.SID_SETTINGS
        if k == "display_timeout":
            return ns.display_timeout(magic, int(value)), ns.SID_MODULE
        raise KeyError(k)

    async def _apply(self, ctx: AppContext, it: Item, value: Any) -> None:
        g = self.native_of(ctx)
        try:
            payload, sid = self.build(it, value, g.alloc.next_magic())
            reply = await g.apply_setting(payload, sid=sid, timeout=self.timeout_s)
            if reply is None:
                self.status = f"{it.label}: no ack"
            else:
                ok = ns.parse_ack(reply.payload) if sid == ns.SID_SETTINGS else None
                acked = ok is None or ok.get("flag", 0) == 0
                self.status = f"{it.label}: {'acked' if acked else 'rejected'}"
                if acked:
                    self.values[it.key] = value
            if it.snap is not None:
                before = self.values.get(it.key)
                snap = await g.read_settings(timeout=self.timeout_s)
                self._from_snapshot(snap)
                after = self.values.get(it.key)
                if snap is None:
                    self.status += ", no readback"
                elif after == value:
                    self.status += ", confirmed"
                else:
                    self.status += f", readback {self.fmt(it, after)}"
                    if before != after:
                        self.values[it.key] = after
            elif it.key == "display_timeout":
                await self._read_timeout(ctx)
                if self.values.get("display_timeout") == value:
                    self.status += ", confirmed"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            ctx.log.exception("settings apply failed")
            self.status = f"{it.label}: failed {type(exc).__name__}"
        await ctx.show(self.text(ctx))

    async def close(self, ctx: AppContext) -> None:
        self.editing = False
