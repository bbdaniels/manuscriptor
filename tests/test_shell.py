"""Tests for the standalone macOS shell (`shell/Manuscriptor.app`).

These are pure Python and need no build. What they can and cannot reach:

  COVERED HERE
    - the manuscript-root rule, exhaustively, against a real reference
      implementation in `shell/resolve_root.py`
    - the banner-parsing rule that turns the server's stdout into a port
    - `Info.plist` really declaring `.tex`, so Finder can offer the app
    - `build.sh` existing and being executable
    - the Swift source still carrying the invariants that only exist as
      Swift (child terminated on quit, `--no-window --port 0`, unbuffered
      child stdout, `isInspectable`, frame autosave)

  COVERED BY THE BINARY CROSS-CHECK (runs once `shell/build.sh` has run)
    - the Swift resolution and the Python reference agreeing case for case.
      This is the guard against the two drifting apart; the source greps
      above only pin that the rule is still spelled out in Swift at all.

  NOT COVERED BY ANY TEST
    - that the window actually renders the manuscript. That needs a running
      app and is verified by screenshot.
"""

from __future__ import annotations

import importlib.util
import os
import plistlib
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SHELL = REPO / "shell"
PLIST = SHELL / "Resources" / "Info.plist"
BUILD_SH = SHELL / "build.sh"
INSTALL_SH = SHELL / "install.sh"
SOURCES = SHELL / "Sources" / "Manuscriptor"
APP_BIN = SHELL / "build" / "Manuscriptor.app" / "Contents" / "MacOS" / "Manuscriptor"


