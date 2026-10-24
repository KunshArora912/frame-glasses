"""Terminal Mode without Even's app: Claude Code (and any console program) on
the glasses, everything on this laptop.

    python g2/terminal.py serve                     # hub on http://127.0.0.1:8767
    python g2/terminal.py install-hooks             # let Claude Code report to the hub
    python g2/terminal.py uninstall-hooks
    python g2/terminal.py run -- claude             # a session the glasses can type into
    python g2/terminal.py run --cols 100 --rows 30 -- powershell
    python g2/terminal.py sessions                  # what the hub knows right now

Three sources feed one session registry:

* transcript watcher: tails the newest N ``*.jsonl`` files under
  ``~/.claude/projects`` and derives project, status (busy / idle / waiting),
  the last assistant text and the last tool call per session;
* hook endpoints ``POST /hook/<event>``: ``g2/terminal_hook.py`` forwards
  Claude Code's hook JSON here; a ``PermissionRequest`` blocks until the
  glasses answer (or the timeout passes, in which case Claude Code shows its
  normal prompt);
* managed sessions: ``run`` spawns a command in a ConPTY (pywinpty), renders
  it with pyte and lets the glasses type into it.

Clients (the Terminal app in g2/apps/terminal.py, the page at ``/``) follow
``/ws``: the full state is broadcast at most 4 times a second, and they send
``{"type": "input"|"decide", ...}`` back. The server binds 127.0.0.1 only.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import shutil
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

log = logging.getLogger("g2.terminal")

HOST = "127.0.0.1"
PORT = int(os.environ.get("G2_TERMINAL_PORT", "8767"))
PROJECTS_DIR = Path(os.environ.get("CLAUDE_PROJECTS_DIR") or (Path.home() / ".claude" / "projects"))
SETTINGS_PATH = Path(os.environ.get("CLAUDE_SETTINGS") or (Path.home() / ".claude" / "settings.json"))
HOOK_SCRIPT = HERE / "terminal_hook.py"

COLS, ROWS = 48, 9                 # the glasses grid minus the header row
TEXT_CHARS = 400                   # of the last assistant message
TAIL_BYTES = 512 * 1024            # how much of a transcript we read
SESSION_LIMIT = 8
STALE_S = 600                      # a "busy" transcript older than this is really idle
PROMPT_QUIET_S = 1.5
BROADCAST_HZ = 4
APPROVAL_TIMEOUT_S = 30            # how long a PermissionRequest waits for the glasses (hook timeout is 60)

# ---- hook contract (verified against code.claude.com/docs/en/hooks, 2026-10-23)
# Every hook gets session_id, transcript_path, cwd, permission_mode,
# hook_event_name on stdin. Tool events add tool_name and tool_input
# (PermissionRequest has no tool_use_id). A PermissionRequest hook answers with
#   {"hookSpecificOutput": {"hookEventName": "PermissionRequest",
#                           "decision": {"behavior": "allow"|"deny", ["message": ...], ["interrupt": bool]}}}
# and exit 0 with empty stdout means "no decision, prompt as usual".
# Non-decision hooks are installed with "async": true (documented for command
# hooks) so they never block a tool call.
HOOK_EVENTS = ("SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse",
               "PermissionRequest", "Notification", "Stop", "SessionEnd")
HOOK_TIMEOUTS = {"PermissionRequest": 60}
HOOK_DEFAULT_TIMEOUT = 5
HOOK_MARK = "terminal_hook.py"      # how we recognise our own entries in settings.json

PROMPT_RE = re.compile(r"(?:[>$#%:?]|\]|\)|❯|›|»)\s*$|\(y/n\)|\[y/n\]|\[Y/n\]|\[y/N\]|yes/no|press enter|continue\?", re.I)
YES_RE = re.compile(r"^\W*(yes|yeah|yep|yup|approve|approved|allow|allowed|ok|okay|go ahead|do it|sure|confirm)\b", re.I)
NO_RE = re.compile(r"^\W*(no|nope|deny|denied|don't|do not|stop|cancel|reject)\b", re.I)


# =============================================================== helpers
def _iso_to_epoch(s: Any) -> float | None:
    if not isinstance(s, str) or not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def age_text(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def summarize_tool(name: str, tool_input: Any, width: int = 60) -> str:
    """One short line for a tool call: 'Bash git status', 'Edit base.py'."""
    name = str(name or "tool")
    detail = ""
    if isinstance(tool_input, dict):
        for key in ("command", "file_path", "notebook_path", "pattern", "description", "prompt", "url", "query", "skill"):
            v = tool_input.get(key)
            if isinstance(v, str) and v.strip():
                detail = v.strip()
                if key in ("file_path", "notebook_path"):
                    detail = Path(detail).name
                break
        else:
            for v in tool_input.values():
                if isinstance(v, str) and v.strip():
                    detail = v.strip()
                    break
    elif isinstance(tool_input, str):
        detail = tool_input
    detail = " ".join(detail.split())
    s = f"{name} {detail}".strip()
    return s if len(s) <= width else s[:width - 1] + "…"


def _block_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def project_from_cwd(cwd: str | None, slug: str | None = None) -> str:
    if cwd:
        name = Path(cwd).name or cwd
        if name:
            return name
    if slug:
        return slug.strip("-").rsplit("-", 1)[-1] or slug
    return "?"


# ============================================================ transcripts
@dataclass
class TranscriptInfo:
    session_id: str
    project: str = "?"
    cwd: str = ""
    title: str = ""
    status: str = "idle"              # busy | idle
    last_text: str = ""
    last_tool: str = ""
    last_activity: float = 0.0
    records: int = 0
    first_prompt: str = ""


def parse_transcript_lines(lines, session_id: str, slug: str | None = None, now: float | None = None,
                           mtime: float | None = None) -> TranscriptInfo:
    """Derive the session summary from JSONL records (the tail is enough)."""
    info = TranscriptInfo(session_id=session_id)
    last_kind = None                     # what the newest main-line record was
    last_ts = None
    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            o = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(o, dict):
            continue
        t = o.get("type")
        if t == "ai-title" and isinstance(o.get("aiTitle"), str):
            info.title = o["aiTitle"].strip()
            continue
        if o.get("isSidechain"):
            continue
        ts = _iso_to_epoch(o.get("timestamp"))
        if t == "system":
            if o.get("subtype") == "turn_duration":
                last_kind = "idle"
                last_ts = ts or last_ts
            continue
        if t not in ("user", "assistant"):
            continue
        info.records += 1
        if ts:
            last_ts = ts
        if isinstance(o.get("cwd"), str) and o["cwd"]:
            info.cwd = o["cwd"]
        msg = o.get("message") if isinstance(o.get("message"), dict) else {}
        content = msg.get("content")
        if t == "user":
            if isinstance(content, str) or (isinstance(content, list)
                                            and not any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)):
                text = _block_text(content).strip()
                if text and not o.get("isMeta") and not text.startswith("<"):
                    info.first_prompt = info.first_prompt or text
                last_kind = "prompt"
            else:
                last_kind = "tool_result"
            continue
        # assistant
        if isinstance(content, list):
            for b in content:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text" and b.get("text", "").strip():
                    info.last_text = b["text"].strip()
                elif b.get("type") == "tool_use":
                    info.last_tool = summarize_tool(b.get("name", ""), b.get("input"))
        elif isinstance(content, str) and content.strip():
            info.last_text = content.strip()
        stop = msg.get("stop_reason")
        if stop == "end_turn" or stop == "stop_sequence":
            last_kind = "idle"
        elif stop == "tool_use":
            last_kind = "tool_use"
        else:
            last_kind = "assistant_partial"
    info.last_activity = last_ts or mtime or 0.0
    info.status = "idle" if last_kind in (None, "idle") else "busy"
    now = time.time() if now is None else now
    if info.status == "busy" and info.last_activity and now - info.last_activity > STALE_S:
        info.status = "idle"
    info.project = project_from_cwd(info.cwd, slug)
    if not info.title:
        info.title = " ".join(info.first_prompt.split())[:60]
    if len(info.last_text) > TEXT_CHARS:
        info.last_text = info.last_text[:TEXT_CHARS - 1] + "…"
    return info


def parse_transcript(path: Path, now: float | None = None, tail_bytes: int = TAIL_BYTES) -> TranscriptInfo:
    size = path.stat().st_size
    with path.open("rb") as fh:
        if size > tail_bytes:
            fh.seek(size - tail_bytes)
            fh.readline()                      # drop the partial line
        data = fh.read().decode("utf-8", errors="replace")
    return parse_transcript_lines(data.splitlines(), path.stem, path.parent.name, now, path.stat().st_mtime)


class TranscriptWatcher:
    """Newest N transcripts under <root>/<project-slug>/<session>.jsonl, re-parsed when they change."""

    def __init__(self, root: Path = PROJECTS_DIR, limit: int = SESSION_LIMIT, clock: Callable[[], float] = time.time) -> None:
        self.root = Path(root)
        self.limit = limit
        self.clock = clock
        self._seen: dict[Path, tuple[float, int]] = {}
        self.infos: dict[str, TranscriptInfo] = {}

    def candidates(self) -> list[Path]:
        if not self.root.is_dir():
            return []
        files = [p for p in self.root.glob("*/*.jsonl") if p.is_file()]
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return files[:self.limit]

    def scan(self) -> tuple[dict[str, TranscriptInfo], bool]:
        """Returns (session_id -> info, changed)."""
        changed = False
        keep: dict[str, TranscriptInfo] = {}
        for p in self.candidates():
            try:
                st = p.stat()
            except OSError:
                continue
            key = (st.st_mtime, st.st_size)
            if self._seen.get(p) != key or p.stem not in self.infos:
                try:
                    self.infos[p.stem] = parse_transcript(p, self.clock())
                except OSError:
                    continue
                self._seen[p] = key
                changed = True
            keep[p.stem] = self.infos[p.stem]
        if set(keep) != set(self.infos):
            changed = True
        self.infos = keep
        return keep, changed


# ======================================================= managed sessions
def prompt_pending(lines: list[str], last_output_at: float, now: float, quiet_s: float = PROMPT_QUIET_S) -> bool:
    """True when the program looks like it is waiting at a prompt."""
    if now - last_output_at < quiet_s:
        return False
    last = ""
    for line in reversed(lines):
        if line.strip():
            last = line.rstrip()
            break
    return bool(last) and PROMPT_RE.search(last) is not None


def render_ansi(data: str | bytes, cols: int = COLS, rows: int = ROWS) -> list[str]:
    """Feed terminal output through pyte and return the screen lines."""
    import pyte
    screen = pyte.Screen(cols, rows)
    if isinstance(data, bytes):
        pyte.ByteStream(screen).feed(data)
    else:
        pyte.Stream(screen).feed(data)
    return [line.rstrip() for line in screen.display]


KEYS = {"enter": "\r", "escape": "\x1b", "esc": "\x1b", "tab": "\t", "ctrl-c": "\x03", "ctrl-d": "\x04",
        "ctrl-z": "\x1a", "backspace": "\x7f", "space": " ", "y": "y", "n": "n",
        "up": "\x1b[A", "down": "\x1b[B", "right": "\x1b[C", "left": "\x1b[D"}


class ManagedSession:
    """A command in a ConPTY, rendered through pyte, writable from the glasses.

    ``spawn`` can be swapped for tests: it must return an object with
    ``read(size) -> str`` (blocking, EOFError at the end), ``write(str)``,
    ``isalive()``, ``terminate()``.
    """

    def __init__(self, command: list[str], *, cols: int = COLS, rows: int = ROWS, cwd: str | None = None,
                 history: int = 400, clock: Callable[[], float] = time.monotonic,
                 spawn: Callable[..., Any] | None = None, session_id: str | None = None) -> None:
        import pyte
        self.id = session_id or f"term-{uuid.uuid4().hex[:8]}"
        self.command = list(command)
        self.cols, self.rows = cols, rows
        self.cwd = cwd or os.getcwd()
        self.clock = clock
        self.screen = pyte.HistoryScreen(cols, rows, history=history, ratio=0.5)
        self.stream = pyte.Stream(self.screen)
        self.last_output_at = clock()
        self.started_at = clock()
        self._alive = False
        self.exit_code: int | None = None
        self._spawn = spawn
        self._pty = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self.on_change: Callable[[], None] | None = None
        self.on_output: Callable[[str], None] | None = None     # raw passthrough (local console)

    # ---- lifecycle
    def start(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        self._loop = loop
        if self._spawn is None:
            from winpty import PtyProcess
            env = dict(os.environ)
            env.setdefault("TERM", "xterm-256color")
            self._pty = PtyProcess.spawn(self.command, cwd=self.cwd, env=env, dimensions=(self.rows, self.cols))
        else:
            self._pty = self._spawn(self.command, cwd=self.cwd, dimensions=(self.rows, self.cols))
        self._alive = True
        self._thread = threading.Thread(target=self._reader, name=f"pty-{self.id}", daemon=True)
        self._thread.start()

    def _reader(self) -> None:
        try:
            while True:
                data = self._pty.read(4096)
                if not data:
                    continue
                self.feed(data)
        except (EOFError, OSError):
            pass
        finally:
            self._alive = False
            try:
                self.exit_code = self._pty.exitstatus if hasattr(self._pty, "exitstatus") else None
            except Exception:
                self.exit_code = None
            self._changed()

    def feed(self, data: str | bytes) -> None:
        if isinstance(data, bytes):
            data = data.decode("utf-8", errors="replace")
        with self._lock:
            self.stream.feed(data)
            self.last_output_at = self.clock()
        if self.on_output:
            try:
                self.on_output(data)
            except Exception:
                pass
        self._changed()

    def _changed(self) -> None:
        cb = self.on_change
        if cb is None:
            return
        if self._loop is not None and not self._loop.is_closed():
            try:
                self._loop.call_soon_threadsafe(cb)
                return
            except RuntimeError:
                pass
        cb()

    @property
    def alive(self) -> bool:
        """ConPTY does not always deliver EOF to the reader, so ask the process too."""
        if not self._alive:
            return False
        try:
            if self._pty is not None and hasattr(self._pty, "isalive") and not self._pty.isalive():
                self._alive = False
                try:
                    self.exit_code = getattr(self._pty, "exitstatus", None)
                except Exception:
                    pass
                self._changed()
        except Exception:
            pass
        return self._alive

    def stop(self) -> None:
        if self._pty is not None and self._alive:
            try:
                self._pty.terminate()
            except Exception:
                pass
        self._alive = False

    # ---- output
    def lines(self) -> list[str]:
        with self._lock:
            return [line.rstrip() for line in self.screen.display]

    def history_lines(self) -> list[str]:
        with self._lock:
            out = []
            for line in self.screen.history.top:
                out.append("".join(line[x].data for x in range(self.cols)).rstrip())
            return out

    def needs_input(self, now: float | None = None) -> bool:
        return self.alive and prompt_pending(self.lines(), self.last_output_at, self.clock() if now is None else now)

    # ---- input
    def write(self, text: str = "", enter: bool = False, key: str | None = None) -> None:
        data = text or ""
        if key:
            data += KEYS.get(key.lower(), "")
        if enter:
            data += "\r"
        if data and self._pty is not None:
            self._pty.write(data)


# ================================================================ registry
@dataclass
class Pending:
    id: str
    session_id: str
    tool_name: str
    summary: str
    tool_input: Any
    created: float
    future: asyncio.Future = field(repr=False)


@dataclass
class Session:
    id: str
    kind: str = "transcript"          # transcript | managed
    project: str = "?"
    title: str = ""
    cwd: str = ""
    status: str = "idle"              # busy | idle | waiting | ended
    last_text: str = ""
    last_tool: str = ""
    last_activity: float = 0.0
    hint_status: str = ""             # from hooks: busy | idle
    hint_at: float = 0.0
    pending_id: str | None = None
    managed: Any = None               # ManagedSession or RemoteManaged
    command: str = ""
    transcript: bool = False
    hook_seen: bool = False

    def effective_status(self, now: float) -> str:
        if self.pending_id:
            return "waiting"
        if self.kind == "managed":
            m = self.managed
            if m is None or not getattr(m, "alive", False):
                return "ended"
            return "waiting" if m.needs_input() else "busy"
        if self.hint_at and self.hint_at >= self.last_activity:
            st = self.hint_status or self.status
            if st == "busy" and now - self.hint_at > STALE_S:
                return "idle"
            return st
        return self.status


class Registry:
    """All sessions and pending approvals; no network in here, so it is easy to test."""

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self.clock = clock
        self.sessions: dict[str, Session] = {}
        self.pending: dict[str, Pending] = {}
        self.dirty = True
        self.on_dirty: Callable[[], None] | None = None

    def touch(self) -> None:
        self.dirty = True
        if self.on_dirty:
            self.on_dirty()

    def get(self, sid: str, kind: str = "transcript") -> Session:
        s = self.sessions.get(sid)
        if s is None:
            s = self.sessions[sid] = Session(id=sid, kind=kind, last_activity=self.clock())
        return s

    # ---- transcripts
    def apply_transcripts(self, infos: dict[str, TranscriptInfo]) -> None:
        for sid, info in infos.items():
            s = self.get(sid)
            s.transcript = True
            s.project, s.cwd, s.title = info.project, info.cwd, info.title or s.title
            s.status, s.last_text, s.last_tool = info.status, info.last_text, info.last_tool
            s.last_activity = max(info.last_activity, s.last_activity if s.hook_seen else 0.0)
        for sid in list(self.sessions):
            s = self.sessions[sid]
            if s.transcript and sid not in infos and not s.pending_id and s.kind != "managed":
                del self.sessions[sid]
        self.touch()

    # ---- hooks
    def hook_event(self, event: str, data: dict) -> Pending | None:
        sid = str(data.get("session_id") or "")
        if not sid:
            return None
        now = self.clock()
        s = self.get(sid)
        s.hook_seen = True
        s.last_activity = now
        cwd = data.get("cwd")
        if isinstance(cwd, str) and cwd:
            s.cwd = cwd
            s.project = project_from_cwd(cwd)
        pending = None
        if event == "PermissionRequest":
            pid = f"perm-{uuid.uuid4().hex[:8]}"
            name = str(data.get("tool_name") or "tool")
            try:
                fut = asyncio.get_running_loop().create_future()
            except RuntimeError:
                import concurrent.futures
                fut = concurrent.futures.Future()
            pending = Pending(pid, sid, name, summarize_tool(name, data.get("tool_input")), data.get("tool_input"),
                              now, fut)
            self.pending[pid] = pending
            s.pending_id = pid
            s.last_tool = pending.summary
        elif event in ("SessionStart",):
            s.hint_status, s.hint_at = "idle", now
        elif event in ("UserPromptSubmit", "PostToolUse"):
            s.hint_status, s.hint_at = "busy", now
        elif event == "PreToolUse":
            s.hint_status, s.hint_at = "busy", now
            s.last_tool = summarize_tool(str(data.get("tool_name") or "tool"), data.get("tool_input"))
        elif event == "Stop":
            s.hint_status, s.hint_at = "idle", now
            msg = data.get("last_assistant_message")
            if isinstance(msg, str) and msg.strip():
                s.last_text = msg.strip()[:TEXT_CHARS]
        elif event == "SessionEnd":
            s.hint_status, s.hint_at = "idle", now
            self._resolve_session(sid, None)
        elif event == "Notification":
            kind = data.get("notification_type")
            if kind == "idle_prompt":
                s.hint_status, s.hint_at = "idle", now
        self.touch()
        return pending

    def _resolve_session(self, sid: str, decision: dict | None) -> bool:
        s = self.sessions.get(sid)
        if s is None or not s.pending_id:
            return False
        return self.resolve(s.pending_id, decision)

    def resolve(self, pid: str, decision: dict | None) -> bool:
        p = self.pending.pop(pid, None)
        if p is None:
            return False
        s = self.sessions.get(p.session_id)
        if s is not None and s.pending_id == pid:
            s.pending_id = None
            s.hint_status, s.hint_at = "busy", self.clock()
        if not p.future.done():
            p.future.set_result(decision)
        self.touch()
        return True

    def decide(self, target: str, behavior: str, message: str = "") -> bool:
        """target is a session id or an approval id. Returns False when nothing was pending."""
        behavior = "allow" if behavior == "allow" else "deny"
        decision: dict[str, Any] = {"behavior": behavior}
        if behavior == "deny" and message:
            decision["message"] = message
        if target in self.pending:
            return self.resolve(target, decision)
        return self._resolve_session(target, decision)

    # ---- managed
    def add_managed(self, managed) -> Session:
        s = self.get(managed.id, "managed")
        s.kind = "managed"
        s.managed = managed
        s.command = " ".join(managed.command) if isinstance(managed.command, (list, tuple)) else str(managed.command)
        s.cwd = getattr(managed, "cwd", "") or ""
        s.project = project_from_cwd(s.cwd) if s.cwd else s.command.split()[0]
        s.title = s.command
        s.last_activity = self.clock()
        if hasattr(managed, "on_change"):
            managed.on_change = self._managed_changed(s.id)
        self.touch()
        return s

    def _managed_changed(self, sid: str):
        def cb():
            s = self.sessions.get(sid)
            if s is not None:
                s.last_activity = self.clock()
            self.touch()
        return cb

    def remove(self, sid: str) -> None:
        s = self.sessions.pop(sid, None)
        if s is not None and s.pending_id:
            self.resolve(s.pending_id, None)
        self.touch()

    def input_target(self, s: Session) -> Session | None:
        """Where typed text for ``s`` goes. A transcript session borrows the
        console of a live managed session running claude in the same folder
        (heuristic: Claude Code does not tell a hook which console it is in)."""
        if s.kind == "managed":
            return s
        if not s.cwd:
            return None
        want = os.path.normcase(os.path.normpath(s.cwd))
        hits = [m for m in self.sessions.values()
                if m.kind == "managed" and m.managed is not None and getattr(m.managed, "alive", False)
                and "claude" in m.command.lower() and m.cwd
                and os.path.normcase(os.path.normpath(m.cwd)) == want]
        return hits[0] if len(hits) == 1 else None

    def send_input(self, sid: str, text: str = "", enter: bool = False, key: str | None = None) -> tuple[bool, str]:
        s = self.sessions.get(sid)
        if s is None:
            return False, "no such session"
        s = self.input_target(s)
        if s is None or s.managed is None:
            return False, "read-only: use terminal.py run"
        if not getattr(s.managed, "alive", False):
            return False, "session ended"
        s.managed.write(text, enter=enter, key=key)
        return True, "sent"

    # ---- views
    def ordered(self) -> list[Session]:
        now = self.clock()
        rank = {"waiting": 0, "busy": 1, "idle": 2, "ended": 3}
        return sorted(self.sessions.values(), key=lambda s: (rank.get(s.effective_status(now), 9), -s.last_activity))

    def summary(self, s: Session) -> dict:
        now = self.clock()
        p = self.pending.get(s.pending_id) if s.pending_id else None
        return {
            "id": s.id, "kind": s.kind, "project": s.project, "title": s.title, "cwd": s.cwd,
            "status": s.effective_status(now), "age_s": max(0, int(now - s.last_activity)),
            "last_activity": s.last_activity, "last_text": s.last_text, "last_tool": s.last_tool,
            "command": s.command,
            "input": (lambda t: t.id if t is not None else None)(self.input_target(s)),
            "pending": None if p is None else {"id": p.id, "tool_name": p.tool_name, "summary": p.summary,
                                                "age_s": int(now - p.created)},
        }

    def detail(self, s: Session) -> dict:
        d = self.summary(s)
        if s.kind == "managed" and s.managed is not None:
            m = s.managed
            d["screen"] = m.lines()
            d["history"] = m.history_lines()[-200:]
            d["needs_input"] = m.needs_input()
            d["alive"] = bool(getattr(m, "alive", False))
        return d

    def state(self) -> dict:
        return {"type": "state", "sessions": [self.detail(s) for s in self.ordered()], "time": self.clock()}


class RemoteManaged:
    """A managed session living in another process (``run`` attached over /ws/agent)."""

    def __init__(self, sid: str, command: list[str], cwd: str, send: Callable[[dict], Any]) -> None:
        self.id, self.command, self.cwd = sid, command, cwd
        self._send = send
        self.alive = True
        self._lines: list[str] = []
        self._history: list[str] = []
        self._needs_input = False
        self.on_change: Callable[[], None] | None = None

    def update(self, msg: dict) -> None:
        self._lines = list(msg.get("lines") or [])
        self._history = list(msg.get("history") or [])
        self._needs_input = bool(msg.get("needs_input"))
        self.alive = bool(msg.get("alive", True))
        if self.on_change:
            self.on_change()

    def lines(self) -> list[str]:
        return self._lines

    def history_lines(self) -> list[str]:
        return self._history

    def needs_input(self, now=None) -> bool:
        return self.alive and self._needs_input

    def write(self, text: str = "", enter: bool = False, key: str | None = None) -> None:
        self._send({"type": "input", "text": text, "enter": enter, "key": key})


# ================================================================== server
INDEX_HTML = """<!doctype html><meta charset="utf-8"><title>G2 Terminal</title>
<style>body{font:14px/1.4 Consolas,monospace;background:#0b0f0b;color:#9f9;margin:16px}
.s{border:1px solid #363;border-radius:4px;padding:8px;margin:0 0 12px}pre{margin:6px 0;white-space:pre;overflow-x:auto}
b.w{color:#ff6}input{width:30em;background:#000;color:#9f9;border:1px solid #363}button{margin-left:4px}</style>
<h2>G2 Terminal <small id="st"></small></h2><div id="list"></div>
<script>
let ws;const esc=s=>String(s??'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
function connect(){ws=new WebSocket('ws://'+location.host+'/ws');
ws.onopen=()=>st.textContent='connected';ws.onclose=()=>{st.textContent='disconnected, retrying';setTimeout(connect,2000)};
ws.onmessage=e=>{const m=JSON.parse(e.data);if(m.type!=='state')return;list.innerHTML=m.sessions.map(s=>`<div class="s">
<b class="${s.status==='waiting'?'w':''}">${esc(s.status)}</b> ${esc(s.project)} <small>${esc(s.title||s.command)} · ${s.age_s}s · ${esc(s.id)}</small>
${s.pending?`<div>APPROVE? ${esc(s.pending.summary)} <button onclick="decide('${s.id}','allow')">allow</button><button onclick="decide('${s.id}','deny')">deny</button></div>`:''}
${s.screen?`<pre>${esc(s.screen.join('\\n'))}</pre>`:`<pre>${esc(s.last_text)}</pre><small>${esc(s.last_tool)}</small>`}
${s.kind==='managed'?`<div><input id="i-${s.id}" placeholder="type, Enter sends"><button onclick="key('${s.id}','ctrl-c')">^C</button><button onclick="key('${s.id}','escape')">Esc</button></div>`:''}
</div>`).join('');document.querySelectorAll('input').forEach(i=>i.onkeydown=ev=>{if(ev.key==='Enter'){send({type:'input',session:i.id.slice(2),text:i.value,enter:true});i.value=''}})}}
function send(o){ws.send(JSON.stringify(o))}function decide(s,b){send({type:'decide',session:s,behavior:b})}
function key(s,k){send({type:'input',session:s,key:k})}connect();
</script>"""


class TerminalServer:
    def __init__(self, host: str = HOST, port: int = PORT, projects_dir: Path | None = PROJECTS_DIR,
                 limit: int = SESSION_LIMIT, scan_interval: float = 2.0, clock: Callable[[], float] = time.time,
                 approval_timeout: float = APPROVAL_TIMEOUT_S) -> None:
        from aiohttp import web
        self.web = web
        self.host, self.port = host, port
        self.registry = Registry(clock)
        self.registry.on_dirty = self._mark_dirty
        self.watcher = TranscriptWatcher(projects_dir, limit, clock) if projects_dir else None
        self.scan_interval = scan_interval
        self.approval_timeout = approval_timeout
        self.wait_without_clients = False
        self.clients: set = set()
        self._dirty = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        self.managed: list[ManagedSession] = []
        self.app = web.Application()
        self.app.add_routes([
            web.get("/", self.h_index), web.get("/sessions", self.h_sessions), web.get("/sessions/{sid}", self.h_session),
            web.post("/sessions", self.h_spawn), web.post("/sessions/{sid}/input", self.h_input),
            web.post("/sessions/{sid}/decide", self.h_decide), web.post("/approvals/{pid}/decide", self.h_decide),
            web.post("/hook/{event}", self.h_hook), web.get("/ws", self.h_ws), web.get("/ws/agent", self.h_ws_agent),
            web.get("/health", self.h_health),
        ])
        self._runner = None

    # ---- lifecycle
    async def start(self) -> None:
        self._runner = self.web.AppRunner(self.app, access_log=None)
        await self._runner.setup()
        site = self.web.TCPSite(self._runner, self.host, self.port)
        await site.start()
        if self.port == 0:
            self.port = self._runner.addresses[0][1]
        self._tasks = [asyncio.create_task(self._scan_loop(), name="terminal-scan"),
                       asyncio.create_task(self._broadcast_loop(), name="terminal-broadcast")]
        log.info("terminal server on http://%s:%d", self.host, self.port)

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        for m in self.managed:
            m.stop()
        for ws in list(self.clients):
            try:
                await ws.close()
            except Exception:
                pass
        if self._runner is not None:
            await self._runner.cleanup()

    def _mark_dirty(self) -> None:
        self._dirty.set()

    async def _scan_loop(self) -> None:
        while True:
            self.scan_once()
            await asyncio.sleep(self.scan_interval)

    def scan_once(self) -> None:
        if self.watcher is None:
            return
        try:
            infos, changed = self.watcher.scan()
        except Exception:
            log.exception("transcript scan failed")
            return
        if changed:
            self.registry.apply_transcripts(infos)

    def _signature(self) -> tuple:
        now = self.registry.clock()
        return tuple((s.id, s.effective_status(now)) for s in self.registry.sessions.values())

    async def _broadcast_loop(self) -> None:
        period = 1.0 / BROADCAST_HZ
        sig, last = None, 0.0
        while True:
            try:
                await asyncio.wait_for(self._dirty.wait(), 0.5)
            except asyncio.TimeoutError:
                pass
            new = self._signature()          # catches the prompt timer and silent exits
            mono = time.monotonic()
            if not self._dirty.is_set() and new == sig and mono - last < 15:
                continue
            await asyncio.sleep(period)
            self._dirty.clear()
            sig, last = self._signature(), mono
            await self.broadcast()

    async def broadcast(self) -> None:
        if not self.clients:
            return
        payload = json.dumps(self.registry.state())
        for ws in list(self.clients):
            try:
                await ws.send_str(payload)
            except Exception:
                self.clients.discard(ws)

    def add_managed(self, m: ManagedSession) -> Session:
        self.managed.append(m)
        return self.registry.add_managed(m)

    # ---- handlers
    async def h_index(self, request):
        return self.web.Response(text=INDEX_HTML, content_type="text/html")

    async def h_health(self, request):
        return self.web.json_response({"ok": True, "sessions": len(self.registry.sessions)})

    async def h_sessions(self, request):
        return self.web.json_response(self.registry.state())

    async def h_session(self, request):
        s = self.registry.sessions.get(request.match_info["sid"])
        if s is None:
            return self.web.json_response({"error": "no such session"}, status=404)
        return self.web.json_response(self.registry.detail(s))

    async def h_spawn(self, request):
        body = await request.json()
        cmd = body.get("command")
        if isinstance(cmd, str):
            cmd = cmd.split()
        if not cmd:
            return self.web.json_response({"error": "command required"}, status=400)
        m = ManagedSession(cmd, cols=int(body.get("cols", COLS)), rows=int(body.get("rows", ROWS)), cwd=body.get("cwd"))
        try:
            m.start(asyncio.get_running_loop())
        except Exception as exc:
            return self.web.json_response({"error": f"{type(exc).__name__}: {exc}"}, status=500)
        s = self.add_managed(m)
        return self.web.json_response(self.registry.detail(s))

    async def h_input(self, request):
        body = await request.json()
        ok, why = self.registry.send_input(request.match_info["sid"], str(body.get("text", "")),
                                           bool(body.get("enter")), body.get("key"))
        return self.web.json_response({"ok": ok, "message": why}, status=200 if ok else 409)

    async def h_decide(self, request):
        body = await request.json()
        target = request.match_info.get("sid") or request.match_info.get("pid")
        ok = self.registry.decide(target, str(body.get("behavior", "deny")), str(body.get("message", "")))
        return self.web.json_response({"ok": ok}, status=200 if ok else 404)

    async def h_hook(self, request):
        event = request.match_info["event"]
        try:
            data = await request.json()
        except Exception:
            data = {}
        if not isinstance(data, dict):
            data = {}
        pending = self.registry.hook_event(event, data)
        if pending is None:
            return self.web.json_response({"ok": True})
        if not self.clients and not self.wait_without_clients:
            # nobody is looking at the glasses or the page: let Claude Code prompt at once
            self.registry.resolve(pending.id, None)
            return self.web.json_response({"ok": True, "decision": None, "message": "no client connected"})
        try:
            timeout = float(request.query.get("timeout", self.approval_timeout))
        except ValueError:
            timeout = self.approval_timeout
        try:
            decision = await asyncio.wait_for(asyncio.shield(pending.future), timeout=timeout)
        except asyncio.TimeoutError:
            decision = None
            self.registry.resolve(pending.id, None)
        return self.web.json_response({"ok": True, "decision": decision})

    async def _handle_client_message(self, msg: dict) -> dict:
        kind = msg.get("type")
        if kind == "input":
            ok, why = self.registry.send_input(str(msg.get("session", "")), str(msg.get("text", "")),
                                               bool(msg.get("enter")), msg.get("key"))
            return {"type": "ack", "ok": ok, "message": why, "session": msg.get("session")}
        if kind == "decide":
            ok = self.registry.decide(str(msg.get("session") or msg.get("approval") or ""),
                                      str(msg.get("behavior", "deny")), str(msg.get("message", "")))
            return {"type": "ack", "ok": ok, "message": "decided" if ok else "nothing pending", "session": msg.get("session")}
        if kind == "refresh":
            return self.registry.state()
        return {"type": "ack", "ok": False, "message": "unknown message"}

    async def h_ws(self, request):
        ws = self.web.WebSocketResponse(heartbeat=20)
        await ws.prepare(request)
        self.clients.add(ws)
        try:
            await ws.send_str(json.dumps(self.registry.state()))
            async for m in ws:
                if m.type != self.web.WSMsgType.TEXT:
                    continue
                try:
                    msg = json.loads(m.data)
                except ValueError:
                    continue
                await ws.send_str(json.dumps(await self._handle_client_message(msg)))
        finally:
            self.clients.discard(ws)
        return ws

    async def h_ws_agent(self, request):
        ws = self.web.WebSocketResponse(heartbeat=20)
        await ws.prepare(request)
        remote: RemoteManaged | None = None

        def send(d):
            asyncio.ensure_future(ws.send_str(json.dumps(d)))
        try:
            async for m in ws:
                if m.type != self.web.WSMsgType.TEXT:
                    continue
                try:
                    msg = json.loads(m.data)
                except ValueError:
                    continue
                if msg.get("type") == "register":
                    remote = RemoteManaged(str(msg.get("id")), list(msg.get("command") or []), str(msg.get("cwd") or ""), send)
                    self.registry.add_managed(remote)
                elif msg.get("type") == "screen" and remote is not None:
                    remote.update(msg)
        finally:
            if remote is not None:
                remote.alive = False
                self.registry.touch()
                asyncio.get_running_loop().call_later(30, self.registry.remove, remote.id)
        return ws


# ====================================================== settings.json hooks
def hook_command(event: str, python: str | None = None, script: Path | None = None) -> str:
    py = python or sys.executable
    return f'"{py}" "{script or HOOK_SCRIPT}" {event}'


def _is_ours(entry: dict) -> bool:
    return any(HOOK_MARK in str(h.get("command", "")) for h in entry.get("hooks", []) if isinstance(h, dict))


def merge_hooks(settings: dict, python: str | None = None, script: Path | None = None,
                events=HOOK_EVENTS) -> tuple[dict, list[str]]:
    """Return (new settings, change log). Non-destructive and idempotent."""
    out = json.loads(json.dumps(settings))
    hooks = out.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError('"hooks" in settings.json is not an object')
    changes = []
    for ev in events:
        handler = {"type": "command", "command": hook_command(ev, python, script),
                   "timeout": HOOK_TIMEOUTS.get(ev, HOOK_DEFAULT_TIMEOUT)}
        if ev != "PermissionRequest":
            handler["async"] = True           # status reports never slow Claude down
        entry = {"hooks": [handler]}
        lst = hooks.setdefault(ev, [])
        if not isinstance(lst, list):
            raise ValueError(f'"hooks.{ev}" is not a list')
        ours = [i for i, e in enumerate(lst) if isinstance(e, dict) and _is_ours(e)]
        if ours:
            if lst[ours[0]] != entry:
                lst[ours[0]] = entry
                changes.append(f"updated {ev}")
            for i in reversed(ours[1:]):
                del lst[i]
                changes.append(f"removed duplicate {ev}")
        else:
            lst.append(entry)
            changes.append(f"added {ev}")
    return out, changes


def unmerge_hooks(settings: dict) -> tuple[dict, list[str]]:
    out = json.loads(json.dumps(settings))
    hooks = out.get("hooks")
    changes = []
    if isinstance(hooks, dict):
        for ev in list(hooks):
            lst = hooks[ev]
            if not isinstance(lst, list):
                continue
            kept = [e for e in lst if not (isinstance(e, dict) and _is_ours(e))]
            if len(kept) != len(lst):
                changes.append(f"removed {ev}")
            if kept:
                hooks[ev] = kept
            else:
                del hooks[ev]
        if not hooks:
            del out["hooks"]
    return out, changes


def _rewrite_settings(path: Path, fn) -> list[str]:
    path = Path(path)
    settings = {}
    if path.exists():
        text = path.read_text(encoding="utf-8")
        settings = json.loads(text) if text.strip() else {}
    new, changes = fn(settings)
    if changes:
        if path.exists():
            backup = path.with_name(f"{path.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
            n = 1
            while backup.exists():
                backup = path.with_name(f"{path.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}-{n}")
                n += 1
            shutil.copy2(path, backup)
            print(f"backup: {backup}")
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(new, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    for c in changes:
        print("  " + c)
    print(f"{path}: {'no change' if not changes else f'{len(changes)} change(s)'}")
    return changes


def cmd_install_hooks(a) -> int:
    _rewrite_settings(Path(a.settings), lambda s: merge_hooks(s, a.python))
    print("hooks run: " + hook_command("<Event>", a.python))
    print("they report to http://%s:%d; Claude Code re-reads settings.json on change" % (HOST, a.port))
    return 0


def cmd_uninstall_hooks(a) -> int:
    _rewrite_settings(Path(a.settings), unmerge_hooks)
    return 0


# ================================================================= run / cli
async def _serve(a, managed: ManagedSession | None = None) -> int:
    server = TerminalServer(a.host, a.port, Path(a.projects) if a.projects else None, a.limit)
    await server.start()
    if managed is not None:
        server.add_managed(managed)
    print(f"terminal hub: http://{a.host}:{a.port}/   (Ctrl-C stops it)")
    try:
        if managed is not None:
            while managed.alive:
                await asyncio.sleep(0.5)
        else:
            while True:
                await asyncio.sleep(3600)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await server.stop()
    return 0


def _enable_vt() -> None:
    if os.name != "nt":
        return
    try:
        import ctypes
        k = ctypes.windll.kernel32
        h = k.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if k.GetConsoleMode(h, ctypes.byref(mode)):
            k.SetConsoleMode(h, mode.value | 0x0004)
    except Exception:
        pass


def _local_keys(m: ManagedSession) -> None:
    """Forward keystrokes from this console to the pty (Windows console only)."""
    try:
        import msvcrt
    except ImportError:
        return

    def loop():
        while m.alive:
            try:
                ch = msvcrt.getwch()
            except Exception:
                return
            if ch in ("\x00", "\xe0"):
                code = msvcrt.getwch()
                ch = {"H": "\x1b[A", "P": "\x1b[B", "M": "\x1b[C", "K": "\x1b[D"}.get(code, "")
            if ch:
                m.write(ch)
    threading.Thread(target=loop, name="local-keys", daemon=True).start()


async def _attach_agent(a, m: ManagedSession) -> bool:
    """Publish ``m`` to an already running hub over /ws/agent. False if there is none."""
    import aiohttp
    url = f"http://{a.host}:{a.port}/ws/agent"
    try:
        session = aiohttp.ClientSession()
        ws = await session.ws_connect(url, timeout=aiohttp.ClientWSTimeout(ws_close=2))
    except Exception:
        try:
            await session.close()
        except Exception:
            pass
        return False
    print(f"attached to the hub at http://{a.host}:{a.port}/ as session {m.id}")
    await ws.send_json({"type": "register", "id": m.id, "command": m.command, "cwd": m.cwd, "cols": m.cols, "rows": m.rows})
    dirty = asyncio.Event()
    loop = asyncio.get_running_loop()
    m.on_change = lambda: dirty.set()
    m._loop = loop

    async def publish():
        was_waiting = False
        try:
            while True:
                try:
                    await asyncio.wait_for(dirty.wait(), 0.5)
                except asyncio.TimeoutError:
                    pass
                alive, waiting = m.alive, m.needs_input()
                if not dirty.is_set() and alive and waiting == was_waiting:
                    continue                    # nothing new; the 0.5 s poll catches exit and the prompt timer
                await asyncio.sleep(1.0 / BROADCAST_HZ)
                dirty.clear()
                was_waiting = waiting
                await ws.send_json({"type": "screen", "id": m.id, "lines": m.lines(), "history": m.history_lines()[-200:],
                                    "needs_input": m.needs_input(), "alive": m.alive})
                if not m.alive:
                    await ws.close()
                    return
        except Exception:
            pass

    pub = asyncio.create_task(publish())
    dirty.set()
    try:
        async for msg in ws:
            if msg.type != aiohttp.WSMsgType.TEXT:
                break
            try:
                d = json.loads(msg.data)
            except ValueError:
                continue
            if d.get("type") == "input":
                m.write(str(d.get("text", "")), enter=bool(d.get("enter")), key=d.get("key"))
            if not m.alive:
                break
    finally:
        pub.cancel()
        await ws.close()
        await session.close()
    return True


async def _run(a) -> int:
    if not a.command:
        print("usage: python g2/terminal.py run [--cols N --rows N] -- <command...>")
        return 2
    m = ManagedSession(a.command, cols=a.cols, rows=a.rows, cwd=a.cwd)
    if not a.quiet:
        _enable_vt()
        m.on_output = lambda s: (sys.stdout.write(s), sys.stdout.flush())
    m.start(asyncio.get_running_loop())
    if not a.no_keys:
        _local_keys(m)
    try:
        if not await _attach_agent(a, m):
            print(f"no hub on port {a.port}; serving this session myself")
            await _serve(a, m)
        while m.alive:
            await asyncio.sleep(0.2)
    finally:
        m.stop()
    print(f"\n[{' '.join(a.command)} exited{'' if m.exit_code is None else f' with {m.exit_code}'}]")
    return 0


def cmd_sessions(a) -> int:
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://{a.host}:{a.port}/sessions", timeout=3) as r:
            st = json.loads(r.read())
    except Exception as exc:
        print(f"no hub at http://{a.host}:{a.port}/ ({type(exc).__name__}); start one: python g2/terminal.py serve")
        return 1
    for s in st["sessions"]:
        print(f"{s['status']:<8} {s['project']:<20} {age_text(s['age_s']):>4}  {s['kind']:<10} {s['id']}")
        if s.get("pending"):
            print(f"         APPROVE? {s['pending']['summary']}")
    if not st["sessions"]:
        print("no sessions")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog=__doc__.split("\n\n", 1)[1])
    ap.add_argument("--host", default=HOST)
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("-v", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("serve", help="run the hub"); p.add_argument("--projects", default=str(PROJECTS_DIR)); p.add_argument("--limit", type=int, default=SESSION_LIMIT)
    p.set_defaults(fn=lambda a: asyncio.run(_serve(a)))
    p = sub.add_parser("run", help="spawn a command the glasses can follow and type into")
    p.add_argument("--cols", type=int, default=COLS); p.add_argument("--rows", type=int, default=ROWS); p.add_argument("--cwd")
    p.add_argument("--quiet", action="store_true", help="do not echo the program here"); p.add_argument("--no-keys", action="store_true", help="do not forward this console's keys")
    p.add_argument("--projects", default=str(PROJECTS_DIR)); p.add_argument("--limit", type=int, default=SESSION_LIMIT)
    p.add_argument("command", nargs=argparse.REMAINDER); p.set_defaults(fn=lambda a: asyncio.run(_run(a)))
    p = sub.add_parser("install-hooks", help="add our hooks to Claude Code's settings.json"); p.add_argument("--settings", default=str(SETTINGS_PATH)); p.add_argument("--python", default=sys.executable); p.set_defaults(fn=cmd_install_hooks)
    p = sub.add_parser("uninstall-hooks", help="remove them again"); p.add_argument("--settings", default=str(SETTINGS_PATH)); p.set_defaults(fn=cmd_uninstall_hooks)
    p = sub.add_parser("sessions", help="print what the hub knows"); p.set_defaults(fn=cmd_sessions)
    a = ap.parse_args(argv)
    if getattr(a, "command", None) and a.command and a.command[0] == "--":
        a.command = a.command[1:]
    logging.basicConfig(level=logging.DEBUG if a.v else logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    try:
        return a.fn(a)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
