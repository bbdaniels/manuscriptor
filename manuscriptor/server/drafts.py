"""Unsaved text, on disk, where the author can get at it.

A draft belongs to its block, and until 2026-07-26 it belonged to the browser as
well: the only store was localStorage. Two edits were lost that afternoon and
both had to be dug out of WebKit's sqlite by hand, once because the save path
went silent under a rename and once because the server died with the draft
unsent. Neither is a recovery an author can perform.

So drafts get a file. Two rules follow from who writes it:

**The server is the only writer, so this file may be rewritten whole.**
`comments.jsonl` is append-only precisely because Claude writes it too; nothing
else writes here, so there is no interleaving to protect against and a map is
the honest shape.

**It lives in `.manuscriptor/`, beside the log and OUTSIDE the cache.** The
manuscript directory is a git working tree the author cares about, and serving a
paper must never make `git status` grow, so the whole hidden directory ignores
itself. Not under `cache/`, though, and that placement is load-bearing: `cache/`
is what `manuscriptor clean` removes, and an unsaved paragraph is the one thing
here that no rebuild can reconstruct. The command used to delete this file.

A draft is keyed by (document, block). The document because one directory serves
several and a paragraph id could exist in both; the block because that is what
the author is typing into. Block ids are content-derived, so a save RENAMES the
block its draft belongs to: `rekey` is not an optimisation, it is what keeps a
draft findable across the save before it.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

FILENAME = "drafts.json"


# There was a `path_for(build_dir)` here, kept "for callers that already have
# the containing directory in hand". Its one caller handed it the CACHE
# directory, so `build()` read a `drafts.json` nobody writes and the payload's
# drafts were always empty. A second answer to "where does the store live" is
# what let the two ends disagree. `paths.drafts(manuscript_dir)` is the answer.


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load(path: Path | str) -> list[dict]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return []
    records = data.get("drafts") if isinstance(data, dict) else None
    if not isinstance(records, list):
        return []
    return [
        r for r in records
        if isinstance(r, dict) and isinstance(r.get("block"), str)
        and isinstance(r.get("text"), str)
    ]


def _save(path: Path | str, records: list[dict]) -> None:
    """Rewrite the file, atomically.

    The store is small and the server owns it, so a whole rewrite is right; the
    rename is what keeps a crash mid-write from leaving half a file where the
    author's unsaved paragraph used to be.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"drafts": records}, ensure_ascii=False, indent=1)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".drafts-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, p)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def imbalance(text: str) -> str:
    """Why this draft cannot be written, or "" if it can.

    The page refuses to write a block that would not parse, and a draft is
    unsaved precisely because its author stopped mid-command: `\\citep{` with no
    closing brace is the ordinary case, not the exotic one. Anything that applies
    a draft to the manuscript owes the author the same refusal, or the terminal
    becomes a way to break a paper that the editor would have caught.
    """
    opens, closes = text.count("{"), text.count("}")
    if opens == closes:
        return ""
    more, fewer = ("{", "}") if opens > closes else ("}", "{")
    return f"{abs(opens - closes)} more {more} than {fewer}"


def read(path: Path | str) -> dict[tuple[str, str], str]:
    """Every stored draft, keyed by (doc, block)."""
    return {(str(r.get("doc", "")), r["block"]): r["text"] for r in _load(path)}


def for_doc(path: Path | str, doc: str) -> dict[str, str]:
    """The drafts belonging to one document, keyed by block."""
    return {b: t for (d, b), t in read(path).items() if d == doc}


def put(path: Path | str, *, doc: str, block: str, text: str) -> None:
    """Store a draft. Empty text is a discard, not an empty paragraph."""
    if not block:
        return
    records = [r for r in _load(path) if not (r.get("doc") == doc and r["block"] == block)]
    if text:
        records.append({"doc": doc, "block": block, "text": text, "ts": _now()})
    _save(path, records)


def drop(path: Path | str, *, doc: str, block: str) -> None:
    """Forget a draft, because it saved or the author discarded it."""
    records = _load(path)
    kept = [r for r in records if not (r.get("doc") == doc and r["block"] == block)]
    if len(kept) != len(records):
        _save(path, kept)


def rekey(path: Path | str, renamed: dict[str, str]) -> None:
    """Carry drafts across the renames a rebuild reported.

    A draft under an id the current build no longer has is a draft nobody will
    ever be offered. Where the destination already holds a draft, the renamed one
    wins: it is the newer text, since the rename is what the save just did.
    """
    if not renamed:
        return
    records = _load(path)
    moved = {k: v for k, v in renamed.items() if v and k != v}
    if not any(r["block"] in moved for r in records):
        return

    carried = [dict(r, block=moved[r["block"]]) for r in records if r["block"] in moved]
    others = [r for r in records if r["block"] not in moved]
    landed = {(r.get("doc", ""), r["block"]) for r in carried}
    kept = [r for r in others if (r.get("doc", ""), r["block"]) not in landed]
    _save(path, kept + carried)
