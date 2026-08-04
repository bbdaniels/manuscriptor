"""Reading the page footers back off a finished PDF, and failing the compile
when they disagree with the document.

WHY THIS EXISTS. `\\pageref{LastPage}` is a BACKWARD reference. The `lastpage`
package writes the label at the end of a run, and every footer on every page
reads it out of the PREVIOUS run's `.aux`. So if the page count changes on the
last pass that runs, the correct total is written into the `.aux` and read by
nobody, and the PDF ships with a whole run of wrong footers. Nothing in LaTeX
notices: exit 0, no warning, a file that opens and is quietly wrong.

Observed on covet-india, deterministically, from a clean tree:

    pass 1  no `.bbl` yet, so no bibliography                    17 pages
    pass 2  the bibliography is typeset                          21 pages
    pass 3  the citation superscripts render for the first time
            and the reflow adds a page                           22 pages

The page count changed on pass 3, which was the last pass the recipe ran. Every
footer in the shipped PDF said "/21" and the final page said "22/21".

`compile_pdf` now iterates pdflatex to a stable `.aux`, which removes the cause.
This module is the gate that keeps it removed, and it reads the numbers off the
finished PDF because that is the only place the number a reader actually sees
exists.

THREE THINGS ARE ANCHORED HERE, AND THE FIRST TWO ARE WHERE THE AUTHOR'S OWN
SCRIPT CANNOT BE COPIED LITERALLY.

**The footer is found by POSITION, not by pattern.** A document may legitimately
print "n/m" in its own content -- a figure data label, a ratio in a table cell --
so the first `n/m` on a page is not the footer. The search is confined to a band
along the bottom of the page and takes the lowest line in it.

**The band is a fraction of the page height, not a fixed 45pt.** The
covet-india script's band is tuned to `wlscirep.cls`, which sits its footer 32pt
off the bottom edge. A plain `article` under `fancyhdr` puts it at 91pt, and a
45pt band finds nothing there at all -- which in a checker that treats "no
footer" as a failure fails every document it was not tuned for.

**A document with no total-pages footer is not a failing document.** That script
is run against two documents known to carry one, so it can report their absence
as a problem. Manuscriptor compiles whatever the author has, and most classes
print a bare `\\thepage`; failing those would replace a bug that is silent and
rare with one that is loud and wrong every time. The check applies only once the
document is seen to use an "n/m" footer.

The numbering is judged AGAINST ITSELF rather than against the sheet count: a
paper with separately numbered front matter prints "12/12" on the fifteenth
piece of paper, and comparing the printed total to the physical page count would
fail it forever. The offset is read off the first footered page and every other
footer, including the total, is required to agree with it.
"""
from __future__ import annotations

import re
from pathlib import Path

# THE READER IS pdfminer.six, WHICH IS ALREADY A HARD DEPENDENCY of this
# package, and that is the whole argument. The covet-india script uses PyMuPDF;
# adopting it here would add a second PDF engine and a large binary wheel to a
# project whose `pyproject.toml` records, in as many words, that pdfannots was
# chosen over PyMuPDF precisely because it "adds no new transitive tree at all".
# `pdftotext` was the other candidate and was rejected for a sharper reason: it
# is an external binary that may simply be absent, and a gate that skips itself
# when a binary is missing is the exact failure this module exists to stop. What
# pdfminer gives is installed with Manuscriptor, so the gate always runs.
from pdfminer.high_level import extract_pages
from pdfminer.layout import LAParams, LTTextContainer, LTTextLine
from pdfminer.pdfparser import PDFSyntaxError


class Unreadable(Exception):
    """The PDF could not be parsed, so nothing can be said about its footers."""


# A fraction of the page height rather than an absolute drop: see the module
# docstring. 0.14 of US Letter is 111pt, which clears `wlscirep.cls` at 32pt and
# `fancyhdr` on `article` at 91pt, and still sits below the body text of both.
BAND_FRACTION = 0.14

