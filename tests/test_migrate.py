"""Moving a real manuscript into the hidden layout.

These run against a git repository rather than a bare directory, because the
thing most likely to go wrong is the comment record: it is tracked, it is
append-only, and its whole value is being the history of a review. A migration
that turns it into a delete plus an untracked add has thrown that away while
appearing to succeed.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from manuscriptor.server import migrate, paths


def git(d: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=d, capture_output=True, text=True).stdout


def repo(tmp_path: Path) -> Path:
    d = tmp_path / "paper"
    d.mkdir(parents=True)
    git(d, "init", "-q")
    (d / "main.tex").write_text("\\documentclass{article}\n", encoding="utf-8")
    git(d, "add", "main.tex")
    git(d, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "in")
    return d


def old_layout(d: Path, *, commit_log: bool = True) -> None:
    """A manuscript exactly as a pre-2026-07-27 serve left it."""
    (d / "comments.jsonl").write_text(
        '{"id": "c-0001", "kind": "comment", "body": "tighten this"}\n'
        '{"id": "c-0001", "kind": "state", "state": "done"}\n', encoding="utf-8")
    if commit_log:
        git(d, "add", "comments.jsonl")
        git(d, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "log")

    build = d / "build" / "manuscriptor"
    (build / "tikz").mkdir(parents=True)
    (build / ".gitignore").write_text("*\n", encoding="utf-8")
    (build / "manuscript.html").write_text("<p/>", encoding="utf-8")
    (build / "citations.json").write_text("{}", encoding="utf-8")
    (build / "drafts.json").write_text(
        '{"drafts": [{"doc": "main.tex", "block": "b-1", "text": "unsaved words"}]}',
        encoding="utf-8")
    (build / "agent.log").write_text("the session ran\n", encoding="utf-8")
    (build / "agent-stream.jsonl").write_text('{"t": "x"}\n', encoding="utf-8")
    (build / "tikz" / "pic.png").write_bytes(b"\x89PNG")


# --------------------------------------------------------------- what it detects


def test_a_manuscript_in_the_new_shape_needs_nothing(tmp_path):
    d = repo(tmp_path)
    paths.ensure(d)
    assert migrate.needed(d) is False
    assert not migrate.run(d)


def test_either_half_of_the_old_shape_is_enough(tmp_path):
    d = repo(tmp_path)
    (d / "comments.jsonl").write_text("{}\n", encoding="utf-8")
    assert migrate.needed(d) is True

    e = repo(tmp_path / "two")
    (e / "build" / "manuscriptor").mkdir(parents=True)
    assert migrate.needed(e) is True


# ------------------------------------------------------------------ the move


def test_every_file_lands_in_the_tier_it_belongs_to(tmp_path):
    d = repo(tmp_path)
    old_layout(d)
    migrate.run(d)

    assert paths.comments(d).read_text(encoding="utf-8").count("\n") == 2
    assert "unsaved words" in paths.drafts(d).read_text(encoding="utf-8")
    assert (paths.agent_dir(d) / "agent.log").exists()
    assert (paths.agent_dir(d) / "agent-stream.jsonl").exists()
    assert (paths.cache(d) / "manuscript.html").exists()
    assert (paths.cache(d) / "citations.json").exists()
    assert (paths.cache(d) / "tikz" / "pic.png").exists()


def test_the_unsaved_text_does_not_land_where_clean_would_take_it(tmp_path):
    """The whole reason the tiers exist, checked end to end through a move."""
    d = repo(tmp_path)
    old_layout(d)
    migrate.run(d)
    assert paths.cache(d) not in paths.drafts(d).parents


def test_the_old_directories_are_gone(tmp_path):
    d = repo(tmp_path)
    old_layout(d)
    migrate.run(d)
    assert not (d / "build" / "manuscriptor").exists()
    assert not (d / "build").exists()
    assert not (d / "comments.jsonl").exists()


def test_an_authors_own_build_directory_survives(tmp_path):
    """`build/` may be theirs. Only our subdirectory is ours to remove."""
    d = repo(tmp_path)
    old_layout(d)
    (d / "build" / "their-output.pdf").write_bytes(b"%PDF")
    migrate.run(d)
    assert not (d / "build" / "manuscriptor").exists()
    assert (d / "build" / "their-output.pdf").exists()


# ------------------------------------------------------------------- and git


def test_the_tracked_record_keeps_its_history(tmp_path):
    """`git mv`, not a rename.

    A plain move reads to git as a delete plus an untracked add. The file whose
    entire value is being an append-only record would arrive with no history
    and, worse, outside the index entirely.
    """
    d = repo(tmp_path)
    old_layout(d, commit_log=True)
    report = migrate.run(d)

    assert report.tracked is True
    assert git(d, "ls-files", ".manuscriptor/comments.jsonl").strip() == \
        ".manuscriptor/comments.jsonl"
    status = git(d, "status", "--porcelain").split("\n")[0].split()
    assert status[0] == "R", f"expected a rename, got {status}"
    # `git log` reads committed history, and the rename is still staged, so
    # the follow check only means anything once the move is committed.
    git(d, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "move")
    assert "log" in git(d, "log", "--oneline", "--follow", "--",
                        ".manuscriptor/comments.jsonl"), "history must follow the rename"


def test_an_untracked_record_still_moves(tmp_path):
    d = repo(tmp_path)
    old_layout(d, commit_log=False)
    report = migrate.run(d)
    assert report.tracked is False
    assert paths.comments(d).exists()
    assert not (d / "comments.jsonl").exists()


def test_a_manuscript_outside_a_repository_still_moves(tmp_path):
    d = tmp_path / "loose"
    d.mkdir()
    (d / "main.tex").write_text("\\documentclass{article}\n", encoding="utf-8")
    old_layout(d, commit_log=False)
    migrate.run(d)
    assert paths.comments(d).exists()
    assert (paths.cache(d) / "manuscript.html").exists()


# ------------------------------------------------------ refusing to lose data


def test_a_record_already_in_the_new_place_is_never_overwritten(tmp_path):
    """Two files both called comments.jsonl is for the author to resolve."""
    d = repo(tmp_path)
    old_layout(d)
    paths.ensure(d)
    paths.comments(d).write_text('{"id": "c-9999", "body": "already here"}\n',
                                 encoding="utf-8")

    report = migrate.run(d)
    assert "already here" in paths.comments(d).read_text(encoding="utf-8")
    assert (d / "comments.jsonl").exists(), "the source must survive"
    assert any(p == d / "comments.jsonl" for p, _ in report.skipped)
    assert "left in place" in report.summary()


def test_running_it_twice_changes_nothing(tmp_path):
    d = repo(tmp_path)
    old_layout(d)
    migrate.run(d)
    after = {p.relative_to(d): p.read_bytes()
             for p in sorted(d.rglob("*")) if p.is_file() and ".git/" not in str(p)}

    second = migrate.run(d)
    assert not second
    again = {p.relative_to(d): p.read_bytes()
             for p in sorted(d.rglob("*")) if p.is_file() and ".git/" not in str(p)}
    assert again == after


def test_the_summary_says_what_happened(tmp_path):
    d = repo(tmp_path)
    old_layout(d)
    line = migrate.run(d).summary()
    assert paths.HOME in line
    assert "git" in line
    assert line, "a migration must never be silent"


# ------------------------------------------------------- through a real serve


def served(d: Path, **kw):
    from manuscriptor.server.app import Session

    return Session(d, **kw)


def test_opening_a_manuscript_moves_it_and_says_so(tmp_path):
    d = repo(tmp_path)
    old_layout(d)
    s = served(d)

    assert paths.comments(d).exists()
    assert not (d / "comments.jsonl").exists()
    assert not (d / "build").exists()
    assert s.notices and paths.HOME in s.notices[0]
    # and the session reads the record from where it now lives
    assert s.log == paths.comments(d)


def test_a_read_only_serve_moves_nothing_of_the_authors(tmp_path):
    """`--read-only` must not relocate a file the author owns.

    A migration is the most well-intentioned write there is, and it is still a
    write to a manuscript somebody opened to read. Note what this does NOT
    claim: `build()` still creates the hidden directory and renders into it,
    because it cannot see the read-only flag. That gap predates this change --
    the same build used to create `build/manuscriptor/` on a read-only serve --
    and it is recorded here rather than papered over.
    """
    d = repo(tmp_path)
    old_layout(d)
    mine = {p.relative_to(d): p.read_bytes()
            for p in d.rglob("*")
            if p.is_file() and ".git/" not in str(p) and paths.HOME not in p.parts}

    s = served(d, read_only=True)

    still = {p.relative_to(d): p.read_bytes()
             for p in d.rglob("*")
             if p.is_file() and ".git/" not in str(p) and paths.HOME not in p.parts}
    assert still == mine, "a read-only serve must not move or change the author's files"
    assert (d / "comments.jsonl").exists(), "the record stays where it was"
    assert (d / "build" / "manuscriptor" / "drafts.json").exists()
    assert s.notices == []


def test_a_second_open_announces_nothing(tmp_path):
    d = repo(tmp_path)
    old_layout(d)
    served(d)
    assert served(d).notices == []
