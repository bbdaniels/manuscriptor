"""Exhibit rendering: what reaches the page when a real table goes through.

Every failure here is silent. Pandoc exits zero, the table is present, and a rule
command has quietly become a row of numbers in the middle of the author's
regression output. Found by looking at the running page, not by reading code, so
each one is pinned to what was actually on screen.
"""
from __future__ import annotations

import re


from manuscriptor.render.pandoc import normalize_for_pandoc, render_document


# MathML carries the original LaTeX in an <annotation> for copy-paste and
# accessibility tools. Browsers never display it. A regex that strips tags does
# sweep it in, which made an earlier pass of these tests report raw TeX on a
# page that showed none. Read what the reader sees.
_ANNOTATION_RE = re.compile(r"<annotation\b.*?</annotation>", re.S)


def visible(html: str) -> str:
    return re.sub(r"<[^>]+>", "", _ANNOTATION_RE.sub("", html))


def cells(html: str) -> list[str]:
    return [
        visible(c).strip()
        for c in re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", html, re.S)
    ]


def rows(html: str) -> list[str]:
    return [visible(r).strip() for r in re.findall(r"<tr[^>]*>.*?</tr>", html, re.S)]


def doc(body: str) -> str:
    return (
        "\\documentclass{article}\n"
        "\\usepackage{longtable,booktabs,multirow,array}\n"
        "\\begin{document}\n" + body + "\n\\end{document}\n"
    )


# --------------------------------------------------------------- rule commands


def test_cmidrule_does_not_become_a_row_of_numbers(tmp_path):
    """The one visible on screen: `\\cmidrule(lr){2-3}` rendered as `2-3 (lr)4-5`
    as the first body row of every table in the reference manuscript."""
    html = render_document(
        doc(
            "\\begin{tabular}{lcc}\n\\toprule\n"
            "Variable & Means & Diff \\\\\n"
            "\\cmidrule(lr){2-3}\\cmidrule(lr){4-5}\n"
            "Age & 68.7 & -0.643 \\\\\n\\bottomrule\n\\end{tabular}\n"
        ),
        cwd=tmp_path,
        bib=None,
    )
    body = cells(html)
    assert not any("(lr)" in c for c in body), f"cmidrule leaked: {body}"
    assert not any(re.fullmatch(r"[\d\-\s]+", c) and "-" in c and len(c) < 5 for c in body)
    assert "68.7" in " ".join(body), "and the real data survives"


def test_cline_is_dropped_too(tmp_path):
    html = render_document(
        doc("\\begin{tabular}{lc}\nA & 1 \\\\\n\\cline{1-2}\nB & 2 \\\\\n\\end{tabular}\n"),
        cwd=tmp_path,
        bib=None,
    )
    assert not any("1-2" == c for c in cells(html))


# --------------------------------------------------------- invisible spacing


def test_phantom_does_not_reach_the_page(tmp_path):
    """estonia-ecm pads significance stars with `\\phantom{*}` for alignment.
    It is print geometry, and a reader must never see the braces."""
    html = render_document(
        doc(
            "\\begin{tabular}{lc}\nAge & -0.643$^{*}$\\phantom{*}\\phantom{*} \\\\\n"
            "Male & 0.016\\phantom{-} \\\\\n\\end{tabular}\n"
        ),
        cwd=tmp_path,
        bib=None,
    )
    text = " ".join(cells(html))
    assert "phantom" not in text.lower(), text
    assert "-0.643" in text and "0.016" in text


# ------------------------------------------------------ repeated longtable heads


def test_a_longtable_header_appears_once(tmp_path):
    """`\\endfirsthead` marks a header for page one. HTML has no pages, so pandoc
    emits the header block and then the same rows again as body."""
    html = render_document(
        doc(
            "\\begin{longtable}{lcc}\n"
            "\\caption{Balance} \\\\\n\\hline\n"
            "Variable & Control & Treatment \\\\\n"
            "\\hline\n\\endfirsthead\n"
            "Age & 68.7 & 67.3 \\\\\n"
            "Male & 0.436 & 0.462 \\\\\n"
            "\\end{longtable}\n"
        ),
        cwd=tmp_path,
        bib=None,
    )
    r = [x for x in rows(html) if x]
    header_like = [x for x in r if "Variable" in x and "Control" in x]
    assert len(header_like) <= 1, f"header repeated {len(header_like)}x: {r}"
    assert any("68.7" in x for x in r), "and the data is still there"


