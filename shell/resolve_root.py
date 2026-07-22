"""The manuscript-root rule, written once in Python so it can be tested.

`ManuscriptRoot.swift` mirrors this file line for line. Two copies of a rule
is a split that rots, so `tests/test_shell.py` runs the built binary's
`--resolve-root` against this implementation over the same case table; if
either side drifts the cross-check fails.

WHY THE RULE EXISTS. Finder hands the app whatever file was double-clicked.
Serving `appendix/e_data_details.tex` would render a fragment with no
preamble, no bibliography and no cross-references, and the feature would look
broken on the first thing anyone tries it on. So an opened file resolves to
the manuscript it belongs to, and the page then jumps to the file.

Nothing here imports manuscriptor. The shell is a client.
"""

from __future__ import annotations

import re
from pathlib import Path

MAIN_NAME = "main.tex"

# \documentclass lives in the first line or two of a root file. Reading the
# head keeps a directory scan from paging in a 300KB .bbl-sized appendix per
# candidate.
HEAD_BYTES = 65536

_PORT = re.compile(r"http://127\.0\.0\.1:(\d+)/")


def parse_port(line: str) -> int | None:
    """Read the port off the server's banner: `manuscriptor  http://127.0.0.1:PORT/`.

    Anchored on the loopback address rather than on any URL, so a line that
    merely mentions a host cannot send the window somewhere else.
    """
    m = _PORT.search(line or "")
    return int(m.group(1)) if m else None


def strip_comment(line: str) -> str:
    """Everything before the first unescaped `%`."""
    out = []
    i = 0
    n = len(line)
    while i < n:
        c = line[i]
        if c == "\\":
            out.append(c)
            if i + 1 < n:
                out.append(line[i + 1])
            i += 2
            continue
        if c == "%":
            break
        out.append(c)
        i += 1
    return "".join(out)


def has_documentclass(path: Path) -> bool:
    """True when the file really declares a document class.

    A commented-out `\\documentclass` does not count: manuscripts routinely
    carry a dead journal-template header, and treating it as a root would pick
    the wrong file.
    """
    try:
        with path.open("rb") as fh:
            head = fh.read(HEAD_BYTES)
    except OSError:
        return False
    text = head.decode("utf-8", errors="replace")
    return any("\\documentclass" in strip_comment(ln) for ln in text.splitlines())


def _tex_files(d: Path) -> list[Path]:
    try:
        return sorted(p for p in d.iterdir()
                      if p.is_file() and p.suffix == ".tex" and not p.name.startswith("."))
    except OSError:
        return []


def _root_here(d: Path) -> str:
    """The name of this directory's root .tex, or "" if it is not a root.

    `main.tex` first, because that is the convention every manuscript in the
    corpus follows and the server's own `find_main_tex` agrees. Otherwise the
    directory qualifies only if EXACTLY ONE .tex declares a document class:
    two roots is not a root, and picking one would silently serve the wrong
    paper.
    """
    if (d / MAIN_NAME).is_file():
        return MAIN_NAME
    roots = [p for p in _tex_files(d) if has_documentclass(p)]
    return roots[0].name if len(roots) == 1 else ""


def _is_boundary(d: Path) -> bool:
    """Stop climbing here (after checking this directory).

    A repository root is the edge of a project: a `main.tex` above it belongs
    to some other paper and must never be served in place of this one. Home
    and the filesystem root are the same idea, less often reached.
    """
    return (d / ".git").exists() or d == Path.home() or d.parent == d


def find_root(start: Path) -> tuple[Path, str]:
    """Walk up from `start` to the manuscript root. Returns (dir, main name)."""
    d = start
    while True:
        name = _root_here(d)
        if name:
            return d, name
        if _is_boundary(d):
            break
        d = d.parent
    # Nothing above it is a manuscript. Serve where the file sits: a fragment
    # rendered alone is more useful than an error, and the diagnostics say so.
    return start, ""


def resolve(path) -> tuple[Path, str, str]:
    """Resolve an opened path to (manuscript dir, main .tex name, file to jump to).

    `main` is "" when no root could be identified, in which case the server
    picks for itself. The third value is relative to the manuscript dir, with
    forward slashes, and is "" when a directory was opened.
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(str(p))
    p = p.resolve()
    start = p if p.is_dir() else p.parent
    root, main = find_root(start)
    if p.is_dir():
        return root, main, ""
    try:
        rel = p.relative_to(root).as_posix()
    except ValueError:
        rel = p.name
    return root, main, rel


if __name__ == "__main__":  # pragma: no cover - parity harness
    import sys

    root, main, rel = resolve(sys.argv[1])
    print(f"{root}\t{main}\t{rel}")
