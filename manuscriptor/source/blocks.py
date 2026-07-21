"""M1 — cut the flattened buffer into addressable blocks with stable ids.

The block is the unit of work. A worker may read as widely as it needs but may
only ever write one block, which is the property that makes running edits live
acceptable at all.

Block identity is derived from content, never from position. On re-parse we
match exact id first, then nearest-neighbour similarity within the same file,
then position. This is what stops anchor drift from being a problem: a comment
follows its paragraph through edits above it rather than trusting a line number
that moves.

In practice a block is usually a paragraph. In these manuscripts `main.tex` is
written one paragraph per line, so a paragraph anchor is effectively a line
number and an edit to one is a single-line replacement.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

Kind = Literal[
    "paragraph",
    "heading",
    "table",
    "figure",
    "equation",
    "caption",
    "footnote",
    "list_item",
    "generated",
]


@dataclass(frozen=True)
class Block:
    id: str
    kind: Kind
    file: Path
    line_start: int
    line_end: int
    source_text: str
    parent_heading: str | None
    generated_by: Path | None  # phase 2: the script that writes this block's file


def segment(flat) -> tuple[Block, ...]:  # flat: FlatSource
    """Cut a FlatSource into blocks in document order."""
    raise NotImplementedError("M1")


def block_id(source_text: str) -> str:
    """Content-derived stable id. Normalizes whitespace before hashing."""
    raise NotImplementedError("M1")


def rematch(old: tuple[Block, ...], new: tuple[Block, ...]) -> dict[str, str | None]:
    """Map old block ids to new ones after a source edit.

    Exact id, then nearest-neighbour similarity within the same file, then
    position. Returns None for a block that genuinely disappeared, so callers
    can mark its comments orphaned rather than silently reattaching them to the
    wrong paragraph.
    """
    raise NotImplementedError("M1")
