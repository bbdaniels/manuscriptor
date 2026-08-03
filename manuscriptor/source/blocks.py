"""M1 — cut the flattened buffer into addressable blocks with stable ids.

The block is the unit of work. A worker may read as widely as it needs but may
only ever write one block, which is the property that makes running edits live
acceptable at all.

Block identity is derived from content, never from position. On re-parse we
match exact id first, then nearest-neighbour similarity within the same file.
This is what stops anchor drift from being a problem: a comment follows its
paragraph through edits above it rather than trusting a line number that moves.

In practice a block is usually a paragraph. In these manuscripts `main.tex` is
written one paragraph per line, so a paragraph anchor is effectively a line
number and an edit to one is a single-line replacement.

**`source_text` versus `flat_text` is the most important distinction here.**
qutub-india inlines auto-exported results mid-sentence: `p=\\input{exhibits/pval}`
flattens to `p=0.096`. `flat_text` is what pandoc rendered and what the anchors
address; `source_text` is the unflattened host-file source, with every include
directive put back verbatim, and it is the only thing the editor may show. If a
save round-tripped the flattened text the `\\input` would be destroyed and the
result hardcoded, which is exactly what this tool exists to prevent.

Reconstructing `source_text` is the fiddly part. flatten emits the host file's
own bytes and the expansion of each include as separate segments, so the gap
between two consecutive host segments in the host *file* is precisely the
directive text that produced everything in between. Putting that gap back is the
whole trick.
"""
from __future__ import annotations

import bisect
import difflib
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

# Deliberately reusing flatten's own scanning primitives rather than declaring a
# second copy. Two LaTeX parsers in one codebase that have to agree is the kind
# of split that rots quietly and then bites mid-revision.
from manuscriptor.source.flatten import (
    _INCLUDE_RE,
    command_re,
    is_commented,
    _resolve,
    FlatSource,
)

KINDS = (
    "paragraph",
    "heading",
    "table",
    "figure",
    "equation",
    "caption",
    "list_item",
    "generated",
)

# Sectioning commands, shallowest first. `\part` and `\subparagraph` are not in
# the spec but cost nothing and would otherwise be swallowed into a paragraph.
_SECTION_LEVEL = {
    "part": 0,
    "chapter": 1,
    "section": 2,
    "subsection": 3,
    "subsubsection": 4,
    "paragraph": 5,
    "subparagraph": 6,
}

_FLOATS = {
    "table": "table",
    "table*": "table",
    "figure": "figure",
    "figure*": "figure",
    "sidewaystable": "table",
    "sidewaysfigure": "figure",
    "longtable": "table",
    "wraptable": "table",
    "wrapfigure": "figure",
}

_MATH = {
    "equation",
    "equation*",
    "align",
    "align*",
    "alignat",
    "alignat*",
    "gather",
    "gather*",
    "multline",
    "multline*",
    "flalign",
    "flalign*",
    "eqnarray",
    "eqnarray*",
    "displaymath",
}

_LISTS = {"itemize", "enumerate", "description"}

_ID_RE = re.compile(r"^b-[0-9a-f]{10}(?:-\d+)?$")
_CTRL_RE = re.compile(r"\\(?:([a-zA-Z@]+)\*?|(.))", re.S)
_BLANK_RE = re.compile(r"\n[ \t\r]*(?:\n[ \t\r]*)+")

# `\captionsetup` and friends must not match, so the name ends where it ends.
_CAPTION_RE = re.compile(r"\\caption(of)?\*?(?![a-zA-Z])")

# How long a name may be. The ticker clamps at 26ch in CSS and keeps the whole
# string on hover, but the queue, the inspector title and the work item handed
# to the agent all read this same string and none of them has a stylesheet.
LABEL_WORDS = 14
LABEL_CHARS = 80

