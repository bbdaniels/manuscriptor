"""manuscriptor CLI.

`evidence` works today: it is the absorbed cite-evidence pipeline, renamed.
The rest arrive with their milestones and exit with a clear message until then,
rather than failing obscurely.
"""
from __future__ import annotations

import argparse
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import __version__
from .server import paths

_NOT_YET = "not implemented yet (lands with {milestone}); see the Phase 1 design in the vault"


def _resolve_manuscript_paths(manuscript_dir: Path, main: str | None, bib: str | None) -> tuple[Path, Path]:
    if main:
        main_path = manuscript_dir / main
    else:
        from manuscriptor.source import root as root_mod

        try:
            main_path = manuscript_dir / root_mod.choose_main(manuscript_dir)
        except (LookupError, FileNotFoundError) as exc:
            sys.exit(str(exc))
    if not main_path.exists():
        sys.exit(f"main TeX not found: {main_path}")

    if bib:
        bib_path = manuscript_dir / bib
    else:
        candidates = sorted(manuscript_dir.glob("*.bib"))
        if not candidates:
            sys.exit(f"No .bib file found in {manuscript_dir}; pass --bib")
        bib_path = candidates[0]
    if not bib_path.exists():
        sys.exit(f"bib file not found: {bib_path}")
    return main_path, bib_path


def cmd_blocks(args: argparse.Namespace) -> int:
    from manuscriptor.source.blocks import segment
    from manuscriptor.source.flatten import flatten
    from manuscriptor.server import producers

    main_tex = Path(args.main_tex).resolve()
    flat = flatten(main_tex)
    blocks = producers.apply(
        segment(flat), producers.scan(main_tex.parent), root_file=main_tex
    )
    for b in blocks:
        lock = "  " if b.editable else "RO"
        head = " ".join(b.source_text.split())[:64]
        print(f"{lock} {b.id:16s} {b.kind:10s} {b.file.name}:{b.line_start:<5d} {head}")
    n_ed = sum(1 for b in blocks if b.editable)
    print(f"\n{len(blocks)} blocks · {n_ed} editable · "
          f"{len({b.file for b in blocks})} files · "
          f"{sum(len(b.includes) for b in blocks)} inline includes")
    if flat.missing:
        print(f"unresolved includes: {sorted(set(flat.missing))}")
    return 0


def cmd_build(args: argparse.Namespace) -> int:
    from importlib import resources
    from jinja2 import Template

    from manuscriptor.server import build as build_mod
    from manuscriptor.templates.ext import load as _extensions

    out = Path(args.output).resolve() if args.output else None
    b = build_mod.build(Path(args.manuscript).resolve(), main=args.main, bib=args.bib, output_dir=out)
    out = out or paths.cache(args.manuscript)

    tpl = resources.files("manuscriptor.templates").joinpath("index.html.j2").read_text(encoding="utf-8")
    css = resources.files("manuscriptor.templates.static").joinpath("styles.css").read_text(encoding="utf-8")
    js = resources.files("manuscriptor.templates.static").joinpath("viewer.js").read_text(encoding="utf-8")
    page = Template(tpl).render(ms=b.blob, styles_css=css, viewer_js=js, extensions=_extensions())
    (out / "index.html").write_text(page, encoding="utf-8")

    st = b.blob["stats"]
    print(f"{len(b.blob['blocks'])} blocks · {st['files']} files · {st['cites']} citations · "
          f"{st['values']} computed values · {st['exhibits']} exhibits")
    for key, label in (("unanchored", "unanchored"), ("unresolved_refs", "unresolved refs"),
                       ("missing_includes", "missing includes")):
        if b.blob["diagnostics"].get(key):
            print(f"  {len(b.blob['diagnostics'][key])} {label}")
    print(f"done -> {out / 'index.html'}")
    return 0


# ------------------------------------------------------------ serve --with-agent
#
# THE LAUNCH LIVES HERE AND NOWHERE ELSE. `manuscriptor/server/` has zero
# knowledge of Claude and must not learn that a process exists; the two halves
# share a filesystem and meet at the `.tex` tree and `comments.jsonl`. `cli.py`
# is allowed to start both, because it is the thing the author typed.

def agent_prompt(manuscript_dir, manuscriptor: str) -> str:
    """The standing session's instructions. One session, many wakes.

    Verified 2026-07-23: a print-mode session survives parking a background
    task and is re-woken when it finishes, which is what makes a persistent
    drain possible at all. Marking `working` before reading anything is the
    latency fix: the author's pin moves in seconds, and the vault reading
    happens while he can already see it is alive.
    """
    d = str(Path(manuscript_dir).resolve())
    ms = manuscriptor
    return (
        f"You are the standing Manuscriptor drain for {d}. Boot once, now: use the "
        "manuscriptor-drain skill and load the owning vault project's context. Then loop.\n"
        f"1. Run `{ms} proc {d} --json`.\n"
        f"2. If items are pending: for EACH item, run `{ms} state {d} <id> working` "
        "IMMEDIATELY, before reading anything else; the author is watching the pin. "
        "Then work them per the skill: one subagent per comment, one block per "
        f"write, `{ms} reply` when words are needed, `{ms} state {d} <id> done` "
        "when the edit has landed.\n"
        f"3. Park: start `{ms} proc {d} --wait` as a BACKGROUND task "
        "(run_in_background: true) and end your turn. The task finishing means new "
        "comments are on disk; when it wakes you, return to step 1.\n"
        "4. After roughly 20 wakes, or when your context has grown long, exit "
        "cleanly instead of parking; the loop restarts you fresh.\n"
        "Never stop the loop because one comment failed: reply with why, mark it "
        "orphaned if its paragraph is gone, and continue. Do nothing the comments "
        "did not ask for."
    )