def test_a_table_declaring_both_heads_only_keeps_one(tmp_path):
    """longtable writes its header twice on purpose: once for the first page and
    once for every page after. HTML has no pages, so pandoc faithfully emits both
    and the reader sees the header line doubled. tableE1_coding.tex in the
    reference manuscript does exactly this."""
    src = normalize_for_pandoc(
        "\\begin{longtable}{lc}\n"
        "Variable & Source \\\\\n\\endfirsthead\n"
        "Variable & Source \\\\\n\\endhead\n"
        "Age & EHIF \\\\\n\\end{longtable}\n"
    )
    assert src.count("Variable & Source") == 1, "the repeated head must go"
    assert "Age & EHIF" in src, "the body must not"


def test_a_table_with_only_a_first_head_is_left_alone(tmp_path):
    """Most of the corpus declares only `\\endfirsthead`. Pandoc already handles
    that correctly, so touching it would be a fix in search of a bug."""
    body = ("\\begin{longtable}{lc}\nVariable & Source \\\\\n\\endfirsthead\n"
            "Age & EHIF \\\\\n\\end{longtable}\n")
    assert normalize_for_pandoc(body).count("Variable & Source") == 1


# ------------------------------------------------------------------- multirow


def test_multirow_keeps_its_content(tmp_path):
    html = render_document(
        doc("\\begin{tabular}{lc}\n\\multirow{2}{*}{\\textbf{Variable}} & 1 \\\\\n & 2 \\\\\n\\end{tabular}\n"),
        cwd=tmp_path,
        bib=None,
    )
    text = " ".join(cells(html))
    assert "Variable" in text, "the label must survive the unwrap"
    assert "multirow" not in text.lower() and "{2}" not in text


# ------------------------------------------------------ nothing else regresses


def test_normalization_leaves_prose_and_math_alone():
    src = (
        "Some prose with a \\citep{key} and math $\\alpha_i = \\frac{1}{2}$.\n\n"
        "\\input{exhibits/pval}\n"
    )
    assert normalize_for_pandoc(src) == src


def test_a_plain_table_still_renders(tmp_path):
    html = render_document(
        doc("\\begin{tabular}{lr}\n\\toprule\nA & 1 \\\\\nB & 2 \\\\\n\\bottomrule\n\\end{tabular}\n"),
        cwd=tmp_path,
        bib=None,
    )
    assert "<table" in html
    assert {"A", "B", "1", "2"} <= set(cells(html))


# --------------------------------------------- repeated column specifications


def test_esttab_star_column_spec_renders(tmp_path):
    """`l*{1}{ccccc}` is what Stata's esttab emits by default, and pandoc drops
    the whole table without a word. estonia-qbs lost five of its seven tables
    to this."""
    html = render_document(
        doc(
            "\\begin{tabular}{l*{1}{ccccc}}\n\\toprule\n"
            " & count & mean & sd & min & max \\\\\n\\midrule\n"
            "Panel size & 749 & 1751.77 & 351.57 & 271.00 & 2921.00 \\\\\n"
            "\\bottomrule\n\\end{tabular}\n"
        ),
        cwd=tmp_path,
        bib=None,
    )
    assert "<table" in html, "the table must survive at all"
    text = " ".join(cells(html))
    assert "1751.77" in text and "2921.00" in text


def test_a_multiplier_greater_than_one_repeats(tmp_path):
    from manuscriptor.render.pandoc import _plain_colspec

    assert _plain_colspec("l*{3}{cc}") == _plain_colspec("lcccccc")


def test_nothing_happens_to_a_spec_without_a_star():
    from manuscriptor.render.pandoc import _plain_colspec

    assert _plain_colspec("lrc") == "lrc"


# --------------------------------------------------- wide tables and the page


def test_a_table_gets_its_own_scroll_container(tmp_path):
    """A regression table with eight columns is wider than the reading measure.
    It must scroll inside itself, or the manuscript column scrolls sideways and
    takes the prose with it."""
    from manuscriptor.render.postprocess import _wrap_tables

    out = _wrap_tables("<p>prose</p><table><tr><td>1</td></tr></table><p>more</p>")
    assert out.count('<div class="table-scroll">') == 1
    assert out.index('<div class="table-scroll">') < out.index("<table")
    assert "</table></div>" in out
    assert "<p>prose</p>" in out and "<p>more</p>" in out


def test_every_table_is_wrapped_not_just_the_first():
    from manuscriptor.render.postprocess import _wrap_tables

    out = _wrap_tables("<table>a</table><table>b</table>")
    assert out.count('class="table-scroll"') == 2
    assert out.count("</table></div>") == 2


