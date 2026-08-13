"""What `Session.on_change` owes the open page: a redraw, or an explanation.

Two failures live here, and both were found by measuring a real serve rather
than by reading the code -- on qutub-ayush, whose figures only started
resolving with `a8e80fd` and whose page then stopped keeping up with typing.

THE REBUILD MUST NOT RUN ON THE EVENT LOOP. It is half a second to a second of
pandoc, hashing and file IO on a real manuscript, and for as long as it ran
inline the entire server was stopped for that whole stretch: no `saved` ack, no
websocket frame, no image, no route at all. A page with no figures never
noticed, because it asked the server for nothing while the author typed. A page
with eight of them asks constantly.

AND A FAILURE AFTER THE REBUILD MUST REACH SOMEBODY. `on_change` is started by
the watcher with `run_coroutine_threadsafe`, whose future nobody retrieves, and
a `concurrent.futures.Future` -- unlike an `asyncio` one -- never logs the
exception it is holding. So anything raised after the rebuild vanished
completely: the build advanced, the socket stayed open, the page kept the old
paragraph, and no line of output anywhere said why.
"""
from __future__ import annotations

import asyncio
import shutil
import time
from pathlib import Path

import pytest

from manuscriptor.server import app as app_mod

HAS_PANDOC = shutil.which("pandoc") is not None
pytestmark = pytest.mark.skipif(not HAS_PANDOC, reason="the session needs pandoc to build")

DOC = r"""\documentclass{article}
\begin{document}
First paragraph, entirely unremarkable, and long enough to be a real block of prose.

Second paragraph, which is the one the author is about to edit in the browser.

Third paragraph, which must not be disturbed by anything happening above it.
\end{document}
"""


def _session(tmp_path: Path):
    (tmp_path / "main.tex").write_text(DOC, encoding="utf-8")
    return app_mod.Session(tmp_path, auto_compile=False)


def _edit(session):
    p = session.root / "main.tex"
    p.write_text(p.read_text(encoding="utf-8").replace(
        "in the browser.", "in the browser, now edited."), encoding="utf-8")


def test_a_rebuild_does_not_stop_the_server(tmp_path):
    """Everything else on the loop keeps running while the rebuild runs.

    Measured the way the symptom is felt: a second task ticks every 5ms, and
    the assertion is on the longest gap between its ticks. A rebuild called
    inline holds the loop for its whole duration, so the gap is the rebuild.

    The rebuild is given a fixed synchronous cost rather than a big manuscript,
    because the property under test is not how long a rebuild takes -- it is
    that whatever it takes is not taken out of the loop. Half a second is what
    it measures at on qutub-ayush; a three-paragraph fixture rebuilds in 30ms
    and would pass this test by being too fast to notice.
    """
    session = _session(tmp_path)
    _edit(session)
    real_rebuild = session.rebuild

    def slow_rebuild():
        time.sleep(0.2)
        return real_rebuild()

    session.rebuild = slow_rebuild

    async def go():
        gaps = []

        async def ticker(stop):
            last = time.perf_counter()
            while not stop.is_set():
                await asyncio.sleep(0.005)
                now = time.perf_counter()
                gaps.append(now - last)
                last = now

        stop = asyncio.Event()
        t = asyncio.create_task(ticker(stop))
        await asyncio.sleep(0.02)
        t0 = time.perf_counter()
        await session.on_change()
        took = time.perf_counter() - t0
        stop.set()
        await t
        return took, max(gaps)

    took, worst = asyncio.run(go())
    # The rebuild has to have been long enough for the question to mean
    # anything; a trivial one would pass this test by being fast.
    assert took > 0.05, f"rebuild too quick ({took:.3f}s) for this to prove anything"
    assert worst < took / 2, (
        f"the loop was blocked for {worst:.3f}s of a {took:.3f}s rebuild -- "
        "the whole server is stopped while the page is redrawn")


def test_a_failure_after_the_rebuild_reaches_the_page(tmp_path, capsys):
    """The one thing worse than a stale page is a stale page with no reason.

    `_diff` is made to raise, standing in for anything that can fail between
    the rebuild and the broadcast. Started the way the WATCHER starts it, which
    is the whole point: a direct `await` has a caller who sees the exception,
    and the watcher is the one launcher that never did.
    """
    session = _session(tmp_path)
    _edit(session)
    sent = []

    async def capture(msg):
        sent.append(msg)

    session.broadcast = capture
    boom = RuntimeError("diff exploded")
    real = app_mod._diff

    def raiser(*a, **k):
        raise boom

    app_mod._diff = raiser
    try:
        async def go():
            loop = asyncio.get_running_loop()
            session.spawn(session.on_change(), "redraw after a source change",
                          loop=loop)
            for _ in range(200):
                await asyncio.sleep(0.01)
                if any(m.get("type") == "error" for m in sent):
                    return

        asyncio.run(go())
    finally:
        app_mod._diff = real

    kinds = [m.get("type") for m in sent]
    assert "error" in kinds, (
        f"nothing told the page anything; it sent {kinds} and kept the old paragraph")
    said = " ".join(str(m.get("message", "")) for m in sent if m.get("type") == "error")
    assert "diff exploded" in said, f"the error frame did not say what went wrong: {said!r}"
    assert "diff exploded" in capsys.readouterr().out, "and it was not printed either"