def agent_log_path(manuscript_dir: Path) -> Path:
    """Where the session's output goes.

    Durable but private, so it lives in the hidden directory beside the drain's
    other logs rather than under the cache: a rebuild does not reconstruct what
    the agent said, and `clean` has no business taking it.
    """
    paths.ensure(manuscript_dir)
    out = paths.agent_dir(manuscript_dir)
    out.mkdir(parents=True, exist_ok=True)
    return out / "agent.log"


def agent_loop_script(manuscript_dir: Path, *, claude: str, manuscriptor: str,
                      add_dirs: list | tuple = ()) -> str:
    """The drain loop, as a script the author can read.

    ONE PERSISTENT SESSION, not a cold start per comment. The old shape paid
    ~90 seconds of session boot per wake before the first visible state; this
    one boots once, parks `proc --wait` as a background task inside the
    session, and is re-woken by the task finishing, so a comment's pin moves
    in seconds. The shell's only job is restarting the session when it exits:
    fresh after ~20 wakes by its own choice, or after a crash, with a backoff
    because the common crash is a credential problem that will not fix itself
    in the next hundred milliseconds.

    `add_dirs` carries the git repository root when the manuscript lives in a
    subdirectory of it: a figure comment names a producing script in
    `analysis/`, and a session scoped to `paper/` alone was blocked from the
    very file the comment is about.
    """
    d = str(Path(manuscript_dir).resolve())
    adds = "".join(f" --add-dir {_sh(str(a))}" for a in add_dirs)
    prompt = agent_prompt(d, manuscriptor)
    return f"""#!/bin/sh
# Written by `manuscriptor serve`. One persistent session parks on the comment
# log (a BACKGROUND task inside the session) and wakes per comment. Kill the
# server and this goes with it: it runs in its own process group and the
# server kills the group.
DIR={_sh(d)}
cd "$DIR" || exit 1
while :; do
  echo "--- $(date '+%Y-%m-%d %H:%M:%S')  session starting"
  {_sh(claude)} -p {_sh(prompt)} --permission-mode acceptEdits{adds} || sleep 5
  sleep 2
done
"""


def _sh(value: str) -> str:
    """One shell-quoted argument. The manuscript path is the author's, not ours."""
    return "'" + str(value).replace("'", "'\\''") + "'"


def manuscriptor_command(out_dir: Path, *, which=None) -> str:
    """One executable that runs manuscriptor, whatever the install looks like.

    The console script is not always on PATH -- it was not on this machine until
    it was symlinked into `~/.local/bin`. The obvious fallback,
    `sys.executable -m manuscriptor.cli`, is three words, and the loop runs this
    as ONE, so it would be looked up as a file with that literal name and the
    watcher would never run. A shim keeps it one token and survives a path with
    spaces in it.
    """
    which = which or shutil.which
    found = which("manuscriptor")
    if found:
        return found
    shim = Path(out_dir) / "manuscriptor-shim.sh"
    shim.write_text(f'#!/bin/sh\nexec {_sh(sys.executable)} -m manuscriptor.cli "$@"\n',
                    encoding="utf-8")
    shim.chmod(0o755)
    return str(shim)


def spawn_group(argv: list[str], *, cwd: Path, log_path: Path,
                env: dict | None = None, pass_fds: tuple = ()) -> subprocess.Popen:
    """Start a child in its OWN process group, with its output on disk.

    The group is the point. A drain loop starts a `claude`, which starts its own
    children; signalling the shell alone would leave those running, and a
    session still editing a manuscript after the server is gone is the worst
    failure this design has.

    `pass_fds` carries the queue lock down to the drain. Popen closes inherited
    descriptors otherwise, and a lock the detached drain does not hold would be
    released the moment the server was killed -- while the drain it started kept
    editing.
    """
    log = open(log_path, "a", buffering=1, encoding="utf-8")
    try:
        return subprocess.Popen(
            argv, cwd=str(cwd), stdout=log, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, start_new_session=True,
            env=env, pass_fds=tuple(pass_fds),
        )
    finally:
        log.close()   # the child holds its own descriptor


def terminate_group(proc: subprocess.Popen | None, *, grace: float = 5.0) -> None:
    """Take the whole tree down, not just the process at the top of it."""
    if proc is None or (proc.poll() is not None and _group_gone(proc)):
        return
    try:
        pgid = os.getpgid(proc.pid)
    except (ProcessLookupError, PermissionError):
        return
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return

    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            break
        time.sleep(0.05)

    # The launcher exiting is not the same as the group being empty: a `claude`
    # it started can outlive it. Sweep whatever is left.
    try:
        os.killpg(pgid, 0)
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        proc.wait(timeout=2)
    except Exception:
        pass


def _group_gone(proc: subprocess.Popen) -> bool:
    try:
        os.killpg(os.getpgid(proc.pid), 0)
    except (ProcessLookupError, PermissionError):
        return True
    return False


