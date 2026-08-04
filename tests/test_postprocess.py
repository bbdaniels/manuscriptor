"""M2 — pandoc output becomes an addressable document.

Four jobs, and each of them fails in a way that is invisible if it is not
tested. A marker that never becomes a `data-mx` leaves a paragraph the margin
cannot address. A reference that resolves to nothing ships as `??`. A citation
span with no stable id cannot carry an evidence status across a re-render. An
image that is never copied renders as a broken box in a page that is otherwise
perfect, which is the kind of defect that survives a demo.

`postprocess` computes the unanchored set itself, from the blocks it was handed
against the `data-mx` values actually present in the output, rather than
trusting the harvester's own report. It is the only party that knows which
blocks were supposed to be there.
"""
from __future__ import annotations

import hashlib
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from unittest import mock

import pytest

from manuscriptor.source import anchors
from manuscriptor.render import postprocess as pp_mod
from manuscriptor.render.postprocess import postprocess


@dataclass(frozen=True)
class FakeBlock:
    """Only `.id` is read. blocks.py is being written in parallel; postprocess
    must not care which fields the real dataclass ends up carrying."""

    id: str


A = "b-3f2a91c0de"
B = "b-aa11bb22cc"
C = "b-0011223344"


def mark(block_id: str) -> str:
    return anchors.marker(block_id[2:] if block_id.startswith("b-") else block_id)


# ------------------------------------------------------- a reference harvester
#
# The real one lives in manuscriptor/source/anchors.py and is owned by another
# track. This double implements the documented contract so that the tests below
# exercise postprocess rather than the harvester; the integration test at the
# bottom runs against the real thing.


def _reference_harvest(html: str) -> str:
    """Attach each marker to the element that opened immediately before it."""
    pieces: list[str] = []
    cursor = 0
    for m in anchors.MARKER_RE.finditer(html):
        open_tag = html.rfind("<", 0, m.start())
        close = html.find(">", open_tag) if open_tag != -1 else -1
        if open_tag == -1 or close == -1 or html[open_tag + 1] == "/":
            # Nowhere to attach: drop the marker and leave the block unanchored.
            pieces.append(html[cursor:m.start()])
            cursor = m.end()
            continue
        pieces.append(html[cursor:close])
        pieces.append(f' data-mx="b-{m.group(1)}"')
        pieces.append(html[close:m.start()])
        cursor = m.end()
    pieces.append(html[cursor:])
    return "".join(pieces)


@pytest.fixture()
def harvester(monkeypatch):
    monkeypatch.setattr(anchors, "harvest", _reference_harvest)
    return _reference_harvest


# --------------------------------------------------------------- return shape


def test_postprocess_returns_the_four_documented_keys(tmp_path, harvester):
    out = postprocess(
        f"<p>{mark(A)}Prose.</p>",
        blocks=(FakeBlock(A),),
        manuscript_dir=tmp_path,
        output_dir=tmp_path / "out",
        labels={},
    )
    assert set(out) == {"html", "unanchored", "unresolved_refs", "assets"}


# ------------------------------------------------------------------- anchors


def test_a_marker_becomes_a_data_mx_attribute(tmp_path, harvester):
    out = postprocess(
        f"<p>{mark(A)}Prose.</p>",
        blocks=(FakeBlock(A),),
        manuscript_dir=tmp_path,
        output_dir=tmp_path / "out",
        labels={},
    )
    assert f'data-mx="{A}"' in out["html"]
    assert "⟦MX" not in out["html"]
    assert out["unanchored"] == []


def test_a_block_whose_marker_never_arrived_is_reported(tmp_path, harvester):
    """The failure mode that matters: a paragraph the margin cannot address."""
    out = postprocess(
        f"<p>{mark(A)}Prose.</p>",
        blocks=(FakeBlock(A), FakeBlock(B)),
        manuscript_dir=tmp_path,
        output_dir=tmp_path / "out",
        labels={},
    )
    assert out["unanchored"] == [B]


def test_unanchored_blocks_come_back_in_document_order(tmp_path, harvester):
    out = postprocess(
        f"<p>{mark(B)}Prose.</p>",
        blocks=(FakeBlock(A), FakeBlock(B), FakeBlock(C)),
        manuscript_dir=tmp_path,
        output_dir=tmp_path / "out",
        labels={},
    )
    assert out["unanchored"] == [A, C]


def test_block_ids_may_be_passed_as_bare_strings(tmp_path, harvester):
    out = postprocess(
        f"<p>{mark(A)}Prose.</p>",
        blocks=(A, B),
        manuscript_dir=tmp_path,
        output_dir=tmp_path / "out",
        labels={},
    )
    assert out["unanchored"] == [B]


# ---------------------------------------------------------------- references


def test_references_are_resolved(tmp_path, harvester):
    html = f'<p>{mark(A)}See Table <span class="math inline">\\(\\ref{{tab:x}}\\)</span>.</p>'
    out = postprocess(
        html,
        blocks=(FakeBlock(A),),
        manuscript_dir=tmp_path,
        output_dir=tmp_path / "out",
        labels={"tab:x": "2"},
    )
    assert "See Table 2." in out["html"]
    assert out["unresolved_refs"] == []


def test_an_unresolved_reference_is_reported(tmp_path, harvester):
    html = f"<p>{mark(A)}See \\ref{{tab:ghost}}.</p>"
    out = postprocess(
        html,
        blocks=(FakeBlock(A),),
        manuscript_dir=tmp_path,
        output_dir=tmp_path / "out",
        labels={},
    )
    assert out["unresolved_refs"] == ["tab:ghost"]
    assert "tab:ghost" in out["html"]


