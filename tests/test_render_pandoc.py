"""M2/M3 — the pandoc invocation, and the single-block hot path.

Two things are being pinned down here.

The first is that a full render is a build step, not an interaction. Measured on
estonia-ecm it is 5.5 seconds, and the editor writes on every typing pause, so
patching one paragraph has to go through a different door. `render_block` is
that door and the timing test below is the reason it exists.

The second is that a render must never fail silently. Pandoc exits non-zero on
a body it cannot parse, and the recovery path retries against a simplified
preamble. When even that fails the caller gets pandoc's own diagnosis, not an
empty string that looks like an empty manuscript.
"""
from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

from manuscriptor.render.pandoc import (
    PandocError,
    extract_preamble,
    normalize_for_pandoc,
    render_block,
    render_document,
)

ESTONIA = Path("/Users/bbdaniels/Projects/estonia-ecm/latex/main.tex")

SIMPLE = """\\documentclass{article}
\\begin{document}
Ordinary prose survives the round trip.
\\end{document}
"""


def write(root: Path, name: str, text: str) -> Path:
    p = root / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


# ------------------------------------------------------------ document round trip


def test_a_document_round_trips_to_html(tmp_path):
    html = render_document(SIMPLE, cwd=tmp_path, bib=None)
    assert "<p>Ordinary prose survives the round trip.</p>" in html


def test_the_manuscript_directory_is_not_written_to(tmp_path):
    """The manuscript dir is watched and under git. A render must not churn it."""
    before = sorted(p.name for p in tmp_path.iterdir())
    render_document(SIMPLE, cwd=tmp_path, bib=None)
    assert sorted(p.name for p in tmp_path.iterdir()) == before


def test_math_becomes_native_mathml(tmp_path):
    """Math is emitted as MathML, which browsers render themselves.

    The alternative was bundling MathJax, roughly a megabyte of JavaScript, to
    render notation the browser already knows. MathML costs nothing, needs no
    script, and keeps the page self-contained, which the CSP requires anyway.
    """
    src = SIMPLE.replace("Ordinary prose", "Math $x^2$ and prose")
    html = render_document(src, cwd=tmp_path, bib=None)
    assert "<math" in html and "MathML" in html
    assert "<msup>" in html, "structure, not an image or a fallback string"
    assert "class=\"math inline\"" not in html, "no MathJax spans left to render"


def test_relative_image_paths_are_preserved_verbatim(tmp_path):
    src = """\\documentclass{article}
\\usepackage{graphicx}
\\begin{document}
\\includegraphics{figures/fig1.png}
\\end{document}
"""
    html = render_document(src, cwd=tmp_path, bib=None)
    assert 'src="figures/fig1.png"' in html


def test_a_bibliography_produces_a_resolved_citation(tmp_path):
    bib = write(
        tmp_path,
        "refs.bib",
        "@article{smith2020,\n  title={A Title},\n  author={Smith, Jane},\n"
        "  year={2020},\n  journal={J. Things}\n}\n",
    )
    src = """\\documentclass{article}
\\begin{document}
As shown elsewhere \\citep{smith2020}.
\\bibliography{refs}
\\end{document}
"""
    html = render_document(src, cwd=tmp_path, bib=bib)
    assert 'data-cites="smith2020"' in html
    # citeproc actually ran: the key was replaced by a formatted citation.
    assert "Smith" in html


# ---------------------------------------------------------------- fallback


BROKEN_BY_A_PREAMBLE_MACRO = """\\documentclass[12pt]{fancyclass}
\\usepackage{fancyclass}
\\newcommand{\\opensection}[1]{\\begin{itemize}\\item #1}
\\begin{document}
\\opensection{alpha}
Ordinary prose survives.
\\end{document}
"""


def test_the_unsimplified_render_really_does_fail(tmp_path):
    """Guard on the guard. If pandoc ever stops failing here, the fallback test
    below would pass without the fallback having fired."""
    r = subprocess.run(
        ["pandoc", "--from=latex+raw_tex", "--to=html5", "--mathjax", "--wrap=preserve"],
        input=BROKEN_BY_A_PREAMBLE_MACRO,
        capture_output=True,
        text=True,
        cwd=tmp_path,
    )
    assert r.returncode != 0