def repo_root(manuscript_dir: Path) -> Path | None:
    """The git repository root above the manuscript, when there is one.

    The producing scripts a comment names routinely live beside the
    manuscript (`analysis/` next to `paper/`), and a session scoped to the
    manuscript alone is blocked from the very file the comment is about.
    """
    try:
        run = subprocess.run(
            ["git", "-C", str(manuscript_dir), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if run.returncode != 0:
        return None
    top = Path(run.stdout.strip()).resolve()
    return top if top != Path(manuscript_dir).resolve() else None


def start_agent(manuscript_dir: Path, *, lock=None) -> tuple[subprocess.Popen, Path]:
    """Launch the standing drain beside the server. Says what it started.

    The drain is `manuscriptor drain`, a supervisor holding ONE long-lived
    session that is handed work over stdin. It replaces a shell loop around
    print-mode sessions, which had no way to tell a thinking session from a
    wedged one: a comment sat marked `working` for fourteen minutes on
    2026-07-26 with a dispatched teammate that had gone silent, twice.
    """
    claude = shutil.which("claude")
    if not claude:
        sys.exit(
            "the drain needs the `claude` CLI on PATH and it is not there. "
            "Install Claude Code, or run with --no-agent and drain by hand with "
            "`manuscriptor proc <dir>` in a session of your own."
        )
    from manuscriptor.server import feed as feed_mod
    from manuscriptor.server.supervisor import STALL_AFTER

    log = agent_log_path(manuscript_dir)
    ms = manuscriptor_command(log.parent)
    # `ms` may be "python3 -m manuscriptor" rather than a single binary.
    argv = [*ms.split(), "drain", str(Path(manuscript_dir).resolve())]
    # The queue lock goes down with it: the drain is detached, so it outlives a
    # killed server, and the lock has to say so.
    fds = () if lock is None or lock.fd is None else (lock.fd,)
    env = lock.child_env() if lock is not None else None
    proc = spawn_group(argv, cwd=Path(manuscript_dir).resolve(), log_path=log,
                       env=env, pass_fds=fds)

    print(f"  agent  one supervised session, fed as work arrives   (pid {proc.pid})")
    print(f"         silence past {int(STALL_AFTER)}s mid-turn is treated as wedged "
          "and restarted")
    print(f"         feed {feed_mod.progress_path(log.parent)}")
    # Named beside the feed, because they are read for opposite reasons: the
    # feed is what it is doing and is rewritten, the history is what it has
    # done and is appended to. The panel reads both.
    print(f"         history {feed_mod.history_path(log.parent)}")
    print(f"         log {log}")
    return proc, log


def refusal(lock) -> str:
    """What to say when the queue already has a drain.

    Names the pid, because the author's next question is always "which one",
    and on 2026-07-27 there was no way to answer it: two servers, two headless
    sessions, one working tree, and nothing on disk saying either existed.
    """
    pid = lock.holder()
    who = f"pid {pid}" if pid else "another process"
    return (f"{who} is already draining {lock.root}\n"
            f"  its claim: {lock.path}\n"
            "  a second drain on one comment queue means two sessions editing "
            "one working tree, which is how four files were rewritten under "
            "their own workers on 2026-07-27.")


def cmd_drain(args: argparse.Namespace) -> int:
    """Run the standing drain: one supervised session, fed work as it arrives.

    Replaces the shell loop that restarted a print session per wake. The loop
    lives in `server/supervisor.py` now, where a session that goes quiet can be
    noticed and restarted, and where what it is doing can be written somewhere
    the page can read. Run in the foreground here so `serve` can own it as a
    child, and so it can be run by hand when something needs watching.
    """
    import threading

    from manuscriptor.server.single import DrainLock
    from manuscriptor.server.supervisor import Drain

    d = Path(args.manuscript).resolve()
    root = repo_root(d)
    out = paths.agent_dir(d)
    out.mkdir(parents=True, exist_ok=True)

    # ONE DRAIN PER QUEUE. Started by `serve` the lock is inherited, because the
    # server took it before spawning and this is the process it took it for.
    # Typed by hand it is taken here, which is the other way two sessions came
    # to fan out into one working tree.
    lock = DrainLock.inherited(d)
    if lock is None:
        lock = DrainLock(d)
        if not lock.acquire():
            print(refusal(lock), file=sys.stderr)
            return 1

    drain = Drain(
        d,
        manuscriptor=manuscriptor_command(out),
        add_dirs=[root] if root else [],
        model=args.model or None,
        stall_after=float(args.stall_after),
    )
    stop = threading.Event()

    def bye(*_):
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, bye)
        except (ValueError, OSError):
            pass
    print(f"drain  {d}")
    print(f"       feed {drain.feed.path}")
    print(f"       stream {drain.log}")
    if lock.degraded:
        print("       note: this filesystem cannot lock, so a second drain on "
              "this queue would not be noticed")
    try:
        drain.run(stop)
    finally:
        lock.release()
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    from manuscriptor.server.app import serve

    d = Path(args.manuscript).resolve()
    if args.with_agent and args.read_only:
        sys.exit(
            "--with-agent and --read-only contradict each other: an agent that "
            "cannot write cannot answer a comment, and the comment log is not "
            "written either. Pick one."
        )

    # THE AGENT IS THE DEFAULT. The full workflow is: open a manuscript, leave
    # a comment, have it answered; that only holds if serve runs the drain
    # without being asked. --no-agent opts out; --read-only implies out,
    # silently, because wanting to read a paper is a mode, not a mistake. A
    # missing `claude` CLI downgrades to serving without the agent; only the
    # EXPLICIT --with-agent makes that a hard error, because then the author
    # asked for it by name.
    # THE DRAIN AGENT FOLLOWS THE CURRENT DOCUMENT. With the tree view the open
    # document's comments live in <document root>/comments.jsonl -- a subfolder
    # when the paper sits below the served project root -- and the drain reads
    # that file directly (drain.collect opens `<dir>/comments.jsonl`). So the
    # agent binds to the CURRENT document's root, not the top-level served dir,
    # and is RE-BOUND when the reader switches documents: the Session fires
    # `on_switch(new_root)` and we restart the drain there. For a single-directory
    # serve the root is `d` throughout, so nothing changes. The switch is driven
    # from an async handler, so the rebind runs off the event loop in a thread,
    # guarded by a lock + a `down` flag so shutdown can't be out-raced.
    # ONE DRAIN PER COMMENT QUEUE, across processes. Two servers reach the same
    # queue while looking unrelated at every other layer -- different arguments,
    # different resolved directories, different derived ports, one
    # `current_root`. The claim is taken on the document root here and handed to
    # the drain child, and a server that cannot take it serves anyway: reading a
    # manuscript somebody else is draining is legitimate, and only the second
    # AGENT is the damage.
    from manuscriptor.server.single import DrainLock

    want_agent = not args.no_agent and not args.read_only
    have_claude = bool(args.with_agent or shutil.which("claude"))
    agent_box = {"proc": None, "down": False, "lock": None}
    agent_lock = threading.Lock()

    def _release():
        """Stop the drain, THEN drop the claim. The child holds the same lock,
        so releasing first would free the queue while a drain still ran on it."""
        if agent_box["proc"] is not None:
            terminate_group(agent_box["proc"])
            agent_box["proc"] = None
        if agent_box["lock"] is not None:
            agent_box["lock"].release()
            agent_box["lock"] = None

    def _bind(new_root):
        """Take the queue and start the drain on it. Caller holds `agent_lock`."""
        claim = DrainLock(new_root)
        if not claim.acquire():
            print("  agent  " + refusal(claim).replace("\n  ", "\n         "))
            print("         serving without an agent; your comments queue for "
                  "the drain that is already running.")
            return
        agent_box["lock"] = claim
        agent_box["proc"], _ = start_agent(new_root, lock=claim)
        if claim.degraded:
            print("         note: this filesystem cannot lock, so a second "
                  "drain on this queue would not be noticed")

    def _rebind(new_root):
        def work():
            with agent_lock:
                if agent_box["down"]:
                    return
                # The claim moves with the document. Releasing the old root is
                # what keeps it drainable after the reader switches away.
                _release()
                _bind(new_root)
        threading.Thread(target=work, daemon=True).start()

    on_switch = None
    if want_agent and have_claude:
        from manuscriptor.source import tree as tree_mod
        with agent_lock:                       # the first bind is synchronous
            _bind(tree_mod.current_root(d, args.main))
        on_switch = _rebind
    elif want_agent:
        print("  agent  claude CLI not found on PATH; serving without the agent.")
        print("         comments will queue until a session runs `manuscriptor proc`.")

    # The agent must not outlive the server. KeyboardInterrupt is handled inside
    # serve(); a SIGTERM would otherwise kill this process without running the
    # cleanup below and leave a session editing the manuscript.
    previous = {}
    if agent_box["proc"] is not None:
        def _stop(signum, _frame):
            raise SystemExit(128 + signum)
        for sig in (signal.SIGTERM, signal.SIGHUP):
            try:
                previous[sig] = signal.signal(sig, _stop)
            except (ValueError, OSError):
                pass

    try:
        serve(
            d,
            port=args.port,
            open_window=not args.no_window,
            main=args.main,
            bib=args.bib,
            read_only=args.read_only,
            on_switch=on_switch,
        )
    finally:
        for sig, handler in previous.items():
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):
                pass
        with agent_lock:
            agent_box["down"] = True
            had = agent_box["proc"] is not None
            _release()
            if had:
                print("  agent stopped")
    return 0


