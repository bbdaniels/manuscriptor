"""M4 — write one block back, touching nothing else.

Reconciliation is a splice, never a diff, so the guarantee that matters is
negative: after writing block 2 the bytes of blocks 1 and 3 are unchanged. The
other guarantee is that a stale splice refuses. Block ids are content derived,
so if the bytes at the target range no longer hash to the block's id then
something else has already rewritten it and writing would silently destroy that.
"""
from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

import pytest

from manuscriptor.source.blocks import segment
from manuscriptor.source.flatten import flatten
from manuscriptor.source.splice import (
    BlockLocked,
    NotEditable,
    StaleBlock,
    lock,
    splice,
    unlock,
)

BODY = (
    "\\documentclass{article}\n"
    "\\begin{document}\n"
    "First paragraph of the manuscript.\n"
    "\n"
    "Second paragraph of the manuscript.\n"
    "\n"
    "Third paragraph of the manuscript.\n"
    "\\end{document}\n"
)


def manuscript(tmp_path: Path, text: str = BODY) -> Path:
    main = tmp_path / "main.tex"
    main.write_text(text, encoding="utf-8")
    return main


def blocks_of(main: Path):
    return segment(flatten(main))


# --------------------------------------------------------------------- write


def test_splice_replaces_only_its_own_range(tmp_path):
    main = manuscript(tmp_path)
    before = main.read_text(encoding="utf-8")
    blocks = blocks_of(main)

    splice(blocks[1], "Second paragraph, rewritten.", root=tmp_path)

    after = main.read_text(encoding="utf-8")
    assert "Second paragraph, rewritten." in after
    assert "Second paragraph of the manuscript." not in after
    head, tail = before.split("Second paragraph of the manuscript.")
    assert after.startswith(head)
    assert after.endswith(tail)


def test_splice_leaves_neighbours_byte_identical(tmp_path):
    main = manuscript(tmp_path)
    blocks = blocks_of(main)
    splice(blocks[1], "A completely different middle paragraph, longer than before.", root=tmp_path)

    new_blocks = blocks_of(main)
    assert new_blocks[0].source_text == blocks[0].source_text
    assert new_blocks[2].source_text == blocks[2].source_text
    assert new_blocks[0].id == blocks[0].id
    assert new_blocks[2].id == blocks[2].id


def test_splice_can_write_a_multi_line_paragraph(tmp_path):
    main = manuscript(tmp_path)
    blocks = blocks_of(main)
    splice(blocks[1], "Line one of two.\nLine two of two.", root=tmp_path)
    text = main.read_text(encoding="utf-8")
    assert "\nLine one of two.\nLine two of two.\n\nThird" in text


def test_splice_leaves_no_temp_file_behind(tmp_path):
    main = manuscript(tmp_path)
    splice(blocks_of(main)[0], "Rewritten opener.", root=tmp_path)
    assert [p.name for p in tmp_path.iterdir()] == ["main.tex"]


def test_splice_preserves_the_rest_of_the_file_exactly(tmp_path):
    main = manuscript(tmp_path)
    splice(blocks_of(main)[2], "Third paragraph, edited.", root=tmp_path)
    text = main.read_text(encoding="utf-8")
    assert text.startswith("\\documentclass{article}\n\\begin{document}\n")
    assert text.endswith("Third paragraph, edited.\n\\end{document}\n")


# ----------------------------------------------------------------- line drift


def test_splice_still_finds_its_block_after_an_edit_above(tmp_path):
    main = manuscript(tmp_path)
    blocks = blocks_of(main)
    third = blocks[2]

    # Someone else rewrites the first paragraph into three lines, shifting
    # every line number below it.
    text = main.read_text(encoding="utf-8").replace(
        "First paragraph of the manuscript.",
        "First paragraph,\nnow spread\nover three lines.",
    )
    main.write_text(text, encoding="utf-8")

    splice(third, "Third paragraph, still addressable.", root=tmp_path)
    out = main.read_text(encoding="utf-8")
    assert "Third paragraph, still addressable." in out
    assert "now spread" in out


# ---------------------------------------------------------------------- stale


def test_stale_splice_raises(tmp_path):
    main = manuscript(tmp_path)
    blocks = blocks_of(main)
    stale = blocks[1]

    main.write_text(
        main.read_text(encoding="utf-8").replace(
            "Second paragraph of the manuscript.", "Someone else already fixed this."
        ),
        encoding="utf-8",
    )
    with pytest.raises(StaleBlock):
        splice(stale, "My version of the second paragraph.", root=tmp_path)
    assert "Someone else already fixed this." in main.read_text(encoding="utf-8")