def test_the_fallback_recovers_a_document_the_direct_render_cannot_parse(tmp_path):
    html = render_document(BROKEN_BY_A_PREAMBLE_MACRO, cwd=tmp_path, bib=None)
    assert "Ordinary prose survives." in html


def test_the_fallback_swaps_the_document_class(tmp_path):
    from manuscriptor.render.pandoc import simplify_preamble

    simplified = simplify_preamble(BROKEN_BY_A_PREAMBLE_MACRO)
    assert "\\documentclass{article}" in simplified
    assert "fancyclass" not in simplified


def test_an_unrecoverable_document_raises_with_pandocs_own_diagnosis(tmp_path):
    """A body-level syntax error no preamble simplification can fix."""
    src = """\\documentclass{article}
\\begin{document}
\\begin{itemize}\\item dangling
\\end{document}
"""
    with pytest.raises(PandocError) as exc:
        render_document(src, cwd=tmp_path, bib=None)
    assert "itemize" in str(exc.value)


# -------------------------------------------------- typesetting-only wrappers
#
# Flattening is what makes tables reach pandoc at all, and it turns out pandoc
# cannot read the ones it now receives. Five constructs, all measured against
# pandoc 3.1.1 on 2026-07-22, all present in the corpus, and four of the five
# fail *silently* — exit code zero, table gone:
#
#   \newcolumntype   70 in estonia-ecm    hard parse failure on the #1 in its body
#   adjustbox        16 in sdi-caseloads  environment and contents both vanish
#   resizebox        12 in qutub-india    contents vanish
#   scalebox                              contents vanish
#   threeparttable    1 in estonia-ecm    contents vanish
#   \multicolumn{n}{m{3cm}}               whole table vanishes
#
# Every one of them is a scaling, spacing, or column-width instruction with no
# HTML meaning whatsoever, so neutralizing them costs nothing and is the
# difference between a page with tables and a page that quietly has none.


def raw_pandoc(body: str) -> tuple[int, str]:
    doc = f"\\documentclass{{article}}\n\\begin{{document}}\n{body}\n\\end{{document}}\n"
    r = subprocess.run(
        ["pandoc", "--from=latex+raw_tex", "--to=html5", "--mathjax", "--wrap=preserve"],
        input=doc,
        capture_output=True,
        text=True,
    )
    return r.returncode, r.stdout


def pandoc_version() -> tuple[int, ...]:
    """The version of the pandoc actually on PATH, as a comparable tuple.

    Whichever pandoc is installed is the one that renders the author's papers,
    so a test that characterizes an upstream bug has to say which pandoc it is
    talking about. Nothing else in the pipeline branches on this: the
    normalizations run unconditionally, by design.
    """
    out = subprocess.run(["pandoc", "--version"], capture_output=True, text=True).stdout
    return tuple(int(n) for n in out.split()[1].split("."))


TABULAR = "\\begin{tabular}{cc}a&b\\\\\\end{tabular}"


def test_newcolumntype_is_a_hard_parse_failure_before_normalizing():
    rc, _ = raw_pandoc("\\newcolumntype{m}[1]{>{\\centering}p{#1}}\nProse after.")
    assert rc != 0


def test_newcolumntype_is_removed(tmp_path):
    src = """\\documentclass{article}
\\begin{document}
\\newcolumntype{m}[1]{>{\\centering\\arraybackslash}p{#1}}
Prose after the declaration.
\\end{document}
"""
    html = render_document(src, cwd=tmp_path, bib=None)
    assert "Prose after the declaration." in html
    assert "newcolumntype" not in html


