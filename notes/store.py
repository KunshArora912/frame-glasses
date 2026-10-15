"""Where spoken thoughts are kept.

An append-only JSON file on the laptop, because a thought you had on a walk
should not depend on a network, an account, or a service still existing next
year. Everything else -- the phone page, the glasses -- is a view of this.

Two rules keep the laptop and the published page from fighting over the same
record, without any merge logic:

  * the laptop only ever CREATES thoughts (you speak, it writes)
  * the page only ever UPDATES their done flag (you tick, it saves)

So a sync is one-way for content and one-way for status, and neither side
can clobber the other's field.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

DEFAULT_PATH = Path(__file__).parent.parent / "thoughts.json"


@dataclass
class Thought:
    text: str
    created: str = ""
    id: str = ""
    done: bool = False
    synced: bool = False
    seconds: float = 0.0          # how long you spoke for
    confidence: str = ""          # transcription quality, if known
    tags: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.id:
            self.id = uuid.uuid4().hex[:12]
        if not self.created:
            self.created = datetime.now().astimezone().isoformat(timespec="seconds")

    @property
    def when(self) -> str:
        try:
            return datetime.fromisoformat(self.created).strftime("%d %b %H:%M")
        except ValueError:
            return self.created[:16]

    @property
    def summary(self) -> str:
        """First clause, for the 26-character panel."""
        head = self.text.split(".")[0].strip() or self.text.strip()
        return head[:60]


# Words that mean "this is a task", not part of the task itself. Stripping
# them is what turns "um, remind me to call the landlord" into something
# worth reading back in a list.
_PREFIXES = (
    "note to self", "reminder", "remind me to", "remind me",
    "don't forget to", "dont forget to", "don't forget", "dont forget",
    "i need to", "i have to", "i should", "make a note to",
    "make a note", "add a todo", "add to do", "todo", "to do",
    "new note", "new task", "add task",
)
_FILLER = ("um", "uh", "er", "erm", "like", "you know", "okay so", "ok so")


def _leading(words: tuple[str, ...]) -> "re.Pattern[str]":
    """Match any of these at the very start, with whatever punctuation and
    space follows. Matching on a word boundary rather than a trailing space
    is what makes 'um, remind me...' strip the same as 'um remind me...'."""
    alternatives = "|".join(re.escape(w) for w in
                            sorted(words, key=len, reverse=True))
    return re.compile(rf"^(?:{alternatives})\b[\s,.:;!?-]*", re.IGNORECASE)


_PREFIX_RE = _leading(_PREFIXES)
_FILLER_RE = _leading(_FILLER)


def clean(text: str) -> str:
    """Trim the scaffolding people say before the actual thought.

    Longest alternatives are tried first so "remind me to" wins over
    "remind me", and nothing is ever stripped down to nothing -- a thought
    that is ONLY scaffolding is kept verbatim rather than lost.
    """
    out = (text or "").strip().lstrip(" ,.")
    if not out:
        return ""

    for _ in range(6):                     # bounded: no unbounded rewriting
        stripped = _FILLER_RE.sub("", out, count=1)
        stripped = _PREFIX_RE.sub("", stripped, count=1)
        stripped = stripped.strip()
        if not stripped:                   # it was scaffolding all the way
            break
        if stripped == out:
            break
        out = stripped

    out = out.strip().strip(",;:-").strip()
    return out[:1].upper() + out[1:] if out else ""


class Store:
    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path else DEFAULT_PATH
        self._items: list[Thought] = []
        self.load()

    # ------------------------------------------------------------- disk

    def load(self) -> None:
        if not self.path.exists():
            self._items = []
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            # Never lose the file to a parse error -- move it aside.
            broken = self.path.with_suffix(".corrupt.json")
            try:
                self.path.replace(broken)
            except OSError:
                pass
            self._items = []
            return
        self._items = [Thought(**row) for row in raw if isinstance(row, dict)]

    def save(self) -> None:
        """Atomic: a crash mid-write must not take the file with it."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps([asdict(t) for t in self._items],
                             indent=2, ensure_ascii=False)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # ------------------------------------------------------------ items

    def add(self, text: str, seconds: float = 0.0,
            confidence: str = "") -> Thought | None:
        tidy = clean(text)
        if not tidy:
            return None
        thought = Thought(text=tidy, seconds=seconds, confidence=confidence)
        self._items.append(thought)
        self.save()
        return thought

    def all(self) -> list[Thought]:
        return list(self._items)

    def pending(self) -> list[Thought]:
        return [t for t in self._items if not t.done]

    def unsynced(self) -> list[Thought]:
        return [t for t in self._items if not t.synced]

    def mark_synced(self, ids: list[str]) -> None:
        wanted = set(ids)
        for t in self._items:
            if t.id in wanted:
                t.synced = True
        self.save()

    def apply_done(self, done_ids: list[str]) -> int:
        """Take tick state back from the page. Status only, never text."""
        wanted = set(done_ids)
        changed = 0
        for t in self._items:
            should = t.id in wanted
            if t.done != should:
                t.done = should
                changed += 1
        if changed:
            self.save()
        return changed

    def __len__(self) -> int:
        return len(self._items)
