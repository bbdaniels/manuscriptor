r"""The notes belong to the exhibit, and the page has to say so.

An exhibit's notes are where the stars are defined, the clustering is stated and
the sample is named. In the compiled PDF they are part of the float: small type,
under the table, inside the frame. On the page they were none of those things.

qutub-ayush's `tab:main` is the case that opened this. The float holds a
caption, an `\input`ed tabular, a `\medskip`, and then a `\footnotesize` notes
paragraph. Pandoc hands the table back as one element and the notes back as the
NEXT one, and by then nothing in the HTML says they were ever together: the
notes rendered at full body size, outside the framed card, and clicking them
selected nothing at all, because the block's `data-mx` sits on the table and the
notes paragraph was outside the element carrying it. Those bytes are in the
block. The reader could not reach them.

WHAT DECIDES MEMBERSHIP IS THE BLOCK, and that is the whole design. The author
wrote the exhibit and its notes into one block; `server/blocks.py` computed that
extent from the source and `render/anchors.py` wrote it into the HTML as the
markers. So `render/cards.py` only has to READ what is already declared -- it
never asks what a paragraph says, and in particular it never looks for the word
"Notes:", which the corpus spells six different ways and sometimes not at all.

The block scope is also what draws the line correctly at the other end.
covet-india and sdi-caseloads write their exhibits without a float, as a
`\subsection*` heading, an `\input`, and a separate note paragraph -- and those
are separate BLOCKS, each with its own anchor. Folding them would put two block
ids on one card, which is the one thing the anchor contract forbids. They are
left alone, and that falls out of the rule rather than being a special case in
it.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from manuscriptor.render.cards import fold_exhibit_notes
from manuscriptor.render.pandoc import normalize_for_pandoc, render_document
from manuscriptor.render.postprocess import postprocess


# ------------------------------------------------------------------ fixtures

TABLE = (
    '<figure class="ms-table" data-mx="b-1">'
    '<div class="table-scroll"><table><tbody><tr><td>1</td></tr></tbody></table></div>'
    "<figcaption>Impact of PPIA</figcaption>"
    "</figure>"
)


def notes(text: str = "Notes: clustered by provider.") -> str:
    return f"<p>{text}</p>"


def body(text: str = "The next paragraph of the paper.") -> str:
    return f'<p data-mx="b-2">{text}</p>'


# --------------------------------------------------------------- the fold

def test_a_trailing_notes_paragraph_moves_inside_the_card():
    out, n = fold_exhibit_notes(TABLE + notes() + body())
    assert n == 1
    card = out[out.index('<figure class="ms-table"'): out.index("</figure>") + len("</figure>")]
    assert "clustered by provider" in card
    # And nothing is left behind between the card and the next block.
    assert out.index("clustered by provider") < out.index("</figure>")


def test_the_notes_carry_the_notes_class():
    out, _ = fold_exhibit_notes(TABLE + notes())
    assert '<p class="ms-notes">Notes: clustered by provider.</p>' in out


def test_the_notes_land_inside_the_element_that_carries_the_block_id():
    """The bug the author saw: the notes were unclickable.

    A click resolves to the nearest ancestor carrying `data-mx`. Outside the
    card there is no such ancestor, so the notes selected nothing.
    """
    out, _ = fold_exhibit_notes(TABLE + notes())
    anchor = out.index('data-mx="b-1"')
    close = out.index("</figure>", anchor)
    assert anchor < out.index("clustered by provider") < close


def test_several_trailing_paragraphs_all_fold():
    out, n = fold_exhibit_notes(TABLE + notes("First note.") + notes("Second note.") + body())
    assert n == 1
    card = out[: out.index("</figure>")]
    assert "First note." in card and "Second note." in card
    assert out.index("First note.") < out.index("Second note.")


def test_body_prose_owned_by_another_block_is_left_alone():
    out, _ = fold_exhibit_notes(TABLE + body())
    assert "ms-notes" not in out
    assert out.index("</figure>") < out.index("The next paragraph")


def test_an_exhibit_with_no_notes_is_returned_untouched():
    html = TABLE + body()
    assert fold_exhibit_notes(html) == (html, 0)


def test_a_paragraph_holding_only_an_image_is_not_notes():
    """estonia-ecm's subfigures arrive as `<p><img></p>`. They are the exhibit."""
    html = ('<figure data-mx="b-1"><p><img src="a.png" /></p>'
            "<figcaption>Panel A</figcaption></figure>")
    assert fold_exhibit_notes(html) == (html, 0)