@pytest.mark.parametrize(
    "wrapper",
    [
        "\\resizebox{\\textwidth}{!}{BODY}",
        "\\scalebox{0.8}{BODY}",
        "\\begin{adjustbox}{max width=\\textwidth}BODY\\end{adjustbox}",
        "\\begin{threeparttable}BODY\\end{threeparttable}",
    ],
    ids=["resizebox", "scalebox", "adjustbox", "threeparttable"],
)
def test_a_typesetting_wrapper_silently_swallows_its_table_before_normalizing(wrapper):
    """Guard on the guard: exit code zero, and the table is simply gone."""
    rc, html = raw_pandoc(wrapper.replace("BODY", TABULAR))
    assert rc == 0
    assert "<table" not in html


@pytest.mark.parametrize(
    "wrapper",
    [
        "\\resizebox{\\textwidth}{!}{BODY}",
        "\\scalebox{0.8}{BODY}",
        "\\begin{adjustbox}{max width=\\textwidth}BODY\\end{adjustbox}",
        "\\begin{threeparttable}BODY\\end{threeparttable}",
    ],
    ids=["resizebox", "scalebox", "adjustbox", "threeparttable"],
)
def test_a_typesetting_wrapper_is_unwrapped_and_the_table_survives(wrapper, tmp_path):
    src = (
        "\\documentclass{article}\n\\begin{document}\n"
        + wrapper.replace("BODY", TABULAR)
        + "\n\\end{document}\n"
    )
    assert "<table" in render_document(src, cwd=tmp_path, bib=None)


def test_a_parameterised_multicolumn_spec_before_normalizing():
    """Pandoc 3.10.1 fixed this one; the reduction is kept anyway.

    Through pandoc 3.9 a parameterised `\\multicolumn` spec took the WHOLE
    table with it -- exit zero, nothing on stderr, no table. That is the bug
    that took estonia-qbs from 2 of 7 tables to 7 of 7 on 2026-07-22.

    Measured 2026-07-28 against both pandocs on this machine, 3.1.1 at
    /usr/local and 3.10.1 at /opt/homebrew: 3.10.1 renders the table correctly
    from the raw source, byte for byte identical to what it renders from the
    reduced source. So `tables.plain_multicolumn_specs` is now a no-op here rather
    than a repair, and a no-op is exactly why it stays. The manuscripts are
    also opened on machines with older pandocs, removing it would silently
    delete tables there, and it cannot double-handle output that is already
    correct: reducing `m{3.4cm}` to `c` yields the same centered column pandoc
    now produces on its own.

    This test asserts what the installed pandoc actually does, so it keeps
    documenting the bug on a machine that still has it.
    """
    rc, html = raw_pandoc(
        "\\begin{tabular}{ccc}\\multicolumn{2}{m{3.4cm}}{X} & b\\\\\\end{tabular}"
    )
    assert rc == 0
    if pandoc_version() >= (3, 10):
        assert "<table" in html
        assert 'colspan="2"' in html
    else:
        assert "<table" not in html


def test_a_parameterised_multicolumn_spec_is_reduced_to_an_alignment(tmp_path):
    src = """\\documentclass{article}
\\begin{document}
\\begin{tabular}{ccc}\\multicolumn{2}{m{3.4cm}}{X} & b\\\\\\end{tabular}
\\end{document}
"""
    assert "<table" in render_document(src, cwd=tmp_path, bib=None)


# ------------------------------------------------- equation labels (pandoc 3.10)
#
# The one thing that renders WORSE under 3.10.1 than under 3.1.1, measured
# 2026-07-28 across estonia-ecm, estonia-qbs and dsp-bias. Everything else in
# the comparison was unchanged or repaired upstream; this went backwards, and
# it goes backwards silently, in the body of the paper.

EQ_TRAILING = (
    "\\begin{equation}\n"
    "\\Delta \\widehat{VA}_{i} = \\alpha + \\beta \\, \\widetilde{S}_i\n"
    "\\label{eq:va-xs}\n"
    "\\end{equation}"
)