def _load_reference():
    spec = importlib.util.spec_from_file_location(
        "ms_resolve_root", SHELL / "resolve_root.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


rr = _load_reference()


DOC = "\\documentclass{article}\n\\begin{document}\nhi\n\\end{document}\n"
FRAG = "Some prose that is only ever \\input into something else.\n"


def _write(p: Path, text: str) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


# --------------------------------------------------------------- the cases
#
# One table, used by both the pure-Python tests and the binary cross-check,
# so the two can never test different things.


def case_main_beside(tmp: Path):
    """The ordinary shape: main.tex sits in the directory you opened."""
    _write(tmp / "paper" / "main.tex", DOC)
    return tmp / "paper" / "main.tex", tmp / "paper", "main.tex", "main.tex"


def case_nested_appendix(tmp: Path):
    """The reason the feature exists: an appendix fragment must not be served alone."""
    _write(tmp / "paper" / "main.tex", DOC)
    f = _write(tmp / "paper" / "appendix" / "e_data_details.tex", FRAG)
    return f, tmp / "paper", "main.tex", "appendix/e_data_details.tex"


def case_deeply_nested(tmp: Path):
    _write(tmp / "paper" / "main.tex", DOC)
    f = _write(tmp / "paper" / "a" / "b" / "c" / "frag.tex", FRAG)
    return f, tmp / "paper", "main.tex", "a/b/c/frag.tex"


def case_unique_documentclass(tmp: Path):
    """No main.tex, but exactly one .tex declares a documentclass."""
    _write(tmp / "paper" / "article.tex", DOC)
    f = _write(tmp / "paper" / "notes.tex", FRAG)
    return f, tmp / "paper", "article.tex", "notes.tex"


def case_main_beats_sibling_documentclass(tmp: Path):
    """main.tex wins even when a sibling also declares a documentclass."""
    _write(tmp / "paper" / "main.tex", DOC)
    _write(tmp / "paper" / "cover_letter.tex", DOC)
    f = _write(tmp / "paper" / "tables" / "t1.tex", FRAG)
    return f, tmp / "paper", "main.tex", "tables/t1.tex"


def case_ambiguous_documentclass(tmp: Path):
    """Two roots is not a root. Guessing one would serve the wrong paper."""
    (tmp / "repo" / ".git").mkdir(parents=True)
    _write(tmp / "repo" / "paper" / "a.tex", DOC)
    _write(tmp / "repo" / "paper" / "b.tex", DOC)
    f = _write(tmp / "repo" / "paper" / "frag.tex", FRAG)
    return f, tmp / "repo" / "paper", "", "frag.tex"


def case_commented_documentclass_does_not_count(tmp: Path):
    _write(tmp / "paper" / "dead.tex", "% \\documentclass{article}\n" + FRAG)
    _write(tmp / "paper" / "live.tex", DOC)
    f = _write(tmp / "paper" / "frag.tex", FRAG)
    return f, tmp / "paper", "live.tex", "frag.tex"


def case_only_commented_documentclass(tmp: Path):
    """A commented-out declaration leaves the directory with no root at all."""
    (tmp / "repo" / ".git").mkdir(parents=True)
    _write(tmp / "repo" / "paper" / "dead.tex", "% \\documentclass{article}\n" + FRAG)
    f = _write(tmp / "repo" / "paper" / "frag.tex", FRAG)
    return f, tmp / "repo" / "paper", "", "frag.tex"


def case_git_boundary(tmp: Path):
    """A main.tex outside the repository is somebody else's paper."""
    _write(tmp / "outer" / "main.tex", DOC)
    (tmp / "outer" / "repo" / ".git").mkdir(parents=True)
    f = _write(tmp / "outer" / "repo" / "sub" / "frag.tex", FRAG)
    return f, tmp / "outer" / "repo" / "sub", "", "frag.tex"


def case_git_root_is_itself_the_manuscript(tmp: Path):
    """Stopping AT the repository root means checking it, not skipping it."""
    (tmp / "repo" / ".git").mkdir(parents=True)
    _write(tmp / "repo" / "main.tex", DOC)
    f = _write(tmp / "repo" / "appendix" / "a.tex", FRAG)
    return f, tmp / "repo", "main.tex", "appendix/a.tex"


def case_directory_given(tmp: Path):
    """Opening the directory itself is the `serve` case, with nothing to jump to."""
    _write(tmp / "paper" / "main.tex", DOC)
    return tmp / "paper", tmp / "paper", "main.tex", ""


def case_orphan_fragment(tmp: Path):
    """Nothing above it is a manuscript: serve where it sits rather than nothing."""
    (tmp / "repo" / ".git").mkdir(parents=True)
    f = _write(tmp / "repo" / "loose" / "frag.tex", FRAG)
    return f, tmp / "repo" / "loose", "", "frag.tex"


def case_opened_document_is_the_document(tmp: Path):
    """A file that itself declares a documentclass IS the document, even beside
    a main.tex. estonia-ecm keeps `Highlights for JPubE.tex` next to the paper;
    opening it must serve the highlights, not the paper with a jump to a file
    the paper does not contain."""
    _write(tmp / "paper" / "main.tex", DOC)
    f = _write(tmp / "paper" / "highlights.tex", DOC)
    return f, tmp / "paper", "highlights.tex", "highlights.tex"


def case_opened_document_settles_an_ambiguous_directory(tmp: Path):
    """Two roots is not a root when walking up from a fragment, but opening one
    of the roots directly is not ambiguous at all."""
    (tmp / "repo" / ".git").mkdir(parents=True)
    f = _write(tmp / "repo" / "paper" / "a.tex", DOC)
    _write(tmp / "repo" / "paper" / "b.tex", DOC)
    return f, tmp / "repo" / "paper", "a.tex", "a.tex"


CASES = [
    case_main_beside,
    case_nested_appendix,
    case_deeply_nested,
    case_unique_documentclass,
    case_main_beats_sibling_documentclass,
    case_ambiguous_documentclass,
    case_commented_documentclass_does_not_count,
    case_only_commented_documentclass,
    case_git_boundary,
    case_git_root_is_itself_the_manuscript,
    case_directory_given,
    case_orphan_fragment,
    case_opened_document_is_the_document,
    case_opened_document_settles_an_ambiguous_directory,
]


@pytest.mark.parametrize("case", CASES, ids=[c.__name__ for c in CASES])
def test_manuscript_root_resolution(tmp_path, case):
    opened, want_root, want_main, want_rel = case(tmp_path)
    root, main, rel = rr.resolve(opened)
    # Both sides resolved: Finder hands over symlinked and aliased paths, and
    # /var is a symlink to /private/var on this platform.
    assert Path(root).resolve() == want_root.resolve()
    assert main == want_main
    assert rel == want_rel


def test_relative_path_uses_forward_slashes_and_is_relative(tmp_path):
    _write(tmp_path / "paper" / "main.tex", DOC)
    f = _write(tmp_path / "paper" / "appendix" / "e.tex", FRAG)
    _, _, rel = rr.resolve(f)
    assert rel == "appendix/e.tex"
    assert not rel.startswith("/")


def test_resolution_terminates_at_the_filesystem_root(tmp_path):
    """No .git, no home, nothing found: must stop rather than loop."""
    f = _write(tmp_path / "nowhere" / "frag.tex", FRAG)
    root, main, rel = rr.resolve(f)
    assert Path(root).resolve() == (tmp_path / "nowhere").resolve()
    assert (main, rel) == ("", "frag.tex")


def test_missing_path_is_an_error(tmp_path):
    with pytest.raises(FileNotFoundError):
        rr.resolve(tmp_path / "does" / "not" / "exist.tex")


# ------------------------------------------------------------ the banner
#
# The shell learns the port by reading it off the server's stdout. If this
# rule is wrong the window loads nothing at all.

PORT_CASES = [
    ("manuscriptor  http://127.0.0.1:64971/", 64971),
    ("manuscriptor  http://127.0.0.1:8801/   [read-only]", 8801),
    ("  384 blocks · 24 files · 92 citations · 2 computed values", None),
    ("", None),
    ("done -> /tmp/x/index.html", None),
    ("http://192.168.1.5:8801/", None),
    ("http://127.0.0.1:notaport/", None),
]


@pytest.mark.parametrize("line,want", PORT_CASES, ids=[repr(c[0])[:40] for c in PORT_CASES])
def test_port_is_read_off_the_banner(line, want):
    assert rr.parse_port(line) == want


# ----------------------------------------------------------- the bundle


def test_info_plist_exists_and_parses():
    assert PLIST.is_file(), f"missing {PLIST}"
    with PLIST.open("rb") as fh:
        plistlib.load(fh)


def _plist():
    with PLIST.open("rb") as fh:
        return plistlib.load(fh)


def test_info_plist_declares_tex_documents():
    """Without this, Finder never offers Manuscriptor in Open With."""
    types = _plist().get("CFBundleDocumentTypes")
    assert types, "no CFBundleDocumentTypes"
    exts = {e.lower() for t in types for e in t.get("CFBundleTypeExtensions", [])}
    assert "tex" in exts, f"tex not declared; found {sorted(exts)}"


def test_tex_document_type_is_an_editor_role():
    types = _plist()["CFBundleDocumentTypes"]
    tex = [t for t in types
           if "tex" in {e.lower() for e in t.get("CFBundleTypeExtensions", [])}]
    assert tex[0].get("CFBundleTypeRole") == "Editor"
    assert tex[0].get("LSHandlerRank") in {"Alternate", "Owner", "Default"}


def test_info_plist_has_the_keys_launchservices_needs():
    p = _plist()
    assert p.get("CFBundleExecutable") == "Manuscriptor"
    assert p.get("CFBundlePackageType") == "APPL"
    assert p.get("CFBundleIdentifier", "").count(".") >= 2
    assert p.get("NSHighResolutionCapable") is True
    assert p.get("LSMinimumSystemVersion")
    # A plain window app, not a background agent.
    assert p.get("LSUIElement") in (None, False)


def test_build_script_exists_and_is_executable():
    assert BUILD_SH.is_file(), f"missing {BUILD_SH}"
    assert os.access(BUILD_SH, os.X_OK), f"{BUILD_SH} is not executable"


def test_build_script_produces_the_documented_bundle():
    text = BUILD_SH.read_text(encoding="utf-8")
    assert text.startswith("#!"), "no shebang"
    assert "build/Manuscriptor.app" in text
    assert "Info.plist" in text


def test_install_script_present_and_executable():
    assert INSTALL_SH.exists(), "shell/install.sh must exist"
    assert os.access(INSTALL_SH, os.X_OK), "shell/install.sh must be executable"
    body = INSTALL_SH.read_text()
    assert "build.sh" in body, "install must build first"
    assert "/Applications" in body, "install must copy into /Applications"


# ------------------------------------------------- invariants only Swift holds
#
# These are greps. They cannot prove the behaviour; they exist so that
# deleting the behaviour cannot pass silently. The behaviour itself is
# verified by running the app.


def _swift_source() -> str:
    assert SOURCES.is_dir(), f"missing {SOURCES}"
    files = sorted(SOURCES.glob("*.swift"))
    assert files, "no Swift sources"
    return "\n".join(f.read_text(encoding="utf-8") for f in files)


def test_swift_still_spells_out_the_root_rule():
    src = _swift_source()
    for marker in ('"main.tex"', "documentclass", '".git"'):
        assert marker in src, f"root rule lost its {marker} branch"


def test_swift_owns_the_server_child():
    """A server outliving its window is a process quietly holding a paper open."""
    src = _swift_source()
    assert '"--no-window"' in src
    assert '"--port"' in src and '"0"' in src
    assert "terminate()" in src, "child is never terminated"
    # Python block-buffers a piped stdout, so the banner never arrives and the
    # window loads nothing. Verified empirically: 12s with no output.
    assert "PYTHONUNBUFFERED" in src


def test_swift_keeps_the_web_inspector_and_the_window_frame():
    src = _swift_source()
    assert "isInspectable" in src
    assert "setFrameAutosaveName" in src or "frameAutosaveName" in src


def test_swift_jumps_to_the_opened_file():
    """Serving the root is only half of it; the page has to land on the file."""
    src = _swift_source()
    assert "MSViewer" in src


def test_recents_invariants():
    src = (SOURCES / "AppDelegate.swift").read_text()
    assert "RecentManuscripts" in src, "recents must use a dedicated UserDefaults key"
    assert "func pushRecent" in src and "func recents" in src
    assert "Open Recent" in src, "File menu must offer Open Recent"
    # bounded so the list cannot grow without limit
    assert "prefix(" in src, "recents must be bounded"


def test_home_screen_invariants():
    src = (SOURCES / "AppDelegate.swift").read_text()
    home = SHELL / "Resources" / "home.html"
    assert home.exists(), "bundled home.html must exist"
    assert "WKScriptMessageHandler" in src
    assert "manuscriptor" in home.read_text() or "ms.open" in home.read_text()
    # cold open loads the home, not a bare NSOpenPanel
    assert "loadHome" in src, "cold open must present the home surface"


# ------------------------------------------------- the Swift/Python parity check


needs_build = pytest.mark.skipif(
    not APP_BIN.exists(), reason=f"not built: run {BUILD_SH}"
)


@needs_build
@pytest.mark.parametrize("case", CASES, ids=[c.__name__ for c in CASES])
def test_swift_resolution_matches_the_reference(tmp_path, case):
    opened, want_root, want_main, want_rel = case(tmp_path)
    out = subprocess.run(
        [str(APP_BIN), "--resolve-root", str(opened)],
        capture_output=True, text=True, timeout=30,
    )
    assert out.returncode == 0, out.stderr
    got_root, got_main, got_rel = out.stdout.rstrip("\n").split("\t")
    assert Path(got_root).resolve() == want_root.resolve()
    assert got_main == want_main
    assert got_rel == want_rel


@needs_build
@pytest.mark.parametrize("line,want", PORT_CASES, ids=[repr(c[0])[:40] for c in PORT_CASES])
def test_swift_banner_parsing_matches_the_reference(line, want):
    out = subprocess.run(
        [str(APP_BIN), "--parse-port", line],
        capture_output=True, text=True, timeout=30,
    )
    got = out.stdout.strip()
    assert (int(got) if got else None) == want, out.stderr
