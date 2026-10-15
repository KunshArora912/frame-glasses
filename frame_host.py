"""Laptop companion for the launcher on the glasses.

    python frame_host.py

Leave this running. It waits for the glasses to appear, connects, and listens.
When you pick an app on the glasses (menu in device/main.lua, two taps), the
glasses print "launch:<id>"; this script lets go of the Bluetooth link and
runs the matching program from the table below in this terminal. When that
program exits it resets the glasses back to the wake screen, and this script
reconnects and waits for the next pick.

While connected the glasses stay awake, so main.lua enforces its own
on-battery sleep after five idle minutes; a sleep drops the link and this
script simply goes back to watching.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from brilliant_ble import BrilliantBle

ROOT = Path(__file__).parent
PY = sys.executable

# id printed by main.lua -> command. Edit freely; navigate needs a destination.
APPS: dict[str, list[str]] = {
    "assistant": [PY, "assistant.py"],
    "shell":     [PY, "frame.py"],
    "todo":      [PY, "todo.py"],
    "navigate":  [PY, "navigate.py", "--to", "coffee", "--from", "here"],
    "showcase":  [PY, "showcase.py"],
}

SCAN_PAUSE = 4.0


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


class Companion:
    def __init__(self) -> None:
        self.launch: asyncio.Queue[str] = asyncio.Queue()
        self.dropped = asyncio.Event()

    def on_print(self, text: str) -> None:
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("launch:"):
                self.launch.put_nowait(line[len("launch:"):])
            elif line:
                log(f"glasses: {line}")

    def on_disconnect(self) -> None:
        self.dropped.set()

    async def session(self) -> str | None:
        """Hold one connection until the glasses ask for an app or vanish."""
        frame = BrilliantBle()
        self.dropped.clear()
        try:
            await frame.connect(timeout=6,
                                print_response_handler=self.on_print,
                                disconnect_handler=self.on_disconnect)
        except Exception:
            return None
        log("connected; waiting for a pick on the glasses (two taps on a menu item)")
        try:
            # make sure the wake screen is what is running
            await frame.send_reset_signal()
            while True:
                waiter = asyncio.create_task(self.launch.get())
                drop = asyncio.create_task(self.dropped.wait())
                done, _ = await asyncio.wait({waiter, drop}, return_when=asyncio.FIRST_COMPLETED)
                for t in (waiter, drop):
                    if t not in done:
                        t.cancel()
                if waiter in done:
                    return waiter.result()
                log("link dropped (asleep, or another program took it)")
                return None
        finally:
            try:
                await frame.disconnect()
            except Exception:
                pass

    async def run(self) -> None:
        log("frame_host: watching for the glasses (Ctrl-C to stop)")
        while True:
            app_id = await self.session()
            if app_id is None:
                await asyncio.sleep(SCAN_PAUSE)
                continue
            cmd = APPS.get(app_id)
            if cmd is None:
                log(f"glasses asked for {app_id!r} but it is not in APPS")
                continue
            log(f"launching {app_id}: {' '.join(Path(c).name if c == PY else c for c in cmd)}")
            await asyncio.sleep(1.0)   # let the link settle before the app connects
            rc = await asyncio.to_thread(subprocess.call, cmd, cwd=str(ROOT))
            log(f"{app_id} exited with {rc}; back to watching")
            await asyncio.sleep(2.0)


if __name__ == "__main__":
    try:
        asyncio.run(Companion().run())
    except KeyboardInterrupt:
        pass
