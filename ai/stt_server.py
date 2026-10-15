"""Speech-to-text worker process.

Runs as a separate process, deliberately. faster-whisper's native backend
(ctranslate2) segfaults when loaded inside the Bluetooth process, because
bleak's WinRT backend has already initialised a COM apartment on that
thread. Isolating Whisper in its own process sidesteps that entirely, and
keeping the process warm means we pay the ~20 s model load once rather than
per question.

Protocol, one JSON object per line on stdin/stdout:

    in : {"pcm": "<path>", "rate": 16000, "depth": 8}
    out: {"ok": true, "text": "...", "dbfs": -28.4}
         {"ok": false, "error": "..."}

Prints {"ready": true} once the model is loaded.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> int:
    model_name = sys.argv[1] if len(sys.argv) > 1 else "base.en"

    try:
        from ai.transcribe import load_model, transcribe
    except ImportError:                                   # run as a script
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from ai.transcribe import load_model, transcribe

    try:
        load_model(model_name)
    except Exception as exc:
        print(json.dumps({"ready": False, "error": str(exc)}), flush=True)
        return 1

    print(json.dumps({"ready": True}), flush=True)

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
            if req.get("quit"):
                return 0
            pcm = Path(req["pcm"]).read_bytes()
            text, dbfs = transcribe(pcm, int(req["rate"]), int(req["depth"]))
            print(json.dumps({"ok": True, "text": text, "dbfs": dbfs}),
                  flush=True)
        except Exception as exc:
            print(json.dumps({"ok": False,
                              "error": f"{type(exc).__name__}: {exc}"}),
                  flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
