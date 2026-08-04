"""Reading the printed page footers back off a finished PDF.

THE BUG THIS EXISTS FOR SHIPS AT EXIT 0. `\\pageref{LastPage}` is a backward
reference: the `lastpage` package writes the label at the END of a run and every
footer reads it from the PREVIOUS run's `.aux`. If the page count changes on the
LAST pass that runs, the right total is written and read by nobody, and the PDF
carries a whole run of wrong footers with no error anywhere. Observed on
covet-india: 22 pages, every footer reading "/21", the last page reading
"22/21".

The compile loop that iterates to a stable `.aux` removes the cause. This is the
gate that keeps it removed, and it reads the numbers off the finished PDF
because that is the only place the number a reader actually sees exists.

TWO THINGS ARE ANCHORED HERE AND BOTH WERE GOT WRONG FIRST.

**The footer is found by position, not by pattern.** A document may legitimately
print "n/m" in its own content -- a figure data label, a ratio in a table cell --
so the first `n/m` on a page is not the footer. The match is confined to the
bottom band of the page and takes the lowest line in it.

**The band is a fraction of the page, not a fixed 45pt.** The covet-india
script's band is tuned to `wlscirep.cls`, whose footer sits 32pt off the bottom
edge. A plain `article` with `fancyhdr` puts it at 91pt, and a fixed 45pt band
finds NOTHING there -- which, in a checker that treats "no footer" as a failure,
fails every document it was not tuned for.

**A document with no such footer is not a failing document.** Manuscriptor
compiles arbitrary manuscripts and most classes print a bare `\\thepage`. The
covet-india script reports "no footer found" as a problem because it is run
against two documents that are known to have one. Here that would fail nearly
every paper, so the check only applies once the document is seen to use a
total-pages footer at all.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from manuscriptor.server import pagination
from tests.minipdf import _pdf, _two_lines


# The hand-built PDF fixture lives in `tests/minipdf.py`, because the compile
# tests need the same thing: a PDF whose footers are wrong, which no working
# LaTeX run will produce to order.


def test_the_hand_built_pdf_is_readable(tmp_path):
    """The fixture is load-bearing for everything below it, so it is asserted
    before anything is asserted through it."""
    p = _pdf(tmp_path / "a.pdf", ["1/2", "2/2"])
    total, found = pagination.footers(p)
    assert total == 2
    assert found == {1: (1, 2), 2: (2, 2)}


# ------------------------------------------------------------ the failure


def test_a_stale_total_is_caught(tmp_path):
    """The covet-india bug, in miniature: four sheets, every footer saying three.

    This is what shipped at exit 0 and what nothing else in the compile notices.
    """
    p = _pdf(tmp_path / "a.pdf", ["1/3", "2/3", "3/3", "4/3"])
    problems = pagination.check(p)
    assert problems
    said = " ".join(problems)
    assert "4/3" in said and "4 pages" in said
    assert "LastPage" in said


def test_a_document_whose_footers_agree_passes(tmp_path):
    p = _pdf(tmp_path / "a.pdf", ["1/3", "2/3", "3/3"])
    assert pagination.check(p) == []


def test_a_last_page_with_no_footer_is_a_problem(tmp_path):
    """The last sheet is the one the whole label hangs on. A document that
    footers every page but that one cannot be checked, and the reason it lost
    its footer is usually that the pass which added it never ran."""
    p = _pdf(tmp_path / "a.pdf", ["1/3", "2/3", None])
    problems = pagination.check(p)
    assert problems and "last page" in " ".join(problems)


def test_a_title_page_without_a_footer_is_fine(tmp_path):
    """`\\thispagestyle{empty}` on page one is how every one of these papers is
    typeset. Only the LAST page's footer is required."""
    p = _pdf(tmp_path / "a.pdf", [None, "2/3", "3/3"])
    assert pagination.check(p) == []


def test_a_page_number_that_is_not_its_sheet_is_caught(tmp_path):
    p = _pdf(tmp_path / "a.pdf", ["1/3", "9/3", "3/3"])
    problems = pagination.check(p)
    assert problems and "does not match the sheet" in " ".join(problems)


