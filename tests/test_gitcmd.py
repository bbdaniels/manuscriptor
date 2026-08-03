"""Bytes out of git are not guaranteed UTF-8, and a history panel is not worth
a crash.

Switching documents in the running editor died with

    'utf-8' codec can't decode byte 0xb7 in position 24471: invalid start byte

raised inside `subprocess.run(..., text=True)` in `manifest._git`, propagating
out of `Session.rebuild()` because the wrapper caught only `OSError` and
`subprocess.SubprocessError`. Two separate faults produced it, and both are
tested here:

* `_git_history` handed git the PARENT DIRECTORIES of the fragment files, so a
  committed PDF figure sitting beside them was inlined into a `log -p` diff.
  `%\\xb7\\xbe\\xad\\xaa` on line 2 is the conventional "this file has high
  bytes" marker every PDF writer emits, and git does not classify a small one as
  binary.
* the decode was strict.

The pathspec fix alone would leave the decode armed for the next non-UTF-8 byte
in a commit message or a Latin-1 fragment, and the decode fix alone would leave
every history rebuild dragging whole PDFs through a pipe. Both.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from manuscriptor.server import gitcmd, manifest

SRC = Path(__file__).resolve().parent.parent / "manuscriptor"

# What a PDF writer puts on line 2 so a transport that mangles high bytes is
# caught early. 0xb7 is the byte the live crash named.
PDF_HEAD = b"%PDF-1.3\n%\xb7\xbe\xad\xaa\n"


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, check=True)


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "exhibits").mkdir(parents=True)
    _git(root.parent, "init", "-q", str(root))
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    return root


def _commit(root: Path, message: str) -> None:
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", message)


def _manuscript_with_a_committed_pdf(tmp_path: Path) -> tuple[Path, Path]:
    """A fragment and a PDF figure in one directory, both committed.

    The filler matters: git sniffs only the first 8000 bytes for a NUL, and a
    file it calls binary is summarised as "Binary files differ" rather than
    inlined. A real `sf2-attrition.pdf` has no NUL that early, so neither does
    this one -- the point is to reproduce what git actually did, not to assert
    that a contrived file breaks it.
    """
    root = _repo(tmp_path)
    frag = root / "exhibits" / "correct_p2.tex"
    frag.write_text("0.41\n", encoding="utf-8")
    (root / "exhibits" / "sf2-attrition.pdf").write_bytes(
        PDF_HEAD + b"1 0 obj <</Type /Page>> endobj\n" * 400)
    _commit(root, "first draft of the attrition figure")

    frag.write_text("0.44\n", encoding="utf-8")
    _commit(root, "reweight round 2")
    return root, frag


# --------------------------------------------------- the crash, end to end


def test_a_committed_pdf_beside_a_fragment_does_not_kill_the_rebuild(tmp_path):
    root, frag = _manuscript_with_a_committed_pdf(tmp_path)

    out = manifest.describe(root, [frag], repo=root)

    entry = out["correct_p2"]
    assert entry["value"] == "0.41" or entry["value"] == "0.44"
    values = [h["value"] for h in entry["history"]]
    assert "0.44" in values, entry
    # And nothing from the PDF leaked into the panel.
    assert not any("PDF" in v or "obj" in v for v in values), values


def test_history_survives_a_commit_message_that_is_not_utf8(tmp_path):
    """The decode has to hold even when the pathspecs are perfect."""
    root, frag = _manuscript_with_a_committed_pdf(tmp_path)
    frag.write_text("0.47\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "-c", "i18n.commitEncoding=ISO-8859-1",
         "commit", "-qm", "caf\xe9 rounding".encode("latin-1").decode("latin-1"))

    out = manifest.describe(root, [frag], repo=root)
    assert "0.47" in [h["value"] for h in out["correct_p2"]["history"]]


# ----------------------------------------------- ask git the right question


def test_git_is_never_asked_for_the_pdf(tmp_path, monkeypatch):
    """Per-file pathspecs, not the directory the file happens to sit in."""
    root, frag = _manuscript_with_a_committed_pdf(tmp_path)
    seen: list[list[str]] = []
    real = gitcmd.run

    def spy(args, **kw):
        seen.append(list(args))
        return real(args, **kw)

    monkeypatch.setattr(gitcmd, "run", spy)
    manifest.describe(root, [frag], repo=root)

    logs = [a for a in seen if "log" in a]
    assert logs, seen
    for args in logs:
        after = args[args.index("--") + 1:]
        assert after == ["exhibits/correct_p2.tex"], after
        assert not any(a.endswith(".pdf") for a in after), after
        assert "exhibits" not in after, after


def test_the_pdf_bytes_are_not_in_what_git_returns(tmp_path):
    root, frag = _manuscript_with_a_committed_pdf(tmp_path)
    out = manifest._git_history(root, root, {
        "correct_p2": {"path": "exhibits/correct_p2.tex"}})
    assert out.get("correct_p2"), out
    assert not any("PDF" in h["value"] for h in out["correct_p2"])


# ------------------------------------------------------- the helper itself


def test_the_helper_replaces_undecodable_bytes_instead_of_raising(tmp_path):
    root = _repo(tmp_path)
    (root / "f.tex").write_bytes(b"caf\xe9\n")
    _commit(root, "latin-1 fragment")

    done = gitcmd.run(["log", "-p", "--", "f.tex"], cwd=root)
    assert done is not None
    assert done.returncode == 0
    assert "�" in done.stdout


def test_the_helper_reports_failure_rather_than_raising(tmp_path):
    done = gitcmd.run(["rev-parse", "HEAD"], cwd=tmp_path / "nowhere")
    assert done is None or done.returncode != 0


def test_the_helper_takes_text_on_stdin(tmp_path):
    """`tidy` feeds candidate paths to `git check-ignore --stdin`."""
    root = _repo(tmp_path)
    (root / ".gitignore").write_text("*.log\n", encoding="utf-8")
    done = gitcmd.run(["check-ignore", "--stdin"], cwd=root, input="a.log\nb.tex\n")
    assert done is not None
    assert done.stdout.splitlines() == ["a.log"]


# ------------------------------------------------------------------ the guard
#
# In the style of tests/test_paths.py. That guard was written matching double
# quotes only and never fired once in months; this one is watched failing
# against a planted violation below before it is trusted.


def _strip_comments(text: str) -> str:
    text = re.sub(r'"""(?:.|\n)*?"""', "", text)
    text = re.sub(r"'''(?:.|\n)*?'''", "", text)
    return "\n".join(line.split("#", 1)[0] for line in text.splitlines())


def _squash(body: str) -> str:
    """Drop quotes, string prefixes and whitespace, so quoting cannot hide a call.

    `["git", "-C", ...]`, `['git', ...]`, `[f"git"]` and `("git",)` all collapse
    to a body containing `[git,` or `(git,`.
    """
    return re.sub(
        r"""(?<![A-Za-z0-9_])[rbufRBUF]{1,2}['"]|['"]|\s+""", "", body)


