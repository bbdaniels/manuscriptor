"""M2 — invoke pandoc on the flattened, anchored source.

Measured on estonia-ecm (84KB, AEA.cls): 5.5 seconds for a full render, 119KB
of HTML5. Prose, section hierarchy, footnotes, math, and figures all survive,
and citations arrive as `<span class="citation" data-cites="key">`.

Tolerable for a full build, too slow for a single-paragraph edit. The
optimization is `render_block`, below, which should come in well under a
second. Build the full path first, measure, then optimize.

A working invocation with a documentclass-swap fallback and CSL discovery
already exists at `manuscriptor/evidence/parse.py`; move it here rather than
rewriting it.
"""
from __future__ import annotations

from pathlib import Path


def render_document(flat_text: str, *, cwd: Path, bib: Path | None) -> str:
    """Full render. Falls back to `article` class when the real class fails."""
    raise NotImplementedError("M2")


def render_block(block_source: str, *, preamble: str, cwd: Path) -> str:
    """Render a single block against the document preamble, for hot patching."""
    raise NotImplementedError("M3")
