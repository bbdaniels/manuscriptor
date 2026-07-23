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


def test_insert_at_cursor_refuses_an_unplaced_caret():
    """A textarea that has never been focused reports selectionStart 0, so an
    insert with no cursor silently prepended to the start of the paragraph.
    That is how two empty footnotes reached a real manuscript.

    The rule is checked here against the shipped viewer source rather than a
    browser: the guard must exist and must not be a comment.
    """
    from pathlib import Path

    js = Path(__file__).resolve().parent.parent / "manuscriptor/templates/static/viewer.js"
    src = js.read_text(encoding="utf-8")
    fn = src[src.index("function insertAtCursor"):]
    fn = fn[: fn.index("\n  }")]
    assert "caretKnown" in fn, "insertAtCursor must not trust an unplaced caret"
    assert "src.value.length" in fn, "and must fall back to the end, never the start"


# ------------------------------------------------------------- read-only mode


def test_read_only_refuses_every_write(tmp_path):
    """Pointing the editor at a real manuscript should be safe by construction,
    not by remembering. In read-only mode no path may reach the filesystem."""
    import asyncio

    manuscript(tmp_path)
    from manuscriptor.server.app import Session

    s = Session(tmp_path, read_only=True)
    before = (tmp_path / "main.tex").read_text(encoding="utf-8")
    bid = next(b.id for b in s.build.blocks if b.editable)

    reply = asyncio.run(s.on_edit(bid, "REWRITTEN"))
    assert reply["type"] == "held"
    assert "read-only" in reply["reason"].lower()
    assert (tmp_path / "main.tex").read_text(encoding="utf-8") == before

    chat_reply = asyncio.run(s.on_chat(bid, "a note"))
    assert chat_reply["type"] == "held"
    assert not (tmp_path / "comments.jsonl").exists(), "not even the log"


def test_read_write_is_still_the_default(tmp_path):
    import asyncio

    manuscript(tmp_path)
    from manuscriptor.server.app import Session

    s = Session(tmp_path)
    bid = next(b.id for b in s.build.blocks if b.editable)
    src = s.build.by_id[bid].source_text
    reply = asyncio.run(s.on_edit(bid, src + " edited."))
    assert reply["type"] == "saved"
    assert "edited." in (tmp_path / "main.tex").read_text(encoding="utf-8")


# --------------------------------------------------- serving a top-level tree


def _tree_project(tmp_path: Path) -> Path:
    """A repo whose paper lives in latex/, with a response in another folder and
    a fragment that must never be openable -- the shape ClaudeHUD hands over."""
    (tmp_path / "latex").mkdir()
    (tmp_path / "latex" / "main.tex").write_text(DOC, encoding="utf-8")
    (tmp_path / "latex" / "section.tex").write_text(
        "A fragment only ever \\input into the paper.\n", encoding="utf-8")
    (tmp_path / "response").mkdir()
    (tmp_path / "response" / "response.tex").write_text(DOC, encoding="utf-8")
    return tmp_path


def test_serving_a_top_level_dir_without_a_direct_main_does_not_error(tmp_path):
    """The pivot's crux: the served directory holds no .tex of its own, only
    subfolders. Before, choose_main raised; now it opens on the first document."""
    from manuscriptor.server.app import Session

    _tree_project(tmp_path)
    s = Session(tmp_path)
    assert s.doc == "main.tex"
    assert s.current_ref == "latex/main.tex"
    assert s.root == (tmp_path / "latex").resolve()


def test_the_switcher_list_spans_the_whole_tree(tmp_path):
    from manuscriptor.server.app import Session

    _tree_project(tmp_path)
    blob = Session(tmp_path).blob
    # Every document, none of the fragments, named by path from the served root.
    assert blob["docs"] == ["latex/main.tex", "response/response.tex"]
    assert "latex/section.tex" not in blob["docs"]
    assert blob["main"] == "latex/main.tex"


def test_switching_serves_a_document_from_its_own_folder(tmp_path):
    from manuscriptor.server.app import Session

    _tree_project(tmp_path)
    s = Session(tmp_path)
    s.switch("response/response.tex")
    assert s.current_ref == "response/response.tex"
    assert s.doc == "response.tex"
    # The comment log and build output follow the document into its own folder.
    assert s.root == (tmp_path / "response").resolve()
    assert s.log == (tmp_path / "response" / "comments.jsonl")


def test_a_switch_to_a_document_the_tree_does_not_offer_is_refused(tmp_path):
    import pytest

    from manuscriptor.server.app import Session

    _tree_project(tmp_path)
    s = Session(tmp_path)
    with pytest.raises(ValueError):
        s.switch("latex/section.tex")        # a fragment is not a document
    with pytest.raises(ValueError):
        s.switch("../escape.tex")            # nor a path out of the project
    assert s.current_ref == "latex/main.tex", "a refused switch changes nothing"


def test_a_single_directory_manuscript_is_unchanged(tmp_path):
    """The document sitting at the served root still opens exactly as before,
    its switcher naming the sibling documents by bare filename."""
    from manuscriptor.server.app import Session

    manuscript(tmp_path)
    (tmp_path / "appendix.tex").write_text(DOC, encoding="utf-8")
    s = Session(tmp_path)
    assert s.doc == "main.tex"
    assert s.current_ref == "main.tex"
    assert s.root == tmp_path.resolve()
    assert s.blob["docs"] == ["main.tex", "appendix.tex"]
