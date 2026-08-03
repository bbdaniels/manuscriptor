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


PREAMBLE_ABSTRACT = (
    "\\documentclass{wlscirep}\n"
    "\\title{A Paper}\n"
    "\\begin{abstract}\n"
    "The abstract lives above the document environment.\n"
    "\\end{abstract}\n"
    "\\begin{document}\n"
    "\\maketitle\n"
    "\n"
    "Body text.\n"
    "\\end{document}\n"
)


def test_splice_to_a_preamble_abstract_writes_only_its_own_bytes(tmp_path):
    # covet-india's abstract is a contiguous range in the root .tex, above
    # \begin{document}. Once it has a block id it must be spliceable like any
    # other block, and touch nothing above or below it.
    main = manuscript(tmp_path, PREAMBLE_ABSTRACT)
    before = main.read_text(encoding="utf-8")
    block = next(b for b in blocks_of(main) if "lives above" in b.source_text)

    new = "\\begin{abstract}\nA rewritten abstract.\n\\end{abstract}"
    splice(block, new, root=tmp_path)

    after = main.read_text(encoding="utf-8")
    head, tail = before.split(block.source_text)
    assert after == head + new + tail
    assert after.startswith("\\documentclass{wlscirep}\n\\title{A Paper}\n")
    assert after.endswith("\\begin{document}\n\\maketitle\n\nBody text.\n\\end{document}\n")


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


# ------------------------------------------------------- concurrent splices


