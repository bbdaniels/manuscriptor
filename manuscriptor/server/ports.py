"""The port a manuscript is served on.

A manuscript keeps ITS OWN port, derived from its path. The browser keys
localStorage by origin, so an ephemeral port made every launch a new origin:
drafts and the colour preference started empty every time, and a draft stranded
by a crash could only be read out of WebKit's sqlite by hand. It also made
recovery impossible in the one case that matters. When the server dies its port
dies with it, so the page's own websocket retry loop, which exists precisely to
survive a server restart, had nothing to retry against.

Deriving the port from the path costs one thing worth naming: two manuscripts
could collide, and something else on the machine could hold the port already.
Both are handled by stepping aside to a neighbour, deterministically, and only
falling back to an ephemeral port when even that fails. Serving the manuscript
matters more than serving it at a predictable address.
"""
from __future__ import annotations

import hashlib
import socket
from pathlib import Path

# The range to derive into: above the privileged ports, below the ephemeral
# range macOS allocates from (49152 and up), so a derived port is never one the
# system might hand to something else while we are not looking.
_LOW = 20000
_SPAN = 20000

# How far to walk when the derived port is taken. Small on purpose: a long walk
# would silently land a manuscript on a neighbour's number.
NEIGHBOURS = 8


def stable_port(root: Path | str) -> int:
    """The port this manuscript directory always gets.

    Keyed on the resolved path, so `serve .`, an absolute path, a path through
    `..`, and a symlink are one manuscript and therefore one origin.
    """
    real = Path(root).resolve()
    digest = hashlib.sha256(str(real).encode("utf-8")).digest()
    return _LOW + int.from_bytes(digest[:4], "big") % _SPAN


def is_free(port: int, host: str = "127.0.0.1") -> bool:
    """Can we bind it right now? Asked by actually binding, because every other
    way of asking is a guess about someone else's socket."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
        except OSError:
            return False
    return True


def choose_port(root: Path | str, *, is_free=is_free) -> int:
    """The stable port if it is free, else the nearest free neighbour, else 0.

    0 means "let the OS pick", which loses the origin and the reconnect. It is
    the last resort rather than the default, and the caller says so out loud.
    """
    want = stable_port(root)
    if is_free(want):
        return want
    for step in range(1, NEIGHBOURS + 1):
        if is_free(want + step):
            return want + step
    return 0
