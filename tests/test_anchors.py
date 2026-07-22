"""M2 — sentinel markers must survive into the right enclosing element.

Verified 2026-07-21 that pandoc carries U+27E6 MX... U+27E7 through a latex to
html5 render intact, including inside footnotes, list items, and table captions.
These tests cover our half of that contract: putting the markers in at the head
of each block, and taking them back out onto `data-mx` afterwards.

A marker that cannot be attached must be reported. An unanchored block is a
paragraph the margin cannot address, and a silent drop would look like the
comment simply never arrived.
"""
from __future__ import annotations

from pathlib import Path

from manuscriptor.source.anchors import MARKER_RE, harvest, inject, marker
from manuscriptor.source.blocks import Block, segment
from manuscriptor.source.flatten import flatten


def mkblock(
    bid: str,
    flat_start: int,
    flat_end: int,
    text: str,
    kind: str = "paragraph",
) -> Block:
    return Block(
        id=bid,
        kind=kind,
        file=Path("main.tex"),
        line_start=1,
        line_end=1,
        flat_start=flat_start,
        flat_end=flat_end,
        source_text=text,
        flat_text=text,
        parent_heading=None,
        editable=True,
        includes=(),
    )


# -------------------------------------------------------------------- marker


def test_marker_shape():
    assert marker("b-3f2a91c0de") == "⟦MX3f2a91c0de⟧"


def test_marker_strips_only_the_prefix():
    assert marker("b-3f2a91c0de-2") == "⟦MX3f2a91c0de-2⟧"


def test_marker_re_matches_a_plain_id():
    m = MARKER_RE.search("before ⟦MX3f2a91c0de⟧ after")
    assert m is not None
    assert m.group(1) == "3f2a91c0de"


def test_marker_re_matches_a_duplicate_suffixed_id():
    m = MARKER_RE.fullmatch(marker("b-3f2a91c0de-12"))
    assert m is not None
    assert m.group(1) == "3f2a91c0de-12"


# -------------------------------------------------------------------- inject


def test_inject_puts_a_marker_at_each_block_head():
    text = "Alpha.\n\nBeta.\n"
    blocks = (
        mkblock("b-1111111111", 0, 6, "Alpha."),
        mkblock("b-2222222222", 8, 13, "Beta."),
    )
    out = inject(text, blocks)
    assert out == (
        marker("b-1111111111") + "Alpha.\n\n" + marker("b-2222222222") + "Beta.\n"
    )


def test_inject_leaves_unmarked_regions_alone():
    text = "keep me\n\nAlpha.\n"
    blocks = (mkblock("b-1111111111", 9, 15, "Alpha."),)
    assert inject(text, blocks) == "keep me\n\n" + marker("b-1111111111") + "Alpha.\n"


def test_inject_handles_blocks_out_of_order():
    text = "Alpha.\n\nBeta.\n"
    blocks = (
        mkblock("b-2222222222", 8, 13, "Beta."),
        mkblock("b-1111111111", 0, 6, "Alpha."),
    )
    out = inject(text, blocks)
    assert out.index(marker("b-1111111111")) < out.index(marker("b-2222222222"))
    assert out.count("Alpha.") == 1


def test_inject_puts_the_list_item_marker_after_the_item_command():
    text = "\\begin{itemize}\n\\item first\n\\end{itemize}\n"
    blocks = (mkblock("b-3333333333", 16, 27, "\\item first", kind="list_item"),)
    out = inject(text, blocks)
    assert "\\item" + marker("b-3333333333") + " first" in out


def test_inject_skips_an_optional_item_label():
    text = "\\item[Label] body\n"
    blocks = (mkblock("b-4444444444", 0, 17, "\\item[Label] body", kind="list_item"),)
    out = inject(text, blocks)
    assert out.startswith("\\item[Label]" + marker("b-4444444444"))


def test_inject_is_offset_exact_on_a_real_manuscript(tmp_path):
    main = tmp_path / "main.tex"
    main.write_text(
        "\\documentclass{article}\n\\begin{document}\n"
        "First paragraph.\n\nSecond paragraph.\n"
        "\\end{document}\n",
        encoding="utf-8",
    )
    flat = flatten(main)
    blocks = segment(flat)
    out = inject(flat.text, blocks)
    for block in blocks:
        assert marker(block.id) + block.flat_text in out


