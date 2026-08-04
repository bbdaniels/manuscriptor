"""Which `.aux` the page's numbers come from, and what a compile does to them.

The defect this file was written against, observed live on covet-india: the
author pressed Compile, the compile succeeded, and every `\\ref` on the page
still read `??`. There were two answers to "which .aux is the truth" and the
page read the one the app's own compile can never touch.

`compile.py` writes into `paths.compile_dir()`. `build.py` read
`main_tex.with_suffix(".aux")`, the file beside the source, which only the
author's own terminal `make` ever writes. So the Compile button was structurally
incapable of updating the page's cross-references -- for every manuscript,
always, with no error anywhere.

Two things follow, and each is a guard here.

**One chooser, in `render/refs.py`.** That module is already the single answer to
"what number did TeX give this", so the choice of which file to read belongs to
it. It picks by FRESHNESS, not by tier: an author who has just run `make` in his
terminal has the newer numbers, and preferring our own cache would then serve him
numbers older than the ones his PDF already shows. Per document, because a
directory holding `main.tex` and `supplement.tex` holds two of these.

**A successful compile refreshes the page**, and a failed one refreshes nothing.
A compile that leaves the page showing `??` is the whole bug; a failed compile
that redrew from its half-written `.aux` would be the same bug wearing the
opposite sign.
"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
from pathlib import Path

import pytest

from manuscriptor.render import refs
from manuscriptor.server import compile as compile_mod
from manuscriptor.server import paths

HAS_PANDOC = shutil.which("pandoc") is not None

SRC = Path(__file__).resolve().parent.parent / "manuscriptor"


def _aux(path: Path, label: str, number: str, *, age: float = 0.0) -> Path:
    """An `.aux` carrying one label, optionally backdated by `age` seconds."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\\relax\n"
        "\\newlabel{%s}{{%s}{7}}\n" % (label, number),
        encoding="utf-8",
    )
    if age:
        when = path.stat().st_mtime - age
        os.utime(path, (when, when))
    return path


def _tree(tmp_path: Path) -> tuple[Path, Path]:
    """A manuscript directory and its main `.tex`."""
    d = tmp_path / "ms"
    d.mkdir()
    main = d / "main.tex"
    main.write_text(
        "\\documentclass{article}\n\\begin{document}\n"
        "See Table~\\ref{tab:design} for the design.\n"
        "\\end{document}\n",
        encoding="utf-8",
    )
    return d, main


# ------------------------------------------------------------- the chooser


def test_no_aux_anywhere_chooses_nothing(tmp_path):
    d, main = _tree(tmp_path)
    assert refs.choose_aux(main, paths.compile_dir(d)) is None


def test_the_compile_cache_aux_is_chosen_when_it_is_the_only_one(tmp_path):
    d, main = _tree(tmp_path)
    cached = _aux(paths.compile_dir(d) / "main.aux", "tab:design", "1")
    assert refs.choose_aux(main, paths.compile_dir(d)) == cached


def test_the_aux_beside_the_source_is_chosen_when_it_is_the_only_one(tmp_path):
    d, main = _tree(tmp_path)
    beside = _aux(d / "main.aux", "tab:design", "1")
    assert refs.choose_aux(main, paths.compile_dir(d)) == beside


def test_the_fresher_aux_wins_when_the_app_compiled_last(tmp_path):
    d, main = _tree(tmp_path)
    _aux(d / "main.aux", "tab:design", "1", age=600)
    cached = _aux(paths.compile_dir(d) / "main.aux", "tab:design", "4")
    chosen = refs.choose_aux(main, paths.compile_dir(d))
    assert chosen == cached, (
        "the app's own compile wrote the newer numbers and the page must read them; "
        "this is the covet-india defect, where Compile could never move a \\ref")
    assert refs.load_labels(chosen)["tab:design"] == "4"


def test_the_fresher_aux_wins_when_the_author_ran_make_last(tmp_path):
    """The honesty case. Freshness decides, not whose file it is."""
    d, main = _tree(tmp_path)
    _aux(paths.compile_dir(d) / "main.aux", "tab:design", "4", age=600)
    beside = _aux(d / "main.aux", "tab:design", "9")
    chosen = refs.choose_aux(main, paths.compile_dir(d))
    assert chosen == beside, (
        "the author's own build wrote the newer numbers, so serving ours would "
        "show him numbers older than the PDF on his screen")
    assert refs.load_labels(chosen)["tab:design"] == "9"


def test_each_document_gets_its_own_aux(tmp_path):
    """The compile directory holds every document this directory has compiled."""
    d, main = _tree(tmp_path)
    supplement = d / "supplement.tex"
    supplement.write_text(
        "\\documentclass{article}\n\\begin{document}\nS\\end{document}\n",
        encoding="utf-8")
    _aux(paths.compile_dir(d) / "main.aux", "tab:design", "1")
    supp = _aux(paths.compile_dir(d) / "supplement.aux", "tab:design", "S3")
    chosen = refs.choose_aux(supplement, paths.compile_dir(d))
    assert chosen == supp
    assert refs.load_labels(chosen)["tab:design"] == "S3"


def test_a_missing_compile_directory_is_not_an_error(tmp_path):
    """Nothing has ever compiled here, which is the ordinary first serve."""
    d, main = _tree(tmp_path)
    beside = _aux(d / "main.aux", "tab:design", "1")
    assert refs.choose_aux(main, paths.compile_dir(d)) == beside
    assert not paths.compile_dir(d).exists()


def test_no_compile_dir_at_all_still_answers(tmp_path):
    d, main = _tree(tmp_path)
    assert refs.choose_aux(main, None) is None
    beside = _aux(d / "main.aux", "tab:design", "1")
    assert refs.choose_aux(main, None) == beside


