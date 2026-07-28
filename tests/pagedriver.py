"""Drive the real page with the server's real frames.

Not a test module. This is the harness the client-side tests were missing.

The rule it exists to enforce: A TEST MAY NOT WRITE A FRAME. Every frame handed
to `drive` has to come out of the server -- `app._diff` for a patch, the state
broadcast for a state, `Session.blob` for anything derived. Hand-typing the
object under test is what let three live-path bugs sit under 975 passing tests:
the seed path was asserted everywhere and the push path nowhere, so a frame the
server never sends was the only frame ever checked.

`record(session)` is the other half: it captures what `Session.broadcast` was
actually asked to send, so a test asserts on the wire and not on an intention.

Requires node and jsdom. Install with:

    cd tests/js && npm install
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JS = ROOT / "tests" / "js"
DRIVE = JS / "drive.js"
NODE = shutil.which("node")


def missing() -> str | None:
    """Why the page cannot be driven here, or None when it can."""
    if not NODE:
        return "node is not installed"
    if not (JS / "node_modules" / "jsdom").exists():
        return "jsdom is not installed; run `npm install` in tests/js"
    return None


def page(session) -> str:
    """The page the server would serve, built by the server's own renderer."""
    from manuscriptor.server import app as app_mod

    return app_mod._page(session)


def drive(page_html: str, frames, *, source=None, steps=None, tmp_path: Path) -> dict:
    """Load `page_html` in jsdom, hand `frames` to the real `handle`, and report.

    `frames` are dicts the SERVER produced. Passing a literal here defeats the
    whole harness, so callers take them from `_diff` or from `record`.

    `page_html` must be the page as it was BEFORE the change under test.
    Rendering it afterwards makes the assertion pass on a page that would have
    been right after a reload, which is the one thing not in question.

    `source` names block ids whose held LaTeX to report, for the assertions that
    are about the source editor rather than about the render.

    `steps` is what the AUTHOR does, in order, with the string ``"frames"``
    marking the moment the server's frames arrive. Without it the frames are
    delivered to a page nobody is using, which cannot see anything that only
    goes wrong under a live cursor. The vocabulary is::

        select:<block id>   click that paragraph in the manuscript
        fold:<comment id>   collapse that history row
        unfold:<comment id> open that history row
        tab:<n>             click the nth tab of the panel
        act:<name>          click the control carrying that data-act
        focus / blur        the source editor gains or loses focus
        type:<text>         type that into the source editor
        compose:<text>      type that into the chat composer
        bleed:<text>        an input event on the source editor while the
                            author's focus is somewhere else, which is how
                            Blink delivers an undo pressed in another box
        frames              every frame not yet delivered arrives
        frames:<n>          only the next n of them do, so a test can put the
                            author's own action in the middle of the stream

    The report gains a ``trail``: after every step, whether the source editor
    is still the SAME ELEMENT it was, what block it names, and what it holds.
    """
    why = missing()
    if why:
        raise RuntimeError(why)
    html_file = tmp_path / "page.html"
    html_file.write_text(page_html, encoding="utf-8")
    plan_file = tmp_path / "frames.json"
    plan = {"frames": list(frames), "source": list(source or [])}
    if steps is not None:
        plan["steps"] = list(steps)
    plan_file.write_text(json.dumps(plan), encoding="utf-8")
    p = subprocess.run(
        [NODE, str(DRIVE), str(html_file), str(plan_file)],
        capture_output=True, text=True, cwd=str(JS),
    )
    assert p.returncode == 0, p.stderr
    out = json.loads(p.stdout)
    assert not out["errors"], f"the page threw: {out['errors']}"
    return out


class record:
    """Capture what a session broadcast, in order.

    A patch frame is only half the live path; the other half is whether the
    server sent anything at all. Two of the three bugs this harness was built
    for are silences -- a rebuild that produced no frame -- and a silence is
    invisible to any assertion made on a frame that was handed over by hand.

        with record(session) as sent:
            await session.on_change()
        assert [f["type"] for f in sent] == ["patch", "stats"]
    """

    def __init__(self, session):
        self.session = session
        self.frames: list[dict] = []
        self._was = None

    def __enter__(self) -> list[dict]:
        self._was = self.session.broadcast

        async def spy(msg):
            self.frames.append(msg)
            return await self._was(msg)

        self.session.broadcast = spy
        return self.frames

    def __exit__(self, *exc):
        self.session.broadcast = self._was
        return False


def of(frames, kind: str) -> list[dict]:
    return [f for f in frames if f.get("type") == kind]


def one(frames, kind: str) -> dict:
    got = of(frames, kind)
    assert len(got) == 1, f"expected exactly one {kind} frame, got {len(got)}"
    return got[0]
