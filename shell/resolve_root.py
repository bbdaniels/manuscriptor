"""The shell's side of the manuscript-root rule.

The rule itself lives in `manuscriptor/source/root.py` now, because the server
needs it too (the default main and the doc switcher both read it); this file
re-exports it and adds the two pieces only the shell wants, `parse_port` and
`resolve`. `ManuscriptRoot.swift` mirrors the whole surface, and
`tests/test_shell.py` runs the built binary's `--resolve-root` against this
module over the same case table; if either side drifts the cross-check fails.

WHY THE RULE EXISTS. Finder hands the app whatever file was double-clicked.
Serving `appendix/e_data_details.tex` would render a fragment with no
preamble, no bibliography and no cross-references, and the feature would look
broken on the first thing anyone tries it on. So an opened file resolves to
the manuscript it belongs to, and the page then jumps to the file.

The Swift binary never runs this file; it exists for the parity harness and as
the readable statement of the rule beside the mirror.
"""

from __future__ import annotations

import re
from pathlib import Path

from manuscriptor.source.root import (  # noqa: F401  (re-exported surface)
    HEAD_BYTES,
    MAIN_NAME,
    find_root,
    has_documentclass,
    is_boundary,
    root_here,
    strip_comment,
)

# Kept under their pre-unification names so the Swift mirror and the parity
# case table did not have to move in the same commit as the rule.
_root_here = root_here
_is_boundary = is_boundary

_PORT = re.compile(r"http://127\.0\.0\.1:(\d+)/")


def parse_port(line: str) -> int | None:
    """Read the port off the server's banner: `manuscriptor  http://127.0.0.1:PORT/`.

    Anchored on the loopback address rather than on any URL, so a line that
    merely mentions a host cannot send the window somewhere else.
    """
    m = _PORT.search(line or "")
    return int(m.group(1)) if m else None


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
    # A file that itself declares a documentclass IS the document, even beside
    # a main.tex: the author opened a document, not a fragment of some other
    # one. estonia-ecm keeps `Highlights for JPubE.tex` next to the paper.
    if not p.is_dir() and p.suffix == ".tex" and has_documentclass(p):
        return start, p.name, p.name
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
