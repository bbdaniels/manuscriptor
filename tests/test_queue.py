"""The agent queue, the header status, and the ticker.

A session that edits the author's prose while he is reading is only acceptable
if he can glance up and see what it is doing. A pin on one paragraph is not
that. These tests hold three things:

  * the QUEUE is a real list, oldest first, and every entry names a block the
    page still has. A queue entry addressed to a block id that an edit renamed
    is the same defect that froze the margin on `working`: the frame lands on
    nothing and the author's only sign of life stops moving.
  * the TICKER reports what actually happened, from the log's state records and
    the patch frames, newest first, naming the block by its section rather than
    by a hex id the author never chose.
  * `serve --with-agent` refuses to combine with `--read-only`, and whatever it
    launches dies with the server. A stray session editing a manuscript after
    the server is gone is the worst failure this project has.

Nothing in the queue may call a model: it reads `comments.jsonl` and the block
map, and that is the whole of its knowledge.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from jinja2 import Template

from manuscriptor.server import paths
from manuscriptor import cli
from manuscriptor.server import build as build_mod
from manuscriptor.server import chat, drain
from manuscriptor.server.build import flatten_ws

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "manuscriptor" / "templates"
INDEX = TEMPLATES / "index.html.j2"
STYLES = TEMPLATES / "static" / "styles.css"
VIEWER = TEMPLATES / "static" / "viewer.js"
NODE = shutil.which("node")

DOC = r"""\documentclass{article}
\begin{document}
\section{Results}
The treatment raised screening rates substantially across all three cohorts followed.

We interpret this as evidence that the contract itself, rather than the payment, drove it.

