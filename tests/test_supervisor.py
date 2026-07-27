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

from manuscriptor.server import paths
from manuscriptor.server import supervisor as sup


# ---------------------------------------------------------------- the decisions


def test_work_is_handed_to_an_idle_session():
    d = sup.decide(pending=("c-1", "c-2"), in_flight=False, silent_for=0.0)
    assert d.do == "send" and d.ids == ("c-1", "c-2")


def test_a_comment_that_lands_mid_turn_joins_the_running_session():
    """The behaviour the author asked for, and the one that was not happening.

    Measured on dsp-bias 2026-07-27: c-0009 was left four seconds after c-0008
    was picked up and waited 8m54s, because this branch answered `wait` until
    the turn ended. Eleven comments, eleven turns, zero overlap. Measured on a
    real `claude -p --input-format stream-json`: a message written to stdin
    mid-turn is acted on WHILE the first turn runs (answered at 9.7s against a
    turn that ended at 40.5s), so handing it over is worth something.
    """
    d = sup.decide(pending=("c-1", "c-2"), in_flight=True, silent_for=5.0, sent=("c-1",))
    assert d.do == "send" and d.ids == ("c-2",), "the new comment waited for the turn"


def test_a_comment_already_handed_over_is_not_handed_over_twice():
    """A `working` comment stays pending, deliberately, so a restart recovers
    it. That must not read as new work every two seconds."""
    assert sup.decide(pending=("c-1",), in_flight=True, silent_for=5.0,
                      sent=("c-1",)).do == "wait"


def test_only_so_much_is_piled_onto_one_running_session():
    d = sup.decide(pending=("c-1", "c-2", "c-3", "c-4", "c-5"), in_flight=True,
                   silent_for=5.0, sent=("c-1", "c-2"), max_in_flight=3)
    assert d.do == "send" and d.ids == ("c-3",), "the cap counts what is already in flight"
    full = sup.decide(pending=("c-1", "c-2", "c-3", "c-4"), in_flight=True,
                      silent_for=5.0, sent=("c-1", "c-2", "c-3"), max_in_flight=3)
    assert full.do == "wait" and full.why, "a cap with no reason is a silent stall"


def test_the_cap_does_not_limit_a_backlog_handed_to_an_idle_session():
    """Idle means nothing is in flight, so the cap has nothing to count. A queue
    of five at boot is one prompt naming five, which is what the skill fans out."""
    d = sup.decide(pending=("c-1", "c-2", "c-3", "c-4", "c-5"), in_flight=False,
                   silent_for=0.0, max_in_flight=3)
    assert d.do == "send" and len(d.ids) == 5


def test_a_wedged_session_is_still_not_given_more_work():
    """The reason the old blanket wait was written, and it is kept: the stall
    branch has to win over the send branch."""
    d = sup.decide(pending=("c-1", "c-2"), in_flight=True, silent_for=200.0,
                   stall_after=150.0, sent=("c-1",))
    assert d.do == "restart"


def test_an_idle_session_with_nothing_pending_waits():
    assert sup.decide(pending=(), in_flight=False, silent_for=999.0).do == "wait"


def test_a_silent_turn_is_a_fault():
    """THE defect. Fourteen minutes of nothing looked exactly like working."""
    d = sup.decide(pending=("c-6",), in_flight=True, silent_for=200.0, stall_after=150.0)
    assert d.do == "restart"
    assert "200s" in d.why, "the reason has to name how long it was quiet"


def test_a_turn_that_is_merely_slow_is_left_alone():
    """Slow is not wedged. `sent` carries the comment the turn is already on,
    which is the loop's own state: a `working` comment stays pending so a
    restart recovers it, and must not be handed over a second time."""
    d = sup.decide(pending=("c-6",), in_flight=True, silent_for=149.0,
                   stall_after=150.0, sent=("c-6",))
    assert d.do == "wait"


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


@pytest.mark.parametrize(
    "call",
    [
        {"type": "tool_use", "name": "Bash",
         "input": {"command": "Rscript analysis/make_figures.R", "description": "rebuild"}},
        {"type": "tool_use", "name": "Glob", "input": {}},
        {"type": "tool_use", "name": "Read", "input": {"file_path": "main.tex"}},
    ],
)
def test_a_tool_call_is_not_something_the_author_needs_to_read(call):
    """The feed answers what the session is DOING, and `Bash: manuscriptor reply
    ...` is not that. The full stream, tool calls included, is still written to
    `agent-stream.jsonl`, which is where anyone debugging the session looks."""
    assert sup.summarize(assistant(call)) == []


