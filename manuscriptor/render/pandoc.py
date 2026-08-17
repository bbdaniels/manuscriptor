"""M2 — invoke pandoc on the flattened, anchored source.

Prose, section hierarchy, footnotes, math, and figures all survive, and
citations arrive as `<span class="citation" data-cites="key">`.

Timings, re-measured 2026-07-22 on the flattened and anchored estonia-ecm
(296KB of source, 471KB of HTML, citeproc over a 72KB bibliography):

    full render                0.83 s
    full render, no bib        0.62 s
    single block               0.032 s

The 5.5 seconds recorded earlier for this manuscript does not reproduce, on a
source three and a half times larger than the one that produced it; treat the
numbers above as the current baseline. The conclusion is unchanged and is the
reason `render_block` exists: the editor writes on a typing pause, roughly once
a second, and 0.83 s per keystroke pause is not a budget. 26x is.

A working invocation with a documentclass-swap fallback and CSL discovery
already exists at `manuscriptor/evidence/parse.py`; move it here rather than
rewriting it.

Two things about that move are worth writing down, because both look like
regressions and are not.

**The source goes in on stdin, not as a file in the manuscript directory.**
Verified 2026-07-22: pandoc emits `\\includegraphics` paths verbatim into
`<img src>` and resolves nothing, so a temp file next to the figures buys
nothing. It costs, though. That directory is under git and is watched by the
server, so a temp `.tex` written and deleted on every typing pause churns the
working tree and re-triggers the watcher that caused the render.

**The fallback is a preamble simplification, of which the documentclass swap is
one part.** Pandoc 3.1.1 does not load `.cls` files and does not fail on an
unknown class; a class name alone can never be the cause of a failure, and the
preamble is skipped wholesale, so nothing in it fails either. What does fail is
a *body* that a custom macro expands into unbalanced markup, and the cure is to
drop the definition so `raw_tex` passes the macro through untouched. Swapping
the class and dropping project-local packages are kept from the original, since
they cost nothing and the corpus is not fully explored; the macro strip is the
part that has been observed to turn a hard failure into a usable render.

And one thing that is new here rather than moved. Flattening is what makes
tables reach pandoc at all, so this is the first time pandoc has been asked to
read them, and it cannot. Six constructs break it, measured 2026-07-22 across
the corpus, and four of the six fail *silently* — exit status zero, table gone,
nothing in stderr:

    \\newcolumntype   70 estonia-ecm, 4 sdi-caseloads   hard failure on its #1
    adjustbox        16 sdi-caseloads, 4 estonia-ecm   silent
    resizebox        12 qutub-india, 7 dsp-bias        silent
    scalebox                                           silent
    threeparttable    1 estonia-ecm                    silent
    \\multicolumn{n}{m{3cm}}                            silent, whole table

Every one is a scaling, spacing, or column-width instruction, which is to say
every one is meaningless in HTML: there is nothing to preserve by keeping them
and a table to lose by not. `normalize_for_pandoc` neutralizes them on the way
in. This is the risk the Technical Notes flagged as "esttab regression table
output has not been rendered ... could invalidate M2", and it was real.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

# The column-type pair -- and the brace helpers every caller of it needs -- live
# in `tables.py`, which is their one home. A second copy of them once existed in
# the Word submission skills and had already drifted; see that module's
# docstring and `tests/test_render_tables.py`.
from manuscriptor.source.flatten import command_re, is_commented
from manuscriptor.render import graphics
from manuscriptor.render.tables import (
    declared_column_types,
    group_start,
    mark_header_rows,
    plain_multicolumn_specs,
    plain_table_specs,
    skip_group,
    skip_optional,
    strip_newcolumntypes,
)

_BASE_FLAGS = (
    "--from=latex+raw_tex",
    "--to=html5",
    "--mathml",
    "--wrap=preserve",
)

_BEGIN_DOCUMENT_RE = re.compile(r"\\begin\s*\{document\}")
_DOCUMENTCLASS_RE = re.compile(r"\\documentclass\s*(\[[^\]]*\])?\s*\{([^}]*)\}")
_USEPACKAGE_RE = re.compile(r"\\usepackage\s*(\[[^\]]*\])?\s*\{([^}]*)\}[ \t]*\n?")
_DEF_RE = re.compile(
    r"\\(?:new|renew|provide)command\s*\*?\s*\{?\s*\\[A-Za-z@]+\s*\}?"
    r"(?:\[[^\]]*\])*\s*"
)


def _live(pattern: re.Pattern[str], source: str, start: int = 0) -> re.Match[str] | None:
    """The first match of `pattern` that is real LaTeX rather than prose in a comment.

    A manuscript talks ABOUT its own commands. covet-india's `supplement.tex`
    carries a preamble comment explaining that `wlscirep.cls`'s `\\maketitle`
    always typesets `\\theabstract` -- and that mention, being the first literal
    `\\maketitle` in the file, is where `_render_frontmatter` built the title
    heading. It landed above `\\begin{document}`, which pandoc discards whole,
    so the supplement rendered with no title while the real `\\maketitle` sat
    below as an empty block. Nothing in the rewriter was wrong about titles; it
    was reading a sentence as code.

    Every scan this module runs over document source goes through here, because
    the ones that do not are exactly the ones that will read the next comment.
    The comment test itself is `flatten.is_commented`, the one scanner that
    already knows `\\%` is a literal percent sign.
    """
    for m in pattern.finditer(source, start):
        if not is_commented(source, m.start()):
            return m
    return None


# Macros whose last mandatory argument is content and whose earlier arguments
# are pure geometry. `\resizebox{w}{h}{...}`, `\scalebox{f}{...}`.
_SCALING_MACROS = {"resizebox": 2, "scalebox": 1}

# Environments that wrap content in a box and mean nothing outside of print.
# The value is how many mandatory arguments follow `\begin{name}`.
# The setspace family is here because dsp-bias wraps its whole front matter in
# `singlespace`, and an environment pandoc does not know swallows everything
# inside it: the title vanished with the line spacing.
#
# `landscape` (lscape/pdflscape) is the same failure on a whole exhibit.
# covet-india's supplement sets its widest table sideways, and the entire
# exhibit -- heading, table and note -- rendered as one empty paragraph on the
# Manuscriptor page, at exit 0, with the block still anchored so the author
# could click a sliver of nothing. Turning the page is print geometry and an
# HTML page has no orientation, so unwrapping it costs nothing.
#
# `tablenotes` is the same failure a second time, and the loudest of them.
# Pandoc drops an unknown environment that stands inside a `center` -- and
# estonia-ecm wraps every one of its tables in exactly that, so twenty tables
# rendered with their notes GONE: no stars defined, no clustering stated, no
# sample named, at exit 0 and with the table itself intact so nothing looked
# wrong. Unwrapped, the note is ordinary prose in the block that owns the
# table, which is what `render/cards.py` then folds into the card.
_WRAPPER_ENVS = {
    "adjustbox": 1, "threeparttable": 0, "tablenotes": 0,
    "singlespace": 0, "singlespacing": 0, "onehalfspace": 0,
    "doublespace": 0, "spacing": 1,
    "landscape": 0,
}


class PandocError(RuntimeError):
    """Pandoc could not parse the input, and simplifying the preamble did not
    help. Carries pandoc's own diagnosis, because a render that fails silently
    is indistinguishable from an empty manuscript."""


# ------------------------------------------------------------ full document


def render_document(flat_text: str, *, cwd: Path, bib: Path | None) -> str:
    """Full render. Falls back to `article` class when the real class fails."""
    cwd = Path(cwd)
    source = normalize_for_pandoc(flat_text, cwd=cwd)
    try:
        return _invoke(source, cwd=cwd, bib=bib)
    except PandocError as first:
        try:
            return _invoke(simplify_preamble(source), cwd=cwd, bib=bib)
        except PandocError:
            raise first from None


def extract_preamble(flat_text: str) -> str:
    """Everything up to `\\begin{document}`.

    A fragment with no `\\begin{document}` has no preamble; returning the whole
    fragment would make `render_block` wrap the block's own text around itself.
    """
    m = _live(_BEGIN_DOCUMENT_RE, flat_text)
    return flat_text[: m.start()] if m else ""


# ---------------------------------------------------------------- hot path


def render_block(block_source: str, *, preamble: str, cwd: Path) -> str:
    """Render a single block against the document preamble, for hot patching.

    The editor writes on a typing pause, so this runs at roughly one call per
    second of typing, against a full render's 0.83 s. Measured at 0.032 s on
    estonia-ecm. The preamble is threaded through rather than dropped because a
    block may call a macro the manuscript defines, and an unexpanded macro is a
    visible hole in the paragraph the author is looking at.

    No bibliography and therefore no citeproc: parsing estonia-ecm's 72KB `.bib`
    is a third of what a full render costs. Citations still come out as
    `<span class="citation" data-cites="...">` so postprocess can address them;
    only the formatted text inside the span is missing, and the full render
    supplies that.
    """
    document = f"{preamble}\n\\begin{{document}}\n{block_source}\n\\end{{document}}\n"
    return _invoke(normalize_for_pandoc(document, cwd=cwd), cwd=Path(cwd), bib=None)


# --------------------------------------------------------------- internals


def normalize_for_pandoc(source: str, *, cwd: Path | str | None = None) -> str:
    """Neutralize typesetting-only constructs pandoc cannot read.

    Not a fallback and not a repair: this always runs, because every construct
    it touches is print geometry with no HTML counterpart, and leaving any of
    them in place costs either the whole render or, worse, one table and no
    error message. Block markers, prose, math, and citations are untouched.

    `cwd` is the manuscript directory, and it is what lets an
    `\\includegraphics` be resolved against `\\graphicspath` and LaTeX's
    extension search -- the one place that resolution happens, so everything
    downstream sees an ordinary relative path (`render/graphics.py`). Without
    it nothing is resolved, which is right for the Word/compile path: that
    source is read by LaTeX itself, and LaTeX does its own searching.
    """
    source = _render_frontmatter(source)
    if cwd is not None:
        source = graphics.resolve_includes(source, cwd)
    declared = declared_column_types(source)
    source, _ = strip_newcolumntypes(source)
    source = _expand_sym(source)
    source = _hoist_equation_labels(source)
    source = _unwrap_text_outside_math(source)
    source = _flatten_stacked_cells(source)
    source = _strip_rules(source)
    source = _single_longtable_head(source)
    for name, skip in _SCALING_MACROS.items():
        source = _unwrap_macro(source, name, skip)
    for name, args in _WRAPPER_ENVS.items():
        source = _unwrap_environment(source, name, args)
    source, _ = plain_multicolumn_specs(source, declared)
    source, _ = plain_table_specs(source, declared)
    # Last, and it has to be. Every step above changes what a row looks like --
    # `_flatten_stacked_cells` removes the `\\` inside a makecell that is not a
    # row break, `_strip_rules` removes the partial rules that are not header
    # boundaries, `_single_longtable_head` collapses the head a longtable
    # declares twice, and the multicolumn rejoin puts a broken spanning cell
    # back on one line. Marking before any of them would be reading a table
    # nobody is going to render.
    source, _ = mark_header_rows(source)
    return source


# ------------------------------------------------------------- front matter
#
# Pandoc reads `\title`, `\author`, and the abstract environment into document
# METADATA, and a fragment render shows metadata nowhere. So the page opened on
# the Introduction with no title and no abstract above it, and the blocks
# carrying them rendered as empty anchors: clickable 11px slivers around
# nothing (found by the 2026-07-22 design audit). The front matter has to be
# rewritten into constructs pandoc keeps.
#
# The title becomes a starred section and the byline a plain paragraph, each
# tagged with a token that `render/postprocess.py` turns into a class, so the
# stylesheet can set them apart from a section heading. The tokens use the
# sentinel brackets because those are proven to survive pandoc, and they spell
# no hex, so `anchors.MARKER_RE` can never mistake one for a block marker.

TITLE_TOKEN = "⟦MXTITLE⟧"
BYLINE_TOKEN = "⟦MXBYLINE⟧"
ABSTRACT_TOKEN = "⟦MXABSTRACT⟧"

_MAKETITLE_RE = re.compile(r"\\maketitle\b")
# `\begin{document}` is asked for in three places in this module and had two
# spellings of the pattern until 2026-08-04. One question, one regex: where the
# body starts decides what `extract_preamble` hands the hot path, where a
# relocated abstract may land, and where `_degrade` cuts.
_ABSTRACT_BEGIN_RE = re.compile(r"\\begin\s*\{abstract\}\s*")
_ABSTRACT_END_RE = re.compile(r"\\end\s*\{abstract\}")
_THANKS_RE = re.compile(r"\\thanks\s*(?=\{)")
# The block marker standing immediately before the construct being rewritten.
# Same pattern as anchors.MARKER_RE, including the `-2` disambiguation suffix.
_MARKER_BEFORE_RE = re.compile(r"⟦MX[0-9a-f]+(?:-\d+)?⟧\s*$")
# The same thing with the marker and its trailing whitespace apart, so the
# marker can be lifted out without dragging the line break after it along.
_MARKER_ONLY_BEFORE_RE = re.compile(r"(⟦MX[0-9a-f]+(?:-\d+)?⟧)\s*$")


def _render_frontmatter(source: str) -> str:
    after_title: int | None = None
    m = _live(_MAKETITLE_RE, source)
    if m:
        title = command_text(source, "title", break_as=" ")
        if title:
            byline = _byline(source)
            # The title block's marker is HANDED OVER to the constructed
            # heading, explicitly, rather than being left to whichever marker
            # happens to stand nearest it.
            #
            # This is where the title differs from the abstract, and the
            # difference is the whole bug. The abstract is MOVED: its bytes
            # leave the preamble and arrive in the body with the marker still
            # glued to them, so adjacency keeps telling the truth. The title is
            # COPIED -- `_command_text` reads the words out of `\title{}` and
            # leaves the original command exactly where it stands -- so two
            # blocks are candidates for one `<h1>`: the title's, and
            # `\maketitle`'s, which sits at the site being rewritten. Adjacency
            # picked `\maketitle` in all six manuscripts, and clicking a paper's
            # title opened an inspector whose entire source was
            # `\flushbottom\maketitle`.
            #
            # The marker is written INSIDE the `\section*{}` argument, so pandoc
            # nests it in the heading element and `anchors.harvest` attaches it
            # there by containment rather than by proximity. That also settles
            # the loser: `postprocess._hoist_empty_anchors` refuses to overwrite
            # an element that already carries a block id, so `\maketitle`'s
            # marker stops on its own empty paragraph and becomes the void
            # anchor it should always have been.
            #
            # And it is lifted, not copied: one marker, one element. A second
            # copy left at the `\title{}` site either dies in discarded preamble
            # or -- on estonia-qbs and qutub-india, which write `\title` inside
            # the document -- anchors the empty, zero-height paragraph the
            # author could never click.
            mark, mark_at = _marker_on_title(source)
            repl = "\\section*{" + TITLE_TOKEN + mark + title + "}"
            if byline:
                repl += "\n\n" + BYLINE_TOKEN + byline + "\n"
            edits = [(m.start(), m.end(), repl)]
            if mark_at is not None:
                edits.append((mark_at[0], mark_at[1], ""))
            source, after_title = _apply_edits(source, edits, past=0)
        # No \title: nothing to show and nothing to invent. The empty block is
        # collapsed by the viewer's void pass rather than papered over here.

    m = _live(_ABSTRACT_BEGIN_RE, source)
    if m:
        end = _live(_ABSTRACT_END_RE, source, m.end())
        if end:
            body = source[m.end(): end.start()].strip()
            head = "\\subsection*{" + ABSTRACT_TOKEN + "Abstract}\n\n"
            before = source[: m.start()]
            # The block's anchor sits just before `\begin{abstract}`. The label
            # heading goes in FRONT of the marker, so the marker lands on the
            # abstract's own prose and the paragraph stays clickable; stranded
            # on the label it would select a heading around nothing.
            mk = _MARKER_BEFORE_RE.search(before)
            at = mk.start() if mk else m.start()
            # WHERE THE ABSTRACT STANDS RELATIVE TO THE TITLE, not whether it
            # stands in the preamble. An abstract written ABOVE `\maketitle` --
            # in the preamble, as covet-india does, or inside the document, as
            # qutub-ayush does -- is typeset BELOW the title by every class that
            # accepts that layout, because such a class captures the abstract
            # rather than typesetting it where it stands: `wlscirep.cls` reads
            # `\abstract` as a delimited macro into `\theabstract` and
            # `\maketitle` sets it under the byline. Render order follows the
            # compiled document, not the order of the source. Choosing on the
            # preamble alone put qutub-ayush's abstract above its own title.
            #
            # The destination is the title site when there is one, and the top
            # of the body otherwise -- and it is measured in the POST-EDIT
            # string, which is what `_apply_edits` returns `after_title` for.
            doc = _live(_BEGIN_DOCUMENT_RE, source)
            site = after_title if after_title is not None else (
                doc.end() if doc is not None else None)
            if site is not None and m.start() < site:
                source = _relocate_abstract(
                    source, at, end.end(), head, mk.group(0) if mk else "",
                    body, site,
                )
            else:
                source = (
                    before[:at] + head + before[at:]
                    + body + "\n" + source[end.end():]
                )
    return source


def _relocate_abstract(
    source: str, at: int, stop: int, head: str, mark: str, body: str, dest: int
) -> str:
    """Move an abstract written above the title down to `dest`, marker and all.

    covet-india's class (`wlscirep`) takes the abstract as a delimited macro
    read above `\\begin{document}`, so rewriting it where it stands leaves it in
    the preamble -- and pandoc discards every byte above `\\begin{document}`.
    The abstract rendered nowhere at all. qutub-ayush writes the same delimited
    macro INSIDE the document and still above `\\maketitle`, where rewriting it
    in place costs not the abstract but its position: it rendered above the
    paper's own title. One move covers both, because both are the same fact --
    the class typesets the abstract at `\\maketitle`, wherever it was written.

    The MARKER travels with the body, and that is load-bearing rather than
    tidy: the block it names still lives at its original preamble byte range,
    which is what a splice writes back to. Leave the marker behind and the
    relocated paragraph belongs to no block -- unclickable, unsplicable prose --
    while the marker itself lands in discarded preamble and the block becomes an
    anchor pointing at nothing.
    """
    piece = head + mark + body + "\n\n"
    cut = source[:at] + source[stop:]
    d = dest - (stop - at) if dest > at else dest   # dest was measured pre-cut
    return cut[:d] + "\n\n" + piece + cut[d:]


def _marker_on_title(source: str) -> tuple[str, tuple[int, int] | None]:
    """The block marker anchoring `\\title{...}`, and where to lift it from.

    `("", None)` when the title carries no marker -- a fragment, a render of one
    block, a manuscript whose `\\title` is inside a construct the segmenter did
    not cut. Then nothing is handed over and nothing is taken away.
    """
    spans = _command_spans(source, "title")
    if not spans:
        return "", None
    mk = _MARKER_ONLY_BEFORE_RE.search(source, 0, spans[0][0])
    if mk is None:
        return "", None
    return mk.group(1), mk.span(1)


def _apply_edits(
    source: str, edits: list[tuple[int, int, str]], *, past: int
) -> tuple[str, int]:
    """Apply non-overlapping `(start, stop, text)` edits, left to right.

    Returns the new text and the offset just past edit `past` IN IT, because the
    abstract relocation needs a destination measured against the string it will
    actually cut, and an offset taken before a second edit landed above it is a
    position in a document that no longer exists.
    """
    order = sorted(range(len(edits)), key=lambda i: edits[i][0])
    out: list[str] = []
    prev = 0
    end_of_past = 0
    for i in order:
        start, stop, text = edits[i]
        out.append(source[prev:start])
        out.append(text)
        prev = stop
        if i == past:
            end_of_past = sum(len(p) for p in out)
    out.append(source[prev:])
    return "".join(out), end_of_past


def _command_spans(source: str, name: str) -> list[tuple[int, int, int]]:
    """Every `\\name[opt]{...}`, as `(command start, argument start, argument end)`.

    What counts as a `\\name` is decided by `flatten.command_re` and not here.
    `source/blocks.py` asks the same question about `\\title` from the other end
    -- it cuts the title's BYTES into a block where this reads its TEXT for the
    heading -- and two answers to it means the block and the rendered heading
    stop describing the same thing, with the marker handoff between them
    silently finding nothing to hand over.
    """
    out: list[tuple[int, int, int]] = []
    for m in command_re(name).finditer(source):
        # A commented-out `\title` is a superseded title, not the paper's, and
        # `command_text` reads the FIRST span -- so without this the old title
        # left above the live one names the document everywhere.
        if is_commented(source, m.start()):
            continue
        end = skip_group(source, m.end())
        if end > m.end():
            out.append((m.start(), m.end(), end))
    return out


def document_title(source: str) -> str:
    """The paper's title as plain words, or `""`. One implementation.

    Four other spellings of this existed on 2026-08-03 -- `server/build.py` for
    the browser tab, `source/tree.py` for the document switcher,
    `evidence/render.py` for a report header, and the `\\title` reader inside
    `_render_frontmatter` -- and every one of them was a bare
    `\\title\\s*\\{(...)\\}`. All four were blind to `\\title[Short]{Long}`, and
    two stopped at the FIRST closing brace, so `\\title{A \\textbf{bold} title}`
    named the tab `A \\textbf{bold`. They did not need months to diverge; they
    were written apart and were wrong together from the start.

    Markup that survives is dropped rather than rendered: this feeds a tab, a
    switcher row and a report heading, none of which have a stylesheet.
    """
    got = command_text(source, "title", break_as=" ")
    if not got:
        return ""
    text = re.sub(r"\\[a-zA-Z]+\*?", "", got)
    return re.sub(r"\s+", " ", text.replace("{", "").replace("}", "")).strip()


def command_text(source: str, name: str, *, break_as: str = " ") -> str | None:
    """The cleaned text of a preamble command's argument, or None."""
    spans = _command_spans(source, name)
    if not spans:
        return None
    _, lo, hi = spans[0]
    return _clean_inline(source[lo + 1: hi - 1], break_as=break_as) or None


