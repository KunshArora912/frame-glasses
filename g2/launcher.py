"""Home screen and app host for the Even G2, driven from the laptop through the
local gateway. No phone, no Even account, nothing leaves the machine.

    python g2/run.py                      # gateway + this launcher, supervised
    python g2/launcher.py [--brain offline|cli|api] [--model base.en] [--no-stt]

Apps live in g2/apps/*.py (see g2/DEVELOPING.md): drop a file in, the menu
picks it up, and in dev mode (default) a saved file is reloaded live.

Gestures (right temple): swipe up/down moves the selection, single tap opens,
double tap leaves the app. With the gateway's park_on_exit the firmware's own
dashboard then stays on screen until our entry is picked from the glasses menu.

The home screen copies Even's dashboard arrangement: status (time, date,
battery) on the left, one card (our menu) on the right, a dim hint below.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Coroutine

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from g2.gateway_client import G2Gateway, GatewayError  # noqa: E402
from g2.apps.base import App, AppContext, COLS, battery_text, wrap, paginate, brain_name  # noqa: E402,F401
from g2 import apps as app_registry  # noqa: E402

log = logging.getLogger("g2.launcher")

IDLE_BLANK_S = 120
TICK_S = 10
DEV_TICK_S = 2
OPEN_REQUEST = HERE / "logs" / "open_app.txt"     # written by `dev.py push`


class Launcher:
    def __init__(self, gw, speech=None, brain=None, store=None, *,
                 apps: list[App] | None = None,
                 clock=time.time, now=datetime.now,
                 idle_blank_s: float = IDLE_BLANK_S,
                 transcripts_dir: Path | None = None,
                 data_dir: Path | None = None,
                 dev: bool = False,
                 hub=None,                            # hub: g2/hub.py Hub (ctx.native / ctx.hub), optional
                 phone=None,                          # --- phone link: g2/phone_link.py PhoneLink (ctx.phone)
                 start_parked: bool = False) -> None: # start_parked: native dashboard first, board only on launch
        self.gw = gw
        self.hub = hub                                # --- hub (g2/hub.py)
        self.phone = phone                            # --- phone link
        self.start_parked = start_parked              # --- start parked
        self._last_park = 0.0
        self.speech = speech
        self.brain = brain
        self.store = store
        self.clock = clock
        self.now = now
        self.idle_blank_s = idle_blank_s
        self.dev = dev
        self.data_dir = data_dir or HERE
        self.transcripts_dir = transcripts_dir or (HERE / "transcripts")
        self.apps: list[App] = apps if apps is not None else app_registry.discover()
        self.ctx = AppContext(self)
        self.screen = "parked" if start_parked else "home"   # home | blank | parked | <app key>  (--- start parked)
        self.app: App | None = None
        self.sel = 0
        self.last_input = clock()
        self.started_at = clock()
        self.shown = ""                      # last string sent (for redraws)
        self.busy: asyncio.Task | None = None
        self._ticker: asyncio.Task | None = None
        gw.on("touch", self.on_touch)
        gw.on("connection", self.on_connection)
        gw.on("battery", self.on_battery)
        gw.on("reinit", self.on_reinit)

    # ------------------------------------------------------------- menu
    @property
    def menu(self) -> list[tuple[str, str]]:
        return [(a.name, a.blurb) for a in self.visible_apps]

    @property
    def visible_apps(self) -> list[App]:
        return [a for a in self.apps if not a.hidden]

    def find_app(self, key: str) -> App | None:
        key = key.lower().replace(" ", "_")
        for a in self.apps:
            if a.key == key or getattr(a, "module", "") == key:
                return a
        return None

    # ------------------------------------------------------------ output
    async def show(self, text: str) -> None:
        self.shown = text
        if not getattr(self.gw, "ready", True):
            return
        t0 = time.time()
        try:
            draw = getattr(self.gw, "text_inset", None) or self.gw.text
            await draw(text)
        except GatewayError as exc:
            log.warning("display failed: %s", exc)
        if time.time() - t0 > 1.0:
            log.warning("display write took %.1f s", time.time() - t0)

    async def show_layout(self, elements: list[dict]) -> None:
        self.shown = "\n".join(e.get("text", "") for e in elements if e.get("type") == "text")
        if not getattr(self.gw, "ready", True):
            return
        layout = getattr(self.gw, "layout", None)
        if layout is None:
            await self.show(self.shown)
            return
        try:
            await layout(elements)
        except GatewayError as exc:
            log.warning("layout failed: %s", exc)

    # ------------------------------------------------------------- home
    def home_text(self) -> str:
        """Plain-text twin of the home layout (fallback, previews, tests)."""
        t = self.now()
        bat = battery_text(getattr(self.gw, "battery", None), getattr(self.gw, "charging", None))
        lines = [f"{t:%H:%M}  {t:%a %d %b}", bat, ""]
        for i, (name, _) in enumerate(self.menu):
            lines.append(("> " if i == self.sel else "  ") + name)
        return "\n".join(lines)

    def home_elements(self) -> list[dict]:
        t = self.now()
        bat = battery_text(getattr(self.gw, "battery", None), getattr(self.gw, "charging", None))
        menu = self.menu
        self.sel = max(0, min(self.sel, len(menu) - 1)) if menu else 0
        rows = "\n".join(("> " if i == self.sel else "  ") + name for i, (name, _) in enumerate(menu)) or "no apps"
        blurb = menu[self.sel][1] if menu else "add a file to g2/apps/"
        return [
            {"type": "text", "text": f"{t:%H:%M}\n{t:%a %d %b}", "x": 16, "y": 12, "width": 200, "height": 96,
             "padding": 0, "container_name": "status"},
            {"type": "text", "text": bat, "x": 16, "y": 116, "width": 200, "height": 60,
             "padding": 0, "container_name": "battery"},
            {"type": "text", "text": rows, "x": 232, "y": 12, "width": 328, "height": 164,
             "padding": 8, "border_width": 1, "border_radius": 4, "capture_events": True,
             "container_name": "menu"},
            {"type": "text", "text": blurb, "x": 232, "y": 184, "width": 328, "height": 40,
             "padding": 8, "container_name": "blurb"},
            {"type": "text", "text": "swipe  move     tap  open     double tap  exit",
             "x": 16, "y": 252, "width": 544, "height": 32, "padding": 0, "container_name": "hint"},
        ]

    async def show_home(self) -> None:
        await self.show_layout(self.home_elements())
        self.shown = self.home_text()

    async def home(self) -> None:
        await self.leave_current()
        self.screen = "home"
        self.last_input = self.clock()
        await self.show_home()

    async def blank(self) -> None:
        self.screen = "blank"
        await self.show(" ")

    async def park(self) -> bool:                     # --- start parked
        """Leave the board: POST /api/page shutdown (exit_mode 1) so the firmware's own dashboard is what
        the user sees; the gateway answers with reinit page_parked, which keeps us parked.  Deduplicated
        over 5 s because start() and the first connection event both land here."""
        self.screen = "parked"
        page = getattr(self.gw, "page", None)
        if page is None or not getattr(self.gw, "ready", True):
            return False
        if self.clock() - self._last_park < 5.0:
            return True
        self._last_park = self.clock()
        try:
            await page(shutdown=True, exit_mode=1)
        except GatewayError as exc:
            log.warning("park failed: %s", exc)
            return False
        log.info("parked: native dashboard on screen; pick Laptop from the glasses menu for the board")
        return True

    # -------------------------------------------------------- app control
    def run_busy(self, coro: Coroutine[Any, Any, Any], name: str = "app-task") -> asyncio.Task:
        if self.busy and not self.busy.done():
            self.busy.cancel()
        self.busy = asyncio.create_task(coro, name=name)
        return self.busy

    async def cancel_busy(self) -> None:
        if self.busy and not self.busy.done():
            self.busy.cancel()
            try:
                await self.busy
            except (asyncio.CancelledError, Exception):
                pass
        self.busy = None

    async def leave_current(self) -> None:
        await self.cancel_busy()
        app, self.app = self.app, None
        if app is not None:
            try:
                await app.close(self.ctx)
            except Exception:
                log.exception("%s.close failed", app.name)

    async def open(self, key: str) -> bool:
        app = self.find_app(key)
        if app is None:
            log.warning("no app %r", key)
            return False
        await self.leave_current()
        self.app = app
        self.screen = app.key
        self.last_input = self.clock()
        try:
            await app.open(self.ctx)
        except Exception as exc:
            log.exception("%s.open failed", app.name)
            await self.show(self.ctx.screen(app.name, [f"failed to open: {type(exc).__name__}", str(exc)[:COLS * 2]]))
        return True

    # ------------------------------------------------------------ events
    async def on_touch(self, gesture: str, source=None) -> None:
        self.last_input = self.clock()
        log.info("touch %s (source %s) on %s", gesture, source, self.screen)
        if self.screen == "blank":
            await self.home()
            return
        if self.screen == "parked":
            return
        if gesture not in ("single_tap", "double_tap", "swipe_up", "swipe_down", "long_press_release"):
            return                       # foreground_enter/exit etc. are surface notices
        if gesture == "double_tap":
            # Even's rule: double tap closes what is open. Inside an app that
            # means back to our home; on our home it means leave the board
            # entirely and give the screen back to Even's dashboard.
            if self.screen == "home" and self.start_parked:
                await self.leave_current()
                self._last_park = 0.0
                await self.park()
            else:
                await self.home()
            return
        if self.screen == "home":
            await self.touch_home(gesture)
        elif self.app is not None:
            try:
                await self.app.touch(self.ctx, gesture)
            except Exception:
                log.exception("%s.touch failed", self.app.name)

    async def touch_home(self, gesture: str) -> None:
        n = len(self.menu)
        if gesture == "swipe_down":
            self.sel = min(self.sel + 1, max(n - 1, 0))
        elif gesture == "swipe_up":
            self.sel = max(self.sel - 1, 0)
        elif gesture == "single_tap" and n:
            await self.open(self.visible_apps[self.sel].key)
            return
        await self.show_home()

    async def on_connection(self, ready: bool, phase: str) -> None:
        log.info("glasses %s (%s)", "ready" if ready else "away", phase)
        if self.hub is not None:                      # --- hub (g2/hub.py): menu re-push after the gateway's own
            self.hub.on_connection(ready)
        if self.phone is not None:                    # --- phone link: re-enable notifications after a reconnect
            self.phone.on_glasses_connection(ready)
        if ready and self.start_parked and self.screen == "parked":   # --- start parked: never draw home here
            await self.park()
            return
        if ready:
            await self.redraw()
            if self.app is not None:
                await self._link(True)
        else:
            if self.busy and not self.busy.done():
                self.busy.cancel()
            if self.app is not None:
                await self._link(False)
                fresh = self.app.redraw_text()
                if fresh:
                    self.shown = fresh

    async def _link(self, ready: bool) -> None:
        try:
            await self.app.on_link(self.ctx, ready)
        except Exception:
            log.exception("%s.on_link failed", self.app.name)

    async def on_reinit(self, reason=None) -> None:
        # The firmware treats a double tap as "exit this app". With the
        # gateway's park_on_exit the native dashboard stays visible
        # (page_parked) until the user picks us from the glasses menu
        # (dashboard_launch) or a program draws (page_create). Without it the
        # gateway rebuilds the page at once (page_shutdown).
        if reason == "page_parked":
            log.info("user left the app; parked behind the native dashboard")
            await self.leave_current()
            self.screen = "parked"
            self.sel = 0
            return
        if reason == "display_request":
            if self.screen == "parked":
                self.screen = "home"       # a program drew while parked; follow it
            return                         # otherwise it was our own write
        if reason in ("page_shutdown", "dashboard_launch", "page_create"):
            log.info("app (re)entered (%s); home", reason)
            await self.home()
            return
        log.info("gateway reinitialised (%s); redrawing", reason)
        await self.redraw()
        if self.app is not None:
            await self._link(True)

    async def on_battery(self, level, charging) -> None:
        if self.screen == "home":
            await self.show_home()

    async def redraw(self) -> None:
        if self.screen == "parked":
            return
        if self.screen == "home":
            await self.show_home()
        elif self.screen == "blank":
            await self.show(" ")
        else:
            text = (self.app.redraw_text() if self.app is not None else None) or self.shown
            if text:
                await self.show(text)

    # ------------------------------------------------------------- ticks
    async def tick(self) -> None:
        if self.dev:
            await self.dev_tick()
        if self.screen == "parked":
            return
        if self.screen == "home":
            if self.clock() - self.last_input >= self.idle_blank_s:
                await self.blank()
            else:
                await self.show_home()

    async def dev_tick(self) -> None:
        """Hot reload changed app files; open an app requested by dev.py push."""
        changed = app_registry.changed_modules()
        if changed:
            log.info("reloading apps: %s", ", ".join(sorted(changed)))
            current = getattr(self.app, "module", None)
            if current in changed:
                await self.leave_current()
            self.apps = app_registry.discover(reload=True)
            if current in changed:
                await self.open(current)
            elif self.screen == "home":
                await self.show_home()
        try:
            if OPEN_REQUEST.exists():
                key = OPEN_REQUEST.read_text(encoding="utf-8").strip()
                OPEN_REQUEST.unlink()
                if key:
                    log.info("dev.py asked to open %s", key)
                    if key == "home":
                        await self.home()
                    elif not await self.open(key):
                        await self.show(self.ctx.screen("dev", [f"no app named {key}", "", "python g2/dev.py list"]))
        except OSError:
            pass

    async def _ticker_loop(self) -> None:
        while True:
            await asyncio.sleep(DEV_TICK_S if self.dev else TICK_S)
            try:
                await self.tick()
            except Exception:
                log.exception("tick failed")

    async def start(self, first_app: str | None = None) -> None:
        self._ticker = asyncio.create_task(self._ticker_loop(), name="g2-ticker")
        if self.hub is not None:                      # --- hub (g2/hub.py): feeds + keep-alive
            try:
                await self.hub.start()
            except Exception:
                log.exception("hub start failed; launcher continues without it")
            # Native features behind the firmware's own screens: Even AI (voice
            # assistant) and the Teleprompt / Translate / Conversate host.
            try:
                from g2 import even_ai as even_ai_mod
                self.even_ai = even_ai_mod.attach(self)
            except Exception:
                log.exception("Even AI attach failed")
            try:
                from g2.native_apps import NativeApps
                self.native_apps = NativeApps(self.hub.glasses, speech=self.speech, brain=self.brain,
                                              transcripts_dir=self.transcripts_dir)
                self.native_apps.attach()
            except Exception:
                log.exception("native apps attach failed")
        if self.phone is not None:                    # --- phone link: notifications + GPS from the phone
            try:
                await self.phone.start()
            except Exception:
                log.exception("phone link start failed; launcher continues without it")
        if first_app and await self.open(first_app):
            return
        if self.start_parked:                         # --- start parked: home only on dashboard_launch / page_create
            await self.park()
            return
        await self.home()

    async def stop(self) -> None:
        if self._ticker:
            self._ticker.cancel()
        await self.leave_current()
        for name in ("native_apps", "even_ai"):
            obj = getattr(self, name, None)
            if obj is None:
                continue
            try:
                r = obj.detach() if hasattr(obj, "detach") else obj.stop()
                if asyncio.iscoroutine(r):
                    await r
            except Exception:
                log.exception("%s stop failed", name)
        if self.phone is not None:                    # --- phone link
            try:
                await self.phone.stop()
            except Exception:
                log.exception("phone link stop failed")
        if self.hub is not None:                      # --- hub (g2/hub.py)
            try:
                await self.hub.stop()
            except Exception:
                log.exception("hub stop failed")


async def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--gateway", default=os.environ.get("G2_GATEWAY", "http://127.0.0.1:8765"))
    ap.add_argument("--model", default=os.environ.get("G2_STT_MODEL", "base.en"))
    ap.add_argument("--brain", default=os.environ.get("G2_BRAIN"), choices=[None, "offline", "cli", "api"])
    ap.add_argument("--no-stt", action="store_true", help="skip the speech worker (menu only)")
    ap.add_argument("--idle-blank", type=float, default=IDLE_BLANK_S)
    ap.add_argument("--app", help="open this app right away instead of the home screen")
    ap.add_argument("--no-dev", action="store_true", help="disable hot reload and dev.py push")
    ap.add_argument("--hub", action="store_true", help="run the native dashboard hub (g2/hub.py) in this process")  # --- hub
    ap.add_argument("--no-phone", action="store_true", help="do not start the phone link (g2/phone_link.py)")  # --- phone link
    ap.add_argument("--start-parked", dest="start_parked", action="store_true", default=True,                        # --- start parked
                    help="(default) show the firmware's dashboard on connect; the board only when launched from the glasses menu")
    ap.add_argument("--board", dest="start_parked", action="store_false", help="draw our board right away (old behaviour)")
    ap.add_argument("-v", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.v else logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

    decoder = None
    try:
        from g2.lc3dec import Decoder
        decoder = Decoder()
    except Exception as exc:
        log.warning("no LC3 decoder (%s); mic features disabled", exc)

    gw = G2Gateway(a.gateway, decoder=decoder)
    speech = None
    if decoder is not None and not a.no_stt:
        from g2.stt import Speech
        speech = Speech(gw, model=a.model)
    from ai.brain import make_brain
    from notes.store import Store

    async def battery_level() -> float:          # OfflineBrain awaits its getter
        if gw.battery is None:
            raise RuntimeError("battery unknown")
        return float(gw.battery)
    brain = make_brain(battery_getter=battery_level, prefer=a.brain)
    store = Store()
    hub = None
    if a.hub:                                         # --- hub (g2/hub.py): one native Glasses per process
        from g2.hub import Hub, HubConfig
        from g2.native.transport import GatewayRawTransport, Glasses
        hub = Hub(Glasses(GatewayRawTransport(a.gateway), log=logging.getLogger("g2.native").info),
                  HubConfig.load(), store=store)
    phone = None
    if hub is not None and not a.no_phone:           # --- phone link: starts with the hub, exposed as hub.phone / ctx.phone
        from g2.phone_link import PhoneConfig, attach as phone_attach
        pcfg = PhoneConfig.from_dict(hub.config.phone)   # already a PhoneConfig; dict-tolerant
        phone = phone_attach(hub, pcfg) if pcfg.enabled else None
    launcher = Launcher(gw, speech, brain, store, idle_blank_s=a.idle_blank, dev=not a.no_dev, hub=hub, phone=phone,
                        start_parked=a.start_parked and not a.app)       # --- start parked
    log.info("apps: %s", ", ".join(x.name for x in launcher.apps) or "none")

    await gw.start()
    if speech is not None:
        log.info("loading speech model %s ...", a.model)
        ok = await speech.start()
        log.info("speech %s", "ready" if ok else "FAILED (voice apps disabled)")
        if not ok:
            speech = None
            launcher.speech = None
    for _ in range(50):
        if gw.gateway_up:
            break
        await asyncio.sleep(0.1)
    await launcher.start(a.app)
    log.info("launcher running; Ctrl-C to stop")
    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await launcher.stop()
        if speech is not None:
            await speech.stop()
        await gw.stop()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        pass
