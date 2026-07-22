"""M4 — write an edited block back into its source file.

Reconciliation is a splice, never a diff. A human edit replaces block 212's
byte range and a Claude edit replaces block 340's, and the two cannot interact.
The only genuine conflict is both writing the same block at once, which is one
lock on one block.

Two guards are load bearing. The write is atomic, so a crash mid-write cannot
truncate a manuscript. And the bytes at the target range must still hash to the
block's id, because a stale splice would silently overwrite whatever had already
replaced them.

Line numbers are not trusted to find the range. A block's id is content derived
precisely so that an edit above it does not move it, and splice honours that: it
locates the block by its own source text and uses the recorded line only to pick
between identical candidates.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path

from manuscriptor.source.blocks import base_id, block_id


class BlockLocked(Exception):
    """Raised when a block is already being written by the other party."""


class StaleBlock(Exception):
    """Raised when the bytes at a block's range are no longer the block's."""


class NotEditable(Exception):
    """Raised on an attempt to hand-edit a machine-generated block."""


_LOCKS: dict[str, str] = {}
_GUARD = threading.Lock()


def splice(block, new_source: str, *, root, holder: str | None = None) -> None:
    """Replace exactly `block`'s byte range in `block.file`.

    `root` is the manuscript directory; a block claiming to live outside it is
    refused rather than followed. `holder` names the writer, and only matters
    when the block is locked.
    """
    if not block.editable:
        raise NotEditable(
            f"{block.file} is not the root manuscript file, so block {block.id} is "
            "treated as machine generated and refuses hand edits. Change the code "
            "that writes it instead; editing it here would hardcode a result."
        )

    root_dir = Path(root).resolve()
    path = Path(block.file)
    if not path.is_absolute():
        path = root_dir / path
    path = path.resolve()
    if not path.is_relative_to(root_dir):
        raise ValueError(f"{path} is outside the manuscript root {root_dir}")

    with _GUARD:
        owner = _LOCKS.get(block.id)
    if owner is not None and owner != holder:
        raise BlockLocked(f"block {block.id} is held by {owner!r}")

    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise StaleBlock(f"cannot read {path}: {exc}") from exc

    text = raw.decode("utf-8")
    crlf = "\r\n" in text
    if crlf:
        text = text.replace("\r\n", "\n")

    at = _locate(text, block)
    if at is None:
        raise StaleBlock(
            f"block {block.id} no longer matches the bytes in {path}; "
            "something else rewrote it first"
        )

    updated = text[:at] + new_source + text[at + len(block.source_text) :]
    if crlf:
        updated = updated.replace("\n", "\r\n")
    _atomic_write(path, updated.encode("utf-8"))


def lock(block_id: str, holder: str) -> None:
    with _GUARD:
        owner = _LOCKS.get(block_id)
        if owner is not None and owner != holder:
            raise BlockLocked(f"block {block_id} is held by {owner!r}")
        _LOCKS[block_id] = holder


def unlock(block_id: str, holder: str) -> None:
    with _GUARD:
        owner = _LOCKS.get(block_id)
        if owner is not None and owner != holder:
            raise BlockLocked(f"block {block_id} is held by {owner!r}, not {holder!r}")
        _LOCKS.pop(block_id, None)


def holder_of(block_id: str) -> str | None:
    with _GUARD:
        return _LOCKS.get(block_id)


# ----------------------------------------------------------------- internals


def _locate(text: str, block) -> int | None:
    """Find the block's current byte offset, or None if it is gone.

    Candidates are exact occurrences of the block's source text; each is
    confirmed by re-deriving the id from the bytes found, so nothing is ever
    written to a range that does not hash back to the block. When a manuscript
    genuinely repeats a paragraph, the candidate nearest the recorded line wins.
    """
    src = block.source_text
    if not src:
        return None
    want = base_id(block.id)

    hits: list[int] = []
    at = text.find(src)
    while at >= 0:
        if block_id(text[at : at + len(src)]) == want:
            hits.append(at)
        at = text.find(src, at + 1)
    if not hits:
        return None

    starts = [0]
    for i, c in enumerate(text):
        if c == "\n":
            starts.append(i + 1)
    idx = min(max(block.line_start - 1, 0), len(starts) - 1)
    expected = starts[idx]
    return min(hits, key=lambda h: (abs(h - expected), h))


def _atomic_write(path: Path, payload: bytes) -> None:
    """Write a sibling temp file, flush it to disk, then rename over the target.

    Same directory, so the rename is atomic on one filesystem; a crash leaves
    either the old file or the new one, never half a manuscript.
    """
    tmp = path.with_name(f".{path.name}.mxtmp{os.getpid()}")
    try:
        with open(tmp, "wb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