# The line must be a footer and not a sentence that happens to contain a ratio.
# A short run of non-digits ahead of the numbers is common -- a running head, a
# separator -- but almost nothing follows them, so the tail is kept tight: it is
# the tail that separates "3/21" from "3/4 of the sample was reached".
FOOTER_RE = re.compile(r"^\D{0,24}?(\d+)\s*/\s*(\d+)\D{0,3}$")


def _lines(container) -> list:
    out = []
    for element in container:
        if isinstance(element, LTTextLine):
            out.append(element)
        elif isinstance(element, LTTextContainer):
            out.extend(_lines(element))
    return out


def footers(path) -> tuple[int, dict[int, tuple[int, int]]]:
    """`(page count, {sheet: (printed page, printed total)})`.

    A sheet is absent from the mapping when its bottom band holds no footer,
    which is the ordinary case for a title page under `\\thispagestyle{empty}`.
    """
    found: dict[int, tuple[int, int]] = {}
    count = 0
    try:
        pages = list(extract_pages(str(path), laparams=LAParams()))
    except PDFSyntaxError as exc:
        raise Unreadable(f"{path} could not be read as a PDF: {exc}") from exc
    except Exception as exc:  # pdfminer raises a wide family on damaged files
        raise Unreadable(f"{path} could not be read as a PDF: {exc}") from exc
    for count, page in enumerate(pages, start=1):
        ceiling = page.bbox[1] + page.height * BAND_FRACTION
        band = [ln for ln in _lines(page) if ln.y0 < ceiling]
        for line in sorted(band, key=lambda ln: ln.y0):
            m = FOOTER_RE.match(" ".join(line.get_text().split()))
            if m:
                found[count] = (int(m.group(1)), int(m.group(2)))
                break
    return count, found


def check(path) -> list[str]:
    """Everything wrong with this PDF's printed pagination, in the author's
    terms. An empty list is a document whose footers can be trusted, or one that
    does not print a total at all."""
    total, found = footers(path)
    if not found:
        # Not an unfootered document's failure. See the module docstring.
        return []

    problems: list[str] = []
    first = min(found)
    # How the printed numbering is offset from the sheets, read off the document
    # rather than assumed, so separately numbered front matter is not a bug.
    offset = found[first][0] - first
    last_printed = total + offset

    if total not in found:
        problems.append(
            f"{Path(path).name}: the last page ({total}) carries no footer, so "
            f"the page count printed in the document cannot be verified. The "
            f"pass that would have written it did not run.")

    for sheet, (shown_page, shown_total) in sorted(found.items()):
        if shown_page != sheet + offset:
            problems.append(
                f"{Path(path).name} page {sheet}: footer reads "
                f"\"{shown_page}/{shown_total}\", so \\thepage does not match "
                f"the sheet.")
        if shown_total != last_printed:
            tail = (f"but the document is {total} pages" if offset == 0
                    else f"but its last page is numbered {last_printed}")
            problems.append(
                f"{Path(path).name} page {sheet}: footer reads "
                f"\"{shown_page}/{shown_total}\" {tail}. \\pageref{{LastPage}} "
                f"resolved from a stale .aux.")
    return problems


def summarize(problems: list[str]) -> str:
    """One line per KIND of problem, not one per page.

    A stale `LastPage` is wrong on every page at once, and an author handed
    twenty-two copies of the same sentence has been told the same thing
    twenty-two times and the cause zero times.

    The exemplar kept is the LAST page carrying the problem rather than the
    first, because the last page is where the discrepancy is legible: "22/21"
    on the final sheet says what went wrong, where "1/21" on the first sheet
    could be almost anything.
    """
    first: dict[str, str] = {}
    seen: dict[str, int] = {}
    for p in problems:
        key = re.sub(r"\d+", "#", p)
        first[key] = p  # the LAST page, deliberately: see the docstring
        seen[key] = seen.get(key, 0) + 1
    lines = []
    for key, exemplar in first.items():
        more = seen[key] - 1
        lines.append(exemplar + (f" (and {more} further page{'s' if more > 1 else ''})"
                                 if more else ""))
    return "\n".join(lines)