def _byline(source: str) -> str | None:
    """Every `\\author` joined, because `authblk` and `wlscirep` take one each.

    estonia-ecm writes a single `\\author{A, B and C}`; covet-india writes nine
    `\\author[n]{...}` commands. Reading only the first would put one name under
    a nine-author paper, which is a byline for a different paper.
    """
    names = [
        _clean_inline(source[lo + 1: hi - 1], break_as=", ")
        for _, lo, hi in _command_spans(source, "author")
    ]
    return ", ".join(n for n in names if n) or None


def _clean_inline(text: str, *, break_as: str = " ") -> str:
    """Reduce a `\\title`/`\\author` argument to its displayable text.

    `\\thanks{...}` is a footnote on the title page, not text of the title, so
    it is dropped with its balanced argument. A forced break is print layout:
    in a title it joins as a space, in an author list it separates names and
    joins as a comma, which is what `break_as` carries. `\\and` always
    separates authors.
    """
    out: list[str] = []
    i = 0
    while True:
        m = _THANKS_RE.search(text, i)
        if m is None:
            out.append(text[i:])
            break
        out.append(text[i: m.start()])
        i = skip_group(text, m.end())
    text = "".join(out)
    text = re.sub(r"\\and\b", ", ", text)
    text = re.sub(r"\\\\(\[[^\]]*\])?", break_as, text)
    text = re.sub(r"\s+", " ", text)
    return re.sub(r"\s+([,.;])", r"\1", text).strip(" ,")