GIT_CALL = re.compile(r"[\[(]git[,\])]")
TEXT_TRUE = "text=True"


def _sources() -> list[Path]:
    return sorted(p for p in SRC.rglob("*.py") if p.name != "gitcmd.py")


def test_only_gitcmd_invokes_git():
    offenders = [p.relative_to(SRC.parent) for p in _sources()
                 if GIT_CALL.search(_squash(_strip_comments(p.read_text("utf-8"))))]
    assert offenders == [], (
        "git is invoked outside server/gitcmd.py: "
        f"{[str(p) for p in offenders]}")


def test_nothing_decodes_a_subprocess_strictly():
    """`text=True` is how the crash got out, and it is banned package-wide.

    Not only for git. Every one of these reads bytes it did not write -- a TeX
    log naming a font in cp1252, `pdftotext` over a PDF with a Latin-1 title,
    pandoc's stderr, `zotero-cli` -- and `text=True` turns any of them into a
    `UnicodeDecodeError` that no `except (OSError, SubprocessError)` will catch.

    The sanctioned spellings are `encoding="utf-8", errors="replace"` for a call
    whose output is read as text (which is `text=True` with the strictness taken
    off, and keeps `bufsize=1` line buffering working for the supervisor's
    stream), or plain `capture_output=True` with no decode at all where only the
    return code is read. `gitcmd` itself decodes explicitly.
    """
    offenders = [p.relative_to(SRC.parent) for p in _sources()
                 if TEXT_TRUE in _squash(_strip_comments(p.read_text("utf-8")))]
    assert offenders == [], (
        "a subprocess is decoded strictly outside server/gitcmd.py: "
        f"{[str(p) for p in offenders]}")


@pytest.mark.parametrize("spelling", [
    'subprocess.run(["git", "-C", str(root)] + args)',
    "subprocess.run(['git', 'log'], cwd=root)",
    'subprocess.run(("git", "status"))',
    'subprocess.run([ "git" , "mv" ])',
    'subprocess.Popen(["git"])',
])
def test_the_git_guard_is_not_defeated_by_how_the_call_is_quoted(spelling):
    assert GIT_CALL.search(_squash(spelling)), spelling


@pytest.mark.parametrize("innocent", [
    'IGNORED = {".git", "build"}',
    'if path.name == "git-notes.tex":',
    'run(["kpsewhich", f"{style}.bst"])',
    'note = "git holds no earlier value of it"',
])
def test_the_git_guard_does_not_trip_on_a_mention_of_git(innocent):
    assert not GIT_CALL.search(_squash(innocent)), innocent


@pytest.mark.parametrize("spelling", ["text=True", "text = True", "text  =  True"])
def test_the_decode_guard_is_not_defeated_by_spacing(spelling):
    assert TEXT_TRUE in _squash(spelling), spelling
