"""M2 — inject sentinel markers before pandoc, harvest them after.

Verified 2026-07-21 against pandoc: markers of the form U+27E6 MX nnnn U+27E7
survive a latex-to-html5 render into exactly the right enclosing element,
including inside footnotes, list items, and table captions. A marker placed
before a float emerges as its own orphan paragraph, which is usable as that
float's anchor.

The same technique is already used inside the absorbed cite-evidence parser to
carry citation identity through pandoc, so it is proven in this codebase.

`render/postprocess.py` must agree with `MARKER_RE` here. Note that it admits a
`-2` style suffix: `segment()` disambiguates blocks that normalize to the same
content that way, and a pattern of `[0-9a-f]+` alone would silently truncate the
id and leave `-2⟧` sitting in the rendered page.
"""
from __future__ import annotations

import re

MARKER_RE = re.compile(r"⟦MX([0-9a-f]+(?:-\d+)?)⟧")

# Only consumes whitespace when an optional argument actually follows, so
# `\item first` anchors immediately after `\item` rather than after the space.
_ITEM_RE = re.compile(r"\\item(?:[ \t]*\[[^\]]*\])?")

_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
_DECL_RE = re.compile(r"<![^>]*>")
_TAG_RE = re.compile(r"<(/?)([a-zA-Z][-\w:]*)([^>]*)>")
_NEXT_RE = re.compile(r"<|⟦")

_VOID = {
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
}


def marker(block_id: str) -> str:
    return f"⟦MX{block_id[2:] if block_id.startswith('b-') else block_id}⟧"


def inject(flat_text: str, blocks) -> str:  # blocks: tuple[Block, ...]
    """Insert one marker at the head of each block's flat range.

    Written as a single left-to-right rebuild against the *original* offsets,
    which is the same result as inserting from the end backwards and cannot
    invalidate an offset at all. Blocks need not arrive in document order.

    One exception to "head of the range": a list item anchors just after its
    `\\item`, because a marker before it would fall outside the item and pandoc
    would hang the text off the previous bullet instead.
    """
    edits = sorted(
        ((_anchor_offset(flat_text, b), marker(b.id)) for b in blocks),
        key=lambda e: e[0],
    )
    parts: list[str] = []
    prev = 0
    for at, mark in edits:
        at = max(prev, min(at, len(flat_text)))
        parts.append(flat_text[prev:at])
        parts.append(mark)
        prev = at
    parts.append(flat_text[prev:])
    return "".join(parts)


def harvest(html: str) -> tuple[str, list[str]]:
    """Move each marker onto its enclosing element as `data-mx`, then strip it.

    Returns the rewritten html and the ids of markers that could not be
    attached. A marker that survives the render but has no element to land on
    must be reported, never dropped: an unanchored block is a paragraph the
    margin cannot address, and a silent drop looks to the author like a comment
    that simply never arrived.

    Deliberately no parser dependency. A left-to-right scan keeping a stack of
    open tags is enough for pandoc's output, and it puts the marker on the
    innermost element rather than on whichever tag happens to be nearest in the
    raw text.
    """
    stack: list[tuple[str, int, int]] = []
    attach: list[tuple[int, int, str]] = []
    orphans: list[str] = []
    taken: set[int] = set()

    i = 0
    while True:
        nxt = _NEXT_RE.search(html, i)
        if nxt is None:
            break
        i = nxt.start()

        if html[i] == "⟦":
            m = MARKER_RE.match(html, i)
            if m is None:
                i += 1
                continue
            bid = "b-" + m.group(1)
            if stack and stack[-1][1] not in taken:
                taken.add(stack[-1][1])
                attach.append((stack[-1][1], stack[-1][2], bid))
            else:
                # No open element, or one already carrying another block's id.
                orphans.append(bid)
            i = m.end()
            continue

        skip = _COMMENT_RE.match(html, i) or _DECL_RE.match(html, i)
        if skip is not None:
            i = skip.end()
            continue

        tag = _TAG_RE.match(html, i)
        if tag is None:
            i += 1
            continue

        # A marker swallowed into a tag (an attribute value, say) can never be
        # attached, but it still has to be reported and stripped.
        for stray in MARKER_RE.finditer(tag.group(0)):
            orphans.append("b-" + stray.group(1))

        name = tag.group(2).lower()
        if tag.group(1) == "/":
            if any(f[0] == name for f in stack):
                while stack and stack.pop()[0] != name:
                    pass
        elif not tag.group(3).rstrip().endswith("/") and name not in _VOID:
            stack.append((name, tag.start(), tag.end()))
        i = tag.end()

    edits: list[tuple[int, int, str]] = [
        (m.start(), m.end(), "") for m in MARKER_RE.finditer(html)
    ]
    for tag_start, tag_end, bid in attach:
        at = tag_end - 1
        if at > tag_start and html[at - 1] == "/":
            at -= 1
        edits.append((at, at, f' data-mx="{bid}"'))
    edits.sort(key=lambda e: (e[0], e[1]))

    out: list[str] = []
    prev = 0
    for start, stop, text in edits:
        if start < prev:
            continue
        out.append(html[prev:start])
        out.append(text)
        prev = stop
    out.append(html[prev:])
    return "".join(out), orphans


def _anchor_offset(flat_text: str, block) -> int:  # block: Block
    if block.kind != "list_item":
        return block.flat_start
    m = _ITEM_RE.match(flat_text, block.flat_start)
    return m.end() if m is not None else block.flat_start