_SYM_DEF_RE = re.compile(r"\\def\\sym\s*#1\s*\{[^\n]*\^\{#1\}[^\n]*\}[ \t]*\n?")
_SYM_CALL_RE = re.compile(r"\\sym\s*\{([^{}]*)\}")


def _expand_sym(source: str) -> str:
    """Expand esttab's significance macro, which pandoc cannot evaluate.

    esttab writes `\\def\\sym#1{\\ifmmode^{#1}\\else\\(^{#1}\\)\\fi}` and then calls
    `\\sym{***}` on every significant coefficient. The definition is a TeX
    conditional, and pandoc does not evaluate conditionals, so a literal caret
    reaches the reader and the stars break across lines. estonia-qbs uses it 55
    times, estonia-ecm in most of its table files.

    Both branches of that conditional mean the same thing, a superscript, so the
    call becomes `$^{...}$` and the definition is dropped.

    Only expanded when the document actually defines `\\sym`. Expanding a macro
    the manuscript never declared would be inventing a meaning for it.
    """
    if not _SYM_DEF_RE.search(source):
        return source
    source = _SYM_DEF_RE.sub("", source)
    return _SYM_CALL_RE.sub(lambda m: "$^{" + m.group(1) + "}$", source)


_EQUATION_RE = re.compile(r"\\begin\s*\{(equation\*?)\}(.*?)\\end\s*\{\1\}", re.S)
_LABEL_OR_TAG_RE = re.compile(r"\\(?:label|tag)\s*\{[^{}]*\}")


