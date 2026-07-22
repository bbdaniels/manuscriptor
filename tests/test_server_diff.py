"""The patch frame the server pushes after a file changes.

The bug this exists to prevent was found by driving the real thing in a browser,
not by reading the code. Block ids are derived from content, so the moment the
author edits a paragraph its id changes. A diff that compares id sets directly
sees that as a delete plus an insert, the client tears the block out and puts a
stranger in its place, and the draft and chat keyed to the old id are orphaned.
Which is precisely the failure the content-derived id scheme exists to avoid.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from manuscriptor.server import build as build_mod
from manuscriptor.server.app import _diff, block_html

DOC = r"""\documentclass{article}
\begin{document}
First paragraph, entirely unremarkable, and long enough to be a real block of prose.

Second paragraph, which is the one the author is about to edit in the browser.

Third paragraph, which must not be disturbed by anything happening above it.
\end{document}
"""


def manuscript(tmp_path: Path, body: str = DOC) -> Path:
    p = tmp_path / "main.tex"
    p.write_text(body, encoding="utf-8")
    return p


@pytest.fixture
def two_builds(tmp_path):
    """A build, an edit to the middle paragraph, and the build after it."""
    manuscript(tmp_path)
    before = build_mod.build(tmp_path)
    src = (tmp_path / "main.tex").read_text(encoding="utf-8")
    (tmp_path / "main.tex").write_text(
        src.replace("about to edit in the browser.", "about to edit in the browser, now edited."),
        encoding="utf-8",
    )
    after = build_mod.build(tmp_path)
    return before, after


def test_an_edit_is_a_change_not_a_delete_and_insert(two_builds):
    before, after = two_builds
    patch = _diff(before, after)
    assert patch is not None
    assert patch["removed"] == [], "an edited block must never read as removed"
    assert patch["added"] == [], "nor its replacement as added"
    assert len(patch["blocks"]) == 1, "exactly one block changed"


def test_the_rename_reaches_the_client(two_builds):
    """Without this the client cannot carry the draft and chat across."""
    before, after = two_builds
    patch = _diff(before, after)
    assert patch["renamed"], "the id changed, so the mapping must be sent"
    old_id, new_id = next(iter(patch["renamed"].items()))
    assert old_id != new_id
    assert old_id in {b.id for b in before.blocks}
    assert new_id in {b.id for b in after.blocks}
    assert new_id in patch["blocks"], "the renamed block's markup must come with it"


def test_untouched_blocks_are_not_in_the_patch(two_builds):
    before, after = two_builds
    patch = _diff(before, after)
    edited = set(patch["blocks"])
    untouched = [b for b in after.blocks if b.id not in edited and b.kind == "paragraph"]
    assert untouched, "the fixture should leave paragraphs alone"
    for b in untouched:
        assert b.id not in patch["blocks"]


def test_no_change_produces_no_patch(tmp_path):
    manuscript(tmp_path)
    a = build_mod.build(tmp_path)
    b = build_mod.build(tmp_path)
    assert _diff(a, b) is None


def test_a_deleted_block_is_reported_as_removed(tmp_path):
    manuscript(tmp_path)
    before = build_mod.build(tmp_path)
    src = (tmp_path / "main.tex").read_text(encoding="utf-8")
    (tmp_path / "main.tex").write_text(
        src.replace(
            "\nThird paragraph, which must not be disturbed by anything happening above it.\n", ""
        ),
        encoding="utf-8",
    )
    patch = _diff(before, build_mod.build(tmp_path))
    assert patch["removed"], "a genuinely deleted block must still be reported"


def test_block_html_returns_the_whole_element(two_builds):
    _, after = two_builds
    bid = next(b.id for b in after.blocks if b.kind == "paragraph")
    frag = block_html(after.blob["html"], bid)
    assert frag.startswith("<")
    assert frag.rstrip().endswith(">")
    assert frag.count("data-mx=") == 1, "one block's markup, not its neighbours'"


# --------------------------------------------------------- the page itself


def test_the_server_can_actually_render_its_page(tmp_path):
    """The gap that let a tidy break the running product.

    268 tests passed while `serve` returned a 500 on every request, because not
    one of them rendered the page the server actually hands to a browser. The
    template, the blob and the session are each tested; the line where they meet
    was not.
    """
    manuscript(tmp_path)
    from manuscriptor.server.app import Session, _page

    page = _page(Session(tmp_path))
    assert page.lstrip().startswith("<!DOCTYPE html>")
    assert "window.MS" in page, "the data blob must reach the page"
    assert "data-mx=" in page, "and the anchors with it"
    assert page.count("<style>") == 1 and "function" in page, "css and js inlined"


def test_the_session_exposes_its_blob_as_data_not_a_call(tmp_path):
    """`blob` is a property. A caller invoking it got a TypeError at request
    time rather than at import time, which is how this shipped."""
    manuscript(tmp_path)
    from manuscriptor.server.app import Session

    blob = Session(tmp_path).blob
    assert isinstance(blob, dict) and blob["blocks"]