# ----------------------------------------------------------------- citations


CITE = '<span class="citation" data-cites="smith2020">(Smith 2020)</span>'
CITE2 = '<span class="citation" data-cites="jones2019">(Jones 2019)</span>'


# A real five-key stack out of dsp-bias, verbatim from the served page. Note
# `li2025systematic`, whose rendered year is 2026: cite keys lie about years, so
# the year cannot be used to pair a key with the name it rendered as.
REAL_STACK = (
    '<span class="citation" data-cites="persad2016 li2025systematic grevisse2024 '
    'holderried2024 daniels2026vignette">(Persad, Stroulia, and Forgie 2016; '
    'D. Li and Lutfi 2026; Grévisse 2024; Holderried et al. 2024; '
    'Daniels et al. 2026)</span>'
)


def cites_of(html: str) -> list[str]:
    return re.findall(r'data-cites="([^"]*)"', html)


def test_a_citation_stack_becomes_one_span_per_key(tmp_path, harvester):
    """A five-key stack could only ever carry one evidence colour.

    The page underlines a span, and pandoc emits `\\citep{a,b,c}` as ONE span
    with three keys, so the first key's status coloured the whole parenthetical
    and the other four were invisible whether they were supported or not.
    """
    out = postprocess(
        f"<p>{mark(A)}Body {REAL_STACK}.</p>",
        blocks=(FakeBlock(A),),
        manuscript_dir=tmp_path,
        output_dir=tmp_path / "out",
        labels={},
    )
    html = out["html"]
    assert cites_of(html) == [
        "persad2016", "li2025systematic", "grevisse2024",
        "holderried2024", "daniels2026vignette",
    ], "each key must get its own span, in source order"

    # Each span carries only its own rendered text, and the punctuation that
    # joins them stays outside, or the underline would swallow it.
    inner = re.findall(r'<span class="citation"[^>]*>(.*?)</span>', html)
    assert inner[0] == "Persad, Stroulia, and Forgie 2016"
    assert inner[1] == "D. Li and Lutfi 2026"
    assert inner[4] == "Daniels et al. 2026"
    assert "(" not in "".join(inner) and ")" not in "".join(inner)
    assert "(<span" in html and "</span>)" in html
    assert "</span>; <span" in html

    # And every one of them is separately addressable.
    ids = re.findall(r'data-cite-id="([^"]+)"', html)
    assert len(ids) == 5 and len(set(ids)) == 5


def test_a_stack_whose_names_contradict_the_key_order_is_left_alone(tmp_path, harvester):
    """A CSL that sorts a group would pair every key with the wrong name.

    Splitting positionally is only safe while the rendered order matches the
    key order. When a key's own surname turns up in somebody else's chunk, the
    group is left whole: one colour on five citations is wrong, and the wrong
    name against a key is worse.
    """
    sorted_group = (
        '<span class="citation" data-cites="smith2020 jones2019">'
        "(Jones 2019; Smith 2020)</span>"
    )
    out = postprocess(
        f"<p>{mark(A)}Body {sorted_group}.</p>",
        blocks=(FakeBlock(A),),
        manuscript_dir=tmp_path,
        output_dir=tmp_path / "out",
        labels={},
    )
    assert cites_of(out["html"]) == ["smith2020 jones2019"], (
        "a contradicted pairing must not be split"
    )


def test_a_stack_with_an_unrecognisable_key_still_splits(tmp_path, harvester):
    """`who2019` renders as "World Health Organization 2019", so its surname is
    unverifiable rather than contradicted. Absence of evidence is not a
    contradiction, and refusing to split there would give up on institutional
    authors."""
    group = (
        '<span class="citation" data-cites="who2019 smith2020">'
        "(World Health Organization 2019; Smith 2020)</span>"
    )
    out = postprocess(
        f"<p>{mark(A)}Body {group}.</p>",
        blocks=(FakeBlock(A),),
        manuscript_dir=tmp_path,
        output_dir=tmp_path / "out",
        labels={},
    )
    assert cites_of(out["html"]) == ["who2019", "smith2020"]


def test_a_stack_that_does_not_divide_evenly_is_left_alone(tmp_path, harvester):
    """Three keys, two rendered names: something about this citation is not what
    the splitter assumes, so it does not touch it."""
    group = (
        '<span class="citation" data-cites="a2001 b2002 c2003">'
        "(Aye 2001; Bee 2002)</span>"
    )
    out = postprocess(
        f"<p>{mark(A)}Body {group}.</p>",
        blocks=(FakeBlock(A),),
        manuscript_dir=tmp_path,
        output_dir=tmp_path / "out",
        labels={},
    )
    assert cites_of(out["html"]) == ["a2001 b2002 c2003"]


def test_a_single_key_citation_is_untouched(tmp_path, harvester):
    out = postprocess(
        f"<p>{mark(A)}Body {CITE}.</p>",
        blocks=(FakeBlock(A),),
        manuscript_dir=tmp_path,
        output_dir=tmp_path / "out",
        labels={},
    )
    assert "(Smith 2020)" in out["html"], "a lone citation keeps its parentheses"
    assert cites_of(out["html"]) == ["smith2020"]


