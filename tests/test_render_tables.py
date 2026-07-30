r"""LaTeX column-type normalization is decided in one module.

The reason this file exists: the operation was implemented twice -- here, and
standalone inside the Word submission skills. Diffed on 2026-07-29 over 3,477
`.tex` files -- five manuscript repositories plus the local papers archive -- the
two agreed on the declared alignment map everywhere and disagreed on the output
of eight files. All eight were the same defect, and it was THIS repository's copy
that carried it:
a `\multicolumn` whose content group sat on the next line was left alone, and
pandoc answered estonia-ecm's balance table with 29 bytes of nothing, at exit 0.

A guard that only checked `render/tables.py` returned the right answer would not
have caught that, because the other copy also returned the right answer -- on all
but eight files. What matters is that nobody else implements it at all.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from manuscriptor.render import tables

SRC = Path(__file__).resolve().parent.parent / "manuscriptor"
CANONICAL = SRC / "render" / "tables.py"

# The Word submission skills import from here rather than carrying a copy. They
# live outside this repository, so the guard covers them when they are present
# on the machine and the guards above still stand on their own when they are not.
SKILLS = Path.home() / ".claude" / "skills" / "submission"

# The scan is not confined to `submission/`, because the next re-implementation
# has no reason to land there. Any skill that touches LaTeX is a plausible home
# for one, and the whole point of the collapse is that NOBODY else implements it.
SKILLS_ROOT = Path.home() / ".claude" / "skills"


def _sources() -> list[Path]:
    return sorted(p for p in SRC.rglob("*.py") if p != CANONICAL)


def _strip_comments(text: str) -> str:
    """Drop `#` comments and docstrings.

    A guard that trips on the prose explaining why the logic is absent is a
    guard only silence can satisfy -- the mistake made once already in the
    `representedFilename` check on 2026-07-26. Every module that CALLS this
    normalization has good reason to name `\\newcolumntype` while explaining
    itself, and none of them are re-implementing it by doing so.
    """
    text = re.sub(r'"""(?:.|\n)*?"""', "", text)
    text = re.sub(r"'''(?:.|\n)*?'''", "", text)
    return "\n".join(line.split("#", 1)[0] for line in text.splitlines())


def _squash(body: str) -> str:
    """Strip everything that merely DECORATES the code, quoting included.

    Same lesson as `test_paths._squash`: the first version of that guard matched
    double quotes only, so a single-quoted spelling of the same path slipped past
    it for months. A guard a change of quote character defeats proves nothing.
    Quote characters, their string prefixes, and all whitespace come out.
    """
    return re.sub(
        r"""(?<![A-Za-z0-9_])[rbufRBUF]{1,2}['"]|['"]|\s+""", "", body)


# Two signatures of a re-implementation, each sufficient on its own.
#
# The first is the declaration parser. What separates it from prose is not the
# word but what is DONE with it: an implementation hands `newcolumntype` to a
# regex or to a string operation that cuts the source at it, while everything
# else merely names it.
#
# Two false positives shaped this, and both are worth keeping in mind before
# widening it again. Keying on the following character -- a `{` or a `\s*` --
# fired on `assert_no_raw_latex.py`, which PRINTS "A `\newcolumntype{R}[1]{...}`
# was stripped but the specs" to tell an author what went wrong. Adding
# "mentions raggedright and raggedleft and newcolumntype" as a third signature
# fired on `source/blocks.py`, which lists all three among the LaTeX commands it
# skips over and knows nothing about column types. Neither re-implements
# anything, and a guard that a diagnostic message or a keyword list trips is a
# guard that gets switched off.
_PARSER_RE = re.compile(
    r"(?:re\.(?:compile|finditer|search|sub|match|split|findall)|"
    r"\.(?:replace|split|find|index|partition))\([^)]{0,80}?newcolumntype")