# Commands whose argument is machinery rather than words. `\input` is a path and
# would put `exhibits/pval` where a name goes; `\ref` is a number this module
# cannot know; a `\footnote` is a different sentence that happens to start here.
# Dropped whole, command and arguments together. `begin`/`end` are here for the
# blocks that are a whole environment: an abstract is one paragraph-shaped block
# whose source opens `\begin{abstract}`, and naming it that put markup in the
# queue where the abstract's first line belongs.
_DROP = {
    "begin", "end",
    "input", "include", "label", "index", "nocite", "bibliography",
    "bibliographystyle", "includegraphics", "vspace", "hspace", "footnote",
    "footnotetext", "thanks", "ref", "eqref", "autoref", "pageref", "nameref",
    "cref", "Cref", "cpageref", "Cpageref", "vref", "protect",
    # Document machinery that opens a block ahead of its first real word.
    # dsp-bias's abstract begins `\begin{singlespace} \setlength{\parskip}{2pt}
    # \maketitle \vspace{-2em}` and only then says what the paper is about.
    "setlength", "addtolength", "setcounter", "counterwithin", "numberwithin",
    "renewcommand", "newcommand", "providecommand", "newcolumntype",
    "addcontentsline", "captionsetup", "includepdf", "pagenumbering",
    "pagestyle", "thispagestyle", "hypersetup", "graphicspath",
}
# Commands that print nothing and take no argument, so the token goes and
# whatever follows it stays. `\item` heads every list-item block by
# construction, which is why the list is not empty.
_BARE = {
    "item", "par", "noindent", "indent", "centering", "raggedright",
    "raggedleft", "clearpage", "newpage", "bigskip", "medskip", "smallskip",
    "hfill", "linebreak", "newline", "footnotesize", "scriptsize", "small",
    "normalsize", "large", "Large", "LARGE", "huge", "Huge",
    "maketitle", "appendix", "tableofcontents", "listoftables", "listoffigures",
    "singlespacing", "doublespacing", "onehalfspacing",
}
# Commands that wrap words the author meant to read. Sectioning is here so a
# heading block, whose whole source IS the command, comes back as its title --
# and `title` is here for exactly the same reason. A `\title{...}` block is
# markup end to end, so `label()`'s markup test threw the name away and the
# paper's own title reported itself in the queue, the ticker and the inspector
# as its parent heading, or as nothing at all.
_UNWRAP = {
    "textbf", "textit", "textrm", "textsf", "texttt", "textsc", "textnormal",
    "emph", "text", "mbox", "textsuperscript", "textsubscript", "uline",
    "underline", "MakeUppercase", "MakeLowercase", "title", *_SECTION_LEVEL,
}
# Words that end in a period and do not end a sentence. Initialisms (`U.S.`,
# `e.g.`) are matched by shape rather than listed.
_ABBREV = {
    "fig.", "figs.", "tab.", "tabs.", "eq.", "eqs.", "no.", "nos.", "vs.", "cf.",
    "al.", "approx.", "est.", "ref.", "refs.", "sec.", "ch.", "pp.", "p.", "dr.",
    "prof.", "st.", "mr.", "ms.", "mrs.",
}
_INITIALISM_RE = re.compile(r"^(?:[A-Za-z]\.)+$")
# Inline math only. `$$` and display environments do not appear in a caption,
# and an unpaired `$` is left alone rather than eating the rest of the name.
_MATH_RE = re.compile(r"(?<!\\)\$([^$]*)(?<!\\)\$")
_ESCAPED_RE = re.compile(r"\\([%&_#${}])")
# Markup that survived the cleaner. A name is words; if this still matches, the
# block has not told us what it is called and its heading is the better answer.
_MARKUP_RE = re.compile(r"\\[a-zA-Z@]|[{}]")
# A citation in a caption is attribution, not part of the exhibit's name, and it
# cannot be rendered here: `\citealt{rijnhart_mediation}` would put a bibtex key
# on screen. estonia-ecm's mediation flowchart is captioned "Mediation analysis
# flowchart (based on \citealt{...})", whose name is the part before the paren.
_CITE = r"\\cite[a-zA-Z]*\*?(?:\[[^\]]*\])*\s*\{[^{}]*\}"
_CITE_PAREN_RE = re.compile(r"\s*\([^()]*" + _CITE + r"[^()]*\)")
_CITE_RE = re.compile(_CITE)


@dataclass(frozen=True)
class Include:
    """One `\\input`/`\\include` directive falling inside a block.

    `flat_start`/`flat_end` bound what the directive *expanded to* in the
    flattened buffer; `directive` is the verbatim text that sits in the host
    file and must survive any edit. Unresolvable directives are not recorded:
    flatten leaves those in place as ordinary host bytes, so they are already
    part of `source_text` and there is nothing to substitute back.
    """

    directive: str
    target: Path
    flat_start: int
    flat_end: int


@dataclass(frozen=True)
class Block:
    id: str
    kind: str
    file: Path
    line_start: int
    line_end: int
    flat_start: int
    flat_end: int
    source_text: str
    flat_text: str
    parent_heading: str | None
    editable: bool
    includes: tuple[Include, ...] = ()
    caption: str | None = None
    # The single characters that delimit the block in its host file, `""` at
    # the start or the end of the file. WHERE THE CUT FELL, which is the one
    # thing the id cannot say: an id says what the block's bytes ARE, so it
    # hashes back just as happily when those bytes have become the first half
    # of a longer paragraph. `splice` needs both facts to know it is replacing
    # a whole block and not a prefix of one; see `splice._locate`.
    src_before: str = ""
    src_after: str = ""


# ------------------------------------------------------------------ public API


def block_id(source_text: str) -> str:
    """Content-derived stable id: `b-` plus 10 hex chars of sha256.

    Whitespace is normalized first, so rewrapping a paragraph does not orphan
    its comments. Duplicate-suffix disambiguation (`-2`, `-3`) is applied by
    `segment`, which is the only thing that knows document order.
    """
    norm = " ".join(source_text.split())
    return "b-" + hashlib.sha256(norm.encode("utf-8")).hexdigest()[:10]


# Blocks named by their OWN words. A paragraph, a list item and a heading are
# made of words, so the first of them is a name. A table's own words are column
# specifications and ampersands, and an equation's are operators, so those keep
# the caption-then-heading rule.
_SELF_NAMED = {"paragraph", "list_item", "heading"}


