"""M4 — the comment log.

`comments.jsonl` lives in the manuscript repo and is git-tracked. It is append
only, and that is structural rather than stylistic: two processes write it, the
server on the author's behalf and Claude on its own. If either rewrote the file
there would be conflicts; if both only append there can never be one. It also
leaves a complete audit trail of what changed in a manuscript and why.

A comment, a direct edit, and a state change are all records in the same log, so
there is one history rather than two.

    {"id":"c-0007","kind":"comment","block":"b-3f2a","file":"main.tex",
     "lines":[212,212],"quote":"first 120 chars of the anchored source",
     "body":"you say this twice, tighten","author":"bb","ts":"..."}
    {"id":"c-0007","kind":"state","state":"working","ts":"..."}
    {"id":"c-0007","kind":"state","state":"done","edit":{...},"ts":"..."}

Drafts deliberately do NOT live here. An unsent draft must never be drained, and
it has a single writer, so it has no business in an append-only log shared with
another process.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

State = Literal["queued", "working", "done", "orphaned"]

TERMINAL = {"done", "orphaned"}


@dataclass(frozen=True)
class Chat:
    """One block's conversation: a comment plus every state record that followed."""

    id: str
    block: str
    file: str
    body: str
    quote: str
    state: State
    author: str = "bb"
    ts: str = ""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def append(log: Path, record: dict) -> dict:
    """Append one record. Opens in append mode and flushes; never rewrites.

    Returns the record as written, with `ts` filled in when absent, so a caller
    can echo exactly what landed rather than a hopeful copy of it.
    """
    record = dict(record)
    record.setdefault("ts", now())
    log = Path(log)
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    return record


def read_records(log: Path) -> list[dict]:
    """Every record, in order. A malformed line is skipped, never fatal.

    One corrupt line must not make a manuscript's whole comment history
    unreadable, which is what parsing the file as a single document would do.
    """
    log = Path(log)
    if not log.exists():
        return []
    out: list[dict] = []
    for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict) and rec.get("id"):
            out.append(rec)
    return out


def next_id(log: Path) -> str:
    n = sum(1 for r in read_records(log) if r.get("kind") == "comment")
    return f"c-{n + 1:04d}"


def read_chats(log: Path) -> tuple[Chat, ...]:
    """Fold the append-only log into current chat state."""
    base: dict[str, dict] = {}
    state: dict[str, str] = {}
    for rec in read_records(log):
        cid = rec["id"]
        if rec.get("kind") == "comment":
            base[cid] = rec
            state.setdefault(cid, "queued")
        elif rec.get("kind") == "state" and rec.get("state"):
            state[cid] = rec["state"]
    return tuple(
        Chat(
            id=cid,
            block=rec.get("block", ""),
            file=str(rec.get("file", "")),
            body=rec.get("body", ""),
            quote=rec.get("quote", ""),
            state=state.get(cid, "queued"),
            author=rec.get("author", "bb"),
            ts=rec.get("ts", ""),
        )
        for cid, rec in base.items()
    )


def by_block(log: Path) -> dict[str, list[dict]]:
    """The shape the viewer wants: block id -> its messages, oldest first."""
    out: dict[str, list[dict]] = {}
    for c in sorted(read_chats(log), key=lambda c: c.ts):
        out.setdefault(c.block, []).append(
            {"id": c.id, "who": c.author, "body": c.body, "ts": c.ts, "state": c.state}
        )
    return out


def pending(log: Path) -> tuple[Chat, ...]:
    """Chats awaiting work. This is what a drain reads."""
    return tuple(c for c in read_chats(log) if c.state not in TERMINAL)
