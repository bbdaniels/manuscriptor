"""M2 — inject sentinel markers before pandoc, harvest them after.

Verified 2026-07-21 against pandoc: markers of the form U+27E6 MX nnnn U+27E7
survive a latex-to-html5 render into exactly the right enclosing element,
including inside footnotes, list items, and table captions. A marker placed
before a float emerges as its own orphan paragraph, which is usable as that
float's anchor.

The same technique is already used inside the absorbed cite-evidence parser to
carry citation identity through pandoc, so it is proven in this codebase.
"""
from __future__ import annotations

import re

MARKER_RE = re.compile(r"⟦MX([0-9a-f]+)⟧")


def marker(block_id: str) -> str:
    return f"⟦MX{block_id}⟧"


def inject(flat_text: str, blocks) -> str:  # blocks: tuple[Block, ...]
    """Insert one marker at the head of each block's source range."""
    raise NotImplementedError("M2")


def harvest(html: str) -> str:
    """Move each marker onto its enclosing element as `data-mx`, then strip it.

    A marker that survives into the output but cannot be attached to an element
    must be reported, not silently dropped: an unanchored block is a paragraph
    the margin cannot address.
    """
    raise NotImplementedError("M2")