def label(block: Block) -> str | None:
    """What to call this block in front of the author.

    Its own words, in whatever form it has them: a caption if it has one, else
    its opening words, and only then the heading it sits under. The heading is a
    fact about position and is usually not a name at all.

    Both halves of that came from the same bug, six days apart. dsp-bias
    `\\input`s a file of two tables fifty lines below a `\\paragraph{Socioeconomic
    status.}`, so both tables answered to those words and the ticker reported an
    edit landing on one of them in language indistinguishable from the other,
    which was open in the inspector and untouched. Naming an exhibit by its
    `\\caption` fixed that for exhibits -- and left it armed for the large
    majority of blocks in any manuscript, because a paragraph has no caption to
    be named by and so still came back as its section, once per paragraph in the
    section. An opening clause distinguishes two paragraphs; a shared heading
    never can.

    One function, because the queue, the ticker, the inspector title and the
    work item handed to the agent must all name a block the same way; naming it
    two ways is how the page comes to contradict itself.
    """
    if block.caption:
        return _clip(block.caption)
    if block.kind in _SELF_NAMED:
        # Clipped BEFORE the markup test, because the test is about what the
        # author will read. estonia-ecm's holistic-care paragraph opens with
        # thirty clean words and then a `\citep`-free bit of math, and testing
        # the whole first sentence threw the name away over markup that the
        # ticker was never going to show.
        own = _clip(_name(block.source_text))
        # A paragraph that is only markup -- `\maketitle`, a `\setlength` pair, a
        # displayed derivation carrying `\sum` and `\alpha'` -- has no words to
        # be named by, and printing what is left of it names nothing. Those are
        # the blocks the heading was always the right answer for.
        if own and not _MARKUP_RE.search(own):
            return own
    return block.parent_heading


def base_id(bid: str) -> str:
    """Strip a duplicate suffix, so `b-3f2a91c0de-2` hashes back to its content."""
    if _ID_RE.match(bid) and bid.count("-") == 2:
        return bid[: bid.rindex("-")]
    return bid


def segment(flat: FlatSource) -> tuple[Block, ...]:
    """Cut a FlatSource into blocks in document order."""
    if not flat.segments:
        return ()

    texts, line_starts = _read_sources(flat)
    pieces = _locate_pieces(flat, texts, line_starts)
    # Built once. qutub-india resolves 369 segments, and rebuilding this index
    # per block would make segmentation quadratic in the number of fragments.
    piece_starts = [p.flat_start for p in pieces]

    tokens = _tokenize(flat.text, 0, len(flat.text))
    body_start, body_end = _body_bounds(flat.text, tokens)
    # Prose the author edits that is written ABOVE `\begin{document}`, plus the
    # body. Each region is cut by the SAME `_cut`, so there is still exactly one
    # implementation of splitting LaTeX into blocks. Sorted rather than
    # prepended, because "which comes first" is a fact about the manuscript --
    # covet writes the title above the abstract, and nothing forbids the
    # reverse.
    regions = [(body_start, body_end)]
    for extra in (
        _preamble_title(flat.text, body_start),
        _preamble_abstract(tokens, body_start),
    ):
        if extra is not None:
            regions.append(extra)
    regions.sort()

    cuts: list[_Cut] = []
    for lo, hi in regions:
        region_tokens = [t for t in tokens if t.start >= lo and t.end <= hi]
        cuts.extend(_cut(flat.text, region_tokens, lo, hi))

    blocks: list[Block] = []
    seen: dict[str, int] = {}
    headings: list[tuple[int, str]] = []

    for cut in cuts:
        built = _rebuild(flat, pieces, piece_starts, texts, cut.start, cut.end)
        if built is None:
            continue
        host, source_text, includes, src_lo, src_hi, whole = built

        # Provenance is NOT decided here. Whether a file is machine-written is a
        # question about the analysis code that produced it, which this module
        # cannot see; `server/producers.py` answers it and rewrites `kind` and
        # `editable` afterwards. Guessing it from the path marked 283 of
        # estonia-ecm's 384 blocks uneditable, nearly all of them hand-written
        # prose appendices.
        kind = cut.kind

        if cut.kind == "heading":
            while headings and headings[-1][0] >= cut.level:
                headings.pop()
            parent = headings[-1][1] if headings else None
            headings.append((cut.level, cut.title))
        else:
            parent = headings[-1][1] if headings else None

        bid = block_id(source_text)
        n = seen.get(bid, 0) + 1
        seen[bid] = n
        if n > 1:
            bid = f"{bid}-{n}"

        starts = line_starts[host]
        # Normalized the way `splice` normalizes the file it reads, or every
        # block of a CRLF manuscript would record a boundary splice can never
        # see.
        host_text = texts[host]
        before = host_text[src_lo - 1] if src_lo > 0 else ""
        after = host_text[src_hi] if src_hi < len(host_text) else ""
        blocks.append(
            Block(
                id=bid,
                kind=kind,
                file=host,
                line_start=bisect.bisect_right(starts, src_lo),
                line_end=bisect.bisect_right(starts, max(src_lo, src_hi - 1)),
                flat_start=cut.start,
                flat_end=cut.end,
                source_text=source_text,
                flat_text=flat.text[cut.start : cut.end],
                parent_heading=parent,
                editable=whole,  # splicing safety only; see producers.apply
                includes=tuple(includes),
                caption=_caption_of(source_text),
                src_before="\n" if before == "\r" else before,
                src_after="\n" if after == "\r" else after,
            )
        )

    return tuple(blocks)


