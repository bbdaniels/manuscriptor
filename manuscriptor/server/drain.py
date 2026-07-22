"""M5 — the drain: handing pending chats to a Claude Code session.

Nothing in this module calls a model, and that is the point. The server has zero
knowledge of Claude and Claude never talks to the server; they share a
filesystem and communicate through the `.tex` tree and `comments.jsonl`. So the
drain is not a worker. It is a way of *presenting* what is pending, in enough
context that a Claude Code session sharing the directory can act, and a way of
recording that it did.

Two modes, because the loop has two triggers.

`pending()` is the on-demand read, behind `manuscriptor proc`. It is what runs
when the author says "process the comments".

`wait()` blocks until the log grows and then exits, behind
`manuscriptor proc --wait`. Run as a backgrounded Claude Code job, the process
exiting is what wakes the session, so the drain fires on a comment hitting disk
rather than on anyone asking. No polling in the server, one blocked process.

CONTEXT WIDE, UNIT NARROW. Each item carries the whole section around the block
so the worker can read as widely as it needs, and names exactly one block as the
thing it may write. A worker that can read everything and change one paragraph
cannot silently wreck a paper, and that constraint is what makes running this
live acceptable at all.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from manuscriptor.server import build as build_mod
from manuscriptor.server import chat

NEIGHBOURS = 2


@dataclass
class Item:
    """One pending chat, with everything needed to answer it."""

    chat_id: str
    block_id: str
    body: str
    state: str
    file: str
    line_start: int
    line_end: int
    editable: bool
    source: str
    section: str | None
    before: list[str] = field(default_factory=list)
    after: list[str] = field(default_factory=list)
    cites: list[str] = field(default_factory=list)
    values: list[dict] = field(default_factory=list)
    note: str | None = None

    def as_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items()}


def collect(manuscript_dir: Path, *, main: str | None = None, bib: str | None = None) -> list[Item]:
    """Every chat awaiting work, resolved against the manuscript as it is now.

    Comments are keyed to a block id, and ids are content derived, so a comment
    written before an edit points at an id that no longer exists. Rather than
    dropping those, the block is re-found by its recorded quote, which is what
    the anchoring design promised. A comment that cannot be placed is returned
    with a note saying so, never silently discarded.
    """
    manuscript_dir = Path(manuscript_dir).resolve()
    waiting = chat.pending(manuscript_dir / "comments.jsonl")
    if not waiting:
        return []

    b = build_mod.build(manuscript_dir, main=main, bib=bib)
    order = {blk.id: i for i, blk in enumerate(b.blocks)}
    records = b.blob["blocks"]

    items: list[Item] = []
    for c in sorted(waiting, key=lambda c: c.ts):
        bid, note = _locate(c, b, records)
        if bid is None:
            items.append(
                Item(
                    chat_id=c.id, block_id=c.block, body=c.body, state=c.state,
                    file=c.file, line_start=0, line_end=0, editable=False,
                    source="", section=None,
                    note="the block this was written on is gone; re-anchor it by hand or close it",
                )
            )
            continue

        rec = records[bid]
        i = order[bid]
        items.append(
            Item(
                chat_id=c.id,
                block_id=bid,
                body=c.body,
                state=c.state,
                file=rec["file"],
                line_start=rec["line_start"],
                line_end=rec["line_end"],
                editable=rec["editable"],
                source=rec["source"],
                section=rec["parent_heading"],
                before=[b.blocks[j].source_text for j in range(max(0, i - NEIGHBOURS), i)],
                after=[b.blocks[j].source_text for j in range(i + 1, min(len(b.blocks), i + 1 + NEIGHBOURS))],
                cites=rec["cites"],
                values=rec["values"],
                note=note,
            )
        )
    return items


def mark(manuscript_dir: Path, chat_id: str, state: str, *, edit: dict | None = None) -> dict:
    """Record what happened to a chat. A new record, never a rewrite."""
    if state not in ("queued", "working", "done", "orphaned"):
        raise ValueError(f"unknown state {state!r}")
    rec = {"id": chat_id, "kind": "state", "state": state}
    if edit:
        rec["edit"] = edit
    return chat.append(Path(manuscript_dir).resolve() / "comments.jsonl", rec)


def wait(manuscript_dir: Path, *, timeout: float | None = None) -> bool:
    """Block until the comment log grows. True when it did, False on timeout.

    Meant to run inside a backgrounded Claude Code job: the process exiting is
    the wake signal, so the session is re-invoked with the new comment already
    on disk.
    """
    import time

    from manuscriptor.server.watch import block_until_log_grows

    log = Path(manuscript_dir).resolve() / "comments.jsonl"
    start = log.stat().st_size if log.exists() else 0
    if timeout is None:
        block_until_log_grows(log, from_offset=start)
        return True

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        size = log.stat().st_size if log.exists() else 0
        if size > start:
            return True
        time.sleep(0.25)
    return False


# ----------------------------------------------------------------- rendering


def as_json(items: list[Item]) -> str:
    return json.dumps([i.as_dict() for i in items], indent=2, ensure_ascii=False)


def as_text(items: list[Item]) -> str:
    """The briefing a Claude Code session reads.

    Deliberately says what may be written and what may only be read, on every
    item, rather than assuming the reader remembers.
    """
    if not items:
        return "No pending comments."

    out: list[str] = [
        f"{len(items)} pending comment{'s' if len(items) != 1 else ''}.",
        "",
        "Read as widely as you need. Write ONE block per comment, the one named",
        "under EDIT. When done, run: manuscriptor state <dir> <chat-id> done",
        "",
    ]
    for it in items:
        out.append("=" * 72)
        out.append(f"{it.chat_id}  [{it.state}]")
        out.append(f"  COMMENT  {it.body}")
        if it.note:
            out.append(f"  NOTE     {it.note}")
        if not it.source:
            out.append("")
            continue
        where = f"{it.file}:{it.line_start}-{it.line_end}"
        out.append(f"  EDIT     {where}" if it.editable else f"  READ ONLY {where}")
        if not it.editable:
            out.append("           generated by analysis code; change the script, not this file")
        if it.section:
            out.append(f"  SECTION  {it.section}")
        if it.cites:
            out.append(f"  CITES    {', '.join(it.cites)}")
        for v in it.values:
            out.append(f"  VALUE    {v['key']} <- {v['producer'] or 'unknown producer'}")
        if it.before:
            out.append("  BEFORE   " + _clip(it.before[-1]))
        out.append("")
        out.append(_indent(it.source))
        out.append("")
        if it.after:
            out.append("  AFTER    " + _clip(it.after[0]))
        out.append("")
    return "\n".join(out)


def _clip(text: str, n: int = 100) -> str:
    flat = " ".join(text.split())
    return flat[:n] + ("…" if len(flat) > n else "")


def _indent(text: str) -> str:
    return "\n".join("  | " + line for line in text.splitlines())


def _locate(c, b, records) -> tuple[str | None, str | None]:
    """Find the block a comment belongs to, even after its id changed."""
    if c.block in records:
        return c.block, None
    if c.quote:
        for blk in b.blocks:
            if blk.source_text.startswith(c.quote[:60]):
                return blk.id, "re-anchored: the block was edited after this comment was written"
        for blk in b.blocks:
            if c.quote[:40] and c.quote[:40] in blk.source_text:
                return blk.id, "re-anchored by quote match; check it landed on the right paragraph"
    return None, None