def test_a_trailing_equation_label_before_normalizing():
    """Characterizes the upstream regression, per installed pandoc.

    `raw_pandoc` asks for `--mathjax`, where an unparsed equation is not an
    absent `<math>` but a RETAINED `\\begin{equation}`: pandoc that understood
    the environment consumes it and emits the inner math, pandoc that did not
    hands the whole environment through verbatim. Exit status is zero either
    way, which is what makes this worth a test rather than a bug report.
    """
    rc, html = raw_pandoc(EQ_TRAILING)
    assert rc == 0
    assert "\\label{eq:va-xs}" in html
    if pandoc_version() >= (3, 10):
        assert "\\begin{equation}" in html
    else:
        assert "\\begin{equation}" not in html


def test_a_trailing_equation_label_is_hoisted_to_the_front():
    out = normalize_for_pandoc(EQ_TRAILING)
    assert out.startswith("\\begin{equation}\\label{eq:va-xs}")
    assert out.count("\\label{eq:va-xs}") == 1


def test_an_equation_with_a_trailing_label_renders_as_math(tmp_path):
    """The guarantee that matters, and the one the reader sees.

    The real pipeline renders with `--mathml`, where a failed parse degrades to
    `<span class="math display">$$...$$</span>` -- the LaTeX source, on the
    page, as text. So this asserts BOTH that MathML was produced and that no
    such fallback span exists. `\\begin{equation}` itself is not asserted
    absent: MathML carries the TeX round trip in `<annotation>`, so the string
    is legitimately present in a correct render.
    """
    src = f"\\documentclass{{article}}\n\\begin{{document}}\n{EQ_TRAILING}\n\\end{{document}}\n"
    html = render_document(src, cwd=tmp_path, bib=None)
    assert "<math" in html
    assert 'class="math display"' not in html


def test_a_trailing_tag_is_hoisted_too():
    out = normalize_for_pandoc("\\begin{equation}\ny = x\n\\tag{1}\n\\end{equation}")
    assert out.startswith("\\begin{equation}\\tag{1}")


def test_hoisting_an_equation_label_is_idempotent():
    once = normalize_for_pandoc(EQ_TRAILING)
    assert normalize_for_pandoc(once) == once


@pytest.mark.parametrize("env", ["align", "gather", "multline", "eqnarray"])
def test_multi_row_math_environments_keep_their_labels_in_place(env):
    """A label in `align` numbers ONE row. Hoisting it would renumber the
    equation, and these environments parse a trailing label correctly under
    both pandoc versions, so there is nothing to repair."""
    src = "\\begin{" + env + "}\ny &= x \\label{a}\\\\ z &= w \\label{b}\n\\end{" + env + "}"
    assert normalize_for_pandoc(src) == src


def test_an_equation_that_is_only_a_label_is_left_alone():
    """Rewriting a body with no math into `\\begin{equation}\\label{a}\\end{equation}`
    would buy nothing and risks turning a degenerate case into a parse error."""
    src = "\\begin{equation}\\label{a}\\end{equation}"
    assert normalize_for_pandoc(src) == src


@pytest.mark.parametrize(
    "spec,expected",
    [
        ("c", "c"),
        ("m{3.4cm}", "c"),
        ("p{2cm}", "l"),
        (">{\\centering\\arraybackslash}p{4cm}", "l"),
        ("S[table-format=1.2]", "l"),
        ("|c|", "|c|"),
    ],
)
def test_column_specs_reduce_to_plain_alignments(spec, expected):
    out = normalize_for_pandoc("\\multicolumn{2}{" + spec + "}{X}")
    assert out == "\\multicolumn{2}{" + expected + "}{X}"


def test_a_column_type_pandoc_does_not_know_silently_kills_the_table():
    """The reason the spec rewrite is mandatory rather than cosmetic. `R` and
    `M` come from the \\newcolumntype declarations stripped above, so removing
    those declarations without this would trade a loud failure for a silent
    one, which is strictly worse."""
    rc, html = raw_pandoc("\\begin{tabular}{R{4cm} M{2cm}} a&b\\\\\\end{tabular}")
    assert rc == 0
    assert "<table" not in html


