"""manuscriptor CLI.

`evidence` works today: it is the absorbed cite-evidence pipeline, renamed.
The rest arrive with their milestones and exit with a clear message until then,
rather than failing obscurely.
"""
from __future__ import annotations

import argparse
import shutil
import sys
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

    out = Path(args.output).resolve() if args.output else None
    b = build_mod.build(Path(args.manuscript).resolve(), main=args.main, bib=args.bib, output_dir=out)
    out = out or Path(args.manuscript).resolve() / "build" / "manuscriptor"

    tpl = resources.files("manuscriptor.templates").joinpath("index.html.j2").read_text(encoding="utf-8")
    css = resources.files("manuscriptor.templates.static").joinpath("styles.css").read_text(encoding="utf-8")
    js = resources.files("manuscriptor.templates.static").joinpath("viewer.js").read_text(encoding="utf-8")
    page = Template(tpl).render(ms=b.blob, styles_css=css, viewer_js=js)
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


def cmd_serve(args: argparse.Namespace) -> int:
    from manuscriptor.server.app import serve

    serve(
        Path(args.manuscript).resolve(),
        port=args.port,
        open_window=not args.no_window,
        main=args.main,
        bib=args.bib,
        read_only=args.read_only,
    )
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

    p_proc = sub.add_parser("proc", help="Show pending comments with the context needed to answer them.")
    p_proc.add_argument("manuscript", help="Path to the manuscript directory")
    p_proc.add_argument("--wait", action="store_true",
                        help="Block until a new comment lands, then exit. Run backgrounded so the exit wakes the session.")
    p_proc.add_argument("--timeout", type=float, default=None, help="Give up waiting after N seconds")
    p_proc.add_argument("--json", action="store_true", help="Machine-readable output")
    p_proc.add_argument("--main", help="Main .tex filename")
    p_proc.add_argument("--bib", help="Bibliography filename")
    p_proc.set_defaults(func=cmd_proc)

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
