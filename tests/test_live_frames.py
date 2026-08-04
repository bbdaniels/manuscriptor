"""The live push path: what the server sends after a rebuild, and what the page
does when it arrives.

WHY THIS FILE EXISTS. Every other client-side test in this suite calls one pure
function with an object typed out in the test. That proved the seed path -- what
a page holds when it opens -- and proved nothing about the push path, which is
`Session.broadcast` -> the socket -> `handle` -> a renderer -> the DOM. Three
bugs were living in that gap at once while 975 tests passed, because no test
anywhere constructed a frame the server had built:

  * a `\\citep` added to one paragraph patched that paragraph and left the
    bibliography below it as it was;
  * a change to `references.bib` alone produced NO FRAME AT ALL -- the file is
    watched, the rebuild is right, and the whole of it was discarded in silence;
  * the citation count in the header, and the colour of every citation
    underline, froze at page load and never moved again;
  * a live ticker line led with the paragraph's own first words instead of with
    what the author had asked for, and two requests on one paragraph rendered as
    the same line printed twice.

They are one bug. The BOOT PATH IS CORRECT AND THE PUSH PATH IS WRONG, in four
places, because nothing ever drove the push path.

So the rule here, and it is the point of the file: A TEST MAY NOT WRITE A FRAME.
Every frame below comes out of the server -- `_diff`, or whatever `broadcast`
was actually handed -- and goes into the real `handle` inside the real page. A
literal typed here would be the same blindness in a new file.

`tests/pagedriver.py` is the harness; it needs node and jsdom (`cd tests/js &&
npm install`).
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from tests import pagedriver
from manuscriptor.server import app, chat, drain, paths
from manuscriptor.server import build as build_mod

WHY = pagedriver.missing()
pytestmark = pytest.mark.skipif(bool(WHY), reason=str(WHY))


BIB = """@article{smith2020,
  author = {Smith, Ada},
  title = {On contracts},
  journal = {Journal of Testing},
  year = {2020},
}
@article{jones2021,
  author = {Jones, Bo},
  title = {On payment},
  journal = {Review of Fixtures},
  year = {2021},
}
"""

CITED = r"""\documentclass{article}
\begin{document}
First paragraph, entirely unremarkable, citing \citep{smith2020} and long enough to be a block.

Second paragraph, which is the one the author is about to edit in the browser today.

Third paragraph, which must not be disturbed by anything happening above it at all.

\bibliographystyle{plain}
\bibliography{references}
\end{document}
"""

COMMENTED = r"""\documentclass{article}
\begin{document}
\section{Results}
The treatment raised screening rates substantially across all three cohorts followed.

We interpret this as evidence that the contract itself, rather than the payment, drove it.
\end{document}
"""


def served(tmp_path: Path, body: str, *, bib: str | None = None):
    """A session on a real manuscript, and the page the server would serve.

    The page is captured BEFORE the change under test, which is what makes the
    rest of the test the live path rather than a reload: the browser is holding
    the old document and the frames have to carry it to the new one.
    """
    (tmp_path / "main.tex").write_text(body, encoding="utf-8")
    if bib is not None:
        (tmp_path / "references.bib").write_text(bib, encoding="utf-8")
    session = app.Session(tmp_path)
    return session, pagedriver.page(session)


def rewrite(tmp_path: Path, old: str, new: str) -> None:
    p = tmp_path / "main.tex"
    src = p.read_text(encoding="utf-8")
    assert old in src, "the fixture moved"
    p.write_text(src.replace(old, new), encoding="utf-8")


def pushed(session) -> list[dict]:
    """Everything `on_change` broadcast, in order. An empty list is a finding."""
    async def go():
        with pagedriver.record(session) as sent:
            await session.on_change()
        return sent
    return asyncio.run(go())


# ------------------------------------------------------- the render, not the source


def test_a_new_citation_reaches_the_bibliography_on_the_page(tmp_path):
    """Adding a `\\citep` to one paragraph adds an entry to the bibliography.

    The bibliography's own LaTeX is `\\bibliography{references}` and it did not
    move, so a diff that asks "did this block's SOURCE change" says no and the
    entry never arrives. The author sees his new citation inline and an
    unchanged reference list underneath it.
    """
    session, page = served(tmp_path, CITED, bib=BIB)
    rewrite(tmp_path, "about to edit in the browser today.",
            "about to edit in the browser today, now citing \\citep{jones2021}.")
    frames = pushed(session)

    assert "Jones" in session.blob["html"], "the rebuild itself is not in doubt"
    out = pagedriver.drive(page, frames, tmp_path=tmp_path)
    assert "Jones" in (out["refsHtml"] or ""), \
        "the new source is missing from the bibliography the author is looking at"
    assert "Smith" in out["refsHtml"], "and the one that was already there stays"


def test_a_bib_only_change_is_pushed_at_all(tmp_path):
    """The author fixes a journal name in `references.bib` and nothing else.

    No block's LaTeX moved, so the diff is empty and the entire rebuild is
    discarded without a word. The `.bib` IS watched and the rebuild IS correct;
    the frame is what never happens.
    """
    session, page = served(tmp_path, CITED, bib=BIB)
    (tmp_path / "references.bib").write_text(
        BIB.replace("Journal of Testing", "Quarterly Journal of Fixtures"), encoding="utf-8")
    frames = pushed(session)

    assert "Quarterly Journal of Fixtures" in session.blob["html"], \
        "the rebuild itself is not in doubt"
    assert frames, "the rebuild was discarded in silence"
    out = pagedriver.drive(page, frames, tmp_path=tmp_path)
    assert "Quarterly Journal of Fixtures" in (out["refsHtml"] or "")


def test_a_resolved_reference_reaches_the_page(tmp_path):
    """The same failure, one input over: `\\ref` resolves from the `.aux`.

    A block's rendered text changes when the `.aux` beside it changes, with its
    LaTeX untouched. Nothing about this is specific to bibliographies, which is
    why the rule has to be about the render and not about any one input.
    """
    body = r"""\documentclass{article}