def rematch(old: tuple[Block, ...], new: tuple[Block, ...]) -> dict[str, str | None]:
    """Map old block ids onto new ones after a source edit.

    Exact id first, then the best similarity above 0.6 among unmatched blocks in
    the same file. `None` means the block genuinely vanished, so callers can
    orphan its comments rather than silently reattaching them to a paragraph the
    author never commented on.
    """
    new_by_id = {b.id: b for b in new}
    mapping: dict[str, str | None] = {}
    claimed: set[str] = set()

    for b in old:
        hit = new_by_id.get(b.id)
        if hit is not None and hit.id not in claimed:
            mapping[b.id] = hit.id
            claimed.add(hit.id)

    unmatched_old = [b for b in old if b.id not in mapping]
    unmatched_new = [b for b in new if b.id not in claimed]

    norm = {b.id: " ".join(b.source_text.split()) for b in unmatched_old}
    norm.update({b.id: " ".join(b.source_text.split()) for b in unmatched_new})

    candidates: list[tuple[float, str, str]] = []
    for a in unmatched_old:
        for c in unmatched_new:
            if a.file != c.file:
                continue
            sm = difflib.SequenceMatcher(None, norm[a.id], norm[c.id], autojunk=False)
            if sm.real_quick_ratio() <= 0.6 or sm.quick_ratio() <= 0.6:
                continue
            ratio = sm.ratio()
            if ratio > 0.6:
                candidates.append((ratio, a.id, c.id))

    candidates.sort(key=lambda t: (-t[0], t[1], t[2]))
    for _, old_id, new_id in candidates:
        if old_id in mapping or new_id in claimed:
            continue
        mapping[old_id] = new_id
        claimed.add(new_id)

    for b in old:
        mapping.setdefault(b.id, None)
    return mapping


# ------------------------------------------------------------------- tokenizer


@dataclass(frozen=True)
class _Tok:
    kind: str  # begin | end | item | section | dmath_open | dmath_close | blank
    start: int
    end: int
    name: str = ""
    title: str = ""


def _tokenize(text: str, start: int, end: int) -> list[_Tok]:
    """Structural tokens only. Prose produces nothing, which is the point: the
    segmenter tracks an open cursor and everything between two tokens is body."""
    toks: list[_Tok] = []
    i = start
    while i < end:
        c = text[i]
        if c == "%":
            nl = text.find("\n", i, end)
            i = end if nl < 0 else nl
            continue
        if c == "\\":
            m = _CTRL_RE.match(text, i)
            if m is None:
                i += 1
                continue
            word, sym, j = m.group(1), m.group(2), m.end()
            if word in ("begin", "end"):
                name, k = _read_group(text, j)
                if name is not None:
                    toks.append(_Tok(word, i, k, name=name.strip()))
                    i = k
                    continue
            elif word == "item":
                k = _skip_optional(text, j)
                toks.append(_Tok("item", i, k))
                i = k
                continue
            elif word in _SECTION_LEVEL:
                k = _skip_optional(text, j)
                title, k2 = _read_group(text, k)
                if title is not None:
                    toks.append(_Tok("section", i, k2, name=word, title=title.strip()))
                    i = k2
                    continue
            elif sym == "[":
                toks.append(_Tok("dmath_open", i, j))
                i = j
                continue
            elif sym == "]":
                toks.append(_Tok("dmath_close", i, j))
                i = j
                continue
            i = j
            continue
        if c == "\n":
            m = _BLANK_RE.match(text, i)
            if m is not None:
                stop = min(m.end(), end)
                toks.append(_Tok("blank", i, stop))
                i = stop
                continue
        i += 1
    return toks


def _read_group(text: str, pos: int) -> tuple[str | None, int]:
    """Read a balanced `{...}` at or after `pos`, skipping leading whitespace."""
    i = pos
    while i < len(text) and text[i] in " \t\n\r":
        i += 1
    if i >= len(text) or text[i] != "{":
        return None, pos
    depth = 0
    j = i
    while j < len(text):
        c = text[j]
        if c == "\\":
            j += 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[i + 1 : j], j + 1
        j += 1
    return None, pos


def _read_optional(text: str, pos: int) -> tuple[str | None, int]:
    """Read a balanced `[...]` at or after `pos`, e.g. `\\caption[short]`."""
    i = pos
    while i < len(text) and text[i] in " \t":
        i += 1
    if i >= len(text) or text[i] != "[":
        return None, pos
    depth = 0
    j = i
    while j < len(text):
        c = text[j]
        if c == "\\":
            j += 2
            continue
        if c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                return text[i + 1 : j], j + 1
        j += 1
    return None, pos