def test_an_unknown_column_type_is_reduced_and_the_table_survives(tmp_path):
    src = """\\documentclass{article}
\\begin{document}
\\newcolumntype{R}[1]{>{\\raggedright\\arraybackslash}p{#1}}
\\newcolumntype{M}[1]{>{\\centering\\arraybackslash}p{#1}}
\\begin{tabular}{R{4cm} M{2cm}} a&b\\\\\\end{tabular}
\\end{document}
"""
    assert "<table" in render_document(src, cwd=tmp_path, bib=None)


@pytest.mark.parametrize("env", ["tabular", "longtable", "supertabular"])
def test_table_environment_specs_are_reduced_too(env):
    out = normalize_for_pandoc("\\begin{" + env + "}{m{2cm} p{3cm}}a&b\\\\")
    assert out.startswith("\\begin{" + env + "}{cl}")


@pytest.mark.parametrize("env", ["tabular*", "tabularx", "xltabular"])
def test_a_width_taking_environment_keeps_its_width(env):
    """`tabularx` takes a target width before the spec. Reducing the width by
    mistake would turn a table into a one-column table."""
    out = normalize_for_pandoc("\\begin{" + env + "}{\\textwidth}{m{2cm} p{3cm}}a&b\\\\")
    assert out.startswith("\\begin{" + env + "}{\\textwidth}{cl}")


def test_an_optional_environment_argument_is_kept():
    out = normalize_for_pandoc("\\begin{longtable}[l]{r{4cm} m{2cm}}")
    assert out == "\\begin{longtable}[l]{rc}"


def test_a_declared_column_type_is_read_rather_than_guessed():
    """estonia-ecm redefines `r` as ragged-right, so the standard meaning is
    the wrong one. The declaration is right there in the source; use it."""
    src = (
        "\\newcolumntype{r}[1]{>{\\raggedright\\arraybackslash}p{#1}}\n"
        "\\newcolumntype{M}[1]{>{\\centering\\arraybackslash}p{#1}}\n"
        "\\begin{longtable}{r{4cm} M{2cm}}a&b\\\\\\end{longtable}\n"
    )
    assert "\\begin{longtable}{lc}" in normalize_for_pandoc(src)


def test_an_undeclared_column_type_falls_back_to_left():
    assert normalize_for_pandoc("\\begin{tabular}{Z{2cm}}") == "\\begin{tabular}{l}"


def test_a_brace_inside_a_latex_comment_does_not_confuse_the_unwrapper():
    """Flattening does not strip comments, and a stray brace in one would make
    a naive brace counter close the wrapper in the middle of the table."""
    comment = "% closing } brace in a comment\n"
    src = "\\resizebox{\\textwidth}{!}{" + comment + TABULAR + "\n}\nAfter."
    assert normalize_for_pandoc(src) == comment + TABULAR + "\n\nAfter."


def test_an_escaped_percent_does_not_start_a_comment():
    src = "\\resizebox{\\textwidth}{!}{50\\% of " + TABULAR + "}\nAfter."
    assert normalize_for_pandoc(src) == "50\\% of " + TABULAR + "\nAfter."


def test_normalizing_leaves_ordinary_source_alone():
    src = "Prose with $x^2$ and a \\citep{key} and \\begin{tabular}{cc}a&b\\\\\\end{tabular}."
    assert normalize_for_pandoc(src) == src


def test_normalizing_does_not_disturb_block_markers():
    """Markers are injected before the render. Losing one costs an anchor."""
    src = "⟦MXdeadbeef01⟧\\resizebox{\\textwidth}{!}{" + TABULAR + "}"
    assert "⟦MXdeadbeef01⟧" in normalize_for_pandoc(src)


# ---------------------------------------------------------------- preamble


def test_extract_preamble_stops_at_begin_document():
    src = "\\documentclass{article}\n\\usepackage{amsmath}\n\\begin{document}\nBody.\n\\end{document}\n"
    assert extract_preamble(src) == "\\documentclass{article}\n\\usepackage{amsmath}\n"


def test_extract_preamble_of_a_fragment_is_empty():
    assert extract_preamble("Just a paragraph of prose.\n") == ""