# ------------------------------------------------------------------- makecell


def test_makecell_does_not_shatter_the_table(tmp_path):
    """The worst exhibit bug in the corpus, and entirely silent.

    `makecell` stacks a coefficient over its standard error inside ONE cell,
    using `\\\\` as an internal line break. Pandoc reads that `\\\\` as a row
    separator, so a seven-column regression table renders with column one
    populated, columns two to seven empty, and every standard error on a row of
    its own. estonia-ecm's hospitalization and mortality table looked like a
    single column of numbers on screen.
    """
    html = render_document(
        doc(
            "\\begin{tabular}{lcc}\n\\toprule\n"
            "Variable & Design & IV \\\\\n\\midrule\n"
            "ECM patient & \\makecell{-0.021$^{*}$ \\\\ (0.011)} & \\makecell{-0.025 \\\\ (0.015)} \\\\\n"
            "\\bottomrule\n\\end{tabular}\n"
        ),
        cwd=tmp_path,
        bib=None,
    )
    body = [r for r in rows(html) if r]
    data = [r for r in body if "ECM patient" in r]
    assert len(data) == 1, f"the row was split: {body}"
    row = data[0]
    assert "0.021" in row and "(0.011)" in row, f"coefficient and SE lost: {row}"
    assert "0.025" in row and "(0.015)" in row, f"later columns emptied: {row}"
    assert "makecell" not in row.lower()


def test_makecell_with_an_alignment_option_is_handled(tmp_path):
    from manuscriptor.render.pandoc import normalize_for_pandoc

    out = normalize_for_pandoc("A & \\makecell[lc]{x \\\\ y} & B \\\\\n")
    assert "makecell" not in out
    assert "x" in out and "y" in out
    assert out.count("\\\\") == 1, f"only the real row break may survive: {out!r}"


def test_a_row_break_outside_makecell_still_separates_rows(tmp_path):
    html = render_document(
        doc("\\begin{tabular}{lc}\nA & 1 \\\\\nB & 2 \\\\\n\\end{tabular}\n"),
        cwd=tmp_path, bib=None,
    )
    body = [r for r in rows(html) if r]
    assert len(body) == 2, f"row breaks must still work: {body}"


# ------------------------------------------------------------ the minus sign


def test_a_negative_coefficient_stays_negative(tmp_path):
    """The most dangerous defect in the corpus.

    esttab writes a minus as `\\text{-}` to get the right glyph and alignment.
    `\\text` is an amsmath command valid only in math mode, and in a table cell
    pandoc drops it silently. The result is a negative treatment effect
    displayed as a positive one, with no error and nothing on screen to suggest
    anything happened.
    """
    html = render_document(
        doc(
            "\\begin{tabular}{lc}\n"
            "ECM patient & \\makecell{\\text{-}0.021$^{*}$ \\\\ (0.011)} \\\\\n"
            "Age & \\text{-}0.003 \\\\\n"
            "\\end{tabular}\n"
        ),
        cwd=tmp_path,
        bib=None,
    )
    text = " ".join(cells(html))
    assert "0.021" in text and "0.003" in text
    for value in ("0.021", "0.003"):
        i = text.index(value)
        assert text[i - 1] in "-−", f"the minus before {value} was dropped: {text!r}"


def test_the_sign_survives_the_whole_pipeline():
    from manuscriptor.render.pandoc import normalize_for_pandoc

    out = normalize_for_pandoc("A & \\makecell{\\text{-}0.021 \\\\ (0.011)} \\\\\n")
    assert "0.021" in out
    assert "-0.021" in out or "−0.021" in out, out


def test_text_braces_do_not_reach_the_reader(tmp_path):
    html = render_document(
        doc("\\begin{tabular}{lc}\nA & \\text{n.a.} \\\\\n\\end{tabular}\n"),
        cwd=tmp_path, bib=None,
    )
    text = " ".join(cells(html))
    assert "\\text" not in text and "{" not in text, text
    assert "n.a." in text


# ------------------------------------------------------- significance markers


def test_significance_stars_are_superscripts_not_latex():
    """Pandoc emits `\\(^{*}\\)` expecting MathJax. The page is self-contained and
    carries none, so a reader sees the markup instead of the stars, in every
    cell of every regression table."""
    from manuscriptor.render.postprocess import _simple_math_to_html

    out = _simple_math_to_html('<td>-0.021<span class="math inline">\\(^{*}\\)</span> (0.011)</td>')
    assert "<sup>*</sup>" in out
    assert "\\(" not in out and "math inline" not in out
    assert "-0.021" in out and "(0.011)" in out


