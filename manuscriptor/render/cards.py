r"""What belongs to an exhibit card, decided here and nowhere else.

An exhibit's notes define the stars, name the clustering and give the sample.
In the compiled PDF they are inside the float, in small type, under the table.
On the page they were outside it, at body size, and unclickable: pandoc returns
a `\begin{table}` float as one element and its trailing `\footnotesize`
paragraph as the NEXT one, and the block's `data-mx` rides on the first. So the
notes sat outside the element carrying the block id, and a click on them
resolved to no block at all -- for bytes that are unambiguously in the block.

THE BLOCK DECIDES MEMBERSHIP. That is the entire rule, and it is a CARRY rather
than a guess. The author put the exhibit and its notes in one block;
`server/blocks.py` computed that extent from the source bytes and
`render/anchors.py` wrote it into the HTML as the markers this pass reads back.
Nothing here asks what a paragraph SAYS. It must not: the seven manuscripts in
the corpus spell the label `Notes:`, `Note:`, `\underline{\textbf{Notes}}:`, and
nothing at all, and dsp-bias and qutub-india put the same content inside
`\caption{}` instead. A match on the word would find some of them, miss more,
and claim a body paragraph the first time an author began a sentence with
"Notes on the sample follow."

This is the same shape as `render/tables.mark_header_rows` and the opposite of
its hazard. There, the truth is only in the LaTeX -- the rules say which rows
are headers and the rules are gone by the time pandoc is done -- so the marking
has to happen before pandoc and `postprocess` may only carry it. Here the truth
survives, because block extent IS the marker, so no LaTeX-side marking is needed
at all. That difference is worth stating because it is what keeps the Word
submission path out of this: `server/compile.py` hands
`normalize_for_pandoc`'s output to pandoc and on into the `.docx`, so anything
minted in the LaTeX ships as a literal glyph in a journal submission. This pass
mints nothing there.

THE BLOCK SCOPE ALSO DRAWS THE OTHER LINE, and drawing it is the point rather
than a limitation. covet-india and sdi-caseloads write no floats at all: an
exhibit is a `\subsection*` heading, an `\input`, and a note paragraph after a
blank line -- three constructs, and the note is its OWN block with its own
anchor. Folding it into the table's card would put two block ids on one element,
which is the one thing `harvest` refuses, because two blocks fighting over one
click is worse than a note at body size. Thirty-four exhibits across those two
manuscripts are left exactly as they are, and that falls out of the rule instead
of being an exception written into it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# The class the stylesheet subordinates, and the wrapper minted when an exhibit
# has no card of its own to fold into.
NOTES_CLASS = "ms-notes"
CARD_CLASS = "ms-exhibit"
# The exhibit's NAME, which every card carries and both kinds style alike.
TITLE_CLASS = "ms-title"

# Elements that ARE the exhibit. `table-scroll` is the container
# `postprocess._wrap_tables` gives an uncaptioned table, and it is an exhibit
# rather than a wrapper: a run of notes after it belongs to it.
_EXHIBIT_TAGS = {"table", "figure"}

_TAG_RE = re.compile(r"<(/?)([a-zA-Z][-\w]*)((?:[^>\"']|\"[^\"]*\"|'[^']*')*?)(/?)>")
_VOID = {"img", "br", "hr", "embed", "input", "meta", "link", "source", "col",
         "area", "base", "param", "track", "wbr"}
_MX_RE = re.compile(r'\sdata-mx="[^"]*"')
_TEXT_RE = re.compile(r"<[^>]*>")


@dataclass
class _Node:
    tag: str
    attrs: str
    start: int          # first character of the open tag
    inner: int          # one past the open tag
    close: int          # first character of the close tag
    end: int            # one past the close tag
    children: list = field(default_factory=list)


def _parse(html: str) -> list[_Node]:
    """The element tree, by depth counting over tags.

    A parser rather than a regex because the shapes that matter are nested:
    estonia-ecm's Figure 2 is a `<figure>` of four `<figure>`s with the notes
    among them, and `postprocess._wrap_tables` has already put a `<table>`
    inside a `<div>` inside a `<figure>`. A regex reaching for "the paragraph
    after the figure" finds the wrong `</figure>` on the first of those.
    """
    roots: list[_Node] = []
    stack: list[_Node] = []
    for m in _TAG_RE.finditer(html):
        closing, name, attrs, selfclosing = m.group(1), m.group(2).lower(), m.group(3), m.group(4)
        if name in _VOID or selfclosing:
            continue
        if not closing:
            node = _Node(name, attrs, m.start(), m.end(), m.end(), m.end())
            (stack[-1].children if stack else roots).append(node)
            stack.append(node)
            continue
        # An unmatched close tag closes the nearest open element of that name,
        # and is ignored outright if there is none. Pandoc does not emit
        # malformed HTML, but a render that dies on one would take the page
        # with it, and there is nothing here worth that.
        for depth in range(len(stack) - 1, -1, -1):
            if stack[depth].tag == name:
                node = stack[depth]
                node.close, node.end = m.start(), m.end()
                del stack[depth:]
                break
    return roots


def _block_id(node: _Node) -> str | None:
    m = re.search(r'data-mx="([^"]*)"', node.attrs)
    return m.group(1) if m else None


def _has_text(html: str, node: _Node) -> bool:
    return bool(_TEXT_RE.sub("", html[node.inner: node.close]).strip())


def _is_exhibit(node: _Node) -> bool:
    return node.tag in _EXHIBIT_TAGS or "table-scroll" in node.attrs


# Elements pandoc mints AROUND an exhibit rather than as one. A `\label` inside
# a table float comes back as `<div id="tab:main">` wrapping the table on pandoc
# 3.1.1 and as an `id` on the `<table>` itself on 3.10.1 -- the same manuscript,
# the same source bytes, two element trees.
_WRAPPER_TAGS = {"div", "center", "section"}


def _own_text(html: str, node: _Node) -> bool:
    """Text belonging to this element rather than to a descendant."""
    at = node.inner
    for child in node.children:
        if _TEXT_RE.sub("", html[at: child.start]).strip():
            return True
        at = child.end
    return bool(_TEXT_RE.sub("", html[at: node.close]).strip())


def _exhibit_within(html: str, node: _Node) -> _Node | None:
    """The exhibit this root node PRESENTS, itself or through a wrapper.

    A run of notes follows the thing the reader sees, and what the reader sees
    is the exhibit however many elements pandoc wrapped around it. This descent
    is why the fold does not depend on a pandoc version: the author's server
    resolves `pandoc` to 3.1.1, which wraps a LABELLED table float in a
    `<div id="tab:main">`, so the block's `data-mx` lands on the div and the
    exhibit is its grandchild while the notes are the DIV's next sibling.
    Matching the exhibit at root level alone found neither, and qutub-ayush's
    `tab:main` -- the case the fold was written for -- stayed outside the card
    on the author's own machine while the corpus check, run against 3.10.1,
    reported it folded.

    Conservative on purpose: only a wrapper holding the exhibit and nothing
    else. A `div` with prose of its own beside a table is a layout the author
    wrote, and reaching inside it to move a paragraph would be rewriting his
    page on a guess.
    """
    if _is_exhibit(node):
        return node
    if node.tag not in _WRAPPER_TAGS or _own_text(html, node):
        return None
    kids = [c for c in node.children if _has_text(html, c) or _is_exhibit(c)]
    if len(kids) != 1:
        return None
    return _exhibit_within(html, kids[0])


def _is_notes(html: str, node: _Node) -> bool:
    """A loose paragraph with words in it.

    The text test is what keeps a subfigure out. estonia-ecm's panels come back
    as `<p><img></p>` and its panel spacing as `<p><br></p>`: paragraphs by tag,
    exhibit and whitespace by content. Neither has text, and a card whose notes
    were its own images would be a fold that ate the exhibit.
    """
    return (node.tag == "p" and _has_text(html, node)
            and NOTES_CLASS not in node.attrs)


def _classed(html: str, node: _Node) -> str:
    """The node's own source, with the notes class added to its open tag."""
    open_tag = html[node.start: node.inner]
    if 'class="' in open_tag:
        return (open_tag.replace('class="', f'class="{NOTES_CLASS} ', 1)
                + html[node.inner: node.end])
    return (open_tag[:-1].rstrip() + f' class="{NOTES_CLASS}">'
            + html[node.inner: node.end])