def test_every_citation_span_gets_a_cite_id(tmp_path, harvester):
    out = postprocess(
        f"<p>{mark(A)}One {CITE} and two {CITE2}.</p>",
        blocks=(FakeBlock(A),),
        manuscript_dir=tmp_path,
        output_dir=tmp_path / "out",
        labels={},
    )
    ids = re.findall(r'data-cite-id="([^"]+)"', out["html"])
    assert len(ids) == 2
    assert len(set(ids)) == 2


def test_changing_which_source_is_cited_changes_the_id(tmp_path, harvester):
    """Evidence is gathered per citation. If swapping the key kept the id, the
    quotes verified against one source would silently stand in for another."""
    kwargs = dict(manuscript_dir=tmp_path, output_dir=tmp_path / "out", labels={})
    smith = postprocess(f"<p>{mark(A)}Body {CITE}.</p>", blocks=(FakeBlock(A),), **kwargs)
    jones = postprocess(f"<p>{mark(A)}Body {CITE2}.</p>", blocks=(FakeBlock(A),), **kwargs)
    assert re.findall(r'data-cite-id="([^"]+)"', smith["html"]) != re.findall(
        r'data-cite-id="([^"]+)"', jones["html"]
    )


def test_cite_ids_are_stable_across_renders(tmp_path, harvester):
    html = f"<p>{mark(A)}One {CITE}.</p>"
    kwargs = dict(
        blocks=(FakeBlock(A),),
        manuscript_dir=tmp_path,
        output_dir=tmp_path / "out",
        labels={},
    )
    first = re.findall(r'data-cite-id="([^"]+)"', postprocess(html, **kwargs)["html"])
    second = re.findall(r'data-cite-id="([^"]+)"', postprocess(html, **kwargs)["html"])
    assert first == second


def test_a_citation_added_above_does_not_renumber_the_ones_below(tmp_path, harvester):
    """The failure a positional id would cause. Insert a citation into the
    first paragraph and every id below it shifts by one, taking every evidence
    record keyed to them along. Content-derived ids do not move."""
    kwargs = dict(
        manuscript_dir=tmp_path,
        output_dir=tmp_path / "out",
        labels={},
    )
    before = postprocess(
        f"<p>{mark(A)}Intro.</p><p>{mark(B)}Body {CITE}.</p>",
        blocks=(FakeBlock(A), FakeBlock(B)),
        **kwargs,
    )
    after = postprocess(
        f"<p>{mark(A)}Intro, now with {CITE2}.</p><p>{mark(B)}Body {CITE}.</p>",
        blocks=(FakeBlock(A), FakeBlock(B)),
        **kwargs,
    )
    id_before = re.findall(r'data-cite-id="([^"]+)"', before["html"])
    id_after = re.findall(r'data-cite-id="([^"]+)"', after["html"])
    assert len(id_before) == 1 and len(id_after) == 2
    # The citation in block B is the same citation; it keeps the same id.
    assert id_before[0] == id_after[1]


def test_the_same_citation_twice_in_one_block_gets_two_ids(tmp_path, harvester):
    out = postprocess(
        f"<p>{mark(A)}First {CITE} then again {CITE}.</p>",
        blocks=(FakeBlock(A),),
        manuscript_dir=tmp_path,
        output_dir=tmp_path / "out",
        labels={},
    )
    ids = re.findall(r'data-cite-id="([^"]+)"', out["html"])
    assert len(ids) == 2 and ids[0] != ids[1]


def test_a_citation_is_identified_by_its_block_not_by_what_else_cites_it(tmp_path, harvester):
    """The same key cited in two paragraphs is two citations, and neither one's
    identity may depend on the other existing. If it did, citing a source a
    second time somewhere else in the paper would silently detach the evidence
    already gathered for the first."""
    kwargs = dict(
        manuscript_dir=tmp_path,
        output_dir=tmp_path / "out",
        labels={},
    )
    together = postprocess(
        f"<p>{mark(A)}Here {CITE}.</p><p>{mark(B)}There {CITE}.</p>",
        blocks=(FakeBlock(A), FakeBlock(B)),
        **kwargs,
    )
    alone = postprocess(
        f"<p>{mark(B)}There {CITE}.</p>",
        blocks=(FakeBlock(B),),
        **kwargs,
    )
    both = re.findall(r'data-cite-id="([^"]+)"', together["html"])
    only = re.findall(r'data-cite-id="([^"]+)"', alone["html"])
    assert len(set(both)) == 2
    assert both[1] == only[0]


def test_the_existing_citation_markup_is_not_disturbed(tmp_path, harvester):
    out = postprocess(
        f"<p>{mark(A)}One {CITE}.</p>",
        blocks=(FakeBlock(A),),
        manuscript_dir=tmp_path,
        output_dir=tmp_path / "out",
        labels={},
    )
    assert 'data-cites="smith2020"' in out["html"]
    assert "(Smith 2020)</span>" in out["html"]


# -------------------------------------------------------------------- assets


def test_a_referenced_image_is_copied_mirroring_its_relative_path(tmp_path, harvester):
    manuscript = tmp_path / "latex"
    (manuscript / "figures").mkdir(parents=True)
    (manuscript / "figures" / "fig1.png").write_bytes(b"\x89PNG fake")
    out_dir = tmp_path / "out"
    out = postprocess(
        f'<p>{mark(A)}<img src="figures/fig1.png" /></p>',
        blocks=(FakeBlock(A),),
        manuscript_dir=manuscript,
        output_dir=out_dir,
        labels={},
    )
    assert (out_dir / "figures" / "fig1.png").read_bytes() == b"\x89PNG fake"
    assert out["assets"] == ["figures/fig1.png"]