def cmd_compile(args: argparse.Namespace) -> int:
    """Compile the manuscript to PDF or to Word.

    A subprocess, not a model call: `pdflatex` three times around a `bibtex`,
    or the `pandoc-docx` skill's scripts in the order it documents. Each step
    prints as it finishes, because the whole thing takes tens of seconds.
    """
    from manuscriptor.server import compile as compile_mod

    d = Path(args.manuscript).resolve()
    kind = "docx" if args.docx else "pdf"
    print(f"compiling {d} to {kind}")

    def say(step):
        mark = " " if step.ok else "!"
        print(f"  {mark} {step.name:<24s} {step.seconds:5.1f}s"
              + (f"   {step.detail}" if step.detail else ""))

    fn = compile_mod.compile_docx if args.docx else compile_mod.compile_pdf
    res = fn(d, main=args.main, bib=args.bib, on_step=say)
    for note in res.notes:
        print(f"  note: {note}")
    if not res.ok:
        print(f"failed after {res.seconds:.1f}s\n{res.error}")
        if res.log:
            print(f"log: {res.log}")
        return 1
    print(f"done in {res.seconds:.1f}s -> {res.output}")
    return 0


def cmd_proc(args: argparse.Namespace) -> int:
    """Present pending chats to whoever is going to act on them.

    Nothing here calls a model. The server has no knowledge of Claude and Claude
    never talks to the server; they share a filesystem. This prints the work.
    """
    from manuscriptor.server import drain

    d = Path(args.manuscript).resolve()

    if args.wait:
        # Runs as a backgrounded Claude Code job: this process exiting is the
        # wake signal, so the drain fires on a comment hitting disk rather than
        # on anyone asking for it.
        woke = drain.wait(d, timeout=args.timeout)
        if not woke:
            print("no new comments")
            return 1
        print("new comment on disk")
        return 0

    items = drain.collect(d, main=args.main, bib=args.bib)
    print(drain.as_json(items) if args.json else drain.as_text(items))
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    """Read a marked-up PDF or .docx into the comment log.

    Segments the manuscript rather than building it: the importer needs block ids
    and their text, and pandoc has nothing to say about either. Provenance is
    applied anyway so the block table matches what a running server holds, since
    it changes `kind` without touching an id.
    """
    from manuscriptor.server import importer, producers
    from manuscriptor.source.blocks import segment
    from manuscriptor.source.flatten import flatten

    d = Path(args.manuscript).resolve()
    path = Path(args.file).resolve()
    if not path.exists():
        sys.exit(f"no such file: {path}")

    from manuscriptor.server.build import find_main_tex

    main_tex = find_main_tex(d, args.main)
    blocks = producers.apply(
        segment(flatten(main_tex)), producers.scan(d), root_file=main_tex
    )

    try:
        report = importer.ingest(path.read_bytes(), path.name,
                                 blocks=blocks, log=paths.comments(d))
    except importer.Unreadable as exc:
        sys.exit(str(exc))

    print(f"{report['marks']} marks in {report['file']} · {report['anchored']} anchored · "
          f"{report['unplaced']} in the tray"
          + (f" · {report['already']} already read in" if report["already"] else ""))
    for it in report["items"]:
        if it["already"]:
            continue
        head = f"  {it['id']}  {it['kind']:9s} {it['author']} ({it['where']})"
        if it["block"]:
            print(f"{head} -> {it['block']}")
        else:
            print(f"{head} -> tray: {it['reason']}")
        if it["marked"]:
            print(f"        \"{_clip(it['marked'])}\"")
    waiting = importer.tray(paths.comments(d))
    if waiting:
        print(f"\n{len(waiting)} waiting in the tray. Place them in the page, "
              f"or in a session with `manuscriptor state`.")
    return 0