\begin{document}
\section{Results}\label{sec:results}

As set out in Section \ref{sec:results}, the treatment raised screening rates.
\end{document}
"""
    aux = tmp_path / "main.aux"
    aux.write_text("\\newlabel{sec:results}{{1}{1}}\n", encoding="utf-8")
    session, page = served(tmp_path, body)
    assert "Section 1" in session.blob["html"]

    aux.write_text("\\newlabel{sec:results}{{4}{9}}\n", encoding="utf-8")
    frames = pushed(session)
    assert "Section 4" in session.blob["html"], "the rebuild itself is not in doubt"
    assert frames, "the rebuild was discarded in silence"
    out = pagedriver.drive(page, frames, tmp_path=tmp_path)
    assert "Section 4" in out["docHtml"]


def test_a_no_op_rebuild_still_pushes_nothing(tmp_path):
    """The other half of the rule, and the one that keeps it honest.

    Comparing renders instead of sources is only a fix if a rebuild that changed
    nothing still says nothing. A diff that re-emits the whole document on every
    save would repaint under the author's cursor and cost more than the bug.
    """
    session, page = served(tmp_path, CITED, bib=BIB)
    assert pushed(session) == [], "nothing changed on disk, so nothing may be sent"
    # and again, because a first rebuild can differ from a second by cache alone
    assert pushed(session) == []


def test_every_byte_of_the_document_belongs_to_some_block(tmp_path):
    """The assumption the comparison rests on, held explicitly.

    A block's markup is its RUN -- its anchored element plus every unanchored
    element after it -- so everything from the first anchor to the end of the
    container belongs to some block, and there is nothing left over for a frame
    to fail to address. If the render pass ever starts emitting markup outside
    a run, the diff can see it move and cannot express it, and this says so
    before an author finds out by reading a stale page.
    """
    body = r"""\documentclass{article}
\title{A paper}
\author{Ada}
\begin{document}
\maketitle
\begin{abstract}
An abstract, which pandoc renders as several elements at once.
\end{abstract}
\section{Results}
A paragraph citing \citep{smith2020} and running on long enough to be a real block.

\begin{table}[h]\centering\caption{A table}\begin{tabular}{ll}a & b\end{tabular}\end{table}

