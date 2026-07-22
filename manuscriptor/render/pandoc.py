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

import re
import subprocess
from pathlib import Path

_BASE_FLAGS = (
    "--from=latex+raw_tex",
    "--to=html5",
    "--mathjax",
    "--wrap=preserve",
)

_BEGIN_DOCUMENT_RE = re.compile(r"\\begin\s*\{document\}")
_DOCUMENTCLASS_RE = re.compile(r"\\documentclass\s*(\[[^\]]*\])?\s*\{([^}]*)\}")
_USEPACKAGE_RE = re.compile(r"\\usepackage\s*(\[[^\]]*\])?\s*\{([^}]*)\}[ \t]*\n?")
_DEF_RE = re.compile(
    r"\\(?:new|renew|provide)command\s*\*?\s*\{?\s*\\[A-Za-z@]+\s*\}?"
    r"(?:\[[^\]]*\])*\s*"
)

_NEWCOLUMNTYPE_RE = re.compile(r"\\newcolumntype\s*\{[^}]*\}\s*(?:\[[^\]]*\])?\s*")
_MULTICOLUMN_RE = re.compile(r"\\multicolumn\s*\{[^}]*\}\s*(?=\{)")

# Table environments, and how many mandatory arguments stand between
# `\begin{...}` and the column specification. `tabular*` and `tabularx` take a
# target width first; the rest go straight to the spec.
_TABLE_ENVS = {
    "tabular": 0,
    "longtable": 0,
    "supertabular": 0,
    "tabular*": 1,
    "tabularx": 1,
    "xltabular": 1,
}
_TABLE_BEGIN_RE = re.compile(
    r"\\begin\s*\{("
    + "|".join(re.escape(n) for n in sorted(_TABLE_ENVS, key=len, reverse=True))
    + r")\}\s*"
)

# Macros whose last mandatory argument is content and whose earlier arguments
# are pure geometry. `\resizebox{w}{h}{...}`, `\scalebox{f}{...}`.
_SCALING_MACROS = {"resizebox": 2, "scalebox": 1}

# Environments that wrap content in a box and mean nothing outside of print.
# The value is how many mandatory arguments follow `\begin{name}`.
_WRAPPER_ENVS = {"adjustbox": 1, "threeparttable": 0}

# LaTeX column types reduced to the alignment pandoc can actually express.
_ALIGN = {"c": "c", "l": "l", "r": "r", "m": "c", "p": "l", "b": "l", "X": "l"}


class PandocError(RuntimeError):
    """Pandoc could not parse the input, and simplifying the preamble did not
    help. Carries pandoc's own diagnosis, because a render that fails silently
    is indistinguishable from an empty manuscript."""


# ------------------------------------------------------------ full document


def render_document(flat_text: str, *, cwd: Path, bib: Path | None) -> str:
    """Full render. Falls back to `article` class when the real class fails."""
    cwd = Path(cwd)
    source = normalize_for_pandoc(flat_text)
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
    m = _BEGIN_DOCUMENT_RE.search(flat_text)
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
    return _invoke(normalize_for_pandoc(document), cwd=Path(cwd), bib=None)


# --------------------------------------------------------------- internals


def normalize_for_pandoc(source: str) -> str:
    """Neutralize typesetting-only constructs pandoc cannot read.

    Not a fallback and not a repair: this always runs, because every construct
    it touches is print geometry with no HTML counterpart, and leaving any of
    them in place costs either the whole render or, worse, one table and no
    error message. Block markers, prose, math, and citations are untouched.
    """
    declared = _declared_column_types(source)
    source = _strip_newcolumntypes(source)
    for name, skip in _SCALING_MACROS.items():
        source = _unwrap_macro(source, name, skip)
    for name, args in _WRAPPER_ENVS.items():
        source = _unwrap_environment(source, name, args)
    source = _plain_multicolumn_specs(source, declared)
    return _plain_table_specs(source, declared)


