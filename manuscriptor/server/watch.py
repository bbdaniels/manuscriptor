"""M3 — watch the manuscript tree and the comment log.

Two watchers with different consumers.

The tree watcher drives the redraw: on any `.tex` change, re-render, diff
blocks, and push only what changed. Debounce, because the author may be typing
in the page while Claude is writing a different paragraph.

The log watcher drives the drain. A Claude Code session runs a job that blocks
until `comments.jsonl` grows and exits when it does, which wakes the session.
So the drain fires on a comment hitting disk rather than on anyone asking for
it. `proc` remains the manual fallback for when no watcher is running.

Each wake costs a model turn, so a comment resolves in roughly a minute. Live
here means never having to ask, not watching the cursor move.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable


def watch_tree(root: Path, on_change: Callable[[set[Path]], None], *, debounce_ms: int = 250) -> None:
    raise NotImplementedError("M3")


def block_until_log_grows(log: Path, *, from_offset: int) -> int:
    """Block until `log` exceeds `from_offset`; return the new size.

    Runs as a foreground process inside a backgrounded Claude Code job, so that
    the job exiting is what wakes the session. No polling.
    """
    raise NotImplementedError("M5")
