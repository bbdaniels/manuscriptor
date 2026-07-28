"""The live agent feed, as a file.

The drain writes it and the server reads it, which is the whole of their
relationship. It lives here rather than beside the session that produces it so
that the server can read it without importing anything that knows what Claude
is: `server/supervisor.py` spawns a model, and nothing in the server may.

Rewritten rather than appended, because it holds what is happening NOW and has
exactly one writer. The durable record of what happened is `comments.jsonl`,
which is append-only for the opposite reason: two processes write that one.
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

PROGRESS_NAME = "agent-progress.json"

# How many feed entries to keep. The live feed answers "what is it doing", which
# is a recent question; the durable record of what happened is the comment log.
KEEP = 80

# Seconds of silence, mid-turn, before the session is treated as wedged. Long
# enough for a slow model call or a big file read, short enough that an author
# waiting on a figure is not left guessing for a quarter of an hour.
STALL_AFTER = 150.0

# The tools this work legitimately needs. Named rather than bypassed: a print
# session cannot ask for permission, so anything it might reach for and cannot
# have becomes a hang, and a blanket bypass on the author's own repository is a
# different kind of bad afternoon.
ALLOWED_TOOLS = (
    "Read", "Glob", "Grep", "Edit", "Write", "NotebookEdit", "TodoWrite",
    "Agent", "Skill", "WebFetch", "WebSearch",
    "Bash(Rscript:*)", "Bash(R:*)", "Bash(python3:*)", "Bash(python:*)",
    "Bash(latexmk:*)", "Bash(pdflatex:*)", "Bash(xelatex:*)", "Bash(bibtex:*)",
    "Bash(stata-mp:*)", "Bash(stata:*)",
    "Bash(git status:*)", "Bash(git diff:*)", "Bash(git log:*)",
    "Bash(ls:*)", "Bash(cat:*)", "Bash(head:*)", "Bash(tail:*)", "Bash(wc:*)",
)

def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

# What an entry may be. `tool` is deliberately absent: a tool call is not
# something the author needs to read, and a busy turn emits enough of them to
# push the sentences that ARE out of a KEEP-length window. Enforced here rather
# than at the two ends, so `summarize` not producing them and a file written by
# an older build not showing them are the same rule and cannot drift apart.
KINDS = ("thinking", "text", "result", "note")


@dataclass
class Entry:
    """One line of the live feed."""

    ts: str
    who: str          # "agent" | "teammate"
    kind: str         # one of KINDS
    text: str

    def as_dict(self) -> dict:
        return {"ts": self.ts, "who": self.who, "kind": self.kind, "text": self.text}

def progress_path(agent_dir: Path | str) -> Path:
    """The feed file, given the directory that holds it: `paths.agent_dir`.

    The parameter was called `build_dir` from the old layout, and both readers
    duly handed it a build directory: `build()` passed the cache, `serve`
    passed `build/manuscriptor`. Neither is written, and neither failed loudly.
    """
    return Path(agent_dir) / PROGRESS_NAME

@dataclass
class Feed:
    """The live feed, written where the server can read it.

    Rewritten rather than appended: it holds what is happening now, the author
    reads it as a whole, and the drain is its only writer. The durable record of
    what happened is `comments.jsonl`, which is append-only for the opposite
    reason. Writes are coalesced, because a busy turn produces events faster than
    any reader needs them.
    """

    path: Path
    state: str = "idle"
    working: tuple[str, ...] = ()
    entries: list[Entry] = field(default_factory=list)
    every: float = 0.4
    _last_write: float = 0.0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def add(self, entries: list[Entry], *, force: bool = False) -> None:
        with self._lock:
            self.entries.extend(entries)
            del self.entries[:-KEEP]
            due = force or (time.monotonic() - self._last_write) >= self.every
            if due:
                self._write_locked()

    def set(self, *, state: str | None = None, working: tuple[str, ...] | None = None) -> None:
        with self._lock:
            if state is not None:
                self.state = state
            if working is not None:
                self.working = working
            self._write_locked()

    def note(self, text: str) -> None:
        self.add([Entry(now(), "agent", "note", text)], force=True)

    def flush(self) -> None:
        with self._lock:
            self._write_locked()

    def _write_locked(self) -> None:
        self._last_write = time.monotonic()
        payload = {
            "state": self.state,
            "working": list(self.working),
            "at": now(),
            "entries": [e.as_dict() for e in self.entries],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            pass          # a feed that cannot be written must not stop the work

def read_feed(path: Path | str) -> dict:
    """The feed as the page consumes it. Absent or corrupt reads as idle."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return {"state": "idle", "working": [], "entries": []}
    if not isinstance(data, dict):
        return {"state": "idle", "working": [], "entries": []}
    entries = data.get("entries")
    return {
        "state": str(data.get("state") or "idle"),
        "working": [str(w) for w in (data.get("working") or []) if isinstance(w, str)],
        "at": data.get("at") or "",
        "entries": [
            e for e in entries
            if isinstance(e, dict) and e.get("kind") in KINDS
        ] if isinstance(entries, list) else [],
    }
