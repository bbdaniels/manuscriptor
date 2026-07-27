"""Reporting the artifacts Manuscriptor does not own.

The reference case is dsp-bias, whose `paper/` folder held thirty-four entries
of which two were Manuscriptor's. The rest is the author's: seventeen LaTeX
artifacts from their own `latexmk` runs, and a set of documents and scripts that
belong exactly where they are.

Two properties matter more than any listing detail. A `.tex` is never touched,
and a file git TRACKS is never swept even when its suffix says it is disposable:
the reference manuscript has `main.aux`, `main.bbl` and `main.log` committed.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from manuscriptor.server import tidy


def git(d: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=d, capture_output=True, text=True).stdout


def paper(tmp_path: Path, *, ignore: str = "*.aux\n*.log\n*.blg\n*.bbl\n.DS_Store\n") -> Path:
    """A manuscript directory shaped like the real one."""
    d = tmp_path / "paper"
    d.mkdir()
    git(d, "init", "-q")
    (d / ".gitignore").write_text(ignore, encoding="utf-8")
    for name in ("main.tex", "references.bib", "make_word.py"):
        (d / name).write_text("content\n", encoding="utf-8")
    for name in ("main.aux", "main.log", "main.blg", "main.bbl",
                 "supplement.aux", "supplement.log"):
        (d / name).write_text("litter\n", encoding="utf-8")
    (d / ".DS_Store").write_bytes(b"\x00")
    (d / "main.pdf").write_bytes(b"%PDF")
    git(d, "add", ".gitignore", "main.tex", "references.bib", "make_word.py")
    git(d, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "in")
    return d


def names(findings) -> set[str]:
    return {f.path.name for f in findings}


# ------------------------------------------------------------ what it reports


def test_it_finds_the_latex_litter_and_the_os_junk(tmp_path):
    d = paper(tmp_path)
    found = names(tidy.scan(d))
    assert {"main.aux", "main.log", "main.blg", "main.bbl",
            "supplement.aux", "supplement.log", ".DS_Store"} <= found


def test_it_never_lists_the_manuscript_itself(tmp_path):
    d = paper(tmp_path)
    found = names(tidy.scan(d))
    assert not found & {"main.tex", "references.bib", "make_word.py"}


def test_the_pdf_is_a_deliverable_not_litter(tmp_path):
    """On many manuscripts the compiled PDF is the only copy anybody has."""
    d = paper(tmp_path)
    assert "main.pdf" not in names(tidy.scan(d))


def test_it_leaves_the_hidden_directory_to_clean(tmp_path):
    from manuscriptor.server import paths

    d = paper(tmp_path)
    paths.ensure(d)
    (paths.cache(d) / "render.log").write_text("x", encoding="utf-8")
    assert "render.log" not in names(tidy.scan(d))


def test_an_empty_directory_says_so(tmp_path):
    d = tmp_path / "clean"
    d.mkdir()
    (d / "main.tex").write_text("x", encoding="utf-8")
    findings = tidy.scan(d)
    assert findings == []
    assert "nothing stray" in tidy.report(d, findings)


# ------------------------------------------------------------ what it may take


def test_a_tracked_artifact_is_reported_but_never_safe(tmp_path):
    """The reference manuscript has `main.aux`, `main.bbl` and `main.log`
    COMMITTED. Sweeping one would change the repository."""
    d = paper(tmp_path, ignore="*.blg\n.DS_Store\n")
    (d / "main.aux").write_text("tracked litter\n", encoding="utf-8")
    git(d, "add", "-f", "main.aux")
    git(d, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "aux")

    by_name = {f.path.name: f for f in tidy.scan(d)}
    assert by_name["main.aux"].safe is False
    assert "git tracks it" in by_name["main.aux"].reason
    assert by_name["main.blg"].safe is True


def test_sweep_removes_the_safe_ones_and_keeps_the_rest(tmp_path):
    d = paper(tmp_path, ignore="*.blg\n.DS_Store\n")
    git(d, "add", "-f", "main.aux")
    git(d, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "aux")

    gone, kept = tidy.sweep(tidy.scan(d))
    assert (d / "main.aux").exists(), "a tracked file must survive a sweep"
    assert not (d / "main.blg").exists()
    assert not (d / ".DS_Store").exists()
    assert (d / "main.tex").exists() and (d / "main.pdf").exists()
    assert {p.name for p in kept and [f.path for f in kept]} == {"main.aux"}
    assert "main.blg" in {p.name for p in gone}


def test_sweeping_deletes_nothing_git_was_watching(tmp_path):
    """The point of asking git rather than guessing from the suffix.

    Nothing tracked is removed, so no line reads as deleted, and the only thing
    still untracked afterwards is the PDF, which tidy will not take.
    """
    d = paper(tmp_path)
    tidy.sweep(tidy.scan(d))
    status = git(d, "status", "--porcelain").splitlines()
    assert not [ln for ln in status if ln.startswith((" D", "D "))], \
        "a sweep must never delete a tracked file"
    assert [ln.split() for ln in status] == [["??", "main.pdf"]]


# ------------------------------------------------------------------- the CLI


def test_the_command_reports_and_does_not_remove(tmp_path, capsys):
    from manuscriptor import cli

    d = paper(tmp_path)
    assert cli.main(["tidy", str(d)]) == 0
    out = capsys.readouterr().out
    assert "main.aux" in out and "--sweep" in out
    assert (d / "main.aux").exists(), "reporting must never remove"


def test_the_command_removes_only_when_asked(tmp_path, capsys):
    from manuscriptor import cli

    d = paper(tmp_path)
    assert cli.main(["tidy", str(d), "--sweep"]) == 0
    assert "removed" in capsys.readouterr().out
    assert not (d / "main.aux").exists()
    assert (d / "main.tex").exists()


def test_a_manuscript_outside_a_repository_is_reported_conservatively(tmp_path):
    d = tmp_path / "loose"
    d.mkdir()
    (d / "main.tex").write_text("x", encoding="utf-8")
    (d / "main.aux").write_text("y", encoding="utf-8")
    findings = tidy.scan(d)
    assert names(findings) == {"main.aux"}
    assert all(f.safe for f in findings)
