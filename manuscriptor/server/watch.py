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

from manuscriptor.server import paths

# Source suffixes drive a re-render; figure suffixes drive an asset refresh.
# Figures are here because the agent answers a figure comment by editing the
# producing script and regenerating the PDF, and a watcher that only knew
# source left the page showing the old raster indefinitely.
WATCHED = {".tex", ".bib", ".aux", ".jsonl", ".pdf", ".png", ".jpg", ".jpeg"}
ASSET_SUFFIXES = {".pdf", ".png", ".jpg", ".jpeg"}

# Never redraw because of our own output, or because git touched something.
# `build` carries the rasterized figures, so watching it would be a loop.
IGNORED_DIRS = {".git", "build", "__pycache__", ".venv", "node_modules", "renv"}


# ONE OBSERVER PER DIRECTORY, BECAUSE THE OPERATING SYSTEM ALREADY DECIDED THAT.
# watchdog's fsevents backend registers its streams in a process-global table
# keyed by `ObservedWatch(path, is_recursive)`, so two `Observer()` objects
# watching one directory are not two registrations -- the second raises
# `RuntimeError: ... it is already scheduled` inside its own emitter thread,
# where nothing is waiting to catch it. The observer object survives, `stop()`
# still works, and the caller holds a watcher that will never deliver an event.
#
# `serve` watches the drain's live feed and the drain's ledger by name; both
# live in `paths.agent_dir`, so that was every boot. Sharing here rather than
# merging the two call sites, because the collision is a property of the
# directory and not of those particular two callers: any future pair would have
# reintroduced it, silently, in the same way.
_SHARED: dict[tuple[str, bool], "_Shared"] = {}
_SHARED_LOCK = threading.Lock()


class _Fanout(FileSystemEventHandler):
    """Delivers one directory's events to every handler registered for it."""

    def __init__(self):
        self.handlers: list[FileSystemEventHandler] = []
        self.guard = threading.Lock()

    def dispatch(self, event):
        with self.guard:
            handlers = list(self.handlers)
        for handler in handlers:
            handler.dispatch(event)


class _Shared:
    def __init__(self, path: str, recursive: bool):
        self.fanout = _Fanout()
        self.observer = Observer()
        self.observer.schedule(self.fanout, path, recursive=recursive)
        self.observer.daemon = True
        self.observer.start()


def _schedule(path: str, recursive: bool, handler: FileSystemEventHandler):
    """Attach `handler` to the watch on `path`, starting it if it is the first.

    Returns a function that detaches it, stopping the observer once the last
    handler has gone -- and only then, since tearing it down on the first leaver
    would leave the remaining callers holding a watch that never fires, which is
    the same failure wearing the opposite sign.
    """
    key = (path, recursive)
    with _SHARED_LOCK:
        shared = _SHARED.get(key)
        if shared is None:
            shared = _SHARED[key] = _Shared(path, recursive)
        with shared.fanout.guard:
            shared.fanout.handlers.append(handler)

    def stop() -> None:
        with _SHARED_LOCK:
            held = _SHARED.get(key)
            if held is not shared:
                return
            with shared.fanout.guard:
                if handler in shared.fanout.handlers:
                    shared.fanout.handlers.remove(handler)
                remaining = len(shared.fanout.handlers)
            if remaining:
                return
            del _SHARED[key]
        shared.observer.stop()
        shared.observer.join(timeout=2)

    return stop


def active_watches() -> list[tuple[str, bool]]:
    """Every directory currently watched, as `(path, recursive)`. For tests."""
    with _SHARED_LOCK:
        return list(_SHARED)


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
        # OUR OWN OUTPUT IS NOT THE AUTHOR'S MANUSCRIPT. The hidden directory
        # holds the drain's stream log and its history, both `.jsonl` and both
        # appended to as fast as a session emits events. Every one of those
        # appends classified as a source change and re-rendered the whole
        # manuscript through pandoc, because `IGNORED_DIRS` still named the
        # pre-2026-07-27 `build/` and nothing had told it where we moved.
        # `comments.jsonl` is the deliberate exception: it is how the page
        # learns the agent picked a comment up. The live feed and the ledger
        # are watched BY NAME instead, in `cmd_serve`.
        if paths.HOME in path.parts and path.name != paths.COMMENTS_NAME:
            return
        # An atomic splice writes a dotfile then renames; the rename is the event
        # that matters and the temp file must never trigger a render of its own.
        # Our own lock sidecar and atomic-write temp files are not source.
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
    return _schedule(str(Path(root).resolve()), True, handler)


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


def watch_file(path: Path, on_change: Callable[[], None], *, debounce_ms: int = 200):
    """Watch ONE file, wherever it lives. Returns a function that stops it.

    The tree watcher skips generated output, and rightly: it holds the
    rasterized figures this pipeline writes, so watching it would redraw on its
    own output forever. But the drain's live feed is generated too, because it
    must not make `git status` grow, and the page needs it as it changes. One
    file, watched by name, is the narrow exception rather than a hole in the
    rule.

    Two files in one directory is the normal case here -- the feed and the
    ledger both sit in `paths.agent_dir` -- so the directory's watch is shared
    (see `_schedule`) and this handler only picks its own filename out of it.

    Ask `server/paths.py` for the path. This docstring used to say the feed
    lived under `build/`, and the caller that believed it watched a file the
    drain had stopped writing months earlier -- which fires no events, so the
    panel simply never moved and nothing reported an error.
    """
    path = Path(path).resolve()

    class _One(FileSystemEventHandler):
        def __init__(self):
            self.timer: threading.Timer | None = None
            self.guard = threading.Lock()

        def on_any_event(self, event):
            if event.is_directory:
                return
            touched = Path(getattr(event, "dest_path", "") or event.src_path)
            if touched.name != path.name:
                return
            with self.guard:
                if self.timer is not None:
                    self.timer.cancel()
                self.timer = threading.Timer(debounce_ms / 1000.0, on_change)
                self.timer.daemon = True
                self.timer.start()

    path.parent.mkdir(parents=True, exist_ok=True)
    return _schedule(str(path.parent), False, _One())