def test_remote_and_absolute_sources_are_left_alone(tmp_path, harvester):
    out = postprocess(
        f'<p>{mark(A)}<img src="https://example.com/a.png" /><img src="/tmp/b.png" />'
        f'<img src="data:image/png;base64,AAAA" /></p>',
        blocks=(FakeBlock(A),),
        manuscript_dir=tmp_path,
        output_dir=tmp_path / "out",
        labels={},
    )
    assert out["assets"] == []


def test_a_missing_image_is_not_reported_as_copied(tmp_path, harvester):
    out = postprocess(
        f'<p>{mark(A)}<img src="figures/absent.png" /></p>',
        blocks=(FakeBlock(A),),
        manuscript_dir=tmp_path,
        output_dir=tmp_path / "out",
        labels={},
    )
    assert out["assets"] == []


def test_an_image_referenced_twice_is_copied_once(tmp_path, harvester):
    (tmp_path / "figures").mkdir()
    (tmp_path / "figures" / "f.png").write_bytes(b"x")
    out = postprocess(
        f'<p>{mark(A)}<img src="figures/f.png" /><img src="figures/f.png" /></p>',
        blocks=(FakeBlock(A),),
        manuscript_dir=tmp_path,
        output_dir=tmp_path / "out",
        labels={},
    )
    assert out["assets"] == ["figures/f.png"]


def test_a_percent_encoded_source_resolves_to_the_real_file(tmp_path, harvester):
    """Pandoc percent-encodes spaces. Copying the literal src would miss the
    file and the page would render a broken image."""
    (tmp_path / "figures").mkdir()
    (tmp_path / "figures" / "my fig.png").write_bytes(b"y")
    out_dir = tmp_path / "out"
    out = postprocess(
        f'<p>{mark(A)}<img src="figures/my%20fig.png" /></p>',
        blocks=(FakeBlock(A),),
        manuscript_dir=tmp_path,
        output_dir=out_dir,
        labels={},
    )
    assert (out_dir / "figures" / "my fig.png").read_bytes() == b"y"
    assert out["assets"] == ["figures/my fig.png"]


def test_an_asset_may_not_escape_the_output_directory(tmp_path, harvester):
    """A `../` in an image path would otherwise let a manuscript write anywhere
    the server can reach."""
    (tmp_path / "secret.txt").write_bytes(b"s")
    manuscript = tmp_path / "latex"
    manuscript.mkdir()
    out_dir = tmp_path / "out" / "sub"
    out = postprocess(
        f'<p>{mark(A)}<img src="../secret.txt" /></p>',
        blocks=(FakeBlock(A),),
        manuscript_dir=manuscript,
        output_dir=out_dir,
        labels={},
    )
    assert out["assets"] == []
    assert not (tmp_path / "out" / "secret.txt").exists()


# --------------------------------------------------------------- integration


def test_the_real_harvester_anchors_the_blocks_postprocess_was_given(tmp_path):
    """No double here. If anchors.harvest is still a stub this fails, which is
    the correct signal: postprocess cannot make a document addressable alone."""
    out = postprocess(
        f"<p>{mark(A)}First paragraph.</p><p>{mark(B)}Second paragraph.</p>",
        blocks=(FakeBlock(A), FakeBlock(B)),
        manuscript_dir=tmp_path,
        output_dir=tmp_path / "out",
        labels={},
    )
    assert out["unanchored"] == []
    assert "⟦MX" not in out["html"]


ESTONIA = Path("/Users/bbdaniels/Projects/estonia-ecm/latex/main.tex")


@pytest.mark.skipif(not ESTONIA.exists(), reason="estonia-ecm checkout not present")
def test_the_whole_render_track_against_the_reference_manuscript(tmp_path):
    """flatten to segment to inject to pandoc to postprocess, on the real thing.

    Only this track's obligations are asserted. How many blocks the segmenter
    finds and how many markers survive belong to M1; that no marker is left
    lying in the output, that every reference resolves, and that every figure
    is copied belong here."""
    from manuscriptor.render.pandoc import render_document
    from manuscriptor.render.refs import load_labels
    from manuscriptor.source.anchors import inject
    from manuscriptor.source.blocks import segment
    from manuscriptor.source.flatten import flatten

    flat = flatten(ESTONIA)
    blocks = segment(flat)
    html = render_document(
        inject(flat.text, blocks),
        cwd=ESTONIA.parent,
        bib=ESTONIA.parent / "references.bib",
    )
    out = postprocess(
        html,
        blocks=blocks,
        manuscript_dir=ESTONIA.parent,
        output_dir=tmp_path / "out",
        labels=load_labels(ESTONIA.parent / "main.aux"),
    )

    assert "⟦MX" not in out["html"]
    assert out["unresolved_refs"] == []
    assert "\\ref{" not in out["html"]
    assert len(out["assets"]) >= 15
    assert all((tmp_path / "out" / a).is_file() for a in out["assets"])
    assert out["html"].count("data-cite-id") >= 70

    # The unanchored report has to be exactly the blocks with no data-mx, or it
    # is telling the margin something untrue about what it can address.
    present = set(re.findall(r'data-mx="([^"]+)"', out["html"]))
    assert set(out["unanchored"]) == {b.id for b in blocks} - present