def _skip_optional(text: str, pos: int) -> int:
    """Skip an optional `[...]` argument, e.g. `\\item[Label]`, `\\section[short]`."""
    return _read_optional(text, pos)[1]


def _caption_of(source_text: str) -> str | None:
    """The block's own caption, as words rather than as LaTeX.

    The first caption in the block, because a longtable declares its head twice
    and both say the same thing. The `[short]` form wins when it is there: an
    author writes it precisely so the exhibit has a name shorter than its
    explanation. Only the title SENTENCE is kept, since the rest of an academic
    caption is the note under the table and is no use as a name in a status line
    a hundred pixels wide.
    """
    for m in _CAPTION_RE.finditer(source_text):
        i = m.end()
        if m.group(1):                      # \captionof{table}{...}: the type first
            _, i = _read_group(source_text, i)
        short, i = _read_optional(source_text, i)
        body, _ = _read_group(source_text, i)
        text = short if (short or "").strip() else body
        cleaned = _name(text or "")
        if cleaned:
            return cleaned
    return None


def _name(text: str) -> str:
    """LaTeX as the plain-text name of the thing it wrote.

    One implementation, used for a caption and for the opening words of a
    paragraph alike. They are the same job -- turn markup into what the author
    would say this block is -- and two strippers that have to agree is the split
    that rots quietly and then names one block two ways.

    Inline math keeps its CONTENT and loses its delimiters: `($N = 216$)` is
    part of what the author calls the table, and dropping it loses the sample
    size, but a `$` on screen is LaTeX leaking into a label. Nothing here tries
    to render math -- a name that is an equation will read oddly, and that is
    better than a name that reads as nothing.

    Only the first SENTENCE, since the rest of an academic caption is the note
    under the table and the rest of a paragraph is the argument, and neither is
    any use in a status line a hundred pixels wide.
    """
    # A commented-out first line is not what the block is called, and estonia-ecm
    # opens paragraphs with shouted drafting notes: the Introduction's fourth
    # paragraph begins `%CONTRACTING LITERATURE IS MISSING FROM ...`.
    text = _strip_comments(text)
    text = _CITE_RE.sub("", _CITE_PAREN_RE.sub("", text))
    text = _macros(text)
    text = _MATH_RE.sub(lambda m: m.group(1).strip(), text)
    text = text.replace("\\\\", " ").replace("~", " ")
    text = _ESCAPED_RE.sub(r"\1", text)
    # TeX quoting is typography, not markup, and every one of these manuscripts
    # uses it: `an explicit ``care plan''` would otherwise be read back verbatim.
    text = text.replace("``", "“").replace("''", "”")
    return _first_sentence(" ".join(text.split()))


def _macros(text: str) -> str:
    """Drop machinery commands with their arguments; unwrap wrapping ones.

    A single scan rather than a pile of regexes, because both jobs need balanced
    braces: `\\footnote{see \\emph{also} p. 4}` has to be dropped to its closing
    brace and not to the first one, and `\\textbf{\\emph{x}}` has to unwrap to
    the bottom. Unwrapping recurses, so nesting costs no extra pass.

    A command this does not know is emitted verbatim WITH its arguments, braces
    and all: `\\textcolor{red}{flagged}` would otherwise print `red`, and printing
    the wrong word is worse than printing the markup, which at least reads as
    something the author can go and fix.

    That is also why the brace strip lives here rather than after. A brace is
    grouping unless some command this pass did not recognise is holding it, and
    only the pass knows which is which. Deciding it afterwards on the whole
    string -- keep every brace if any backslash survives anywhere -- meant one
    `\\alpha` in the last line of a paragraph left `Y_{ik,t}` in its first.
    """
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c != "\\":
            # A brace reached here is grouping. One held by a command this pass
            # did not recognise never arrives, because that command is emitted
            # whole -- braces and arguments together -- below.
            if c not in "{}":
                out.append(c)
            i += 1
            continue
        m = _CTRL_RE.match(text, i)
        if m is None or m.group(1) is None:  # `\\`, `\%`, `\&`: later passes own these
            out.append(text[i : m.end()] if m else text[i])
            i = m.end() if m else i + 1
            continue
        word, j = m.group(1), m.end()
        if word in _DROP:
            i = _args_end(text, j)
            continue
        if word in _BARE:
            i = j
            continue
        if word in _UNWRAP:
            body, k = _read_group(text, _skip_optional(text, j))
            if body is not None:
                out.append(_macros(body))
                i = k
                continue
        k = _args_end(text, j)
        out.append(text[i:k])
        i = k
    return "".join(out)


def _args_end(text: str, pos: int) -> int:
    """The end of a command's arguments: `[...]` and `{...}`, run together.

    Adjacency is what stops it running away. `\\addcontentsline{toc}{section}
    {Appendix}` is three arguments to one command and all three are machinery,
    but `\\begin{center}` followed on the next line by a `{...}` group is a
    command and then a group, and eating the group would eat a heading.
    """
    j = _skip_optional(text, pos)
    _, j = _read_group(text, j)
    while j < len(text) and text[j] in "[{":
        k = _read_optional(text, j)[1] if text[j] == "[" else _read_group(text, j)[1]
        if k == j:
            break
        j = k
    return j


