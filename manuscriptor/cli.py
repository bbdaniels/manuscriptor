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
import time
from pathlib import Path

from . import __version__

_NOT_YET = "not implemented yet (lands with {milestone}); see the Phase 1 design in the vault"


def _resolve_manuscript_paths(manuscript_dir: Path, main: str | None, bib: str | None) -> tuple[Path, Path]:
    if main:
        main_path = manuscript_dir / main
    else:
        candidates = sorted(manuscript_dir.glob("*.tex"))
        candidates = [c for c in candidates if c.name == "main.tex"] or candidates
        if not candidates:
            sys.exit(f"No .tex file found in {manuscript_dir}; pass --main")
        main_path = candidates[0]
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
    out = out or Path(args.manuscript).resolve() / "build" / "manuscriptor"

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

AGENT_PROMPT = (
    "Drain the pending Manuscriptor comments in this directory. Use the "
    "manuscriptor-drain skill: work the queue oldest first, mark each comment "
    "working before you start and done when the edit has landed, and change "
    "exactly one block per comment. Read as widely as you need. Do nothing the "
    "comments did not ask for."
)


def agent_log_path(manuscript_dir: Path) -> Path:
    """Where the session's output goes.

    Inside the build directory, which writes its own `.gitignore`, because
    serving a paper must never be the reason `git status` grows.
    """
    from manuscriptor.server.build import keep_out_of_git

    out = Path(manuscript_dir).resolve() / "build" / "manuscriptor"
    out.mkdir(parents=True, exist_ok=True)
    keep_out_of_git(out)
    return out / "agent.log"


