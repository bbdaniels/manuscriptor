"""The save badge, and the rule that it may never move the box being typed in.

THE REPORT, 2026-08-04: "the 'unsaved' popup when typing moves the text window
which messes with inserts."

MEASURED IN CHROME 151, on a served copy of covet-india at a 1440x900 window
and the default 654px inspector. Selecting a paragraph puts the editor's top
edge at y=365.9. The first keystroke inserts the `.dirtybar` banner into
`#ibody` ABOVE the editor's card -- 33.0px tall -- and the editor's top edge
goes to y=398.9. The save then lands about a second later, `clearDraft` empties
the banners, and it comes back to 365.9. So the box the author is typing into
walks 33px down and 33px up on roughly a one-second cycle for the whole of a
typing session, which is what lands mid-keystroke and breaks an insert.

The header's own save line is a second source of the same defect and it only
shows at a narrower inspector, where its sentence wraps. Measured the same way
at a 420px inspector: "No unsaved changes in this block." is one line and 24.2px,
"Saved to exhibits/t2-changes-note.tex, just now. Writes on a pause, not a
button." is two lines and 41.9px, and the editor's top edge moves 422.3 -> 440.1,
a further 17.8px. At 360px the first keystroke alone costs both, 50.8px in total.

WHAT THE GUARDS BELOW ASSERT, AND WHAT THEY DO NOT. jsdom does no layout, so
none of the numbers above can be re-measured here; every box jsdom reports is
zero. Two weaker claims stand in, and each is the CAUSE of a number above:

  * the structural one -- the editor's card sits in `#ibody`'s normal flow, so
    the only thing that can move it is a box appearing or vanishing above it,
    and `trail[...]["above"]` is that list. It is asserted unchanged across the
    author's keystrokes and across a save landing under them.
  * the cascade one -- what the stylesheet declares for the header's save line,
    which is what decides whether its sentence can wrap to a second line at all.

Both defects are long-standing rather than regressions.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from tests import pagedriver
from manuscriptor.server import app

WHY = pagedriver.missing()
pytestmark = pytest.mark.skipif(bool(WHY), reason=str(WHY))


PAPER = r"""\documentclass{article}
\begin{document}
\section{Results}
An opening paragraph, entirely unremarkable, and long enough to be a block of its own.

Second paragraph, which is the one the author is about to edit in the browser today.