def _clip(text: str, n: int = 90) -> str:
    flat = " ".join(str(text or "").split())
    return flat[:n] + "…" if len(flat) > n else flat


def cmd_state(args: argparse.Namespace) -> int:
    """Record what happened to a chat. A new record, never a rewrite."""
    from manuscriptor.server import drain

    rec = drain.mark(Path(args.manuscript).resolve(), args.chat_id, args.state)
    print(f"{rec['id']} -> {rec['state']}")
    return 0


def cmd_drafts(args: argparse.Namespace) -> int:
    """Show, print, or land unsaved text the server is holding.

    This exists because of an afternoon spent recovering an author's paragraph
    out of WebKit's sqlite. Unsaved text now lives in a file, and a file the
    author cannot reach from the tool is barely better than a debugger, so:
    `drafts` lists them, `--print BLOCK` writes one to stdout, and
    `--apply BLOCK` splices it into the manuscript.
    """
    from manuscriptor.server import build as bmod
    from manuscriptor.server import drafts as dmod
    from manuscriptor.source import splice as smod

    d = Path(args.manuscript).resolve()
    main_tex = bmod.find_main_tex(d, args.main)
    root = main_tex.parent
    store = paths.drafts(root)
    held = dmod.read(store)
    if not held:
        print(f"no drafts held for {root}")
        return 0

    if args.print_block or args.apply:
        want = args.print_block or args.apply
        hit = {b: t for (doc, b), t in held.items() if b == want or b == f"b-{want}"}
        if not hit:
            print(f"no draft for block {want}", file=sys.stderr)
            return 1
        block_id, text = next(iter(hit.items()))
        if args.print_block:
            sys.stdout.write(text)
            return 0
        # The same rule the page enforces: nothing is written while it would not
        # parse. A draft is unsaved precisely because the author stopped
        # mid-command often enough that applying one blind would be a way to
        # break a manuscript from the terminal.
        why = dmod.imbalance(text)
        if why:
            print(
                f"the draft for {want} does not balance: {why}. Nothing was "
                f"written. Read it with --print {want} and finish it in the "
                "editor, where the same check runs on every pause.",
                file=sys.stderr)
            return 1
        build = bmod.build(root, main=main_tex.name)
        block = build.by_id.get(block_id)
        if block is None:
            print(
                f"block {block_id} is not in the current build, so the draft cannot "
                f"be spliced. Its text is intact: use --print {block_id} to read it.",
                file=sys.stderr)
            return 1
        smod.splice(block, text, root=root)
        for (doc, b) in list(held):
            if b == block_id:
                dmod.drop(store, doc=doc, block=b)
        print(f"applied the draft for {block_id} to {block.file}")
        return 0

    # Which of these blocks still exist? A draft under an id the current build
    # does not have is not offered by the page at all (it iterates the build's
    # blocks), so this listing is the only place it is visible. Saying so is the
    # difference between a recovery path and a pile.
    try:
        live = set(bmod.build(root, main=main_tex.name).by_id)
    except Exception:
        live = set()

    for (doc, block), text in sorted(held.items()):
        first = next((ln for ln in text.splitlines() if ln.strip()), "")
        tag = "" if (not live or block in live) else "  [not in the current build]"
        print(f"{doc}  {block}  {len(text):>6} chars  {first[:60]}{tag}")
    return 0


