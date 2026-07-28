"""One long-lived Claude session, fed work and watched.

This replaces a shell loop that restarted a print-mode session per wake. The old
shape had three faults, all of which cost the author real time on 2026-07-26:

**A silent session was indistinguishable from a working one.** A comment was
marked `working`, a subagent was dispatched, and nothing happened for fourteen
minutes: no output, no file changed, no way to tell whether it was thinking,
blocked on a permission it could not ask for, or dead. It happened twice in a
row on the same comment.

**Nothing could be said back to it.** A print session with a closed stdin cannot
be told "yes, use that figure" or "the second arm, not the first". An agent that
needed an input had no way to ask for one, so it either guessed or stopped.

**Every wake paid for a boot.** The park-and-wake protocol lived inside the
session's own prompt, so the loop restarted the process to get a fresh one.

The runtime already answers all three, and the answer is checked rather than
assumed (probed 2026-07-26): `--input-format stream-json` keeps one session alive
across many messages, so work is handed over rather than booted; `--output-format
stream-json` emits the assistant's thinking, its tool calls and their results as
they happen; and `--forward-subagent-text` brings a TEAMMATE's thinking and text
into the same stream, which is exactly what went dark.

Two rules this module keeps:

**The server never learns about Claude.** This runs in the drain process. It
writes two files -- the append-only comment log for milestones, and a rewritable
progress file for the live feed -- and the server only ever reads them. Nothing
here imports the server's app, and nothing in the server imports this.

**Silence is a fault.** A turn that emits no event for `stall_after` seconds is
treated as wedged: it is announced, the session is killed, and it is restarted.
A comment left `working` is still pending to the drain, so the work is picked up
again rather than stranded.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from manuscriptor.server import paths
from manuscriptor.server.feed import (  # the file the server reads
    KEEP, PROGRESS_NAME, Entry, Feed, now, progress_path, read_feed,
)

# Seconds of silence, mid-turn, before the session is treated as wedged. Long
# enough for a slow model call or a big file read, short enough that an author
# waiting on a figure is not left guessing for a quarter of an hour, which is
# what happened on 2026-07-26.
STALL_AFTER = 150.0

# How many comments may be in flight at one running session. Work arriving while
# a turn runs is handed straight over rather than queued, but not without bound:
# the author can leave six comments in a minute, and six turns dispatching their
# own subagents at once is a session doing bookkeeping instead of writing.
MAX_IN_FLIGHT = 3

# The tools this work legitimately needs. Named rather than bypassed: a print
# session cannot ask for permission, so anything it might reach for and cannot
# have becomes a hang, and a blanket bypass on the author's own repository is a
# different kind of bad afternoon.
ALLOWED_TOOLS = (
    "Read", "Glob", "Grep", "Edit", "Write", "NotebookEdit", "TodoWrite",
    "Agent", "Skill", "WebFetch", "WebSearch",
    "Bash(Rscript:*)", "Bash(R:*)", "Bash(python3:*)", "Bash(python:*)",
    "Bash(latexmk:*)", "Bash(pdflatex:*)", "Bash(xelatex:*)", "Bash(bibtex:*)",
    "Bash(stata-mp:*)", "Bash(stata:*)",
    "Bash(git status:*)", "Bash(git diff:*)", "Bash(git log:*)",
    "Bash(ls:*)", "Bash(cat:*)", "Bash(head:*)", "Bash(tail:*)", "Bash(wc:*)",
)


def summarize(event: dict, *, work: tuple[str, ...] = ()) -> list[Entry]:
    """A stream event as feed entries. Pure, because this is the part worth testing.

    Everything the author would want to see, and nothing they would not: a tool's
    name and a short argument rather than its whole input, a result's first line
    rather than a file. Teammate entries are marked as such, because "which of
    them is doing this" is the first question a stalled figure raises.

    `work` is which comment ids this event belongs to, decided by `Attributor`
    and passed in rather than worked out here: attribution is stateful (it
    remembers which teammate was dispatched for which comment) and this function
    is worth keeping pure.
    """
    kind = event.get("type")
    if kind == "system":
        sub = event.get("subtype")
        if sub == "init":
            return [Entry(now(), "agent", "note", "session ready", work)]
        return []
    if kind == "result":
        got = event.get("subtype") or "done"
        return [Entry(now(), "agent", "result", f"turn finished ({got})", work)]
    if kind == "user":
        return _edits(event, work=work)

    message = event.get("message") or {}
    if kind != "assistant" or not isinstance(message.get("content"), list):
        return []

    # A sidechain event belongs to a teammate rather than to the dispatcher.
    who = "teammate" if (event.get("isSidechain") or event.get("parent_tool_use_id")) else "agent"
    out: list[Entry] = []
    for part in message["content"]:
        what = part.get("type")
        if what == "thinking":
            text = " ".join((part.get("thinking") or "").split())
            if text:
                out.append(Entry(now(), who, "thinking", text[:400], work))
        elif what == "text":
            text = " ".join((part.get("text") or "").split())
            if text:
                out.append(Entry(now(), who, "text", text[:400], work))
        elif what == "tool_use":
            # A tool call is not something the author needs to read. `Bash:
            # manuscriptor reply /paper c-0007 "Fixed at the root in..."` says
            # nothing about the manuscript, and a dozen of them per turn push
            # the sentences that DO out of an 80-entry window. The whole stream,
            # tool calls included, is still written to `agent-stream.jsonl`.
            #
            # Dispatching a teammate is the exception, and it is not really a
            # tool call: it is the work moving to someone else, which was the
            # last thing anyone saw before the fourteen-minute silence this
            # panel exists to end. So it goes in as a note, in those words.
            if (part.get("name") or "") == "Agent":
                arg = _tool_argument(part.get("input") or {})
                out.append(Entry(now(), who, "note",
                                 "dispatched a teammate" + (f": {arg}" if arg else ""),
                                 work))
    return out

def _edits(event: dict, *, work: tuple[str, ...] = ()) -> list[Entry]:
    """What a write actually changed, when the stream says so.

    An `Edit` or a `Write` comes back with a `structuredPatch`: the file, and
    the hunks, whose lines carry their own `+` and `-`. That is a REAL `+N -M`,
    counted from the diff the tool itself produced, and it is the only exact one
    available here.

    IT IS ONLY AVAILABLE FOR THE SESSION'S OWN WRITES. `--forward-subagent-text`
    forwards a teammate's text and thinking and nothing else, so a teammate's
    edits produce no such event: in dsp-bias's 11,741-line stream, 83 dispatched
    teammates left 5 structured patches, all of them from the top-level session.
    The drain skill puts one teammate on each comment, so most work items will
    have no line counts and MUST NOT be given invented ones -- what is true for
    those is the outcome, the reply, and the paragraph, which the server reads
    from the comment log.

    A `note`, deliberately, rather than a new kind: `feed.KINDS` is the single
    rule for what the panel shows, and "edited main.tex - +6 -4" is a line the
    author reads in the same place as the rest of the narrative.
    """
    result = event.get("tool_use_result")
    if not isinstance(result, dict):
        return []
    hunks = result.get("structuredPatch")
    if not isinstance(hunks, list) or not hunks:
        return []
    added = removed = 0
    for hunk in hunks:
        for line in (hunk or {}).get("lines") or []:
            if not isinstance(line, str) or not line:
                continue
            if line[0] == "+":
                added += 1
            elif line[0] == "-":
                removed += 1
    if not (added or removed):
        return []
    name = Path(str(result.get("filePath") or "")).name or "a file"
    who = "teammate" if event.get("parent_tool_use_id") else "agent"
    return [Entry(now(), who, "note", f"edited {name} · +{added} −{removed}", work)]

def _tool_argument(data: dict) -> str:
    """The one field of a tool call worth showing."""
    for field_name in ("command", "file_path", "pattern", "description", "query", "skill"):
        value = data.get(field_name)
        if isinstance(value, str) and value.strip():
            return " ".join(value.split())[:160]
    return ""


# ============================================================ whose work is this
#
# A comment id, on every line the author reads. Until this existed the only link
# between a sentence and the request that caused it was the envelope's `working`
# list, which is current-state-only and rewritten, so the panel could say what
# was being worked NOW and could never say what any past line had been about.

CHAT_ID = re.compile(r"\bc-\d{4}\b")

# How many dispatched teammates to remember. A session dispatches on the order
# of one per comment and is restarted every few hours; this is a ceiling on a
# dictionary that would otherwise grow for as long as the process lives.
REMEMBER_DISPATCHES = 512


def named_ids(event: dict, among: tuple[str, ...]) -> tuple[str, ...]:
    """The comment ids this event names, restricted to the ones in flight.

    The drain's own prompts put the id in the text -- `Work these comments now:
    c-0042`, and the skill's teammate prompt quotes `THE COMMENT (c-0042)`
    verbatim -- so an event usually says what it is about. Restricted to what is
    actually in flight because a paragraph may quote an old id, and a line about
    c-0007 four hours after c-0007 was closed is a line about nothing.
    """
    if not among:
        return ()
    live = set(among)
    seen: list[str] = []
    for hit in CHAT_ID.findall(json.dumps(event, ensure_ascii=False, default=str)):
        if hit in live and hit not in seen:
            seen.append(hit)
    return tuple(seen)


class Attributor:
    """Which work item a stream event belongs to. Stateful, on purpose.

    THE PRECISE SIGNAL IS `parent_tool_use_id`. A dispatched teammate's every
    event carries the id of the `Agent` tool call that started it, and that call
    names its comment in the prompt it was given. So the dispatch is remembered
    and every line the teammate then emits resolves to exactly one comment,
    which is what makes three comments in flight readable as three threads
    rather than as one interleaved smear. Checked against a real 11,741-line
    stream: 5,811 of its events carried a parent that was a known dispatch, and
    `isSidechain` -- which the teammate check also looks for -- never appeared
    once.

    WHEN THERE IS NO SUCH SIGNAL, the rules in order:

    * the event names exactly one in-flight comment -> that one;
    * it names several -> those several;
    * it names none and one comment is in flight -> that one;
    * it names none and several are in flight -> ALL OF THEM. The line genuinely
      cannot be told apart, and the honest record of "the session said this
      while carrying these three" is the set. The panel shows such a line under
      each of them, marked as shared. Picking one would be inventing the link
      the whole ledger exists to make trustworthy;
    * nothing is in flight -> none. Booting, restarting, idle chatter: it
      belongs to the session and to no request, and the live feed is where the
      author reads it.
    """

    def __init__(self) -> None:
        self._by_dispatch: dict[str, tuple[str, ...]] = {}
        self._working: tuple[str, ...] = ()

    def aim(self, ids) -> None:
        """The comments the drain currently has in flight."""
        self._working = tuple(ids)

    def stamp(self, event: dict) -> tuple[str, ...]:
        """The work this event belongs to, learning from it as it goes."""
        parent = event.get("parent_tool_use_id")
        if parent and parent in self._by_dispatch:
            got = self._by_dispatch[parent]
        else:
            named = named_ids(event, self._working)
            got = named or self._working
        self._learn(event, got)
        return got

    def _learn(self, event: dict, fallback: tuple[str, ...]) -> None:
        message = event.get("message") or {}
        if event.get("type") != "assistant" or not isinstance(message.get("content"), list):
            return
        for part in message["content"]:
            if part.get("type") != "tool_use" or (part.get("name") or "") != "Agent":
                continue
            tool_id = part.get("id")
            if not isinstance(tool_id, str) or not tool_id:
                continue
            named = named_ids({"input": part.get("input") or {}}, self._working)
            self._by_dispatch[tool_id] = named or fallback
            while len(self._by_dispatch) > REMEMBER_DISPATCHES:
                self._by_dispatch.pop(next(iter(self._by_dispatch)))


# ============================================================== what to do next
#
# The loop's decisions are a pure function of what it can observe, so they are
# tested by driving them rather than by watching a real session and hoping the
# interesting case turns up. The interesting case here is the one that cost the
# afternoon: work in flight, and nothing coming back.


@dataclass(frozen=True)
class Decision:
    do: str                    # "wait" | "send" | "restart"
    ids: tuple[str, ...] = ()
    why: str = ""


def decide(*, pending: tuple[str, ...], in_flight: bool, silent_for: float,
           stall_after: float = STALL_AFTER, alive: bool = True,
           sent: tuple[str, ...] | frozenset[str] = (),
           max_in_flight: int = MAX_IN_FLIGHT) -> Decision:
    """What the supervisor should do, given what it can see.

    Order matters. A dead session is restarted before anything else is
    considered, and a wedged one is restarted before more work is handed to it:
    sending a second comment to a session that has stopped answering the first is
    how one stuck figure becomes two.

    A RUNNING session, though, is given new work as it arrives. This used to
    wait, and the waiting was the whole of the missing parallelism: on dsp-bias
    every one of eleven comments was worked to completion before the next
    started, because a comment left four seconds after a turn began was invisible
    until that turn ended -- 8m54s for one of them, against 0-2s for every
    comment that happened to land while the loop was idle. The machinery
    underneath was always concurrent (`splice` holds a per-file lock across
    read-locate-write, and eight concurrent splices all land), and the drain
    skill has always said to dispatch together; this branch was what none of it
    ever got to do. Measured against a real `claude -p --input-format
    stream-json`: a message written mid-turn is acted on while the first turn is
    still running.

    `sent` is what has already been handed over and is why a `working` comment --
    still pending, deliberately, so a restart recovers it -- is not handed over
    again every poll. The cap bounds only what is added to a RUNNING session; an
    idle one takes the whole backlog in one prompt, which is the fan-out the
    skill describes.
    """
    if not alive:
        return Decision("restart", why="the session exited")
    if in_flight and silent_for >= stall_after:
        return Decision("restart", why=f"nothing for {int(silent_for)}s while working")
    already = frozenset(sent)
    fresh = tuple(c for c in pending if c not in already)
    if not fresh:
        return Decision("wait")
    if not in_flight:
        return Decision("send", ids=fresh)
    room = max_in_flight - sum(1 for c in pending if c in already)
    if room <= 0:
        return Decision("wait", why=f"{max_in_flight} already in flight")
    return Decision("send", ids=fresh[:room])


# ================================================================== the session


class Session:
    """One `claude` process, held open, with its stream read as it arrives."""

    def __init__(self, root: Path, *, feed: Feed, manuscriptor: str,
                 claude: str | None = None, add_dirs=(), model: str | None = None,
                 agents: str | None = None, log: Path | None = None):
        self.root = Path(root).resolve()
        self.feed = feed
        self.manuscriptor = manuscriptor
        self.claude = claude or shutil.which("claude") or "claude"
        self.add_dirs = [str(d) for d in add_dirs]
        self.model = model
        self.agents = agents
        self.log = log
        self.proc: subprocess.Popen | None = None
        self.turns = 0
        self._last_event = time.monotonic()
        # A COUNT, not a flag. Turns overlap now: a message handed over mid-turn
        # is acted on while the first is still running, and its `result` frame
        # arrives first. A boolean read idle there -- which would hand the
        # session more work on the strength of an answer to something else, and
        # would tell the author it had stopped while it was still going.
        self._in_flight = 0
        self._reader: threading.Thread | None = None
        self._lock = threading.Lock()
        # Per session, because what it remembers is this session's dispatches.
        # A restart is a new process with new tool call ids, and carrying the
        # old map across would attribute nothing and could mis-attribute.
        self.attribution = Attributor()

    def aim(self, ids) -> None:
        """Tell the stream reader which comments are in flight."""
        self.attribution.aim(ids)

    # -------------------------------------------------------------- the process

    def argv(self) -> list[str]:
        args = [
            self.claude, "-p", "--verbose",
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            # A teammate's thinking and text come into this stream too. Without
            # it a dispatched subagent is a black box, which is exactly how a
            # wedged figure rebuild looked from outside.
            "--forward-subagent-text",
            "--permission-mode", "acceptEdits",
            "--allowedTools", *ALLOWED_TOOLS,
        ]
        if self.model:
            args += ["--model", self.model]
        if self.agents:
            args += ["--agents", self.agents]
        for d in self.add_dirs:
            args += ["--add-dir", d]
        return args

    def start(self) -> None:
        self.proc = subprocess.Popen(
            self.argv(), cwd=str(self.root),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1, start_new_session=True,
        )
        self._last_event = time.monotonic()
        self._in_flight = 0
        self._reader = threading.Thread(target=self._pump, daemon=True)
        self._reader.start()
        self.feed.set(state="starting")

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    @property
    def in_flight(self) -> bool:
        return self._in_flight > 0

    @property
    def turns_running(self) -> int:
        return self._in_flight

    def _note_sent(self) -> None:
        with self._lock:
            self._in_flight += 1
            self._last_event = time.monotonic()

    def _note_result(self) -> None:
        """One turn answered. Idle only when the last of them has."""
        with self._lock:
            self._in_flight = max(0, self._in_flight - 1)
            self.turns += 1
            done = self._in_flight == 0
        if done:
            self.feed.set(state="idle")

    @property
    def silent_for(self) -> float:
        return time.monotonic() - self._last_event

    def send(self, text: str) -> bool:
        """Hand the session a message. This is the whole reason for stream input:
        work is given to a session that is already awake, and an answer to a
        question it asked is given the same way."""
        if not self.alive or self.proc.stdin is None:
            return False
        frame = {"type": "user", "message": {"role": "user", "content": text}}
        try:
            self.proc.stdin.write(json.dumps(frame) + "\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, ValueError, OSError):
            return False
        self._note_sent()
        return True

    def stop(self) -> None:
        proc, self.proc = self.proc, None
        if proc is None or proc.poll() is not None:
            return
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                proc.terminate()
            except OSError:
                pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass

    # ---------------------------------------------------------------- the stream

    def _pump(self) -> None:
        proc = self.proc
        if proc is None or proc.stdout is None:
            return
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            with self._lock:
                self._last_event = time.monotonic()
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if self.log is not None:
                self._append_log(line)
            entries = summarize(event, work=self.attribution.stamp(event))
            if entries:
                self.feed.add(entries)
            if event.get("type") == "result":
                self._note_result()
        # The stream ended: the process is gone, and the loop will see it.
        with self._lock:
            self._in_flight = 0

    def _append_log(self, line: str) -> None:
        try:
            with open(self.log, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass


# =================================================================== the loop


BOOT = (
    "You are the standing Manuscriptor drain for {root}. Boot now: use the "
    "manuscriptor-drain skill, and load the owning vault project's context "
    "(Dashboard, Tasks ## Active, Technical Notes) read-only. Then wait. "
    "Work is handed to you as messages; there is nothing to poll and nothing to "
    "park. Reply with one line when you are ready."
)

WORK = (
    "Work these comments now: {ids}.\n"
    "`{ms} proc {root} --json` presents them. Per the skill: one teammate per "
    "comment, ONE BLOCK PER WRITE, `{ms} reply {root} <id> \"…\"` when words are "
    "the right answer, `{ms} state {root} <id> done` when the edit has landed, "
    "orphaned when its paragraph is gone.\n"
    "Say what you are about to do before you delegate, in one line, so the author "
    "reading the feed knows what is happening. If you need a decision only the "
    "author can make, ask it with `reply` and leave the comment working rather "
    "than guessing.\n"
    "MORE WORK MAY ARRIVE WHILE YOU ARE STILL ON THIS. The author types while you "
    "work, and a comment that lands mid-turn is handed straight to you rather than "
    "held until you finish. Dispatch it at once, alongside what is already running. "
    "Do not finish the comment you are on first: two teammates editing two "
    "paragraphs of the same file at the same time is safe by construction, and "
    "waiting is what made a comment left four seconds late wait nine minutes."
)


