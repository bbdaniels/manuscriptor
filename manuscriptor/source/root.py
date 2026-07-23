"""The manuscript-root rule, written once.

Three parties need to answer "which .tex is the document": the server picking a
default main, the doc switcher listing what else in the directory could be
served, and the shell resolving whatever file Finder handed it. They must
agree, or the app opens one paper and the CLI another. This module is the one
implementation: `shell/resolve_root.py` re-exports it for the Swift parity
harness, and `ManuscriptRoot.swift` mirrors it.

The alphabetical fallback this replaces was a defect: a directory holding
`abstract.tex`, `appendix.tex`, and `paper.tex` served `abstract.tex`,
silently. A root is a file that DECLARES itself one, with an uncommented
`\\documentclass`; commented ones do not count, because manuscripts routinely
carry a dead journal-template header.
"""
from __future__ import annotations

from pathlib import Path

MAIN_NAME = "main.tex"

# \documentclass lives in the first line or two of a root file. Reading the
# head keeps a directory scan from paging in a 300KB .bbl-sized appendix per
# candidate.
HEAD_BYTES = 65536


class AmbiguousRoot(LookupError):
    """More than one .tex declares a document class and none is `main.tex`.

    Carries the candidate names so the caller can say what the choices were
    rather than that a choice existed.
    """

    def __init__(self, directory: Path, names: list[str]):
        self.directory = Path(directory)
        self.names = list(names)
        super().__init__(
            f"{self.directory} holds {len(names)} documents "
            f"({', '.join(names)}); pass --main to pick one"
        )


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


def tex_files(d: Path) -> list[Path]:
    try:
        return sorted(p for p in d.iterdir()
                      if p.is_file() and p.suffix == ".tex" and not p.name.startswith("."))
    except OSError:
        return []


def candidates(d: Path) -> list[str]:
    """The documents this directory can serve: every .tex that declares a
    document class, `main.tex` first because that is the convention the corpus
    follows, the rest in name order. This list is the doc switcher."""
    names = [p.name for p in tex_files(d) if has_documentclass(p)]
    if MAIN_NAME in names:
        names.remove(MAIN_NAME)
        names.insert(0, MAIN_NAME)
    return names


def choose_main(d: Path) -> str:
    """The name to serve when the caller expressed no preference.

    `main.tex` when present; otherwise the sole declared document; otherwise a
    sole .tex of any kind, because a fragment rendered alone is more useful
    than an error. Several declared documents is a genuine question, and
    guessing would silently serve the wrong paper, so it raises with the
    choices named.
    """
    d = Path(d)
    if (d / MAIN_NAME).is_file():
        return MAIN_NAME
    names = candidates(d)
    if len(names) == 1:
        return names[0]
    if len(names) > 1:
        raise AmbiguousRoot(d, names)
    all_tex = tex_files(d)
    if len(all_tex) == 1:
        return all_tex[0].name
    if all_tex:
        raise AmbiguousRoot(d, [p.name for p in all_tex])
    raise FileNotFoundError(f"no .tex file in {d}; pass --main")


def root_here(d: Path) -> str:
    """The name of this directory's root .tex, or "" if it is not a root.

    `main.tex` first, then EXACTLY ONE declared document: two roots is not a
    root when walking up from a fragment, because picking one would silently
    serve the wrong paper.
    """
    if (d / MAIN_NAME).is_file():
        return MAIN_NAME
    roots = [p for p in tex_files(d) if has_documentclass(p)]
    return roots[0].name if len(roots) == 1 else ""


def is_boundary(d: Path) -> bool:
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
        name = root_here(d)
        if name:
            return d, name
        if is_boundary(d):
            break
        d = d.parent
    # Nothing above it is a manuscript. Serve where the file sits: a fragment
    # rendered alone is more useful than an error, and the diagnostics say so.
    return start, ""
