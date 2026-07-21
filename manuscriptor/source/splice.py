"""M4 — write an edited block back into its source file.

Reconciliation is a splice, never a diff. A human edit replaces block 212's
byte range and a Claude edit replaces block 340's, and the two cannot interact.
The only genuine conflict is both writing the same block at once, which is one
lock on one block.
"""
from __future__ import annotations

from pathlib import Path


class BlockLocked(Exception):
    """Raised when a block is already being written by the other party."""


def splice(block, new_source: str) -> None:  # block: Block
    """Replace exactly `block`'s byte range in its origin file.

    Must be atomic (write a sibling temp file, then rename) so a crash mid-write
    cannot truncate a manuscript. Must verify the block's current bytes still
    hash to its id before writing, and refuse otherwise: a stale splice would
    overwrite whatever replaced it.
    """
    raise NotImplementedError("M4")


def lock(block_id: str, holder: str) -> None:
    raise NotImplementedError("M4")


def unlock(block_id: str, holder: str) -> None:
    raise NotImplementedError("M4")
