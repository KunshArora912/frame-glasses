"""Run the G2 stack: the Bluetooth gateway plus the launcher, supervised.

    python g2/run.py [--no-launcher] [launcher args...]

- Starts gateway_server.py (headless) unless something already answers on
  the gateway port, and restarts it if it exits.
- Runs the launcher in this process; its WebSocket reconnects to a restarted
  gateway on its own, and the gateway reconnects to the arms when they come
  back from the case, so the launcher only has to redraw.
- Ctrl-C stops everything. Gateway output goes to g2/logs/gateway.log.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
GATEWAY_DIR = HERE / "vendor" / "men-g2-ble-gateway"
LOG_DIR = HERE / "logs"
log = logging.getLogger("g2.run")


def gateway_alive(base: str) -> bool:
    try:
        with urllib.request.urlopen(base + "/api/status", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


def spawn_gateway() -> subprocess.Popen:
    LOG_DIR.mkdir(exist_ok=True)
    out = open(LOG_DIR / "gateway.log", "ab")
    out.write(f"\n==== gateway start {time.strftime('%Y-%m-%d %H:%M:%S')} ====\n".encode())
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    extra = os.environ.get("G2_GATEWAY_FLAGS", "--park-on-exit --raw-events").split()
    return subprocess.Popen([sys.executable, "gateway_server.py", "--no-gui", *extra],
                            cwd=str(GATEWAY_DIR), stdout=out, stderr=subprocess.STDOUT,
                            creationflags=flags)


async def alive(base: str) -> bool:
    return await asyncio.to_thread(gateway_alive, base)


async def supervise_gateway(base: str, stop: asyncio.Event) -> None:
    proc: subprocess.Popen | None = None
    ours = not await alive(base)
    if not ours:
        log.info("gateway already running at %s; not starting another", base)
    backoff = 3.0
    while not stop.is_set():
        if ours and (proc is None or proc.poll() is not None):
            if proc is not None:
                lived = time.time() - started
                # A gateway that dies young is fighting another one for the
                # port or the arms; back off instead of hammering the radio.
                backoff = 3.0 if lived > 60 else min(backoff * 2, 120.0)
                log.warning("gateway exited with %s after %.0f s; restarting in %.0f s",
                            proc.returncode, lived, backoff)
                await asyncio.sleep(backoff)
                if stop.is_set():
                    break
            proc = spawn_gateway()
            started = time.time()
            log.info("gateway started (pid %s)", proc.pid)
            for _ in range(60):
                if await alive(base) or stop.is_set() or proc.poll() is not None:
                    break
                await asyncio.sleep(1)
            log.info("gateway %s", "answering" if await alive(base) else "not answering yet")
        await asyncio.sleep(2)
    if proc is not None and proc.poll() is None:
        stop_gateway(proc)


def stop_gateway(proc: subprocess.Popen) -> None:
    """Ctrl-Break first so the gateway disconnects the arms cleanly."""
    import signal
    try:
        if os.name == "nt":
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            proc.terminate()
        proc.wait(timeout=8)
        return
    except (subprocess.TimeoutExpired, OSError, ValueError):
        pass
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except (subprocess.TimeoutExpired, OSError):
        proc.kill()


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--gateway", default=os.environ.get("G2_GATEWAY", "http://127.0.0.1:8765"))
    ap.add_argument("--no-launcher", action="store_true")
    ap.add_argument("--no-hub", action="store_true", help="launcher without the native dashboard hub (g2/hub.py)")  # --- hub
    a, rest = ap.parse_known_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    stop = asyncio.Event()
    sup = asyncio.create_task(supervise_gateway(a.gateway, stop))
    try:
        if a.no_launcher:
            while True:
                await asyncio.sleep(3600)
        for _ in range(60):
            if await alive(a.gateway):
                break
            await asyncio.sleep(1)
        sys.path.insert(0, str(ROOT))
        from g2.launcher import main as launcher_main
        await launcher_main(["--gateway", a.gateway, *([] if a.no_hub else ["--hub"]), *rest])  # --- hub: on by default
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        stop.set()
        await sup
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        pass