A third paragraph, present so the neighbour context has something to pick up.
\end{document}
"""


def ago_iso(seconds: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat(timespec="seconds")


def setup(tmp_path: Path, body: str = DOC):
    """A manuscript with one comment on its second paragraph."""
    (tmp_path / "main.tex").write_text(body, encoding="utf-8")
    b = build_mod.build(tmp_path)
    bid = [x.id for x in b.blocks if x.kind == "paragraph"][1]
    blk = b.by_id[bid]
    chat.append(
        paths.comments(tmp_path),
        {"id": "c-0001", "kind": "comment", "block": bid, "file": str(blk.file),
         "lines": [blk.line_start, blk.line_end], "quote": blk.source_text[:120],
         "body": "This overclaims. Soften it.", "author": "bb", "ts": ago_iso(300)},
    )
    return tmp_path, bid, b


def comment_on(d: Path, blocks, index: int, cid: str, body: str, *, age: float = 0.0) -> str:
    """Leave a comment on the nth paragraph and return its block id."""
    para = [x for x in blocks if x.kind == "paragraph"][index]
    chat.append(
        paths.comments(d),
        {"id": cid, "kind": "comment", "block": para.id, "file": str(para.file),
         "lines": [para.line_start, para.line_end], "quote": para.source_text[:120],
         "body": body, "author": "bb", "ts": ago_iso(age)},
    )
    return para.id


# --------------------------------------------------------------------- the queue


def test_the_queue_lists_pending_work_oldest_first(tmp_path):
    """The order a drain should work them, so the list is the plan."""
    d, bid, b = setup(tmp_path)
    comment_on(d, b.blocks, 0, "c-0002", "tighten this", age=100)
    comment_on(d, b.blocks, 2, "c-0003", "and this", age=10)

    q = build_mod.queue_view(paths.comments(d), b.blocks)
    assert [e["id"] for e in q] == ["c-0001", "c-0002", "c-0003"]


def test_a_queue_entry_carries_what_the_header_and_the_margin_need(tmp_path):
    d, bid, b = setup(tmp_path)
    e = build_mod.queue_view(paths.comments(d), b.blocks)[0]
    assert e["id"] == "c-0001"
    assert e["block"] == bid
    assert e["state"] == "queued"
    # Its own opening words, not its section: the fixture has three paragraphs
    # under one `\section{Results}` and naming them all "Results" is what the
    # author read as one item three times over.
    assert e["section"] == \
        "We interpret this as evidence that the contract itself, rather than the…"
    assert e["body"] == "This overclaims. Soften it."
    assert e["since"], "a state with no start cannot be aged"
    assert e["waited"] >= 290, e["waited"]


def test_a_body_is_one_line_because_the_header_is_one_line(tmp_path):
    d, _, b = setup(tmp_path, DOC)
    chat.append(paths.comments(d),
                {"id": "c-0002", "kind": "comment", "block": b.blocks[1].id,
                 "quote": b.blocks[1].source_text[:120],
                 "body": "Cut the second sentence.\n\nThen fold the claim into the first.",
                 "author": "bb"})
    e = [x for x in build_mod.queue_view(paths.comments(d), b.blocks) if x["id"] == "c-0002"][0]
    assert "\n" not in e["body"]
    assert e["body"].startswith("Cut the second sentence. Then fold")


def test_waited_measures_the_current_state_not_the_comment(tmp_path):
    """A comment queued for an hour and picked up ten seconds ago has been
    WORKING for ten seconds. Reporting the hour would say the agent had been
    stuck on it, which is the opposite of what happened."""
    d, _, b = setup(tmp_path)
    chat.append(paths.comments(d),
                {"id": "c-0001", "kind": "state", "state": "working", "ts": ago_iso(9)})
    e = build_mod.queue_view(paths.comments(d), b.blocks)[0]
    assert e["state"] == "working"
    assert e["waited"] < 60, e["waited"]


def test_a_finished_chat_leaves_the_queue(tmp_path):
    d, _, b = setup(tmp_path)
    assert len(build_mod.queue_view(paths.comments(d), b.blocks)) == 1
    drain.mark(d, "c-0001", "done")
    assert build_mod.queue_view(paths.comments(d), b.blocks) == []


def test_a_queue_entry_never_names_a_block_the_page_has_lost(tmp_path):
    """The bug that froze the margin, one layer up.

    Ids are content derived, so answering a comment renames its block. A queue
    entry still carrying the old id names nothing on the page, and the header
    would count work against a paragraph that no longer exists.
    """
    d, bid, _ = setup(tmp_path)
    src = (d / "main.tex").read_text(encoding="utf-8")
    (d / "main.tex").write_text(src.replace("drove it.", "drove it, we now think."), encoding="utf-8")

    b = build_mod.build(d)
    assert bid not in b.by_id, "the fixture must actually change the id"

    q = build_mod.queue_view(paths.comments(d), b.blocks)
    assert len(q) == 1
    assert q[0]["block"] != bid
    assert q[0]["block"] in b.by_id, "every entry must name a live block"
    # The edit landed past the clip, so the name is stable across the rename.
    assert q[0]["section"] == \
        "We interpret this as evidence that the contract itself, rather than the…"


def test_a_chat_whose_paragraph_is_gone_is_listed_without_a_block(tmp_path):
    """Never dropped, and never pointed at the wrong paragraph either."""
    d, _, _ = setup(tmp_path)
    src = (d / "main.tex").read_text(encoding="utf-8")
    (d / "main.tex").write_text(
        src.replace(
            "We interpret this as evidence that the contract itself, rather than the payment, drove it.\n",
            "",
        ),
        encoding="utf-8",
    )
    b = build_mod.build(d)
    q = build_mod.queue_view(paths.comments(d), b.blocks)
    assert len(q) == 1, "never silently discarded"
    assert q[0]["block"] is None
    assert q[0]["section"] is None


def test_the_blob_carries_the_queue_and_the_ticker(tmp_path):
    """A page loading mid-run must not start blank and claim idle."""
    d, bid, _ = setup(tmp_path)
    drain.mark(d, "c-0001", "working")
    blob = build_mod.build(d).blob
    assert [e["id"] for e in blob["queue"]] == ["c-0001"]
    assert blob["queue"][0]["state"] == "working"
    assert blob["ticker"], "the log's state records seed the ticker"


# --------------------------------------------------------------------- the ticker


def test_the_ticker_is_newest_first_and_names_the_section(tmp_path):
    d, _, b = setup(tmp_path)
    drain.mark(d, "c-0001", "working")
    drain.mark(d, "c-0001", "done")
    t = build_mod.ticker_view(paths.comments(d), build_mod.build(d).blocks)
    assert t[0]["state"] == "done"
    assert t[1]["state"] == "working"
    assert t[0]["section"] == \
        "We interpret this as evidence that the contract itself, rather than the…", \
        "the author's terms, not a hex id and not the section three of them share"
    assert t[0]["when"]


def test_a_ticker_entry_carries_what_was_asked(tmp_path):
    """The ticker reports work, and the work is the request.

    `ticker_view` read only the state records, so an entry knew which block was
    moving and never what anyone had asked for. The chats are already in hand
    two lines above -- the id on a state record is the comment it answers.
    """
    d, _, _ = setup(tmp_path)
    drain.mark(d, "c-0001", "working")
    t = build_mod.ticker_view(paths.comments(d), build_mod.build(d).blocks)
    assert t[0]["asked"] == "This overclaims. Soften it."


def test_two_requests_on_one_block_are_two_distinguishable_lines(tmp_path):
    """The case that was impossible. Naming an entry by its block made both
    requests the same line printed twice, with nothing saying which had moved."""
    d, _, b = setup(tmp_path)
    para = [x for x in b.blocks if x.kind == "paragraph"][1]
    chat.append(
        paths.comments(d),
        {"id": "c-0002", "kind": "comment", "block": para.id, "file": str(para.file),
         "lines": [para.line_start, para.line_end], "quote": para.source_text[:120],
         "body": "Also check the citation here.", "author": "bb", "ts": ago_iso(200)},
    )
    drain.mark(d, "c-0001", "working")
    drain.mark(d, "c-0002", "working")
    t = build_mod.ticker_view(paths.comments(d), build_mod.build(d).blocks)

    two = [e for e in t if e["state"] == "working"]
    assert len(two) == 2
    assert two[0]["block"] == two[1]["block"], "same paragraph, which is the point"
    assert {e["asked"] for e in two} == {
        "This overclaims. Soften it.", "Also check the citation here."}

    if NODE:
        lines = [node_call("tickerText", e) for e in two]
        assert lines[0] != lines[1], "two requests, two lines"
        assert all("working" in ln for ln in lines)


def test_a_long_request_is_clipped_in_python_not_only_in_css(tmp_path):
    """The queue and the agent's work item read this string too, and neither
    has a stylesheet."""
    d, _, b = setup(tmp_path)
    para = [x for x in b.blocks if x.kind == "paragraph"][0]
    chat.append(
        paths.comments(d),
        {"id": "c-0009", "kind": "comment", "block": para.id, "file": str(para.file),
         "lines": [para.line_start, para.line_end], "quote": para.source_text[:120],
         "body": " ".join(["rewrite"] * 60), "author": "bb", "ts": ago_iso(10)},
    )
    drain.mark(d, "c-0009", "working")
    t = build_mod.ticker_view(paths.comments(d), build_mod.build(d).blocks)
    asked = next(e for e in t if e["id"] == "c-0009")["asked"]
    assert len(asked) <= 64
    assert asked.endswith("…")


@pytest.mark.skipif(not NODE, reason="node not installed")
def test_a_ticker_line_leads_with_the_request_and_hovers_the_place():
    e = {"kind": "state", "state": "working", "asked": "Soften this claim",
         "section": "We interpret this as evidence"}
    assert node_call("tickerText", e) == "“Soften this claim” · working"
    title = node_call("tickerTitle", e)
    assert "Soften this claim" in title and "We interpret this as evidence" in title


@pytest.mark.skipif(not NODE, reason="node not installed")
def test_a_patch_has_no_request_and_names_its_block_instead():
    """A `patch` is the ONE entry with nothing behind it that could be quoted.

    It reports an edit landing on the page, which no author asked for in words,
    so naming the place is the whole of what it can say.

    This test used to make the same claim about a `state` entry, and that was
    wrong in a way that mattered. A state entry answers a comment, a comment
    always has a body, and both paths that build one derive `asked` from it --
    so a state entry with no request is not a thing the server can produce. By
    asserting on one, this pinned the fallback branch as though it were the
    normal shape of a live line, and the live frames were arriving in exactly
    that shape for months with nothing to say otherwise. What the server does
    produce is now held in `tests/test_live_frames.py`, against the frame it
    actually sends, and the seed side is held below.

    The fallback itself stays in `tickerText`: a line that renders as a bare
    " · done" tells the author nothing at all, and a defensive lead is cheap.
    It is not, however, a description of any entry the server writes.
    """
    assert node_call("tickerText", {"kind": "patch", "section": "Data", "n": 2}) \
        == "Data · edited, 2 blocks"
    assert node_call("tickerTitle", {"kind": "patch", "section": "Data", "n": 2}) == "Data"


def test_a_state_entry_always_says_what_was_asked(tmp_path):
    """The seed half of the guard above: `ticker_view` never omits `asked`.

    A comment carries a body, so there is no state record whose request cannot
    be recovered. If this ever fails, the ticker has an entry that will render
    through the defensive fallback -- and the author will read a line about his
    paragraph where he should be reading one about his request.
    """
    d, _, b = setup(tmp_path)
    drain.mark(d, "c-0001", "working")
    drain.mark(d, "c-0001", "done")
    t = build_mod.ticker_view(paths.comments(d), build_mod.build(d).blocks)
    assert t, "the fixture marked a comment worked and finished"
    for e in t:
        assert e["asked"], f"a state entry with no request: {e!r}"


@pytest.mark.skipif(not NODE, reason="node not installed")
def test_the_ticker_key_separates_two_requests_on_one_block():
    """The key decides whether a refresh redraws. Two requests on one paragraph
    share kind, state, block and often the second they landed in, so without
    the comment id the second one never appears."""
    a = {"kind": "state", "state": "working", "id": "c-1", "block": "b-x", "when": "T"}
    b = {"kind": "state", "state": "working", "id": "c-2", "block": "b-x", "when": "T"}
    assert node_call("tickerKey", [a]) != node_call("tickerKey", [b])


def test_a_block_with_no_section_is_still_named_something_the_author_can_find(tmp_path):
    """Watched live: a real session picked up a comment on the ABSTRACT and the
    ticker read "the manuscript · working". The abstract sits above every
    heading, so it genuinely has no section, and the fallback told the author
    nothing at all. A file and a line is a place he can go.

    The abstract now names itself, by its own first sentence -- which is both
    better than a file and a line and the reason to check no markup rides along
    with it: the block's source opens `\\begin{abstract}`, and that environment
    is part of the block, not part of what the author calls it.
    """
    body = DOC.replace("\\begin{document}\n",
                       "\\begin{document}\n\\begin{abstract}\nThis paper examines a contract.\n\\end{abstract}\n")
    (tmp_path / "main.tex").write_text(body, encoding="utf-8")
    b = build_mod.build(tmp_path)
    blk = next(x for x in b.blocks if "examines a contract" in x.source_text)
    assert blk.parent_heading is None, "the fixture must actually be sectionless"
    chat.append(paths.comments(tmp_path),
                {"id": "c-0001", "kind": "comment", "block": blk.id, "file": str(blk.file),
                 "quote": blk.source_text[:120], "body": "tighten", "author": "bb"})

    e = build_mod.queue_view(paths.comments(tmp_path), b.blocks, root=tmp_path)[0]
    assert e["section"] == "This paper examines a contract."
    assert "\\" not in e["section"] and "{" not in e["section"]
    assert e["where"] == f"main.tex:{blk.line_start}", e["where"]


def test_a_block_with_neither_a_section_nor_words_falls_back_to_its_place(tmp_path):
    """The `where` fallback, still exercised now that prose names itself. A
    captionless figure above every heading has nothing to say about itself."""
    body = DOC.replace(
        "\\begin{document}\n",
        "\\begin{document}\n\\begin{figure}\n\\includegraphics{plot.pdf}\n\\end{figure}\n")
    (tmp_path / "main.tex").write_text(body, encoding="utf-8")
    b = build_mod.build(tmp_path)
    blk = next(x for x in b.blocks if x.kind == "figure")
    assert blk.parent_heading is None and blk.caption is None
    chat.append(paths.comments(tmp_path),
                {"id": "c-0001", "kind": "comment", "block": blk.id, "file": str(blk.file),
                 "quote": blk.source_text[:120], "body": "crop it", "author": "bb"})

    e = build_mod.queue_view(paths.comments(tmp_path), b.blocks, root=tmp_path)[0]
    assert e["section"] is None
    assert e["where"] == f"main.tex:{blk.line_start}", e["where"]


def test_the_authors_own_comment_is_not_agent_activity(tmp_path):
    """Observed in a browser: three comments filled the ticker with three lines
    reading `queued` and pushed the agent's actual work out of it. Queued is the
    STANDING state and the header already counts it. The ticker is for what
    moved.
    """
    d, _, _ = setup(tmp_path)
    blocks = build_mod.build(d).blocks
    assert build_mod.ticker_view(paths.comments(d), blocks) == []
    chat.append(paths.comments(d), {"id": "c-0001", "kind": "state", "state": "queued"})
    assert build_mod.ticker_view(paths.comments(d), blocks) == []
    drain.mark(d, "c-0001", "working")
    assert len(build_mod.ticker_view(paths.comments(d), blocks)) == 1


def test_the_ticker_is_a_handful_not_a_scrollback(tmp_path):
    d, _, b = setup(tmp_path)
    for i in range(12):
        chat.append(paths.comments(d),
                    {"id": "c-0001", "kind": "state",
                     "state": "working" if i % 2 else "queued", "ts": ago_iso(100 - i)})
    t = build_mod.ticker_view(paths.comments(d), build_mod.build(d).blocks)
    assert 0 < len(t) <= 8, len(t)


# ------------------------------------------------------------------ the history
#
# The queue is what is waiting and the ticker is what just happened. Neither is
# a record of the work itself: the sentences the agent wrote while doing it
# lived in an 80-entry ring that the next drain restart overwrote, so the
# author could watch it work and could never afterwards read what it had done.


def wrote(d: Path, *lines) -> None:
    """Lines the drain's session emitted, written the way the drain writes them.

    Through `Feed`, not by hand: a test that types the file out asserts on a
    shape nothing produces, which is how the seed path came to be checked
    everywhere and the live path nowhere.
    """
    from manuscriptor.server import feed as feed_mod

    feed = feed_mod.Feed(path=feed_mod.progress_path(paths.agent_dir(d)), every=0.0)
    feed.add([feed_mod.Entry(feed_mod.now(), who, kind, text, tuple(work))
              for who, kind, text, work in lines], force=True)


def history_of(d: Path, **kw):
    from manuscriptor.server import feed as feed_mod

    b = build_mod.build(d)
    return build_mod.history_view(
        paths.comments(d), b.blocks,
        feed_mod.read_history(feed_mod.history_path(paths.agent_dir(d))),
        root=d, **kw)


def test_a_work_item_carries_the_request_the_place_the_lines_and_the_outcome(tmp_path):
    """The whole row, which is the point: what was asked, where it landed, what
    the agent said while doing it, and how it ended."""
    d, bid, _ = setup(tmp_path)
    drain.mark(d, "c-0001", "working")
    wrote(d,
          ("agent", "text", "Reading the analysis script.", ["c-0001"]),
          ("teammate", "thinking", "the negation prefix was never checked here", ["c-0001"]),
          ("agent", "note", "edited main.tex · +6 −4", ["c-0001"]))
    drain.reply(d, "c-0001", "Softened it and kept the interval.")
    drain.mark(d, "c-0001", "done")

    got = history_of(d)
    assert len(got) == 1, got
    row = got[0]
    assert row["id"] == "c-0001"
    assert row["asked"] == "This overclaims. Soften it."
    assert row["state"] == "done", "the outcome comes from the log, not from the ledger"
    assert row["block"] == bid and row["section"], "a row must be somewhere he can go"
    assert row["reply"] == "Softened it and kept the interval."
    assert [l["text"] for l in row["lines"]] == [
        "Reading the analysis script.",
        "the negation prefix was never checked here",
        "edited main.tex · +6 −4",
    ], "the lines of one item read forwards, as a narrative"
    assert row["lines"][1]["who"] == "teammate"


def test_the_history_survives_a_restart_of_both_processes(tmp_path):
    """The requirement in one test: the ledger is on disk, and the payload a
    fresh build hands a fresh page carries it."""
    d, _, _ = setup(tmp_path)
    wrote(d, ("agent", "text", "Tightening the claim.", ["c-0001"]))
    drain.mark(d, "c-0001", "done")

    blob = build_mod.build(d).blob          # a new server process, a new page
    assert [r["id"] for r in blob["history"]] == ["c-0001"]
    assert blob["history"][0]["lines"][0]["text"] == "Tightening the claim."
    assert blob["agent_feed"]["entries"], "the live feed must not be regressed by this"


def test_a_line_about_the_appendix_is_not_shown_under_the_paper(tmp_path):
    """The ledger is per manuscript DIRECTORY, because the drain session is,
    while this payload is per DOCUMENT. Reconciled by the comment each line
    names: a line belongs to whichever document its comment does."""
    d, _, b = setup(tmp_path)
    (d / "appendix.tex").write_text(DOC.replace("Results", "Appendix"), encoding="utf-8")
    for cid, doc in (("c-0008", "main.tex"), ("c-0009", "appendix.tex")):
        chat.append(paths.comments(d),
                    {"id": cid, "kind": "comment", "block": "", "doc": doc,
                     "body": f"the {doc} request", "author": "bb", "ts": ago_iso(50)})
    wrote(d,
          ("agent", "text", "about the paper", ["c-0008"]),
          ("agent", "text", "about the appendix", ["c-0009"]))
    drain.mark(d, "c-0008", "done")
    drain.mark(d, "c-0009", "done")

    paper = [r["id"] for r in history_of(d, doc="main.tex")]
    assert "c-0008" in paper and "c-0009" not in paper, paper
    assert all("appendix" not in l["text"]
               for r in history_of(d, doc="main.tex") for l in r["lines"])

    appendix = [r["id"] for r in history_of(d, doc="appendix.tex")]
    assert "c-0009" in appendix and "c-0008" not in appendix, appendix


def test_a_line_that_could_not_be_told_apart_says_so_under_each(tmp_path):
    """Three comments in flight and a line that names none of them. It is shown
    under each and marked shared, because the alternative is to invent the link
    the ledger exists to make trustworthy."""
    d, _, b = setup(tmp_path)
    comment_on(d, b.blocks, 0, "c-0002", "and this one", age=100)
    wrote(d, ("agent", "text", "Reading the analysis script.", ["c-0001", "c-0002"]))
    rows = {r["id"]: r for r in history_of(d)}
    assert set(rows) == {"c-0001", "c-0002"}
    assert all(r["lines"][0]["shared"] is True for r in rows.values())


def test_a_comment_nobody_has_touched_is_not_history(tmp_path):
    """It is in the queue. Putting it here as well would make the history a
    second copy of the queue with none of its ordering."""
    d, _, _ = setup(tmp_path)
    assert history_of(d) == []


def test_the_newest_work_is_first_and_the_list_is_bounded(tmp_path):
    d, _, b = setup(tmp_path)
    for i in range(2, 20):
        comment_on(d, b.blocks, i % 3, f"c-{i:04d}", f"request {i}", age=1000 - i * 10)
        chat.append(paths.comments(d), {"id": f"c-{i:04d}", "kind": "state",
                                        "state": "done", "ts": ago_iso(900 - i * 10)})
    got = history_of(d)
    assert len(got) == build_mod.HISTORY_LIMIT, len(got)
    assert got[0]["id"] == "c-0019", "the newest work must lead"
    assert got[0]["at"] >= got[-1]["at"]


# ------------------------------------------------------------------- the frames


def collect_frames(session):
    sent = []
    session.broadcast = lambda msg: sent.append(msg) or asyncio.sleep(0)
    return sent


def test_a_queue_frame_is_broadcast_when_the_log_changes(tmp_path):
    from manuscriptor.server.app import Session

    d, bid, _ = setup(tmp_path)
    s = Session(d)
    asyncio.run(s.on_log_change())
    sent = collect_frames(s)

    drain.mark(d, "c-0001", "working")
    asyncio.run(s.on_log_change())

    qs = [m for m in sent if m["type"] == "queue"]
    assert qs, "the page cannot show a queue it is never sent"
    assert [e["state"] for e in qs[-1]["queue"]] == ["working"]


def test_the_queue_is_not_repainted_when_nothing_changed(tmp_path):
    """Re-reading an unchanged log must not repaint anything."""
    from manuscriptor.server.app import Session

    d, _, _ = setup(tmp_path)
    s = Session(d)
    asyncio.run(s.on_log_change())
    sent = collect_frames(s)
    asyncio.run(s.on_log_change())
    assert sent == []


def test_a_history_frame_goes_out_when_either_half_of_the_row_moves(tmp_path):
    """A row is a join of two files that move independently.

    The drain appends a line to the ledger, which is one watcher; the outcome
    that closes the row is a state record in the comment log, which is the
    other. Pushed from both, or the row carries the agent's sentences and then
    sits on `working` until something unrelated happens to shift it.
    """
    from manuscriptor.server.app import Session

    d, _, _ = setup(tmp_path)
    s = Session(d)
    asyncio.run(s.on_log_change())
    sent = collect_frames(s)

    wrote(d, ("agent", "text", "Reading the analysis script.", ["c-0001"]))
    asyncio.run(s.on_feed_change())
    hs = [m for m in sent if m["type"] == "history"]
    assert hs, "a line landed in the ledger and the page was never told"
    assert hs[-1]["history"][0]["lines"][0]["text"] == "Reading the analysis script."
    assert hs[-1]["history"][0]["state"] == "queued"

    drain.mark(d, "c-0001", "done")
    asyncio.run(s.on_log_change())
    hs = [m for m in sent if m["type"] == "history"]
    assert hs[-1]["history"][0]["state"] == "done", (
        "the outcome landed in the comment log and the row never closed"
    )


def test_the_history_is_not_repainted_when_nothing_changed(tmp_path):
    """It is compared whole rather than by an identity tuple, because a row
    changes by GAINING A LINE and no summary of ids and states can see that."""
    from manuscriptor.server.app import Session

    d, _, _ = setup(tmp_path)
    wrote(d, ("agent", "text", "Reading it.", ["c-0001"]))
    s = Session(d)
    asyncio.run(s.on_feed_change())
    sent = collect_frames(s)
    asyncio.run(s.on_feed_change())
    assert [m for m in sent if m["type"] == "history"] == []

    wrote(d, ("agent", "text", "Now rewriting it.", ["c-0001"]))
    asyncio.run(s.on_feed_change())
    assert [m for m in sent if m["type"] == "history"], (
        "a second line on the same row, in the same state, moved nothing"
    )


def test_a_queue_frame_is_reanchored_like_the_state_frame(tmp_path):
    """The whole point of item 1, asserted end to end.

    The agent edits the paragraph and then reports on it. The state frame is
    re-anchored today; a queue frame that is not would name the dead id and the
    header would count work on a block the page cannot find.
    """
    from manuscriptor.server.app import Session

    d, bid, _ = setup(tmp_path)
    s = Session(d)
    asyncio.run(s.on_log_change())

    src = (d / "main.tex").read_text(encoding="utf-8")
    (d / "main.tex").write_text(src.replace("drove it.", "drove it, we now think."), encoding="utf-8")
    asyncio.run(s.on_change())

    sent = collect_frames(s)
    drain.mark(d, "c-0001", "working")
    asyncio.run(s.on_log_change())

    q = [m for m in sent if m["type"] == "queue"][-1]["queue"]
    live = {b.id for b in s.build.blocks}
    assert q and q[0]["block"] != bid
    assert q[0]["block"] in live, "a frame naming a block the page lost is the freeze bug"


def test_a_state_frame_says_when_it_happened(tmp_path):
    """The ticker reports what happened, so it needs the log's time and not the
    client's clock at the moment the frame arrived."""
    from manuscriptor.server.app import Session

    d, _, _ = setup(tmp_path)
    s = Session(d)
    asyncio.run(s.on_log_change())
    sent = collect_frames(s)
    drain.mark(d, "c-0001", "working")
    asyncio.run(s.on_log_change())
    st = [m for m in sent if m["type"] == "state"][-1]
    assert st.get("at"), "a state frame with no time cannot be aged"


