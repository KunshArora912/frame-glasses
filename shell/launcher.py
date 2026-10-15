"""The shell: connect once, upload once, then run apps.

Every program in this project used to own the whole device -- connect,
upload its Lua, run, disconnect. Switching tasks meant ~6 s of upload and
there was no way to move between them from the glasses. The shell connects
once, uploads one resident Lua app, and switches what it draws.

Gesture language, chosen around a noisy tap sensor rather than around what
would be elegant:

    menu    1 tap  move down        2 taps  open        3 taps  (nothing)
    app     1 tap  the app's "next" 2 taps  the app's "do it"
                                    3 taps  back to the menu
    any     4+ taps                 quit the shell

One tap is the cheap, frequent action everywhere; the destructive one
needs four.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime

from brilliant_msg import BrilliantMsg, RxAudio, RxIMU, RxTap, TxCode

from park import park
from shell.app import App, Screen
from shell.gestures import Gesture, TapInput
from shell.mudra import MudraInput
from shell.protocol import (AUDIO_CTRL_MSG, IMU_CTRL_MSG, MAX_ROWS,
                            TAP_CTRL_MSG)

LUA_APP = "app/lua/shell_frame_app.lua"
STD_LIBS = ["data", "sprite", "code", "tap", "audio", "imu"]

IDLE_TIMEOUT = 0.25          # how often the loop wakes to tick an app
BATTERY_EVERY = 120.0


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


class Launcher:
    def __init__(self, frame: BrilliantMsg, apps: list[App],
                 dry_run: bool = False) -> None:
        self.frame = frame
        self.apps = apps
        self.dry_run = dry_run
        self.screen = Screen(self._send)
        self.rx_tap = RxTap()
        self.rx_audio = RxAudio(streaming=True)
        self.audio_q = None
        self.taps: TapInput | None = None
        self.mudra = MudraInput()          # second input: the band, via Mudra Link's local stream
        self._mudra_seen = False
        self._audio_task: asyncio.Task | None = None
        self._listener = None
        self.rx_imu = RxIMU(smoothing_samples=1)   # raw; apps smooth
        self.imu_q = None
        self._imu_task: asyncio.Task | None = None
        self._imu_handler = None
        self.selected = 0
        self.current: App | None = None
        self._battery: int | None = None
        self._battery_at = 0.0
        self._send_fails = 0
        self.context: dict = {}

    # ------------------------------------------------------------ the link

    async def _send(self, code: int, payload: bytes,
                    force: bool = False) -> bool:
        if self.dry_run:
            print(f"    -> {code:#04x} {len(payload)}B", flush=True)
            return True
        try:
            await self.frame.send_message(code, payload)
            self._send_fails = 0
            return True
        except Exception as exc:
            # A dropped write must not end the session; only a link that
            # stays down should.
            self._send_fails += 1
            log(f"  send failed ({self._send_fails}): "
                f"{type(exc).__name__}: {exc}")
            return False

    async def battery(self) -> int | None:
        if self.dry_run:
            return None
        if time.monotonic() - self._battery_at < BATTERY_EVERY:
            return self._battery
        self._battery_at = time.monotonic()
        try:
            reply = await self.frame.ble.send_lua(
                "print(frame.battery_level())", await_print=True, timeout=6)
            fresh = int(float(str(reply).strip()))
            if fresh != self._battery:
                log(f"battery {fresh}%")
            self._battery = fresh
        except Exception:
            pass
        return self._battery

    # -------------------------------------------------------------- audio

    async def start_audio(self, listener) -> bool:
        """Begin streaming the microphone into `listener`.

        Owned by the launcher rather than by each app, because the
        microphone is a single shared resource and an app that forgot to
        stop it would silently starve the next one.
        """
        if self.dry_run or self.audio_q is None:
            return False
        await self.stop_audio()
        self._listener = listener
        await self._send(AUDIO_CTRL_MSG, bytes([1, 1, 8]))   # 16 kHz, 8-bit
        self._audio_task = asyncio.create_task(self._drain_audio(),
                                               name="shell-audio")
        return True

    async def stop_audio(self) -> None:
        if self._audio_task is not None:
            self._audio_task.cancel()
            try:
                await self._audio_task
            except asyncio.CancelledError:
                pass
            self._audio_task = None
        if self._listener is not None:
            await self._listener.stop()
            self._listener = None
        if not self.dry_run:
            await self._send(AUDIO_CTRL_MSG, bytes([0, 1, 8]))

    async def _drain_audio(self) -> None:
        while True:
            chunk = await self.audio_q.get()
            if chunk is None:
                return
            if self._listener is not None:
                self._listener.feed(chunk)

    # ---------------------------------------------------------------- imu

    async def start_imu(self, handler) -> bool:
        """Stream accelerometer samples to `handler(sample)` at ~10 Hz."""
        if self.dry_run or self.imu_q is None:
            return False
        await self.stop_imu()
        self._imu_handler = handler
        await self._send(IMU_CTRL_MSG, TxCode(value=1).pack())
        self._imu_task = asyncio.create_task(self._drain_imu(),
                                             name="shell-imu")
        return True

    async def stop_imu(self) -> None:
        if self._imu_task is not None:
            self._imu_task.cancel()
            try:
                await self._imu_task
            except asyncio.CancelledError:
                pass
            self._imu_task = None
        if self._imu_handler is not None:
            self._imu_handler = None
            if not self.dry_run:
                await self._send(IMU_CTRL_MSG, TxCode(value=0).pack())

    async def _drain_imu(self) -> None:
        while True:
            sample = await self.imu_q.get()
            handler = self._imu_handler
            if handler is not None and sample is not None:
                try:
                    handler(sample)
                except Exception as exc:
                    log(f"  imu handler failed: {type(exc).__name__}: {exc}")

    # --------------------------------------------------------------- menu

    def menu_labels(self) -> list[str]:
        return [app.label for app in self.apps][:MAX_ROWS]

    async def draw_menu(self) -> None:
        await self.screen.menu(self.menu_labels(), self.selected)

    # --------------------------------------------------------------- apps

    async def open_app(self, app: App) -> None:
        log(f"opening {app.name}")
        app.screen = self.screen
        app.context = self.context
        app.context["shell"] = self
        self.current = app
        self.screen.invalidate()
        try:
            await app.start()
        except Exception as exc:
            log(f"  {app.name} failed to start: {type(exc).__name__}: {exc}")
            await self.close_app()
            await self.screen.says("app failed", heading=False)
            await asyncio.sleep(2.0)
            await self.draw_menu()

    async def close_app(self) -> None:
        if self.current is None:
            return
        name = self.current.name
        try:
            await self.stop_audio()
            await self.stop_imu()
            await self.current.stop()
        except Exception as exc:
            log(f"  {name} stop failed: {type(exc).__name__}: {exc}")
        self.current = None
        self.screen.invalidate()

    # --------------------------------------------------------------- loop

    async def run(self) -> int:
        if not self.dry_run:
            queue = await self.rx_tap.attach(self.frame)
            await self.frame.send_message(TAP_CTRL_MSG, TxCode(value=1).pack())
            self.audio_q = await self.rx_audio.attach(self.frame)
            self.imu_q = await self.rx_imu.attach(self.frame)
            self.taps = TapInput(queue)
            await asyncio.sleep(0.4)
            dropped = await self.taps.drain()
            if dropped:
                log(f"  ignored {dropped} wake-up tap(s)")
            self.taps.arm()

        await self.mudra.start()
        await self.draw_menu()
        log(f"shell ready -- {len(self.apps)} apps. "
            f"1 tap = move, 2 = open, 3 = back, 4+ = quit")
        log("mudra: tap = open, double tap = back, twist = next, "
            "double twist = quit, d-pad = move (needs Mudra Link > Mudra Studio on)")

        last_tick = 0.0
        while True:
            if self._send_fails >= 10:
                log("the glasses stopped responding -- giving up")
                await self.mudra.stop()
                return 2

            if self.mudra.connected != self._mudra_seen:
                self._mudra_seen = self.mudra.connected
                log("mudra: band " + ("ready" if self._mudra_seen else "away"))

            # The band first (instant), then the glasses' taps (which block
            # for IDLE_TIMEOUT while grouping a burst).
            gesture = await self.mudra.next_gesture(timeout=0)
            if gesture is None:
                if self.taps is not None:
                    gesture = await self.taps.next_gesture(timeout=IDLE_TIMEOUT)
                else:
                    await asyncio.sleep(IDLE_TIMEOUT)

            if gesture is Gesture.QUIT:
                log("quit")
                await self.close_app()
                await self.screen.clear()
                await self.mudra.stop()
                return 0

            if self.current is None:
                if gesture is Gesture.NEXT:
                    self.selected = (self.selected + 1) % len(self.apps)
                    await self.draw_menu()
                elif gesture is Gesture.PREV:
                    self.selected = (self.selected - 1) % len(self.apps)
                    await self.draw_menu()
                elif gesture is Gesture.SELECT:
                    await self.open_app(self.apps[self.selected])
            else:
                if gesture is Gesture.BACK:
                    await self.close_app()
                    await self.draw_menu()
                elif gesture is not None:
                    try:
                        staying = await self.current.gesture(gesture)
                    except Exception as exc:
                        log(f"  {self.current.name} error: "
                            f"{type(exc).__name__}: {exc}")
                        staying = False
                    if not staying:
                        await self.close_app()
                        await self.draw_menu()

                # Tick the open app on its own schedule.
                app = self.current
                if app is not None and app.tick_seconds > 0:
                    now = time.monotonic()
                    if now - last_tick >= app.tick_seconds:
                        last_tick = now
                        try:
                            await app.tick()
                        except Exception as exc:
                            log(f"  {app.name} tick failed: "
                                f"{type(exc).__name__}: {exc}")

            await self.battery()


async def report_battery(frame: BrilliantMsg, when: str) -> float | None:
    try:
        reply = await frame.ble.send_lua("print(frame.battery_level())",
                                         await_print=True, timeout=6)
        level = float(str(reply).strip())
        log(f"battery {level:.0f}% ({when})")
        return level
    except Exception as exc:
        log(f"battery unreadable ({when}): {type(exc).__name__}: {exc}")
        return None


async def connect(frame: BrilliantMsg, attempts: int = 20) -> bool:
    for i in range(1, attempts + 1):
        try:
            await frame.connect()
            return True
        except Exception as exc:
            log(f"  connect {i}/{attempts}: {exc}")
            await asyncio.sleep(3)
    return False


async def boot(apps: list[App], dry_run: bool = False) -> int:
    """Connect, upload the shell once, and run until the wearer quits."""
    from pathlib import Path

    if dry_run:
        return await Launcher(None, apps, dry_run=True).run()

    frame = BrilliantMsg()
    log("connecting...")
    if not await connect(frame):
        log("could not connect -- dock for 2s then undock to wake it")
        return 2

    try:
        log(f"connected (payload {frame.ble.max_lua_payload()})")
        await report_battery(frame, "on connect")
        await frame.ble.send_break_signal()
        await asyncio.sleep(0.5)
        await frame.ble.drain_print_channel()
        await frame.upload_stdlua_libs(lib_names=STD_LIBS)
        lua = Path(__file__).parent.parent / LUA_APP
        await frame.upload_frame_app(local_filename=str(lua))
        await frame.start_frame_app()

        return await Launcher(frame, apps).run()
    except KeyboardInterrupt:
        log("stopped")
        return 0
    except Exception as exc:
        log(f"error: {type(exc).__name__}: {exc}")
        return 1
    finally:
        try:
            await frame.stop_frame_app()
        except Exception:
            pass
        await park(frame)
        await frame.disconnect()
        log("disconnected")
