"""M5 — presenting pending chats to whoever will act on them.

Nothing in the drain calls a model, and the tests hold it to that: the server
has zero knowledge of Claude, so this module's whole job is to say what is
pending, in enough context to answer it, and to record that it was answered.

The case that matters most is re-anchoring. A comment is keyed to a block id and
ids are content-derived, so the moment anyone edits that paragraph the id the
comment was written against no longer exists. Dropping those would break the
promise the whole anchoring design makes.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from manuscriptor.server import build as build_mod
from manuscriptor.server import chat, drain

DOC = r"""\documentclass{article}
\begin{document}
\section{Results}
The treatment raised screening rates substantially across all three cohorts followed.

We interpret this as evidence that the contract itself, rather than the payment, drove it.

A third paragraph, present so the neighbour context has something to pick up.
\end{document}
"""


def setup(tmp_path: Path, body: str = DOC) -> tuple[Path, str]:
    (tmp_path / "main.tex").write_text(body, encoding="utf-8")
    b = build_mod.build(tmp_path)
    bid = [x.id for x in b.blocks if x.kind == "paragraph"][1]
    blk = b.by_id[bid]
    chat.append(
        tmp_path / "comments.jsonl",
        {"id": "c-0001", "kind": "comment", "block": bid, "file": str(blk.file),
         "lines": [blk.line_start, blk.line_end], "quote": blk.source_text[:120],
         "body": "This overclaims. Soften it.", "author": "bb"},
    )
    return tmp_path, bid


def test_a_pending_comment_arrives_with_its_block(tmp_path):
    d, bid = setup(tmp_path)
    items = drain.collect(d)
    assert len(items) == 1
    it = items[0]
    assert it.chat_id == "c-0001" and it.block_id == bid
    assert "interpret this as evidence" in it.source
    assert it.editable is True
    assert it.section == "Results"


def test_context_is_wide_and_the_writable_unit_is_narrow(tmp_path):
    """The safety property, asserted rather than assumed: a worker may see the
    neighbours but is handed exactly one block to change."""
    d, _ = setup(tmp_path)
    it = drain.collect(d)[0]
    assert it.before and it.after, "neighbours must be supplied"
    assert isinstance(it.source, str)
    assert "raised screening rates" in it.before[-1]
    assert "third paragraph" in it.after[0]


def test_a_comment_survives_its_block_being_edited(tmp_path):
    """The id it was written against is gone. It must still find its paragraph."""
    d, bid = setup(tmp_path)
    src = (d / "main.tex").read_text(encoding="utf-8")
    (d / "main.tex").write_text(src.replace("drove it.", "drove it, we think."), encoding="utf-8")

    items = drain.collect(d)
    assert len(items) == 1
    it = items[0]
    assert it.block_id != bid, "the id changed, which is the whole point"
    assert "interpret this as evidence" in it.source
    assert it.note and "re-anchored" in it.note


def test_a_comment_whose_block_vanished_is_reported_not_dropped(tmp_path):
    d, _ = setup(tmp_path)
    src = (d / "main.tex").read_text(encoding="utf-8")
    (d / "main.tex").write_text(
        src.replace(
            "We interpret this as evidence that the contract itself, rather than the payment, drove it.\n",
            "",
        ),
        encoding="utf-8",
    )
    items = drain.collect(d)
    assert len(items) == 1, "never silently discarded"
    assert items[0].source == ""
    assert "gone" in (items[0].note or "")


def test_marking_done_removes_it_from_pending(tmp_path):
    d, _ = setup(tmp_path)
    assert len(drain.collect(d)) == 1
    drain.mark(d, "c-0001", "done")
    assert drain.collect(d) == []


def test_marking_appends_and_never_rewrites(tmp_path):
    d, _ = setup(tmp_path)
    before = (d / "comments.jsonl").read_text(encoding="utf-8")
    drain.mark(d, "c-0001", "working")
    after = (d / "comments.jsonl").read_text(encoding="utf-8")
    assert after.startswith(before), "the log is append only"
    assert len(after.splitlines()) == len(before.splitlines()) + 1


def test_an_unknown_state_is_refused(tmp_path):
    d, _ = setup(tmp_path)
    with pytest.raises(ValueError):
        drain.mark(d, "c-0001", "finished-ish")


def test_a_generated_block_is_flagged_read_only(tmp_path):
    (tmp_path / "tables").mkdir()
    (tmp_path / "tables" / "t1.tex").write_text(
        "\\begin{tabular}{lc}\\toprule A & 1 \\\\ B & 2 \\\\ \\bottomrule\\end{tabular}\n",
        encoding="utf-8",
    )
    (tmp_path / "main.tex").write_text(
        "\\documentclass{article}\n\\begin{document}\nLead in paragraph here.\n\n"
        "\\input{tables/t1}\n\\end{document}\n",
        encoding="utf-8",
    )
    b = build_mod.build(tmp_path)
    gen = next(x for x in b.blocks if not x.editable)
    chat.append(tmp_path / "comments.jsonl",
                {"id": "c-0001", "kind": "comment", "block": gen.id, "file": str(gen.file),
                 "lines": [1, 1], "quote": gen.source_text[:120], "body": "fix this", "author": "bb"})
    it = drain.collect(tmp_path)[0]
    assert it.editable is False
    assert "generated by analysis code" in drain.as_text([it])


def test_wait_returns_when_a_comment_lands(tmp_path):
    (tmp_path / "main.tex").write_text(DOC, encoding="utf-8")
    log = tmp_path / "comments.jsonl"
    log.write_text("", encoding="utf-8")

    def later():
        time.sleep(0.4)
        chat.append(log, {"id": "c-9", "kind": "comment", "block": "b-x", "body": "hi"})

    threading.Thread(target=later, daemon=True).start()
    assert drain.wait(tmp_path, timeout=5) is True


def test_wait_gives_up_when_nothing_arrives(tmp_path):
    (tmp_path / "main.tex").write_text(DOC, encoding="utf-8")
    (tmp_path / "comments.jsonl").write_text("", encoding="utf-8")
    assert drain.wait(tmp_path, timeout=0.6) is False


def test_nothing_pending_says_so(tmp_path):
    (tmp_path / "main.tex").write_text(DOC, encoding="utf-8")
    assert drain.collect(tmp_path) == []
    assert "No pending" in drain.as_text([])
