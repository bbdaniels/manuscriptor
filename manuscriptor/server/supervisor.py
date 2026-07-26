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
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from manuscriptor.server.feed import (  # the file the server reads
    KEEP, PROGRESS_NAME, Entry, Feed, now, progress_path, read_feed,
)

# Seconds of silence, mid-turn, before the session is treated as wedged. Long
# enough for a slow model call or a big file read, short enough that an author
# waiting on a figure is not left guessing for a quarter of an hour, which is
# what happened on 2026-07-26.
STALL_AFTER = 150.0

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


def summarize(event: dict) -> list[Entry]:
    """A stream event as feed entries. Pure, because this is the part worth testing.

    Everything the author would want to see, and nothing they would not: a tool's
    name and a short argument rather than its whole input, a result's first line
    rather than a file. Teammate entries are marked as such, because "which of
    them is doing this" is the first question a stalled figure raises.
    """
    kind = event.get("type")
    if kind == "system":
        sub = event.get("subtype")
        if sub == "init":
            return [Entry(now(), "agent", "note", "session ready")]
        return []
    if kind == "result":
        got = event.get("subtype") or "done"
        return [Entry(now(), "agent", "result", f"turn finished ({got})")]

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
                out.append(Entry(now(), who, "thinking", text[:400]))
        elif what == "text":
            text = " ".join((part.get("text") or "").split())
            if text:
                out.append(Entry(now(), who, "text", text[:400]))
        elif what == "tool_use":
            name = part.get("name") or "a tool"
            arg = _tool_argument(part.get("input") or {})
            out.append(Entry(now(), who, "tool", f"{name}{(': ' + arg) if arg else ''}"))
    return out

def _tool_argument(data: dict) -> str:
    """The one field of a tool call worth showing."""
    for field_name in ("command", "file_path", "pattern", "description", "query", "skill"):
        value = data.get(field_name)
        if isinstance(value, str) and value.strip():
            return " ".join(value.split())[:160]
    return ""


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
           stall_after: float = STALL_AFTER, alive: bool = True) -> Decision:
    """What the supervisor should do, given what it can see.

    Order matters. A dead session is restarted before anything else is
    considered, and a wedged one is restarted before more work is handed to it:
    sending a second comment to a session that has stopped answering the first is
    how one stuck figure becomes two.
    """
    if not alive:
        return Decision("restart", why="the session exited")
    if in_flight and silent_for >= stall_after:
        return Decision("restart", why=f"nothing for {int(silent_for)}s while working")
    if in_flight:
        return Decision("wait")
    if pending:
        return Decision("send", ids=tuple(pending))
    return Decision("wait")


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
        self._in_flight = False
        self._reader: threading.Thread | None = None
        self._lock = threading.Lock()

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
        self._in_flight = False
        self._reader = threading.Thread(target=self._pump, daemon=True)
        self._reader.start()
        self.feed.set(state="starting")

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    @property
    def in_flight(self) -> bool:
        return self._in_flight

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
        with self._lock:
            self._in_flight = True
            self._last_event = time.monotonic()
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
            entries = summarize(event)
            if entries:
                self.feed.add(entries)
            if event.get("type") == "result":
                with self._lock:
                    self._in_flight = False
                    self.turns += 1
                self.feed.set(state="idle")
        # The stream ended: the process is gone, and the loop will see it.
        with self._lock:
            self._in_flight = False

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
    "than guessing."
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
                 log: Path | None = None, build_dir: Path | None = None):
        self.root = Path(root).resolve()
        self.manuscriptor = manuscriptor
        self.claude = claude
        self.add_dirs = list(add_dirs)
        self.model = model
        self.agents = agents
        self.stall_after = stall_after
        self.poll = poll
        build = Path(build_dir) if build_dir else self.root / "build" / "manuscriptor"
        self.feed = Feed(path=progress_path(build))
        self.log = log if log is not None else build / "agent-stream.jsonl"
        self.session: Session | None = None
        self.restarts = 0
        self._sent: set[str] = set()

    # ------------------------------------------------------------------ pending

    def pending_ids(self) -> tuple[str, ...]:
        """Comments awaiting work, oldest first. Read from the log, like everything.

        A comment already marked `working` is still pending here, deliberately:
        that is what lets a restarted session pick up work its predecessor was
        holding when it went quiet.
        """
        from manuscriptor.server import chat

        return tuple(c.id for c in chat.pending(self.root / "comments.jsonl"))

    # -------------------------------------------------------------------- turns

    def boot(self) -> None:
        assert self.session is not None
        self.session.send(BOOT.format(root=self.root))
        self.feed.set(state="booting")

    def hand_over(self, ids: tuple[str, ...]) -> None:
        from manuscriptor.server import drain as drain_mod

        # Marked working BEFORE the message goes out, because the author is
        # watching the pin and a comment picked up should look picked up.
        for cid in ids:
            try:
                drain_mod.mark(self.root, cid, "working")
            except Exception:
                pass
        self.feed.set(state="working", working=ids)
        self.feed.note("picked up " + ", ".join(ids))
        assert self.session is not None
        self.session.send(WORK.format(ids=", ".join(ids), ms=self.manuscriptor, root=self.root))
        self._sent.update(ids)

    def restart(self, why: str) -> None:
        self.restarts += 1
        self.feed.note(f"restarting the session: {why}")
        if self.session is not None:
            self.session.stop()
        self.session = self.new_session()
        self.session.start()
        self.boot()
        self._sent.clear()

    def new_session(self) -> Session:
        return Session(
            self.root, feed=self.feed, manuscriptor=self.manuscriptor,
            claude=self.claude, add_dirs=self.add_dirs, model=self.model,
            agents=self.agents, log=self.log,
        )

    # --------------------------------------------------------------------- run

    def run(self, stop: threading.Event | None = None) -> None:
        stop = stop or threading.Event()
        self.session = self.new_session()
        self.session.start()
        self.boot()
        try:
            while not stop.is_set():
                s = self.session
                assert s is not None
                d = decide(pending=self.pending_ids(), in_flight=s.in_flight,
                           silent_for=s.silent_for, stall_after=self.stall_after,
                           alive=s.alive)
                if d.do == "restart":
                    self.restart(d.why)
                elif d.do == "send":
                    fresh = tuple(i for i in d.ids if i not in self._sent)
                    if fresh:
                        self.hand_over(fresh)
                stop.wait(self.poll)
        finally:
            self.feed.set(state="stopped")
            self.feed.flush()
            if self.session is not None:
                self.session.stop()