def test_the_dispatch_of_a_teammate_is_still_visible():
    """The one exception, and it is not really a tool call: it is the work
    moving to someone else. It was the last thing anyone saw before the
    fourteen-minute silence this panel was built to end."""
    got = sup.summarize(assistant({
        "type": "tool_use", "name": "Agent",
        "input": {"subagent_type": "data-engineer", "description": "Rebuild fig3 as 6-panel"},
    }))
    assert len(got) == 1 and got[0].kind == "note"
    assert "teammate" in got[0].text and "Rebuild fig3 as 6-panel" in got[0].text


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


def test_a_feed_written_by_an_older_build_stops_showing_its_tool_calls(tmp_path):
    """dsp-bias had 17KB of `Bash: ...` on disk the moment this changed.

    The feed is rewritten whole by its one writer, so the stale lines clear
    themselves on the next turn -- but a session that is idle keeps showing
    them until then, and the page must not be the thing that decides.
    """
    from manuscriptor.server import feed as feed_mod
    p = tmp_path / "agent-progress.json"
    p.write_text(json.dumps({
        "state": "working", "working": ["c-0001"],
        "entries": [
            {"ts": "2026-07-27T07:00:00+00:00", "who": "agent", "kind": "tool",
             "text": "Bash: manuscriptor reply /paper c-0007"},
            {"ts": "2026-07-27T07:00:01+00:00", "who": "agent", "kind": "text",
             "text": "Rewriting the caption."},
        ],
    }), encoding="utf-8")
    got = feed_mod.read_feed(p)
    assert [e["kind"] for e in got["entries"]] == ["text"]
    assert got["state"] == "working" and got["working"] == ["c-0001"]


def test_one_answer_does_not_mean_the_session_is_idle(tmp_path):
    """Bookkeeping that only breaks once turns overlap.

    The probe that proved mid-turn hand-off works also showed the second
    message's `result` frame arriving while the FIRST turn was still running.
    A boolean in-flight flag reads idle there, which would hand the session
    more work on the strength of an answer to something else, and would tell
    the author the agent had stopped while it was still going.
    """
    from manuscriptor.server.feed import Feed
    s = sup.Session(tmp_path, feed=Feed(path=tmp_path / "p.json"), manuscriptor="ms")
    s._in_flight = 0
    s._note_sent(); s._note_sent()
    assert s.in_flight
    s._note_result()
    assert s.in_flight, "one result closed a turn that was not the only one running"
    s._note_result()
    assert not s.in_flight
    s._note_result()
    assert not s.in_flight, "a stray result must not drive the count below zero"


def test_the_loop_hands_a_mid_turn_comment_to_the_running_session(tmp_path):
    """The whole thing, driven: a comment arrives while a turn is running.

    `decide` being right is not enough -- the loop has to pass its own `_sent`
    in, or the cap counts nothing and a capped session looks empty. Driven
    against a stub session so the assertion is about the dispatcher, not about
    a model.
    """
    from manuscriptor.server import chat

    log = paths.comments(tmp_path)

    class Stub:
        alive = True
        silent_for = 0.0

        def __init__(self):
            self.sent = []
            self.in_flight = False

        def send(self, text):
            self.sent.append(text)
            self.in_flight = True
            return True

        def start(self): pass
        def stop(self): pass

    d = sup.Drain(tmp_path, manuscriptor="ms", build_dir=tmp_path / "build")
    stub = Stub()
    d.session = stub
    handed = []
    d.hand_over = lambda ids: (handed.append(tuple(ids)), stub.send(""), d._sent.update(ids))

    for i in (1, 2, 3, 4, 5):
        chat.append(log, {"id": f"c-000{i}", "kind": "comment", "block": "b-1",
                          "body": "x", "author": "bb"})
        d.step()

    assert handed[0] == ("c-0001",), handed
    assert stub.in_flight, "the stub must be mid-turn for the rest to mean anything"
    assert handed[1] == ("c-0002",), "a comment arriving mid-turn waited"
    assert handed[2] == ("c-0003",), handed
    assert len(handed) == 3, f"the cap of {d.max_in_flight} did not hold: {handed}"
