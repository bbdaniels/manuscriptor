"""M2 — resolve \\ref and \\pageref out of the compiled .aux.

Pandoc leaves cross-references as raw LaTeX; estonia-ecm leaked 59 of them. The
`pandoc-docx` skill already carries a working recipe for reading `main.aux`,
so reuse it rather than reinventing.

Resolution needs a compiled `.aux`, which a never-compiled manuscript will not
have. That case must degrade visibly (a marked unresolved reference) rather
than silently, so the author can tell the difference between a broken reference
and an uncompiled one.

Two shapes matter on the way out, and only one of them is obvious.

Verified against pandoc 3.1.1 on 2026-07-22: under `--from=latex+raw_tex` a
reference does not arrive as bare text. It arrives inside a MathJax span,

    <span class="math inline">\\(\\ref{tab:foo}\\)</span>

so a substitution that only walks text finds nothing. When the span holds
nothing but the reference it is unwrapped, because leaving a bare number inside
`\\(...\\)` would typeset a section number in italic math. When the reference
sits inside larger math it is substituted in place and the span survives.

The same run established that pandoc *discards* `\\pageref` outright, in both
`latex` and `latex+raw_tex` mode, since an HTML page has no page numbers. It is
still handled here, because this function is also the resolver for markup that
did not come through pandoc, but nothing in the current corpus uses it: `\\ref`
is the only cross-reference macro any of the six test manuscripts calls.

Page numbers ride in the same `dict[str, str]` under a `@@page` suffix. LaTeX
labels routinely contain `@` (hyperref writes `sub@subfig:...`), but never two
in a row, so the sentinel cannot collide with a real label.
"""
from __future__ import annotations

import re
from pathlib import Path

PAGE_SUFFIX = "@@page"

# Macros this module can print correctly. `\autoref` and `\cref` are excluded
# deliberately: they print a type name ("Table 2") that the aux entry does not
# carry, so resolving them here would invent the wrong word.
_REF_MACROS = ("ref", "pageref", "eqref")

_MACRO_RE = re.compile(r"\\(" + "|".join(_REF_MACROS) + r")\*?\s*\{([^}]*)\}")

_MATH_SPAN_RE = re.compile(
    r'<span class="math (inline|display)">(.*?)</span>', re.DOTALL
)

# Pandoc's other shape, emitted when `raw_tex` is off.
_REF_ANCHOR_RE = re.compile(
    r'(<a\b[^>]*\bdata-reference="([^"]+)"[^>]*>)(.*?)(</a>)', re.DOTALL
)

_MATH_DELIMS = (("\\(", "\\)"), ("\\[", "\\]"))

# One left-to-right scan over the three shapes, so nothing this function writes
# is ever read back. The marker emitted for an unresolved reference still
# contains a literal `\ref{...}`, and a second pass would wrap it again.
_SCAN_RE = re.compile(
    "(?P<math>" + _MATH_SPAN_RE.pattern + ")"
    "|(?P<anchor>" + _REF_ANCHOR_RE.pattern + ")"
    "|(?P<bare>" + _MACRO_RE.pattern + ")",
    re.DOTALL,
)


# ------------------------------------------------------------------ aux side


def load_labels(aux: Path) -> dict[str, str]:
    """Read `\\newlabel` entries out of a LaTeX .aux, following `\\@input`.

    Returns label to printed number, plus `label@@page` to printed page. A
    missing or unreadable file yields an empty mapping rather than raising: a
    manuscript that has never been compiled has no aux, and that has to surface
    as "every reference unresolved", which the author can see.
    """
    labels: dict[str, str] = {}
    _read_aux(Path(aux), labels, seen=set())
    return labels


def _read_aux(aux: Path, labels: dict[str, str], seen: set[Path]) -> None:
    try:
        resolved = aux.resolve()
    except OSError:
        return
    if resolved in seen:
        return
    seen.add(resolved)
    try:
        text = resolved.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return

    for m in re.finditer(r"\\newlabel\s*", text):
        key, after = _balanced(text, m.end())
        if key is None:
            continue
        value, _ = _balanced(text, after)
        if value is None:
            continue
        fields = _fields(value)
        if not fields:
            continue
        number = _printable(fields[0])
        if number:
            labels[key] = number
        if len(fields) > 1:
            page = _printable(fields[1])
            if page:
                labels[key + PAGE_SUFFIX] = page

    for m in re.finditer(r"\\@input\s*", text):
        child, _ = _balanced(text, m.end())
        if child:
            _read_aux(resolved.parent / child.strip(), labels, seen)


def _balanced(text: str, at: int) -> tuple[str | None, int]:
    """Read one brace group starting at or after `at`. Returns (inner, end)."""
    i = at
    while i < len(text) and text[i] in " \t\n\r":
        i += 1
    if i >= len(text) or text[i] != "{":
        return None, at
    depth = 0
    start = i
    while i < len(text):
        c = text[i]
        if c == "\\":
            i += 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start + 1:i], i + 1
        i += 1
    return None, at


