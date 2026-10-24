"""Claude Code hook -> the G2 terminal hub (g2/terminal.py). Installed by
``python g2/terminal.py install-hooks`` as

    "<python>" "g2/terminal_hook.py" <EventName>

It reads the hook JSON on stdin and POSTs it to http://127.0.0.1:8767/hook/<Event>.
For PermissionRequest it waits (G2_TERMINAL_APPROVAL_TIMEOUT seconds, default
30) for the glasses to approve or deny and prints the documented decision:

    {"hookSpecificOutput": {"hookEventName": "PermissionRequest",
                            "decision": {"behavior": "allow"}}}

No answer, no hub, or any error: it prints nothing and exits 0, so Claude Code
falls back to its normal permission prompt. It never blocks a session for long
and never writes anything to disk. Standard library only (it starts per event).
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any, Callable

HUB = os.environ.get("G2_TERMINAL_URL", f"http://127.0.0.1:{os.environ.get('G2_TERMINAL_PORT', '8767')}")
APPROVAL_TIMEOUT_S = float(os.environ.get("G2_TERMINAL_APPROVAL_TIMEOUT", "30"))
EVENT_TIMEOUT_S = 2.0


def post(url: str, body: dict, timeout: float) -> dict | None:
    req = urllib.request.Request(url, json.dumps(body).encode("utf-8"), {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")
    except (urllib.error.URLError, OSError, ValueError):
        return None


def decision_output(decision: Any) -> dict | None:
    """The stdout object for a PermissionRequest decision, or None for 'no decision'."""
    if not isinstance(decision, dict) or decision.get("behavior") not in ("allow", "deny"):
        return None
    d: dict[str, Any] = {"behavior": decision["behavior"]}
    if d["behavior"] == "deny":
        if decision.get("message"):
            d["message"] = str(decision["message"])
        if decision.get("interrupt"):
            d["interrupt"] = True
    elif isinstance(decision.get("updatedInput"), dict):
        d["updatedInput"] = decision["updatedInput"]
    return {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": d}}


def run_hook(event: str, data: dict, *, hub: str = HUB, approval_timeout: float = APPROVAL_TIMEOUT_S,
             poster: Callable[[str, dict, float], dict | None] = post) -> str:
    """Forward one hook event; returns what to print on stdout ('' = nothing)."""
    event = event or str(data.get("hook_event_name") or "unknown")
    if event == "PermissionRequest":
        url = f"{hub}/hook/{event}?timeout={approval_timeout:g}"
        reply = poster(url, data, approval_timeout + 5)
        out = decision_output((reply or {}).get("decision"))
        return json.dumps(out) if out else ""
    poster(f"{hub}/hook/{event}", data, EVENT_TIMEOUT_S)
    return ""


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    try:
        raw = sys.stdin.buffer.read().decode("utf-8", errors="replace")
        data = json.loads(raw) if raw.strip() else {}
        if not isinstance(data, dict):
            data = {}
        out = run_hook(argv[0] if argv else "", data)
        if out:
            sys.stdout.write(out)
            sys.stdout.flush()
    except Exception:
        pass                        # a hook must never break the session
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
