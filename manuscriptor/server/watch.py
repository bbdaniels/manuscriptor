"""M3 — watch the manuscript tree and the comment log.

Two watchers with different consumers.

The tree watcher drives the redraw: on any `.tex` change, re-render, diff blocks,
and push only what changed. Debounced, because the author may be typing in the
page while Claude is writing a different paragraph, and because continuous save
means a write lands on every typing pause.

The log watcher drives the drain. A Claude Code session runs a job that blocks
until `comments.jsonl` grows and exits when it does, which wakes the session. So
the drain fires on a comment hitting disk rather than on anyone asking for it.
`proc` remains the manual fallback for when no watcher is running.

Each wake costs a model turn, so a comment resolves in roughly a minute. Live
here means never having to ask, not watching the cursor move.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Callable

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

WATCHED = {".tex", ".bib", ".aux"}

# Never redraw because of our own output, or because git touched something.
IGNORED_DIRS = {".git", "build", "__pycache__", ".venv", "node_modules", "renv"}


class _Handler(FileSystemEventHandler):
    def __init__(self, on_batch: Callable[[set[Path]], None], debounce: float):
        self.on_batch = on_batch
        self.debounce = debounce
        self.pending: set[Path] = set()
        self.timer: threading.Timer | None = None
        self.guard = threading.Lock()

    def on_any_event(self, event):
        if event.is_directory:
            return
        path = Path(getattr(event, "dest_path", "") or event.src_path)
        if path.suffix not in WATCHED:
            return
        if any(part in IGNORED_DIRS for part in path.parts):
            return
        # An atomic splice writes a dotfile then renames; the rename is the event
        # that matters and the temp file must never trigger a render of its own.
        if path.name.startswith("."):
            return
        with self.guard:
            self.pending.add(path)
            if self.timer is not None:
                self.timer.cancel()
            self.timer = threading.Timer(self.debounce, self._fire)
            self.timer.daemon = True
            self.timer.start()

    def _fire(self):
        with self.guard:
            batch, self.pending, self.timer = self.pending, set(), None
        if batch:
            self.on_batch(batch)


def watch_tree(
    root: Path,
    on_change: Callable[[set[Path]], None],
    *,
    debounce_ms: int = 250,
) -> Callable[[], None]:
    """Watch `root` for manuscript changes. Returns a function that stops it."""
    handler = _Handler(on_change, debounce_ms / 1000.0)
    observer = Observer()
    observer.schedule(handler, str(Path(root).resolve()), recursive=True)
    observer.daemon = True
    observer.start()

    def stop() -> None:
        observer.stop()
        observer.join(timeout=2)

    return stop


def block_until_log_grows(log: Path, *, from_offset: int, poll: float = 0.5) -> int:
    """Block until `log` exceeds `from_offset`; return the new size.

    Runs as a foreground process inside a backgrounded Claude Code job, so that
    the job exiting is what wakes the session. The polling here is an
    implementation detail of one blocked process, not a busy loop in the server.
    """
    log = Path(log)
    while True:
        size = log.stat().st_size if log.exists() else 0
        if size > from_offset:
            return size
        time.sleep(poll)