class Drain:
    """The dispatcher: watches the log, hands work over, and watches the watcher.

    The loop that used to live inside the session's own prompt lives here, where
    it can be observed and where a wedge can be broken. The session is told what
    to do and never asked to wait for anything.
    """

    def __init__(self, root: Path, *, manuscriptor: str, claude: str | None = None,
                 add_dirs=(), model: str | None = None, agents: str | None = None,
                 stall_after: float = STALL_AFTER, poll: float = 2.0,
                 log: Path | None = None, build_dir: Path | None = None,
                 max_in_flight: int = MAX_IN_FLIGHT):
        self.root = Path(root).resolve()
        self.manuscriptor = manuscriptor
        self.claude = claude
        self.add_dirs = list(add_dirs)
        self.model = model
        self.agents = agents
        self.stall_after = stall_after
        self.poll = poll
        self.max_in_flight = max_in_flight
        agent = Path(build_dir) if build_dir else paths.agent_dir(self.root)
        self.feed = Feed(path=progress_path(agent))
        self.log = log if log is not None else agent / "agent-stream.jsonl"
        self.session: Session | None = None
        self.restarts = 0
        self._sent: set[str] = set()
        self._working: tuple[str, ...] = ()

    # ------------------------------------------------------------------ pending

    def pending_ids(self) -> tuple[str, ...]:
        """Comments awaiting work, oldest first. Read from the log, like everything.

        A comment already marked `working` is still pending here, deliberately:
        that is what lets a restarted session pick up work its predecessor was
        holding when it went quiet.
        """
        from manuscriptor.server import chat

        return tuple(c.id for c in chat.pending(paths.comments(self.root)))

    # -------------------------------------------------------------------- turns

    def boot(self) -> None:
        assert self.session is not None
        self.session.send(BOOT.format(root=self.root))
        self.feed.set(state="booting")

    def hand_over(self, ids: tuple[str, ...], pending: tuple[str, ...] = ()) -> None:
        from manuscriptor.server import drain as drain_mod

        # Marked working BEFORE the message goes out, because the author is
        # watching the pin and a comment picked up should look picked up.
        for cid in ids:
            try:
                drain_mod.mark(self.root, cid, "working")
            except Exception:
                pass
        self._sent.update(ids)
        # Aimed BEFORE the note, so "picked up c-0042" is itself stamped with
        # the work it announces rather than with whatever came before it.
        self.aim(pending or ids)
        self.feed.set(state="working")
        self.feed.note("picked up " + ", ".join(ids), work=tuple(ids))
        assert self.session is not None
        self.session.send(WORK.format(ids=", ".join(ids), ms=self.manuscriptor, root=self.root))

    def aim(self, pending: tuple[str, ...]) -> None:
        """Point the feed and the attribution at what is REALLY in flight.

        Everything handed over that has not yet reached a terminal state -- not
        the latest batch. `hand_over` used to write its own `ids` straight into
        the envelope, so handing a second comment to a running session erased
        the first from `working`: the author watched the pin move off a
        paragraph still being worked, and every line the session then emitted
        would have been stamped with the newcomer alone. The set shrinks by
        itself, because a comment the agent marks `done` leaves `pending`.
        """
        live = tuple(c for c in pending if c in self._sent)
        if live != self._working:
            self._working = live
            self.feed.set(working=live)
        if self.session is not None:
            self.session.aim(live)

    def restart(self, why: str) -> None:
        self.restarts += 1
        # Stamped with what was in flight: "the session was restarted while
        # working this" is part of that work item's history, and reading it
        # under the item is how the author learns why an answer arrived twice.
        self.feed.note(f"restarting the session: {why}", work=self._working)
        if self.session is not None:
            self.session.stop()
        self.session = self.new_session()
        self.session.start()
        self.boot()
        self._sent.clear()
        self._working = ()
        self.feed.set(working=())

    def new_session(self) -> Session:
        return Session(
            self.root, feed=self.feed, manuscriptor=self.manuscriptor,
            claude=self.claude, add_dirs=self.add_dirs, model=self.model,
            agents=self.agents, log=self.log,
        )

    # --------------------------------------------------------------------- run

    def step(self) -> Decision:
        """One pass of the loop: look, decide, act. Separate from `run` so a test
        can drive the real thing rather than a second copy of it -- passing
        `_sent` in is load-bearing (the cap counts what is already in flight, so
        a filter anywhere else would let a capped session look empty)."""
        s = self.session
        assert s is not None
        pending = self.pending_ids()
        d = decide(pending=pending, in_flight=s.in_flight,
                   silent_for=s.silent_for, stall_after=self.stall_after,
                   alive=s.alive, sent=frozenset(self._sent),
                   max_in_flight=self.max_in_flight)
        if d.do == "restart":
            self.restart(d.why)
        elif d.do == "send":
            self.hand_over(d.ids, pending)
        else:
            # Every pass, not only on a send: this is what retires a comment
            # from `working` when the agent marks it done, and the pending list
            # was read a line ago anyway.
            self.aim(pending)
        return d

    def run(self, stop: threading.Event | None = None) -> None:
        stop = stop or threading.Event()
        self.session = self.new_session()
        self.session.start()
        self.boot()
        try:
            while not stop.is_set():
                s = self.session
                assert s is not None
                self.step()
                stop.wait(self.poll)
        finally:
            self.feed.set(state="stopped")
            self.feed.flush()
            if self.session is not None:
                self.session.stop()