def clip(text: str | None, *, words: int = LABEL_WORDS, chars: int = LABEL_CHARS) -> str:
    """Text short enough to be read at a glance, in Python and not in CSS.

    Public because the ticker clips a request the same way the queue clips a
    name, and two functions that shorten a string to a word boundary would drift
    into disagreeing about the ellipsis.
    """
    if not text:
        return ""
    parts = text.split()
    cut = len(parts) > words
    parts = parts[:words]
    out = " ".join(parts)
    while len(out) > chars - 1 and len(parts) > 1:
        parts.pop()
        cut = True
        out = " ".join(parts)
    if len(out) > chars - 1:                 # one very long word, and nothing else
        out, cut = out[: chars - 1], True
    return out.rstrip(" ,;:") + "…" if cut else out


_clip = clip


def _first_sentence(text: str) -> str:
    """Up to the first sentence-ending period. `Fig. 2` and `U.S.` do not end one."""
    i = 0
    while True:
        j = text.find(". ", i)
        if j < 0:
            return text
        word = text[:j].split()
        last = (word[-1] + "." if word else "").lower()
        if last in _ABBREV or _INITIALISM_RE.match(last):
            i = j + 2
            continue
        return text[: j + 1]


def _body_bounds(text: str, tokens: list[_Tok]) -> tuple[int, int]:
    """Everything between `\\begin{document}` and `\\end{document}`.

    A fragment with neither is treated as all body, so a single included file
    can be segmented on its own.
    """
    start, end = 0, len(text)
    for t in tokens:
        if t.kind == "begin" and t.name == "document":
            start = t.end
            break
    for t in reversed(tokens):
        if t.kind == "end" and t.name == "document":
            end = max(start, t.start)
            break
    return start, end


_TITLE_CMD_RE = command_re("title")


def _preamble_title(text: str, body_start: int) -> tuple[int, int] | None:
    """The span of a `\\title{...}` written ABOVE `\\begin{document}`.

    Four of the six manuscripts put it there, and it fell in no region: no
    block, no id, and nothing for the rendered `<h1>` to answer to except the
    `\\maketitle` block standing next to it. Clicking a paper's title opened an
    inspector whose entire source was `\\flushbottom\\maketitle`.

    One contiguous byte range in the root file, the same shape as a
    `\\section{...}` heading block the editor already splices, so it is
    admissible as exactly one block.

    What counts as a `\\title` is decided by `flatten.command_re` and not here,
    because `render/pandoc.py` asks the same question from the other end -- it
    reads the title's TEXT where this reads its BYTES -- and two answers to it
    means the block and the rendered heading stop describing the same thing.
    """
    if body_start <= 0:                      # a fragment: it is all body already
        return None
    m = _TITLE_CMD_RE.search(text, 0, body_start)
    if m is None:
        return None
    _, end = _read_group(text, m.end())
    if end <= m.end() or end > body_start:
        return None
    return m.start(), end


def _preamble_abstract(tokens: list[_Tok], body_start: int) -> tuple[int, int] | None:
    """The span of an `abstract` environment written ABOVE `\\begin{document}`.

    covet-india's class (`wlscirep`) defines no abstract environment: the .cls
    captures a delimited macro into `\\theabstract` for `\\maketitle` to typeset
    later, so the abstract is written in the preamble. `_body_bounds` cuts it
    away with the rest of the preamble, and the paper's abstract then had no
    block, no id and no anchor at all.

    Returned as a second region rather than by widening the body, because
    everything else above `\\begin{document}` -- `\\usepackage`, `\\newcommand`,
    class options -- genuinely is not prose and must stay outside every cut.
    """
    if body_start <= 0:                      # a fragment: it is all body already
        return None
    for i, t in enumerate(tokens):
        if t.start >= body_start:
            return None
        if t.kind == "begin" and t.name == "abstract":
            j = _match_end(tokens, i, "abstract")
            if j is None or tokens[j].end > body_start:
                return None
            return t.start, tokens[j].end
    return None


# ------------------------------------------------------------------- segmenter


@dataclass(frozen=True)
class _Cut:
    kind: str
    start: int
    end: int
    title: str = ""
    level: int = 0


