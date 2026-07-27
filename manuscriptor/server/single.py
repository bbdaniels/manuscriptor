"""One drain per comment queue, enforced across processes.

**The unit is the directory that owns `comments.jsonl`, not the served path and
not the port.** On 2026-07-27 two servers drained one manuscript at the same
time and nothing noticed, because at every other layer they looked unrelated:
`manuscriptor serve ~/Projects/dsp-bias` and `manuscriptor serve paper` are
different arguments, resolve to different directories, and therefore derive
different stable ports -- but `cmd_serve` binds the drain to
`tree.current_root`, which sent both to the same `paper/`. Two headless sessions
then fanned out into one working tree, and a worker recorded the damage in its
own log: four files "rewritten along their own brief between their first read
and their first edit", and one generator that the other session was holding
read-only edited anyway.

**`fcntl.flock`, because a killed holder must not wedge the next launch.** The
lock lives on an open file description and the kernel drops it when the last
process holding that description dies, which is exactly the property a pid file
does not have. These processes get killed with `kill -9` routinely; a guard that
survived that and refused every later launch would be a worse defect than the
one it fixes. `source/splice.py` already uses flock for the same reason, and
this follows it rather than inventing a second scheme.

**The lock is handed to the drain child, not merely held by the server.**
`serve` starts `manuscriptor drain` as a subprocess and detaches it into its own
session, so a `kill -9` on the server leaves the drain alive and still editing.
Passing the descriptor down (`pass_fds`, so it survives exec) makes the lock
outlive the server exactly as long as the drain does: the answer to "is anything
draining this queue" stays true while a real drain exists, and becomes false the
moment the last one is gone. It also resolves what would otherwise be a
contradiction, since a drain launched by hand takes this same lock and would
collide with its own parent.

The file is in the manuscript's hidden directory rather than in `/tmp`. A
tempdir lock can be swept while it is held, and the next process then creates a
fresh inode, takes a lock on it, and drains a queue somebody else is already
draining -- silently. `.manuscriptor/` is gitignored whole, so nothing here
reaches the author's `git status`.
"""
from __future__ import annotations

import errno
import fcntl
import os
from pathlib import Path

from . import paths

# How the descriptor is named to the child. Popped on adoption so it cannot
# reach a grandchild -- the drain starts a `claude`, which starts its own tree.
FD_ENV = "MANUSCRIPTOR_DRAIN_LOCK_FD"


class DrainLock:
    """Exclusive claim on one comment queue, held for the drain's lifetime.

    `acquire()` answers False when somebody else holds it; `holder()` then names
    the pid so the refusal can say who. A filesystem that cannot lock is not
    treated as a refusal -- see `degraded`.
    """

    def __init__(self, root: Path | str):
        self.root = Path(root).resolve()
        self.path = paths.drain_lock(self.root)
        self.degraded = False
        self._fh = None

    # --------------------------------------------------------------- taking it

    def acquire(self) -> bool:
        """Take it, or answer False. Non-blocking: waiting is not a behaviour
        anyone wants from a server that is otherwise ready to serve."""
        if self._fh is not None:
            return True
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # Read-write and NOT append: the pid is written over the file, and
            # in append mode a seek(0) is ignored, so the drain's stamp landed
            # after the server's and `holder()` named the wrong process.
            fh = os.fdopen(os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644),
                           "r+", encoding="utf-8")
        except OSError:
            # An unwritable manuscript directory is not a second drain. A drain
            # that cannot write cannot do damage either, so proceed unguarded
            # and let the caller say the guard is off.
            self.degraded = True
            return True
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            fh.close()
            if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK):
                return False
            # Some network filesystems have no flock at all. Same reasoning as
            # above: an absent lock is not a held lock.
            self.degraded = True
            return True
        self._fh = fh
        self._stamp()
        return True

    @classmethod
    def inherited(cls, root: Path | str) -> "DrainLock | None":
        """The lock this process was handed by the server that started it.

        Returns None when there is none, which is the ordinary case for
        `manuscriptor drain <dir>` typed by hand; the caller acquires instead.
        """
        raw = os.environ.pop(FD_ENV, None)
        if not raw:
            return None
        try:
            fd = int(raw)
        except ValueError:
            return None
        lock = cls(root)
        # Confirm the descriptor is the lock file before taking ownership of it.
        # Adopting the wrong fd would report a lock this process does not hold.
        try:
            got, want = os.fstat(fd), lock.path.stat()
        except OSError:
            return None
        if (got.st_dev, got.st_ino) != (want.st_dev, want.st_ino):
            return None
        try:
            lock._fh = os.fdopen(fd, "r+", encoding="utf-8")
        except OSError:
            return None
        lock._stamp()      # the drain's own pid is the one worth naming
        return lock

    # -------------------------------------------------------------- letting go

    def release(self) -> None:
        """Drop it. Unlocking the shared description frees it for the child too,
        so callers stop the drain FIRST and release after."""
        fh, self._fh = self._fh, None
        if fh is None:
            return
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            fh.close()

    # ------------------------------------------------------------- who has it

    @property
    def held(self) -> bool:
        return self._fh is not None

    @property
    def fd(self) -> int | None:
        """The descriptor to hand down, so the lock survives the server."""
        return None if self._fh is None else self._fh.fileno()

    def holder(self) -> int | None:
        """The pid written by whoever holds it, when it can be read."""
        try:
            first = self.path.read_text(encoding="utf-8").split("\n", 1)[0]
        except OSError:
            return None
        try:
            pid = int(first.strip())
        except ValueError:
            return None
        return pid or None

    def child_env(self, env: dict | None = None) -> dict:
        """Environment for the drain, naming the descriptor to adopt."""
        out = dict(os.environ if env is None else env)
        if self.fd is None:
            out.pop(FD_ENV, None)
        else:
            out[FD_ENV] = str(self.fd)
        return out

    # -------------------------------------------------------------- internals

    def _stamp(self) -> None:
        """Write the holding pid. Written over rather than truncated first, so a
        reader racing the write sees a stale pid rather than an empty file."""
        if self._fh is None:
            return
        try:
            self._fh.seek(0)
            self._fh.write(f"{os.getpid()}\n{self.root}\n")
            self._fh.truncate()
            self._fh.flush()
        except OSError:
            pass

    # THERE IS DELIBERATELY NO `__del__`. A first version released on garbage
    # collection, and because the drain child holds the SAME open file
    # description, dropping a stray reference in the server unlocked a queue a
    # live drain was working -- silently, and only sometimes, since it depended
    # on when the collector ran. Caught by the handoff test, which held the lock
    # in an expression rather than a variable. Releasing is a decision the caller
    # makes out loud; an unreleased lock leaks one descriptor until exit, which
    # is the lesser failure by a wide margin.
