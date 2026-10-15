"""Local device configuration. The Frame's Bluetooth address is read from the
FRAME_ADDR environment variable, or from an untracked frame_local.py next to
this file (see frame_local.example.py). Never commit frame_local.py.
"""
import os

try:
    from frame_local import FRAME_ADDR as _LOCAL_ADDR
except ImportError:
    _LOCAL_ADDR = None

FRAME_ADDR = os.environ.get("FRAME_ADDR") or _LOCAL_ADDR
if not FRAME_ADDR:
    raise RuntimeError("Set FRAME_ADDR or create frame_local.py (see frame_local.example.py)")