Third paragraph, which must not be disturbed by anything happening above it at all.
\end{document}
"""


def served(tmp_path: Path, body: str = PAPER):
    (tmp_path / "main.tex").write_text(body, encoding="utf-8")
    session = app.Session(tmp_path)
    return session, pagedriver.page(session)


def para(session, words: str):
    """The block whose source contains `words`, so no test writes down a
    content-derived id that its own edit would rename."""
    return next(b for b in session.build.blocks if words in b.source_text)


def test_the_fixture_really_opens_an_editor(tmp_path):
    """Both guards below are vacuous if the paragraph has no source editor."""
    session, page = served(tmp_path)
    b = para(session, "about to edit in the browser")
    out = pagedriver.drive(page, [], tmp_path=tmp_path, steps=["select:" + b.id])
    last = out["trail"][-1]
    assert last["present"] and last["showing"], "no editor for the selected paragraph"
    assert last["above"] is not None, "the harness could not read the panel body"


def test_typing_puts_nothing_above_the_editor(tmp_path):
    """A keystroke may not insert a box above the box being typed in.

    In Chrome the inserted box is `.dirtybar`, 33.0px, and the editor's top edge
    goes 365.9 -> 398.9 on the first character. Here it is the arrival of an
    element in `#ibody` before the editor's card, which is what those 33 pixels
    are made of.
    """
    session, page = served(tmp_path)
    b = para(session, "about to edit in the browser")
    out = pagedriver.drive(page, [], tmp_path=tmp_path, steps=[
        "select:" + b.id,
        "type:Second paragraph, rewritten by hand while he watches the box.",
    ])
    rest, typing = out["trail"][0], out["trail"][1]
    assert typing["above"] == rest["above"], (
        "typing added "
        + repr([h for h in typing["above"] if h not in rest["above"]])
        + " above the editor, which pushes it down the panel mid-keystroke"
    )


def test_a_save_landing_under_the_cursor_takes_nothing_away_above_the_editor(tmp_path):
    """The save is the other half of the same 33px, in the other direction.

    The draft is restored from the server's own store, so the panel opens with
    the `.restored` notice already above the editor; the author types, and the
    real `saved` frame the server produced for that edit arrives. `clearDraft`
    drops both the draft AND the restored flag, `renderBanners` empties the
    box, and the editor jumps back UP by the height of whatever was there.

    The frame is the server's: `Session.on_edit` built it. Writing one here
    would be the blindness `pagedriver` exists to prevent.
    """
    session, _ = served(tmp_path)
    b = para(session, "about to edit in the browser")
    held = "Second paragraph, held unsaved from the last time he was here."
    session.keep_draft(b.id, held)
    # The held text is read into the blob when a session builds, so the page a
    # RELOAD would serve is the page of a session opened after the draft was
    # kept -- which is exactly the situation the `.restored` notice is for.
    reopened = app.Session(tmp_path)
    page = pagedriver.page(reopened)
    assert held in page, "the served page is not carrying the held draft"

    typed = "Second paragraph, rewritten by hand while he watches the box."
    frame = asyncio.run(session.on_edit(b.id, typed))
    assert frame["type"] == "saved", frame

    out = pagedriver.drive(page, [frame], tmp_path=tmp_path, steps=[
        "select:" + b.id, "type:" + typed, "blur", "frames",
    ])
    opened, after = out["trail"][0], out["trail"][-1]
    assert after["above"] == opened["above"], (
        "the save removed "
        + repr([h for h in opened["above"] if h not in after["above"]])
        + " from above the editor, which pulls it up the panel"
    )


def test_the_restored_notice_is_retired_when_he_leaves_the_block(tmp_path):
    """Surviving the save must not mean surviving forever.

    It reports what was waiting when the panel opened, so it belongs to the
    visit. Leaving the block ends the visit, and coming back to a block whose
    draft has since been written must not still announce a restored draft. The
    check is here rather than in the code that saves because moving it OUT of
    the save is the whole repair above.
    """
    session, _ = served(tmp_path)
    b = para(session, "about to edit in the browser")
    other = para(session, "must not be disturbed")
    session.keep_draft(b.id, "Second paragraph, held unsaved from last time.")
    page = pagedriver.page(app.Session(tmp_path))

    out = pagedriver.drive(page, [], tmp_path=tmp_path, steps=[
        "select:" + b.id, "select:" + other.id, "select:" + b.id,
    ])
    opened, back = out["trail"][0], out["trail"][-1]
    assert any("restored" in h for h in opened["above"]), \
        "the fixture never showed the restored notice at all"
    assert not any("restored" in h for h in back["above"]), \
        "the notice came back on a second visit: " + repr(back["above"])


def test_the_save_state_is_still_there_to_read(tmp_path):
    """Taking the transient banner away may not take the FACT away.

    The header's line is the persistent one: it is present for every block,
    before a keystroke and after it, and only its wording changes. That is what
    makes deleting the second, transient rendering of the same fact a
    simplification rather than a loss.

    The harness's socket never opens -- there is no server behind it -- so the
    unsaved state renders here in its offline wording, "Not connected. Your
    draft is held in this window...". It is the same fact in the same box; the
    assertion is on the box still telling him, not on which sentence a live
    socket would have chosen.
    """
    session, page = served(tmp_path)
    b = para(session, "about to edit in the browser")
    out = pagedriver.drive(page, [], tmp_path=tmp_path, steps=[
        "select:" + b.id,
        "type:Second paragraph, rewritten by hand while he watches the box.",
    ])
    rest, typing = out["trail"][0], out["trail"][1]
    assert rest["save"], "no save line at all when the paragraph is opened"
    assert "No unsaved changes" in rest["save"], rest["save"]
    assert typing["save"], "the save line went away as soon as he typed"
    assert "No unsaved changes" not in typing["save"], (
        "the panel still claims there is nothing unsaved: " + repr(typing["save"])
    )
    assert "draft" in typing["save"] or "Unsaved" in typing["save"], (
        "the panel stopped saying the block has unwritten text: " + repr(typing["save"])
    )


def test_the_header_save_line_cannot_wrap_to_a_second_line(tmp_path):
    """One line, always, whatever the state says and however narrow the panel.

    A CASCADE assertion, not a measurement: it asserts the declarations that
    decide whether the sentence may wrap, on the element they govern. Their
    effect was measured in Chrome at a 420px inspector -- 24.2px for "No unsaved
    changes in this block.", 41.9px for "Saved to ..., just now. Writes on a
    pause, not a button.", and the editor's top edge moving 422.3 -> 440.1 as
    the state changed under the author.

    The state's own word leads the line and is never what gets elided; the
    trailing explanation is, and it is on the element's `title`.
    """
    session, page = served(tmp_path)
    got, = pagedriver.computed(
        page, [{"sel": "#headsave", "props": ["white-space"]}], tmp_path=tmp_path,
    )
    assert got["found"], "#headsave is not on the page the server rendered"
    assert got["declared"]["white-space"] == "nowrap", (
        "#headsave declares white-space: "
        + repr(got["declared"]["white-space"])
        + ", so its sentence wraps to a second line at a narrow inspector and "
        "the editor below it moves when the state changes"
    )
