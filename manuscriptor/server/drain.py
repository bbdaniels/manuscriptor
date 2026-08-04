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
from manuscriptor.server import paths

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
    check: str = ""

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
    # Scoped to the document being drained: the log is shared by every
    # document in the directory, and working the appendix's comments against
    # the paper's blocks would re-anchor them onto the wrong paragraphs.
    doc = build_mod.find_main_tex(manuscript_dir, main).name
    waiting = chat.pending(paths.comments(manuscript_dir), doc=doc)
    if not waiting:
        return []

    b = build_mod.build(manuscript_dir, main=main, bib=bib)
    order = {blk.id: i for i, blk in enumerate(b.blocks)}
    records = b.blob["blocks"]

    items: list[Item] = []
    for c in sorted(waiting, key=lambda c: c.ts):
        # A comment with no block is about the document, not a paragraph. It
        # is orchestration work: decompose it into per-block tasks, each of
        # which still writes exactly one block. The constraint the design
        # rests on is per write, not per comment.
        if not c.block:
            note = ("document-level: no single block to edit. Decompose into "
                    "per-block subagent tasks; every write is still one block. "
                    "Answer in words with `manuscriptor reply` when the right "
                    "response is an answer rather than an edit.")
            if c.check:
                note = (f"a check: invoke the `{c.check}` skill on this manuscript. "
                        "Land each finding as a review comment with `manuscriptor "
                        "comment --review`, quote the exact sentence it concerns so "
                        "it anchors, then reply with a summary and mark this done.")
            items.append(
                Item(
                    chat_id=c.id, block_id="", body=c.body, state=c.state,
                    file="", line_start=0, line_end=0, editable=False,
                    source="", section=None, note=note, check=c.check,
                )
            )
            continue
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
                section=rec["label"],
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
    if state not in ("queued", "working", "done", "orphaned", "review"):
        raise ValueError(f"unknown state {state!r}")
    rec = {"id": chat_id, "kind": "state", "state": state}
    if edit:
        rec["edit"] = edit
    return chat.append(paths.comments(manuscript_dir), rec)


def comment(manuscript_dir: Path, *, body: str, quote: str = "",
            author: str = chat.AUTHOR,
            doc: str = "", check: str = "", block: str = "",
            review: bool = False) -> dict | None:
    """Append a comment from outside the page: a check's finding, usually.

    The quote is what anchors it: the server's re-anchoring machinery places
    it on the paragraph containing that text, the same way an imported referee
    comment or a drifted chat is placed. `review=True` files it as a finding,
    which is pinned and readable at once but never drained as work.

    Deduped against OPEN comments with the same quote, author and doc, so
    running a check twice does not raise every finding twice. A dismissed
    finding re-found by a later run is a new comment, deliberately: the check
    is telling the author it still thinks so.
    """
    log = paths.comments(manuscript_dir)
    if quote:
        for c in chat.read_chats(log):
            if (c.quote == quote and c.author == author
                    and c.doc == doc and c.state not in chat.TERMINAL):
                return None
    rec = chat.append(log, {
        "id": chat.next_id(log), "kind": "comment", "block": block, "doc": doc,
        "quote": quote, "body": str(body), "author": author,
        **({"check": check} if check else {}),
    })
    if review:
        chat.append(log, {"id": rec["id"], "kind": "state", "state": "review"})
    return rec


def reply(manuscript_dir: Path, chat_id: str, body: str, *, author: str = "claude") -> dict:
    """Answer a comment in words, into the same chat.

    States say that something happened; a reply says what, or why not. It is
    how the agent declines ("this needs the producing script, not the prose"),
    reports a decision, or answers a question the comment asked. Same log,
    same append-only discipline, joined to its comment by id.
    """
    if not str(body).strip():
        raise ValueError("an empty reply says nothing; use a state for that")
    rec = {"id": chat_id, "kind": "reply", "body": str(body), "author": author}
    return chat.append(paths.comments(manuscript_dir), rec)


def wait(manuscript_dir: Path, *, timeout: float | None = None) -> bool:
    """Block until there is work. True when there is, False on timeout.

    Meant to run inside a backgrounded Claude Code job: the process exiting is
    the wake signal, so the session is re-invoked with the new comment already
    on disk.

    A NON-EMPTY QUEUE RETURNS IMMEDIATELY, before any watching. The question
    the park asks is "is there work", not "did the file grow": a comment that
    landed while the session was working sits inside the size baseline, and a
    park that watched only for growth slept through it until some third record
    happened along. Found by the persistent session's own control-flow test.
    """
    import time

    from manuscriptor.server.watch import block_until_log_grows

    # The log lives inside the hidden directory now, and a park may be the
    # first thing that ever runs against a manuscript: `proc --wait` on a paper
    # nobody has served yet has no directory to watch, and the watcher raises
    # on the missing parent rather than waiting.
    paths.ensure(manuscript_dir)
    log = paths.comments(manuscript_dir)
    try:
        doc = build_mod.find_main_tex(Path(manuscript_dir).resolve()).name
    except (LookupError, FileNotFoundError):
        doc = None
    if chat.pending(log, doc=doc):
        return True
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
        f"{len(items)} pending comment{'s' if len(items) != 1 else ''}, oldest first,",
        "which is the order to work them: the author is watching a counter, and",
        "answering the newest leaves the one he has waited longest for until last.",
        "",
        "Read as widely as you need. Write ONE block per comment, the one named",
        "under EDIT. Say you have started before you start, or the edit appears",
        "under him with no warning:",
        "",
        "    manuscriptor state <dir> <chat-id> working",
        "    manuscriptor state <dir> <chat-id> done",
        "",
        "A comment can be answered in words as well as with an edit:",
        "",
        "    manuscriptor reply <dir> <chat-id> \"what happened, or why not\"",
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
    flat = build_mod.flatten_ws(text)
    return flat[:n] + ("…" if len(flat) > n else "")


def _indent(text: str) -> str:
    return "\n".join("  | " + line for line in text.splitlines())


def _locate(c, b, records) -> tuple[str | None, str | None]:
    """Find the block a comment belongs to, even after its id changed.

    Placement goes through `build_mod.match_by_quote`, which is the single rule
    the page's re-anchoring uses too. Do not reimplement it here: it was written
    twice once already, and the copies disagreed.
    """
    if c.block in records:
        return c.block, None
    match = build_mod.match_by_quote(c.quote, b.blocks, file=c.file or "")
    if match:
        return match, "re-anchored by quote match; check it landed on the right paragraph"
    return None, None
