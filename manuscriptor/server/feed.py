"""The live agent feed, as a file. And beside it, the history.

The drain writes both and the server reads them, which is the whole of their
relationship. They live here rather than beside the session that produces them
so that the server can read them without importing anything that knows what
Claude is: `server/supervisor.py` spawns a model, and nothing in the server may.

TWO FILES, because there are two questions and they have opposite shapes.

`agent-progress.json` answers "what is happening NOW". It is rewritten rather
than appended, it holds a short ring, and it has exactly one writer.

`agent-history.jsonl` answers "what has it done". It is APPEND ONLY, like
`comments.jsonl` and for a related reason: it must survive the writer. The ring
does not. `Feed` is constructed with `entries=[]` and `Supervisor.run` writes
`booting` into it before anything else, so every drain restart -- roughly one
per twenty wakes -- erased the whole record of the session before it. The
complete stream was always on disk in `agent-stream.jsonl` and nothing has ever
read it back: it is 31MB of tool calls and permission events on a real
manuscript, which is a debugging artifact and not a history.

So the entries the author would actually read are appended here as they are
made, each STAMPED WITH THE COMMENT IT BELONGS TO, and the server threads them
by work item against `comments.jsonl`. Nothing about the outcome is copied into
this file: the outcome is a state record in the comment log, and one fact with
two homes is one fact that will disagree with itself.
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
HISTORY_NAME = "agent-history.jsonl"

# How many feed entries to keep. The live feed answers "what is it doing", which
# is a recent question; what happened is the history beside it.
KEEP = 80

# HOW THE HISTORY IS BOUNDED, deliberately, because an append-only file on a
# manuscript drained for months has no other limit.
#
# The writer rolls the file at `ROTATE_AT` bytes and keeps exactly ONE previous
# generation, so the ledger costs at most twice that on disk and the oldest
# work ages out rather than accumulating. The reader takes the newest
# `READ_BACK` records across both generations, so a panel read is bounded
# whatever the file did. Two megabytes is several days of heavy draining: an
# entry is clipped to 400 characters, and dsp-bias's busiest recorded run --
# 71 turns, 83 dispatched teammates -- would have written about a fifth of it.
ROTATE_AT = 2_000_000
READ_BACK = 4000

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
    """One line of the live feed, and one line of the history.

    `work` is the comment ids this line belongs to. It is the whole of the link
    between a sentence and the request that caused it: until it existed the only
    work-item linkage anywhere was the envelope's `working` list, which is
    current-state-only and rewritten, so a line read a minute later belonged to
    nothing. Empty means the line belongs to the session rather than to any
    request -- booting, restarting, idle chatter -- and MORE THAN ONE means the
    session was carrying several requests at once and this line could not be
    told apart between them. Both are recorded as what they are; neither is
    guessed into a single id.
    """

    ts: str
    who: str          # "agent" | "teammate"
    kind: str         # one of KINDS
    text: str
    work: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {"ts": self.ts, "who": self.who, "kind": self.kind, "text": self.text,
                "work": list(self.work)}

def progress_path(agent_dir: Path | str) -> Path:
    """The feed file, given the directory that holds it: `paths.agent_dir`.

    The parameter was called `build_dir` from the old layout, and both readers
    duly handed it a build directory: `build()` passed the cache, `serve`
    passed `build/manuscriptor`. Neither is written, and neither failed loudly.
    """
    return Path(agent_dir) / PROGRESS_NAME

def history_path(agent_dir: Path | str) -> Path:
    """The append-only ledger, given the directory that holds it.

    Beside the live feed in `paths.agent_dir`, which is durable and private:
    NOT under `cache/`, the one tier `manuscriptor clean` may remove. A history
    a routine cleanup deletes is not a history.
    """
    return Path(agent_dir) / HISTORY_NAME

def rolled_path(path: Path | str) -> Path:
    """The one previous generation of a rolled ledger."""
    p = Path(path)
    return p.with_name(p.name + ".1")

@dataclass
class Ledger:
    """The history, as a file. One record per line, appended, never rewritten.

    Same discipline as `comments.jsonl` and for the same reason: a state change
    is a new record. Nothing here is ever edited, so no reader can be handed
    half of a rewrite, and a crash mid-write costs the last line rather than the
    file. A malformed line is skipped on read, never fatal.
    """

    path: Path
    rotate_at: int = ROTATE_AT

    def append(self, entries: list[Entry]) -> None:
        if not entries:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._roll_if_full()
            with open(self.path, "a", encoding="utf-8") as fh:
                for e in entries:
                    fh.write(json.dumps(e.as_dict(), ensure_ascii=False) + "\n")
        except OSError:
            pass          # a history that cannot be written must not stop the work

    def _roll_if_full(self) -> None:
        """Start a new file once this one is big, keeping one generation.

        `os.replace` rather than a copy: it is atomic, so a reader either sees
        the old file or the new one and never a half-copied one, and it costs
        nothing on a file of this size.
        """
        try:
            if self.path.stat().st_size < self.rotate_at:
                return
        except OSError:
            return
        try:
            os.replace(self.path, rolled_path(self.path))
        except OSError:
            pass

def read_history(path: Path | str, *, limit: int = READ_BACK) -> list[dict]:
    """The newest `limit` history records, oldest first, across both generations.

    Oldest first because a work item's lines read as a narrative -- what it
    looked at, what it concluded, what it changed. The panel puts the ITEMS
    newest first; the sentences inside one item run forwards.
    """
    out: list[dict] = []
    for p in (rolled_path(path), Path(path)):
        try:
            text = Path(p).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(rec, dict) or rec.get("kind") not in KINDS:
                continue
            out.append({
                "ts": str(rec.get("ts") or ""),
                "who": "teammate" if rec.get("who") == "teammate" else "agent",
                "kind": str(rec["kind"]),
                "text": str(rec.get("text") or ""),
                "work": [str(w) for w in (rec.get("work") or []) if isinstance(w, str)],
            })
    return out[-limit:] if limit else out

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
    # The history beside the ring. DERIVED FROM `path` rather than passed in,
    # so it cannot be forgotten: a Feed built without one would look and behave
    # exactly like this one and quietly keep no record at all.
    history: "Ledger | None" = None
    _last_write: float = 0.0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        if self.history is None:
            self.history = Ledger(history_path(Path(self.path).parent))

    def add(self, entries: list[Entry], *, force: bool = False) -> None:
        with self._lock:
            # The ledger FIRST, and unconditionally. The ring is coalesced and
            # trimmed, which is right for a status and fatal for a record: an
            # entry that arrived inside the write interval, or the eighty-first
            # of a busy turn, would exist nowhere.
            if self.history is not None:
                self.history.append(entries)
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

    def note(self, text: str, *, work: tuple[str, ...] = ()) -> None:
        self.add([Entry(now(), "agent", "note", text, tuple(work))], force=True)

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