@pytest.mark.skipif(not ESTONIA.exists(), reason="estonia-ecm checkout not present")
def test_the_reference_manuscript_renders_its_tables_and_figures():
    """The regression floor. Before normalization this document rendered zero
    tables: flattening delivered them to pandoc and pandoc dropped every one.
    17 live table environments survive the flatten; 15 reach the page."""
    from manuscriptor.source.flatten import flatten

    flat = flatten(ESTONIA)
    bib = ESTONIA.parent / "references.bib"
    html = render_document(flat.text, cwd=ESTONIA.parent, bib=bib if bib.exists() else None)
    assert html.count("<table") >= 15
    assert html.count("<img") >= 15
    assert html.count('class="citation"') >= 70


@pytest.mark.skipif(not ESTONIA.exists(), reason="estonia-ecm checkout not present")
def test_extract_preamble_on_the_real_manuscript():
    from manuscriptor.source.flatten import flatten

    preamble = extract_preamble(flatten(ESTONIA).text)
    assert preamble.startswith("\\documentclass")
    assert "\\begin{document}" not in preamble


# ------------------------------------------------------------- render_block


def test_a_block_renders_on_its_own(tmp_path):
    html = render_block(
        "A single paragraph of prose.",
        preamble="\\documentclass{article}",
        cwd=tmp_path,
    )
    assert "<p>A single paragraph of prose.</p>" in html


def test_a_block_sees_macros_defined_in_the_preamble(tmp_path):
    """This is why the preamble is threaded through rather than dropped."""
    html = render_block(
        "The estimate is \\effect{} standard deviations.",
        preamble="\\documentclass{article}\n\\newcommand{\\effect}{0.42}",
        cwd=tmp_path,
    )
    assert "0.42" in html


def test_a_block_keeps_its_citation_hook(tmp_path):
    """No bibliography on the hot path, but the data-cites hook must survive so
    postprocess can still address the span."""
    html = render_block(
        "As shown \\citep{smith2020}.",
        preamble="\\documentclass{article}",
        cwd=tmp_path,
    )
    assert 'data-cites="smith2020"' in html


def test_a_block_that_cannot_be_parsed_raises(tmp_path):
    with pytest.raises(PandocError):
        render_block(
            "\\begin{itemize}\\item dangling",
            preamble="\\documentclass{article}",
            cwd=tmp_path,
        )


@pytest.mark.skipif(not ESTONIA.exists(), reason="estonia-ecm checkout not present")
def test_a_single_block_renders_far_faster_than_the_whole_document():
    """The editor saves on every typing pause. If a block render costs what a
    document render costs, continuous saving is not possible at all."""
    from manuscriptor.source.flatten import flatten

    flat = flatten(ESTONIA)
    preamble = extract_preamble(flat.text)
    bib = ESTONIA.parent / "references.bib"
    cwd = ESTONIA.parent

    t0 = time.perf_counter()
    render_document(flat.text, cwd=cwd, bib=bib if bib.exists() else None)
    full = time.perf_counter() - t0

    block = (
        "We estimate the effect of enrolment on the probability of a "
        "hypertension diagnosis, controlling for baseline utilisation."
    )
    # Warm any cost that is not per-call, then measure.
    render_block(block, preamble=preamble, cwd=cwd)
    t0 = time.perf_counter()
    render_block(block, preamble=preamble, cwd=cwd)
    single = time.perf_counter() - t0

    print(f"\nfull render: {full:.2f}s   single block: {single:.3f}s   ratio {full / single:.0f}x")
    assert single < 1.0, f"block render took {single:.3f}s; the hot path must be sub-second"
    assert single < full / 5, f"block {single:.3f}s vs full {full:.2f}s is not a material gain"


# ------------------------------------------------------------- front matter
#
# Pandoc reads \title, \author, and the abstract environment into document
# metadata, and a fragment render shows metadata nowhere. Measured on
# estonia-ecm 2026-07-22: the page opened on the Introduction with no title
# and no abstract, and the three blocks carrying them rendered as empty
# anchors — clickable slivers around nothing. The front matter has to be
# rewritten into constructs pandoc keeps.