def shape_cards(html: str) -> tuple[str, int, int]:
    """One card anatomy for every exhibit: a title line, then notes.

    The two halves in the order they have to run. The fold decides what is IN
    the card; the split decides what inside it is the exhibit's NAME and what is
    its notes. Splitting first would leave a caption-borne note outside a card
    that had not been drawn yet.
    """
    html, folded = fold_exhibit_notes(html)
    html, split = split_caption_titles(html)
    return html, folded, split


def fold_exhibit_notes(html: str) -> tuple[str, int]:
    """Put every exhibit's notes inside the exhibit's card. Returns the count.

    Two shapes, one rule, because pandoc puts the notes in two places and which
    one it picks is not the author's doing:

    * A `figure` float keeps them INSIDE the `<figure>` it emits -- but above
      the `<figcaption>`, so the notes read before the caption they annotate.
      Those are classed and moved below it.
    * A `table` float, and every non-float exhibit, leaves them OUTSIDE as the
      next sibling. Those are moved into the card, and if the exhibit has no
      card -- an uncaptioned table is only a scroll container -- one is minted
      and the block id moves onto it, so the whole exhibit stays one click.

    Idempotent, because the render pipeline is not the only caller and a second
    pass over folded output must not nest a card inside a card.
    """
    roots = _parse(html)
    edits: list[tuple[int, int, str]] = []
    folded = _fold_containers(html, roots, edits)
    folded += _fold_siblings(html, roots, edits)
    if not edits:
        return html, 0
    out = html
    for start, end, text in sorted(edits, key=lambda e: e[0], reverse=True):
        out = out[:start] + text + out[end:]
    return out, folded