def _cut(text: str, toks: list[_Tok], body_start: int, body_end: int) -> list[_Cut]:
    out: list[_Cut] = []
    env: list[str] = []
    state = {"open": body_start, "kind": "paragraph"}

    def sdepth() -> int:
        # List environments are transparent: `\item` cuts inside them. Every
        # other environment is opaque, so a blank line inside a table or an
        # align block never splits it.
        return sum(1 for e in env if e not in _LISTS)

    def close(at: int) -> None:
        s, e = _trim(text, state["open"], at)
        if e > s and _strip_comments(text[s:e]).strip():
            out.append(_Cut(state["kind"], s, e))
        state["open"] = at
        state["kind"] = "paragraph"

    i = 0
    while i < len(toks):
        t = toks[i]

        if t.kind == "begin":
            if sdepth() == 0 and (t.name in _FLOATS or t.name in _MATH):
                close(t.start)
                j = _match_end(toks, i, t.name)
                stop = toks[j].end if j is not None else body_end
                kind = _FLOATS.get(t.name, "equation")
                out.append(_Cut(kind, t.start, stop))
                state["open"] = stop
                i = len(toks) if j is None else j + 1
                continue
            if sdepth() == 0 and t.name in _LISTS:
                close(t.start)
                state["open"] = t.end
            env.append(t.name)
            i += 1
            continue

        if t.kind == "end":
            if t.name in env:
                while env and env.pop() != t.name:
                    pass
            if t.name in _LISTS and sdepth() == 0:
                close(t.start)
                state["open"] = t.end
            i += 1
            continue

        if sdepth() > 0:
            i += 1
            continue

        if t.kind == "blank":
            close(t.start)
            state["open"] = t.end
        elif t.kind == "item" and env and env[-1] in _LISTS:
            close(t.start)
            state["open"] = t.start
            state["kind"] = "list_item"
        elif t.kind == "section":
            close(t.start)
            out.append(_Cut("heading", t.start, t.end, t.title, _SECTION_LEVEL[t.name]))
            state["open"] = t.end
        elif t.kind == "dmath_open":
            close(t.start)
            j = next(
                (k for k in range(i + 1, len(toks)) if toks[k].kind == "dmath_close"),
                None,
            )
            stop = toks[j].end if j is not None else body_end
            out.append(_Cut("equation", t.start, stop))
            state["open"] = stop
            i = len(toks) if j is None else j + 1
            continue

        i += 1

    close(body_end)
    return out


def _match_end(toks: list[_Tok], i: int, name: str) -> int | None:
    depth = 0
    for k in range(i, len(toks)):
        t = toks[k]
        if t.kind == "begin" and t.name == name:
            depth += 1
        elif t.kind == "end" and t.name == name:
            depth -= 1
            if depth == 0:
                return k
    return None


def _trim(text: str, start: int, end: int) -> tuple[int, int]:
    while start < end and text[start] in " \t\r\n":
        start += 1
    while end > start and text[end - 1] in " \t\r\n":
        end -= 1
    return start, end


def _strip_comments(chunk: str) -> str:
    """Drop LaTeX comments so a comment-only region is recognised as empty."""
    out: list[str] = []
    for line in chunk.split("\n"):
        i = 0
        cut = len(line)
        while i < len(line):
            if line[i] == "\\":
                i += 2
                continue
            if line[i] == "%":
                cut = i
                break
            i += 1
        out.append(line[:cut])
    return "\n".join(out)


# ------------------------------------------------- flat offsets back to source


@dataclass(frozen=True)
class _Piece:
    """One flat segment, located in the bytes of the file that produced it."""

    flat_start: int
    flat_end: int
    file: Path
    src_start: int
    src_end: int


def _read_sources(flat: FlatSource) -> tuple[dict[Path, str], dict[Path, list[int]]]:
    texts: dict[Path, str] = {}
    line_starts: dict[Path, list[int]] = {}
    for seg in flat.segments:
        if seg.file in texts:
            continue
        # Read exactly the way flatten did, or the offsets stop agreeing.
        texts[seg.file] = seg.file.read_text(encoding="utf-8")
        line_starts[seg.file] = _line_starts(texts[seg.file])
    return texts, line_starts


def _line_starts(text: str) -> list[int]:
    starts = [0]
    for i, c in enumerate(text):
        if c == "\n":
            starts.append(i + 1)
    return starts


def _locate_pieces(
    flat: FlatSource, texts: dict[Path, str], line_starts: dict[Path, list[int]]
) -> list[_Piece]:
    """Give every flat segment its byte range in its own file.

    The segment knows its starting line, and its flat content is a verbatim copy
    of the file's bytes, so the range is found by matching the content on that
    line. A per-file cursor breaks ties when one line contributes two segments
    (`p=\\input{a}q=\\input{b}`), and resets when a file is included twice.
    """
    pieces: list[_Piece] = []
    cursor: dict[Path, int] = {}
    for seg in flat.segments:
        text = texts[seg.file]
        starts = line_starts[seg.file]
        content = flat.text[seg.flat_start : seg.flat_end]
        lo = starts[seg.line_start - 1] if seg.line_start - 1 < len(starts) else 0
        hi = starts[seg.line_start] if seg.line_start < len(starts) else len(text)
        c = cursor.get(seg.file, -1)
        probe = c if lo <= c <= hi else lo
        at = text.find(content, probe)
        if at < 0 or at > hi:
            at = text.find(content, lo)
        if at < 0 or at > hi:
            raise ValueError(
                f"cannot place flat segment {seg.flat_start}:{seg.flat_end} "
                f"on line {seg.line_start} of {seg.file}"
            )
        pieces.append(_Piece(seg.flat_start, seg.flat_end, seg.file, at, at + len(content)))
        cursor[seg.file] = at + len(content)
    return pieces


