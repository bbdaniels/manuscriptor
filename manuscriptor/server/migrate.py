"""Moving a manuscript from the old layout to the hidden one, once.

Before 2026-07-27 a served manuscript grew `comments.jsonl` at its top level and
a `build/manuscriptor/` beside it. Both now live under `.manuscriptor/`. Rather
than teach every reader to look in two places, the move happens once, the first
time a manuscript with the old shape is opened. A dual-path reader is precisely
the scar tissue that outlives the reason for it, and the one thing this module
exists to avoid.

**The comment record is git-tracked, so it moves with `git mv`.** A plain move
of a tracked file reads to git as a delete plus an untracked add, which loses
the history of a file whose whole value is being an append-only record. Outside
a repository, or for a file git does not know about, a plain rename is right and
is what happens.

**Nothing is overwritten and nothing is deleted.** A destination that already
holds a file is left alone and the source is reported rather than clobbered:
two records both called `comments.jsonl` is a situation for the author to look
at, not for a migration to resolve by picking one. The old `build/` directory is
removed only when it is empty afterwards.

**It is idempotent.** A manuscript already in the new shape has nothing to find,
so `run` returns an empty report and the caller says nothing.
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path

from manuscriptor.server import gitcmd, paths

# What lived directly in `build/manuscriptor/` and which tier it belongs to now.
# Anything not named here is render output and goes to the cache, which is the
# safe default: the cost of a wrong guess is one rebuild.
AGENT_FILES = ("agent.log", "agent-stream.jsonl", "agent-progress.json", "agent-loop.sh")
HOME_FILES = ("drafts.json",)


@dataclass
class Report:
    """What moved, and what could not."""

    moved: list[tuple[Path, Path]] = field(default_factory=list)
    skipped: list[tuple[Path, str]] = field(default_factory=list)
    tracked: bool = False

    def __bool__(self) -> bool:
        return bool(self.moved or self.skipped)

    def summary(self) -> str:
        """One line, for the ticker. Never silent about a skip."""
        if not self:
            return ""
        parts = [f"moved {len(self.moved)} file{'s' if len(self.moved) != 1 else ''} "
                 f"into {paths.HOME}/"]
        if self.tracked:
            parts.append("comment record kept in git")
        if self.skipped:
            parts.append(f"{len(self.skipped)} left in place, see below")
        return "; ".join(parts)


def needed(manuscript_dir: Path | str) -> bool:
    """Whether this manuscript still carries the old shape."""
    root = Path(manuscript_dir).resolve()
    return paths.legacy_comments(root).is_file() or paths.legacy_build(root).is_dir()


def _tracked_by_git(path: Path) -> bool:
    return gitcmd.succeeded(["ls-files", "--error-unmatch", str(path)],
                            cwd=path.parent, timeout=10)


def _git_mv(src: Path, dst: Path) -> bool:
    return gitcmd.succeeded(["mv", str(src), str(dst)], cwd=src.parent)


def _move(src: Path, dst: Path, report: Report) -> None:
    """One file or directory, never over the top of something that exists."""
    if dst.exists():
        report.skipped.append((src, f"{dst} already exists"))
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.move(str(src), str(dst))
    except OSError as exc:
        report.skipped.append((src, str(exc)))
        return
    report.moved.append((src, dst))


def run(manuscript_dir: Path | str) -> Report:
    """Move a manuscript into the hidden layout. Safe to call every time."""
    root = Path(manuscript_dir).resolve()
    report = Report()
    if not needed(root):
        return report

    paths.ensure(root)

    old_log = paths.legacy_comments(root)
    if old_log.is_file():
        new_log = paths.comments(root)
        if new_log.exists():
            report.skipped.append((old_log, f"{new_log} already exists"))
        elif _tracked_by_git(old_log) and _git_mv(old_log, new_log):
            report.moved.append((old_log, new_log))
            report.tracked = True
        else:
            _move(old_log, new_log, report)

    old_build = paths.legacy_build(root)
    if old_build.is_dir():
        for child in sorted(old_build.iterdir()):
            if child.name == ".gitignore":
                # The old rule covered the old directory. The new one is
                # written by `ensure` and says something different.
                child.unlink(missing_ok=True)
                continue
            if child.name in HOME_FILES:
                dst = paths.home(root) / child.name
            elif child.name in AGENT_FILES:
                dst = paths.agent_dir(root) / child.name
            else:
                dst = paths.cache(root) / child.name
            _move(child, dst, report)

        _prune(old_build, root)

    return report


def _prune(old_build: Path, root: Path) -> None:
    """Remove `build/manuscriptor` and then `build`, each only if empty.

    An author may keep their own things in `build/`, so an empty check is the
    only safe test. `rmdir` raising is the answer, not a reason to try harder.
    """
    for path in (old_build, old_build.parent):
        if path == root or root not in path.parents:
            return
        try:
            path.rmdir()
        except OSError:
            return