def test_a_state_frame_is_timed_BY_THE_STATE_not_by_the_comment(tmp_path):
    """Found by watching the running page, not by reading the code.

    Every ticker entry claimed to be older than it was: a `done` a second old
    read as six seconds, and the newest line sat above older ones carrying a
    larger age. The frame was carrying the COMMENT's timestamp, because that is
    what `by_block` puts on a message. What the ticker needs is when the state
    changed.
    """
    from manuscriptor.server.app import Session

    d, _, _ = setup(tmp_path)          # the comment is five minutes old
    s = Session(d)
    asyncio.run(s.on_log_change())
    sent = collect_frames(s)
    drain.mark(d, "c-0001", "working")
    asyncio.run(s.on_log_change())

    st = [m for m in sent if m["type"] == "state"][-1]
    age = (datetime.now(timezone.utc)
           - datetime.fromisoformat(st["at"])).total_seconds()
    assert age < 30, f"the frame is timed by the comment, not by the state ({age:.0f}s old)"


def test_proc_tells_the_session_the_order_and_to_mark_it(tmp_path):
    """`proc` is what a session reads, so the queue's two rules belong in it and
    not only in a skill file it may not have loaded: work them oldest first, and
    say you have started before you start."""
    d, _, b = setup(tmp_path)
    comment_on(d, b.blocks, 0, "c-0002", "and this", age=10)
    text = drain.as_text(drain.collect(d))
    assert text.index("c-0001") < text.index("c-0002"), "oldest first"
    head = text[: text.index("=" * 20)]
    assert "oldest first" in head.lower()
    assert "working" in head