# ---------------------------------------------------------------- front matter
#
# The render pass rewrites \maketitle and the abstract into constructs pandoc
# keeps, and tags them with tokens. Postprocess turns each token into a class
# on its element, so the stylesheet can set the title apart from a section
# heading, and no token may ever reach the reader.


def test_frontmatter_tokens_become_classes(tmp_path, harvester):
    html = (
        f'<h1 class="unnumbered">⟦MXTITLE⟧A Title</h1>'
        f"<p>⟦MXBYLINE⟧A. Author, B. Coauthor</p>"
        f"<h2>⟦MXABSTRACT⟧Abstract</h2>"
        f"<p>{mark(A)}The abstract text.</p>"
    )
    out = postprocess(
        html, blocks=(FakeBlock(A),), manuscript_dir=tmp_path,
        output_dir=tmp_path / "out", labels={},
    )
    page = out["html"]
    assert "⟦MXTITLE⟧" not in page
    assert "⟦MXBYLINE⟧" not in page
    assert "⟦MXABSTRACT⟧" not in page
    assert re.search(r'<h1 class="doc-title unnumbered">\s*A Title</h1>', page)
    assert re.search(r'<p class="doc-byline">\s*A. Author', page)
    assert re.search(r'<h2 class="doc-abstract-label">\s*Abstract</h2>', page)


def test_a_stray_token_is_stripped_not_shipped(tmp_path, harvester):
    # A token that lands somewhere unexpected must still never reach the page.
    out = postprocess(
        f"<p>{mark(A)}Prose with a stray ⟦MXTITLE⟧ token.</p>",
        blocks=(FakeBlock(A),), manuscript_dir=tmp_path,
        output_dir=tmp_path / "out", labels={},
    )
    assert "⟦MXTITLE⟧" not in out["html"]
    assert "Prose with a stray" in out["html"]


def test_a_chain_of_empty_anchors_does_not_strand_the_hoist(tmp_path, harvester):
    # estonia-ecm renders `\newpage` then `\section{Introduction}` as two
    # consecutive empty anchors before the <h1>. The first match consumed the
    # second anchor's tag as its "following element", so the heading never
    # received an id and the Introduction was unclickable on the live page
    # (2026-07-22). The nearest preceding anchor must win the element.
    html = (
        f"<p>{mark(A)}Prose.</p>"
        f'<p data-mx="{B}"></p><p data-mx="{C}"></p><h1 id="x">Heading</h1>'
    )
    out = postprocess(
        html, blocks=(FakeBlock(A), FakeBlock(B), FakeBlock(C)),
        manuscript_dir=tmp_path, output_dir=tmp_path / "out", labels={},
    )
    page = out["html"]
    assert f'<h1 id="x" data-mx="{C}">' in page, page
    assert f'<p data-mx="{B}"></p>' in page  # the farther anchor stays put
    assert out["unanchored"] == []


def test_two_markers_in_one_empty_paragraph_do_not_fight_over_the_heading(
    tmp_path,
):
    # The same shape as the chain above, except the two markers COALESCE into
    # one paragraph, because `\newpage` and the `\subsection*` that follows it
    # on the very next line are one LaTeX paragraph and `\newpage` renders
    # nothing between them. covet-india writes every one of its six exhibits
    # that way, so pandoc emits `<p>⟦MXnewpage⟧\n⟦MXheading⟧</p><h2>`.
    #
    # `harvest` hands one element to one block, so the FIRST marker took the
    # paragraph and the heading was reported unanchored; then the hoist moved
    # the PAGE BREAK's id onto the <h2>. The author clicked "Fig. 2." and the
    # inspector showed him `\newpage` under the name of Fig. 1 (2026-08-03).
    # The nearest preceding anchor must still win the element, and no block may
    # lose its anchor to a neighbour that shares its paragraph.
    #
    # No harvester double here: the double attaches every marker to the nearest
    # preceding open tag and would write two data-mx into one <p>, which the
    # real harvest refuses to do. The defect lives in the seam between the real
    # harvest and the hoist, so both must be real.
    html = (
        f"<p>{mark(A)}Prose.</p>"
        f'<p>{mark(B)}\n{mark(C)}</p><h1 id="x">Heading</h1>'
    )
    out = postprocess(
        html, blocks=(FakeBlock(A), FakeBlock(B), FakeBlock(C)),
        manuscript_dir=tmp_path, output_dir=tmp_path / "out", labels={},
    )
    page = out["html"]
    assert f'<h1 id="x" data-mx="{C}">' in page, page
    assert f'<p data-mx="{B}"></p>' in page, page
    assert out["unanchored"] == []


def test_a_coalesced_marker_run_keeps_document_order(tmp_path):
    # Splitting the run must not reorder it: the page break precedes the
    # heading in the source and must precede it in the DOM, or the margin
    # renders its pins in an order the manuscript does not have.
    out = postprocess(
        f'<p>{mark(A)}{mark(B)}{mark(C)}</p><h1 id="x">Heading</h1>',
        blocks=(FakeBlock(A), FakeBlock(B), FakeBlock(C)),
        manuscript_dir=tmp_path, output_dir=tmp_path / "out", labels={},
    )
    page = out["html"]
    assert out["unanchored"] == []
    order = re.findall(r'data-mx="([^"]+)"', page)
    assert order == [A, B, C], order


