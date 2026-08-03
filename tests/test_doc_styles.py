"""Two CSS declarations that decided a table's shape, on the page as served.

WHAT THESE ASSERT, AND WHAT THEY DO NOT. jsdom does no layout, so nothing in
this file measures a width or a height; every box it could report is zero. What
it asserts is the cascade -- the declared value in force on a real element of a
real rendered manuscript -- which is the thing that was wrong in both defects
below. The layout numbers are in the docstrings, measured in Chrome against a
served manuscript, because that is the only honest place for them.

Both defects were long-standing rather than regressions: the output is
byte-identical back through 5356c82.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tests import pagedriver
from manuscriptor.server import app

WHY = pagedriver.missing()
pytestmark = pytest.mark.skipif(bool(WHY), reason=str(WHY))


# `\hspace{0.3cm}` in the stub is what covet-india's Table 1 does to indent a
# row under its panel heading. Pandoc keeps it as a RawInline under
# `latex+raw_tex`, which forces that one cell from `Plain` to `Para` -- so it
# arrives as `<td><p>Facilities</p></td>` beside siblings that are bare text.
# "Opening statement" is the stub whose longest word the table was being sized
# below.
TABLE = r"""\documentclass{article}
\begin{document}
\section{Results}
A paragraph of prose so that the document has an ordinary block above the table.

\begin{table}
\caption{Sample design}
\begin{tabular}{lrr}
\hline
 & Standard Case & Less-Specific Case \\
\hline
\hspace{0.3cm}Facilities & 100 & 200 \\
Opening statement & 12 & 34 \\
\hline
\end{tabular}
\end{table}
\end{document}
"""


def served(tmp_path: Path, body: str) -> str:
    (tmp_path / "main.tex").write_text(body, encoding="utf-8")
    return pagedriver.page(app.Session(tmp_path))


def test_the_page_the_probe_reads_really_has_the_cells(tmp_path):
    """The fixture has to produce the two cell shapes, or both guards are vacuous."""
    page = served(tmp_path, TABLE)
    assert "<td style=\"text-align: left;\"><p>Facilities</p></td>" in page, \
        "pandoc no longer wraps the \\hspace cell in a <p>; the guard below is moot"
    assert ">Opening statement</td>" in page


def test_a_table_cell_does_not_inherit_break_anywhere(tmp_path):
    """`overflow-wrap: anywhere` must not reach a table cell.

    `anywhere` differs from `break-word` in exactly the way that matters to a
    table: it removes the min-content floor from intrinsic sizing, so auto
    layout is free to size a column narrower than its own longest word and then
    break that word in half. Measured in Chrome on covet-india's Table 1, at a
    543px measure: the stub column came out 76px wide with the rule and 95px
    without it, and its content box, 60px, was narrower than the single word
    "statement" (69px) -- which is why the page read "Opening statemen / t".

    The declaration lived on `.doc-inner`, wrapping the whole manuscript, and
    was there for long unbroken strings in prose. `.doc a, .doc code, .doc tt`
    already carries that, scoped, so the blanket was a second implementation of
    a rule that already existed.
    """
    page = served(tmp_path, TABLE)
    got, = pagedriver.computed(
        page, [{"sel": ".doc table td", "inherited": ["overflow-wrap", "word-break"]}],
        tmp_path=tmp_path,
    )
    assert got["found"], "no table cell on the page"
    wrap = got["inherited"]["overflow-wrap"]
    assert wrap["value"] != "anywhere", (
        f"a table cell inherits overflow-wrap:anywhere from <{wrap['from']}>, "
        "so its column can be sized below its longest word and break it mid-word"
    )
    brk = got["inherited"]["word-break"]
    assert brk["value"] not in ("break-all",), \
        f"a table cell inherits word-break:{brk['value']} from <{brk['from']}>"


def test_a_long_url_in_prose_still_breaks(tmp_path):
    """Removing the blanket must not let a bibliography DOI push the page sideways.

    The scoped rule has to keep covering what the blanket was covering. On
    covet-india every one of the 65 tokens of 22 characters or more in the
    rendered manuscript sits inside an `<a>`; none was outside `a`, `code` or
    `tt`.
    """
    body = TABLE.replace(
        "A paragraph of prose so that the document has an ordinary block above the table.",
        "A paragraph naming \\url{https://doi.org/10.1111/j.2517-6161.1995.tb02031.x} "
        "and \\texttt{a\\_very\\_long\\_unbroken\\_identifier\\_indeed} in passing.",
    )
    page = served(tmp_path, body)
    probes = pagedriver.computed(
        page,
        [{"sel": ".doc a", "props": ["overflow-wrap", "word-break"]},
         {"sel": ".doc code", "props": ["overflow-wrap", "word-break"]}],
        tmp_path=tmp_path,
    )
    for got in probes:
        assert got["found"], f"nothing matched {got['sel']}; the fixture moved"
        assert got["declared"]["overflow-wrap"] == "anywhere", \
            f"{got['sel']} lost its break opportunity: {got['declared']}"


def test_a_paragraph_in_a_cell_carries_no_vertical_margin(tmp_path):
    """A `<td>`'s `<p>` must not push its own row open.

    Pandoc emits `<td><p>Facilities</p></td>` for the one cell holding a
    RawInline, and bare text for its siblings. Nothing reset `p` margins inside
    a cell, so that cell alone got the body paragraph's margin: measured in
    Chrome on covet-india's Table 1, 13.12px top and bottom, a row 51px tall
    against 25px for a row without the `<p>`, and the row's label sitting 16px
    BELOW the numbers it labels (rect.top 580.7 against 564.4).
    """
    page = served(tmp_path, TABLE)
    got, = pagedriver.computed(
        page, [{"sel": ".doc table td p", "props": ["margin-top", "margin-bottom"]}],
        tmp_path=tmp_path,
    )
    assert got["found"], "the fixture stopped producing a <p> inside a <td>"
    assert got["declared"]["margin-top"] == "0px", \
        f"a <p> in a cell keeps margin-top {got['declared']['margin-top']}"
    assert got["declared"]["margin-bottom"] == "0px", \
        f"a <p> in a cell keeps margin-bottom {got['declared']['margin-bottom']}"
