"""LC3 decoder for the G2 mic stream (Google liblc3 via its ctypes wrapper).

The glasses send 10 ms frames of 40 bytes at 16 kHz mono; the gateway forwards
five frames per event. Build the library once (see g2/README.md):

    python -m ziglang cc -O3 -std=c11 -shared -Iinclude src/*.c -o liblc3.dll -lm
"""
from __future__ import annotations

import ctypes.util
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
LC3_DIR = HERE / "vendor" / "men-g2-ble-gateway" / "liblc3"
FRAME_BYTES = 40
FRAME_US = 10_000
RATE = 16_000
SAMPLES_PER_FRAME = RATE * FRAME_US // 1_000_000   # 160


def library_path() -> pathlib.Path | None:
    for name in ("liblc3.dll", "liblc3.so", "liblc3.dylib"):
        p = LC3_DIR / name
        if p.exists():
            return p
    return None


class Decoder:
    """Decodes gateway mic events (N x 40-byte LC3 frames) to 16-bit PCM."""

    def __init__(self) -> None:
        dll = library_path()
        if dll is None:
            raise RuntimeError(f"liblc3 not built in {LC3_DIR}; see g2/README.md")
        sys.path.insert(0, str(LC3_DIR / "python"))
        orig = ctypes.util.find_library
        # liblc3's lc3.py asks for libc by name, which fails on Windows.
        ctypes.util.find_library = lambda n: "msvcrt" if n == "c" else orig(n)
        try:
            import lc3  # type: ignore
        finally:
            ctypes.util.find_library = orig
        self._dec = lc3.Decoder(FRAME_US, RATE, num_channels=1, libpath=str(dll))

    def decode(self, payload: bytes, usable: int | None = None) -> bytes:
        if usable is not None:
            payload = payload[:usable]
        out = bytearray()
        for i in range(0, len(payload) - FRAME_BYTES + 1, FRAME_BYTES):
            out += self._dec.decode(payload[i:i + FRAME_BYTES], bit_depth=16)
        return bytes(out)

    def reset(self) -> None:
        """Feed one lost frame so the decoder forgets the previous session."""
        self._dec.decode(None, bit_depth=16)
