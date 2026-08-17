r"""A figure that appears AFTER the LaTeX that names it.

The order the author works in is: write `\includegraphics{f-did-components}`,
then run the script that exports it. Between those two moments the include
resolves to nothing -- `graphics.find` is asked for a file that is not on disk
yet -- and `resolve_includes` leaves the argument exactly as written, on purpose,
so the missing-asset reporting can still name what the manuscript says. The page
then holds `<img src="f-did-components">`, which is not a path to anything and
404s at the assets route.

That is correct until the file lands. What was wrong is what happened next, and
it was two things.

FIRST, THE FILE LANDING WAS NOT AN EVENT. `\graphicspath{{../outputs/}{figures/}}`
is qutub-ayush's, and `../outputs/` is OUTSIDE the manuscript directory --
outside the only tree `serve` watches. So a figure exported into it changed
nothing: no rebuild, no re-resolution, no staging. The broken image survived
every reload, and would have survived until an unrelated `.tex` edit happened to
trigger a rebuild. Observed live on 2026-08-17: the server booted at 11:41, the
figure was exported at 15:03, and at 15:05 the page still read
`<img src="f-did-components">` while every other figure on it resolved.

SECOND, AN ASSET CHANGE REDREW NOTHING. `on_assets_change` rebuilt and then told
the clients only to refetch their images -- right for a figure regenerated under
LaTeX that did not move, and useless here, because what changed is the `src`
attribute itself. The rebuilt HTML was correct on the server and the open page
kept the broken element. This half fires for an in-tree figure too, so the same
bug was reachable with no `\graphicspath` at all.

The block-render path is not implicated and never was: `pandoc.render_block` is
handed the document preamble (that is what its `preamble=` argument is for), and
the server does not call it at all -- every redraw is a full build.
"""
from __future__ import annotations

import asyncio
import shutil
import time
from pathlib import Path

import pytest

from manuscriptor.render import graphics, postprocess
from manuscriptor.server import app as app_mod

HAS_PANDOC = shutil.which("pandoc") is not None

DOC = r"""\documentclass{article}
\graphicspath{{../outputs/}{figures/}}
\begin{document}
A paragraph of prose standing above the figure, long enough to be a real block.

\begin{figure}
\centering
\includegraphics[width=0.95\textwidth]{f-did-components}
\caption{Component outcomes by trial arm and round.}
\end{figure}

A paragraph of prose standing below it, also long enough to be its own block.
\end{document}
"""


def _settle(predicate, timeout: float = 6.0) -> bool:
    """fsevents is not synchronous; poll rather than sleep a magic number."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def _project(tmp_path: Path) -> tuple[Path, Path]:
    """qutub-ayush's shape: `manuscript/` beside `outputs/`, figures in both."""
    manuscript = tmp_path / "manuscript"
    outputs = tmp_path / "outputs"
    (manuscript / "figures").mkdir(parents=True)
    outputs.mkdir()
    (manuscript / "main.tex").write_text(DOC, encoding="utf-8")
    return manuscript, outputs


PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c6360000002000100fbd0e0ff0000000049454e44ae426082"
)


# ------------------------------------------------------- where a render looks


def test_search_dirs_names_every_directory_a_render_reads_figures_from(tmp_path):
    r"""`\graphicspath` first, in its own order, then the manuscript itself.

    The same list `find` walks. A watcher needs it because a file appearing in
    one of these directories changes what an include resolves to, and only this
    module knows which directories those are.
    """
    manuscript, outputs = _project(tmp_path)
    dirs = graphics.search_dirs(DOC, manuscript)
    assert dirs == [outputs.resolve(), (manuscript / "figures").resolve(),
                    manuscript.resolve()], dirs


def test_search_dirs_passes_over_a_directory_that_is_not_there(tmp_path):
    """A `\\graphicspath` entry naming nothing is not a directory to watch."""
    manuscript, _outputs = _project(tmp_path)
    (manuscript / "main.tex").write_text(
        DOC.replace("{figures/}", "{nowhere/}"), encoding="utf-8")
    dirs = graphics.search_dirs(
        (manuscript / "main.tex").read_text(encoding="utf-8"), manuscript)
    assert all(d.is_dir() for d in dirs), dirs
    assert not any(d.name == "nowhere" for d in dirs), dirs


def test_search_dirs_of_a_document_with_no_graphicspath_is_the_manuscript(tmp_path):
    manuscript, _outputs = _project(tmp_path)
    assert graphics.search_dirs("\\documentclass{article}\n", manuscript) == [
        manuscript.resolve()]


# ------------------------------------------------- the directories serve watches


@pytest.mark.skipif(not HAS_PANDOC, reason="the session needs pandoc to build")
def test_the_session_names_the_figure_directories_outside_its_own_tree(tmp_path):
    """`figures/` is already watched by the tree watch; `../outputs/` is not."""
    manuscript, outputs = _project(tmp_path)
    session = app_mod.Session(manuscript, auto_compile=False)
    assert session.external_figure_dirs == [outputs.resolve()], (
        session.external_figure_dirs)


