"""One drain per comment queue.

The defect this guards: on 2026-07-27 `manuscriptor serve ~/Projects/dsp-bias`
and `manuscriptor serve paper` ran at once. Different arguments, different
resolved directories, different derived ports -- and one `current_root`, so both
drains read the same `comments.jsonl` and two headless sessions edited one
working tree. Nothing anywhere refused, because the only locks in the codebase
were `threading.Lock` objects, which cannot see another process.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

from manuscriptor.server import paths
from manuscriptor.server.single import FD_ENV, DrainLock


def _holder_script(root: Path) -> str:
    """A second process that takes the lock and waits to be killed."""
    return textwrap.dedent(f"""
        import sys, time
        sys.path.insert(0, {str(Path(__file__).resolve().parent.parent)!r})
        from manuscriptor.server.single import DrainLock
        lock = DrainLock({str(root)!r})
        print("got" if lock.acquire() else "refused", flush=True)
        time.sleep(120)
    """)


def _other_process_holding(root: Path) -> subprocess.Popen:
    proc = subprocess.Popen([sys.executable, "-c", _holder_script(root)],
                            stdout=subprocess.PIPE, text=True)
    assert proc.stdout.readline().strip() == "got"
    return proc


# ------------------------------------------------------------------ the claim


def test_a_second_process_is_refused(tmp_path):
    other = _other_process_holding(tmp_path)
    try:
        assert DrainLock(tmp_path).acquire() is False
    finally:
        other.kill()
        other.wait()


def test_the_refusal_can_name_the_holding_pid(tmp_path):
    other = _other_process_holding(tmp_path)
    try:
        lock = DrainLock(tmp_path)
        assert lock.acquire() is False
        assert lock.holder() == other.pid
    finally:
        other.kill()
        other.wait()


def test_two_spellings_of_one_directory_are_one_queue(tmp_path):
    """Exactly how the collision happened: same queue, unrecognisable paths."""
    (tmp_path / "paper").mkdir()
    held = DrainLock(tmp_path / "paper")
    assert held.acquire()
    try:
        through_parent = tmp_path / "paper" / ".." / "paper"
        link = tmp_path / "link"
        link.symlink_to(tmp_path / "paper")
        for spelling in (through_parent, link, Path(str(tmp_path / "paper")) ):
            assert DrainLock(spelling).path == held.path
    finally:
        held.release()


def test_a_released_lock_is_free_again(tmp_path):
    first = DrainLock(tmp_path)
    assert first.acquire()
    first.release()
    second = DrainLock(tmp_path)
    assert second.acquire()
    second.release()


def test_two_manuscripts_do_not_block_each_other(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(), b.mkdir()
    first, second = DrainLock(a), DrainLock(b)
    assert first.acquire() and second.acquire()
    first.release(), second.release()


# ------------------------------------------------- the lock that must not wedge


def test_a_killed_holder_leaves_the_queue_free(tmp_path):
    """`kill -9` is how these processes actually die. A pid file would wedge
    here; flock is released by the kernel when the last holder is gone."""
    other = _other_process_holding(tmp_path)
    os.kill(other.pid, signal.SIGKILL)
    other.wait()
    assert paths.drain_lock(tmp_path).exists(), "the file outlives the process"
    assert DrainLock(tmp_path).acquire() is True


# ------------------------------------------------------------------ the handoff


def test_the_lock_survives_the_process_that_took_it(tmp_path):
    """`serve` hands the claim to the drain it spawns, because the drain is
    detached and outlives a killed server -- still editing the manuscript."""
    script = tmp_path / "handoff.py"
    script.write_text(textwrap.dedent(f"""
        import os, subprocess, sys, time
        sys.path.insert(0, {str(Path(__file__).resolve().parent.parent)!r})
        from manuscriptor.server.single import DrainLock

        ROOT = {str(tmp_path)!r}
        if sys.argv[1] == "drain":            # the spawned child
            held = DrainLock.inherited(ROOT)
            print(os.getpid() if held else 0, flush=True)
            time.sleep(120)
        else:                                 # the server
            lock = DrainLock(ROOT)
            assert lock.acquire()
            child = subprocess.Popen(
                [sys.executable, __file__, "drain"],
                env=lock.child_env(), pass_fds=(lock.fd,),
                stdout=subprocess.PIPE, text=True, start_new_session=True)
            print(child.stdout.readline().strip(), flush=True)
            time.sleep(120)
    """), encoding="utf-8")
    server = subprocess.Popen([sys.executable, str(script), "serve"],
                              stdout=subprocess.PIPE, text=True)
    drain_pid = int(server.stdout.readline().strip())
    try:
        assert drain_pid, "the child did not adopt the inherited lock"
        assert DrainLock(tmp_path).holder() == drain_pid, \
            "the drain's own pid is the one worth naming"

        os.kill(server.pid, signal.SIGKILL)   # the server dies; the drain does not
        server.wait()
        assert DrainLock(tmp_path).acquire() is False, \
            "a live drain still holds the queue"
    finally:
        try:
            os.kill(drain_pid, signal.SIGKILL)
        except (ProcessLookupError, TypeError):
            pass
        server.kill()
        server.wait()
    for _ in range(100):
        if DrainLock(tmp_path).acquire():
            return
        time.sleep(0.05)
    raise AssertionError("the queue stayed locked after every holder died")


def test_the_environment_variable_does_not_reach_a_grandchild(tmp_path):
    """The drain starts a `claude`, which starts its own tree. None of them
    should believe they were handed a lock."""
    lock = DrainLock(tmp_path)
    assert lock.acquire()
    try:
        # A duplicate, because a real adopter is a separate process and owns the
        # descriptor it is handed; two objects closing one fd here is a test
        # artifact, not the shape of the code under test.
        os.environ[FD_ENV] = str(os.dup(lock.fd))
        adopted = DrainLock.inherited(tmp_path)
        assert adopted is not None
        assert FD_ENV not in os.environ
        adopted.release()
    finally:
        os.environ.pop(FD_ENV, None)
        lock.release()


def test_a_descriptor_that_is_not_the_lock_is_not_adopted(tmp_path):
    """Adopting the wrong fd would report a claim this process does not hold."""
    paths.ensure(tmp_path)
    stray = open(tmp_path / "stray", "w")
    try:
        os.environ[FD_ENV] = str(stray.fileno())
        paths.drain_lock(tmp_path).parent.mkdir(parents=True, exist_ok=True)
        paths.drain_lock(tmp_path).write_text("", encoding="utf-8")
        assert DrainLock.inherited(tmp_path) is None
    finally:
        os.environ.pop(FD_ENV, None)
        stray.close()


def test_no_inheritance_means_no_lock(tmp_path):
    os.environ.pop(FD_ENV, None)
    assert DrainLock.inherited(tmp_path) is None


# --------------------------------------------------------------- the CLI seam


def test_drain_by_hand_is_refused_while_another_holds_it(tmp_path, capsys, monkeypatch):
    from manuscriptor import cli
    from manuscriptor.server import supervisor

    # A stub, because this guard failing must not start a real session: when the
    # lock was weakened to watch it fail, `cmd_drain` ran on and launched
    # `claude` against the fixture for two minutes before the suite gave up.
    def refuse(*a, **k):
        raise AssertionError("a second drain was started on a held queue")

    monkeypatch.setattr(supervisor, "Drain", refuse)

    other = _other_process_holding(tmp_path)
    try:
        assert cli.main(["drain", str(tmp_path)]) == 1
        said = capsys.readouterr()
        assert str(other.pid) in said.err
        assert str(tmp_path.resolve()) in said.err
    finally:
        other.kill()
        other.wait()


def test_the_claim_is_private_to_the_manuscript(tmp_path):
    """Under the hidden directory, so it never reaches `git status`, and not in
    a temp directory, where a sweep could remove it while it is held."""
    assert paths.drain_lock(tmp_path).parent == paths.agent_dir(tmp_path)