def _hoist_equation_labels(source: str) -> str:
    """Move `\\label` and `\\tag` to the front of an `equation` body.

    Pandoc 3.10.1 rewrote how it hands an `equation` environment to texmath,
    and the new parser accepts `\\label` or `\\tag` ONLY immediately after
    `\\begin{equation}`. One placed after the math -- which is where LaTeX
    convention puts it, and where all ten of estonia-qbs's equations put it --
    fails to parse, and the whole equation degrades to its literal source:

        <span class="math display">$$\\begin{equation}
        \\Delta \\widehat{VA}_{i} = \\alpha + \\beta \\, \\widetilde{S}_i
        \\label{eq:va-xs}
        \\end{equation}$$</span>

    Raw LaTeX, in the body of the paper, where an equation should be. Pandoc
    3.1.1 renders all ten correctly, so this is a regression rather than a
    long-standing gap.

    Worse than that, it is CONTENT DEPENDENT, which is why it cannot be left to
    an author to notice and work around. Whether the parse fails turns on the
    final token of the math body: a body ending in a brace group, a comma or a
    control sequence re-syncs and survives, one ending in a bare identifier
    does not. Of estonia-qbs's ten equations, all ten written identically, two
    broke -- the two ending in `\\varepsilon_i` rather than `\\varepsilon_{it}`.

    Hoisting rather than stripping, because the label is not this module's to
    throw away. Only `equation` and `equation*` are touched. `align`, `gather`,
    `multline` and `eqnarray` parse a trailing label correctly under both
    versions, and in those a label binds to one row, so moving it would change
    which equation it numbers.
    """

    def one(m: re.Match) -> str:
        env, body = m.group(1), m.group(2)
        found = _LABEL_OR_TAG_RE.findall(body)
        if not found:
            return m.group(0)
        rest = _LABEL_OR_TAG_RE.sub("", body)
        if not rest.strip():
            return m.group(0)
        return f"\\begin{{{env}}}{''.join(found)}{rest}\\end{{{env}}}"

    return _EQUATION_RE.sub(one, source)