def _declared_column_types(source: str) -> dict[str, str]:
    """Read the alignment out of each `\\newcolumntype` before dropping it.

    Guessing would be wrong on the reference manuscript, which redefines `r` as
    ragged-*right*, i.e. left-aligned. The declaration says so; there is no
    reason to infer what the source states.
    """
    declared: dict[str, str] = {}
    for m in re.finditer(r"\\newcolumntype\s*\{([^}]*)\}\s*(?:\[[^\]]*\])?", source):
        name = m.group(1).strip()
        if len(name) != 1:
            continue
        end = _skip_group(source, m.end())
        body = source[m.end():end]
        if "\\centering" in body:
            declared[name] = "c"
        elif "\\raggedleft" in body:
            declared[name] = "r"
        elif "\\raggedright" in body:
            declared[name] = "l"
        else:
            base = re.search(r"\\?\b([pmbclr])\s*\{", body)
            declared[name] = _ALIGN.get(base.group(1), "l") if base else "l"
    return declared


def _strip_newcolumntypes(source: str) -> str:
    """`\\newcolumntype{m}[1]{>{\\centering}p{#1}}` — the `#1` is what breaks it."""
    out: list[str] = []
    cursor = 0
    while True:
        m = _NEWCOLUMNTYPE_RE.search(source, cursor)
        if m is None:
            out.append(source[cursor:])
            return "".join(out)
        out.append(source[cursor:m.start()])
        end = _skip_group(source, m.end())
        if end < len(source) and source[end] == "\n":
            end += 1
        cursor = end


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
            nxt = _skip_group(source, at)
            if nxt == at:  # an optional [..] argument, or a shape we do not know
                nxt = _skip_optional(source, at)
                if nxt == at:
                    break
            at = nxt
        inner_start = _group_start(source, at)
        if inner_start is None:
            out.append(source[cursor:m.end()])
            cursor = m.end()
            continue
        inner_end = _skip_group(source, at)
        out.append(source[cursor:m.start()])
        out.append(source[inner_start + 1:inner_end - 1])
        cursor = inner_end
    return "".join(out)


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
        at = _skip_optional(source, m.end())
        for _ in range(args):
            at = _skip_group(source, at)
        closing = source.find(end, at)
        if closing == -1:
            out.append(source[cursor:m.end()])
            cursor = m.end()
            continue
        out.append(source[cursor:m.start()])
        out.append(source[at:closing])
        cursor = closing + len(end)


def _plain_multicolumn_specs(source: str, declared: dict[str, str]) -> str:
    """`\\multicolumn{2}{m{3.4cm}}{X}` becomes `\\multicolumn{2}{c}{X}`."""
    out: list[str] = []
    cursor = 0
    while True:
        m = _MULTICOLUMN_RE.search(source, cursor)
        if m is None:
            out.append(source[cursor:])
            return "".join(out)
        spec_end = _skip_group(source, m.end())
        if spec_end == m.end():
            out.append(source[cursor:m.end()])
            cursor = m.end()
            continue
        spec = source[m.end() + 1:spec_end - 1]
        out.append(source[cursor:m.end()])
        out.append("{" + _plain_colspec(spec, declared) + "}")
        cursor = spec_end


def _plain_table_specs(source: str, declared: dict[str, str]) -> str:
    """`\\begin{tabular}{R{4cm} M{2cm}}` becomes `\\begin{tabular}{lc}`.

    This one is load-bearing rather than tidy. `R` and `M` come from the
    `\\newcolumntype` declarations stripped above, and a column type pandoc does
    not recognise makes it drop the entire table with exit status zero. Removing
    the declarations without also reducing the specs that used them would trade
    a loud failure for a silent one, which is the worse of the two.
    """
    out: list[str] = []
    cursor = 0
    while True:
        m = _TABLE_BEGIN_RE.search(source, cursor)
        if m is None:
            out.append(source[cursor:])
            return "".join(out)
        at = _skip_optional(source, m.end())
        for _ in range(_TABLE_ENVS[m.group(1)]):
            at = _skip_group(source, at)
        spec_end = _skip_group(source, at)
        if spec_end == at:
            out.append(source[cursor:m.end()])
            cursor = m.end()
            continue
        start = _group_start(source, at)
        out.append(source[cursor:start])
        out.append("{" + _plain_colspec(source[start + 1:spec_end - 1], declared) + "}")
        cursor = spec_end