def test_notes_already_inside_a_figure_are_classed_and_moved_below_the_caption():
    """estonia-ecm's Figure 2: pandoc nests the notes, ABOVE the caption."""
    html = ('<figure data-mx="b-1"><p><img src="a.png" /></p>'
            "<p>Notes: 95% confidence intervals.</p>"
            "<figcaption>Dynamics</figcaption></figure>")
    out, n = fold_exhibit_notes(html)
    assert n == 1
    assert 'class="ms-notes"' in out
    assert out.index("Dynamics") < out.index("95% confidence intervals")


def test_a_note_row_inside_the_tabular_is_not_treated_twice():
    """esttab's `addnotes` row is already in the card, as a table row."""
    html = ('<figure class="ms-table" data-mx="b-1"><div class="table-scroll"><table><tbody>'
            "<tr><td>1</td></tr>"
            '<tr><td colspan="7">Multiple-hypothesis correction renders it insignificant.</td></tr>'
            "</tbody></table></div><figcaption>C</figcaption></figure>")
    out, n = fold_exhibit_notes(html)
    assert n == 0
    assert out == html


def test_a_captionless_table_gets_a_card_of_its_own():
    html = ('<div class="table-scroll" data-mx="b-1"><table><tbody><tr><td>1</td></tr>'
            "</tbody></table></div>" + notes())
    out, n = fold_exhibit_notes(html)
    assert n == 1
    assert 'class="ms-exhibit"' in out
    assert out.index('data-mx="b-1"') < out.index("clustered by provider")
    # The id moved to the card, so exactly one element still carries it.
    assert out.count('data-mx="b-1"') == 1


# ------------------------------------------------- the shape pandoc 3.1.1 emits

# What `\label{tab:main}` inside a table float comes back as on pandoc 3.1.1 --
# the binary the author's own server resolves, because `/usr/local/bin` precedes
# `/opt/homebrew/bin` on his PATH -- after `_wrap_tables` has made the card. The
# label becomes a `<div>` AROUND the table, so the block's `data-mx` lands on the
# div and the exhibit is its grandchild, while the notes are the div's sibling.
# On 3.10.1 the same source puts the id on the `<table>` and no div exists at
# all. Two element trees, one manuscript: the fold matched only the second, so
# qutub-ayush's `tab:main` notes sat outside the card on the machine the author
# was reading, under a corpus check that reported them folded.
WRAPPED = (
    '<div id="tab:main" data-mx="b-1">\n'
    '<figure class="ms-table"><div class="table-scroll">'
    "<table><tbody><tr><td>1</td></tr></tbody></table></div>"
    "<figcaption>Impact of PPIA</figcaption></figure>\n"
    "</div>"
)


def test_notes_fold_when_pandoc_wraps_the_exhibit_in_a_labelled_div():
    out, n = fold_exhibit_notes(WRAPPED + notes() + body())
    assert n == 1, "the exhibit is a grandchild of the block element, not a root sibling"
    assert out.index("clustered by provider") < out.index("</figure>")


def test_the_wrapped_notes_land_inside_both_the_card_and_the_block_element():
    """The two containments the author sees: the frame, and the click target."""
    out, _ = fold_exhibit_notes(WRAPPED + notes() + body())
    anchor = out.index('data-mx="b-1"')
    card = out.index('<figure class="ms-table"')
    assert anchor < card < out.index("clustered by provider") < out.index("</figure>")
    assert out.index("</figure>") < out.index("</div>", out.index("</figure>"))
    assert 'class="ms-notes"' in out