_TEXT_RE = re.compile(r"\\text\s*(?=\{)")


def _unwrap_text_outside_math(source: str) -> str:
    """Unwrap `\\text{...}` where it is not inside math, keeping its content.

    `\\text` is an amsmath command and is only legal in math mode. esttab emits
    it in table cells anyway, to get a proper minus glyph (`\\text{-}0.021`) and
    for literals like `\\text{n.a.}`. Outside math, pandoc drops the command AND
    its argument, so a negative coefficient renders as positive and a literal
    disappears. Neither raises anything.

    That makes this the most dangerous normalization in the file: it is the one
    standing between the reader and a sign error in a results table.

    Inside math it is left alone, because MathJax renders it correctly and the
    upright-vs-italic distinction it carries there is real.
    """
    out: list[str] = []
    i = 0
    while True:
        m = _TEXT_RE.search(source, i)
        if m is None:
            out.append(source[i:])
            return "".join(out)
        if _in_math(source, m.start()):
            out.append(source[i : m.end()])
            i = m.end()
            continue
        end = skip_group(source, m.end())
        out.append(source[i : m.start()])
        out.append(source[m.end() + 1 : end - 1])
        i = end


def _in_math(source: str, at: int) -> bool:
    """True when `at` falls inside `$...$`, `$$...$$` or `\\(...\\)`."""
    prefix = source[:at]
    dollars = len(re.findall(r"(?<!\\)\$", prefix))
    if dollars % 2 == 1:
        return True
    opens = len(re.findall(r"\\\(", prefix))
    closes = len(re.findall(r"\\\)", prefix))
    return opens > closes


