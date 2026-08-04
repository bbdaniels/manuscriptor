"""`--read-only` means nothing reaches the author's filesystem.

The promise was made in the CLI's own refusal text ("the comment log is not
written either") and enforced only at the write handlers. Everything BELOW them
kept writing: `build()` took no read-only flag, so it called `paths.ensure` and
rendered into `.manuscriptor/cache/` on every serve, rasterized figures into it,
cached the value manifest in it, and a compile put its `.aux` and `.log` there
too. Opening somebody's paper to read it created a directory inside their
working tree.

The redirect belongs in `paths`, because that module answers "where does
Manuscriptor keep its files" and a read-only serve is that question with a
different answer -- not path arithmetic sprinkled through `build`. So `home()`
answers with a scratch directory under the system temp when the serve is
read-only, and everything derived from it (the cache, the compile directory)
follows without knowing why.

The tiers that are READ rather than written -- the comment log, the drafts
store, the drain's feed -- keep pointing at the manuscript. A reader must still
see the review record; the writes to it are refused at the handlers, which is
where a refusal can say so.
"""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

import pytest

from manuscriptor.server import build as build_mod
from manuscriptor.server import paths

DOC = """\\documentclass{article}
\\begin{document}
\\section{One}
A paragraph of prose that the reader is only reading.

Another paragraph, so the document has more than one block in it.
\\end{document}
"""


def manuscript(tmp_path: Path) -> Path:
    (tmp_path / "main.tex").write_text(DOC, encoding="utf-8")
    return tmp_path


def git(d: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=d, capture_output=True, text=True).stdout


def repo(tmp_path: Path) -> Path:
    d = tmp_path / "paper"
    d.mkdir(parents=True)
    manuscript(d)
    git(d, "init", "-q")
    git(d, "add", "main.tex")
    git(d, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "in")
    return d


def tree(d: Path) -> dict:
    """Every file the author owns, by content. `.git` excluded: reading a repo
    does not touch the object store, and index mtimes are not the author's."""
    return {p.relative_to(d): p.read_bytes()
            for p in sorted(d.rglob("*")) if p.is_file() and ".git" not in p.parts}


# --------------------------------------------------------------- where it writes


def test_a_read_only_build_writes_nothing_into_the_manuscript(tmp_path):
    manuscript(tmp_path)
    before = tree(tmp_path)

    build_mod.build(tmp_path, read_only=True)

    assert tree(tmp_path) == before
    assert not paths.home(tmp_path).exists(), (
        "a read-only build created the hidden directory it promised not to")


def test_a_read_only_build_renders_into_the_system_temp(tmp_path):
    manuscript(tmp_path)
    out = paths.cache(tmp_path, read_only=True)

    assert out.is_relative_to(Path(tempfile.gettempdir()).resolve())
    assert not out.is_relative_to(tmp_path)
    # And it is the SAME directory on the second ask, or a build and the route
    # serving its assets would be looking at two different caches.
    assert paths.cache(tmp_path, read_only=True) == out


def test_the_scratch_directory_is_cleaned_up_at_exit(tmp_path):
    manuscript(tmp_path)
    out = paths.cache(tmp_path, read_only=True)
    paths.ensure(tmp_path, read_only=True)
    assert out.is_dir()

    paths.drop_scratch()
    assert not out.exists(), "the scratch directory outlived the process"


def test_a_normal_build_still_writes_the_hidden_directory(tmp_path):
    """The redirect must not become the only behavior. The read-write serve is
    the default and its layout is the one every other test asserts."""
    manuscript(tmp_path)
    build_mod.build(tmp_path)
    assert paths.cache(tmp_path).is_dir()
    assert paths.cache(tmp_path, read_only=False) == paths.cache(tmp_path)


def test_the_rendered_page_is_the_same_page_either_way(tmp_path):
    """The redirect moves where the render lands, and nothing about what it is."""
    manuscript(tmp_path)
    ro = build_mod.build(tmp_path, read_only=True)
    rw = build_mod.build(tmp_path)
    assert ro.blob["html"] == rw.blob["html"]
    assert list(ro.blob["blocks"]) == list(rw.blob["blocks"])


