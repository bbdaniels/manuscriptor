"""The watchers, and the fact that two of them share one directory.

`serve` watches the drain's live feed and the drain's ledger by name. Both files
live in `paths.agent_dir`, so both watches are on the same directory with the
same recursion flag -- and watchdog's fsevents backend registers its streams in a
PROCESS-GLOBAL table keyed by `ObservedWatch(path, is_recursive)`. Two separate
`Observer()` objects are not two separate registrations there. The second one to
start raises

    RuntimeError: Cannot add watch <ObservedWatch: path='.../agent',
    is_recursive=False> - it is already scheduled

inside its own emitter thread, where nothing is waiting to catch it. The observer
object still exists and `stop()` still works, so the caller sees a live watcher
that will never deliver an event.

These tests assert the behaviour, not the absence of a traceback: both callbacks
must fire, and there must be exactly one scheduled watch for the shared
directory.
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

from manuscriptor.server import watch as watch_mod


def _settle(predicate, timeout: float = 6.0) -> bool:
    """fsevents is not synchronous; poll rather than sleep a magic number."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


class _CaughtEmitterDeath(logging.Handler):
    """watchdog logs the emitter's death rather than raising it anywhere."""

    def __init__(self):
        super().__init__(level=logging.ERROR)
        self.records: list[str] = []

    def emit(self, record):
        self.records.append(record.getMessage())


def test_two_files_in_one_directory_are_both_watched(tmp_path):
    """The defect, stated as the consequence rather than as the traceback."""
    agent = tmp_path / ".manuscriptor" / "agent"
    agent.mkdir(parents=True)
    feed, ledger = agent / "progress.json", agent / "history.jsonl"
    feed.write_text("{}", encoding="utf-8")
    ledger.write_text("", encoding="utf-8")

    fired: set[str] = set()
    lock = threading.Lock()

    def _note(name):
        def _cb():
            with lock:
                fired.add(name)
        return _cb

    caught = _CaughtEmitterDeath()
    logging.getLogger("fsevents").addHandler(caught)
    stop_feed = watch_mod.watch_file(feed, _note("feed"), debounce_ms=20)
    stop_ledger = watch_mod.watch_file(ledger, _note("ledger"), debounce_ms=20)
    try:
        assert _settle(lambda: True, 0.4)  # let both emitters come up
        feed.write_text('{"state": "working"}', encoding="utf-8")
        ledger.write_text('{"text": "a line"}\n', encoding="utf-8")
        _settle(lambda: fired == {"feed", "ledger"})
    finally:
        stop_feed()
        stop_ledger()
        logging.getLogger("fsevents").removeHandler(caught)

    assert fired == {"feed", "ledger"}, (
        f"only {sorted(fired) or 'nothing'} fired. Two watches on one directory "
        "collide in watchdog's global fsevents table, and the loser's emitter "
        "thread dies without ever delivering an event -- so the drain's ledger "
        "changes are never pushed to the open page"
    )
    assert not caught.records, (
        "an emitter thread died: " + " | ".join(caught.records)
    )


def test_one_directory_is_scheduled_once_however_many_files_it_holds(tmp_path):
    """Sharing is the fix; two registrations is the bug, silenced or not."""
    agent = tmp_path / ".manuscriptor" / "agent"
    agent.mkdir(parents=True)
    a, b, c = agent / "progress.json", agent / "history.jsonl", agent / "x.jsonl"
    for p in (a, b, c):
        p.write_text("", encoding="utf-8")

    stops = [watch_mod.watch_file(p, lambda: None) for p in (a, b, c)]
    try:
        held = watch_mod.active_watches()
        here = [w for w in held if Path(w[0]) == agent.resolve()]
        assert len(here) == 1, (
            f"{len(here)} watches on one directory: {here}. The agent directory "
            "has one owner; callers register a filename with it"
        )
    finally:
        for stop in stops:
            stop()

    assert not [w for w in watch_mod.active_watches()
                if Path(w[0]) == agent.resolve()], (
        "the shared watch outlived its last user"
    )


def test_stopping_one_watcher_leaves_the_other_watching(tmp_path):
    """A shared watch must not be torn down by the first caller to leave.

    The bug this guards is the mirror image of the one above: a refcount that
    counts down too eagerly turns a duplicate registration into a silently
    cancelled one, which looks exactly the same from the page.
    """
    agent = tmp_path / ".manuscriptor" / "agent"
    agent.mkdir(parents=True)
    feed, ledger = agent / "progress.json", agent / "history.jsonl"
    feed.write_text("", encoding="utf-8")
    ledger.write_text("", encoding="utf-8")

    seen: list[str] = []
    stop_feed = watch_mod.watch_file(feed, lambda: seen.append("feed"),
                                     debounce_ms=20)
    stop_ledger = watch_mod.watch_file(ledger, lambda: seen.append("ledger"),
                                       debounce_ms=20)
    try:
        _settle(lambda: True, 0.4)
        stop_feed()
        ledger.write_text('{"text": "after"}\n', encoding="utf-8")
        assert _settle(lambda: "ledger" in seen), (
            "the ledger stopped being watched when the feed's watcher was "
            "stopped: the shared watch was torn down by its first leaver"
        )
        assert "feed" not in seen, "a stopped watcher still delivered"
    finally:
        stop_ledger()


def test_the_tree_watch_and_a_file_watch_coexist(tmp_path):
    """`serve` arms both; the tree is recursive and the agent dir is not.

    They are different keys today, but the tree watch is rooted at the
    manuscript and the agent directory sits inside it, so any future change that
    made them collide would take out the redraw. Assert both deliver.
    """
    (tmp_path / "main.tex").write_text("hello\n", encoding="utf-8")
    agent = tmp_path / ".manuscriptor" / "agent"
    agent.mkdir(parents=True)
    feed = agent / "progress.json"
    feed.write_text("", encoding="utf-8")

    batches: list[set[Path]] = []
    fired: list[str] = []
    stop_tree = watch_mod.watch_tree(tmp_path, batches.append, debounce_ms=20)
    stop_feed = watch_mod.watch_file(feed, lambda: fired.append("feed"),
                                     debounce_ms=20)
    try:
        _settle(lambda: True, 0.4)
        (tmp_path / "main.tex").write_text("hello there\n", encoding="utf-8")
        feed.write_text('{"state": "working"}', encoding="utf-8")
        assert _settle(lambda: batches and fired), (
            f"tree batches={batches!r} feed={fired!r}: one of the two watches "
            "never delivered"
        )
    finally:
        stop_tree()
        stop_feed()