_MAKECELL_RE = re.compile(r"\\(?:makecell|thead|shortstack)\s*(?:\[[^\]]*\])?\s*(?=\{)")


def _flatten_stacked_cells(source: str) -> str:
    """Collapse a stacked cell into one line, because its `\\\\` is not a row break.

    `makecell` stacks a coefficient over its standard error inside ONE cell,
    separated by `\\\\`. Pandoc reads that as a row separator, so the row is torn
    apart: column one keeps its label, columns two onward come out empty, and
    every standard error lands on a row of its own. estonia-ecm's hospitalization
    table rendered as a single column of numbers, with no error anywhere.

    The two lines are joined with a space rather than a break. "-0.021* (0.011)"
    is how the author would read it aloud, and HTML has no equivalent of the
    stacking that would survive a table cell.
    """
    out: list[str] = []
    i = 0
    while True:
        m = _MAKECELL_RE.search(source, i)
        if m is None:
            out.append(source[i:])
            return "".join(out)
        end = skip_group(source, m.end())
        inner = source[m.end() + 1 : end - 1]
        # Only the breaks INSIDE the cell; a nested makecell is handled by
        # recursion, so its breaks are gone before this line runs.
        inner = re.sub(r"\\\\\s*", " ", _flatten_stacked_cells(inner)).strip()
        out.append(source[i : m.start()])
        out.append(inner)
        i = end


