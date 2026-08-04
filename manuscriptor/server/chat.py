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
     "body":"you say this twice, tighten","author":"Ben","ts":"..."}
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

# `review` is a finding from a check (preflight, proofread), pinned and
# readable at once but never presented to the drain as work: a --with-agent
# session must not start working the instructions it just wrote itself. The
# author triages it into real work (a new comment) or dismisses it (done).
State = Literal["queued", "working", "done", "orphaned", "review"]

TERMINAL = {"done", "orphaned"}

# The author's display name: what a bubble he wrote is labelled with on the
# page, and what lands in the `author` field of every record the server writes
# on his behalf. It lives here, once, because `comments.jsonl` is durable,
# git-tracked and read by coauthors, so this string is part of a shared record
# rather than a local preference -- which is also why it is a constant and not
# read from git's `user.name`. A derived name would differ on a machine with
# different config and would change silently when config did, writing two names
# into one append-only file that can never be reconciled; `drain.comment` also
# dedupes findings on `author`, so a name that drifts would quietly re-raise
# every open finding as new. Records already written keep whatever name they
# carry: this renames nobody's past, only what is appended next.
AUTHOR = "Ben"


@dataclass(frozen=True)
class Chat:
    """One block's conversation: a comment plus every state record that followed."""

    id: str
    block: str
    file: str
    body: str
    quote: str
    state: State
    author: str = AUTHOR
    ts: str = ""
    # The document the comment was left on, when the directory holds several
    # (the paper, the appendix, a response to reviewers). "" is a record from
    # before documents existed, which belongs to whichever document is being
    # read: exact for the single-document manuscripts that wrote those records.
    doc: str = ""
    # The skill a check request names, and the check a finding came from.
    check: str = ""


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


def read_chats(log: Path, *, doc: str | None = None) -> tuple[Chat, ...]:
    """Fold the append-only log into current chat state.

    With `doc`, only the chats belonging to that document: the log is shared
    by every document in the directory, and presenting the appendix's comments
    while the paper is being served or drained would queue work against
    paragraphs the page does not have. A chat with no recorded document
    belongs to whichever one is being read (see `Chat.doc`).
    """
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
            # A record from before the field existed is the author's own; an
            # older record that names him is left saying exactly what it says.
            author=rec.get("author", AUTHOR),
            ts=rec.get("ts", ""),
            doc=str(rec.get("doc", "")),
            check=str(rec.get("check", "")),
        )
        for cid, rec in base.items()
        if doc is None or rec.get("doc", "") in ("", doc)
    )


def by_block(log: Path, *, doc: str | None = None) -> dict[str, list[dict]]:
    """The shape the viewer wants: block id -> its messages, oldest first.

    A `reply` record is the agent answering in words rather than only in
    states. It shares its comment's id in the log (that is the join), so each
    reply message gets a synthetic id of its own: the page dedupes messages by
    id, and two messages sharing one would collapse into one. A reply inherits
    its comment's scope; it carries no doc of its own.
    """
    replies: dict[str, list[dict]] = {}
    for rec in read_records(log):
        if rec.get("kind") == "reply" and rec.get("body"):
            replies.setdefault(rec["id"], []).append(rec)

    out: dict[str, list[dict]] = {}
    for c in sorted(read_chats(log, doc=doc), key=lambda c: c.ts):
        msgs = out.setdefault(c.block, [])
        msgs.append(
            {"id": c.id, "who": c.author, "body": c.body, "ts": c.ts, "state": c.state}
        )
        for n, rec in enumerate(replies.get(c.id, []), start=1):
            msgs.append({
                "id": f"{c.id}#r{n}",
                "who": rec.get("author", "claude"),
                "body": rec.get("body", ""),
                "ts": rec.get("ts", ""),
                "state": None,
            })
    return out


def pending(log: Path, *, doc: str | None = None) -> tuple[Chat, ...]:
    """Chats awaiting work. This is what a drain reads.

    `review` findings are excluded: they are for the author, and a drain that
    worked them would be an agent acting on its own review.
    """
    return tuple(c for c in read_chats(log, doc=doc)
                 if c.state not in TERMINAL and c.state != "review")
