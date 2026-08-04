"""Compiling in the background when the page's numbers have gone stale.

The author should not have to press Compile to see a cross-reference number.
Every number on the page comes out of the `.aux`; the `.aux` comes out of a
compile; and between the two the page prints `??` -- or, worse, prints the
number an exhibit used to have, which looks like an answer.

So: when the numbering is stale, the server compiles for itself. Four gates make
that safe rather than obnoxious, and each is a test here, each written to be
watched failing with its gate removed.

**It never delivers.** The button's compile copies the finished PDF beside the
author's `.tex`, a write he asked for by pressing it. A run he never requested
must stay entirely in the cache. Flip `deliver_out` and this file fails on the
sha of a PDF nobody asked it to touch.

**It triggers on the label signature, not on typing.** Prose cannot move a
`\\ref`. Remove that gate and every keystroke pause starts a LaTeX run.

**It attempts each signature once.** A `\\ref{typo}` no compile can satisfy
leaves the manuscript exactly as stale as it found it, so re-triggering on the
result of the attempt is an infinite loop with a subprocess in it.

**A failure is quiet, not silent.** It refreshes nothing, retries nothing, opens
no panel -- and it goes out on the same frame the button's failures use, saying
which run it was.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests import pagedriver
from manuscriptor.render import refs
from manuscriptor.server import compile as compile_mod
from manuscriptor.server import paths

HAS_PANDOC = shutil.which("pandoc") is not None
HAS_TEX = shutil.which("pdflatex") is not None
NO_PAGE = pagedriver.missing()

COVET = Path("/Users/bbdaniels/Projects/covet-india/manuscript")


# ------------------------------------------------------------- the signature


def test_prose_is_not_part_of_the_signature():
    """The whole reason this is a signature and not an mtime."""
    a = refs.signature("\\label{t:x}\nSee Table~\\ref{t:x}.\n")
    b = refs.signature("\\label{t:x}\nSee Table~\\ref{t:x} for the design of it.\n")
    assert a == b, "a prose edit changed the numbering signature, so typing compiles"


def test_a_new_label_changes_the_signature():
    a = refs.signature("\\label{t:x}\n")
    b = refs.signature("\\label{t:x}\n\\label{t:y}\n")
    assert a != b


def test_a_new_ref_changes_the_signature():
    a = refs.signature("\\label{t:x}\n")
    b = refs.signature("\\label{t:x}\nTable~\\ref{t:x}\n")
    assert a != b


def test_pageref_and_ref_on_one_key_are_two_different_uses():
    """They are satisfied by two different entries in the same `.aux`."""
    a = refs.signature("\\ref{t:x}")
    b = refs.signature("\\ref{t:x}\\pageref{t:x}")
    assert a != b


def test_a_commented_out_label_is_not_declared():
    assert refs.signature("% \\label{t:x}\n").labels == ()
    assert refs.signature("100\\% \\label{t:x}\n").labels == ("t:x",)


def test_the_counter_binding_is_carried():
    """`\\refstepcounter{figure}\\label{k}` is what decides what `\\thefigure`
    prints, so rebinding it moves a printed exhibit number with no label and no
    ref changing at all."""
    a = refs.signature("\\refstepcounter{figure}\\label{k}\\thefigure")
    b = refs.signature("\\refstepcounter{table}\\label{k}\\thetable")
    assert a.bindings == (("figure", "k"),)
    assert a != b


# ------------------------------------------------------- what the aux satisfies


def _labels(*pairs) -> dict[str, str]:
    out = {}
    for key, number in pairs:
        out[key] = number
        out[key + refs.PAGE_SUFFIX] = "7"
    return out


def test_an_aux_that_carries_everything_is_satisfied():
    sig = refs.signature("\\label{t:x}\nTable~\\ref{t:x}\n")
    assert refs.unsatisfied(sig, _labels(("t:x", "1"))) is None


def test_a_never_compiled_manuscript_says_so():
    sig = refs.signature("\\label{t:x}\nTable~\\ref{t:x}\n")
    assert refs.unsatisfied(sig, {}) == (
        "nothing has been compiled yet, so no cross-reference has a number")


def test_an_empty_manuscript_with_no_aux_is_not_stale():
    assert refs.unsatisfied(refs.signature("Just prose.\n"), {}) is None


def test_an_unresolved_ref_is_named():
    sig = refs.signature("\\label{t:x}\nTable~\\ref{t:x} and~\\ref{t:typo}\n")
    reason = refs.unsatisfied(sig, _labels(("t:x", "1")))
    assert "t:typo" in reason and "unresolved" in reason


def test_a_new_label_the_aux_has_never_seen_is_stale():
    sig = refs.signature("\\label{t:x}\n\\label{t:new}\n")
    reason = refs.unsatisfied(sig, _labels(("t:x", "1")))
    assert "t:new" in reason and "new label" in reason


def test_a_deleted_label_is_stale_because_its_siblings_renumbered():
    sig = refs.signature("\\label{t:x}\n")
    reason = refs.unsatisfied(sig, _labels(("t:x", "1"), ("t:gone", "2")))
    assert "t:gone" in reason and "removed" in reason


def test_the_labels_latex_writes_for_itself_are_not_deletions():
    """hyperref's `sub@`, cleveref's `@cref` and lastpage's `LastPage` appear in
    every compiled `.aux` and in no manuscript. Counting them as removed labels
    would report every paper stale forever, which is the same as no gate."""
    sig = refs.signature("\\label{t:x}\n")
    labels = _labels(("t:x", "1"), ("sub@t:x", "1a"), ("t:x@cref", "tab"),
                     ("LastPage", "22"))
    assert refs.unsatisfied(sig, labels) is None


# --------------------------------------------------------------- the session


def _tree(tmp_path: Path, body: str) -> Path:
    d = tmp_path / "ms"
    d.mkdir(exist_ok=True)
    (d / "main.tex").write_text(
        "\\documentclass{article}\n\\begin{document}\n" + body + "\n\\end{document}\n",
        encoding="utf-8")
    return d


def _write_aux(d: Path, *pairs, read_only: bool = False) -> Path:
    path = paths.compile_dir(d, read_only=read_only) / "main.aux"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\\relax\n" + "".join(
            "\\newlabel{%s}{{%s}{7}}\n" % (k, n) for k, n in pairs),
        encoding="utf-8")
    return path


class Spy:
    """A stand-in for `compile_pdf` that records how it was called."""

    def __init__(self, *, ok=True, writes=(), error=None):
        self.calls = []
        self.ok = ok
        self.writes = writes
        self.error = error

    def __call__(self, manuscript_dir, **kw):
        self.calls.append(kw)
        out = paths.compile_dir(Path(manuscript_dir),
                               read_only=kw.get("read_only", False))
        out.mkdir(parents=True, exist_ok=True)
        pdf = out / "main.pdf"
        if self.ok:
            pdf.write_bytes(b"%PDF-1.4\n")
            if self.writes:
                _write_aux(Path(manuscript_dir), *self.writes,
                           read_only=kw.get("read_only", False))
        return compile_mod.Result(
            kind="pdf", ok=self.ok, output=pdf if self.ok else None,
            seconds=0.1, steps=[], error=self.error, log=None)


def _session(d: Path, **kw):
    from manuscriptor.server.app import Session

    return Session(d, **kw)


async def _settle(session):
    """Wait out whatever the trigger started."""
    for _ in range(600):
        if not compile_mod._BUSY.get(session) and session not in compile_mod._OWED:
            break
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.05)


def _drive(session, spy, *, changes=()):
    """Run `on_change` once per entry in `changes` (each a new main.tex body)."""
    sent = []

    async def capture(msg):
        sent.append(msg)

    session.broadcast = capture
    real = compile_mod.compile_pdf
    compile_mod.compile_pdf = spy
    try:
        async def go():
            for body in (changes or [None]):
                if body is not None:
                    (session.root / "main.tex").write_text(
                        "\\documentclass{article}\n\\begin{document}\n" + body +
                        "\n\\end{document}\n", encoding="utf-8")
                await session.on_change()
                await _settle(session)

        asyncio.run(go())
    finally:
        compile_mod.compile_pdf = real
    return sent


pytestmark = pytest.mark.skipif(not HAS_PANDOC, reason="the session needs pandoc to build")


def test_a_stale_page_compiles_itself(tmp_path):
    d = _tree(tmp_path, "Table~\\ref{t:x}\n\\label{t:x}\n")
    _write_aux(d, ("t:x", "1"))
    session = _session(d)
    spy = Spy(writes=(("t:x", "1"), ("t:new", "2")))
    _drive(session, spy, changes=["Table~\\ref{t:new}\n\\label{t:x}\n\\label{t:new}\n"])
    assert len(spy.calls) == 1, (
        "a \\label and a \\ref the .aux has never seen did not start a compile, "
        "so the page keeps printing ?? until the author presses a button")


def test_typing_prose_compiles_nothing(tmp_path):
    """THE SIGNATURE GATE. Remove it and every save pause runs LaTeX."""
    d = _tree(tmp_path, "Table~\\ref{t:x}\n\\label{t:x}\n")
    _write_aux(d, ("t:x", "1"))
    session = _session(d)
    spy = Spy()
    _drive(session, spy, changes=[
        "Table~\\ref{t:x} for the design.\n\\label{t:x}\n",
        "Table~\\ref{t:x} for the design of the study.\n\\label{t:x}\n",
    ])
    assert spy.calls == [], (
        "prose edits started a background compile; this is the thrash the "
        "signature exists to prevent")


def test_an_unsatisfiable_ref_is_attempted_once(tmp_path):
    """THE ATTEMPT GATE. Remove it and a typo loops forever, one LaTeX run at a
    time, because the failed compile leaves the reference exactly as unresolved
    as it found it."""
    d = _tree(tmp_path, "Table~\\ref{t:x}\n\\label{t:x}\n")
    _write_aux(d, ("t:x", "1"))
    session = _session(d)
    spy = Spy(ok=False, error="! Undefined control sequence.")
    body = "Table~\\ref{t:x} and~\\ref{t:typo}\n\\label{t:x}\n"
    _drive(session, spy, changes=[body, body + "\nMore prose.\n", body + "\nEven more.\n"])
    assert len(spy.calls) == 1, (
        f"a reference no compile can satisfy was attempted {len(spy.calls)} times; "
        "a failed run must wait for the source to change")


def test_a_changed_signature_is_attempted_again_after_a_failure(tmp_path):
    """The other half of the same gate: it waits for the source, and the source
    moving is what releases it."""
    d = _tree(tmp_path, "Table~\\ref{t:x}\n\\label{t:x}\n")
    _write_aux(d, ("t:x", "1"))
    session = _session(d)
    spy = Spy(ok=False, error="! Undefined control sequence.")
    _drive(session, spy, changes=[
        "Table~\\ref{t:x} and~\\ref{t:typo}\n\\label{t:x}\n",
        "Table~\\ref{t:x} and~\\ref{t:fixed}\n\\label{t:x}\n\\label{t:fixed}\n",
    ])
    assert len(spy.calls) == 2


def test_a_background_run_never_delivers(tmp_path):
    """THE DELIVER GATE, at the call. The end-to-end below checks the disk."""
    d = _tree(tmp_path, "Table~\\ref{t:new}\n\\label{t:new}\n")
    _write_aux(d, ("t:x", "1"))
    session = _session(d)
    spy = Spy(writes=(("t:new", "1"),))
    _drive(session, spy, changes=["Table~\\ref{t:new}\n\\label{t:new}\n\\label{t:more}\n"])
    assert spy.calls, "nothing ran, so the gate under test was never exercised"
    assert all(call["deliver_out"] is False for call in spy.calls), (
        "a compile the author never asked for wrote a PDF into his manuscript "
        "directory: " + repr(spy.calls))


def test_the_flag_turns_it_off(tmp_path):
    d = _tree(tmp_path, "Table~\\ref{t:x}\n\\label{t:x}\n")
    session = _session(d, auto_compile=False)
    spy = Spy()
    _drive(session, spy, changes=["Table~\\ref{t:x}\n\\label{t:x}\n\\label{t:y}\n"])
    assert spy.calls == [], "--no-auto-compile did not stop the background compile"


def test_a_failure_is_visible_and_says_whose_run_it_was(tmp_path):
    d = _tree(tmp_path, "Table~\\ref{t:x}\n\\label{t:x}\n")
    _write_aux(d, ("t:x", "1"))
    session = _session(d)
    spy = Spy(ok=False, error="main.tex:4: Undefined control sequence.")
    sent = _drive(session, spy,
                  changes=["Table~\\ref{t:x}\n\\label{t:x}\n\\label{t:y}\n"])
    done = [m for m in sent if m.get("type") == "compile" and m.get("phase") == "done"]
    assert done, "a failed background compile said nothing at all"
    assert done[-1]["auto"] is True
    assert done[-1]["error"].startswith(compile_mod.AUTO_FAILED), done[-1]["error"]
    assert "Undefined control sequence" in done[-1]["error"], (
        "the failure was announced without the reason, which is a notification "
        "nobody can act on")


def test_a_failed_background_compile_refreshes_nothing(tmp_path):
    """Its `.aux` is whatever the run died holding."""
    d = _tree(tmp_path, "Table~\\ref{t:x}\n\\label{t:x}\n")
    _write_aux(d, ("t:x", "1"))
    session = _session(d)
    spy = Spy(ok=False, error="! Emergency stop.")
    session.broadcast = _swallow
    rebuilds = []
    original = session.on_change

    async def counted(**kw):
        rebuilds.append(kw)
        return await original(**kw)

    session.on_change = counted
    real = compile_mod.compile_pdf
    compile_mod.compile_pdf = spy
    compile_mod._BUSY[session] = "auto"
    try:
        asyncio.run(compile_mod._auto_work(session, "because"))
    finally:
        compile_mod.compile_pdf = real
        compile_mod._BUSY.pop(session, None)
    assert rebuilds == [], (
        "a failed background compile triggered a rebuild, so a half-written "
        ".aux reached the page")


async def _swallow(msg):
    return None


def test_a_change_during_a_run_is_paid_once_at_the_end(tmp_path):
    """LATEST WINS. A change arriving mid-compile must not queue a second run
    beside the first, and must not be dropped either."""
    d = _tree(tmp_path, "Table~\\ref{t:x}\n\\label{t:x}\n\\label{t:y}\n")
    _write_aux(d, ("t:x", "1"))
    session = _session(d)
    session.broadcast = _swallow
    spy = Spy(writes=(("t:x", "1"), ("t:y", "2")))
    real = compile_mod.compile_pdf
    compile_mod.compile_pdf = spy
    try:
        async def go():
            compile_mod._BUSY[session] = "pdf"          # the button is running
            for _ in range(3):
                session.auto_sig = None                 # three separate changes
                assert await compile_mod.auto_compile(session) == "busy"
            assert len(spy.calls) == 0
            compile_mod._BUSY.pop(session, None)
            await compile_mod._pay_owed(session)
            await _settle(session)
            assert len(spy.calls) == 1, (
                f"three changes during one compile produced {len(spy.calls)} runs; "
                "the owed re-check is one, not a queue")
            assert session not in compile_mod._OWED

        asyncio.run(go())
    finally:
        compile_mod.compile_pdf = real
        compile_mod._BUSY.pop(session, None)
        compile_mod._OWED.discard(session)


def test_a_read_only_serve_compiles_into_its_scratch_and_nowhere_else(tmp_path):
    """The read-only answer: the run is allowed, because every byte of it lands
    under the system temp -- the `.aux`, the `.log` and the PDF -- and the
    deliver was already withheld."""
    d = _tree(tmp_path, "Table~\\ref{t:x}\n\\label{t:x}\n")
    before = sorted(p.name for p in d.iterdir())
    session = _session(d, read_only=True)
    spy = Spy(writes=(("t:x", "1"),))
    _drive(session, spy, changes=["Table~\\ref{t:x}\n\\label{t:x}\n\\label{t:y}\n"])
    assert spy.calls, "a read-only serve refused to compile into its own scratch"
    assert spy.calls[0]["read_only"] is True
    assert spy.calls[0]["deliver_out"] is False
    assert sorted(p.name for p in d.iterdir()) == before, (
        "a read-only serve wrote into the author's directory")


# ------------------------------------------------- what the author can see

# The page, driven with the frames the server actually built. A failure the
# author cannot find is the defect this app keeps rediscovering, and asserting
# on the frame alone proves only that it left the building.


def _auto_frames(tmp_path, spy):
    """Run one background compile and return what it broadcast, verbatim."""
    from tests import pagedriver

    d = _tree(tmp_path, "Table~\\ref{t:x}\n\\label{t:x}\n\\label{t:y}\n")
    _write_aux(d, ("t:x", "1"))
    session = _session(d)
    real = compile_mod.compile_pdf
    compile_mod.compile_pdf = spy
    compile_mod._BUSY[session] = "auto"
    try:
        with pagedriver.record(session) as sent:
            asyncio.run(compile_mod._auto_work(session, "1 new label(s): t:y"))
    finally:
        compile_mod.compile_pdf = real
        compile_mod._BUSY.pop(session, None)
    return session, [f for f in sent if f.get("type") == "compile"]


@pytest.mark.skipif(bool(NO_PAGE), reason=str(NO_PAGE))
def test_a_background_failure_is_readable_in_the_compile_panel(tmp_path):
    """Quiet, not silent. The run opens no panel over what the author is
    writing -- and when he opens it, it names the failure as the background
    run's, with the reason."""
    from tests import pagedriver

    session, frames = _auto_frames(
        tmp_path, Spy(ok=False, error="main.tex:4: Undefined control sequence."))
    assert frames, "the background compile broadcast nothing at all"
    page = pagedriver.page(session)

    quiet = pagedriver.drive(page, frames, steps=["frames"], tmp_path=tmp_path)
    assert compile_mod.AUTO_FAILED not in (quiet["panel"] or ""), (
        "a compile the author never started replaced the panel he was using "
        "with its own failure: " + (quiet["panel"] or "")[:300])

    opened = pagedriver.drive(page, frames, steps=["frames", "open:compile"],
                              tmp_path=tmp_path)
    panel = opened["panel"] or ""
    assert "auto-compile for numbering failed" in panel, (
        "the background failure is unreadable where the button's failures are "
        "read: " + panel[:400])
    assert "Undefined control sequence" in panel, panel[:400]