# ---------------------------------------------------------------- PDF figures
#
# Pandoc emits <embed> for a PDF figure, the asset copier only knew <img>,
# and a browser paints nothing for an unsized PDF embed: dsp-bias served
# with no figures at all. The postprocess rasterizes PDF figures to PNG in
# the build directory (the docx recipe, 200dpi) and rewrites the element.

MINI_PDF = (b"%PDF-1.4\n"
            b"1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n"
            b"2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj\n"
            b"3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 24 24] >> endobj\n"
            b"trailer << /Root 1 0 R >>\n%%EOF")


def test_a_pdf_figure_becomes_a_png_img(tmp_path, harvester):
    (tmp_path / "outputs").mkdir()
    (tmp_path / "outputs" / "fig2.pdf").write_bytes(MINI_PDF)
    html = f'<figure>{mark(A)}<embed src="outputs/fig2.pdf" /><figcaption>F</figcaption></figure>'
    out = postprocess(html, blocks=(FakeBlock(A),), manuscript_dir=tmp_path,
                      output_dir=tmp_path / "out", labels={})
    assert '<embed' not in out["html"]
    # The PDF's own suffix is kept, so the raster can never land on the cache
    # path the asset copier mirrors a same-stem `fig2.png` onto.
    assert '<img src="outputs/fig2.pdf.png"' in out["html"]
    assert (tmp_path / "out" / "outputs" / "fig2.pdf.png").exists()
    assert "outputs/fig2.pdf.png" in out["assets"]


def test_a_pdf_figure_is_rasterized_once_and_cached(tmp_path, harvester):
    (tmp_path / "outputs").mkdir()
    (tmp_path / "outputs" / "fig2.pdf").write_bytes(MINI_PDF)
    html = f'<p>{mark(A)}x</p><embed src="outputs/fig2.pdf" />'
    postprocess(html, blocks=(FakeBlock(A),), manuscript_dir=tmp_path,
                output_dir=tmp_path / "out", labels={})
    png = tmp_path / "out" / "outputs" / "fig2.pdf.png"
    first = png.stat().st_mtime_ns
    postprocess(html, blocks=(FakeBlock(A),), manuscript_dir=tmp_path,
                output_dir=tmp_path / "out", labels={})
    assert png.stat().st_mtime_ns == first, "an unchanged figure re-rasterized"


# A PNG sitting beside the PDF under the same stem is a DIFFERENT file, and the
# LaTeX named the PDF. Rasterizing to `<stem>.png` put both through one cache
# path, and the asset copier -- which runs after -- copied the manuscript's PNG
# over the raster, with `copy2` carrying its mtime along. Two ways that is worse
# than no cache at all. The page shows a file the manuscript never asked for; and
# the raster's staleness key becomes the OTHER file's mtime, so a PNG newer than
# the PDF pins the check false forever and the figure can never refresh again.
# covet-india ships both forms of all nine of its exhibits, so this was live
# there on every build.


def test_a_png_of_the_same_stem_does_not_replace_the_pdfs_raster(tmp_path, harvester):
    (tmp_path / "outputs").mkdir()
    (tmp_path / "outputs" / "fig2.pdf").write_bytes(MINI_PDF)
    decoy = b"\x89PNG\r\n\x1a\n not the figure the latex named"
    (tmp_path / "outputs" / "fig2.png").write_bytes(decoy)
    html = f'<figure>{mark(A)}<embed src="outputs/fig2.pdf" /><figcaption>F</figcaption></figure>'
    out = postprocess(html, blocks=(FakeBlock(A),), manuscript_dir=tmp_path,
                      output_dir=tmp_path / "out", labels={})
    src = re.search(r'<img src="([^"]+)"', out["html"]).group(1)
    served = tmp_path / "out" / src
    assert served.read_bytes() != decoy, "the page serves the PNG, not the PDF the LaTeX named"
    assert served.stat().st_size > len(decoy)