def _root_file(flat: FlatSource, texts: dict[Path, str]) -> Path:
    """The one file nothing else includes. Blocks hosted anywhere else are
    treated as generated and refuse edits, because a file reached through an
    `\\input` is as likely as not to have been written by analysis code."""
    order: list[Path] = []
    for seg in flat.segments:
        if seg.file not in order:
            order.append(seg.file)
    targets: set[Path] = set()
    for f in order:
        text = texts[f]
        for m in _INCLUDE_RE.finditer(text):
            if is_commented(text, m.start()):
                continue
            hit = _resolve(m.group(2).strip(), including=f, root=flat.root)
            if hit is not None:
                targets.add(hit)
    for f in order:
        if f not in targets:
            return f
    return order[0]


def _rebuild(
    flat: FlatSource,
    pieces: list[_Piece],
    piece_starts: list[int],
    texts: dict[Path, str],
    start: int,
    end: int,
):
    """Reconstruct one block's unflattened source.

    Returns `(host, source_text, includes, src_lo, src_hi, whole)`. `whole` is
    False when the block's flat range does not sit entirely inside one host
    file's own expansion — a paragraph broken across an include boundary. Those
    cannot be written back as a single byte range, so they are marked
    uneditable rather than spliced approximately.
    """
    i = max(0, bisect.bisect_right(piece_starts, start) - 1)
    if i >= len(pieces):
        return None
    host = pieces[i].file
    host_text = texts[host]

    out: list[str] = []
    includes: list[Include] = []
    src_lo: int | None = None
    src_hi = 0
    flat_hi = 0
    whole = True

    k = i
    while k < len(pieces) and pieces[k].flat_start < end:
        p = pieces[k]
        if p.file != host:
            k += 1
            continue
        a, b = max(p.flat_start, start), min(p.flat_end, end)
        sa = p.src_start + (a - p.flat_start)
        sb = p.src_start + (b - p.flat_start)
        if src_lo is None:
            src_lo = sa
        elif sa > src_hi:
            gap = host_text[src_hi:sa]
            out.append(gap)
            includes.extend(
                _includes_in(gap, host, flat.root, pieces, piece_starts, flat_hi, a)
            )
        out.append(host_text[sa:sb])
        src_hi, flat_hi = sb, b
        k += 1

    if src_lo is None:
        return None

    # A trailing include: the block ends inside, or exactly at the end of, an
    # expansion. Substitute the directive back and check nothing but whitespace
    # of that expansion falls outside the block.
    while flat_hi < end:
        m = _INCLUDE_RE.match(host_text, src_hi)
        if m is None:
            break
        target = _resolve(m.group(2).strip(), including=host, root=flat.root)
        exp_end = _expansion_end(pieces, piece_starts, flat_hi, host)
        out.append(m.group(0))
        if target is not None:
            includes.append(Include(m.group(0), target, flat_hi, exp_end))
        if flat.text[end:exp_end].strip():
            whole = False
        src_hi, flat_hi = m.end(), exp_end

    if flat_hi < end:
        # The block began inside an include and ran on into the parent file. It
        # spans two files, so no single splice can express an edit to it.
        out.append(flat.text[max(flat_hi, start) : end])
        whole = False

    return host, "".join(out), includes, src_lo, src_hi, whole


def _expansion_end(
    pieces: list[_Piece], piece_starts: list[int], flat_from: int, host: Path
) -> int:
    """Where the include expansion beginning at `flat_from` stops."""
    k = bisect.bisect_left(piece_starts, flat_from)
    stop = flat_from
    while k < len(pieces) and pieces[k].file != host:
        stop = pieces[k].flat_end
        k += 1
    return stop


def _includes_in(
    gap: str,
    host: Path,
    root: Path,
    pieces: list[_Piece],
    piece_starts: list[int],
    flat_lo: int,
    flat_hi: int,
) -> list[Include]:
    """Directives sitting in the host bytes between two emitted host chunks.

    Anything flatten resolved produced no host bytes of its own, so the gap is
    exactly the directive text. An unresolvable directive never lands in a gap:
    flatten emits it as host content, where it already belongs.
    """
    found = []
    for m in _INCLUDE_RE.finditer(gap):
        target = _resolve(m.group(2).strip(), including=host, root=root)
        if target is not None:
            found.append((m.group(0), target))
    if not found:
        return []
    if len(found) == 1:
        return [Include(found[0][0], found[0][1], flat_lo, flat_hi)]

    # Two directives with no text at all between them. The expansions are split
    # at the first piece belonging to the next directive's target; a target
    # whose own first chunk is empty would be mis-split, which is recorded as a
    # known limit rather than guessed at.
    inner = pieces[
        bisect.bisect_left(piece_starts, flat_lo) : bisect.bisect_left(piece_starts, flat_hi)
    ]
    bounds = [flat_lo]
    g = 0
    for p in inner:
        if g + 1 < len(found) and p.file == found[g + 1][1]:
            g += 1
            bounds.append(p.flat_start)
    bounds.append(flat_hi)
    while len(bounds) < len(found) + 1:
        bounds.insert(-1, bounds[-2])
    return [
        Include(d, t, bounds[n], bounds[n + 1]) for n, (d, t) in enumerate(found)
    ]