# ------------------------------------------------------------- through a serve


def session(d: Path, **kw):
    from manuscriptor.server.app import Session

    return Session(d, **kw)


def test_a_read_only_serve_leaves_git_status_alone(tmp_path):
    d = repo(tmp_path)
    before = tree(d)

    s = session(d, read_only=True)
    s.rebuild()

    assert git(d, "status", "--porcelain") == "", (
        "a read-only serve made the author's working tree dirty")
    assert tree(d) == before
    assert not paths.home(d).exists()


def test_a_whole_read_only_serve_leaves_the_repository_untouched(tmp_path):
    """The real thing, end to end: the session, the build, the render, the
    watchers and the HTTP server, against a git working tree.

    The watchers are the reason this exists rather than being covered by the
    session test above. `watch_file` MAKES the directory it watches, because the
    drain's feed does not exist until a drain first runs -- so arming the feed
    and history watches created `.manuscriptor/agent/` inside the author's
    repository on a serve that had refused every write above it.
    """
    import asyncio
    import threading

    from manuscriptor.server import app as app_mod

    d = repo(tmp_path)
    before = tree(d)

    class Brief(asyncio.Event):
        """The serve loop waits on one of these forever. This one waits long
        enough for the watchers to arm and a page to be fetched."""

        async def wait(self):
            await asyncio.sleep(1.0)

    real_event = asyncio.Event
    asyncio.Event = Brief
    try:
        thread = threading.Thread(
            target=app_mod.serve,
            args=(d,),
            kwargs=dict(port=0, open_window=False, read_only=True),
            daemon=True)
        thread.start()
        thread.join(timeout=30)
    finally:
        asyncio.Event = real_event

    assert not thread.is_alive(), "the serve never came down"
    assert git(d, "status", "--porcelain") == "", (
        "a read-only serve made the author's working tree dirty")
    assert tree(d) == before, "a read-only serve wrote into the manuscript"
    assert not paths.home(d).exists(), "nor created the hidden directory"


def test_a_read_only_serve_serves_its_assets_from_the_scratch_cache(tmp_path):
    """The asset route reads the cache the build wrote. Pointed at the hidden
    directory on a read-only serve it would 404 every figure, because the build
    put them somewhere else."""
    d = repo(tmp_path)
    s = session(d, read_only=True)
    assert s.asset_root == paths.cache(d, read_only=True)
    assert not s.asset_root.is_relative_to(d)

    rw = session(d)
    assert rw.asset_root == paths.cache(d)


def test_a_read_only_serve_starts_no_drain(tmp_path):
    """`--read-only` implies no agent, and the claim on the queue is a file in
    the manuscript's own hidden directory: taking it would be a write."""
    from manuscriptor import cli

    d = repo(tmp_path)
    started = []

    class Stub:
        """A claim that records being taken and is otherwise usable, so the
        mutation that removes the read-only condition fails on the assertion
        below rather than on the stub being too thin to run."""

        fd = None
        degraded = False

        def __init__(self, root):
            started.append(Path(root))

        def acquire(self):
            started.append("acquired")
            return True

        def release(self):
            started.append("released")

    import manuscriptor.server.single as single

    served = {}

    def fake_serve(directory, **kw):
        served.update(kw)
        return None

    import manuscriptor.server.app as app_mod

    def fake_agent(root, lock=None):
        started.append("started")
        return None, None

    old = (single.DrainLock, app_mod.serve, cli.start_agent)
    single.DrainLock, app_mod.serve, cli.start_agent = Stub, fake_serve, fake_agent
    try:
        cli.main(["serve", str(d), "--read-only", "--no-window"])
    finally:
        single.DrainLock, app_mod.serve, cli.start_agent = old

    assert started == [], "a read-only serve claimed the comment queue"
    assert served.get("read_only") is True
    assert not paths.drain_lock(d).exists()
    assert not paths.agent_dir(d).exists()