def agent_loop_script(manuscript_dir: Path, *, claude: str, manuscriptor: str) -> str:
    """The drain loop, as a script the author can read.

    `proc --wait` blocks until the comment log grows and then exits, so the loop
    costs nothing while nothing is happening: one blocked process, no polling.
    Anything already pending is worked before the first block, or a comment left
    before the server started would sit there until the next one arrived.

    A failed session backs off instead of spinning, because the common cause is
    a credential problem that will not fix itself in the next hundred
    milliseconds.
    """
    d = str(Path(manuscript_dir).resolve())
    return f"""#!/bin/sh
# Written by `manuscriptor serve --with-agent`. Kill the server and this goes
# with it: it runs in its own process group and the server kills the group.
DIR={_sh(d)}
MS={_sh(manuscriptor)}
CLAUDE={_sh(claude)}
cd "$DIR" || exit 1

drain() {{
  # A failure is not an empty queue. When the manuscript does not render, `proc`
  # exits non-zero, and reading that as "nothing pending" would be worse: it
  # would start a session on every wake against a paper that cannot be read.
  if ! PENDING=$("$MS" proc "$DIR" --json 2>&1); then
    echo "--- $(date '+%Y-%m-%d %H:%M:%S')  the manuscript does not build; not drained"
    printf '%s\\n' "$PENDING" | tail -3
    sleep 5
    return 0
  fi
  if [ "$(printf '%s' "$PENDING" | tr -d '[:space:]')" = "[]" ]; then
    return 0
  fi
  echo "--- $(date '+%Y-%m-%d %H:%M:%S')  draining"
  "$CLAUDE" -p {_sh(AGENT_PROMPT)} --permission-mode acceptEdits || sleep 5
}}

drain
while :; do
  "$MS" proc "$DIR" --wait || exit 0
  drain
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


def spawn_group(argv: list[str], *, cwd: Path, log_path: Path) -> subprocess.Popen:
    """Start a child in its OWN process group, with its output on disk.

    The group is the point. A drain loop starts a `claude`, which starts its own
    children; signalling the shell alone would leave those running, and a
    session still editing a manuscript after the server is gone is the worst
    failure this design has.
    """
    log = open(log_path, "a", buffering=1, encoding="utf-8")
    try:
        return subprocess.Popen(
            argv, cwd=str(cwd), stdout=log, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, start_new_session=True,
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


def start_agent(manuscript_dir: Path) -> tuple[subprocess.Popen, Path]:
    """Launch the drain loop beside the server. Says what it started."""
    claude = shutil.which("claude")
    if not claude:
        sys.exit(
            "--with-agent needs the `claude` CLI on PATH and it is not there. "
            "Install Claude Code, or drop --with-agent and run "
            "`manuscriptor proc <dir>` in a session yourself."
        )
    log = agent_log_path(manuscript_dir)
    ms = manuscriptor_command(log.parent)
    script = log.parent / "agent-loop.sh"
    script.write_text(agent_loop_script(manuscript_dir, claude=claude, manuscriptor=ms),
                      encoding="utf-8")
    proc = spawn_group(["/bin/sh", str(script)], cwd=Path(manuscript_dir).resolve(),
                       log_path=log)
    print(f"  agent  {claude} -p … --permission-mode acceptEdits   (pid {proc.pid})")
    print("         wakes on each comment; one block per comment")
    print(f"         log {log}")
    return proc, log


def cmd_serve(args: argparse.Namespace) -> int:
    from manuscriptor.server.app import serve

    d = Path(args.manuscript).resolve()
    agent = None
    if args.with_agent:
        if args.read_only:
            sys.exit(
                "--with-agent and --read-only contradict each other: an agent that "
                "cannot write cannot answer a comment, and the comment log is not "
                "written either. Pick one."
            )
        agent, _ = start_agent(d)

    # The agent must not outlive the server. KeyboardInterrupt is handled inside
    # serve(); a SIGTERM would otherwise kill this process without running the
    # cleanup below and leave a session editing the manuscript.
    previous = {}
    if agent is not None:
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
        )
    finally:
        for sig, handler in previous.items():
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):
                pass
        if agent is not None:
            terminate_group(agent)
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
                                 blocks=blocks, log=d / "comments.jsonl")
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
    waiting = importer.tray(d / "comments.jsonl")
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


def cmd_evidence(args: argparse.Namespace) -> int:
    """The absorbed cite-evidence pipeline. Read-only against Zotero."""
    from .evidence import parse, resolve, fetch, extract, render

    manuscript_dir = Path(args.manuscript).resolve()
    if not manuscript_dir.is_dir():
        sys.exit(f"manuscript path is not a directory: {manuscript_dir}")
    output_dir = Path(args.output).resolve() if args.output else manuscript_dir / "build" / "manuscriptor"
    output_dir.mkdir(parents=True, exist_ok=True)

    main_tex, bib_file = _resolve_manuscript_paths(manuscript_dir, args.main, args.bib)
    print(f"manuscript : {main_tex}")
    print(f"bib file   : {bib_file}")
    print(f"output dir : {output_dir}")

    print("[01/05] parse...")
    parse.run(main_tex=main_tex, bib_file=bib_file, output_dir=output_dir)
    print("[02/05] resolve...")
    resolve.run(bib_file=bib_file, output_dir=output_dir)
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


def cmd_clean(args: argparse.Namespace) -> int:
    target = Path(args.build).resolve()
    if target.exists():
        shutil.rmtree(target)
        print(f"removed {target}")
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
    p_serve.add_argument("--port", type=int, default=0, help="Port (default: pick a free one)")
    p_serve.add_argument("--no-window", action="store_true", help="Do not open a window; just serve")
    p_serve.add_argument("--main", help="Main .tex filename")
    p_serve.add_argument("--bib", help="Bibliography filename")
    p_serve.add_argument("--read-only", action="store_true",
                         help="Render and browse without the manuscript ever being written to.")
    p_serve.add_argument("--with-agent", action="store_true",
                         help="Also run a Claude Code session that drains comments as they arrive. "
                              "Refuses to combine with --read-only.")
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

    p_ev = sub.add_parser(
        "evidence",
        help="Citation-evidence viewer (the absorbed cite-evidence pipeline; read-only against Zotero).",
    )
    p_ev.add_argument("manuscript", help="Path to manuscript directory containing the .tex and .bib")
    p_ev.add_argument("--output", "-o", help="Output directory (default: <manuscript>/build/manuscriptor)")
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

    p_clean = sub.add_parser("clean", help="Remove build artifacts; optionally clear the shared cache.")
    p_clean.add_argument("build", help="Path to build directory to remove")
    p_clean.add_argument("--cache", action="store_true", help="Also clear the evidence cache")
    p_clean.set_defaults(func=cmd_clean)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