def _fold_containers(html: str, nodes: list[_Node], edits: list) -> int:
    """The notes pandoc already nested: class them, and put them last."""
    folded = 0
    for node in nodes:
        folded += _fold_containers(html, node.children, edits)
        # A `<figure>` is an exhibit whatever it holds -- pandoc emits one only
        # for a float, and estonia-ecm's Figure 2 holds its four panels as
        # `<p><img></p>`, so a search for a `<table>` or a nested `<figure>`
        # child finds nothing in the one shape this half exists for.
        if node.tag != "figure" and not any(_is_exhibit(c) for c in node.children):
            continue
        notes = [c for c in node.children if _is_notes(html, c)]
        if not notes:
            continue
        # Only what stands between the exhibit and the end of the container. A
        # paragraph ABOVE the exhibit inside its own float is not a note in any
        # manuscript in the corpus, and moving one below the caption would be
        # reordering the author's page on a guess.
        body = [i for i, c in enumerate(node.children)
                if _is_exhibit(c) or (c.tag == "p" and not _has_text(html, c))]
        first = min(body) if body else -1
        notes = [c for c in notes if node.children.index(c) > first]
        if not notes:
            continue
        moved = "".join(_classed(html, c) for c in notes)
        for c in notes:
            edits.append((c.start, c.end, ""))
        edits.append((node.close, node.close, moved))
        folded += 1
    return folded


def _fold_siblings(html: str, roots: list[_Node], edits: list) -> int:
    """The notes pandoc left outside: move them into the block's own card.

    The run is bounded at both ends by things the source already decided. It
    starts at the last exhibit owned by a block, and it ends at the first
    element that is not a loose paragraph -- the next block's `data-mx`, a
    heading, or the footnote `<section>` that closes every document.
    """
    folded = 0
    owner: str | None = None
    exhibit: _Node | None = None
    run: list[_Node] = []

    def flush() -> int:
        nonlocal exhibit, run
        if exhibit is None or not run:
            exhibit, run = None, []
            return 0
        moved = "".join(_classed(html, c) for c in run)
        for c in run:
            edits.append((c.start, c.end, ""))
        if exhibit.tag == "figure":
            # It already is a card. The notes go inside it, under the caption.
            edits.append((exhibit.close, exhibit.close, moved))
        else:
            # An uncaptioned table, or a bare one. Mint the card and MOVE the
            # block id onto it: leaving the id on the inner element would put
            # the notes back outside the thing a click resolves to, which is
            # the bug this pass exists for.
            bid = _block_id(exhibit)
            attrs = f' data-mx="{bid}"' if bid else ""
            inner = html[exhibit.start: exhibit.end]
            if bid:
                inner = _MX_RE.sub("", inner, count=1)
            edits.append((exhibit.start, exhibit.end,
                          f'<figure class="{CARD_CLASS}"{attrs}>{inner}{moved}</figure>'))
        exhibit, run = None, []
        return 1

    for node in roots:
        bid = _block_id(node)
        if bid is not None and bid != owner:
            folded += flush()
            owner = bid
        found = _exhibit_within(html, node)
        if found is not None:
            folded += flush()
            exhibit = found
            continue
        if exhibit is not None and _is_notes(html, node) and bid is None:
            run.append(node)
            continue
        folded += flush()
    folded += flush()
    return folded


# --------------------------------------------------------------- the title line

# Words that end in a period without ending a sentence. A caption reading
# "Panel A. vs. Panel B. Shares are unweighted." has its first boundary at the
# THIRD period, and a split at the second would name the exhibit "Panel A. vs."
_ABBREVIATIONS = {
    "e.g", "i.e", "cf", "vs", "al", "fig", "figs", "tab", "tabs", "no", "nos",
    "dr", "prof", "mr", "mrs", "ms", "st", "approx", "ca", "eq", "eqs", "sec",
    "secs", "pp", "ref", "refs", "ed", "eds", "vol", "col", "cols", "resp",
    "incl", "est", "min", "max", "s.d", "s.e", "u.s", "u.k",
}
_SENTENCE_PUNCT = ".?!"
_CLOSERS = "'’\"”)]}"
_WORD_RE = re.compile(r"[\w.'’-]+$")


