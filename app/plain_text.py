"""Host-side app: push text to the glasses via the message layer.

This is the canonical two-program pattern -- this file plus
lua/plain_text_frame_app.lua, coupled by a one-byte message code that must
match on both sides.

    .venv/Scripts/python.exe app/plain_text.py
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from brilliant_msg import BrilliantMsg, TxPlainText
import sys
from pathlib import Path as _Path
sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))
from park import park

# must match TEXT_MSG in lua/plain_text_frame_app.lua
TEXT_MSG = 0x0A

LUA_APP = Path(__file__).parent / "lua" / "plain_text_frame_app.lua"

LINES = [
    ("HELLO FROM PYTHON", 60, 100),
    ("message layer works", 60, 160),
    ("640 x 400, 16 colors", 60, 220),
]


async def main() -> None:
    frame = BrilliantMsg()
    try:
        await frame.connect()
    except Exception as exc:
        print(f"Could not connect: {type(exc).__name__}: {exc}")
        print("Run 'python diagnose.py' to find out why.")
        return

    try:
        print(f"connected (type={frame.ble.type})")
        await frame.print_short_text("Loading...")

        # Uploads data.min.lua + plain_text.min.lua to the device.
        await frame.upload_stdlua_libs(lib_names=["data", "plain_text"])

        # Uploads as frame_app.lua, which start_frame_app() then requires.
        await frame.upload_frame_app(local_filename=str(LUA_APP))

        frame.attach_print_response_handler()
        await frame.start_frame_app()

        for text, x, y in LINES:
            print(f"-> {text!r} at ({x},{y})")
            await frame.send_message(TEXT_MSG, TxPlainText(text=text, x=x, y=y).pack())
            await asyncio.sleep(2.5)

        await asyncio.sleep(2.0)
    except KeyboardInterrupt:
        print("\ninterrupted")
    except Exception as exc:
        print(f"error: {type(exc).__name__}: {exc}")
    finally:
        # Teardown belongs here: on any error path the device-side app_loop
        # would otherwise keep spinning after we disconnect.
        try:
            frame.detach_print_response_handler()
            await frame.stop_frame_app()
        except Exception:
            pass
        await park(frame)
        await frame.disconnect()
        print("disconnected")


if __name__ == "__main__":
    asyncio.run(main())
