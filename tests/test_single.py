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
    detached and outlives a killed server -- still editing the manuscript.

    This guards the raw handoff, and its drain deliberately does NOT arm the
    orphan watch. Both halves are real: the lock must survive the server (here),
    and the drain must then notice and stop
    (`test_a_drain_whose_server_was_killed_gives_the_queue_back`). What the
    product does is hold the queue for the moment between the two.
    """
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


# ------------------------------------------------------ the drain nobody owns


def _orphan_script(root: Path) -> str:
    """A drain that adopts an inherited lock and arms the orphan watch, which
    is what `cmd_drain` does. Deliberately NOT a mock: the whole defect is a
    real process holding a real kernel lock after its server is gone."""
    return textwrap.dedent(f"""
        import os, sys, threading, time
        sys.path.insert(0, {str(Path(__file__).resolve().parent.parent)!r})
        from manuscriptor.server import single

        ROOT = {str(root)!r}
        held = single.DrainLock.inherited(ROOT)
        assert held is not None, "the child was handed no lock"
        stop = threading.Event()
        single.parent_watch(stop, poll=0.05)
        print(os.getpid(), flush=True)
        stop.wait(60)                  # the watch sets it when the server dies
        held.release()
    """)


def _server_script(root: Path, child: Path) -> str:
    return textwrap.dedent(f"""
        import subprocess, sys, time
        sys.path.insert(0, {str(Path(__file__).resolve().parent.parent)!r})
        from manuscriptor.server.single import DrainLock

        lock = DrainLock({str(root)!r})
        assert lock.acquire()
        kid = subprocess.Popen(
            [sys.executable, {str(child)!r}],
            env=lock.child_env(), pass_fds=(lock.fd,),
            stdout=subprocess.PIPE, text=True, start_new_session=True)
        print(kid.stdout.readline().strip(), flush=True)
        time.sleep(120)
    """)


def _free_within(root: Path, seconds: float = 10.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        probe = DrainLock(root)
        if probe.acquire():
            probe.release()
            return True
        time.sleep(0.05)
    return False


def test_a_drain_whose_server_was_killed_gives_the_queue_back(tmp_path):
    """THE REPRODUCTION, 2026-07-31 and 2026-07-29.

    A COVET server was killed rather than stopped, and 20 minutes later its
    replacement was refused the queue -- correctly, by a genuine flock held by a
    process that was genuinely alive. The drain is detached and reparented to
    pid 1 when its server dies, and nothing ever reaped it, so it owned the
    queue for as long as the machine stayed up. The author read the pid out of
    the file, found it dead by the time he looked, and diagnosed a stale lock;
    the file is not the lock and was never stale.

    A pid-liveness check does not fix this and never could: the holder is ALIVE.
    """
    child = tmp_path / "drain.py"
    child.write_text(_orphan_script(tmp_path), encoding="utf-8")
    script = tmp_path / "server.py"
    script.write_text(_server_script(tmp_path, child), encoding="utf-8")

    server = subprocess.Popen([sys.executable, str(script)],
                              stdout=subprocess.PIPE, text=True)
    drain_pid = int(server.stdout.readline().strip())
    try:
        assert DrainLock(tmp_path).acquire() is False, "the drain holds the queue"
        os.kill(server.pid, signal.SIGKILL)
        server.wait()
        assert _free_within(tmp_path), (
            "the queue stayed locked after its server was killed: the drain is "
            "an orphan nobody can reach, and every later serve is refused")
    finally:
        for pid in (drain_pid, server.pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, TypeError):
                pass
        server.wait()


def test_the_real_drain_command_gives_the_queue_back(tmp_path):
    """The same reproduction through `manuscriptor drain` itself.

    The test above proves the mechanism; this one proves it is WIRED. Without
    it, deleting the arming in `cmd_drain` left the whole suite green -- the
    mechanism existed, nothing called it, and the defect was untouched.

    A stub `claude` on PATH, because the guard under test is the drain's
    lifetime and not what a session does; a real one would cost a minute and
    edit the fixture.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "claude"
    stub.write_text("#!/bin/sh\nexec cat > /dev/null\n", encoding="utf-8")
    stub.chmod(0o755)

    paper = tmp_path / "paper"
    paper.mkdir()
    (paper / "main.tex").write_text(
        "\\documentclass{article}\\begin{document}\nHi.\n\\end{document}\n",
        encoding="utf-8")

    script = tmp_path / "server.py"
    script.write_text(textwrap.dedent(f"""
        import os, subprocess, sys, time
        sys.path.insert(0, {str(Path(__file__).resolve().parent.parent)!r})
        from manuscriptor.server.single import DrainLock

        lock = DrainLock({str(paper)!r})
        assert lock.acquire()
        env = lock.child_env()
        env["PATH"] = {str(bin_dir)!r} + os.pathsep + env.get("PATH", "")
        kid = subprocess.Popen(
            [sys.executable, "-m", "manuscriptor.cli", "drain", {str(paper)!r}],
            env=env, pass_fds=(lock.fd,), start_new_session=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print(kid.pid, flush=True)
        time.sleep(120)
    """), encoding="utf-8")

    server = subprocess.Popen([sys.executable, str(script)],
                              stdout=subprocess.PIPE, text=True)
    drain_pid = int(server.stdout.readline().strip())
    try:
        # THE DRAIN has to hold it, not merely the server -- and the first
        # version of this test checked only that the queue was busy, which the
        # server alone satisfied, so it passed without a drain ever starting.
        # The stamped pid is the drain's own, written when it adopts.
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if DrainLock(paper).holder() == drain_pid:
                break
            time.sleep(0.1)
        else:
            raise AssertionError(
                f"the drain never adopted the lock (holder stayed "
                f"{DrainLock(paper).holder()}, wanted {drain_pid})")
        assert DrainLock(paper).acquire() is False

        os.kill(server.pid, signal.SIGKILL)
        server.wait()
        assert _free_within(paper, seconds=20), (
            "`manuscriptor drain` kept the queue after its server was killed: "
            "the orphan watch is not armed in cmd_drain")
    finally:
        for pid in (drain_pid, server.pid):
            try:
                os.killpg(os.getpgid(pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError, TypeError):
                pass
        server.wait()


def test_a_drain_run_by_hand_outlives_the_shell_that_started_it(tmp_path):
    """The mirror of the guard above, and the reason the arming is gated.

    `manuscriptor drain <dir>` typed by hand has no server to lose. Arming the
    watch on whatever shell happened to start it would end the drain the moment
    that shell exited -- immediately, when it is launched from a wrapper that
    returns. That is the "worse defect than the one it fixes" failure the module
    docstring warns about, arrived at from the other direction.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "claude"
    stub.write_text("#!/bin/sh\nexec cat > /dev/null\n", encoding="utf-8")
    stub.chmod(0o755)
    paper = tmp_path / "paper"
    paper.mkdir()
    (paper / "main.tex").write_text(
        "\\documentclass{article}\\begin{document}\nHi.\n\\end{document}\n",
        encoding="utf-8")

    env = dict(os.environ)
    env.pop(FD_ENV, None)                       # nothing was handed down
    env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")
    # A launcher that starts the drain and exits at once, so the drain is
    # orphaned within milliseconds -- exactly the case the gate protects.
    launcher = subprocess.Popen(
        [sys.executable, "-c", textwrap.dedent(f"""
            import subprocess, sys
            kid = subprocess.Popen(
                [sys.executable, "-m", "manuscriptor.cli", "drain", {str(paper)!r}],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            print(kid.pid, flush=True)
        """)],
        cwd=str(Path(__file__).resolve().parent.parent),
        env=env, stdout=subprocess.PIPE, text=True)
    drain_pid = int(launcher.stdout.readline().strip())
    launcher.wait()                             # the shell is gone immediately
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if DrainLock(paper).holder() == drain_pid:
                break
            time.sleep(0.1)
        else:
            raise AssertionError("the hand-run drain never took the queue")
        # Give the watch, were it wrongly armed, several poll intervals to fire.
        time.sleep(6)
        assert DrainLock(paper).acquire() is False, (
            "a hand-run drain stopped when the shell that started it exited; "
            "the orphan watch must be armed only for a drain a SERVER started")
        os.kill(drain_pid, 0)                   # raises if it has gone
    finally:
        try:
            os.kill(drain_pid, signal.SIGKILL)
        except (ProcessLookupError, TypeError):
            pass


def test_the_orphan_watch_fires_only_on_reparenting(tmp_path):
    """The signal is that OUR parent changed, not that some pid is missing.
    A pid can be recycled; a parent can only change by dying."""
    import threading

    from manuscriptor.server import single

    seen = iter([4242, 4242, 1])          # ...and then the server dies
    stop = threading.Event()
    single.parent_watch(stop, poll=0.02, _parent=lambda: next(seen, 1))
    assert stop.wait(2.0), "a changed parent must stop the drain"

    # The same pid throughout is a live server, however long we wait.
    steady = threading.Event()
    single.parent_watch(steady, poll=0.02, _parent=lambda: 4242)
    assert not steady.wait(0.4), "an unchanged parent must not stop the drain"


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
