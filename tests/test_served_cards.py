r"""What the SERVED page says about an exhibit card, asked over the real route.

The fold was written, tested and verified across the corpus, and the author's
own page still showed the notes outside the card. Every assertion made about it
was made about `fold_exhibit_notes` or about `postprocess`, and the page is
neither: it is what `GET /` returns from a `Session` that built the manuscript
the way a serve builds it. That gap is the same one `tests/test_live_frames.py`
exists to close on the push path -- the function was right and the page was
wrong, under a suite that passed.

So these ask the route. A block's notes have to be inside the element carrying
its `data-mx`, or a click on them selects nothing; and inside the card, or they
read as another paragraph of the paper. Both are properties of the delivered
HTML, and neither is inspectable from a unit test on a fixture string.

The version-specific shape that actually caused it -- pandoc 3.1.1 wrapping a
labelled table float in a `<div id="tab:main">` -- is pinned deterministically in
`tests/test_exhibit_notes.py`, because this file renders with whatever pandoc
the machine has and cannot choose.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from manuscriptor.render import cards
from manuscriptor.server import app

BODY = r"""\documentclass{article}
\usepackage{graphicx,booktabs}
\begin{document}
An opening paragraph, long enough to be a block of its own and to keep the
exhibits below it from being the first thing in the document.

\begin{table}[htbp]
\caption{Impact of PPIA on the Three Outcome Families}
\label{tab:main}
\centering
\begin{tabular}{lc}
\toprule
Outcome & Effect \\
\midrule
Correct case management & 0.21 \\
\bottomrule
\end{tabular}

\medskip

{\footnotesize \textit{Notes:} Standard errors are clustered by provider, and
the sample is the experimental cohort.}
\end{table}

\begin{figure}[htbp]
\centering
\includegraphics{panel.png}
\caption{Component Outcomes by Trial Arm and Round. Shares are unadjusted for
multiple comparisons, and the bands are ninety-five percent intervals.}
\label{fig:panel}
\end{figure}

A closing paragraph, so the figure is not the last element of the document.
\end{document}
"""


def served(tmp_path: Path) -> str:
    """The page the route returns for a real manuscript, built as a serve builds it."""
    (tmp_path / "main.tex").write_text(BODY, encoding="utf-8")
    (tmp_path / "panel.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
    session = app.Session(tmp_path)

    from aiohttp.test_utils import TestClient, TestServer

    async def go() -> str:
        async with TestClient(TestServer(app.make_app(session))) as client:
            r = await client.get("/")
            assert r.status == 200
            return await r.text()

    return asyncio.run(go())


# --------------------------------------------------------------- the tree

def ancestors(html: str, needle: str) -> list:
    """Every element containing `needle`, outermost first."""
    at = html.index(needle)
    out = []

    def walk(nodes):
        for node in nodes:
            if node.start <= at < node.end:
                out.append(node)
                walk(node.children)

    walk(cards._parse(html))
    return out


def classes(node) -> set[str]:
    import re

    m = re.search(r'class="([^"]*)"', node.attrs)
    return set(m.group(1).split()) if m else set()


@pytest.fixture(scope="module")
def page(tmp_path_factory) -> str:
    return served(tmp_path_factory.mktemp("cards"))


# ------------------------------------------------------- the table's notes

def test_the_served_page_puts_the_table_notes_inside_the_card(page):
    chain = ancestors(page, "clustered by provider")
    assert any("ms-table" in classes(n) or "ms-exhibit" in classes(n)
               for n in chain), "the notes render outside the exhibit's card"


def test_the_served_notes_are_inside_the_element_a_click_resolves(page):
    """A click walks up to the nearest `data-mx`. Outside it, there is none."""
    chain = ancestors(page, "clustered by provider")
    assert any(cards._block_id(n) for n in chain), (
        "no ancestor of the notes carries a block id, so clicking them selects nothing")


def test_the_served_notes_are_subordinated_rather_than_body_prose(page):
    chain = ancestors(page, "clustered by provider")
    assert any(cards.NOTES_CLASS in classes(n) for n in chain)


# ------------------------------------------------ one anatomy for both kinds

def test_both_kinds_of_exhibit_carry_a_title_line(page):
    for name in ("Impact of PPIA on the Three Outcome Families",
                 "Component Outcomes by Trial Arm and Round"):
        chain = ancestors(page, name)
        assert any(cards.TITLE_CLASS in classes(n) for n in chain), (
            f"{name!r} is not marked as the exhibit's title")


def test_a_caption_carrying_its_notes_is_split_at_the_first_sentence(page):
    """The figure's caption is a title and a note, not one wall of serif."""
    title = ancestors(page, "Component Outcomes by Trial Arm and Round")
    assert all(cards.NOTES_CLASS not in classes(n) for n in title)
    rest = ancestors(page, "unadjusted for")
    assert any(cards.NOTES_CLASS in classes(n) for n in rest)
    assert all(cards.TITLE_CLASS not in classes(n) for n in rest)


def test_the_split_moves_words_and_never_drops_them(page):
    for phrase in ("Component Outcomes by Trial Arm and Round",
                   "unadjusted for", "ninety-five percent intervals"):
        assert phrase in page


def test_the_figure_notes_stay_inside_the_figure_card(page):
    chain = ancestors(page, "ninety-five percent intervals")
    assert any(n.tag == "figure" for n in chain)
