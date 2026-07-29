"""Session-wide guards that must hold before any test is worth believing.

The only one so far is architecture. It earned its place: for a week the suite
was reported as "1124 passed, 22 failed (pre-existing)" and the twenty-two were
not pre-existing at all, they were the whole importer file failing to import
under a translated interpreter. Twenty-two identical dlopen errors buried in a
scroll of output read as a stale corner of the codebase, so nobody looked, and
the reviewer-PDF and coauthor-tracked-changes import path went unexercised for
a week. One refusal that names the cause is worth more than twenty-two symptoms
that disguise it.
"""

import ctypes
import sys

import pytest


def running_translated() -> bool:
    """True when this process is an x86_64 translation on Apple Silicon.

    `sysctl.proc_translated` is the decisive test and the only one that is not
    a guess. `platform.machine()` reports the architecture of the *process*,
    so under Rosetta it says x86_64 on hardware that is arm64 and agrees with
    itself perfectly while being useless. The sysctl is absent on Intel Macs
    and on every other platform, which is why a non-zero return code is read
    as "not translated" rather than as a failure.
    """
    if sys.platform != "darwin":
        return False
    try:
        libc = ctypes.CDLL("libc.dylib", use_errno=True)
        value = ctypes.c_int(0)
        size = ctypes.c_size_t(ctypes.sizeof(value))
        rc = libc.sysctlbyname(
            b"sysctl.proc_translated",
            ctypes.byref(value),
            ctypes.byref(size),
            None,
            ctypes.c_size_t(0),
        )
    except (OSError, AttributeError):
        return False
    return rc == 0 and value.value == 1


def pytest_configure(config: pytest.Config) -> None:
    """Refuse to collect anything under Rosetta, and say why.

    A universal binary inherits its parent's architecture on macOS, so a
    translated parent silently drags the interpreter to x86_64 while the
    wheels beside it are arm64. Every compiled extension then fails to load.
    Aborting here rather than letting collection proceed is deliberate: the
    tests are not skipped and nothing is marked expected-to-fail, the run
    simply does not pretend to be a result.
    """
    if not running_translated():
        return
    raise pytest.UsageError(
        "Refusing to run: this interpreter is being translated to x86_64 by "
        "Rosetta, but its wheels are arm64, so every compiled extension will "
        "fail to load and the failures will not look like this one.\n"
        "\n"
        "A universal binary takes its architecture from whatever launched it. "
        "Something in the invocation chain is an x86_64 process -- most often "
        "a Homebrew binary from the Intel prefix at /usr/local, and `rtk` in "
        "particular, so `rtk proxy python -m pytest` reproduces this exactly.\n"
        "\n"
        "Run the suite directly instead:\n"
        "    /usr/local/bin/python3 -m pytest\n"
        "\n"
        "`rtk proxy` is not needed for pytest: rtk's own config already lists "
        "pytest and python under [hooks] exclude_commands, so a plain run is "
        "unfiltered already. Confirm an interpreter with "
        "`sysctl -n sysctl.proc_translated`, where 0 is native."
    )