def _plain_colspec(spec: str, declared: dict[str, str] | None = None) -> str:
    """Reduce a column specification to alignment letters and rules.

    Pandoc's HTML tables carry `text-align` and nothing else, so width, font,
    and inter-column material are all noise. The reduction has to be a parse
    rather than a substitution: `>{\\centering\\arraybackslash}p{4cm}` contains
    the letters c, r, and l inside a command name, and a character filter would
    read that single column as four.
    """
    out: list[str] = []
    i = 0
    while i < len(spec):
        c = spec[i]
        if c in "<>@!*":
            nxt = _skip_group(spec, i + 1)
            i = nxt if nxt > i + 1 else i + 1
            continue
        if c == "[":
            i = _skip_optional(spec, i)
            continue
        if c == "|":
            out.append("|")
            i += 1
            continue
        if c.isalpha():
            after = _skip_optional(spec, i + 1)
            after = _skip_group(spec, after)
            out.append((declared or {}).get(c) or _ALIGN.get(c, "l"))
            i = max(after, i + 1)
            continue
        i += 1
    return "".join(out) or "l"


def _group_start(text: str, at: int) -> int | None:
    i = at
    while i < len(text) and text[i] in " \t\n":
        i += 1
    return i if i < len(text) and text[i] == "{" else None


def _skip_optional(text: str, at: int) -> int:
    i = at
    while i < len(text) and text[i] in " \t":
        i += 1
    if i >= len(text) or text[i] != "[":
        return at
    depth = 0
    while i < len(text):
        if text[i] == "[":
            depth += 1
        elif text[i] == "]":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return at


def _invoke(source: str, *, cwd: Path, bib: Path | None) -> str:
    cmd = ["pandoc", *_BASE_FLAGS]
    if bib is not None:
        cmd.append("--citeproc")
        cmd.append(f"--bibliography={Path(bib).resolve()}")
        csl = _find_csl(cwd)
        if csl is not None:
            cmd.append(f"--csl={csl}")
    result = subprocess.run(
        cmd, input=source, capture_output=True, text=True, cwd=str(cwd)
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
    m = _BEGIN_DOCUMENT_RE.search(source)
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
        end = _skip_group(preamble, m.end())
        # Eat a trailing newline so the preamble does not fill with blank lines.
        if end < len(preamble) and preamble[end] == "\n":
            end += 1
        cursor = end


def _skip_group(text: str, at: int) -> int:
    """Return the offset just past the brace group starting at or after `at`.

    Comments are skipped, because LaTeX skips them and flattening does not
    strip them. A `%` line carrying a lone brace is legal source, and counting
    it would close a wrapper somewhere in the middle of the table it wraps.
    """
    i = at
    while i < len(text) and text[i] in " \t":
        i += 1
    if i >= len(text) or text[i] != "{":
        return at
    depth = 0
    while i < len(text):
        c = text[i]
        if c == "\\":
            i += 2
            continue
        if c == "%":
            nl = text.find("\n", i)
            i = len(text) if nl == -1 else nl + 1
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return len(text)


def _find_csl(directory: Path) -> Path | None:
    candidates = sorted(directory.glob("*.csl"))
    if candidates:
        return candidates[0]
    global_csl = Path.home() / ".csl" / "econ.csl"
    return global_csl if global_csl.exists() else None