def cmd_comment(args: argparse.Namespace) -> int:
    """Append a comment from outside the page: a check's finding, usually."""
    from manuscriptor.server import drain

    rec = drain.comment(
        Path(args.manuscript).resolve(),
        body=" ".join(args.body), quote=args.quote or "",
        author=args.author, doc=args.doc or "", check=args.check or "",
        review=args.review,
    )
    if rec is None:
        print("duplicate of an open comment with the same quote; skipped")
        return 0
    print(f"{rec['id']} <- {args.author}" + (" [review]" if args.review else ""))
    return 0


def cmd_reply(args: argparse.Namespace) -> int:
    """Answer a comment in words, into the same chat."""
    from manuscriptor.server import drain

    rec = drain.reply(Path(args.manuscript).resolve(), args.chat_id,
                      " ".join(args.body))
    print(f"{rec['id']} <- reply ({len(rec['body'])} chars)")
    return 0


def cmd_evidence(args: argparse.Namespace) -> int:
    """The absorbed cite-evidence pipeline. Read-only against Zotero."""
    from .evidence import parse, resolve, fetch, extract, render

    manuscript_dir = Path(args.manuscript).resolve()
    if not manuscript_dir.is_dir():
        sys.exit(f"manuscript path is not a directory: {manuscript_dir}")
    output_dir = Path(args.output).resolve() if args.output else paths.cache(manuscript_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    main_tex, bib_file = _resolve_manuscript_paths(manuscript_dir, args.main, args.bib)
    print(f"manuscript : {main_tex}")
    print(f"bib file   : {bib_file}")
    print(f"output dir : {output_dir}")

    print("[01/05] parse...")
    parse.run(main_tex=main_tex, bib_file=bib_file, output_dir=output_dir)
    print("[02/05] resolve...")
    try:
        resolve.run(bib_file=bib_file, output_dir=output_dir)
    except resolve.ZoteroMatchFailure as exc:
        # Everything downstream is built on these matches and every stage of it
        # completes successfully on nothing at all, so this stops here rather
        # than producing an evidence report with no evidence behind it.
        sys.exit(f"\nERROR: {exc}")
    print("[03/05] fetch (read-only)...")
    fetch.run(output_dir=output_dir)
    if args.skip_extract:
        print("[04/05] extract SKIPPED (--skip-extract)")
    else:
        print("[04/05] extract...")
        extract.run(
            output_dir=output_dir,
            model=args.model,
            dry_run=args.dry_run,
            backend=args.backend,
        )
    print("[05/05] render...")
    render.run(output_dir=output_dir, main_tex=main_tex)
    print(f"done → {output_dir / 'index.html'}")
    return 0


def cmd_repair(args: argparse.Namespace) -> int:
    from .evidence import repair

    build_dir = Path(args.build).resolve()
    if not build_dir.is_dir():
        sys.exit(f"build dir not found: {build_dir}")
    return repair.run(build_dir=build_dir)


def cmd_tidy(args: argparse.Namespace) -> int:
    """Say what is lying around beside the manuscript. Remove it only if asked.

    Reports by default, because this runs against real manuscripts and the
    standing rule here is that automation which writes to one eventually writes
    the wrong thing. `--sweep` removes only what git already ignores or has
    never seen, and never a `.tex`.
    """
    from manuscriptor.server import tidy as tidy_mod

    d = Path(args.manuscript).resolve()
    if not d.is_dir():
        sys.exit(f"not a directory: {d}")
    findings = tidy_mod.scan(d)
    print(tidy_mod.report(d, findings))
    if not args.sweep or not findings:
        return 0

    gone, kept = tidy_mod.sweep(findings)
    print()
    print(f"removed {len(gone)} file{'s' if len(gone) != 1 else ''}")
    for f in kept:
        print(f"  kept {f.path.name}: {f.reason}")
    return 0


def cmd_preflight(args: argparse.Namespace) -> int:
    """Say what a clean compile would not have told you. Modifies nothing.

    Exits 2 when a check could not run, and that outranks findings: a skipped
    check is not a pass, and it is the failure that hides the others.
    """
    from manuscriptor.server import preflight

    d = Path(args.manuscript).resolve()
    if not d.is_dir():
        sys.exit(f"not a directory: {d}")
    planned = preflight.plan(d, args.main)
    results = preflight.run(d, args.main)
    print(preflight.report(d, planned, results))
    return preflight.exit_code(planned, results)


def cmd_clean(args: argparse.Namespace) -> int:
    """Remove the regenerable tier, and refuse to remove anything else.

    This command used to take a path and delete it. The path it was documented
    with held `drafts.json`, so running it on a manuscript with unsaved text
    destroyed text no rebuild can reconstruct. It now resolves a manuscript to
    its own `cache/` and will not act on a directory that is not one.
    """
    given = Path(args.manuscript).resolve()
    if paths.is_cache(given):
        target = given
    elif paths.HOME in given.parts:
        # Inside the hidden directory but not the cache: they have named the
        # drafts store, the agent logs, or the whole thing. Deriving a cache
        # path from here would silently invent `.manuscriptor/.manuscriptor/
        # cache` and report success against a directory that never existed.
        sys.exit(f"refusing to remove {given}: name the manuscript directory, "
                 f"or {paths.CACHE_NAME} itself")
    else:
        target = paths.cache(given)
    if not paths.is_cache(target):
        sys.exit(f"refusing to remove {target}: not a manuscriptor cache directory")
    if target.exists():
        shutil.rmtree(target)
        print(f"removed {target}")
    else:
        print(f"nothing to remove at {target}")
    kept = [p for p in (paths.drafts(target.parent.parent),
                        paths.comments(target.parent.parent),
                        paths.agent_dir(target.parent.parent)) if p.exists()]
    if kept:
        print("kept: " + ", ".join(p.name for p in kept))
    if args.cache:
        from .evidence.cache import CACHE_ROOT

        if CACHE_ROOT.exists():
            shutil.rmtree(CACHE_ROOT)
            print(f"removed cache {CACHE_ROOT}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="manuscriptor",
        description="A live manuscript editor: LaTeX rendered to an addressable, commentable page.",
    )
    parser.add_argument("--version", action="version", version=f"manuscriptor {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_serve = sub.add_parser("serve", help="Serve the manuscript live with margin comments and hot reload.")
    p_serve.add_argument("manuscript", help="Path to the manuscript directory")
    p_serve.add_argument("--port", type=int, default=None,
                         help="Port (default: this manuscript's own stable port, "
                              "so drafts and preferences survive a relaunch; "
                              "--port 0 asks for a temporary one)")
    p_serve.add_argument("--no-window", action="store_true", help="Do not open a window; just serve")
    p_serve.add_argument("--main", help="Main .tex filename")
    p_serve.add_argument("--bib", help="Bibliography filename")
    p_serve.add_argument("--read-only", action="store_true",
                         help="Render and browse without the manuscript ever being written to.")
    p_serve.add_argument("--with-agent", action="store_true",
                         help="Require the agent (the default already runs it when the claude "
                              "CLI is present; this makes its absence an error). Refuses to "
                              "combine with --read-only.")
    p_serve.add_argument("--no-agent", action="store_true",
                         help="Serve without the comment-draining agent; comments queue until "
                              "a session runs `manuscriptor proc`.")
    p_serve.set_defaults(func=cmd_serve)

    p_blocks = sub.add_parser("blocks", help="Print the block table for a manuscript (flatten and segment only).")
    p_blocks.add_argument("main_tex", help="Path to the main .tex file")
    p_blocks.set_defaults(func=cmd_blocks)

    p_build = sub.add_parser("build", help="Render a static, anchored HTML copy of the manuscript.")
    p_build.add_argument("manuscript", help="Path to the manuscript directory")
    p_build.add_argument("--output", "-o", help="Output directory")
    p_build.add_argument("--main", help="Main .tex filename")
    p_build.add_argument("--bib", help="Bibliography filename")
    p_build.set_defaults(func=cmd_build)

    p_comp = sub.add_parser("compile", help="Compile the manuscript to PDF (default) or to Word.")
    p_comp.add_argument("manuscript", help="Path to the manuscript directory")
    fmt = p_comp.add_mutually_exclusive_group()
    fmt.add_argument("--pdf", action="store_true", help="LaTeX to PDF: three passes around a bibtex (default)")
    fmt.add_argument("--docx", action="store_true", help="LaTeX to Word, through the pandoc-docx skill")
    p_comp.add_argument("--main", help="Main .tex filename")
    p_comp.add_argument("--bib", help="Bibliography filename")
    p_comp.set_defaults(func=cmd_compile)

    p_proc = sub.add_parser("proc", help="Show pending comments with the context needed to answer them.")
    p_proc.add_argument("manuscript", help="Path to the manuscript directory")
    p_proc.add_argument("--wait", action="store_true",
                        help="Block until a new comment lands, then exit. Run backgrounded so the exit wakes the session.")
    p_proc.add_argument("--timeout", type=float, default=None, help="Give up waiting after N seconds")
    p_proc.add_argument("--json", action="store_true", help="Machine-readable output")
    p_proc.add_argument("--main", help="Main .tex filename")
    p_proc.add_argument("--bib", help="Bibliography filename")
    p_proc.set_defaults(func=cmd_proc)

    p_import = sub.add_parser(
        "import",
        help="Read a marked-up PDF or .docx of tracked changes into the comment log. "
             "Anchored by the text that was marked, never by the page number.")
    p_import.add_argument("manuscript", help="Path to the manuscript directory")
    p_import.add_argument("file", help="The marked-up .pdf or .docx")
    p_import.add_argument("--main", help="Main .tex filename")
    p_import.add_argument("--bib", help="Bibliography filename")
    p_import.set_defaults(func=cmd_import)

    p_state = sub.add_parser("state", help="Mark a chat queued, working, done or orphaned.")
    p_state.add_argument("manuscript", help="Path to the manuscript directory")
    p_state.add_argument("chat_id", help="The chat id, e.g. c-0007")
    p_state.add_argument("state", choices=["queued", "working", "done", "orphaned"])
    p_state.set_defaults(func=cmd_state)

    p_drafts = sub.add_parser(
        "drafts", help="Unsaved text the server is holding for this manuscript")
    p_drafts.add_argument("manuscript", help="Path to the manuscript directory")
    p_drafts.add_argument("--main", help="Main .tex filename")
    p_drafts.add_argument("--print", dest="print_block", metavar="BLOCK",
                          help="Write one draft to stdout")
    p_drafts.add_argument("--apply", metavar="BLOCK",
                          help="Splice one draft into the manuscript and forget it")
    p_drafts.set_defaults(func=cmd_drafts)

    p_drain = sub.add_parser(
        "drain", help="Run the standing drain: one supervised session, watched")
    p_drain.add_argument("manuscript", help="Path to the manuscript directory")
    p_drain.add_argument("--model", default="", help="Model for the session")
    p_drain.add_argument("--stall-after", default=150,
                         help="Seconds of silence mid-turn before it is restarted")
    p_drain.set_defaults(func=cmd_drain)

    p_comment = sub.add_parser("comment", help="Append a comment (a check's finding, usually)")
    p_comment.add_argument("manuscript", help="Path to the manuscript directory")
    p_comment.add_argument("body", nargs="+", help="The comment text")
    p_comment.add_argument("--quote", help="The exact sentence it concerns; this is what anchors it")
    p_comment.add_argument("--author", default="bb", help="Who is saying it (e.g. proofreader)")
    p_comment.add_argument("--doc", help="The document it belongs to (e.g. main.tex)")
    p_comment.add_argument("--check", help="The check it came from (e.g. consistency-check)")
    p_comment.add_argument("--review", action="store_true",
                           help="File as a finding: pinned for the author, never drained as work")
    p_comment.set_defaults(func=cmd_comment)

    p_reply = sub.add_parser("reply", help="Answer a comment in words, into the same chat")
    p_reply.add_argument("manuscript", help="Path to the manuscript directory")
    p_reply.add_argument("chat_id", help="The chat id, e.g. c-0007")
    p_reply.add_argument("body", nargs="+", help="The reply text")
    p_reply.set_defaults(func=cmd_reply)

    p_ev = sub.add_parser(
        "evidence",
        help="Citation-evidence viewer (the absorbed cite-evidence pipeline; read-only against Zotero).",
    )
    p_ev.add_argument("manuscript", help="Path to manuscript directory containing the .tex and .bib")
    # Deliberately not a spelled-out path: this help text said
    # `<manuscript>/build/manuscriptor` for months after the layout moved, and
    # naming the layout in a string is how it goes stale again.
    p_ev.add_argument("--output", "-o",
                      help="Output directory (default: Manuscriptor's cache for this manuscript)")
    p_ev.add_argument("--main", help="Main .tex filename (default: auto-detect main.tex or first *.tex)")
    p_ev.add_argument("--bib", help="Bibliography filename (default: first *.bib)")
    p_ev.add_argument("--model", default="sonnet", help="Model alias for evidence extraction (default: sonnet)")
    p_ev.add_argument(
        "--backend",
        choices=["auto", "claude-p", "anthropic"],
        default="auto",
        help="LLM backend: 'claude-p' uses the claude CLI under your Claude Code plan; 'anthropic' uses the SDK; 'auto' prefers claude-p (default)",
    )
    p_ev.add_argument("--dry-run", action="store_true", help="Estimate cost; do not call the LLM")
    p_ev.add_argument("--skip-extract", action="store_true", help="Skip extraction; render with empty quotes")
    p_ev.set_defaults(func=cmd_evidence)

    p_repair = sub.add_parser("repair", help="Fetch missing PDFs into Zotero (explicit opt-in; writes to Zotero).")
    p_repair.add_argument("build", help="Path to build directory containing missing.json")
    p_repair.set_defaults(func=cmd_repair)

    p_tidy = sub.add_parser("tidy", help="Report stray artifacts beside a manuscript.")
    p_tidy.add_argument("manuscript", help="Manuscript directory to look at")
    p_tidy.add_argument("--sweep", action="store_true",
                        help="Also remove the ones git ignores or has never seen. "
                             "Never removes a tracked file, and never a .tex.")
    p_tidy.set_defaults(func=cmd_tidy)

    p_pre = sub.add_parser("preflight",
                           help="Report what a clean compile would not have told you.")
    p_pre.add_argument("manuscript", help="Manuscript directory to check")
    p_pre.add_argument("--main", default=None,
                       help="Check one document only (default: every document here)")
    p_pre.set_defaults(func=cmd_preflight)

    p_clean = sub.add_parser("clean", help="Remove regenerable render output; optionally clear the shared cache.")
    p_clean.add_argument("manuscript",
                         help="Manuscript directory (its .manuscriptor/cache is removed). "
                              "Drafts, comments and agent logs are never touched.")
    p_clean.add_argument("--cache", action="store_true", help="Also clear the evidence cache")
    p_clean.set_defaults(func=cmd_clean)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
