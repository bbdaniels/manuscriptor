"""The tint on the author's own chat bubble, on the page as served.

WHAT WENT WRONG. `.msg.bb` had one trigger in the page: `m.who === 'you'`, and
`'you'` is only ever the CLIENT-LOCAL optimistic placeholder the composer draws
before the server has echoed the record back. The echo arrives sub-second later
carrying the real author name, and every bubble in the history -- including the
author's own, and including the one he just typed -- rendered untinted. So the
class was very nearly dead, and its name (predating the author display name
becoming "Ben") read as if it were about that name. It is not: nothing ever
compared `who` against the author at all.

WHAT THESE ASSERT. `test_..._carries_the_class` drives the real page and reads
the panel it built, which is behaviour. The cascade test that follows resolves
the real stylesheet against a real rendered bubble -- jsdom does no layout, so
it is the DECLARATION in force, never a measurement. The visible consequence,
measured in a browser, is that the author's bubbles sit on `--accent-soft` with
no border while an agent's keep the default card background and its border.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tests import pagedriver
from manuscriptor.server import app, chat, paths

WHY = pagedriver.missing()
pytestmark = pytest.mark.skipif(bool(WHY), reason=str(WHY))


DOC = r"""\documentclass{article}
\begin{document}
\section{Results}
A first paragraph of ordinary prose, which is the one the comment hangs on.

A second paragraph of ordinary prose, so the document has more than one block.
\end{document}
"""


def commented(tmp_path: Path):
    """A served manuscript whose first paragraph carries three messages: one
    from the author under the current name, one from the agent, and one from
    the author under the OLD name every already-written record uses."""
    (tmp_path / "main.tex").write_text(DOC, encoding="utf-8")
    session = app.Session(tmp_path)
    para = [b for b in session.build.blocks if b.kind == "paragraph"][0]
    log = paths.comments(tmp_path)
    chat.append(log, {
        "id": "c-0001", "kind": "comment", "block": para.id, "file": str(para.file),
        "quote": para.source_text[:120], "body": "This overclaims. Soften it.",
        "author": chat.AUTHOR, "ts": chat.now()})
    chat.append(log, {
        "id": "c-0001", "kind": "reply", "body": "Softened, and here is why.",
        "author": "claude", "ts": chat.now()})
    chat.append(log, {
        "id": "c-0002", "kind": "comment", "block": para.id, "file": str(para.file),
        "quote": para.source_text[:120], "body": "An older note of mine.",
        "author": "bb", "ts": chat.now()})
    session.rebuild()
    return session, pagedriver.page(session), para


def bubbles(panel_html: str) -> list[tuple[str, str]]:
    """(class attribute, body text) for every message in the panel, in order."""
    import re

    out = []
    for chunk in panel_html.split('<div class="msg')[1:]:
        cls = "msg" + chunk.split('"', 1)[0]
        out.append((cls, re.sub("<[^>]+>", " ", chunk.split('"', 1)[1])))
    return out


def panel_of(tmp_path: Path):
    session, page, para = commented(tmp_path)
    out = pagedriver.drive(page, [], steps=["select:" + para.id, "tab:1"],
                           tmp_path=tmp_path)
    return out["panel"] or ""


def test_the_panel_the_probe_reads_really_has_the_three_bubbles(tmp_path):
    """Without all three the guards below are vacuous."""
    panel = panel_of(tmp_path)
    assert "This overclaims" in panel and "Softened, and here" in panel \
        and "An older note" in panel, f"the chat did not render: {panel[:400]!r}"


def test_the_authors_own_bubble_carries_the_class_and_the_agents_does_not(tmp_path):
    """Server-delivered, which is the only kind that survives a second."""
    panel = panel_of(tmp_path)
    got = {body.strip(): cls for cls, body in bubbles(panel)}
    mine = [b for b in got if "overclaims" in b or "older note" in b]
    assert len(mine) == 2, f"the author's bubbles are missing from {got!r}"
    for b in mine:
        assert "mine" in got[b].split(), \
            f"the author's own bubble is untinted: class={got[b]!r}"
    agent, = [b for b in got if "Softened" in b]
    assert "mine" not in got[agent].split(), \
        f"the agent's bubble is tinted as the author's: class={got[agent]!r}"


def test_the_class_the_page_sets_is_the_class_the_stylesheet_tints(tmp_path):
    """The cascade, not a layout: what `.msg.mine` declares against a plain
    `.msg`, resolved out of the real stylesheet on real rendered bubbles.

    In a browser the difference is a filled `--accent-soft` bubble with no
    border against the default card background with one. jsdom reports no box
    at all, so only the declaration is asserted here.
    """
    session, page, para = commented(tmp_path)
    out = pagedriver.drive(page, [], steps=["select:" + para.id, "tab:1"],
                           tmp_path=tmp_path)
    # The page as served plus the panel the page itself built, so the probe
    # resolves the served stylesheet against bubbles nothing typed by hand.
    seeded = page.replace('<script id="ms-data">',
                          '<div id="probe-panel">' + (out["panel"] or "") +
                          '</div><script id="ms-data">')
    got = pagedriver.computed(
        seeded,
        [{"sel": "#probe-panel .msg.mine", "props": ["background", "border-color"]},
         {"sel": "#probe-panel .msg:not(.mine)", "props": ["background", "border-color"]}],
        tmp_path=tmp_path,
    )
    ours, theirs = got
    assert ours["found"], "no bubble on the page carries .mine, so nothing is tinted"
    assert theirs["found"], "every bubble carries .mine; the fixture lost the agent reply"
    assert ours["declared"]["background"] and \
        ours["declared"]["background"] != theirs["declared"]["background"], \
        (f"a .mine bubble declares the same background as any other: "
         f"{ours['declared']!r} against {theirs['declared']!r}")
