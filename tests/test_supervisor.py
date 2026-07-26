"""The drain as a supervised session rather than a shell loop.

Written against the failure that produced it. On 2026-07-26 a comment asking for
a six-panel figure was marked `working`, a teammate was dispatched, and nothing
happened for fourteen minutes: no output, no file touched, no way to tell whether
it was thinking, blocked on a permission a print session cannot ask for, or dead.
Restarting it reproduced the same silence within the minute. Two for two.

So the two things these tests pin are the two things that were missing: silence is
a fault the loop acts on, and what the session and its teammates are doing is
written where the author can read it.
"""
from __future__ import annotations

import json

import pytest

from manuscriptor.server import supervisor as sup


# ---------------------------------------------------------------- the decisions


def test_work_is_handed_to_an_idle_session():
    d = sup.decide(pending=("c-1", "c-2"), in_flight=False, silent_for=0.0)
    assert d.do == "send" and d.ids == ("c-1", "c-2")


def test_nothing_is_handed_over_while_a_turn_is_in_flight():
    """Sending a second comment to a session that has not answered the first is
    how one stuck figure becomes two."""
    assert sup.decide(pending=("c-2",), in_flight=True, silent_for=5.0).do == "wait"


def test_an_idle_session_with_nothing_pending_waits():
    assert sup.decide(pending=(), in_flight=False, silent_for=999.0).do == "wait"


def test_a_silent_turn_is_a_fault():
    """THE defect. Fourteen minutes of nothing looked exactly like working."""
    d = sup.decide(pending=("c-6",), in_flight=True, silent_for=200.0, stall_after=150.0)
    assert d.do == "restart"
    assert "200s" in d.why, "the reason has to name how long it was quiet"


def test_a_turn_that_is_merely_slow_is_left_alone():
    assert sup.decide(pending=("c-6",), in_flight=True, silent_for=149.0,
                      stall_after=150.0).do == "wait"


def test_a_dead_session_is_restarted_before_anything_else():
    d = sup.decide(pending=("c-1",), in_flight=False, silent_for=0.0, alive=False)
    assert d.do == "restart" and "exited" in d.why


def test_a_dead_session_is_not_handed_work():
    """Order matters: the restart branch has to win over the send branch, or the
    supervisor writes a message into a pipe nobody is reading."""
    assert sup.decide(pending=("c-1",), in_flight=False, silent_for=0.0,
                      alive=False).do != "send"


# ------------------------------------------------------------------ the stream


def assistant(*parts, sidechain=False):
    ev = {"type": "assistant", "message": {"role": "assistant", "content": list(parts)}}
    if sidechain:
        ev["isSidechain"] = True
    return ev


def test_thinking_reaches_the_feed():
    got = sup.summarize(assistant({"type": "thinking", "thinking": "The channels are\nout of order"}))
    assert [(e.who, e.kind, e.text) for e in got] == [
        ("agent", "thinking", "The channels are out of order")
    ]


def test_a_teammates_thinking_is_marked_as_the_teammates():
    """The question a stalled figure raises first is which of them is doing it."""
    got = sup.summarize(assistant({"type": "thinking", "thinking": "rebuilding panel 3"},
                                  sidechain=True))
    assert got[0].who == "teammate"


def test_a_tool_call_shows_its_name_and_one_argument():
    got = sup.summarize(assistant({
        "type": "tool_use", "name": "Bash",
        "input": {"command": "Rscript analysis/make_figures.R", "description": "rebuild"},
    }))
    assert got[0].kind == "tool" and got[0].text == "Bash: Rscript analysis/make_figures.R"


def test_a_tool_call_with_no_useful_argument_still_names_itself():
    got = sup.summarize(assistant({"type": "tool_use", "name": "Glob", "input": {}}))
    assert got[0].text == "Glob"


def test_the_dispatch_of_a_teammate_is_visible():
    """The event that was the last thing anyone saw before the silence."""
    got = sup.summarize(assistant({
        "type": "tool_use", "name": "Agent",
        "input": {"subagent_type": "data-engineer", "description": "Rebuild fig3 as 6-panel"},
    }))
    assert got[0].text == "Agent: Rebuild fig3 as 6-panel"


def test_a_finished_turn_is_marked():
    got = sup.summarize({"type": "result", "subtype": "success"})
    assert got[0].kind == "result" and "success" in got[0].text