def test_stale_splice_writes_nothing(tmp_path):
    main = manuscript(tmp_path)
    stale = blocks_of(main)[1]
    main.write_text(BODY.replace("Second paragraph", "Rewritten paragraph"), encoding="utf-8")
    before = main.read_bytes()
    with pytest.raises(StaleBlock):
        splice(stale, "nope", root=tmp_path)
    assert main.read_bytes() == before


def test_splice_refuses_a_block_whose_id_disagrees_with_its_text(tmp_path):
    """The range is found by content, then confirmed by hash. The hash is what
    catches a block whose id and source text were never consistent, which is the
    shape a forged or upstream-corrupted block arrives in."""
    main = manuscript(tmp_path)
    good = blocks_of(main)[1]
    forged = replace(good, id="b-0123456789")
    with pytest.raises(StaleBlock):
        splice(forged, "Should never land.", root=tmp_path)
    assert "Should never land." not in main.read_text(encoding="utf-8")


def test_splice_raises_when_the_file_is_gone(tmp_path):
    main = manuscript(tmp_path)
    block = blocks_of(main)[0]
    main.unlink()
    with pytest.raises(StaleBlock):
        splice(block, "text", root=tmp_path)


# ----------------------------------------------------------------- refusals


def test_generated_block_refuses_the_edit(tmp_path):
    (tmp_path / "tables").mkdir()
    (tmp_path / "tables" / "t1.tex").write_text(
        "\\begin{table}\n\\caption{Balance}\n\\end{table}\n", encoding="utf-8"
    )
    main = manuscript(
        tmp_path,
        "\\documentclass{article}\n\\begin{document}\n"
        "Lead in.\n\n\\input{tables/t1}\n\nTrailing.\n\\end{document}\n",
    )
    # Provenance now comes from producers.apply, not from the path, so the
    # block has to be marked the way the real pipeline marks it before splice
    # can be asked to refuse it.
    from manuscriptor.server import producers
    marked = producers.apply(blocks_of(main), {}, root_file=main)
    generated = next(b for b in marked if not b.editable)
    with pytest.raises(NotEditable):
        splice(generated, "\\begin{table}\\end{table}", root=tmp_path)
    assert "\\caption{Balance}" in (tmp_path / "tables" / "t1.tex").read_text(encoding="utf-8")


def test_splice_refuses_a_file_outside_the_root(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    main = manuscript(outside)
    block = blocks_of(main)[0]
    inside = tmp_path / "inside"
    inside.mkdir()
    with pytest.raises(ValueError):
        splice(block, "text", root=inside)


# ------------------------------------------------------------------- locking


def test_lock_excludes_another_holder():
    lock("b-lock000001", "human")
    try:
        with pytest.raises(BlockLocked):
            lock("b-lock000001", "claude")
    finally:
        unlock("b-lock000001", "human")


def test_lock_is_reentrant_for_its_holder():
    lock("b-lock000002", "human")
    try:
        lock("b-lock000002", "human")
    finally:
        unlock("b-lock000002", "human")


def test_unlock_releases_for_the_next_holder():
    lock("b-lock000003", "human")
    unlock("b-lock000003", "human")
    lock("b-lock000003", "claude")
    unlock("b-lock000003", "claude")


def test_unlock_by_the_wrong_holder_raises():
    lock("b-lock000004", "human")
    try:
        with pytest.raises(BlockLocked):
            unlock("b-lock000004", "claude")
    finally:
        unlock("b-lock000004", "human")


def test_splice_refuses_while_another_holder_has_the_lock(tmp_path):
    main = manuscript(tmp_path)
    block = blocks_of(main)[0]
    lock(block.id, "claude")
    try:
        with pytest.raises(BlockLocked):
            splice(block, "Rewritten opener.", root=tmp_path)
        splice(block, "Rewritten opener.", root=tmp_path, holder="claude")
    finally:
        unlock(block.id, "claude")
    assert "Rewritten opener." in main.read_text(encoding="utf-8")


# --------------------------------------------------------------- atomicity


def test_splice_uses_rename_not_truncation(tmp_path, monkeypatch):
    main = manuscript(tmp_path)
    block = blocks_of(main)[0]
    seen: list[tuple[str, str]] = []
    real_replace = os.replace

    def spy(src, dst, *a, **kw):
        seen.append((str(src), str(dst)))
        return real_replace(src, dst, *a, **kw)

    monkeypatch.setattr(os, "replace", spy)
    splice(block, "Rewritten opener.", root=tmp_path)
    assert seen and seen[0][1] == str(main)
    assert Path(seen[0][0]).parent == main.parent
