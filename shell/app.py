"""What an app is.

An app gets a Screen -- the only thing it may draw with -- and handles
gestures. It never touches Bluetooth, never uploads Lua, and never knows
which input device produced a gesture. That is what makes an app ~60 lines
and testable with a fake Screen and no hardware.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from shell.gestures import Gesture
from shell.protocol import (CLEAR_MSG, MAX_ROWS, MENU_MSG, PAGE_MSG,
                            SPRITE_MSG, pack_menu, pack_page)


class Screen:
    """Everything an app may put on the glasses."""

    def __init__(self, send) -> None:
        self._send = send
        self._last: bytes | None = None

    async def menu(self, labels: list[str], selected: int = 0) -> None:
        await self._put(MENU_MSG, pack_menu(labels, selected))

    async def page(self, lines: list[str], *, heading: bool = False,
                   emphasis: bool = False) -> None:
        await self._put(PAGE_MSG, pack_page(lines, heading=heading,
                                            emphasis=emphasis))

    async def says(self, *lines: str, heading: bool = False) -> None:
        """A short page, vertically placed where the eye already is."""
        padded = [""] + list(lines)
        await self.page(padded[:MAX_ROWS], heading=heading)

    async def sprite(self, packed: bytes) -> None:
        await self._send(SPRITE_MSG, packed, force=True)

    async def clear(self) -> None:
        await self._send(CLEAR_MSG, bytes([1]), force=True)
        self._last = None

    async def _put(self, code: int, payload: bytes) -> None:
        # Redrawing an identical frame costs a full repaint on the device
        # for no visible change, so skip it.
        if payload == self._last:
            return
        self._last = payload
        await self._send(code, payload)

    def invalidate(self) -> None:
        self._last = None


@dataclass
class App:
    """Base class. Subclasses override what they need."""

    name: str = "app"
    label: str = "App"            # how it reads in the launcher
    tick_seconds: float = 0.0     # 0 = never ticked

    screen: Screen = field(default=None, repr=False)
    context: dict = field(default_factory=dict, repr=False)

    async def start(self) -> None:
        """Draw the first frame. Called once when the app is opened."""

    async def tick(self) -> None:
        """Called every `tick_seconds` while the app is open."""

    async def gesture(self, gesture: Gesture) -> bool:
        """Handle a gesture. Return False to leave the app.

        BACK is handled by the launcher, so an app only ever sees NEXT and
        SELECT unless it opts into more.
        """
        return True

    async def stop(self) -> None:
        """Release anything held. Always called, even on an error."""