def test_two_blocks_in_one_file_can_be_spliced_at_once(tmp_path):
    """The race that nearly cost a parallel drain its edits.

    splice reads the whole file, replaces a byte range, and writes the whole
    file back. Two agents editing two different paragraphs: A reads, B reads,
    A writes, B writes, and A's work is gone because B's copy predates it.

    Serializing the agents would fix it and waste the parallelism. Holding a
    lock across read-locate-write costs microseconds and lets both land: the
    second re-reads after the first wrote and finds its own block by content,
    which is how splice already locates.
    """
    import threading

    main = manuscript(
        tmp_path,
        "\\documentclass{article}\n\\begin{document}\n"
        "First paragraph, long enough to be a real block of prose in this test.\n\n"
        "Second paragraph, equally real, and the one edited concurrently.\n\n"
        "Third paragraph, present so neither edit is at the end of the file.\n"
        "\\end{document}\n",
    )
    blocks = [b for b in blocks_of(main) if b.kind == "paragraph" and b.editable]
    assert len(blocks) >= 2

    start = threading.Barrier(2)
    errors = []

    def edit(block, marker):
        try:
            start.wait(timeout=5)
            splice(block, block.source_text + " " + marker, root=tmp_path)
        except Exception as exc:  # noqa: BLE001 - reported, not swallowed
            errors.append(exc)

    threads = [
        threading.Thread(target=edit, args=(blocks[0], "EDIT-A")),
        threading.Thread(target=edit, args=(blocks[1], "EDIT-B")),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    text = main.read_text(encoding="utf-8")
    assert not errors, errors
    assert "EDIT-A" in text, "the first edit was overwritten"
    assert "EDIT-B" in text, "the second edit was overwritten"


def test_many_concurrent_splices_all_land(tmp_path):
    import threading

    paras = "\n\n".join(f"Paragraph number {i}, long enough to stand as its own block." for i in range(8))
    main = manuscript(tmp_path, "\\documentclass{article}\n\\begin{document}\n" + paras + "\n\\end{document}\n")
    blocks = [b for b in blocks_of(main) if b.kind == "paragraph" and b.editable][:8]

    start = threading.Barrier(len(blocks))

    def edit(block, n):
        start.wait(timeout=5)
        splice(block, block.source_text + f" MARK{n}", root=tmp_path)

    threads = [threading.Thread(target=edit, args=(b, i)) for i, b in enumerate(blocks)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    text = main.read_text(encoding="utf-8")
    landed = [i for i in range(len(blocks)) if f"MARK{i}" in text]
    assert len(landed) == len(blocks), f"only {len(landed)} of {len(blocks)} edits survived"


def test_concurrent_splices_from_separate_processes_all_land(tmp_path):
    """The threading lock is blind across processes, and the server splicing an
    author's edit while a Claude session splices its own is exactly that."""
    import subprocess
    import sys
    import textwrap

    paras = "\n\n".join(f"Paragraph number {i}, long enough to stand as its own block." for i in range(6))
    main = manuscript(tmp_path, "\\documentclass{article}\n\\begin{document}\n" + paras + "\n\\end{document}\n")

    script = textwrap.dedent("""
        import sys, time
        from pathlib import Path
        from manuscriptor.source.blocks import segment
        from manuscriptor.source.flatten import flatten
        from manuscriptor.source.splice import splice
        root, n, at = Path(sys.argv[1]), int(sys.argv[2]), float(sys.argv[3])
        blocks = [b for b in segment(flatten(root / "main.tex")) if b.kind == "paragraph"]
        time.sleep(max(0.0, at - time.time()))
        splice(blocks[n], blocks[n].source_text + f" PROC{n}", root=root)
    """)
    runner = tmp_path / "one.py"
    runner.write_text(script, encoding="utf-8")

    import time
    go = time.time() + 1.2
    procs = [subprocess.Popen([sys.executable, str(runner), str(tmp_path), str(i), str(go)])
             for i in range(6)]
    for p in procs:
        p.wait(timeout=30)

    text = main.read_text(encoding="utf-8")
    landed = [i for i in range(6) if f"PROC{i}" in text]
    assert len(landed) == 6, f"only {len(landed)} of 6 survived: {landed}"


# ---------------------------------------------------- a block that has grown


def test_a_block_that_grew_since_it_was_cut_refuses_instead_of_duplicating(tmp_path):
    """The staleness guard's whole job, and the one shape it never caught.

    A block's id says what its bytes ARE, not where it ends. So when a previous
    save APPENDED to the paragraph, the stale block's source text is still in
    the file -- as a prefix of the paragraph it became -- and hashing the bytes
    found there hashes them back to the stale id, because they are the very
    bytes that produced it. The check could not fail. Splicing over that prefix
    left the appended tail sitting behind the new text, which is how ordinary
    typing wrote `... ALPHA. BETA BETA.` into a manuscript on 2026-07-28.
    """
    main = manuscript(tmp_path)
    stale = blocks_of(main)[1]

    # An earlier save already extended the paragraph on disk.
    main.write_text(
        main.read_text(encoding="utf-8").replace(
            "Second paragraph of the manuscript.",
            "Second paragraph of the manuscript. ALPHA."),
        encoding="utf-8")
    grown = main.read_text(encoding="utf-8")

    with pytest.raises(StaleBlock):
        splice(stale, "Second paragraph of the manuscript. ALPHA", root=tmp_path)
    assert main.read_text(encoding="utf-8") == grown, "a refused splice writes nothing"


def test_a_block_that_gained_a_prefix_since_it_was_cut_also_refuses(tmp_path):
    """The same hole from the other end: text PREPENDED to the paragraph leaves
    the stale source text in the file as a suffix, and the id hashes back just
    as happily."""
    main = manuscript(tmp_path)
    stale = blocks_of(main)[1]

    main.write_text(
        main.read_text(encoding="utf-8").replace(
            "Second paragraph of the manuscript.",
            "Newly typed opening. Second paragraph of the manuscript."),
        encoding="utf-8")
    grown = main.read_text(encoding="utf-8")

    with pytest.raises(StaleBlock):
        splice(stale, "Second paragraph, rewritten.", root=tmp_path)
    assert main.read_text(encoding="utf-8") == grown


def test_a_fresh_block_still_splices_after_its_neighbours_are_rewritten(tmp_path):
    """The guard must not turn into a refusal of the ordinary case. Blocks 0 and
    2 are rewritten, block 1 is not re-cut, and its splice must still land: a
    block's identity survives edits above and below it, which is the entire
    point of deriving it from content."""
    main = manuscript(tmp_path)
    blocks = blocks_of(main)
    target = blocks[1]

    splice(blocks[0], "First paragraph, rewritten at length and then some.", root=tmp_path)
    splice(blocks[2], "Third paragraph, also rewritten, and rather longer.", root=tmp_path)

    splice(target, "Second paragraph, rewritten.", root=tmp_path)
    text = main.read_text(encoding="utf-8")
    assert "Second paragraph, rewritten." in text
    assert "Second paragraph of the manuscript." not in text