def _fields(value: str) -> list[str]:
    """Split a `\\newlabel` value block into its top-level brace groups."""
    out: list[str] = []
    i = 0
    while i < len(value):
        if value[i] == "{":
            inner, end = _balanced(value, i)
            if inner is None:
                break
            out.append(inner)
            i = end
            continue
        i += 1
    return out


_STRIP_CMD_RE = re.compile(r"\\[A-Za-z@]+\s*")


def _printable(field: str) -> str:
    """Reduce an aux field to the string LaTeX would have typeset.

    Real entries carry markup (`\\textbf {Consultations}`), grouping
    (`{{A}.1}`), and spacing commands (`\\relax 2.1`). None of that belongs in
    an HTML page, and leaving any of it in would ship visible LaTeX.
    """
    text = _STRIP_CMD_RE.sub("", field)
    text = text.replace("{", "").replace("}", "")
    return " ".join(text.split())


# ----------------------------------------------------------------- html side


def resolve(html: str, labels: dict[str, str]) -> tuple[str, list[str]]:
    """Substitute references; return the html and any keys left unresolved.

    Unresolved keys come back in document order, once each. They are also left
    visible in the output, wrapped in `<span class="ref-unresolved">`, so the
    page shows which label is missing rather than a silent `??` or, worse, a
    MathJax `???` from a `\\ref` nobody stripped.
    """
    unresolved: list[str] = []

    def note(key: str) -> None:
        if key not in unresolved:
            unresolved.append(key)

    out: list[str] = []
    cursor = 0
    for m in _SCAN_RE.finditer(html):
        out.append(html[cursor:m.start()])
        out.append(_replace(m.group(0), labels, note))
        cursor = m.end()
    out.append(html[cursor:])
    return "".join(out), unresolved


def _replace(fragment: str, labels: dict[str, str], note) -> str:
    """Dispatch one matched fragment to the handler for its shape."""
    math = _MATH_SPAN_RE.fullmatch(fragment)
    if math is not None:
        return _resolve_math_span(math, labels, note)
    anchor = _REF_ANCHOR_RE.fullmatch(fragment)
    if anchor is not None:
        return _resolve_anchor(anchor, labels, note)
    bare = _MACRO_RE.fullmatch(fragment)
    if bare is not None:
        return _resolve_bare(bare, labels, note)
    return fragment


def _lookup(macro: str, key: str, labels: dict[str, str]) -> str | None:
    if macro == "pageref":
        value = labels.get(key + PAGE_SUFFIX)
        return value
    value = labels.get(key)
    if value is None:
        return None
    return f"({value})" if macro == "eqref" else value


def _resolve_math_span(m: re.Match, labels, note) -> str:
    body = m.group(2)
    if not _MACRO_RE.search(body):
        return m.group(0)

    inner, opener, closer = _unwrap_math(body)

    # The whole span is one reference: unwrap it so the number reads as prose
    # rather than as italic math.
    sole = _MACRO_RE.fullmatch(inner.strip())
    if sole is not None:
        macro, key = sole.group(1), sole.group(2).strip()
        value = _lookup(macro, key, labels)
        if value is None:
            note(key)
            return _unresolved_span(macro, key)
        return value

    # A reference inside larger math: substitute in place, keep the span. An
    # unresolved one has to stay as LaTeX here, since HTML cannot be injected
    # into a math run.
    def sub(mm: re.Match) -> str:
        macro, key = mm.group(1), mm.group(2).strip()
        value = _lookup(macro, key, labels)
        if value is None:
            note(key)
            return mm.group(0)
        return value

    return f'<span class="math {m.group(1)}">{opener}{_MACRO_RE.sub(sub, inner)}{closer}</span>'


def _unwrap_math(body: str) -> tuple[str, str, str]:
    stripped = body.strip()
    for opener, closer in _MATH_DELIMS:
        if stripped.startswith(opener) and stripped.endswith(closer):
            return stripped[len(opener):-len(closer)], opener, closer
    return body, "", ""


def _resolve_anchor(m: re.Match, labels, note) -> str:
    key = m.group(2)
    kind = "ref"
    kind_match = re.search(r'data-reference-type="([^"]+)"', m.group(1))
    if kind_match and kind_match.group(1) in _REF_MACROS:
        kind = kind_match.group(1)
    value = _lookup(kind, key, labels)
    if value is None:
        note(key)
        return m.group(0)
    return f"{m.group(1)}{value}{m.group(4)}"


def _resolve_bare(m: re.Match, labels, note) -> str:
    macro, key = m.group(1), m.group(2).strip()
    value = _lookup(macro, key, labels)
    if value is None:
        note(key)
        return _unresolved_span(macro, key)
    return value


def _unresolved_span(macro: str, key: str) -> str:
    """Keep the reference on the page, named, and marked."""
    return (
        f'<span class="ref-unresolved" data-ref="{key}" '
        f'title="no \\label{{{key}}} in the compiled .aux">\\{macro}{{{key}}}</span>'
    )