def test_the_queue_never_calls_a_model():
    """The invariant, held in the two modules a queue would be tempted to reach
    out of. The server has zero knowledge of Claude: it may say the name in a
    docstring, and may not import a client or start a process.
    """
    for name in ("build.py", "app.py", "drain.py"):
        text = (ROOT / "manuscriptor" / "server" / name).read_text(encoding="utf-8")
        for banned in ("import anthropic", "from anthropic", "import openai",
                       "import subprocess", "Popen", "os.system"):
            assert banned not in text, f"{banned} reached server/{name}"


# ------------------------------------------------------- the client, under node


def node_call(fn: str, *args):
    assert NODE, "node is required for these tests"
    script = (
        "const v = require(%s);\n"
        "const out = v[%s].apply(null, JSON.parse(process.argv[1]));\n"
        "process.stdout.write(JSON.stringify(out === undefined ? null : out));\n"
    ) % (json.dumps(str(VIEWER)), json.dumps(fn))
    p = subprocess.run([NODE, "-e", script, json.dumps(list(args))],
                       capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout)


@pytest.mark.skipif(not NODE, reason="node not installed")
def test_the_header_summarises_the_queue():
    q = [
        {"id": "c-1", "state": "queued"}, {"id": "c-2", "state": "queued"},
        {"id": "c-3", "state": "working"}, {"id": "c-4", "state": "queued"},
    ]
    assert node_call("queueSummary", q) == "3 queued · 1 working"


@pytest.mark.skipif(not NODE, reason="node not installed")
def test_an_empty_queue_reads_idle():
    assert node_call("queueSummary", []) == "idle"
    assert node_call("queueSummary", None) == "idle"


@pytest.mark.skipif(not NODE, reason="node not installed")
def test_a_ticker_line_names_the_section_not_the_id():
    assert node_call("tickerText", {"kind": "state", "state": "working",
                                    "section": "Results"}) == "Results · working"
    assert node_call("tickerText", {"kind": "patch", "section": "Data", "n": 1}) == "Data · edited"
    line = node_call("tickerText", {"kind": "state", "state": "done", "block": "b-3f2a91c0de"})
    assert "b-3f2a91c0de" not in line, "a hex id is not the author's term for a paragraph"
    # a sectionless block (an abstract) still names a place he can go
    assert node_call("tickerText", {"kind": "state", "state": "done", "section": None,
                                    "where": "main.tex:57"}) == "main.tex:57 · done"


@pytest.mark.skipif(not NODE, reason="node not installed")
def test_the_queue_follows_a_block_through_a_rename():
    """An edit renames its block, so a queue entry keyed to the old id would
    stop matching the page the moment the agent answered the comment."""
    q = [{"id": "c-1", "block": "b-old", "state": "working"}]
    out = node_call("renameQueue", q, {"b-old": "b-new"})
    assert out[0]["block"] == "b-new"
    assert out[0]["id"] == "c-1"


# ------------------------------------------------------------------ the template


def test_the_header_carries_the_agent_status_and_the_ticker():
    tpl = Template(INDEX.read_text(encoding="utf-8"))
    html = tpl.render(ms={"title": "T", "html": "", "blocks": {}, "outline": [],
                          "chats": {}, "todos": [], "activity": [], "queue": [],
                          "ticker": [], "stats": {}},
                      styles_css="", viewer_js="")
    assert 'id="agent-status"' in html, "the standing state has nowhere to live"
    assert 'id="ticker"' in html, "the ticker has nowhere to live"


def test_the_client_handles_the_queue_frame():
    js = VIEWER.read_text(encoding="utf-8")
    assert "'queue'" in js, "the queue frame is unhandled"


def test_the_ticker_respects_reduced_motion():
    css = STYLES.read_text(encoding="utf-8")
    assert ".ticker" in css
    i = css.find(".tk")
    assert i != -1
    assert "prefers-reduced-motion" in css
    # any animation on the ticker has to sit behind the no-preference guard
    for line in css.splitlines():
        if ".tk" in line and "animation:" in line:
            assert "no-preference" in css[: css.find(line)][-400:], (
                "the ticker animates unconditionally"
            )


# ------------------------------------------------------------- serve --with-agent


def test_an_agent_that_cannot_write_is_refused(tmp_path):
    """A read-only agent is a contradiction, and silently dropping one of two
    flags the author typed is worse than refusing."""
    (tmp_path / "main.tex").write_text(DOC, encoding="utf-8")
    with pytest.raises(SystemExit) as e:
        cli.main(["serve", str(tmp_path), "--with-agent", "--read-only", "--no-window"])
    msg = str(e.value)
    assert "--with-agent" in msg and "--read-only" in msg


def test_the_agent_needs_claude_on_the_path(tmp_path, monkeypatch):
    (tmp_path / "main.tex").write_text(DOC, encoding="utf-8")
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    with pytest.raises(SystemExit) as e:
        cli.main(["serve", str(tmp_path), "--with-agent", "--no-window"])
    assert "claude" in str(e.value)


def test_the_agent_runs_in_the_manuscript_directory(tmp_path):
    """It must inherit the author's CLAUDE.md and skills, which means its cwd is
    the manuscript rather than wherever serve was invoked."""
    (tmp_path / "main.tex").write_text(DOC, encoding="utf-8")
    script = cli.agent_loop_script(tmp_path, claude="/usr/bin/claude", manuscriptor="/usr/bin/manuscriptor")
    assert "/usr/bin/manuscriptor" in script and "--wait" in script
    assert "/usr/bin/claude" in script
    assert str(tmp_path.resolve()) in script
    log = cli.agent_log_path(tmp_path)
    assert log.parent.is_dir()
    assert log.parent == paths.agent_dir(tmp_path)