# ------------------------------------------------------------- end to end


@pytest.mark.skipif(not HAS_TEX, reason="needs pdflatex")
@pytest.mark.skipif(not COVET.is_dir(), reason="needs the covet-india manuscript")
def test_end_to_end_on_a_copy_of_a_real_manuscript(tmp_path):
    """A real paper, a real `\\label` and `\\ref` added to a real source file, a
    real LaTeX run: the number reaches the page and the author's PDF is not
    touched."""
    d = tmp_path / "covet"
    shutil.copytree(COVET, d, ignore=shutil.ignore_patterns(
        "__pycache__", ".manuscriptor", "consistency-check"))
    subprocess.run(["git", "init", "-q", str(d)], check=False, capture_output=True)

    pdf = d / "main.pdf"
    was = (pdf.stat().st_mtime_ns, hashlib.sha256(pdf.read_bytes()).hexdigest())

    main = d / "main.tex"
    text = main.read_text(encoding="utf-8")
    marker = "\\section{Results}"
    assert marker in text, "the fixture manuscript changed shape"
    text = text.replace(
        marker,
        marker + "\n\\label{sec:autocompile-probe}\n"
        "Numbering probe: section~\\ref{sec:autocompile-probe}.\n", 1)
    main.write_text(text, encoding="utf-8")

    session = _session(d)
    sent = []

    async def capture(msg):
        sent.append(msg)

    session.broadcast = capture

    async def go():
        await session.on_change()
        for _ in range(6000):        # a real compile: up to a minute
            if not compile_mod._BUSY.get(session):
                break
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.2)

    asyncio.run(go())

    aux = paths.compile_dir(d) / "main.aux"
    assert aux.is_file(), "the background compile wrote no .aux into the cache"
    assert "sec:autocompile-probe" in aux.read_text(encoding="utf-8", errors="replace")

    done = [m for m in sent if m.get("phase") == "done"]
    assert done and done[-1].get("auto") is True, sent
    assert done[-1]["ok"] is True, done[-1].get("error")

    html = session.blob["html"]
    assert "Numbering probe" in html
    probe = html[html.index("Numbering probe"):][:120]
    assert "??" not in probe, probe
    assert session.blob["diagnostics"]["unresolved_refs"] == [] or \
        "sec:autocompile-probe" not in session.blob["diagnostics"]["unresolved_refs"]

    now = (pdf.stat().st_mtime_ns, hashlib.sha256(pdf.read_bytes()).hexdigest())
    assert now == was, (
        "the background compile replaced the author's own PDF beside his .tex; "
        "only the button he presses may deliver")