def test_a_changed_pdf_is_re_rasterized_even_when_its_mtime_did_not_advance(tmp_path, harvester):
    """Content, not mtime. The author regenerates figures constantly, and a
    rebuilt PDF can land with an mtime that does not move forward -- a restore
    from git, a copy that preserves times, or an mtime the asset copier
    overwrote with another file's. A cache keyed on mtime alone then serves the
    old picture with nothing on the page to say so."""
    (tmp_path / "outputs").mkdir()
    pdf = tmp_path / "outputs" / "fig2.pdf"
    pdf.write_bytes(MINI_PDF)
    html = f'<p>{mark(A)}x</p><embed src="outputs/fig2.pdf" />'
    out = postprocess(html, blocks=(FakeBlock(A),), manuscript_dir=tmp_path,
                      output_dir=tmp_path / "out", labels={})
    src = re.search(r'<img src="([^"]+)"', out["html"]).group(1)
    served = tmp_path / "out" / src
    before = served.read_bytes()
    stamp = pdf.stat()

    pdf.write_bytes(MINI_PDF.replace(b"[0 0 24 24]", b"[0 0 96 48]"))
    os.utime(pdf, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    postprocess(html, blocks=(FakeBlock(A),), manuscript_dir=tmp_path,
                output_dir=tmp_path / "out", labels={})
    assert served.read_bytes() != before, "a changed figure kept its old raster"


def test_a_pdf_outside_the_manuscript_is_left_alone(tmp_path, harvester):
    (tmp_path / "ms").mkdir()
    (tmp_path / "secret.pdf").write_bytes(MINI_PDF)
    html = f'<p>{mark(A)}x</p><embed src="../secret.pdf" />'
    out = postprocess(html, blocks=(FakeBlock(A),), manuscript_dir=tmp_path / "ms",
                      output_dir=tmp_path / "ms" / "out", labels={})
    assert '<embed src="../secret.pdf" />' in out["html"]


# The two rules the raster cache lives by, asserted on `ensure_raster` itself
# because the assets route now calls it on the request path -- a rasterization
# that used to happen only inside a build now happens while a browser is reading
# the same file, and both of these become live rather than theoretical.


def test_a_failed_rasterization_does_not_stamp_the_raster_it_did_not_make(tmp_path):
    """The sidecar says which bytes the cached picture was made FROM. Writing it
    after a run that failed says the old picture came from the new PDF, which is
    the one state this cache must never reach: it reports fresh forever, and no
    later build or request can ever tell the difference."""
    pdf = tmp_path / "fig.pdf"
    pdf.write_bytes(MINI_PDF)
    dest = tmp_path / "out" / "fig.pdf.png"
    assert pp_mod.ensure_raster(pdf, dest)
    first = dest.read_bytes()
    stamped = dest.with_name(dest.name + ".sha").read_text()

    pdf.write_bytes(MINI_PDF.replace(b"[0 0 24 24]", b"[0 0 96 48]"))
    failed = subprocess.CompletedProcess([], returncode=1, stdout=b"", stderr=b"boom")
    with mock.patch.object(pp_mod.subprocess, "run", return_value=failed):
        assert pp_mod.ensure_raster(pdf, dest) is True, "the old picture still serves"
    assert dest.read_bytes() == first
    assert dest.with_name(dest.name + ".sha").read_text() == stamped, (
        "a failed run claimed the old raster was made from the new PDF")

    # And the next attempt still sees work to do, which is the whole point.
    assert pp_mod.ensure_raster(pdf, dest) is True
    assert dest.read_bytes() != first


def test_the_raster_is_swapped_in_rather_than_written_where_it_is_served_from(tmp_path):
    """`pdftoppm` writes progressively and the assets route reads the same path,
    so an in-place write hands a browser a half-drawn PNG whenever a refresh and
    a request coincide. The rasterizer is never pointed at the served name."""
    pdf = tmp_path / "fig.pdf"
    pdf.write_bytes(MINI_PDF)
    dest = tmp_path / "out" / "fig.pdf.png"
    seen: list[list[str]] = []
    real = pp_mod.subprocess.run

    def spy(cmd, *a, **k):
        seen.append([str(c) for c in cmd])
        return real(cmd, *a, **k)

    with mock.patch.object(pp_mod.subprocess, "run", spy):
        assert pp_mod.ensure_raster(pdf, dest)
    assert seen, "nothing was rasterized"
    assert str(dest)[: -len(".png")] not in seen[0], (
        f"pdftoppm was pointed at the file being served: {seen[0]}")
    assert dest.is_file() and not list(dest.parent.glob("*.new*"))


def test_the_swap_is_a_rename_and_not_a_copy_into_the_served_name(tmp_path):
    """`os.replace` is what makes the swap atomic; a copy into `dest` is exactly
    the progressive write the temp file exists to avoid. Mutating the mover to
    `shutil.copyfile` left the suite green, so the mover itself is asserted."""
    pdf = tmp_path / "fig.pdf"
    pdf.write_bytes(MINI_PDF)
    dest = tmp_path / "out" / "fig.pdf.png"
    moves: list[tuple[str, str]] = []
    real = pp_mod.os.replace

    def spy(src, dst, *a, **k):
        moves.append((str(src), str(dst)))
        return real(src, dst, *a, **k)

    with mock.patch.object(pp_mod.os, "replace", spy):
        assert pp_mod.ensure_raster(pdf, dest)
    assert [d for _, d in moves] == [str(dest)], (
        f"the raster reached the served name by something other than os.replace: {moves}")
    assert moves[0][0] != str(dest)

    src = tmp_path / "pic.png"
    src.write_bytes(b"\x89PNG" + b"a" * 64)
    copy_dest = tmp_path / "out" / "pic.png"
    moves.clear()
    with mock.patch.object(pp_mod.os, "replace", spy):
        assert pp_mod.mirror(src, copy_dest)
    assert [d for _, d in moves] == [str(copy_dest)], (
        f"the mirrored image reached the served name by something else: {moves}")


# Two writers, one destination. At build time there was one lock-serialized
# writer; the assets route now calls both of these from `asyncio.to_thread` on
# the request path, so two browsers asking for the same stale figure at the same
# moment are two threads inside the same function.


def _slow_pdftoppm(marks, prefixes, delay=0.01):
    """Stand in for `pdftoppm`: writes its prefix + `.png` progressively.

    The real one writes the file in chunks over ~90ms, which is the window the
    race lives in. `marks` hands each caller a distinct body so the served file
    can be attributed to one writer or shown to be two.
    """
    import itertools
    import threading
    import time

    counter = itertools.count()
    seen_lock = threading.Lock()

    def run(cmd, *a, **k):
        prefix = Path(str(cmd[-1]))
        with seen_lock:
            mark = marks[next(counter) % len(marks)]
            prefixes.append(str(prefix))
        made = prefix.with_name(prefix.name + ".png")
        made.parent.mkdir(parents=True, exist_ok=True)
        with open(made, "wb") as fh:
            for i in range(10):
                fh.write(f"CHUNK-{mark}-{i}\n".encode())
                fh.flush()
                time.sleep(delay)
        return subprocess.CompletedProcess(cmd, returncode=0, stdout=b"", stderr=b"")

    return run


def test_two_requests_for_one_stale_raster_do_not_write_each_others_bytes(tmp_path):
    """Both callers used to build the temp from the destination's own name, so
    they wrote the SAME file, `os.replace`d the interleaving into the served
    path, stamped it fresh, and each `finally` could unlink the other's output.
    Reproduced against the route as 20 lines in a raster one clean run writes 10
    into."""
    import threading

    pdf = tmp_path / "fig.pdf"
    pdf.write_bytes(MINI_PDF)
    dest = tmp_path / "out" / "fig.pdf.png"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"OLD-RASTER\n")
    dest.with_name(dest.name + ".sha").write_text("stale")

    prefixes: list[str] = []
    fake = _slow_pdftoppm(["A", "B"], prefixes)
    with mock.patch.object(pp_mod.subprocess, "run", fake):
        threads = [threading.Thread(target=pp_mod.ensure_raster, args=(pdf, dest))
                   for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    body = dest.read_bytes().decode()
    lines = [ln for ln in body.splitlines() if ln]
    assert len(lines) == 10, (
        f"the served raster holds {len(lines)} lines of the 10 one writer produces: {body!r}")
    assert len({ln.split('-')[1] for ln in lines}) == 1, (
        f"the served raster is two writers interleaved: {body!r}")
    assert len(set(prefixes)) == len(prefixes), (
        f"both callers rasterized into the same temp file: {prefixes}")
    assert not list(dest.parent.glob("*.new*")), "a temp file survived"
    # And the sidecar describes the bytes actually being served.
    assert dest.with_name(dest.name + ".sha").read_text().strip() == \
        hashlib.sha256(pdf.read_bytes()).hexdigest()


def test_two_requests_for_one_stale_image_do_not_write_each_others_bytes(tmp_path):
    """`mirror` had the identical shape: one deterministic `dest.name + '.new'`,
    two callers, and `copy2` is not atomic."""
    import threading
    import time

    src = tmp_path / "pic.png"
    src.write_bytes(b"\x89PNG" + b"N" * 200)
    dest = tmp_path / "out" / "pic.png"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"\x89PNG" + b"O" * 8)

    temps: list[str] = []
    real_copy = pp_mod.shutil.copy2

    def slow_copy(a, b, *args, **kw):
        temps.append(str(b))
        data = Path(a).read_bytes()
        with open(b, "wb") as fh:
            for i in range(0, len(data), 16):
                fh.write(data[i:i + 16])
                fh.flush()
                time.sleep(0.005)
        return real_copy and b

    with mock.patch.object(pp_mod.shutil, "copy2", slow_copy):
        threads = [threading.Thread(target=pp_mod.mirror, args=(src, dest))
                   for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    assert dest.read_bytes() == src.read_bytes(), (
        f"the served copy is not what the source holds: {dest.read_bytes()[:40]!r}")
    assert len(set(temps)) == len(temps), f"both callers staged into one temp: {temps}"
    assert not list(dest.parent.glob("*.new*")), "a temp file survived"


# ------------------------------------------------------- a table and its notes


TABLE_WITH_NOTES = (
    "<table>"
    "<caption>Case generation: demographic variation. "
    "F-statistics test whether the six demographic factors jointly predict each "
    "outcome; q-values are Benjamini-Hochberg across the sixteen tests.</caption>"
    "<thead><tr><th>Item</th><th>F</th></tr></thead>"
    "<tbody><tr><td>Q23</td><td>5.30</td></tr></tbody>"
    "</table>"
)


def test_a_tables_notes_read_at_the_measure_not_inside_its_scroll(tmp_path, harvester):
    """A caption inside the scroll container is bound to the TABLE's width.

    A regression table is wider than the reading measure and scrolls inside
    itself, which is right. Its caption carries the notes, and while it sat in
    that container the notes were as wide as the table: to read them you scrolled
    sideways, and the last sentence was off the edge of the page. A figure's
    caption has always sat below the image at the column's own width. Reported
    2026-07-26: "table notes aren't properly attached to tables as they are with
    figures".
    """
    out = postprocess(
        f"<p>{mark(A)}Body.</p>{TABLE_WITH_NOTES}",
        blocks=(FakeBlock(A),),
        manuscript_dir=tmp_path,
        output_dir=tmp_path / "out",
        labels={},
    )
    html = out["html"]
    assert "<figcaption" in html, "the notes belong in a caption below, like a figure's"
    assert "Benjamini-Hochberg" in html
    # The notes must NOT be inside the element that scrolls sideways.
    scroll = html[html.index('class="table-scroll"'):]
    scroll = scroll[:scroll.index("</div>")]
    assert "Benjamini-Hochberg" not in scroll, (
        "the notes are still bound to the table's width"
    )
    # And the table itself still scrolls, which was never the problem.
    assert 'class="table-scroll"' in html and "<table" in html


def test_a_table_with_no_caption_is_left_as_it_was(tmp_path, harvester):
    out = postprocess(
        f"<p>{mark(A)}Body.</p><table><tbody><tr><td>x</td></tr></tbody></table>",
        blocks=(FakeBlock(A),),
        manuscript_dir=tmp_path,
        output_dir=tmp_path / "out",
        labels={},
    )
    assert "<figcaption" not in out["html"]
    assert 'class="table-scroll"' in out["html"]