def split_caption_titles(html: str) -> tuple[str, int]:
    r"""Give every caption one anatomy: a title line, then notes-styled prose.

    ORCHESTRATOR-APPROVED DESIGN DECISION, DELIBERATELY EASY TO REVISIT, AND THE
    AUTHOR HAS NOT SEEN THIS SPECIFIC RULE. A table's notes are their own
    paragraph in the source, so the fold above is enough for them. A figure's
    are not: across qutub-ayush, qutub-india, estonia-qbs and dsp-bias the whole
    note lives INSIDE `\caption{}`, hundreds of words of it, so the card read as
    a picture over an undifferentiated wall of body-size serif while the table
    beside it had a small muted caption and a separate notes block. Same
    manuscript, two anatomies.

    THE RULE: the caption's FIRST SENTENCE is the exhibit's title; everything
    after it is notes. It matches the author's house style -- "Component
    Outcomes by Trial Arm and Round. Share of TB standardized patient
    interactions ..." -- and it is one function, so revisiting it is editing
    `_caption_split` and nothing else. A single-sentence caption stays a title
    with no notes block, which is every caption in a manuscript that keeps its
    notes outside the caption.

    NOTHING IS DROPPED AND NOTHING IS REWORDED. The split moves a byte range
    from one element into another; the words on the page are the caption's own,
    in the caption's own order.

    The title carries `ms-title` whether or not it split, because the point is
    that a figure and a table are styled by the same two rules. Returns the
    number of captions that split.
    """
    roots = _parse(html)
    edits: list[tuple[int, int, str]] = []
    split = 0
    for node in _walk(roots):
        if node.tag != "figcaption":
            continue
        if TITLE_CLASS not in node.attrs:
            edits.append((node.start, node.inner, _with_class(html, node, TITLE_CLASS)))
        inner = html[node.inner: node.close]
        at = _caption_split(inner)
        if at is None:
            continue
        edits.append((node.inner, node.close, inner[:at].rstrip()))
        edits.append((node.end, node.end,
                      f'<p class="{NOTES_CLASS}">{inner[at:].strip()}</p>'))
        split += 1
    if not edits:
        return html, 0
    out = html
    for start, end, text in sorted(edits, key=lambda e: e[0], reverse=True):
        out = out[:start] + text + out[end:]
    return out, split


def _walk(nodes: list[_Node]):
    for node in nodes:
        yield node
        yield from _walk(node.children)


def _with_class(html: str, node: _Node, cls: str) -> str:
    open_tag = html[node.start: node.inner]
    if 'class="' in open_tag:
        return open_tag.replace('class="', f'class="{cls} ', 1)
    return open_tag[:-1].rstrip() + f' class="{cls}">'


def _caption_split(inner: str) -> int | None:
    """Where the first sentence ends, or None if the caption is one sentence.

    Depth-aware, because a boundary found inside a nested element would cut the
    markup in half: the remainder would carry a close tag whose open tag stayed
    behind in the title. Only a boundary at the caption's own level splits.
    """
    depth = 0
    i = 0
    while i < len(inner):
        ch = inner[i]
        if ch == "<":
            m = _TAG_RE.match(inner, i)
            if m is None:
                i += 1
                continue
            name = m.group(2).lower()
            if not (name in _VOID or m.group(4)):
                depth += -1 if m.group(1) else 1
            i = m.end()
            continue
        if depth or ch not in _SENTENCE_PUNCT:
            i += 1
            continue
        end = i + 1
        while end < len(inner) and inner[end] in _CLOSERS:
            end += 1
        rest = end
        while rest < len(inner) and inner[rest].isspace():
            rest += 1
        if rest == end:          # no space after it: a decimal, or mid-word
            i += 1
            continue
        if _abbreviation(inner[:i]) or not _opens_a_sentence(inner[rest:]):
            i += 1
            continue
        title, notes = _words(inner[:end]), _words(inner[rest:])
        # A one-word title is a label rather than a name, and a one-word
        # remainder is a stray "Ibid." -- neither is the shape this splits.
        if len(title) < 2 or len(notes) < 3:
            i += 1
            continue
        return rest
    return None


def _abbreviation(before: str) -> bool:
    m = _WORD_RE.search(_TEXT_RE.sub("", before))
    if m is None:
        return False
    word = m.group(0).rstrip(".").lower()
    return word in _ABBREVIATIONS or len(word) == 1


def _opens_a_sentence(after: str) -> bool:
    text = _TEXT_RE.sub("", after).lstrip(_CLOSERS + "'‘\"“").lstrip()
    return bool(text) and (text[0].isupper() or text[0].isdigit())


def _words(fragment: str) -> list[str]:
    return _TEXT_RE.sub(" ", fragment).split()
