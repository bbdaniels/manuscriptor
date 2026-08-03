r"""LaTeX table-structure repair is decided in one module.

Two repairs live there: column-type normalization, and header-row
identification (added 2026-08-03, when the invariant was widened from the first
to cover both). They answer the same question -- what does this LaTeX table
actually say -- and can only be answered while the LaTeX still says it.

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


# The third signature, added 2026-08-03 when the invariant was widened from
# column types to LaTeX table-structure repair generally. Header-row
# identification is the second repair, and it has the same shape of hazard: a
# partial copy that decides SOME rows are headers is worse than none, because
# the rows it misses stay in the body looking exactly like data.
#
# Two shapes, each sufficient.
#
# The first is deciding the header from the rules -- a regex or a string cut
# handed `\hline`, `\midrule` or `\toprule` -- but ONLY in a module that also
# builds a table head. Both halves of that were needed, and each was found by
# watching this guard fail on `render/pandoc.py`, which does neither:
#
#   * The rule name must not sit inside a longer word. `_strip_rules` compiles
#     `\\(?:cmidrule|cline|specialrule|addlinespace|morecmidrules)` in order to
#     remove the PARTIAL rules -- the ones that are explicitly not boundaries --
#     and `cmidrule` contains `midrule`.
#   * The head signature is the tag, not the words. `header_row` as a signature
#     fired on `from ...tables import mark_header_rows`, which is a module doing
#     the right thing in the most visible possible way.
_RULE_PARSE_RE = re.compile(
    r"(?:re\.(?:compile|finditer|search|sub|match|split|findall)|"
    r"\.(?:replace|split|find|index|partition))\([^)]{0,80}?"
    r"(?<![A-Za-z])(?:hline|midrule|toprule)")
_THEAD_RE = re.compile(r"<thead", re.I)

# The second is spelling the carrier token as a literal instead of importing
# `HEADER_TOKEN`. Half a contract written twice is how the two column-type
# copies drifted, and a token nobody imports is a token that can be changed in
# one place and left stale in the other.
_TOKEN_LITERAL_RE = re.compile(r"MXTHEAD")


def _reimplements(body: str) -> str | None:
    squashed = _squash(_strip_comments(body))
    if _PARSER_RE.search(squashed):
        return "parses a \\newcolumntype declaration"
    if sum(bool(r.search(squashed)) for r in _ALIGN_RES) >= 2:
        return "carries its own column-letter alignment map"
    if _TOKEN_LITERAL_RE.search(squashed):
        return "spells the header token instead of importing HEADER_TOKEN"
    if _RULE_PARSE_RE.search(squashed) and _THEAD_RE.search(squashed):
        return "identifies header rows from the LaTeX rules"
    return None


def test_only_render_tables_implements_table_structure_repair():
    offenders = []
    for path in _sources():
        why = _reimplements(path.read_text(encoding="utf-8"))
        if why is not None:
            offenders.append(f"{path.relative_to(SRC.parent)} ({why})")
    assert offenders == [], (
        "LaTeX table-structure repair is implemented outside "
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
    # The header half. A second decision about which rows are a header is the
    # same hazard as a second decision about column types.
    r'_RULE = re.compile(r"\\(?:hline|midrule)")'
    r'\nhtml = "<thead>" + rows',
    r'head, _, body = latex.partition("\\midrule")\nout = "<thead>%s</thead>" % head',
    # And a second spelling of the carrier token.
    r'TOKEN = "\u27e6MXTHEAD\u27e7"',
    r"if '\u27e6MXTHEAD\u27e7' in cell:",
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
    # pandoc._strip_rules names the full-width rules in order to say it leaves
    # them alone, and knows nothing about table heads. Verbatim.
    '_RULE_RE = re.compile(r"\\\\(?:cmidrule|cline|specialrule)")\n'
    'assert "toprule" not in _RULE_RE.pattern',
    # The carrier: it builds a thead and decides nothing.
    'if "<thead" in table: return table.replace(HEADER_TOKEN, "")',
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
        "a skill re-implements LaTeX table-structure repair instead of "
        "importing manuscriptor.render.tables: " + "; ".join(offenders)
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


# ------------------------------------------------------- header-row marking
#
# The second table-structure repair, and the same argument for one home: it is
# decided in the LaTeX stage, where the rules that delimit a header are still
# visible, and `render/postprocess.py` may only CARRY the marking into
# `<thead>`. Pandoc's LaTeX reader promotes at most ONE row to the header, so a
# table with two header rows emits `<tbody>` and nothing else -- 72 of the 243
# tables in the corpus.


def _marked(text: str) -> list[str]:
    """The rows carrying the header token, as source lines."""
    out, _ = tables.mark_header_rows(text)
    return [line for line in out.splitlines() if tables.HEADER_TOKEN in line]


def test_two_header_rows_are_both_marked():
    """The defect. covet-india's Table 1 writes `\\textbf{Round 1}` over
    `(2014--15)`, and pandoc promotes neither."""
    src = ("\\begin{tabular}{lcc}\n\\toprule\n"
           "A & B & C \\\\\n(1) & (2) & (3) \\\\\n\\midrule\n"
           "x & 1 & 2 \\\\\n\\bottomrule\n\\end{tabular}\n")
    out, n = tables.mark_header_rows(src)
    assert n == 2
    T = tables.HEADER_TOKEN
    # One mark per CELL, so a row survives pandoc absorbing its first cell into
    # a `rowspan`. See `_header_marks`.
    assert T + "A & " + T + "B & " + T + "C" in out
    assert T + "(1) & " + T + "(2) & " + T + "(3)" in out
    assert out.count(T) == 6, "three cells in each of two rows, and no more"
    assert T not in out.split("\\midrule")[1], "no body row may be marked"


def test_three_header_rows_are_all_marked():
    src = ("\\begin{tabular}{lc}\n\\hline\n"
           "A & B \\\\\nC & D \\\\\nE & F \\\\\n\\hline\n"
           "x & 1 \\\\\n\\end{tabular}\n")
    out, n = tables.mark_header_rows(src)
    assert n == 3, "rows, not marks"
    assert out.count(tables.HEADER_TOKEN) == 6, "two cells in each of three rows"


def test_a_single_header_row_is_marked_too():
    """Pandoc already promotes one row, so the marking changes nothing on these
    -- but it must still be honest about which row is the header, because the
    carrier decides what to do by looking at what pandoc did."""
    src = ("\\begin{tabular}{lc}\n\\toprule\nA & B \\\\\n\\midrule\n"
           "x & 1 \\\\\n\\bottomrule\n\\end{tabular}\n")
    out, n = tables.mark_header_rows(src)
    assert n == 1
    assert out.count(tables.HEADER_TOKEN + "A & ") == 1


def test_a_table_with_no_rule_at_all_gets_no_header():
    """Nothing in the source says which row is a header, so nothing is one.
    Pandoc agrees: no rule, no `<thead>`."""
    src = "\\begin{tabular}{lc}\nA & B \\\\\nx & 1 \\\\\n\\end{tabular}\n"
    out, n = tables.mark_header_rows(src)
    assert n == 0
    assert out == src


def test_a_table_whose_rows_all_sit_above_the_last_rule_has_no_header():
    r"""`\toprule` then rows then `\bottomrule`, with no `\midrule` between.

    There is no body after the closing rule, so the rows are not a header
    standing over anything -- and pandoc emits no `<thead>` for this either.
    Marking them would invent a header the source never declared.
    """
    src = ("\\begin{tabular}{lc}\n\\toprule\nA & B \\\\\nx & 1 \\\\\n"
           "\\bottomrule\n\\end{tabular}\n")
    out, n = tables.mark_header_rows(src)
    assert n == 0
    assert out == src


def test_a_doubled_rule_does_not_swallow_the_header():
    r"""`\hline\hline` is one boundary written twice. Reading the empty span
    between them as the header block would leave the real header in the body."""
    src = ("\\begin{tabular}{lc}\n\\hline\\hline\nA & B \\\\\n\\hline\n"
           "x & 1 \\\\\n\\end{tabular}\n")
    out, n = tables.mark_header_rows(src)
    assert n == 1
    assert out.count(tables.HEADER_TOKEN + "A & ") == 1


def test_the_header_may_stand_before_the_first_rule():
    """The old-style table: header, then `\\hline`, then the body."""
    src = "\\begin{tabular}{lc}\nA & B \\\\\n\\hline\nx & 1 \\\\\n\\end{tabular}\n"
    out, n = tables.mark_header_rows(src)
    assert n == 1
    assert out.count(tables.HEADER_TOKEN + "A & ") == 1


def test_a_multicolumn_header_row_is_marked_inside_its_content_group():
    r"""The placement that matters, and the reason it is not simply "the head of
    the row". Pandoc reads a cell that BEGINS with `\multicolumn` as a spanning
    cell; put anything in front of it and the span is lost along with its text.
    Measured: `⟦MXTHEAD⟧\multicolumn{2}{c}{Panel} & C` came out of pandoc as
    three cells reading `⟦MXTHEAD⟧`, `C`, `` -- the word `Panel` gone, colspan
    gone, at exit 0.
    """
    src = ("\\begin{tabular}{lcc}\n\\toprule\n"
           "\\multicolumn{2}{c}{Panel} & C \\\\\n(1) & (2) & (3) \\\\\n"
           "\\midrule\nx & 1 & 2 \\\\\n\\end{tabular}\n")
    out, _ = tables.mark_header_rows(src)
    assert "\\multicolumn{2}{c}{" + tables.HEADER_TOKEN + "Panel}" in out, out
    assert tables.HEADER_TOKEN + "\\multicolumn" not in out


def test_a_full_span_panel_row_mid_body_is_not_a_header():
    r"""covet-india's Table 1 writes `\multicolumn{6}{l}{\textit{Patna}}` as a
    panel label in the MIDDLE of the body. It looks exactly like a header row
    and is not one, which is why the header is identified by the rules that
    delimit it rather than by what a row contains."""
    src = ("\\begin{tabular}{lc}\n\\toprule\nA & B \\\\\n\\midrule\n"
           "\\multicolumn{2}{l}{\\textit{Patna}} \\\\\n"
           "x & 1 \\\\\n\\bottomrule\n\\end{tabular}\n")
    out, n = tables.mark_header_rows(src)
    assert n == 1
    assert "Patna" in out and tables.HEADER_TOKEN + "\\multicolumn" not in out
    assert "{" + tables.HEADER_TOKEN + "\\textit{Patna}}" not in out


def test_a_longtable_caption_row_is_not_the_header():
    r"""A longtable writes `\caption{...} \\` as its first row. It is a row by
    syntax and carries no cells, so reading it as the header would put the
    caption in `<thead>` and leave the real header in the body."""
    src = ("\\begin{longtable}{lcc}\n\\caption{Balance} \\\\\n\\hline\n"
           "Variable & Control & Treatment \\\\\n\\hline\n\\endfirsthead\n"
           "Age & 68.7 & 67.3 \\\\\n\\end{longtable}\n")
    out, n = tables.mark_header_rows(src)
    assert n == 1
    assert out.count(tables.HEADER_TOKEN + "Variable & ") == 1


def test_a_row_break_inside_a_group_is_not_a_row_boundary():
    r"""`\\` inside a brace group is a line break within one cell, not a row
    separator. `_flatten_stacked_cells` removes the makecell case before this
    runs, but the split must not depend on that having happened."""
    src = ("\\begin{tabular}{lc}\n\\hline\n"
           "A & \\makecell{x \\\\ y} \\\\\nB & C \\\\\n\\hline\n"
           "z & 1 \\\\\n\\end{tabular}\n")
    out, n = tables.mark_header_rows(src)
    assert n == 2, out


def test_a_partial_rule_does_not_end_the_header_block():
    r"""`\cmidrule(lr){2-3}` underlines a span of a header; it does not close
    one. Treating it as a boundary would leave the second header row -- the
    column numbers, every time -- sitting in the body."""
    src = ("\\begin{tabular}{lcc}\n\\toprule\n"
           " & \\multicolumn{2}{c}{Means} \\\\\n\\cmidrule(lr){2-3}\n"
           " & (1) & (2) \\\\\n\\midrule\nx & 1 & 2 \\\\\n\\end{tabular}\n")
    _, n = tables.mark_header_rows(src)
    assert n == 2


def test_marking_touches_nothing_outside_a_table():
    prose = "The rate was 4\\% in 2020. See Table~3.\n\nA & B \\\\ is not a table.\n"
    out, n = tables.mark_header_rows(prose)
    assert (out, n) == (prose, 0)


def test_every_marked_row_keeps_its_cells():
    """The regression that would matter: the marking may add a token and may
    change nothing else about the row."""
    src = ("\\begin{tabular}{lcc}\n\\toprule\n"
           "A & B & C \\\\\n(1) & (2) & (3) \\\\\n\\midrule\n"
           "x & 1 & 2 \\\\\n\\bottomrule\n\\end{tabular}\n")
    out, _ = tables.mark_header_rows(src)
    assert out.replace(tables.HEADER_TOKEN, "") == src


def test_a_caption_with_markup_in_it_is_still_not_a_row():
    r"""Found on estonia-ecm, which caught 13 of its 17 tables. A flat
    `\caption\{[^{}]*\}` cannot read `\caption{\textbf{ECM Impact:} On
    patient's care}`, so the caption row looked like a row with cells, became
    the header block, and the two real header rows below it stayed in the body.
    Nothing failed: the mark landed in the caption, where there is no `<tr>` to
    promote, and the table rendered exactly as it had before."""
    src = ("\\begin{longtable}{lcc}\n"
           "\\caption{\\textbf{ECM Impact:} On care (ANCOVA)} \\\\\n\\hline\n"
           "\\multirow{2}{*}{\\textbf{Variable}} & Means & Diff \\\\\n"
           " & (1) & (2) \\\\\n\\hline\n\\endfirsthead\n"
           "Age & 68.7 & 67.3 \\\\\n\\end{longtable}\n")
    out, n = tables.mark_header_rows(src)
    assert n == 2, out
    assert tables.HEADER_TOKEN not in out.split("\\hline")[0], "not in the caption"
    assert "{" + tables.HEADER_TOKEN + "\\textbf{Variable}}" in out, out


def test_a_commented_out_table_does_not_swallow_the_document():
    r"""The one that cost estonia-ecm 13 of its 17 tables, and it cost them
    ALL AT ONCE rather than one at a time.

    Authors leave a superseded table commented out directly above the live one
    -- the same habit `count_table_environments` strips comments for. A
    commented `\begin{longtable}` inside the first table's body raised the
    nesting depth, so the scan for that table's `\end` ran off the end of the
    document, the cursor jumped with it, and every table below the first was
    never looked at. Measured on the flattened manuscript: one table seen, its
    "body" 246,899 characters long.
    """
    src = ("\\begin{longtable}{lc}\n\\hline\nA & B \\\\\n\\hline\nx & 1 \\\\\n"
           "% \\begin{longtable}{lc} the old version\n\\end{longtable}\n\n"
           "\\begin{tabular}{lc}\n\\hline\nC & D \\\\\n\\hline\ny & 2 \\\\\n"
           "\\end{tabular}\n")
    out, n = tables.mark_header_rows(src)
    assert n == 2, f"the second table was never reached: {out}"
    assert out.count(tables.HEADER_TOKEN + "A & ") == 1
    assert out.count(tables.HEADER_TOKEN + "C & ") == 1


def test_a_commented_out_begin_is_not_a_table_of_its_own():
    src = ("% \\begin{tabular}{lc}\n%A & B \\\\\n"
           "\\begin{tabular}{lc}\n\\hline\nC & D \\\\\n\\hline\ny & 2 \\\\\n"
           "\\end{tabular}\n")
    out, n = tables.mark_header_rows(src)
    assert n == 1
    assert out.count(tables.HEADER_TOKEN + "C & ") == 1