# The second is the letter -> alignment table, which the rewrite half cannot be
# written without. Only the non-identity entries count: `m` means centered and
# `p`, `b` and `X` mean left, and nothing else in this codebase needs to know
# that. Both `{"m": "c"}` and `dict(m="c")` spell the same entry.
#
# The entry must be anchored to a container boundary -- `{`, `,`, `(` or `[` --
# because after `_squash` a bare two-character substring matches far too much.
# Scanned over the whole skills tree, `add-citation/scripts/citekit.py` hit the
# threshold on `item=cr` (reading as `m=c`) and `pdf_kb:len(...)` (as `b:l`) and
# was reported as re-implementing column normalization. It knows nothing about
# LaTeX. Widening the scan is only safe once the detector cannot be tripped by
# an incidental substring, and a guard that cries wolf is one that gets
# switched off -- so the false positive is the defect, not the scope.
#
# The optional `]` catches the entry assigned one at a time: `d["m"] = "c"`
# squashes to `d[m]=c`, and a pattern that only allowed `{m:c` walked straight
# past it. That gap was found by watching this guard fail on a stub, which is
# the only way such a gap is ever found.
_ALIGN_ENTRIES = (("m", "c"), ("p", "l"), ("b", "l"), ("X", "l"))
_ALIGN_RES = tuple(
    re.compile(r"[{,(\[]%s\]?[:=]%s" % (k, v)) for k, v in _ALIGN_ENTRIES)


def _reimplements(body: str) -> str | None:
    squashed = _squash(_strip_comments(body))
    if _PARSER_RE.search(squashed):
        return "parses a \\newcolumntype declaration"
    if sum(bool(r.search(squashed)) for r in _ALIGN_RES) >= 2:
        return "carries its own column-letter alignment map"
    return None


def test_only_render_tables_implements_the_column_type_pair():
    offenders = []
    for path in _sources():
        why = _reimplements(path.read_text(encoding="utf-8"))
        if why is not None:
            offenders.append(f"{path.relative_to(SRC.parent)} ({why})")
    assert offenders == [], (
        "LaTeX column-type normalization is implemented outside "
        "manuscriptor/render/tables.py: " + "; ".join(offenders)
    )


@pytest.mark.parametrize("stub", [
    r'_RE = re.compile(r"\\newcolumntype\s*\{([^}]*)\}")',
    r"_RE = re.compile(r'\\newcolumntype\s*\{([^}]*)\}')",
    r'for m in re.finditer("\\\\newcolumntype\\s*\\{", source):',
    r'source = source.replace("\\newcolumntype{m}", "")',
    r'head, _, rest = source.split("\\newcolumntype")',
    r'ALIGN = {"c": "c", "l": "l", "r": "r", "m": "c", "p": "l", "b": "l", "X": "l"}',
    r"ALIGN = {'m': 'c', 'p': 'l', 'b': 'l', 'X': 'l'}",
    r'ALIGN = dict(m="c", p="l", b="l", X="l")',
    # Assigned one entry at a time rather than as a literal.
    r'A = {}\nA["m"] = "c"\nA["p"] = "l"',
    # A scan that never reaches for `re` at all.
    r'for d in source.split("\\newcolumntype")[1:]:'
    r'\n    a = "l" if "\\raggedright" in d else "r" if "\\raggedleft" in d else "c"',
])
def test_the_guard_catches_a_stub_duplicate(stub):
    """Watch it fire on every shape, so it can never again pass on a typo."""
    assert _reimplements(stub) is not None, stub


@pytest.mark.parametrize("innocent", [
    '"a table using a \\newcolumntype degrades to prose at exit 0"',
    'raise SystemExit("without it \\\\newcolumntype tables abort")',
    # The message that tripped the first draft of this guard, verbatim.
    'print("  1. A `\\\\newcolumntype{R}[1]{...}` was stripped but the specs")',
    'if "newcolumntype" not in text:',
    'declared = declared_column_types(source)',
    '_TABULAR_RE = re.compile(r"\\\\(?:toprule|multicolumn|multirow)\\b")',
    'ALIGN = {"left": "l"}',
    'STYLES = {"m": "c"}',
    # source/blocks.py, which skips layout commands and knows no column types.
    'SKIP = ("centering", "raggedright", "raggedleft", "clearpage")',
    # add-citation/scripts/citekit.py, which tripped the threshold on two
    # incidental substrings while knowing nothing about LaTeX at all. Both are
    # verbatim, and together they are what the anchoring exists to reject.
    'item = cr["message"]\nreturn {"pdf_kb": len(pdf_bytes) // 1024}',
])
def test_the_guard_does_not_trip_on_prose_or_an_unrelated_map(innocent):
    assert _reimplements(innocent) is None, innocent