_RULE_RE = re.compile(
    r"\\(?:cmidrule|cline|specialrule|addlinespace|morecmidrules)\s*"
    r"(?:\[[^\]]*\])?\s*(?:\([^)]*\))?\s*(?:\{[^}]*\})?"
)

_LONGTABLE_RE = re.compile(r"\\begin\s*\{longtable\*?\}.*?\\end\s*\{longtable\*?\}", re.S)
_HEAD_SPAN_RE = re.compile(r"\\endfirsthead\b(.*?)\\endhead\b", re.S)


def _strip_rules(source: str) -> str:
    """Drop partial horizontal rules, which have no HTML counterpart.

    `\\cmidrule(lr){2-3}` does not merely fail to render: pandoc emits its
    arguments as table content, so every table in the reference manuscript
    carried a first body row reading `2-3 (lr)4-5`. Silent, and sitting in the
    middle of the author's regression output.

    `\\toprule`, `\\midrule` and `\\bottomrule` are deliberately NOT here.
    Pandoc reads those to find the header boundary, so removing them would cost
    the table its `<thead>`.
    """
    return _RULE_RE.sub("", source)


def _single_longtable_head(source: str) -> str:
    """Keep one header when a longtable declares two.

    longtable writes its header twice by design: the block before
    `\\endfirsthead` for page one, and the block before `\\endhead` for every
    page after. HTML has no pages, so pandoc faithfully emits both and the
    reader sees the header line doubled.

    Only touched when BOTH are present. Most of the corpus declares only
    `\\endfirsthead`, which pandoc already handles, and rewriting that would be
    a fix in search of a bug.
    """

    def one(m: re.Match) -> str:
        return _HEAD_SPAN_RE.sub(lambda _m: "\\endfirsthead\n", m.group(0), count=1)

    return _LONGTABLE_RE.sub(one, source)


def _unwrap_macro(source: str, name: str, skip: int) -> str:
    """`\\resizebox{w}{h}{TABLE}` becomes `TABLE`."""
    pattern = re.compile(r"\\" + name + r"\s*\*?\s*")
    out: list[str] = []
    cursor = 0
    while True:
        m = pattern.search(source, cursor)
        if m is None:
            out.append(source[cursor:])
            break
        at = m.end()
        for _ in range(skip):
            nxt = skip_group(source, at)
            if nxt == at:  # an optional [..] argument, or a shape we do not know
                nxt = skip_optional(source, at)
                if nxt == at:
                    break
            at = nxt
        inner_start = group_start(source, at)
        if inner_start is None:
            out.append(source[cursor:m.end()])
            cursor = m.end()
            continue
        inner_end = skip_group(source, at)
        out.append(source[cursor:m.start()])
        out.append(source[inner_start + 1:inner_end - 1])
        cursor = inner_end
    return "".join(out)


def _balanced(body: str) -> str:
    r"""Make an unwrapped body stand on its own, as a group, in both directions.

    LaTeX forgives either half of this because `\begin{env}` and `\end{env}`
    are themselves a group, and estonia-ecm's appendix tables use up both
    allowances. `\scriptsize{` is opened in every `tablenotes` and closed in
    none, and `tableE1_coding`'s notes were commented out one line at a time
    until only the closing `}` was left live. Unwrapping the environment turns
    the first into text that swallows `\end{center}` and the second into a
    brace that closes a group nobody opened; pandoc failed the entire
    manuscript on each, in turn, at line 1 of nothing the author had touched.

    So: openers with no closer get one where the `\end` stood, and closers with
    no opener are dropped. Comments are not code -- three of the four braces in
    that table's notes are behind a `%`.
    """
    depth = 0
    stray: list[int] = []
    i = 0
    while i < len(body):
        ch = body[i]
        if ch == "\\":
            i += 2
            continue
        if ch == "%":
            nl = body.find("\n", i)
            i = len(body) if nl == -1 else nl + 1
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            if depth:
                depth -= 1
            else:
                stray.append(i)
        i += 1
    for at in reversed(stray):
        body = body[:at] + body[at + 1:]
    return body + "}" * depth


