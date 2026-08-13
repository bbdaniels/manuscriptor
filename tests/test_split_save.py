"""Splitting a paragraph in the editor, one blank line at a time.

THE INCIDENT, 2026-08-13, on a live serve of qutub-ayush. The closing passage of
the introduction -- four paragraphs -- was in `main.tex` FOUR TIMES, and the four
copies were identical except that each one down the file had one fewer blank line
in it than the one above:

    copy 1   P1 · P2 · P3 · P4      four paragraphs
    copy 2   P1 · P2 P3 · P4        three
    copy 3   P1 P2 P3 · P4          two
    copy 4   P1 P2 P3 P4            one

That monotone ladder is the whole diagnosis. It is not something a hand edit or
an agent's string replacement can produce; it is one passage written four times,
by something that saw it as one block and then as two, three, four. The drain
that afternoon issued no write command at all -- its stream has `proc`, `state`
and `reply` and nothing else -- and every copy already existed 29 seconds before
its first edit. The writer was the author's own editor box.

WHAT THE AUTHOR DID, WHICH IS THE ORDINARY THING. He typed a new passage into one
paragraph's box, then formatted it: a blank line before the last sentence, save,
a blank line before the one above it, save, and so on. Each save split the block
he was editing into two. The page renames his box onto the FIRST of them, which
is right, and leaves the text in it alone, which is also right -- the watcher may
not replace the textarea under a live cursor. So from the second save onward his
box holds a passage that the file now keeps as several blocks, and the server
wrote it over the first of those and left the rest of them standing underneath.
One duplicate per save, each with one more blank line than the last: the ladder.

THE FIX IS IN `splice`, because the question is which bytes a save owns, and that
is the only module allowed to answer it. `following` names the blocks after the
target, and a save that still carries them replaces them too.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from tests import pagedriver
from manuscriptor.server import app
from manuscriptor.server import build as build_mod
from manuscriptor.server.app import _diff
from manuscriptor.source import splice as splice_mod


P1 = ("These findings contribute to three strands of the literature on health systems in "
      "low-income countries and quality improvement strategies within them.\n"
      "To begin with, it provides some of the first systematic evidence on quality of care "
      "among Ayush providers in India, despite the fact that they are very numerous.")
P2 = ("Second, it provides sobering evidence that fixing the referral chain from Ayush to "
      "biomedical providers, even for a high-priority condition, is a very difficult task.")
P3 = ("Third, it raises serious questions about the overall strategy of continuing with a "
      "medical system where providers with very different training treat the same patients.")
P4 = ("The remainder of our paper is as follows. Section II details the Context and Data. "
      "Section III discusses the intervention and the empirical strategy we adopt.")

HEAD = (r"\documentclass{article}" "\n" r"\begin{document}" "\n"
        r"\section{Introduction}" "\n\n"
        "We report three main findings, each of which is developed in the sections below.\n\n")
TAIL = ("\n\n\n" r"\section{Context and Data}" "\n\n"
        "The context section opens here and is no part of the passage under test.\n"
        r"\end{document}" "\n")

# What the box holds at each save: the passage, with one more blank line in it
# each time. V[0] is what he first wrote and what the file starts with.
V = [
    "\n".join([P1, P2, P3, P4]),
    "\n".join([P1, P2, P3]) + "\n\n" + P4,
    "\n".join([P1, P2]) + "\n\n" + P3 + "\n\n" + P4,
    P1 + "\n\n" + P2 + "\n\n" + P3 + "\n\n" + P4,
]


def served(tmp_path: Path, body: str) -> app.Session:
    (tmp_path / "main.tex").write_text(body, encoding="utf-8")
    return app.Session(tmp_path)


def copies(tmp_path: Path) -> dict:
    text = (tmp_path / "main.tex").read_text(encoding="utf-8")
    return {"P1": text.count("These findings contribute"),
            "P2": text.count("Second, it provides sobering"),
            "P3": text.count("Third, it raises serious"),
            "P4": text.count("The remainder of our paper")}


def block_with(session, words: str):
    return next(b for b in session.build.blocks if words in b.source_text)


def after(session, block):
    """The blocks following `block` in its own file, in document order."""
    same = [b for b in session.build.blocks if b.file == block.file]
    return tuple(same[same.index(block) + 1:])


# ------------------------------------------------------ the server's save path


def test_a_save_that_still_carries_the_next_block_does_not_leave_it_twice(tmp_path):
    """The second save of the incident, in one call.

    The file holds the passage as two blocks -- everything but the last
    paragraph, then the last paragraph. The box holds all of it, because that is
    what the author typed and nothing has taken it away from him. Writing it must
    leave the manuscript saying the passage once.
    """
    session = served(tmp_path, HEAD + V[1] + TAIL)
    head = block_with(session, "These findings contribute")
    assert block_with(session, "The remainder of our paper").id != head.id, \
        "the fixture must hold the passage as two blocks, or it tests nothing"

    frame = asyncio.run(session.on_edit(head.id, V[2]))
    assert frame["type"] == "saved", frame
    assert copies(tmp_path) == {"P1": 1, "P2": 1, "P3": 1, "P4": 1}, \
        "the save duplicated the paragraphs it still carried"


def test_the_whole_ladder_from_one_block_to_four(tmp_path):
    """Three saves, exactly the incident's sequence, on the server's own path.

    The box is followed the way the page follows it: onto the block the last save
    renamed it to, holding the text the author has in front of him.
    """
    session = served(tmp_path, HEAD + V[0] + TAIL)
    for step in (1, 2, 3):
        head = block_with(session, "These findings contribute")
        frame = asyncio.run(session.on_edit(head.id, V[step]))
        assert frame["type"] == "saved", (step, frame)
        session.build = build_mod.build(tmp_path)
    assert copies(tmp_path) == {"P1": 1, "P2": 1, "P3": 1, "P4": 1}, \
        "the passage was written more than once"
    assert (tmp_path / "main.tex").read_text(encoding="utf-8") == HEAD + V[3] + TAIL


def test_a_paragraph_the_author_adds_is_inserted_and_eats_nothing(tmp_path):
    """The other half of the rule, and the one that keeps it honest.

    A box whose new last paragraph is NOT the block below it is adding a
    paragraph, not re-carrying one. The block below has to survive.
    """
    session = served(tmp_path, HEAD + V[3] + TAIL)
    head = block_with(session, "These findings contribute")
    added = "A fourth strand, newly written, which has never been in this manuscript before."
    frame = asyncio.run(session.on_edit(head.id, P1 + "\n\n" + added))
    assert frame["type"] == "saved", frame
    text = (tmp_path / "main.tex").read_text(encoding="utf-8")
    assert text.count(added) == 1
    assert copies(tmp_path) == {"P1": 1, "P2": 1, "P3": 1, "P4": 1}, \
        "adding a paragraph swallowed or duplicated the ones after it"
    assert text.index(added) < text.index(P2), "the new paragraph landed in the wrong place"


def test_a_tail_the_author_also_edited_is_still_carried(tmp_path):
    """He does not only add blank lines: he types in the tail as well.

    The passage below the split is the same passage with a word changed, so an
    exact comparison misses it -- and missing it is a duplicate, which is the
    defect. It is the same paragraph and it is replaced, not repeated.
    """
    session = served(tmp_path, HEAD + V[1] + TAIL)
    head = block_with(session, "These findings contribute")
    edited = V[2].replace("Section II details", "Section II describes")
    frame = asyncio.run(session.on_edit(head.id, edited))
    assert frame["type"] == "saved", frame
    text = (tmp_path / "main.tex").read_text(encoding="utf-8")
    assert copies(tmp_path) == {"P1": 1, "P2": 1, "P3": 1, "P4": 1}, \
        "an edited tail was written beside the old one instead of over it"
    assert "Section II describes" in text and "Section II details" not in text


def test_a_block_further_down_the_file_is_never_reached(tmp_path):
    """Carrying stops at the first block the save does not still hold.

    Otherwise a passage that happens to end on the words of some later paragraph
    would take everything between them with it.
    """
    session = served(tmp_path, HEAD + V[3] + TAIL)
    head = block_with(session, "These findings contribute")
    # The box ends on the LAST paragraph of the passage, skipping the two in
    # between: nothing here may be carried, because P2 no longer matches.
    frame = asyncio.run(session.on_edit(head.id, P1 + "\n\n" + P4))
    assert frame["type"] == "saved", frame
    assert copies(tmp_path)["P2"] == 1 and copies(tmp_path)["P3"] == 1, \
        "the save reached past a block it was not carrying and deleted it"


# ---------------------------------------------------------------- the splice


def test_splice_without_following_blocks_is_unchanged(tmp_path):
    """The agent's path, which hands one block and nothing else.

    `insert.apply` and every worker splice one block. Widening the unit for the
    author's editor box must not widen it for them.
    """
    session = served(tmp_path, HEAD + V[3] + TAIL)
    head = block_with(session, "These findings contribute")
    splice_mod.splice(head, P1 + " Rewritten.", root=tmp_path)
    assert copies(tmp_path) == {"P1": 1, "P2": 1, "P3": 1, "P4": 1}
    assert "Rewritten." in (tmp_path / "main.tex").read_text(encoding="utf-8")


def test_splice_refuses_a_stale_block_even_when_following_blocks_are_given(tmp_path):
    """The staleness guard is not weakened by the new argument."""
    session = served(tmp_path, HEAD + V[3] + TAIL)
    head = block_with(session, "These findings contribute")
    (tmp_path / "main.tex").write_text(
        HEAD + "Something else entirely, written by somebody else.\n\n" + P4 + TAIL,
        encoding="utf-8")
    with pytest.raises(splice_mod.StaleBlock):
        splice_mod.splice(head, "anything", root=tmp_path, following=after(session, head))


# ------------------------------------------------------------------ the page

WHY = pagedriver.missing()


@pytest.mark.skipif(bool(WHY), reason=str(WHY))
def test_the_page_itself_splits_a_paragraph_without_duplicating_it(tmp_path):
    """The same ladder, but the source is the REAL editor and the real frames.

    Every edit written here is one the page sent, and every frame the page is
    given came out of the server. The page is loaded once and lived once, the way
    the author's is: `drive` is re-entered per round only because a jsdom window
    cannot be paused, and each round replays the rounds before it before doing
    its own.
    """
    session = served(tmp_path, HEAD + V[0] + TAIL)
    page = pagedriver.page(session)
    start = block_with(session, "These findings contribute")

    frames: list[dict] = []
    for round_no in (1, 2, 3):
        steps = ["select:" + start.id]
        for i in range(1, round_no + 1):
            if i > 1:
                steps.append("frames:2")
            steps += ["type:" + V[i], "blur"]
        out = pagedriver.drive(page, frames, tmp_path=tmp_path, steps=steps)
        sent = [json.loads(s) for s in out["sent"]]
        last = [m for m in sent if m.get("type") == "edit"][-1]
        assert last["source"] == V[round_no], "the page did not send what was typed"

        before = session.build
        frame = asyncio.run(session.on_edit(last["block"], last["source"]))
        assert frame["type"] == "saved", (round_no, frame)
        session.build = build_mod.build(tmp_path)
        frames += [frame, _diff(before, session.build)]

    assert copies(tmp_path) == {"P1": 1, "P2": 1, "P3": 1, "P4": 1}, \
        "the page's own saves wrote the passage more than once"
