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
    sys.exit(_NOT_YET.format(milestone="M1, flatten and segment"))


def cmd_build(args: argparse.Namespace) -> int:
    sys.exit(_NOT_YET.format(milestone="M2, render"))


def cmd_serve(args: argparse.Namespace) -> int:
    sys.exit(_NOT_YET.format(milestone="M3, serve and watch"))


def cmd_proc(args: argparse.Namespace) -> int:
    sys.exit(_NOT_YET.format(milestone="M5, drain"))


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
    p_serve.set_defaults(func=cmd_serve)

    p_blocks = sub.add_parser("blocks", help="Print the block table for a manuscript (flatten and segment only).")
    p_blocks.add_argument("main_tex", help="Path to the main .tex file")
    p_blocks.set_defaults(func=cmd_blocks)

    p_build = sub.add_parser("build", help="Render a static, anchored HTML copy of the manuscript.")
    p_build.add_argument("manuscript", help="Path to the manuscript directory")
    p_build.add_argument("--output", "-o", help="Output directory")
    p_build.set_defaults(func=cmd_build)

    p_proc = sub.add_parser("proc", help="Drain pending comments once. Manual fallback for the live watcher.")
    p_proc.add_argument("manuscript", help="Path to the manuscript directory")
    p_proc.set_defaults(func=cmd_proc)

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