def test_noise_is_not_forwarded():
    """`thinking_tokens` and friends arrive constantly and say nothing."""
    assert sup.summarize({"type": "system", "subtype": "thinking_tokens"}) == []
    assert sup.summarize({"type": "rate_limit_event"}) == []
    assert sup.summarize({"type": "assistant", "message": {"content": "not a list"}}) == []


def test_a_ready_session_says_so():
    got = sup.summarize({"type": "system", "subtype": "init", "session_id": "x"})
    assert got and got[0].kind == "note"


# -------------------------------------------------------------------- the feed


def test_the_feed_is_written_where_the_page_can_read_it(tmp_path):
    feed = sup.Feed(path=sup.progress_path(tmp_path), every=0.0)
    feed.set(state="working", working=("c-6",))
    feed.add([sup.Entry(sup.now(), "teammate", "thinking", "panel 3 of 6")], force=True)
    got = sup.read_feed(sup.progress_path(tmp_path))
    assert got["state"] == "working" and got["working"] == ["c-6"]
    assert got["entries"][-1]["text"] == "panel 3 of 6"


def test_the_feed_keeps_only_what_is_recent(tmp_path):
    feed = sup.Feed(path=sup.progress_path(tmp_path), every=0.0)
    for i in range(sup.KEEP + 40):
        feed.add([sup.Entry(sup.now(), "agent", "thinking", f"step {i}")])
    got = sup.read_feed(sup.progress_path(tmp_path))
    assert len(got["entries"]) == sup.KEEP
    assert got["entries"][-1]["text"] == f"step {sup.KEEP + 39}"


def test_an_absent_or_broken_feed_reads_as_idle(tmp_path):
    """The page must render whether or not an agent has ever run."""
    assert sup.read_feed(tmp_path / "nope.json")["state"] == "idle"
    bad = tmp_path / "agent-progress.json"
    bad.write_text("{not json", encoding="utf-8")
    assert sup.read_feed(bad) == {"state": "idle", "working": [], "entries": []}


def test_the_feed_write_is_atomic(tmp_path):
    feed = sup.Feed(path=sup.progress_path(tmp_path), every=0.0)
    feed.note("x" * 4000)
    assert json.loads(sup.progress_path(tmp_path).read_text(encoding="utf-8"))
    assert not list(tmp_path.glob("*.tmp")), "the temporary file must be gone"


# --------------------------------------------------------------- the invocation


def test_the_session_asks_for_the_stream_it_needs(tmp_path):
    s = sup.Session(tmp_path, feed=sup.Feed(path=tmp_path / "f.json"),
                    manuscriptor="/bin/true", claude="/bin/echo")
    argv = s.argv()
    assert "--input-format" in argv and "stream-json" in argv, "work is handed over, not booted"
    assert "--output-format" in argv, "the events are the feed and the liveness signal"
    assert "--forward-subagent-text" in argv, (
        "a teammate's thinking is what went dark; it has to come through"
    )
    assert "--verbose" in argv, "stream-json output requires it"
    # Named tools rather than a bypass: a print session cannot ask for permission,
    # so an unlisted tool is a hang, and a blanket bypass on the author's own
    # repository is a different kind of bad afternoon.
    assert "--allowedTools" in argv
    assert any(a.startswith("Bash(Rscript") for a in argv), "the figure work needs R"
    assert "--dangerously-skip-permissions" not in argv


def test_the_session_can_be_told_things_and_reports_when_it_cannot(tmp_path):
    s = sup.Session(tmp_path, feed=sup.Feed(path=tmp_path / "f.json"),
                    manuscriptor="/bin/true", claude="/bin/echo")
    assert s.send("hello") is False, "an unstarted session cannot be sent to"
    assert s.alive is False


def test_the_work_message_names_the_constraint_that_matters():
    text = sup.WORK.format(ids="c-6", ms="/bin/ms", root="/tmp/paper")
    assert "ONE BLOCK PER WRITE" in text
    assert "c-6" in text
    assert "before you delegate" in text, (
        "the feed is only as good as what the agent says before it goes quiet"
    )


def test_the_boot_message_does_not_ask_the_session_to_park():
    """The loop moved out of the prompt and into the supervisor, which is the
    whole point: a session that parks cannot be watched by the thing waiting on
    it, and every wake used to pay for a boot."""
    text = sup.BOOT.format(root="/tmp/paper")
    assert "park" in text.lower(), "it should say explicitly that there is nothing to park"
    assert "proc --wait" not in text