# -------------------------------------------------------- nobody else chooses

_AUX_LITERAL = re.compile(r'with_suffix\(\s*[\'"]\.aux[\'"]\s*\)'
                          r'|[\'"][^\'"]*\.aux[\'"]\s*\)?\s*$')


def _strip_comments(text: str) -> str:
    text = re.sub(r'"""(?:.|\n)*?"""', "", text)
    text = re.sub(r"'''(?:.|\n)*?'''", "", text)
    return "\n".join(line.split("#", 1)[0] for line in text.splitlines())


def test_only_refs_decides_which_aux_a_document_reads():
    """A second derivation is the defect, not a missing one.

    The whole bug was two answers to one question, so a guard that only checked
    the chooser returns the right file would not have caught it: both answers
    were "right", they were just different files.
    """
    offenders = []
    for path in sorted(SRC.rglob("*.py")):
        if path.name == "refs.py":
            continue
        body = _strip_comments(path.read_text(encoding="utf-8"))
        for line in body.splitlines():
            if re.search(r'with_suffix\(\s*[\'"]\.aux[\'"]\s*\)', line):
                offenders.append(f"{path.relative_to(SRC.parent)}: {line.strip()}")
            elif re.search(r'\+\s*[\'"]\.aux[\'"]', line):
                offenders.append(f"{path.relative_to(SRC.parent)}: {line.strip()}")
    assert offenders == [], (
        "the path of a document's .aux is derived outside render/refs.py, which "
        "is how the page and the compile came to read different files: "
        + "; ".join(offenders))


# ------------------------------------------------------------- the build reads it


@pytest.mark.skipif(not HAS_PANDOC, reason="the build needs pandoc")
def test_the_page_shows_the_number_the_app_s_own_compile_wrote(tmp_path):
    """End to end: the number in the compile cache reaches the rendered page."""
    from manuscriptor.server import build as build_mod

    d, main = _tree(tmp_path)
    _aux(d / "main.aux", "tab:design", "1", age=600)
    _aux(paths.compile_dir(d) / "main.aux", "tab:design", "4")
    built = build_mod.build(d)
    assert "??" not in built.blob["html"]
    assert re.search(r"Table\s*4", built.blob["html"]), built.blob["html"][:400]


@pytest.mark.skipif(not HAS_PANDOC, reason="the build needs pandoc")
def test_the_build_prefers_the_author_s_fresher_aux(tmp_path):
    from manuscriptor.server import build as build_mod

    d, main = _tree(tmp_path)
    _aux(paths.compile_dir(d) / "main.aux", "tab:design", "4", age=600)
    _aux(d / "main.aux", "tab:design", "9")
    built = build_mod.build(d)
    assert re.search(r"Table\s*9", built.blob["html"]), built.blob["html"][:400]


# ------------------------------------------- a successful compile refreshes


def _session(tmp_path):
    from manuscriptor.server.app import Session

    d, main = _tree(tmp_path)
    _aux(paths.compile_dir(d) / "main.aux", "tab:design", "1")
    return Session(d)


def _run_compile(session, result, *, action="pdf"):
    """POST /compile with `compile_pdf` faked to return `result`."""
    from aiohttp.test_utils import TestClient, TestServer

    from manuscriptor.server.app import make_app

    sent = []

    async def capture(msg):
        sent.append(msg)

    session.broadcast = capture
    real = compile_mod.compile_pdf

    def fake(manuscript_dir, *, main=None, bib=None, on_step=None, **kw):
        return result

    compile_mod.compile_pdf = fake
    try:
        async def go():
            client = TestClient(TestServer(make_app(session)))
            await client.start_server()
            resp = await client.post("/compile", json={"action": action})
            assert resp.status == 202
            for _ in range(300):
                if any(m.get("phase") == "done" for m in sent):
                    break
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.05)
            await client.close()

        asyncio.run(go())
    finally:
        compile_mod.compile_pdf = real
    return sent


@pytest.mark.skipif(not HAS_PANDOC, reason="the session needs pandoc to build")
def test_a_successful_compile_redraws_the_page_with_the_new_numbers(tmp_path):
    """The author pressed Compile and the refs must move without him touching
    a source file. Nothing else can do it: the compile writes only into the
    hidden cache, which the tree watcher deliberately ignores."""
    session = _session(tmp_path)
    session.rebuild()
    assert "1" in session.blob["html"]

    # The compile "wrote" a new .aux, exactly as a real one would have.
    _aux(paths.compile_dir(session.root) / "main.aux", "tab:design", "4")
    ok = compile_mod.Result(
        kind="pdf", ok=True, output=paths.compile_dir(session.root) / "main.pdf",
        seconds=0.2, steps=[], error=None, log=None)
    _run_compile(session, ok)

    assert re.search(r"Table\s*4", session.blob["html"]), (
        "a successful compile left the open page on the numbers it had before, "
        "which is the defect: Compile can never resolve a \\ref")


@pytest.mark.skipif(not HAS_PANDOC, reason="the session needs pandoc to build")
def test_a_failed_compile_refreshes_nothing(tmp_path):
    session = _session(tmp_path)
    session.rebuild()
    calls = []
    real = session.on_change

    async def counted(**kw):
        calls.append(kw)
        return await real(**kw)

    session.on_change = counted
    bad = compile_mod.Result(
        kind="pdf", ok=False, output=None, seconds=0.2, steps=[],
        error="! Undefined control sequence.", log=None)
    _run_compile(session, bad)
    assert calls == [], (
        "a failed compile triggered a rebuild; its .aux is whatever the run "
        "died holding and must not reach the page")