# --------------------------------------------------- what it must NOT do


def test_a_document_with_no_total_footer_is_not_a_failure(tmp_path):
    """Most classes print a bare `\\thepage`, and Manuscriptor compiles whatever
    the author has. A checker that fails those has replaced a silent bug with a
    loud one that is wrong every time."""
    p = _pdf(tmp_path / "a.pdf", ["1", "2", "3"])
    assert pagination.check(p) == []


def test_a_ratio_in_the_body_is_not_read_as_a_footer(tmp_path):
    """The author's own warning: a figure data label may print "n/m". The match
    is confined to the bottom band, so a "7/8" sitting in the body of a
    three-page document with no footers at all resolves to no footer, not to a
    document that is five pages short."""
    p = _pdf(tmp_path / "a.pdf", ["7/8", "7/8", "7/8"], y=400.0)
    total, found = pagination.footers(p)
    assert total == 3
    assert found == {}, "a mid-page ratio was mistaken for a page footer"
    assert pagination.check(p) == []


def test_the_footer_wins_over_a_ratio_higher_up_the_page(tmp_path):
    """Both on one page: the body ratio at 400pt, the footer at 32pt. The lowest
    line in the band is the footer, and it is the one that is read."""
    p = _two_lines(tmp_path / "b.pdf", top="3/3", bottom="1/1")
    total, found = pagination.footers(p)
    assert (total, found) == (1, {1: (1, 1)})


def test_a_footer_high_off_the_bottom_edge_is_still_found(tmp_path):
    """A plain `article` with `fancyhdr` sits its footer 91pt off the bottom, far
    outside the 45pt band the covet-india script uses for `wlscirep.cls`. A band
    tuned to one class finds nothing in the other and then reports the document
    as unfootered, which is the same bug in a different direction."""
    p = _pdf(tmp_path / "a.pdf", ["1/2", "2/2"], y=91.0)
    assert pagination.footers(p)[1] == {1: (1, 2), 2: (2, 2)}


def test_page_numbering_that_restarts_is_judged_against_itself(tmp_path):
    """Front matter numbered separately is not a stale total.

    A paper whose body restarts at 1 prints "12/12" on a sheet that is the 15th
    piece of paper. Comparing the printed total to the physical page count would
    fail that document forever. The invariant is that the total equals the page
    number printed on the last sheet, which holds under any numbering scheme.
    """
    p = _pdf(tmp_path / "a.pdf", [None, None, None, "1/3", "2/3", "3/3"])
    assert pagination.check(p) == []


def test_a_restarted_numbering_with_a_stale_total_is_still_caught(tmp_path):
    p = _pdf(tmp_path / "a.pdf", [None, None, None, "1/2", "2/2", "3/2"])
    problems = pagination.check(p)
    assert problems, "a stale total must be caught even when numbering restarts"
    assert "3/2" in " ".join(problems)


def test_a_pdf_that_cannot_be_read_says_so(tmp_path):
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"%PDF-1.4\nnot really\n")
    with pytest.raises(pagination.Unreadable):
        pagination.footers(bad)


def test_the_reference_implementation_is_not_reimplemented_twice():
    """One place decides what a page footer is.

    The covet-india script this came from is the author's, lives in his
    manuscript, and is run by his Makefile; this module is Manuscriptor's and is
    run by the compile. They are two callers of one idea, and the idea is small.
    What must not happen is a THIRD copy inside this repo.
    """
    import manuscriptor
    root = Path(manuscriptor.__file__).resolve().parent
    offenders = []
    for path in root.rglob("*.py"):
        if path.name == "pagination.py":
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        # Reading a footer off a PDF needs a PDF reader, so the tell is a
        # second module that both opens PDFs and talks about footers. Merely
        # MENTIONING the footers -- as `compile.py` does, where the gate is
        # called -- is not a second implementation of finding them.
        opens_pdfs = "pdfminer" in text or "fitz" in text
        if opens_pdfs and "footer" in text.lower():
            offenders.append(str(path))
    assert not offenders, f"the footer check is re-implemented in {offenders}"
