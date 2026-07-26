"""Unsaved text belongs on disk, not only in a browser.

Twice on 2026-07-26 an author's edit existed nowhere but the WebView's
localStorage: once because the save path went silent, once because the server
died with the draft unsent. Both times recovering it meant copying WebKit's
sqlite and its write-ahead log and decoding UTF-16 by hand. A draft is work, and
work does not live somewhere only a debugger can reach.

The server is the only writer here, which is why this file may be rewritten
whole while `comments.jsonl` may not.
"""
from __future__ import annotations

import json

import pytest

from manuscriptor.server import drafts


def test_a_draft_survives_being_written_and_read_back(tmp_path):
    p = tmp_path / "drafts.json"
    drafts.put(p, doc="main.tex", block="b-1", text="hello \\citep{x}")
    assert drafts.read(p) == {("main.tex", "b-1"): "hello \\citep{x}"}


def test_a_draft_is_scoped_to_its_document(tmp_path):
    """One directory serves several documents, and a paragraph id could exist in
    both. A draft typed on the appendix must never surface on the paper."""
    p = tmp_path / "drafts.json"
    drafts.put(p, doc="main.tex", block="b-1", text="paper")
    drafts.put(p, doc="appendix.tex", block="b-1", text="appendix")
    got = drafts.read(p)
    assert got[("main.tex", "b-1")] == "paper"
    assert got[("appendix.tex", "b-1")] == "appendix"
    assert drafts.for_doc(p, "main.tex") == {"b-1": "paper"}


def test_writing_the_same_block_twice_keeps_the_newer_text(tmp_path):
    p = tmp_path / "drafts.json"
    drafts.put(p, doc="main.tex", block="b-1", text="first")
    drafts.put(p, doc="main.tex", block="b-1", text="second")
    assert drafts.read(p) == {("main.tex", "b-1"): "second"}


def test_a_saved_block_drops_its_draft(tmp_path):
    p = tmp_path / "drafts.json"
    drafts.put(p, doc="main.tex", block="b-1", text="x")
    drafts.drop(p, doc="main.tex", block="b-1")
    assert drafts.read(p) == {}


def test_dropping_what_is_not_there_is_not_an_error(tmp_path):
    p = tmp_path / "drafts.json"
    drafts.drop(p, doc="main.tex", block="b-1")
    assert drafts.read(p) == {}


def test_an_edit_renames_its_block_and_the_draft_follows(tmp_path):
    """Ids are content-derived, so the block a draft belongs to is renamed by
    the save before it. A draft left under the old id is a draft nobody can
    find, which is exactly the failure this file exists to prevent."""
    p = tmp_path / "drafts.json"
    drafts.put(p, doc="main.tex", block="b-old", text="text")
    drafts.rekey(p, {"b-old": "b-new"})
    assert drafts.read(p) == {("main.tex", "b-new"): "text"}


def test_a_rename_to_a_block_that_already_has_a_draft_keeps_the_renamed_one(tmp_path):
    p = tmp_path / "drafts.json"
    drafts.put(p, doc="main.tex", block="b-old", text="renamed")
    drafts.put(p, doc="main.tex", block="b-new", text="stale")
    drafts.rekey(p, {"b-old": "b-new"})
    assert drafts.read(p) == {("main.tex", "b-new"): "renamed"}


def test_an_empty_draft_is_a_deletion(tmp_path):
    """Discarding a draft and storing an empty string are the same intention,
    and keeping the empty one would make the page open on a blank editor."""
    p = tmp_path / "drafts.json"
    drafts.put(p, doc="main.tex", block="b-1", text="x")
    drafts.put(p, doc="main.tex", block="b-1", text="")
    assert drafts.read(p) == {}


def test_a_corrupt_file_reads_as_no_drafts_rather_than_raising(tmp_path):
    """A manuscript must render whether or not this file is intact. It is a
    convenience, and a convenience may not take the paper down with it."""
    p = tmp_path / "drafts.json"
    p.write_text("{not json", encoding="utf-8")
    assert drafts.read(p) == {}
    drafts.put(p, doc="main.tex", block="b-1", text="x")
    assert drafts.read(p) == {("main.tex", "b-1"): "x"}


def test_the_write_is_atomic(tmp_path):
    """The file is rewritten whole, so a crash mid-write must not be able to
    leave a half file where the drafts were."""
    p = tmp_path / "drafts.json"
    drafts.put(p, doc="main.tex", block="b-1", text="x" * 5000)
    assert json.loads(p.read_text(encoding="utf-8"))
    assert not list(tmp_path.glob("*.tmp*")), "the temporary file must be gone"


@pytest.mark.parametrize(
    "text,balanced",
    [
        ("plain prose", True),
        (r"a \citep{key} b", True),
        (r"mid-command \citep{", False),
        (r"too many closes}", False),
        (r"\begin{a}\end{a}", True),
    ],
)
def test_a_draft_that_would_not_parse_is_named_as_such(text, balanced):
    """Applying a draft from the terminal must refuse what the editor refuses.

    A draft is unsaved precisely because its author stopped mid-command, so the
    common case is `\\citep{` with nothing after it. Writing that into the
    manuscript would break the render and the compile both.
    """
    assert (drafts.imbalance(text) == "") is balanced
    if not balanced:
        assert "more" in drafts.imbalance(text), "the refusal must say what is wrong"


def test_the_records_carry_a_timestamp(tmp_path):
    p = tmp_path / "drafts.json"
    drafts.put(p, doc="main.tex", block="b-1", text="x")
    rec = json.loads(p.read_text(encoding="utf-8"))["drafts"][0]
    assert rec["ts"] and rec["doc"] == "main.tex" and rec["block"] == "b-1"
