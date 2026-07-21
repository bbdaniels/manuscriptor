"""M1 — resolve \\input and \\include into one buffer, keeping a source map.

Pandoc follows neither `\\input` nor `\\include`; it drops them silently with a
zero exit code. On estonia-ecm that cost every table and the entire appendix.
So we flatten ourselves, and because a flattening pass already knows which byte
of the output came from which line of which file, the source map is free.

That map is what every downstream feature depends on: block anchors, margin
comments, and byte-range write-back all resolve through it.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Segment:
    """One contiguous run of the flattened buffer, traced to its origin file."""

    flat_start: int
    flat_end: int
    file: Path
    line_start: int
    line_end: int


@dataclass(frozen=True)
class FlatSource:
    text: str
    segments: tuple[Segment, ...]

    def locate(self, offset: int) -> tuple[Path, int]:
        """Map a byte offset in `text` back to its (file, line)."""
        raise NotImplementedError("M1")


def flatten(main_tex: Path) -> FlatSource:
    """Recursively resolve \\input and \\include starting from `main_tex`.

    Must handle: relative paths against the main file's directory, the optional
    `.tex` extension, `\\include` implying a page break, and cycles. An
    unresolvable include is left verbatim in the buffer rather than dropped, so
    a missing file is visible in the render instead of silently absent.
    """
    raise NotImplementedError("M1")