@pytest.mark.skipif(not SKILLS_ROOT.is_dir(), reason="no skills tree installed")
def test_the_skills_carry_no_copy_of_their_own():
    """The other half of the collapse, checked where the copy actually was.

    `pandoc-docx/scripts/tex_tables.py` is a re-export and must stay one. A
    machine without Manuscriptor has to fail loudly rather than skip the
    normalization, because a skipped normalization ships silently degraded
    tables into the .docx a journal submission is built from.

    The scan covers the whole skills tree, not just `submission/`. The copy that
    existed lived there, but the next one has no reason to: any skill that
    touches LaTeX is a plausible home, and the guarantee being defended is that
    nobody else implements this at all.
    """
    offenders = []
    for path in sorted(SKILLS_ROOT.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        why = _reimplements(path.read_text(encoding="utf-8", errors="replace"))
        if why is not None:
            offenders.append(f"{path} ({why})")
    assert offenders == [], (
        "a skill re-implements column-type normalization instead of importing "
        "manuscriptor.render.tables: " + "; ".join(offenders)
    )


@pytest.mark.skipif(not (SKILLS / "pandoc-docx" / "scripts" / "tex_tables.py").is_file(),
                    reason="pandoc-docx skill not installed")
def test_the_skill_entry_point_resolves_to_this_module():
    """Not merely "no copy present": the names must resolve back here."""
    import importlib.util

    path = SKILLS / "pandoc-docx" / "scripts" / "tex_tables.py"
    spec = importlib.util.spec_from_file_location("_skill_tex_tables", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for name in ("normalize_tables", "declared_column_types", "plain_colspec",
                 "count_table_environments", "expected_alignments"):
        assert getattr(mod, name).__module__ == "manuscriptor.render.tables", name


# ------------------------------------------------------------ the behavior
#
# Ported from the standalone module's `--selftest` when the copies were
# collapsed. They live in the suite rather than in a hand-run CLI, because a
# check nobody runs is a check that does not exist.

_R_TRAP = (
    "\\newcolumntype{r}[1]{>{\\raggedright\\arraybackslash}p{#1}}\n"
    "\\begin{tabular}{r{4.0cm} c}\nA & B \\\\\n\\end{tabular}\n"
)

_LONGTABLE = (
    "\\newcolumntype{R}[1]{>{\\raggedright\\arraybackslash}p{#1}}\n"
    "\\newcolumntype{M}[1]{>{\\centering\\arraybackslash}p{#1}}\n"
    "\\begin{longtable}{R{4.0cm} M{2.2cm} M{2.2cm}}\n"
    "A & B & C \\\\\n\\end{longtable}\n"
)


def test_a_redefined_r_comes_out_left_aligned():
    """The trap. estonia-ecm redefines the BUILT-IN `r` as ragged-*right*.

    Strip the declaration alone and `r` is still a legal pandoc column letter,
    so the column renders right-aligned -- backwards, silently, at exit 0, with
    the table intact so no structural guard fires.
    """
    out, stats = tables.normalize_tables(_R_TRAP)
    assert stats["declared"]["r"] == "l"
    assert "\\begin{tabular}{lc}" in out
    assert re.search(r"\\begin\{tabular\}\{[^}]*r", out) is None
    assert "newcolumntype" not in out


def test_stripping_alone_leaves_the_backwards_alignment_behind():
    """The regression the pair exists to prevent, watched failing."""
    stripped_only, _ = tables.strip_newcolumntypes(_R_TRAP)
    assert "\\begin{tabular}{r{4.0cm} c}" in stripped_only


def test_custom_letters_in_a_longtable_spec_are_reduced():
    out, stats = tables.normalize_tables(_LONGTABLE)
    assert "\\begin{longtable}{lcc}" in out
    assert stats["declarations"] == 2
    assert stats["table_specs"] == 1


@pytest.mark.parametrize("spec, expected", [
    ("l*{1}{ccccc}", "lccccc"),      # esttab's default; pandoc reads no table
    ("*{3}{lc}", "lclclc"),          # dropping the multiplier discards columns
    (">{\\centering\\arraybackslash}p{4cm}", "l"),  # a parse, not a filter
    ("m{2cm}", "c"),                 # undeclared m keeps its builtin meaning
    ("|l|c|", "|l|c|"),              # rules are alignment-adjacent, keep them
])
def test_a_column_spec_reduces_to_alignment_letters(spec, expected):
    assert tables.plain_colspec(spec) == expected


def test_a_declared_letter_beats_the_builtin():
    assert tables.plain_colspec("m", {"m": "l"}) == "l"


def test_a_width_bearing_multicolumn_spec_is_reduced():
    out, n = tables.plain_multicolumn_specs("\\multicolumn{2}{m{3.4cm}}{X}", {})
    assert out == "\\multicolumn{2}{c}{X}"
    assert n == 1


def test_a_multicolumn_broken_across_a_line_is_rejoined():
    """The divergence that cost estonia-ecm its balance table.

    Pandoc's reader will not carry a `\\multicolumn` across a newline between
    its spec and its content: the whole table comes out as a `Div` + `Para` at
    exit 0, while the identical row on one line parses fine.
    """
    out, _ = tables.plain_multicolumn_specs(
        "A & \\multicolumn{2}{c}\n{\\textbf{M}} & D", {})
    assert out == "A & \\multicolumn{2}{c}{\\textbf{M}} & D"


@pytest.mark.parametrize("text", [
    "\\multicolumn{2}{c}{M} next",   # already on one line
    "\\multicolumn{2}{c} {M}",       # a space is a cell gap, not a broken arg
])
def test_a_multicolumn_that_is_not_broken_is_left_alone(text):
    out, _ = tables.plain_multicolumn_specs(text, {})
    assert out == text


def test_a_tabularx_width_argument_is_not_read_as_the_column_list():
    out, _ = tables.normalize_tables(
        "\\begin{tabularx}{\\textwidth}{lXX}\n\\end{tabularx}")
    assert "\\begin{tabularx}{\\textwidth}{lll}" in out


def test_a_declaration_without_an_arity_strips_cleanly_too():
    out, stats = tables.normalize_tables(
        "\\newcolumntype{Y}{>{\\raggedleft}p{2cm}}\n"
        "\\begin{tabular}{Yc}\\end{tabular}")
    assert stats["declarations"] == 1
    assert "\\begin{tabular}{rc}" in out


@pytest.mark.parametrize("text, expected", [
    # A superseded spec left commented above the live one must not inflate the
    # count and fire the guard on a document that is perfectly fine.
    ("%\\begin{tabular}{c}\n\\begin{tabular}{c}\\end{tabular}", 1),
    ("100\\% \\begin{tabular}{c}\\end{tabular}", 1),
    ("\\begin{tabular}{c}\\end{tabular}\\begin{longtable}{c}\\end{longtable}", 2),
    (_LONGTABLE, 1),
])
def test_table_environments_are_counted_past_the_comments(text, expected):
    assert tables.count_table_environments(text) == expected


def test_an_escaped_percent_is_not_a_comment():
    assert tables.strip_comments("5\\% of x % note") == "5\\% of x "


def test_the_alignments_are_read_through_the_declarations():
    """What the delivered .docx must contain, recorded before the strip."""
    assert tables.expected_alignments(_R_TRAP) == [["left", "center"]]
    assert tables.expected_alignments(_LONGTABLE) == [
        ["left", "center", "center"]]


def test_prose_survives_untouched():
    """This runs over whole manuscripts, not over table files."""
    prose = "The rate was 4\\% in 2020, and $r$ rose. See Table~3.\n"
    out, _ = tables.normalize_tables(prose)
    assert out == prose


def test_the_manifest_round_trips(tmp_path):
    """Without it a post-conversion gate can only compare the docx to an
    already-stripped file, which cannot detect a strip-only normalization."""
    dst = tmp_path / "out.tex"
    _, stats = tables.normalize_tables(_R_TRAP)
    dst.write_text("x", encoding="utf-8")
    tables.write_manifest(dst, stats)
    assert tables.read_manifest(dst)["alignments"] == [["left", "center"]]
    assert tables.read_manifest(tmp_path / "absent.tex") is None


def test_an_unbalanced_group_makes_no_progress_rather_than_eating_the_rest():
    """One of the two points the copies differed on in the brace helper.

    Returning the end of the text instead would make an unclosed brace swallow
    the remainder of the document as though it were a column specification.
    """
    assert tables.skip_group("{unclosed", 0) == 0
    assert tables.skip_group("  {ok} rest", 0) == 6


def test_a_newline_before_the_brace_is_skipped():
    """The other one. `group_start` always skipped it, so a `skip_group` that
    did not left the two disagreeing about the same offset."""
    assert tables.skip_group("\\begin{tabular}\n{lc}", 15) == 20
