"""A manuscript keeps its port, because the browser keys storage by origin.

Serving on an ephemeral port made every launch a new origin, so every launch
started with no drafts and no colour preference, and a draft left behind by a
crash was addressable only by reading WebKit's sqlite by hand. It also meant a
server that died took its port with it, so the page could never reconnect: the
one thing the page's own retry loop is built to do was made impossible by the
port. A port derived from the manuscript's own path fixes both.
"""
from __future__ import annotations

import socket
from pathlib import Path

from manuscriptor.server import ports


def test_the_same_manuscript_always_gets_the_same_port(tmp_path):
    d = tmp_path / "paper"
    d.mkdir()
    assert ports.stable_port(d) == ports.stable_port(d)


def test_the_port_does_not_depend_on_how_the_path_was_written(tmp_path):
    """`serve .` and `serve /abs/path/.` and a symlink to it are one manuscript
    to the reader, so they must be one origin to the browser."""
    d = tmp_path / "paper"
    (d / "sub").mkdir(parents=True)
    link = tmp_path / "link"
    link.symlink_to(d)
    assert ports.stable_port(d) == ports.stable_port(d / "sub" / "..")
    assert ports.stable_port(d) == ports.stable_port(link)


def test_two_manuscripts_do_not_share_a_port(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(), b.mkdir()
    assert ports.stable_port(a) != ports.stable_port(b)


def test_the_port_is_in_the_unprivileged_range(tmp_path):
    for name in ("a", "b", "c", "d", "e", "f", "g", "h"):
        d = tmp_path / name
        d.mkdir()
        assert 1024 < ports.stable_port(d) < 65536


def test_a_taken_port_steps_aside_rather_than_failing(tmp_path):
    """Losing the stable port costs the drafts and the reconnect; failing to
    serve at all costs the manuscript. So it steps aside, deterministically, and
    only falls back to an ephemeral port when it has run out of neighbours."""
    d = tmp_path / "paper"
    d.mkdir()
    want = ports.stable_port(d)
    chosen = ports.choose_port(d, is_free=lambda p: p != want)
    assert chosen != want
    assert chosen != 0, "a neighbour was free; ephemeral is the last resort"
    assert abs(chosen - want) <= ports.NEIGHBOURS


def test_nothing_free_falls_back_to_an_ephemeral_port(tmp_path):
    d = tmp_path / "paper"
    d.mkdir()
    assert ports.choose_port(d, is_free=lambda p: False) == 0


def test_the_free_check_is_real():
    """The default `is_free` has to answer about the actual machine, or the
    fallback is decoration. Asked of a port held open right now, and then of the
    same port once it is closed."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        taken = s.getsockname()[1]
        assert ports.is_free(taken) is False
    assert ports.is_free(taken) is True


def test_a_held_stable_port_is_stepped_over_for_real(tmp_path):
    """The same walk, driven by a socket rather than by a lambda: bind the port
    the manuscript would get and confirm serving would land beside it."""
    d = tmp_path / "paper"
    d.mkdir()
    want = ports.stable_port(d)
    with socket.socket() as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", want))
        except OSError:                      # someone else has it: nothing to prove
            return
        s.listen(1)
        assert ports.choose_port(d) == want + 1
