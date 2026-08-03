"""Every call to git in this package, and the one decode policy they share.

There were eight of these, each spelling `capture_output=True, text=True` and
each catching `(OSError, subprocess.SubprocessError)`. `text=True` decodes
strictly, and `UnicodeDecodeError` is a `ValueError` -- so it went straight past
all eight except clauses. On 2026-08-03 switching documents in the running
editor died with

    'utf-8' codec can't decode byte 0xb7 in position 24471: invalid start byte

because a `git log -p` over a fragment directory had inlined a committed PDF
figure, and the exception rose out of `_git` through `manifest.describe`,
`build()` and `Session.rebuild()` to the websocket handler.

Bytes out of an external process are never guaranteed UTF-8. A commit message
written on a Latin-1 terminal, a fragment saved by Stata in cp1252, a diff over
anything git declines to call binary -- any of them is enough, and none of them
is worth taking the editor down. So: capture bytes, decode with
`errors="replace"`, and let the caller read a mangled character rather than
nothing at all.

The eight sites wanted different things -- stdout only when the command
succeeded, the return code alone, text on stdin -- so this returns the whole
result and lets each caller keep its own semantics. `None` means the process
could not be run at all, which is what every one of the eight already treated as
"git has nothing to say".
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

DEFAULT_TIMEOUT = 30


@dataclass(frozen=True)
class Result:
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def _decode(raw: bytes | None) -> str:
    return (raw or b"").decode("utf-8", errors="replace")


def run(
    args: list[str] | tuple[str, ...],
    *,
    cwd: Path | str | None = None,
    timeout: float | None = DEFAULT_TIMEOUT,
    input: str | None = None,
) -> Result | None:
    """Run `git <args>`, or return None if it could not be run at all.

    `args` never includes the word `git`; this module owns that, so the guard in
    `tests/test_gitcmd.py` can assert nothing else spells it.
    """
    try:
        done = subprocess.run(
            ["git", *args],
            cwd=str(cwd) if cwd is not None else None,
            capture_output=True,
            timeout=timeout,
            input=input.encode("utf-8") if input is not None else None,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return Result(done.returncode, _decode(done.stdout), _decode(done.stderr))


def stdout(
    args: list[str] | tuple[str, ...],
    *,
    cwd: Path | str | None = None,
    timeout: float | None = DEFAULT_TIMEOUT,
) -> str | None:
    """The output of a command that succeeded, or None. The common shape."""
    done = run(args, cwd=cwd, timeout=timeout)
    return done.stdout if done is not None and done.ok else None


def succeeded(
    args: list[str] | tuple[str, ...],
    *,
    cwd: Path | str | None = None,
    timeout: float | None = DEFAULT_TIMEOUT,
) -> bool:
    """Whether the command exited zero. The other common shape."""
    done = run(args, cwd=cwd, timeout=timeout)
    return done is not None and done.ok