def test_a_wrapper_holding_prose_of_its_own_is_not_reached_into():
    """A div the author built with text beside the table is his layout, not ours."""
    html = ('<div data-mx="b-1">Framing sentence.'
            '<figure class="ms-table"><div class="table-scroll">'
            "<table><tbody><tr><td>1</td></tr></tbody></table></div>"
            "<figcaption>C</figcaption></figure></div>" + notes())
    assert fold_exhibit_notes(html) == (html, 0)


def test_the_fold_is_idempotent():
    once, _ = fold_exhibit_notes(TABLE + notes())
    twice, n = fold_exhibit_notes(once)
    assert twice == once and n == 0


# ------------------------------------------------------- through the renderer

def render(body_tex: str, tmp_path: Path) -> str:
    src = ("\\documentclass{article}\n\\usepackage{graphicx,booktabs,longtable}\n"
           "\\begin{document}\n" + body_tex + "\n\\end{document}\n")
    return render_document(normalize_for_pandoc(src, cwd=tmp_path), cwd=tmp_path, bib=None)


TABULAR = ("\\begin{tabular}{lc}\n\\toprule\nA & B \\\\\n\\midrule\n1 & 2 \\\\\n"
           "\\bottomrule\n\\end{tabular}")


def test_a_tablenotes_environment_reaches_the_page_at_all(tmp_path):
    """It did not, for twenty of estonia-ecm's tables.

    `tablenotes` is an unknown environment to pandoc, and inside the `center`
    that wraps every one of those tables pandoc DROPS it -- the whole note, at
    exit 0, with the table still rendering. The notes were not misplaced on the
    page; they were not on the page. Unwrapping it is what the rest of the
    wrapper environments already get.
    """
    html = render("\\begin{center}\n" + TABULAR
                  + "\n\\begin{tablenotes}\n\\item Notes: clustered by provider.\n"
                    "\\end{tablenotes}\n\\end{center}", tmp_path)
    assert "clustered by provider" in html


def test_the_trailing_notes_of_a_real_float_end_up_in_the_card(tmp_path):
    html = render("\\begin{table}\n\\caption{Impact}\n" + TABULAR
                  + "\n\\medskip\n\n\\footnotesize\\textit{Notes:} clustered by provider.\n"
                    "\\end{table}\n\nBody prose follows.", tmp_path)
    out, n = fold_exhibit_notes(_wrap(html))
    assert n == 1
    assert out.index("clustered by provider") < out.index("</figure>")


def _wrap(html: str) -> str:
    """Give the table the card and the anchor the real pass gives it."""
    from manuscriptor.render.postprocess import _wrap_tables

    html = html.replace("<table>", '<table data-mx="b-1">', 1)
    return _wrap_tables(html)


# --------------------------------------------------------------- the Word path

def test_no_marking_reaches_the_latex_the_word_path_compiles(tmp_path):
    """The fold is an HTML pass and adds nothing to the LaTeX.

    `server/compile.py` feeds `normalize_for_pandoc` straight to pandoc and on
    to the `.docx`, so a token minted here would ship as a literal glyph in a
    journal submission.
    """
    src = ("\\documentclass{article}\n\\begin{document}\n\\begin{table}\n"
           "\\caption{C}\n" + TABULAR + "\n\nNotes: hi.\n\\end{table}\n\\end{document}\n")
    out = normalize_for_pandoc(src)
    assert "ms-notes" not in out and "ms-exhibit" not in out
    assert "MXNOTE" not in out


# ----------------------------------------------------------------- one home

def test_nothing_else_decides_what_belongs_to_an_exhibit_card():
    """One home, and this is the guard on it.

    The same question answered in two places is how the header-row repair and
    the column-type repair each grew a second copy. Membership in a card is
    decided in `render/cards.py`; everything else calls it.
    """
    root = Path(__file__).resolve().parent.parent / "manuscriptor"
    offenders = []
    for path in root.rglob("*.py"):
        if path.name == "cards.py":
            continue
        text = path.read_text(encoding="utf-8")
        for needle in ("ms-notes", "ms-exhibit", "ms-title"):
            if needle in text:
                offenders.append(f"{path}: {needle}")
    assert offenders == [], offenders
