"""The Frame shell -- every app on the glasses, from one launcher.

    python frame.py                 # connect and run
    python frame.py --dry-run       # no glasses: prints what it would draw
    python frame.py --no-voice      # don't speak through the earphones

Gestures (taps on the frames):

    1 tap    move down the menu / the app's "next"
    2 taps   open / the app's "do it"
    3 taps   back to the menu
    4+ taps  quit

With a Mudra band paired and its Companion app running, the band's gestures
map onto the same four intents and taps keep working alongside.

Audio OUT goes to whatever earphones Windows is already driving. Frame has
no speaker, but it does not need one: the laptop holds the link and runs
every app, so it can talk to you while the glasses show you things.
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from ai.listener import Listener
from ai.stt_client import SttClient
from shell.apps.agenda import Agenda
from shell.apps.captions import Captions
from shell.apps.teleprompter import Teleprompter
from shell.apps.walk import Walk
from shell.launcher import boot, log

HERE = Path(__file__).parent


class Speech:
    """One speech-to-text worker, started the first time something needs it.

    Loading the model costs time and a few hundred megabytes, and most
    sessions never open a listening app at all -- so it is not paid for
    until an app actually asks. The worker is shared: two apps that both
    listen would otherwise load two copies.
    """

    def __init__(self) -> None:
        self.client: SttClient | None = None
        self._lock = asyncio.Lock()

    async def listener(self) -> Listener | None:
        async with self._lock:
            if self.client is None:
                log("starting the speech worker (loads the model once)...")
                client = SttClient()
                if not await client.start():
                    log("  speech worker failed to start")
                    return None
                self.client = client
                log("  speech worker ready")
        return Listener(self.client)

    async def close(self) -> None:
        if self.client is not None:
            await self.client.stop()
            self.client = None


def build_apps(speech: Speech) -> list:
    """The launcher menu, in the order it is read.

    Seven rows fit on the panel; anything past that is unreachable, so the
    list is kept to what earns its place.
    """
    return [
        Teleprompter(HERE / "script.txt", listener_factory=speech.listener),
        Captions(listener_factory=speech.listener),
        Agenda(HERE / "agenda.json"),
        Walk(),
    ]


async def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true",
                    help="no glasses; print what would be drawn")
    ap.add_argument("--no-voice", action="store_true",
                    help="stay silent even if earphones are connected")
    args = ap.parse_args()

    speech = Speech()
    try:
        return await boot(build_apps(speech), dry_run=args.dry_run)
    finally:
        await speech.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