def test_the_agent_is_killed_with_the_server(tmp_path):
    """The worst failure mode here is a session still editing a manuscript after
    the server is gone. The child gets its own process group so the whole tree
    dies, not just the shell at the top of it."""
    pidfile = tmp_path / "child.pid"
    script = tmp_path / "loop.sh"
    script.write_text(
        "#!/bin/sh\nsleep 60 &\necho $! > %s\nwait\n" % pidfile, encoding="utf-8"
    )
    proc = cli.spawn_group(["/bin/sh", str(script)], cwd=tmp_path, log_path=tmp_path / "a.log")
    for _ in range(80):
        if pidfile.exists() and pidfile.read_text().strip():
            break
        time.sleep(0.05)
    grandchild = int(pidfile.read_text().strip())
    os.kill(grandchild, 0)          # alive before

    cli.terminate_group(proc, grace=3.0)

    assert proc.poll() is not None, "the launcher itself survived"
    deadline = time.time() + 3
    while time.time() < deadline:
        try:
            os.kill(grandchild, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        os.kill(grandchild, signal.SIGKILL)
        pytest.fail("the grandchild survived: a stray session would keep editing")


def test_the_loop_reaches_manuscriptor_even_when_it_is_not_on_the_path(tmp_path):
    """The console script is not always on PATH -- it was not on this machine
    until it was symlinked. The fallback has to be ONE executable, because the
    script runs it as one word: a bare "python -m manuscriptor.cli" would be
    looked up as a file with that literal name and the loop would never run.
    """
    (tmp_path / "main.tex").write_text(DOC, encoding="utf-8")
    out = cli.agent_log_path(tmp_path).parent
    real = shutil.which
    cmd = cli.manuscriptor_command(out, which=lambda name: None if name == "manuscriptor" else real(name))
    assert " " not in cmd, cmd
    assert os.access(cmd, os.X_OK), f"{cmd} is not executable"
    p = subprocess.run([cmd, "--version"], capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    assert "manuscriptor" in p.stdout


def test_one_session_starts_at_once_and_works_what_arrives(tmp_path):
    """The persistent loop's control flow, with a stand-in for the session.

    The old design started a session per wake and guarded hard against
    starting one for an empty queue or a broken build, because each start
    cost a ~90 second boot. The persistent design inverts it: ONE session
    starts immediately (empty queue and all: it parks), works anything
    already pending, and is restarted by the shell when it exits. The
    stand-in mimics a session that drains once and parks once, then exits;
    the shell restarts it, which is also how the ~20-wake refresh works.
    """
    d, _, b = setup(tmp_path)                    # c-0001 already pending
    ms = shutil.which("manuscriptor")
    if not ms:
        pytest.skip("manuscriptor console script not on PATH")

    calls = tmp_path / "calls.txt"
    fake = tmp_path / "fake-claude"
    fake.write_text(
        "#!/bin/sh\n"
        f"echo call >> {calls}\n"
        f'for id in $("{ms}" proc "{tmp_path}" --json | grep \'"chat_id"\' | cut -d\'"\' -f4); do\n'
        f'  "{ms}" state "{tmp_path}" "$id" done\n'
        "done\n"
        f'"{ms}" proc "{tmp_path}" --wait\n',
        encoding="utf-8",
    )
    fake.chmod(0o755)

    log = tmp_path / "agent.log"
    script = tmp_path / "loop.sh"
    script.write_text(cli.agent_loop_script(tmp_path, claude=str(fake), manuscriptor=ms),
                      encoding="utf-8")
    proc = cli.spawn_group(["/bin/sh", str(script)], cwd=tmp_path, log_path=log)

    def wait_until(pred, secs, what):
        end = time.time() + secs
        while time.time() < end:
            if pred():
                return
            time.sleep(0.2)
        pytest.fail(f"{what}; log:\n{log.read_text(encoding='utf-8') if log.exists() else '(none)'}")

    def closed(cid):
        return cid not in {c.id for c in chat.pending(paths.comments(tmp_path))}

    try:
        wait_until(lambda: closed("c-0001"), 45,
                   "the comment waiting before the loop started was never worked")
        assert calls.read_text(encoding="utf-8").count("call") >= 1
        comment_on(tmp_path, b.blocks, 0, "c-0002", "and tighten this one")
        wait_until(lambda: closed("c-0002"), 45,
                   "the restarted session never worked the next comment")
        assert proc.poll() is None, "the shell loop exited instead of restarting"
    finally:
        cli.terminate_group(proc)
    assert proc.poll() is not None


def test_the_agent_log_is_invisible_to_the_manuscript_repository(tmp_path):
    """Serving a paper must never be the reason git status grows."""
    (tmp_path / "main.tex").write_text(DOC, encoding="utf-8")
    log = cli.agent_log_path(tmp_path)
    assert log.is_relative_to(paths.home(tmp_path))
    # The rule sits on the hidden directory, one level above, and covers
    # everything under it that is not the comment record.
    rule = (paths.home(tmp_path) / ".gitignore").read_text(encoding="utf-8")
    assert rule.splitlines() == ["*", "!comments.jsonl"]


def test_an_extension_can_say_a_whole_sentence_in_the_ticker():
    """The extension contract offered notify(text) while the ticker renderer
    read section/where/kind/state and a `when` timestamp, so anything an
    extension announced rendered as "the manuscript · undefined" at the wrong
    age. Caught by the first feature that tried to use it."""
    from pathlib import Path

    js = Path(__file__).resolve().parent.parent / "manuscriptor/templates/static/viewer.js"
    src = js.read_text(encoding="utf-8")

    fn = src[src.index("function tickerText"):]
    fn = fn[: fn.index("\n  }")]
    assert "if (e.text) return" in fn, "a plain sentence must render as itself"

    notify = src[src.index("notify: function"):]
    notify = notify[: notify.index("\n")]
    assert "when:" in notify, "and carry the field the renderer actually reads"
    assert "at:" not in notify


# ------------------------------------------------- documents sharing one log
#
# A directory can hold several documents (the paper, the appendix, a response
# to reviewers), Overleaf-style, and they share the one comments.jsonl. A
# comment left on the response must never be presented while the paper is
# being served or drained. New records carry the document they were left on;
# a record without one (the whole existing corpus) belongs to whichever
# document is being read, which is exact for the single-document manuscripts
# that wrote those records.

APPENDIX = r"""\documentclass{article}
\begin{document}
\section{Robustness}
The appendix paragraph, different words so its block ids differ from the paper's.

A second appendix paragraph, also its own words entirely.
\end{document}
"""


def two_docs(tmp_path: Path):
    (tmp_path / "main.tex").write_text(DOC, encoding="utf-8")
    (tmp_path / "appendix.tex").write_text(APPENDIX, encoding="utf-8")
    return build_mod.build(tmp_path), build_mod.build(tmp_path, main="appendix.tex")


def test_a_comment_is_scoped_to_the_document_it_was_left_on(tmp_path):
    paper, appendix = two_docs(tmp_path)
    log = paths.comments(tmp_path)
    p0 = [x for x in paper.blocks if x.kind == "paragraph"][0]
    a0 = [x for x in appendix.blocks if x.kind == "paragraph"][0]
    chat.append(log, {"id": "c-0001", "kind": "comment", "block": p0.id,
                      "quote": p0.source_text[:120], "body": "on the paper",
                      "author": "bb", "doc": "main.tex"})
    chat.append(log, {"id": "c-0002", "kind": "comment", "block": a0.id,
                      "quote": a0.source_text[:120], "body": "on the appendix",
                      "author": "bb", "doc": "appendix.tex"})

    q_paper = build_mod.queue_view(log, paper.blocks, doc="main.tex")
    q_appx = build_mod.queue_view(log, appendix.blocks, doc="appendix.tex")
    assert [e["id"] for e in q_paper] == ["c-0001"]
    assert [e["id"] for e in q_appx] == ["c-0002"]
    # No doc argument reads everything, which is what a whole-log audit wants.
    assert len(build_mod.queue_view(log, paper.blocks)) == 2


def test_a_legacy_record_belongs_to_the_document_being_read(tmp_path):
    # Every record written before documents existed has no doc field. Treating
    # those as global would spray one manuscript's comments across another;
    # treating them as this-doc is exact for the single-document manuscripts
    # that wrote them.
    paper, appendix = two_docs(tmp_path)
    log = paths.comments(tmp_path)
    p0 = [x for x in paper.blocks if x.kind == "paragraph"][0]
    chat.append(log, {"id": "c-0001", "kind": "comment", "block": p0.id,
                      "quote": p0.source_text[:120], "body": "written last month",
                      "author": "bb"})
    assert [c.id for c in chat.pending(log, doc="main.tex")] == ["c-0001"]
    assert [c.id for c in chat.pending(log, doc="appendix.tex")] == ["c-0001"]


def test_the_ticker_only_reports_this_documents_work(tmp_path):
    paper, appendix = two_docs(tmp_path)
    log = paths.comments(tmp_path)
    p0 = [x for x in paper.blocks if x.kind == "paragraph"][0]
    a0 = [x for x in appendix.blocks if x.kind == "paragraph"][0]
    chat.append(log, {"id": "c-0001", "kind": "comment", "block": p0.id,
                      "quote": p0.source_text[:120], "body": "paper",
                      "author": "bb", "doc": "main.tex"})
    chat.append(log, {"id": "c-0002", "kind": "comment", "block": a0.id,
                      "quote": a0.source_text[:120], "body": "appendix",
                      "author": "bb", "doc": "appendix.tex"})
    drain.mark(tmp_path, "c-0001", "done")
    drain.mark(tmp_path, "c-0002", "done")

    t_paper = build_mod.ticker_view(log, paper.blocks, doc="main.tex")
    assert [e["id"] for e in t_paper] == ["c-0001"]


def test_the_blob_names_its_document_and_the_alternatives(tmp_path):
    paper, appendix = two_docs(tmp_path)
    assert paper.blob["main"] == "main.tex"
    assert appendix.blob["main"] == "appendix.tex"
    assert paper.blob["docs"] == ["main.tex", "appendix.tex"]


def test_the_blob_scopes_its_chats_and_queue_to_its_document(tmp_path):
    _, appendix = two_docs(tmp_path)
    log = paths.comments(tmp_path)
    a0 = [x for x in appendix.blocks if x.kind == "paragraph"][0]
    chat.append(log, {"id": "c-0001", "kind": "comment", "block": a0.id,
                      "quote": a0.source_text[:120], "body": "appendix only",
                      "author": "bb", "doc": "appendix.tex"})
    paper = build_mod.build(tmp_path)
    assert paper.blob["queue"] == []
    assert paper.blob["chats"] == {}
    appendix = build_mod.build(tmp_path, main="appendix.tex")
    assert [e["id"] for e in appendix.blob["queue"]] == ["c-0001"]


def test_the_drain_presents_only_the_documents_own_comments(tmp_path):
    paper, appendix = two_docs(tmp_path)
    log = paths.comments(tmp_path)
    a0 = [x for x in appendix.blocks if x.kind == "paragraph"][0]
    chat.append(log, {"id": "c-0001", "kind": "comment", "block": a0.id,
                      "quote": a0.source_text[:120], "body": "appendix only",
                      "author": "bb", "doc": "appendix.tex"})
    assert drain.collect(tmp_path) == []
    got = drain.collect(tmp_path, main="appendix.tex")
    assert [i.chat_id for i in got] == ["c-0001"]


def test_a_comment_typed_on_the_page_records_its_document(tmp_path):
    from manuscriptor.server.app import Session

    (tmp_path / "main.tex").write_text(DOC, encoding="utf-8")
    (tmp_path / "appendix.tex").write_text(APPENDIX, encoding="utf-8")
    s = Session(tmp_path, main="appendix.tex")
    bid = [x.id for x in s.build.blocks if x.kind == "paragraph"][0]
    asyncio.run(s.on_chat(bid, "note on the appendix"))
    recs = chat.read_records(paths.comments(tmp_path))
    assert recs[-1]["doc"] == "appendix.tex"


def test_the_session_switches_documents_and_refuses_a_stranger(tmp_path):
    from manuscriptor.server.app import Session

    (tmp_path / "main.tex").write_text(DOC, encoding="utf-8")
    (tmp_path / "appendix.tex").write_text(APPENDIX, encoding="utf-8")
    s = Session(tmp_path)
    assert s.blob["main"] == "main.tex"
    s.switch("appendix.tex")
    assert s.blob["main"] == "appendix.tex"
    with pytest.raises(ValueError):
        s.switch("../../../etc/passwd")
    with pytest.raises(ValueError):
        s.switch("nonexistent.tex")
    assert s.blob["main"] == "appendix.tex", "a refused switch must change nothing"


# ------------------------------------------------ the document chat, and replies
#
# Two gaps closed together. A chat about the manuscript rather than one
# paragraph: a comment with no block, drained like any other but presented as
# document-level work. And a voice for the agent: a `reply` record joins its
# comment's chat as words, where before the agent could only signal states.


def test_a_reply_joins_its_comments_chat(tmp_path):
    d, bid, b = setup(tmp_path)
    drain.reply(d, "c-0001", "Softened it and moved the caveat up front.")
    msgs = chat.by_block(paths.comments(d))[bid]
    assert len(msgs) == 2
    assert msgs[0]["who"] == "bb"
    assert msgs[1]["who"] == "claude"
    assert msgs[1]["body"].startswith("Softened it")
    assert msgs[0]["id"] != msgs[1]["id"], "two messages sharing an id dedupe into one"


def test_a_reply_is_scoped_with_its_comment(tmp_path):
    paper, appendix = two_docs(tmp_path)
    log = paths.comments(tmp_path)
    a0 = [x for x in appendix.blocks if x.kind == "paragraph"][0]
    chat.append(log, {"id": "c-0001", "kind": "comment", "block": a0.id,
                      "quote": a0.source_text[:120], "body": "on the appendix",
                      "author": "bb", "doc": "appendix.tex"})
    drain.reply(tmp_path, "c-0001", "Done.")
    assert chat.by_block(log, doc="main.tex") == {}
    assert len(chat.by_block(log, doc="appendix.tex")[a0.id]) == 2


def test_a_reply_reaches_an_open_page(tmp_path):
    from manuscriptor.server.app import Session

    d, bid, _ = setup(tmp_path)
    s = Session(d)
    asyncio.run(s.on_log_change())
    sent = collect_frames(s)
    drain.reply(d, "c-0001", "Rewrote the second sentence.")
    asyncio.run(s.on_log_change())
    frames = [m for m in sent if m["type"] == "chat"]
    assert frames, "a reply the page is never told about is a reply that did not happen"
    assert frames[-1]["message"]["who"] == "claude"


def test_a_document_comment_needs_no_block(tmp_path):
    from manuscriptor.server.app import Session

    (tmp_path / "main.tex").write_text(DOC, encoding="utf-8")
    s = Session(tmp_path)
    asyncio.run(s.on_chat("", "The intro overclaims throughout; tone it down."))
    rec = chat.read_records(paths.comments(tmp_path))[-1]
    assert rec["block"] == ""
    assert rec["doc"] == "main.tex"
    q = build_mod.queue_view(paths.comments(tmp_path), s.build.blocks, doc="main.tex")
    assert len(q) == 1 and q[0]["block"] is None


def test_the_drain_presents_a_document_comment_as_document_work(tmp_path):
    d, bid, b = setup(tmp_path)
    chat.append(paths.comments(d),
                {"id": "c-0002", "kind": "comment", "block": "", "doc": "main.tex",
                 "quote": "", "body": "Check the tenses across the results section.",
                 "author": "bb"})
    items = drain.collect(d)
    doc_items = [i for i in items if i.chat_id == "c-0002"]
    assert doc_items, "a document comment must never be silently dropped"
    it = doc_items[0]
    assert "document" in (it.note or "").lower()
    assert "gone" not in (it.note or "").lower()
    assert it.editable is False
    text = drain.as_text(items)
    assert "Check the tenses" in text


# ------------------------------------------------------------------- to-dos
#
# The rail's to-do list was rendered from a blob field nothing ever wrote, so
# it read "0 of 0" forever and there was no way to add one. They live in
# comments.jsonl like everything else the page and the agent share: a `todo`
# record to create, a `todo-state` record to toggle, append-only, scoped to
# the document.


def test_a_todo_is_stored_and_folded_into_the_blob(tmp_path):
    from manuscriptor.server.app import Session

    (tmp_path / "main.tex").write_text(DOC, encoding="utf-8")
    s = Session(tmp_path)
    frame = asyncio.run(s.on_todo("Recheck the balance table before circulating"))
    assert frame["type"] == "todos"
    assert frame["todos"][0]["text"].startswith("Recheck")
    assert frame["todos"][0]["done"] is False
    rec = chat.read_records(paths.comments(tmp_path))[-1]
    assert rec["kind"] == "todo" and rec["doc"] == "main.tex"

    fresh = build_mod.build(tmp_path)
    assert fresh.blob["todos"][0]["text"].startswith("Recheck")


def test_toggling_a_todo_appends_rather_than_rewrites(tmp_path):
    from manuscriptor.server.app import Session

    (tmp_path / "main.tex").write_text(DOC, encoding="utf-8")
    s = Session(tmp_path)
    tid = asyncio.run(s.on_todo("check tenses"))["todos"][0]["id"]
    frame = asyncio.run(s.on_todo_toggle(tid, True))
    assert frame["todos"][0]["done"] is True
    frame = asyncio.run(s.on_todo_toggle(tid, False))
    assert frame["todos"][0]["done"] is False
    kinds = [r["kind"] for r in chat.read_records(paths.comments(tmp_path))]
    assert kinds.count("todo") == 1 and kinds.count("todo-state") == 2


def test_todos_are_scoped_to_their_document(tmp_path):
    from manuscriptor.server.app import Session

    (tmp_path / "main.tex").write_text(DOC, encoding="utf-8")
    (tmp_path / "appendix.tex").write_text(APPENDIX, encoding="utf-8")
    s = Session(tmp_path, main="appendix.tex")
    asyncio.run(s.on_todo("appendix-only chore"))
    assert build_mod.build(tmp_path).blob["todos"] == []
    assert build_mod.build(tmp_path, main="appendix.tex").blob["todos"][0]["text"] == "appendix-only chore"


def test_a_read_only_page_cannot_write_todos(tmp_path):
    from manuscriptor.server.app import Session

    (tmp_path / "main.tex").write_text(DOC, encoding="utf-8")
    s = Session(tmp_path, read_only=True)
    frame = asyncio.run(s.on_todo("nope"))
    assert frame["type"] == "held"
    assert not paths.comments(tmp_path).exists()


# ---------------------------------------------------- checks, and their findings
#
# A check (preflight, proofread, revision audit) is asked for from a toolbar
# dropdown, travels as a document-level comment carrying a `check` field, and
# its findings come back as comments in a `review` state: pinned and readable
# at once, but never presented to the drain as work, or a --with-agent session
# would start working the instructions it had just written itself. The author
# triages: dismissing marks the finding done; asking for the fix is an
# ordinary new comment.


def test_a_check_request_carries_its_skill(tmp_path):
    from manuscriptor.server.app import Session

    (tmp_path / "main.tex").write_text(DOC, encoding="utf-8")
    s = Session(tmp_path)
    asyncio.run(s.on_chat("", "Run the preflight on this document.",
                          check="consistency-check"))
    rec = chat.read_records(paths.comments(tmp_path))[-1]
    assert rec["check"] == "consistency-check"
    items = drain.collect(tmp_path)
    assert items[0].check == "consistency-check"
    assert "consistency-check" in drain.as_text(items)


def test_a_finding_is_pinned_but_never_drained(tmp_path):
    d, bid, b = setup(tmp_path)
    rec = drain.comment(
        d, quote=b.by_id[bid].source_text[:120],
        body="This claim needs a within-sample benchmark.",
        author="proofreader", check="consistency-check", doc="main.tex",
        review=True,
    )
    assert rec is not None
    # Pinned: the queue view lists it, in its own state.
    q = build_mod.queue_view(paths.comments(d), b.blocks, doc="main.tex")
    states = {e["id"]: e["state"] for e in q}
    assert states[rec["id"]] == "review"
    # Anchored by its quote, like an imported referee comment.
    anchored = [e for e in q if e["id"] == rec["id"]][0]
    assert anchored["block"] == bid
    # Never drained: the finding is not pending work.
    assert rec["id"] not in [i.chat_id for i in drain.collect(d)]


WRAPPED_DOC = r"""\documentclass{article}
\begin{document}
\section{Results}
A first paragraph, present so the finding lands on the second.

This division matters for the controls
available to a program deploying a DSP.
Generation differences are fixed before the encounter
and can be corrected by one-time review.
\end{document}
"""


def test_a_finding_anchors_when_its_quote_is_a_sentence_not_source_bytes(tmp_path):
    # A quote comes from a person or a rendered page, so its words are joined by
    # single spaces. The source they came from is hard wrapped one clause per
    # line, which is how dsp-bias and most of his manuscripts are written. The
    # two strings say the same sentence and differ only in whitespace, so the
    # anchor must survive that. Matching raw source bytes drops the finding on
    # any manuscript whose author presses return mid-sentence.
    d, _bid, b = setup(tmp_path, WRAPPED_DOC)
    target = [x.id for x in b.blocks if x.kind == "paragraph"][1]
    quote = ("This division matters for the controls available to a program "
             "deploying a DSP.")
    assert "\n" in b.by_id[target].source_text  # the fixture really is wrapped
    rec = drain.comment(d, quote=quote, body="Answer the older-tools objection.",
                        author="proofreader", doc="main.tex", review=True)
    assert rec is not None
    q = build_mod.queue_view(paths.comments(d), b.blocks, doc="main.tex")
    anchored = [e for e in q if e["id"] == rec["id"]][0]
    assert anchored["block"] == target


def test_two_findings_land_on_their_own_paragraphs(tmp_path):
    # A check files findings with no block id, so every one of them is keyed by
    # the same absent id. Re-anchoring them as a group gives them all the first
    # one's quote and stacks a whole review on one paragraph. Each finding is
    # placed by ITS OWN quote or it is not placed at all.
    d, _bid, b = setup(tmp_path, WRAPPED_DOC)
    paras = [x.id for x in b.blocks if x.kind == "paragraph"]
    first, second = paras[0], paras[1]
    q1 = flatten_ws(b.by_id[first].source_text)[:70]
    q2 = flatten_ws(b.by_id[second].source_text)[:70]
    r1 = drain.comment(d, quote=q1, body="On the first.", author="proofreader",
                       doc="main.tex", review=True)
    r2 = drain.comment(d, quote=q2, body="On the second.", author="proofreader",
                       doc="main.tex", review=True)
    q = build_mod.queue_view(paths.comments(d), b.blocks, doc="main.tex")
    at = {e["id"]: e["block"] for e in q}
    assert at[r1["id"]] == first
    assert at[r2["id"]] == second


def test_a_finding_is_deduped_against_the_open_one(tmp_path):
    d, bid, b = setup(tmp_path)
    quote = b.by_id[bid].source_text[:120]
    first = drain.comment(d, quote=quote, body="Same finding.",
                          author="proofreader", doc="main.tex", review=True)
    again = drain.comment(d, quote=quote, body="Same finding.",
                          author="proofreader", doc="main.tex", review=True)
    assert first is not None and again is None
    # Dismissed and re-found is a NEW finding, not a duplicate.
    drain.mark(d, first["id"], "done")
    third = drain.comment(d, quote=quote, body="Same finding.",
                          author="proofreader", doc="main.tex", review=True)
    assert third is not None


def test_the_author_dismisses_a_finding(tmp_path):
    from manuscriptor.server.app import Session

    d, bid, b = setup(tmp_path)
    rec = drain.comment(d, quote=b.by_id[bid].source_text[:120],
                        body="Nitpick.", author="proofreader",
                        doc="main.tex", review=True)
    s = Session(d)
    frame = asyncio.run(s.on_dismiss(rec["id"]))
    assert frame["type"] == "state" and frame["state"] == "done"
    states = {c.id: c.state for c in chat.read_chats(paths.comments(d))}
    assert states[rec["id"]] == "done"


def test_a_read_only_page_cannot_dismiss(tmp_path):
    from manuscriptor.server.app import Session

    (tmp_path / "main.tex").write_text(DOC, encoding="utf-8")
    s = Session(tmp_path, read_only=True)
    frame = asyncio.run(s.on_dismiss("c-0001"))
    assert frame["type"] == "held"


def test_the_agent_is_the_default(tmp_path, monkeypatch):
    # The full workflow is: open a manuscript, leave a comment, have it
    # answered. That only holds if serve runs the drain without being asked.
    import manuscriptor.server.app as app_mod

    (tmp_path / "main.tex").write_text(DOC, encoding="utf-8")
    started = []

    def stub(d, *, lock=None):
        # `lock` is the queue's claim: taken before the drain is spawned and
        # handed down to it, one drain per comment queue across processes. Its
        # state is recorded here rather than asserted later, because `serve`
        # releases on the way out and the object would then read as unheld.
        started.append((d, lock is not None and lock.held))
        return None, None

    monkeypatch.setattr(cli, "start_agent", stub)
    monkeypatch.setattr(app_mod, "serve", lambda d, **kw: None)

    cli.main(["serve", str(tmp_path), "--no-window"])
    assert started, "serve without flags must run the agent; that is the workflow"
    assert started[0][1], \
        "the drain must be handed the queue's claim, or two servers drain one queue"

    started.clear()
    cli.main(["serve", str(tmp_path), "--no-window", "--no-agent"])
    assert not started

    # Read-only implies no agent, silently: wanting to READ a paper is not a
    # contradiction to be errored at, it is the other mode.
    started.clear()
    cli.main(["serve", str(tmp_path), "--no-window", "--read-only"])
    assert not started


def test_a_machine_without_claude_still_serves(tmp_path, monkeypatch):
    # The default must degrade: a missing claude CLI downgrades to serving
    # without the agent, with a warning. Only the EXPLICIT --with-agent is a
    # hard error, because then the author asked for it by name.
    import manuscriptor.server.app as app_mod

    (tmp_path / "main.tex").write_text(DOC, encoding="utf-8")
    monkeypatch.setattr(cli.shutil, "which",
                        lambda name: None if name == "claude" else "/usr/bin/" + name)
    monkeypatch.setattr(app_mod, "serve", lambda d, **kw: None)
    cli.main(["serve", str(tmp_path), "--no-window"])  # must not raise


# --------------------------------------------------------- figures that change
#
# The agent answers a figure comment by editing the producing script and
# regenerating the PDF, and the page kept showing the old raster: the watcher
# only knew source suffixes, so a changed figure triggered nothing. Watched
# live on dsp-bias, minutes after the first real figure regeneration.

MINI_PDF = (b"%PDF-1.4\n"
            b"1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n"
            b"2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj\n"
            b"3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 24 24] >> endobj\n"
            b"trailer << /Root 1 0 R >>\n%%EOF")

FIG_DOC = r"""\documentclass{article}
\begin{document}
\section{Results}
Prose before the figure, long enough to be its own paragraph in the map.

\begin{figure}
\includegraphics{outputs/fig.pdf}
\caption{The figure}
\end{figure}
\end{document}
"""


def test_the_watcher_notices_figure_files():
    from manuscriptor.server import watch

    assert ".pdf" in watch.WATCHED
    assert ".png" in watch.WATCHED
    # And our own rasterized output can never retrigger a rebuild.
    assert "build" in watch.IGNORED_DIRS


def test_a_regenerated_figure_reaches_the_page(tmp_path):
    from manuscriptor.server.app import Session

    (tmp_path / "outputs").mkdir()
    (tmp_path / "outputs" / "fig.pdf").write_bytes(MINI_PDF)
    (tmp_path / "main.tex").write_text(FIG_DOC, encoding="utf-8")
    s = Session(tmp_path)
    # `fig.pdf.png`: the raster keeps the PDF's own suffix so it can never
    # share a cache path with a same-stem PNG the asset copier mirrors.
    png = paths.cache(tmp_path) / "outputs" / "fig.pdf.png"
    assert png.exists(), "the figure never rasterized at all"
    first = png.stat().st_mtime_ns

    time.sleep(0.02)
    (tmp_path / "outputs" / "fig.pdf").write_bytes(MINI_PDF + b"\n% regenerated")
    os.utime(tmp_path / "outputs" / "fig.pdf")
    sent = collect_frames(s)
    asyncio.run(s.on_assets_change())

    assert png.stat().st_mtime_ns > first, "the raster is still the old figure"
    frames = [m for m in sent if m["type"] == "assets"]
    assert frames and frames[-1].get("v"), "the page was never told to refetch"


# ------------------------------------------------------- the persistent agent
#
# A cold `claude -p` per wake cost ~90 seconds of boot before the first
# visible state. Verified 2026-07-23 that a print-mode session survives
# parking a background task and is re-woken when it finishes, so the loop is
# now ONE persistent session that parks `proc --wait` and wakes per comment;
# the shell only restarts it when it exits (fresh after ~20 wakes, or a
# crash).


def test_the_agent_session_is_persistent_and_parks_on_the_log(tmp_path):
    script = cli.agent_loop_script(tmp_path, claude="/u/claude", manuscriptor="/u/ms")
    assert "while :" in script, "no restart loop; a crashed session would end the workflow"
    assert "/u/claude" in script and "/u/ms" in script
    assert "--wait" in script, "nothing parks on the log"
    assert "BACKGROUND" in script, "the park must be a background task, or the turn never ends"
    assert "working" in script, "marking working first is the latency fix"


def test_the_agent_reaches_the_producing_scripts(tmp_path):
    # analysis/ lives beside paper/ in the repo, and a session scoped to
    # paper/ was blocked from the very script a figure comment names. The
    # repo root rides along as an added directory.
    (tmp_path / ".git").mkdir()
    ms = tmp_path / "paper"
    ms.mkdir()
    (ms / "main.tex").write_text(DOC, encoding="utf-8")
    import subprocess as sp
    sp.run(["git", "-C", str(tmp_path), "init", "-q"], capture_output=True)
    root = cli.repo_root(ms)
    assert root is not None and root.name == tmp_path.name
    script = cli.agent_loop_script(ms, claude="c", manuscriptor="m",
                                   add_dirs=[str(root)])
    assert "--add-dir" in script and str(root) in script


def test_a_manuscript_outside_any_repo_adds_nothing(tmp_path):
    ms = tmp_path / "loose"
    ms.mkdir()
    script = cli.agent_loop_script(ms, claude="c", manuscriptor="m", add_dirs=[])
    assert "--add-dir" not in script


def test_the_park_returns_at_once_when_work_is_already_pending(tmp_path):
    # The race the persistent session exposed: a comment landing WHILE the
    # session works is inside the park's size baseline, so "wait for the log
    # to grow" never fires for it and it sits unworked until a third record
    # arrives. The park's real question is "is there work", so a non-empty
    # queue returns immediately.
    d, bid, b = setup(tmp_path)                  # c-0001 pending
    t0 = time.monotonic()
    assert drain.wait(tmp_path, timeout=10) is True
    assert time.monotonic() - t0 < 3, "the park blocked despite pending work"


def test_the_park_still_blocks_on_a_quiet_queue(tmp_path):
    (tmp_path / "main.tex").write_text(DOC, encoding="utf-8")
    build_mod.build(tmp_path)
    t0 = time.monotonic()
    assert drain.wait(tmp_path, timeout=1.2) is False
    assert time.monotonic() - t0 >= 1.0


# ------------------------------------------- naming a block the author reads
#
# dsp-bias, 2026-07-27. Two tables live in one `\input`ed file whose nearest
# preceding heading is a `\paragraph{Socioeconomic status.}` fifty lines above,
# so both inherited those words. The ticker names an entry by its section, so
# an edit landing on the second table announced itself in language identical to
# the first table -- which was open in the inspector, still queued, and had not
# been touched. The author read the ticker and the queue as contradicting each
# other about one item. They were talking about two.

TWO_TABLES = r"""\documentclass{article}
\begin{document}
\section{Results}
\paragraph{Socioeconomic status.}
Education mapped to occupation almost deterministically.

\input{outputs/tab_results}
\end{document}
"""

TABLES = r"""\begin{table}[h!]
\caption{Case generation: demographic variation. Cells report means.}
\begin{tabular}{ll}
A & B \\
\end{tabular}
\end{table}

\begin{table}[h!]
\caption{Demographic variation in conversations. Cells report means.}
\begin{tabular}{ll}
C & D \\
\end{tabular}
\end{table}
"""


def two_tables(tmp_path: Path):
    (tmp_path / "main.tex").write_text(TWO_TABLES, encoding="utf-8")
    (tmp_path / "outputs").mkdir()
    (tmp_path / "outputs" / "tab_results.tex").write_text(TABLES, encoding="utf-8")
    b = build_mod.build(tmp_path)
    tables = [x for x in b.blocks if x.file.name == "tab_results.tex"]
    assert len(tables) == 2, [x.source_text[:40] for x in tables]
    return b, tables


def test_two_exhibits_under_one_heading_are_not_called_the_same_thing(tmp_path):
    b, tables = two_tables(tmp_path)
    assert [x.parent_heading for x in tables] == \
        ["Socioeconomic status.", "Socioeconomic status."], "the heading really is shared"

    for i, (t, cid) in enumerate(zip(tables, ("c-0001", "c-0002"))):
        chat.append(
            paths.comments(tmp_path),
            {"id": cid, "kind": "comment", "block": t.id, "file": str(t.file),
             "lines": [t.line_start, t.line_end], "quote": t.source_text[:120],
             "body": "add the r2", "author": "bb", "ts": ago_iso(300 - i)},
        )
    chat.append(paths.comments(tmp_path),
                {"id": "c-0002", "kind": "state", "state": "working", "ts": ago_iso(10)})

    q = build_mod.queue_view(paths.comments(tmp_path), b.blocks)
    names = [e["section"] for e in q]
    assert names == ["Case generation: demographic variation.",
                     "Demographic variation in conversations."], names

    t = build_mod.ticker_view(paths.comments(tmp_path), b.blocks)
    assert [e["section"] for e in t] == ["Demographic variation in conversations."]


def test_the_block_record_carries_the_name_the_page_shows(tmp_path):
    b, tables = two_tables(tmp_path)
    recs = [b.blob["blocks"][t.id] for t in tables]
    assert [r["label"] for r in recs] == ["Case generation: demographic variation.",
                                          "Demographic variation in conversations."]
    assert [r["parent_heading"] for r in recs] == ["Socioeconomic status."] * 2, \
        "the heading is still reported; it is a fact about where the block sits"


def test_the_agent_is_told_which_table_it_is(tmp_path):
    """The work item's section is what the session repeats back in its replies."""
    b, tables = two_tables(tmp_path)
    t = tables[1]
    chat.append(
        paths.comments(tmp_path),
        {"id": "c-0001", "kind": "comment", "block": t.id, "file": str(t.file),
         "lines": [t.line_start, t.line_end], "quote": t.source_text[:120],
         "body": "add the r2", "author": "bb", "ts": ago_iso(60)},
    )
    (item,) = drain.collect(tmp_path)
    assert item.section == "Demographic variation in conversations."