\bibliographystyle{plain}
\bibliography{references}
\end{document}
"""
    session, _ = served(tmp_path, body, bib=BIB)
    _, remainder = app._rendered(session.build)
    assert remainder.strip() == "", f"markup no frame can reach: {remainder!r}"


def test_an_edit_the_render_does_not_show_still_reaches_the_source_editor(tmp_path):
    """The rule has to be about the render OR the source, not the render alone.

    They come apart in both directions. A `.bib` moves the render with the LaTeX
    untouched, which is what the tests above are about; a whitespace or
    comment-only edit moves the LaTeX and pandoc collapses the difference away.
    The block reads identically on the page and its source editor is now showing
    text that is not what is on disk -- so the author's next save is diffed
    against a stale copy of his own file.
    """
    session, page = served(tmp_path, CITED, bib=BIB)
    target = next(b for b in session.build.blocks
                  if "must not be disturbed" in b.source_text)
    rewrite(tmp_path, "must not be disturbed by", "must not be disturbed  by")
    frames = pushed(session)

    fresh = next(b for b in session.build.blocks
                 if "must not be disturbed" in b.source_text)
    assert fresh.id == target.id, "whitespace is normalized out of the id"
    assert fresh.source_text != target.source_text, "and not out of the source"

    out = pagedriver.drive(page, frames, source=[fresh.id], tmp_path=tmp_path)
    assert out["source"][fresh.id] == fresh.source_text, \
        "the editor is holding LaTeX that is no longer in the file"


def test_markup_no_block_covers_forces_a_full_redraw(tmp_path):
    """And if the premise above ever fails, the answer is not silence.

    Nothing outside a block run can be addressed by a patch, so a change out
    there can only be expressed as "redraw all of it". The alternative is the
    bug this whole file is about: a rebuild that is correct, a page that is
    stale, and not a word between them. Reached here by doctoring the rendered
    html rather than by waiting for a render pass to start emitting one, since
    the point is that the diff must cope whenever that happens.
    """
    session, _ = served(tmp_path, CITED, bib=BIB)
    before = session.build
    session.rebuild()
    after = session.build
    assert app._diff(before, after) is None, "nothing changed on disk"

    after.blob["html"] = '<p class="note">Set by the render pass, outside every block.</p>' \
        + after.blob["html"]
    patch = app._diff(before, after)
    assert patch is not None, "the document moved where no frame can reach"
    assert set(patch["blocks"]) == {b.id for b in after.blocks if b.id in patch["blocks"]}
    assert len(patch["blocks"]) == len(after.blocks), \
        "an unaddressable change is the one case that redraws everything"


def test_an_edit_still_patches_only_the_block_that_moved(tmp_path):
    """The narrowness the whole design rests on, held against the new rule."""
    session, page = served(tmp_path, CITED, bib=BIB)
    rewrite(tmp_path, "about to edit in the browser today.",
            "about to edit in the browser today, and now edited.")
    patch = pagedriver.one(pushed(session), "patch")
    assert len(patch["blocks"]) == 1, \
        "one paragraph moved; anything else is the diff re-emitting the document"


# ------------------------------------------------------------ the derived state


def test_the_citation_count_in_the_header_follows_a_rebuild(tmp_path):
    """`stats` is recomputed on every rebuild and broadcast by nothing.

    The header reads "N citations" off the blob the page was born with, so it
    is right once and then frozen for the life of the tab.
    """
    session, page = served(tmp_path, CITED, bib=BIB)
    assert session.blob["stats"]["cites"] == 1
    rewrite(tmp_path, "about to edit in the browser today.",
            "about to edit in the browser today, now citing \\citep{jones2021}.")
    frames = pushed(session)
    assert session.blob["stats"]["cites"] == 2, "the rebuild itself is not in doubt"

    out = pagedriver.drive(page, frames, tmp_path=tmp_path)
    assert "2 citations" in (out["meta"] or ""), \
        f"the header still says {out['meta']!r}"


def test_a_new_verdict_recolours_the_underlines_without_a_reload(tmp_path):
    """`cites` is recomputed on every rebuild and broadcast only when the
    evidence pass finishes, so a verdict arriving any other way never lands."""
    session, page = served(tmp_path, CITED, bib=BIB)
    out = pagedriver.drive(page, [], tmp_path=tmp_path)
    assert all("verbatim" not in c["cls"] for c in out["cites"])

    cache = paths.cache(tmp_path)
    (cache / "citations.json").write_text(
        json.dumps([{"cite_key": "smith2020", "title": "On contracts", "has_fulltext": True}]),
        encoding="utf-8")
    (cache / "evidence.json").write_text(
        json.dumps([{"cite_key": "smith2020",
                     "quotes": [{"text": "contracts raised screening", "status": "verbatim"}]}]),
        encoding="utf-8")
    # A source edit alongside it, so this is the ordinary rebuild and not a
    # special case the evidence route already covers.
    rewrite(tmp_path, "happening above it at all.", "happening above it at all today.")
    frames = pushed(session)
    assert session.blob["cites"]["smith2020"]["status"] == "verbatim"

    out = pagedriver.drive(page, frames, tmp_path=tmp_path)
    smith = next(c for c in out["cites"] if c["keys"] == "smith2020")
    assert "verbatim" in smith["cls"], f"the underline is still {smith['cls']!r}"


# ------------------------------------------------------------------ the ticker


def commented(tmp_path: Path):
    """A manuscript, a page, and two comments on one paragraph."""
    session, page = served(tmp_path, COMMENTED)
    para = [b for b in session.build.blocks if b.kind == "paragraph"][1]
    for cid, body in (("c-0001", "This overclaims. Soften it."),
                      ("c-0002", "Also check the citation here.")):
        chat.append(paths.comments(tmp_path), {
            "id": cid, "kind": "comment", "block": para.id, "file": str(para.file),
            "lines": [para.line_start, para.line_end], "quote": para.source_text[:120],
            "body": body, "author": "bb", "ts": chat.now()})
    return session, page


def worked(session, tmp_path: Path) -> list[dict]:
    async def go():
        with pagedriver.record(session) as sent:
            await session.on_log_change()
            drain.mark(tmp_path, "c-0001", "working")
            drain.mark(tmp_path, "c-0002", "working")
            await session.on_log_change()
        return sent
    return asyncio.run(go())


def test_a_live_ticker_line_leads_with_the_request(tmp_path):
    """A ticker line reports WORK, and the work is what was asked for.

    The seed does this correctly -- `ticker_view` carries `asked` -- and the
    live frame does not carry it at all, so every line the author watches
    arrive falls back to naming the block. Since the label became the
    paragraph's own first words, the line reads as the paragraph quoting
    itself, which is the symptom that was reported.
    """
    session, page = commented(tmp_path)
    frames = worked(session, tmp_path)
    out = pagedriver.drive(page, frames, tmp_path=tmp_path)

    lines = [l["text"] for l in out["tickerLines"]]
    assert lines, "the agent picked two comments up and the ticker is empty"
    assert any("Soften it" in l for l in lines), \
        f"no line says what was asked; they say {lines!r}"
    assert any("check the citation" in l for l in lines)


def test_two_requests_on_one_paragraph_are_two_distinguishable_lines(tmp_path):
    """The dedup half of the same fix, which is inert live for the same reason.

    `tickerKey` keys on the comment id so two requests on one paragraph are two
    rows. A live entry has no id -- the frame carries `msg.id` and the client
    drops it -- so the key cannot tell them apart and the author watches one
    line where two things are happening.
    """
    session, page = commented(tmp_path)
    frames = worked(session, tmp_path)
    out = pagedriver.drive(page, frames, tmp_path=tmp_path)

    ids = [e.get("id") for e in out["ticker"]]
    assert all(ids), f"a live ticker entry names no comment: {ids!r}"
    assert len(set(ids)) == 2, "two requests, two identities"
    lines = {l["text"] for l in out["tickerLines"]}
    assert len(lines) == 2, f"two requests rendered as {lines!r}"


def test_the_live_ticker_line_matches_the_one_the_seed_would_have_drawn(tmp_path):
    """The two paths must agree, because the author cannot tell which he is
    looking at. A reload turns one into the other."""
    session, page = commented(tmp_path)
    frames = worked(session, tmp_path)
    live = pagedriver.drive(page, frames, tmp_path=tmp_path)

    seed = build_mod.ticker_view(paths.comments(tmp_path), session.build.blocks,
                                 root=tmp_path, doc=session.doc)
    assert {e["asked"] for e in seed} == {e.get("asked") for e in live["ticker"]}


# ------------------------------------------------------- the editor's identity
#
# A <textarea> keeps its undo history in the ELEMENT. Rebuild the panel and the
# author's Cmd+Z has nothing left to walk back, so undo worked inside one
# uninterrupted burst of typing and nowhere else -- and the panel was rebuilt on
# a blur, on a tab switch, on a reselection, and whenever a deferred patch
# flushed. The element surviving is the whole repair; these say so.
#
# Every assertion below is on the ELEMENT rather than on undo itself, because
# jsdom has no editing history to press. What jsdom can settle is whether the
# element the author was typing in is the one the panel is still holding, and
# that is the thing that was wrong. Real undo was measured separately, in
# Chrome 151: a blur costs nothing, a replaced element costs everything.


def para(session, words: str):
    """The block whose source contains `words`, which is how a test names one
    without writing down a content-derived id that any edit would change."""
    return next(b for b in session.build.blocks if words in b.source_text)


def test_the_editor_survives_the_authors_own_blur(tmp_path):
    """He types, clicks away, and comes back to the same box.

    The blur saves, the save re-draws the panel, and the re-draw built a new
    textarea -- so a blur was enough to lose everything he had typed his way
    into being able to undo.
    """
    session, page = served(tmp_path, CITED, bib=BIB)
    b = para(session, "about to edit in the browser")
    out = pagedriver.drive(page, [], tmp_path=tmp_path, steps=[
        "select:" + b.id, "type:Second paragraph, rewritten by hand.", "blur",
    ])
    last = out["trail"][-1]
    assert last["present"], "the editor is gone after a blur"
    assert last["same"], "the blur replaced the editor, and its undo history with it"


def test_the_editor_survives_a_trip_to_the_chat_tab(tmp_path):
    """Reading the chat about a paragraph is not abandoning the edit."""
    session, page = served(tmp_path, CITED, bib=BIB)
    b = para(session, "about to edit in the browser")
    out = pagedriver.drive(page, [], tmp_path=tmp_path, steps=[
        "select:" + b.id, "type:Second paragraph, rewritten by hand.",
        "tab:1", "tab:0",
    ])
    away, back = out["trail"][-2], out["trail"][-1]
    assert not away["showing"], "the editor must not be on screen on the chat tab"
    assert back["showing"] and back["same"], \
        "coming back from the chat tab handed him a different box"


def test_the_editor_survives_being_selected_again(tmp_path):
    """Clicking the same paragraph twice is not a reason to lose anything."""
    session, page = served(tmp_path, CITED, bib=BIB)
    b = para(session, "about to edit in the browser")
    out = pagedriver.drive(page, [], tmp_path=tmp_path, steps=[
        "select:" + b.id, "type:Second paragraph, rewritten by hand.", "blur",
        "select:" + b.id,
    ])
    assert out["trail"][-1]["same"], "reselecting the paragraph rebuilt its editor"


def test_a_patch_on_the_block_he_is_typing_in_leaves_his_editor_alone(tmp_path):
    """The case the deferral was built for, driven with the server's own frame.

    A patch for the focused block is held back until the blur, and then the
    panel is rebuilt -- which is exactly where the editor used to be replaced.
    The deferral protected the caret and lost the history, so the author could
    undo his own typing right up until the moment anything else touched the
    paragraph.
    """
    session, page = served(tmp_path, CITED, bib=BIB)
    b = para(session, "about to edit in the browser")
    # Someone else -- a co-author, the drain -- changes the same paragraph on
    # disk while he has it open.
    rewrite(tmp_path, "about to edit in the browser today.",
            "about to edit in the browser today, and someone else got there first.")
    frames = pushed(session)
    assert pagedriver.of(frames, "patch"), "the rebuild itself is not in doubt"

    out = pagedriver.drive(page, frames, tmp_path=tmp_path, steps=[
        "select:" + b.id, "type:Second paragraph, as he is retyping it.",
        "frames", "blur",
    ])
    landed, after = out["trail"][-2], out["trail"][-1]
    assert landed["same"], "the patch replaced the editor under his cursor"
    assert after["same"], "flushing the deferred panel replaced it instead"
    assert after["value"] == "Second paragraph, as he is retyping it.", \
        "his unsaved text was overwritten by the patch"


def test_another_block_gets_its_own_editor(tmp_path):
    """Per-block means per-block, and it is not a nicety.

    Blink keeps ONE undo stack per frame, not one per box: with two editors in
    the document at once a second Cmd+Z pressed in the near one walks back the
    far one (measured, Chrome 151). So the editor is replaced outright when the
    selected block changes, and the old element leaves the document -- which is
    what makes its history unreachable rather than merely out of sight.
    """
    session, page = served(tmp_path, CITED, bib=BIB)
    first = para(session, "about to edit in the browser")
    second = para(session, "must not be disturbed")
    out = pagedriver.drive(page, [], tmp_path=tmp_path, steps=[
        "select:" + first.id, "type:The second paragraph, edited.", "blur",
        "select:" + second.id,
    ])
    last = out["trail"][-1]
    assert last["block"] == second.id, "the panel is showing the wrong block"
    assert not last["same"], \
        "the second block inherited the first block's editor, and its undo history"
    assert [t["count"] for t in out["trail"]] == [1, 1, 1, 1], \
        "the first block's editor is still in the document, within undo's reach"


def test_an_undo_pressed_in_the_chat_composer_does_not_reach_the_manuscript(tmp_path):
    """The hazard the editor's survival creates, held shut.

    Undo is frame-wide. With the source editor still in the document behind the
    chat tab, an undo pressed in the composer is delivered to it as a
    `historyUndo` input event -- measured in Chrome 151 -- and the source editor
    is the thing that writes to the manuscript. So it saves what happens in the
    box the author is actually in, and nothing else.
    """
    session, page = served(tmp_path, CITED, bib=BIB)
    b = para(session, "about to edit in the browser")
    mine = "Second paragraph, as he meant to leave it."
    out = pagedriver.drive(page, [], tmp_path=tmp_path, steps=[
        "select:" + b.id, "type:" + mine, "blur",
        "tab:1", "compose:Please check the tense here.", "bleed:GHOSTWRITTEN",
        "tab:0",
    ])
    assert out["trail"][-1]["value"] == mine, \
        "an undo aimed at the chat composer rewrote the paragraph's source"
    assert "GHOSTWRITTEN" not in json.dumps(out["sent"]), \
        "and it was sent to the server to be written to the file"


def test_reverting_still_puts_the_last_good_text_back(tmp_path):
    """The one control that is SUPPOSED to change what the editor holds.

    The editor now outlives the render, so what it shows is only refreshed when
    the text really moved. Revert is the case that turns on that refresh
    happening at all -- an editor that survived a little too well would leave
    the discarded draft on screen and disagree with the file.
    """
    session, page = served(tmp_path, CITED, bib=BIB)
    b = para(session, "about to edit in the browser")
    out = pagedriver.drive(page, [], tmp_path=tmp_path, steps=[
        "select:" + b.id, "type:Something he thinks better of.", "act:revert",
    ])
    last = out["trail"][-1]
    assert last["value"] == b.source_text, \
        f"revert left the discarded draft in the editor: {last['value']!r}"
    assert last["same"], "and it did not have to rebuild the editor to do it"


def test_the_surviving_editor_still_saves_what_is_typed_into_it(tmp_path):
    """An undo is just more editing, so it has to save like any other keystroke.

    The wiring now happens once, when the element is built, instead of on every
    render. That is the point -- a re-wired editor is a rebuilt editor -- but it
    means the one binding has to go on working after every render that did not
    touch it, and under the block's CURRENT id, since the first save renames it.
    """
    session, page = served(tmp_path, CITED, bib=BIB)
    b = para(session, "about to edit in the browser")
    out = pagedriver.drive(page, [], tmp_path=tmp_path, steps=[
        "select:" + b.id, "type:First go at rewriting the second paragraph.", "blur",
        "type:Second go, after undoing the first.", "blur",
    ])
    edits = [json.loads(s) for s in out["sent"]]
    edits = [e for e in edits if e.get("type") == "edit"]
    assert [e["source"] for e in edits] == [
        "First go at rewriting the second paragraph.",
        "Second go, after undoing the first.",
    ], f"the surviving editor stopped saving: {edits!r}"
    assert edits[-1]["block"] == out["trail"][-1]["block"], \
        "it saved under an id the build no longer has"


# ------------------------------------------------------------- the history card
#
# The panel had one card and it answered one question: what is the agent doing
# NOW. The record of what it had done lived in an 80-entry ring that the drain's
# next restart -- roughly one every twenty wakes -- rewrote from empty. So the
# author could watch it work and could never afterwards read what it had done.


def drained(session, tmp_path: Path, lines, *, then=None) -> list[dict]:
    """The drain works a comment, and the server broadcasts what it makes of it.

    The lines go through `Feed`, which is what the drain writes with, and the
    frames come out of `Session.broadcast`. Nothing here types a frame.
    """
    from manuscriptor.server import feed as feed_mod

    feed = feed_mod.Feed(path=feed_mod.progress_path(paths.agent_dir(tmp_path)), every=0.0)

    async def go():
        with pagedriver.record(session) as sent:
            await session.on_log_change()
            drain.mark(tmp_path, "c-0001", "working")
            await session.on_log_change()
            feed.add([feed_mod.Entry(feed_mod.now(), who, kind, text, ("c-0001",))
                      for who, kind, text in lines], force=True)
            await session.on_feed_change()
            if then:
                then()
                await session.on_log_change()
        return sent
    return asyncio.run(go())


def test_the_page_learns_what_the_agent_did_without_a_reload(tmp_path):
    """The whole live path for the history: ledger -> server -> socket -> DOM."""
    session, page = commented(tmp_path)
    frames = drained(session, tmp_path, [
        ("agent", "note", "read analysis/03_extract.py"),
        ("teammate", "text", "the negation prefix was never checked here"),
        ("agent", "note", "edited main.tex · +6 −4"),
    ], then=lambda: drain.mark(tmp_path, "c-0001", "done"))

    assert [f["type"] for f in frames if f["type"] == "history"], \
        "the server never told the page anything about the work it did"
    out = pagedriver.drive(page, frames, tmp_path=tmp_path)

    rows = {r["id"]: r for r in out["history"]}
    assert "c-0001" in rows, f"no row for the comment that was worked: {out['history']!r}"
    row = rows["c-0001"]
    assert "Soften it" in row["summary"], "the row must say what was ASKED"
    assert "done" in row["summary"], "and how it ended"
    assert row["lines"] == [
        "read analysis/03_extract.py",
        "the negation prefix was never checked here",
        "edited main.tex · +6 −4",
    ], row["lines"]


def test_the_live_history_row_matches_the_one_the_seed_would_have_drawn(tmp_path):
    """The two paths must agree, because the author cannot tell which he is
    looking at: a reload turns one into the other. This is the shape of every
    live-path bug this file exists for."""
    session, page = commented(tmp_path)
    frames = drained(session, tmp_path, [("agent", "text", "Softening the claim.")])
    live = pagedriver.drive(page, frames, tmp_path=tmp_path)

    reloaded = pagedriver.drive(pagedriver.page(session), [], tmp_path=tmp_path)
    assert [r["id"] for r in live["history"]] == [r["id"] for r in reloaded["history"]]
    assert [r["lines"] for r in live["history"]] == [r["lines"] for r in reloaded["history"]], (
        "the pushed row and the reloaded row disagree"
    )


def test_the_history_survives_a_restart_of_the_server(tmp_path):
    """A new `Session` over the same directory: the page it serves must open on
    the work that was done before it existed."""
    session, _ = commented(tmp_path)
    drained(session, tmp_path, [("agent", "text", "Softening the claim.")])

    fresh = app.Session(tmp_path)
    out = pagedriver.drive(pagedriver.page(fresh), [], tmp_path=tmp_path)
    rows = {r["id"]: r for r in out["history"]}
    assert rows["c-0001"]["lines"] == ["Softening the claim."], out["history"]


def test_a_row_the_author_opened_survives_the_next_line_arriving(tmp_path):
    """The card is swapped whole on every frame. A `<details>` folds itself when
    its markup is replaced, so the row he is reading would close under him every
    time the agent said anything."""
    session, page = commented(tmp_path)
    first = drained(session, tmp_path, [("agent", "text", "Reading the script.")])
    more = drained(session, tmp_path, [("agent", "text", "Now rewriting it.")])

    out = pagedriver.drive(page, first + more, tmp_path=tmp_path, steps=[
        "frames:%d" % len(first), "fold:c-0001", "frames",
    ])
    assert out["history"], "no row to fold in the first place"
    assert out["history"][0]["open"] is False, (
        "a line arriving in the row opened it back up under him"
    )
    assert any("Now rewriting it." in l for l in out["history"][0]["lines"]), (
        "and the line that arrived while it was folded never reached it"
    )


# ------------------------------------------------------------------ "Open it"
#
# The author reported that "the open it button does not work", and it did not,
# in the environment he uses. It was an `<a target="_blank">`: a browser tab
# honours that, and the shell's WKWebView cannot, because a second web view
# needs `WKUIDelegate.createWebViewWith` and the shell installs no UI delegate.
# The click was swallowed with no error anywhere -- so these guards are about
# the mechanism and about the silence, in that order.
#
# jsdom is a third environment and honours neither; that is the point. It can
# say what the page ASKS THE SERVER FOR, which is the only claim the fix rests
# on, and it can say that no `target="_blank"` remains to be swallowed.


def compiled(tmp_path: Path, *, deliver: bool):
    """A finished compile, broadcast by the SERVER's own route.

    The frame is not typed here. `compile.route` runs, and `Result.as_frame`
    serializes -- which is the half under test, since `delivered` is the field
    the button now needs and the page never used to be handed it.
    """
    from aiohttp.test_utils import TestClient, TestServer

    from manuscriptor.server import compile as compile_mod

    session, page = served(tmp_path, CITED, bib=BIB)
    built = compile_mod.out_dir(tmp_path) / "main.pdf"
    built.parent.mkdir(parents=True, exist_ok=True)
    built.write_bytes(b"%PDF-1.4\n")
    out = compile_mod.deliver(built, tmp_path) if deliver else None

    def fake(manuscript_dir, *, main=None, bib=None, on_step=None, **kw):
        return compile_mod.Result(
            "pdf", True, built, 3.0, [], None, None, delivered=out)

    was = compile_mod.compile_pdf
    compile_mod.compile_pdf = fake

    async def go():
        with pagedriver.record(session) as sent:
            client = TestClient(TestServer(app.make_app(session)))
            await client.start_server()
            resp = await client.post("/compile", json={"action": "pdf"})
            assert resp.status == 202
            for _ in range(50):
                await asyncio.sleep(0.02)
                if any(f.get("phase") == "done" for f in sent):
                    break
            await client.close()
        return sent

    try:
        frames = asyncio.run(go())
    finally:
        compile_mod.compile_pdf = was
    assert any(f.get("phase") == "done" for f in frames), "the server never finished"
    return session, page, frames, (out or built)


def test_open_it_is_not_a_link_the_shell_cannot_follow(tmp_path):
    """`target="_blank"` needs a UI delegate the shell has not got, so in the
    app the click did nothing and said nothing. Nothing may reintroduce it."""
    session, page, frames, _ = compiled(tmp_path, deliver=True)
    out = pagedriver.drive(page, frames, tmp_path=tmp_path,
                           steps=["frames", "open:compile"])
    panel = out["panel"] or ""
    assert "Open it" in panel, "the button the author pressed is not on the page at all"
    assert "_blank" not in panel, (
        "Open it is still a new-window link, which the app's WKWebView swallows"
    )


def test_open_it_asks_the_server_for_the_file_beside_the_tex(tmp_path):
    """Routed through the server exactly the way Reveal in Finder is -- the one
    mechanism in this app that reliably opens an external thing. And it asks for
    the DELIVERED copy, the PDF beside the `.tex`, not the one in the cache."""
    session, page, frames, want = compiled(tmp_path, deliver=True)
    out = pagedriver.drive(page, frames, tmp_path=tmp_path,
                           steps=["frames", "open:compile", "act:compile:open"])
    posts = [f for f in out["fetched"] if f["url"].endswith("/compile") and f["body"]]
    bodies = [json.loads(p["body"]) for p in posts]
    opens = [b for b in bodies if b.get("action") == "open"]
    assert opens, f"the click asked the server for nothing; it sent {bodies}"
    assert opens[0]["path"] == str(want), opens[0]


def test_open_it_falls_back_to_the_cache_on_a_read_only_serve(tmp_path):
    """`--read-only` withholds the copy beside the `.tex`, so `delivered` is
    None and the only file there is is the one in the cache. The button must
    still open something rather than vanish."""
    session, page, frames, want = compiled(tmp_path, deliver=False)
    out = pagedriver.drive(page, frames, tmp_path=tmp_path,
                           steps=["frames", "open:compile", "act:compile:open"])
    bodies = [json.loads(f["body"]) for f in out["fetched"]
              if f["url"].endswith("/compile") and f["body"]]
    opens = [b for b in bodies if b.get("action") == "open"]
    assert opens, f"nothing to open on a read-only serve; it sent {bodies}"
    assert opens[0]["path"] == str(want), opens[0]


def test_a_refused_open_is_printed_where_the_author_is_looking(tmp_path):
    """THE ACTUAL BUG, one level up from the mechanism. The panel prints an
    error only when the COMPILE failed, so an action failing after a successful
    compile set `S.result.error` into a branch that is never rendered -- which
    is how Reveal in Finder has been failing silently too. A button that does
    nothing and says nothing is the defect, whatever is underneath it."""
    session, page, frames, _ = compiled(tmp_path, deliver=True)
    out = pagedriver.drive(
        page, frames, tmp_path=tmp_path,
        steps=["frames", "open:compile", "act:compile:open"],
        replies=[{"url": "/compile", "status": 400,
                  "body": {"error": "main.pdf is not there any more"}}],
    )
    assert "not there any more" in (out["panel"] or ""), (
        "the server refused and the page said nothing: " + (out["panel"] or "")[:400]
    )


def test_a_refused_reveal_is_printed_too(tmp_path):
    """Same silence, the sibling button. Fixed at the shared surface rather
    than once per action."""
    session, page, frames, _ = compiled(tmp_path, deliver=True)
    out = pagedriver.drive(
        page, frames, tmp_path=tmp_path,
        steps=["frames", "open:compile", "act:compile:reveal"],
        replies=[{"url": "/compile", "status": 400,
                  "body": {"error": "main.pdf is not there any more"}}],
    )
    assert "not there any more" in (out["panel"] or ""), (
        "reveal was refused and the page said nothing"
    )