FRONTMATTER = """\\documentclass{article}
\\title{In Sickness and In Health\\thanks{We thank the funder.}}
\\author{A.~Author\\thanks{One University} \\and B.~Coauthor}
\\begin{document}
⟦MX00a1⟧\\maketitle

⟦MX00a2⟧\\begin{abstract}
The abstract text survives into the page.
\\end{abstract}

⟦MX00a3⟧Ordinary prose follows the front matter.
\\end{document}
"""


def test_the_title_reaches_the_page_as_a_heading(tmp_path):
    html = render_document(FRONTMATTER, cwd=tmp_path, bib=None)
    assert "In Sickness and In Health" in html
    assert "<h1" in html
    assert "\\maketitle" not in html


def test_the_byline_reaches_the_page_without_its_thanks(tmp_path):
    html = render_document(FRONTMATTER, cwd=tmp_path, bib=None)
    assert "Author" in html
    assert "Coauthor" in html
    # \thanks is a footnote on the title page, not text of the title or byline.
    assert "We thank the funder" not in html
    assert "One University" not in html


def test_the_abstract_text_reaches_the_page(tmp_path):
    html = render_document(FRONTMATTER, cwd=tmp_path, bib=None)
    assert "The abstract text survives into the page." in html
    assert "Abstract" in html


def test_the_abstract_keeps_its_anchor_on_its_own_text(tmp_path):
    # The marker before \begin{abstract} must end up on the abstract's prose,
    # not stranded on the label heading, or the block cannot be clicked.
    html = render_document(FRONTMATTER, cwd=tmp_path, bib=None)
    at_marker = html.find("⟦MX00a2⟧")
    at_text = html.find("The abstract text survives")
    assert at_marker != -1
    assert at_marker < at_text
    label_at = html.find("Abstract")
    assert label_at < at_marker, "the label heading must precede the anchor"


def test_a_maketitle_with_no_title_is_left_alone(tmp_path):
    src = "\\documentclass{article}\n\\begin{document}\n\\maketitle\n\nProse.\n\\end{document}\n"
    html = render_document(src, cwd=tmp_path, bib=None)
    # Nothing to show and nothing invented: no heading appears.
    assert "<h1" not in html
    assert "Prose." in html


def test_frontmatter_tokens_are_carried_for_postprocess(tmp_path):
    # The rewritten title and byline carry tokens the postprocess pass turns
    # into classes; render alone must leave them in place, not lose them.
    html = render_document(FRONTMATTER, cwd=tmp_path, bib=None)
    assert "⟦MXTITLE⟧" in html
    assert "⟦MXBYLINE⟧" in html
    assert "⟦MXABSTRACT⟧" in html


def test_a_forced_break_in_the_title_is_a_space_not_a_comma(tmp_path):
    # `\title{In Sickness: \\ Motivating...}` is print layout. Joining it with
    # the author-list comma shipped "In Sickness: , Motivating" on the first
    # live render of the reference manuscript.
    src = (
        "\\documentclass{article}\n"
        "\\title{In Sickness: \\\\ Motivating Care}\n"
        "\\author{A One \\\\ B Two}\n"
        "\\begin{document}\n\\maketitle\n\nProse.\n\\end{document}\n"
    )
    html = render_document(src, cwd=tmp_path, bib=None)
    assert "In Sickness: Motivating Care" in html
    assert "A One, B Two" in html


def test_a_spacing_environment_does_not_swallow_the_title(tmp_path):
    # dsp-bias wraps \maketitle in \begin{singlespace}: a setspace environment
    # pandoc does not know, so the whole front matter vanished with it. Print
    # geometry with no HTML meaning, unwrapped like the other wrappers.
    src = (
        "\\documentclass{article}\n"
        "\\title{The Paper}\n\\author{A. Author}\n\\date{}\n"
        "\\begin{document}\n"
        "\\begin{singlespace}\n\\setlength{\\parskip}{2pt}\n\\maketitle\n"
        "\\end{singlespace}\n\nProse.\n\\end{document}\n"
    )
    html = render_document(src, cwd=tmp_path, bib=None)
    assert "The Paper" in html
    assert "<h1" in html