def _unwrap_environment(source: str, name: str, args: int) -> str:
    """`\\begin{adjustbox}{width=...}TABLE\\end{adjustbox}` becomes `TABLE`."""
    begin = re.compile(r"\\begin\s*\{" + name + r"\}\s*")
    end = "\\end{" + name + "}"
    out: list[str] = []
    cursor = 0
    while True:
        m = begin.search(source, cursor)
        if m is None:
            out.append(source[cursor:])
            return "".join(out)
        at = skip_optional(source, m.end())
        for _ in range(args):
            at = skip_group(source, at)
        closing = source.find(end, at)
        if closing == -1:
            out.append(source[cursor:m.end()])
            cursor = m.end()
            continue
        out.append(source[cursor:m.start()])
        # AN ENVIRONMENT IS A GROUP, and unwrapping one has to keep that part.
        # estonia-ecm opens `\scriptsize{` inside every `tablenotes` and closes
        # it nowhere: LaTeX is satisfied because `\end{tablenotes}` shuts the
        # implicit group, and the author has no way to know otherwise. Drop the
        # environment without closing the brace and everything after it is
        # swallowed -- here, `\end{center}` was, and pandoc failed the WHOLE
        # document with `unexpected \end`. One unbalanced brace in one appendix
        # table, and the manuscript does not render.
        out.append(_balanced(source[at:closing]))
        cursor = closing + len(end)


def _invoke(source: str, *, cwd: Path, bib: Path | None) -> str:
    cmd = ["pandoc", *_BASE_FLAGS]
    if bib is not None:
        cmd.append("--citeproc")
        cmd.append(f"--bibliography={Path(bib).resolve()}")
        csl = find_csl(cwd)
        if csl is not None:
            cmd.append(f"--csl={csl}")
    result = subprocess.run(
        cmd, input=source, capture_output=True,
        encoding="utf-8", errors="replace", cwd=str(cwd)
    )
    if result.returncode != 0:
        raise PandocError(result.stderr.strip() or f"pandoc exited {result.returncode}")
    return result.stdout


def simplify_preamble(source: str) -> str:
    """Strip the preamble back to something pandoc cannot trip over.

    Swaps the documentclass for `article`, drops `\\usepackage` of anything, and
    drops custom macro definitions. Only the last of those has been observed to
    matter, but all three are cheap and only ever run after a failed render.

    The body is untouched. Under `raw_tex` an undefined macro survives as raw
    LaTeX rather than expanding into broken markup, so dropping a definition
    costs at most one unexpanded command and buys back the whole document.
    """
    m = _live(_BEGIN_DOCUMENT_RE, source)
    if m is None:
        return source
    preamble, body = source[: m.start()], source[m.start():]

    preamble = _DOCUMENTCLASS_RE.sub(r"\\documentclass{article}", preamble, count=1)
    preamble = _USEPACKAGE_RE.sub("", preamble)
    preamble = _strip_definitions(preamble)
    return preamble + body


def _strip_definitions(preamble: str) -> str:
    """Remove `\\newcommand`/`\\renewcommand`/`\\providecommand` and their bodies.

    The body is brace-balanced rather than regex-matched, because a macro
    definition routinely contains braces and a `[^}]*` match would cut it in
    half and leave the preamble more broken than it started.
    """
    out: list[str] = []
    cursor = 0
    while True:
        m = _DEF_RE.search(preamble, cursor)
        if m is None:
            out.append(preamble[cursor:])
            return "".join(out)
        out.append(preamble[cursor:m.start()])
        end = skip_group(preamble, m.end())
        # Eat a trailing newline so the preamble does not fill with blank lines.
        if end < len(preamble) and preamble[end] == "\n":
            end += 1
        cursor = end


# Set this to a `.csl` file to use for manuscripts that carry no style of their
# own. Read at CALL time, never at import, so a running process picks up a change
# and a test can set it without reimporting the module.
CSL_ENV = "MANUSCRIPTOR_CSL"

# The default, which is where the author's own copy lives. The environment
# variable overrides it; nothing else does.
DEFAULT_CSL = "~/.csl/econ.csl"


def find_csl(directory: Path) -> Path | None:
    """The citation style for this manuscript, or None to let pandoc choose.

    A `.csl` beside the manuscript wins, always: it is the style that document
    was written for, and it is the one a coauthor cloning the repository gets.
    `MANUSCRIPTOR_CSL` is the machine-wide answer for the manuscripts carrying no
    style of their own. With neither, it looks at `~/.csl/econ.csl` and then
    gives up, which leaves pandoc to pick its own default.

    THIS IS THE ONLY PLACE THAT ANSWERS THE QUESTION, and a guard in
    `tests/test_csl_choice.py` fails if anything re-implements it. There were
    three copies on 2026-08-11 -- here, in `server/compile.py` and in
    `evidence/parse.py` -- and they had already diverged: `parse.py` globbed
    UNSORTED, so a directory holding two `.csl` files got a filesystem-order pick
    there and a deterministic one in the other two. Same manuscript, same
    question, two answers, and the wrong one is invisible because a citation
    still renders. Adding the environment variable to one copy would have made a
    third answer.
    """
    found = sorted(Path(directory).glob("*.csl"))
    if found:
        return found[0]
    override = os.environ.get(CSL_ENV, "").strip()
    if override:
        chosen = Path(override).expanduser()
        if chosen.exists():
            return chosen
    fallback = Path(DEFAULT_CSL).expanduser()
    return fallback if fallback.exists() else None