@pytest.mark.skipif(not HAS_PANDOC, reason="the session needs pandoc to build")
def test_a_figure_exported_outside_the_manuscript_reaches_the_watcher(tmp_path):
    """The event the whole repair hangs on, measured at the filesystem.

    Asserted through a real export into a real directory, because the failure was
    precisely that no event existed: every layer above this behaved correctly on
    a batch it was never handed.
    """
    manuscript, outputs = _project(tmp_path)
    session = app_mod.Session(manuscript, auto_compile=False)
    batches: list[set[Path]] = []
    watches = app_mod.watch_figure_dirs(session, batches.append, debounce_ms=20)
    try:
        watches.rearm()
        assert _settle(lambda: True, 0.4)          # let the emitter come up
        (outputs / "f-did-components.png").write_bytes(PNG)
        assert _settle(lambda: bool(batches)), (
            "a figure exported into the manuscript's own `\\graphicspath` "
            "directory produced no event at all, because the directory is "
            "outside the tree `serve` watches")
    finally:
        watches.stop()
    assert {p.name for b in batches for p in b} == {"f-did-components.png"}


@pytest.mark.skipif(not HAS_PANDOC, reason="the session needs pandoc to build")
def test_the_figure_watches_follow_a_rebuild(tmp_path):
    """A rebuild can change the search list, so it re-arms the watches.

    A document switch is the case that matters: the new document has its own
    `\\graphicspath`, and a watch armed once at boot would be watching the old
    one's directories forever.
    """
    manuscript, outputs = _project(tmp_path)
    other = tmp_path / "elsewhere"
    other.mkdir()
    session = app_mod.Session(manuscript, auto_compile=False)
    watches = app_mod.watch_figure_dirs(session, lambda _b: None, debounce_ms=20)
    try:
        watches.rearm()
        assert outputs.resolve() in watches.held()
        (manuscript / "main.tex").write_text(
            DOC.replace("{../outputs/}", "{../elsewhere/}"), encoding="utf-8")
        session.rebuild()
        assert watches.held() == [other.resolve()], (
            f"{watches.held()}: the watches did not follow the rebuild")
    finally:
        watches.stop()
    assert watches.held() == []


# ------------------------------------------------------- what the page is told


def _sent(session, coro) -> list[dict]:
    sent: list[dict] = []

    async def capture(msg):
        sent.append(msg)

    session.broadcast = capture
    asyncio.run(coro(session))
    return sent


@pytest.mark.skipif(not HAS_PANDOC, reason="the session needs pandoc to build")
def test_the_appearing_figure_reaches_the_open_page(tmp_path):
    """The block is PATCHED, and the file it now names is servable.

    Two assertions because the bug had two halves and either alone still shows
    the author a broken image: the `src` on the open page has to become the
    staged name, and something has to have staged the bytes under it.
    """
    manuscript, outputs = _project(tmp_path)
    session = app_mod.Session(manuscript, auto_compile=False)
    assert 'src="f-did-components"' in session.blob["html"], (
        "the fixture is not in the state the bug needs: the include resolved "
        "before the figure was exported")

    (outputs / "f-did-components.png").write_bytes(PNG)
    sent = _sent(session, lambda s: s.on_assets_change())

    staged = graphics.staged_rel(outputs / "f-did-components.png", manuscript)
    assert staged.startswith(graphics.EXTERNAL_PREFIX + "/"), staged
    patched = " ".join(
        str(h) for m in sent if m.get("type") == "patch"
        for h in _patch_html(m))
    assert staged in patched, (
        f"the page was sent {[m.get('type') for m in sent]} and no block HTML "
        f"carrying {staged!r}: the server resolved the figure and the open page "
        "kept the broken element")
    assert any(m.get("type") == "assets" for m in sent), (
        "and the images the page already holds still need refetching")
    assert (session.asset_root / staged).is_file(), (
        "nothing staged the resolved figure, so its `src` 404s just as loudly "
        "as the unresolved one did")


def _patch_html(msg: dict) -> list[str]:
    """The block HTML a patch frame carries: replacements, and new blocks."""
    return ([str(v) for v in (msg.get("blocks") or {}).values()]
            + [str(a.get("html", "")) for a in (msg.get("added") or [])])


@pytest.mark.skipif(not HAS_PANDOC, reason="the session needs pandoc to build")
def test_a_source_change_still_redraws_and_still_resolves(tmp_path):
    """The full-document path is unchanged: it always resolved, and still does."""
    manuscript, outputs = _project(tmp_path)
    (outputs / "f-did-components.png").write_bytes(PNG)
    session = app_mod.Session(manuscript, auto_compile=False)
    staged = graphics.staged_rel(outputs / "f-did-components.png", manuscript)
    assert f'src="{staged}"' in session.blob["html"]

    p = manuscript / "main.tex"
    p.write_text(p.read_text(encoding="utf-8").replace(
        "standing above the figure", "standing above the figure, now edited"),
        encoding="utf-8")
    sent = _sent(session, lambda s: s.on_change())
    assert any(m.get("type") == "patch" for m in sent), [m.get("type") for m in sent]
    assert f'src="{staged}"' in session.blob["html"]


@pytest.mark.skipif(not HAS_PANDOC, reason="the session needs pandoc to build")
def test_watching_a_figure_directory_does_not_authorize_reading_it(tmp_path):
    """The route's rule is unchanged: only a build stages an external figure.

    Watching `../outputs/` is an instruction about when to REBUILD. It is not
    permission for a browser to name a file out there and be handed it, which is
    what `refresh_asset` refuses and must go on refusing.
    """
    manuscript, outputs = _project(tmp_path)
    session = app_mod.Session(manuscript, auto_compile=False)
    secret = tmp_path / "secret.png"
    secret.write_bytes(PNG)
    rel = graphics.staged_rel(secret, manuscript)
    assert postprocess.refresh_asset(rel, manuscript, session.asset_root) is False
    assert not (session.asset_root / rel).exists()
    assert graphics.source_path("../../secret.png", manuscript) is None
    assert outputs.exists()