def test_three_stars_and_subscripts_too():
    from manuscriptor.render.postprocess import _simple_math_to_html

    assert "<sup>***</sup>" in _simple_math_to_html('<span class="math inline">\\(^{***}\\)</span>')
    assert "<sub>i</sub>" in _simple_math_to_html('<span class="math inline">\\(_{i}\\)</span>')


def test_real_math_is_left_for_mathjax():
    """Only the trivial cases are converted. Anything with structure stays as
    math, because guessing at it would be worse than not rendering it."""
    from manuscriptor.render.postprocess import _simple_math_to_html

    src = '<span class="math inline">\\(\\frac{\\alpha}{\\beta_i}\\)</span>'
    assert _simple_math_to_html(src) == src


# ----------------------------------------------------------------- \sym stars


def test_sym_significance_markers_render(tmp_path):
    """esttab defines `\\def\\sym#1{\\ifmmode^{#1}\\else\\(^{#1}\\)\\fi}` and calls it
    for every significance marker. Pandoc cannot evaluate the `\\ifmmode`
    conditional, so a literal `^` reaches the reader and the stars break across
    lines. estonia-qbs uses it 55 times."""
    html = render_document(
        doc(
            "\\def\\sym#1{\\ifmmode^{#1}\\else\\(^{#1}\\)\\fi}\n"
            "\\begin{tabular}{lc}\n"
            "Shadow Signal & -0.0104\\sym{*} \\\\\n"
            "Weight shift & -0.0785\\sym{***} \\\\\n"
            "\\end{tabular}\n"
        ),
        cwd=tmp_path,
        bib=None,
    )
    text = " ".join(cells(html))
    assert "^" not in text, f"a literal caret reached the page: {text!r}"
    assert "sym" not in text.lower()
    assert "-0.0104" in text and "-0.0785" in text
    assert "<sup>" in html or "<msup" in html, "the stars must be superscripts"


def test_sym_is_expanded_only_when_the_manuscript_defines_it():
    """Expanding a macro the document never declared would be inventing meaning."""
    from manuscriptor.render.pandoc import normalize_for_pandoc

    undefined = "A & 1\\sym{*} \\\\\n"
    assert normalize_for_pandoc(undefined) == undefined


def test_sym_definition_is_removed_once_expanded():
    from manuscriptor.render.pandoc import normalize_for_pandoc

    out = normalize_for_pandoc(
        "\\def\\sym#1{\\ifmmode^{#1}\\else\\(^{#1}\\)\\fi}\nA & 1\\sym{**} \\\\\n"
    )
    assert "\\sym" not in out
    assert "**" in out


# ------------------------------------------------ anchors on their own content


def test_an_empty_anchor_is_hoisted_onto_what_it_anchors():
    """Pandoc emits a marker before a heading or a float as its own empty
    paragraph. 123 of them in estonia-ecm, each an empty block sitting in the
    manuscript. The anchor belongs on the thing it anchors."""
    from manuscriptor.render.postprocess import _hoist_empty_anchors

    out = _hoist_empty_anchors('<p data-mx="b-1"></p>\n<h1 id="intro">Introduction</h1>')
    assert "<p data-mx" not in out, "the empty paragraph must go"
    assert '<h1 id="intro" data-mx="b-1">' in out or 'data-mx="b-1"' in out.split("<h1")[1]
    assert "Introduction" in out


def test_it_works_for_floats_and_paragraphs_too():
    from manuscriptor.render.postprocess import _hoist_empty_anchors

    for tag, close in (("figure", "figure"), ("p", "p"), ("div", "div")):
        out = _hoist_empty_anchors(f'<p data-mx="b-2"></p><{tag} class="x">c</{close}>')
        assert out.count("data-mx") == 1, out
        assert f"<{tag}" in out and 'data-mx="b-2"' in out
        assert not out.startswith("<p data-mx")


def test_an_anchor_is_never_overwritten():
    """If the next element already carries a block id, the empty paragraph stays
    rather than two blocks fighting over one element."""
    from manuscriptor.render.postprocess import _hoist_empty_anchors

    src = '<p data-mx="b-1"></p><p data-mx="b-2">text</p>'
    assert _hoist_empty_anchors(src) == src


def test_a_non_empty_anchor_is_left_alone():
    from manuscriptor.render.postprocess import _hoist_empty_anchors

    src = '<p data-mx="b-1">real prose</p><h1>H</h1>'
    assert _hoist_empty_anchors(src) == src