def test_a_read_only_serve_still_reads_the_authors_record(tmp_path):
    """The redirect is for what Manuscriptor WRITES. The review record and the
    unsaved drafts are read from the manuscript, or opening a paper read-only
    would show an empty rail over a queue full of comments."""
    d = repo(tmp_path)
    paths.ensure(d)
    paths.comments(d).write_text(
        '{"id": "c-0001", "kind": "comment", "doc": "main.tex", '
        '"body": "tighten this", "quote": "A paragraph of prose"}\n',
        encoding="utf-8")

    s = session(d, read_only=True)
    assert s.log == paths.comments(d)
    assert any(msgs for msgs in s.blob["chats"].values()), (
        "a read-only serve lost the author's comments")


def test_a_read_only_build_carries_the_verdicts_from_the_authors_own_cache(tmp_path):
    """The evidence records are READ, so they are read where the author's are.

    A read-only build renders into a scratch directory that no evidence pass has
    ever written into, so reading the verdicts from the render's own output
    would blank every citation underline the author has already earned. This is
    the deviation the comment at `build()` argues for, and it was unguarded:
    pointing the read at the render directory left the whole suite green.
    """
    import json

    d = manuscript(tmp_path)
    out = paths.cache(d)
    out.mkdir(parents=True, exist_ok=True)
    (out / "citations.json").write_text(json.dumps([
        {"cite_key": "croke2026sickness", "title": "In Sickness and In Health",
         "authors": ["Croke"], "year": 2026, "journal": "JDE",
         "doi": "10.1016/x", "zotero_key": "K1", "has_fulltext": True,
         "fulltext_source": "zotero-indexed", "fulltext_chars": 48000},
    ]), encoding="utf-8")

    ro = build_mod.build(d, read_only=True)
    assert "croke2026sickness" in ro.blob["cites"], (
        "a read-only build read its verdicts out of the scratch directory, "
        f"where no evidence pass has ever written: {ro.blob['cites']}")
    assert ro.blob["cites"]["croke2026sickness"]["journal"] == "JDE"


def test_an_explicit_output_directory_is_where_the_verdicts_are_read_from(tmp_path):
    """A caller that says where the build lands says where its records live.

    The redirect above exists for the read-only case, which passes no
    `output_dir` at all. Applying it unconditionally made an explicit
    `output_dir` a lie for half of what the build reads, and pointed the read at
    a `.manuscriptor/` the caller may have been avoiding on purpose.
    """
    import json

    d = manuscript(tmp_path)
    elsewhere = tmp_path / "build-here"
    elsewhere.mkdir()
    (elsewhere / "citations.json").write_text(json.dumps([
        {"cite_key": "onlyhere2026", "title": "Named Only In The Given Directory",
         "authors": ["A"], "year": 2026, "journal": "J", "doi": "10.1/y",
         "zotero_key": "K2", "has_fulltext": False, "fulltext_source": None,
         "fulltext_chars": 0},
    ]), encoding="utf-8")
    (elsewhere / "missing.json").write_text(json.dumps([{"cite_key": "onlyhere2026"}]),
                                            encoding="utf-8")

    b = build_mod.build(d, output_dir=elsewhere)
    assert "onlyhere2026" in b.blob["cites"], (
        "the build ignored the output directory it was given and read the "
        f"manuscript's own cache instead: {b.blob['cites']}")
    assert b.blob["missing_fulltexts"] == 1


# ---------------------------------------------------------------- the compile


def test_a_read_only_compile_writes_no_aux_beside_the_tex(tmp_path):
    from manuscriptor.server import compile as compile_mod

    d = repo(tmp_path)
    out = compile_mod.out_dir(d, read_only=True)
    assert not out.is_relative_to(d)
    assert out.is_relative_to(paths.home(d, read_only=True))
    assert not paths.home(d).exists()
    assert compile_mod.out_dir(d) == paths.compile_dir(d)