# ------------------------------------------------------------------- harvest


def test_harvest_marker_at_the_head_of_a_paragraph():
    html = f"<p>{marker('b-1111111111')}Alpha.</p>"
    out, orphans = harvest(html)
    assert out == '<p data-mx="b-1111111111">Alpha.</p>'
    assert orphans == []


def test_harvest_preserves_existing_attributes():
    html = f'<p class="x">{marker("b-1111111111")}Alpha.</p>'
    out, _ = harvest(html)
    assert out == '<p class="x" data-mx="b-1111111111">Alpha.</p>'


def test_harvest_marker_alone_in_its_own_paragraph():
    html = f"<p>{marker('b-1111111111')}</p>\n<figure><img src=\"a.png\"/></figure>"
    out, orphans = harvest(html)
    assert out.startswith('<p data-mx="b-1111111111"></p>')
    assert orphans == []


def test_harvest_marker_in_the_middle_of_text():
    html = f"<p>lead in {marker('b-1111111111')}tail</p>"
    out, orphans = harvest(html)
    assert out == '<p data-mx="b-1111111111">lead in tail</p>'
    assert orphans == []


def test_harvest_attaches_to_the_innermost_element():
    html = f"<li><p>{marker('b-1111111111')}item text</p></li>"
    out, _ = harvest(html)
    assert '<p data-mx="b-1111111111">' in out
    assert "<li>" in out


def test_harvest_ignores_void_elements():
    html = f'<div><img src="a.png"><span>{marker("b-1111111111")}x</span></div>'
    out, _ = harvest(html)
    assert '<span data-mx="b-1111111111">' in out


def test_harvest_handles_many_markers():
    html = (
        f"<p>{marker('b-1111111111')}one</p>"
        f"<p>{marker('b-2222222222')}two</p>"
        f"<p>{marker('b-3333333333')}three</p>"
    )
    out, orphans = harvest(html)
    assert orphans == []
    assert out.count("data-mx") == 3
    assert MARKER_RE.search(out) is None


def test_harvest_reports_a_marker_with_no_enclosing_element():
    html = f"{marker('b-9999999999')}<p>text</p>"
    out, orphans = harvest(html)
    assert orphans == ["b-9999999999"]
    assert MARKER_RE.search(out) is None
    assert "data-mx" not in out


def test_harvest_reports_the_second_marker_in_one_element():
    html = f"<p>{marker('b-1111111111')}a {marker('b-2222222222')}b</p>"
    out, orphans = harvest(html)
    assert '<p data-mx="b-1111111111">' in out
    assert orphans == ["b-2222222222"]
    assert MARKER_RE.search(out) is None


def test_harvest_never_leaves_a_marker_visible():
    html = f'<h1 id="mx{"1" * 10}intro">{marker("b-1111111111")}Intro</h1>'
    out, _ = harvest(html)
    assert "⟦" not in out and "⟧" not in out


def test_harvest_leaves_marker_free_html_untouched():
    html = "<p>nothing to see</p>"
    assert harvest(html) == (html, [])


def test_harvest_keeps_duplicate_suffixed_ids_whole():
    html = f"<p>{marker('b-1111111111-2')}dup</p>"
    out, orphans = harvest(html)
    assert out == '<p data-mx="b-1111111111-2">dup</p>'
    assert orphans == []


# ----------------------------------------------------------------- round trip


def test_markers_round_trip_through_a_fake_render(tmp_path):
    main = tmp_path / "main.tex"
    main.write_text(
        "\\documentclass{article}\n\\begin{document}\n"
        "First paragraph.\n\nSecond paragraph.\n"
        "\\end{document}\n",
        encoding="utf-8",
    )
    flat = flatten(main)
    blocks = segment(flat)
    marked = inject(flat.text, blocks)

    # Stand in for pandoc: wrap each body paragraph in <p>, markers untouched.
    body = marked.split("\\begin{document}\n")[1].split("\\end{document}")[0]
    html = "".join(f"<p>{p.strip()}</p>" for p in body.split("\n\n") if p.strip())

    out, orphans = harvest(html)
    assert orphans == []
    for block in blocks:
        assert f'data-mx="{block.id}"' in out
    assert MARKER_RE.search(out) is None
