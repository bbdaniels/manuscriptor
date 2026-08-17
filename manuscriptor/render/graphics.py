r"""Which file an `\includegraphics` names, decided once.

LaTeX does not read an include as a path. `\graphicspath{{../outputs/}{figures/}}`
gives the graphics package a search list, and `\includegraphics{f-ayush-1}`
carries neither a directory nor a suffix, so the package walks the list and
tries the extensions it knows. Manuscriptor implemented neither half, and pandoc
implements neither either: it emits `<img src="f-ayush-1">` verbatim, the copier
looks beside `main.tex` for a file of exactly that name, finds none, and skips
it. Every figure in qutub-ayush was a broken-image icon, at exit 0, with the
caption below it rendering perfectly.

ONE HOME, AND IT IS THIS MODULE. The question "which file is this" is asked in
four places -- the render input, the asset copier, the rasterizer, and the
assets route's miss classification -- and answering it in the copier would have
been the cheap fix. It would also have left the LaTeX saying `f-ayush-1` and
every other consumer still guessing, which is the shape this repo has already
paid for twice (`render/tables.py`, `render/refs.py`). So the resolution runs
ONCE, on the way into pandoc, rewriting the include's argument to a path
relative to the manuscript directory; everything downstream keeps working on
ordinary relative paths and knows nothing about `\graphicspath`.

**The raster wins a tie.** A figure exported as both `f.pdf` and `f.png` is
staged from the `.png`, because the pipeline rasterizes a PDF figure anyway and
the author's own export is both cheaper and closer to what they meant. The
`.pdf` still resolves when it is the only file, which is the common case.

**A candidate that is not a file is passed over, not resolved to.** This is not
defensive coding: qutub-ayush's `figures/` is full of symlinks reading
`../outputs/f-x.pdf`, which from inside `figures/` means `manuscript/outputs/`
and does not exist. `is_file` follows the link and answers False, so the search
continues into `../outputs/` and finds the real file. A resolver that stopped at
`exists()` -- which is False for a broken link too -- would be correct here by
accident and wrong on the first dangling link that pointed somewhere readable.

**An out-of-tree figure is staged under a mapped in-tree name.** `../outputs/`
resolves outside the manuscript directory, and the asset copier refuses any
destination escaping the output root. That guard is real -- an `<img src>` is
reachable by anything that can put a string into the HTML, where an author's
`\graphicspath` is an instruction about their own tree -- so it is not
weakened. Instead the resolved absolute path is mapped to
`_mxext/<path without its leading slash>`, which contains no `..` by
construction and therefore lands inside the output root no matter what it names.
The map is reversible, so the assets route can still tell a figure that was
never staged from one whose source the author deleted; the route may only
REFRESH an external asset a build already staged (see `postprocess.refresh_asset`),
because a name a browser can spell is not authority to read outside the paper.
"""
from __future__ import annotations

import re
from pathlib import Path, PurePosixPath

from manuscriptor.source.flatten import command_re, is_commented
from manuscriptor.render.tables import skip_group

# The mapped root for a figure that lives outside the manuscript directory.
# Underscored so it cannot collide with a directory a manuscript would write,
# and short because it prefixes an absolute path.
EXTERNAL_PREFIX = "_mxext"

# Tried in order after the argument verbatim. Raster before PDF; see the module
# docstring. `.eps` is not here because nothing downstream can display one.
SEARCH_EXTENSIONS = (".png", ".jpg", ".jpeg", ".pdf")

_GRAPHICSPATH_RE = re.compile(r"\\graphicspath\s*(?=\{)")
_ENTRY_RE = re.compile(r"\{([^{}]*)\}")


def graphics_dirs(source: str) -> list[str]:
    r"""The directories `\graphicspath` declares, in the order it declares them.

    A commented-out `\graphicspath` is prose about the document, not the
    document's search list -- the same trap `pandoc._live` exists for.
    """
    for m in _GRAPHICSPATH_RE.finditer(source):
        if is_commented(source, m.start()):
            continue
        end = skip_group(source, m.end())
        if end <= m.end():
            continue
        return [e.strip() for e in _ENTRY_RE.findall(source[m.end() + 1: end - 1])]
    return []


def _search_order(dirs: list[str], root: Path) -> list[Path]:
    r"""The directories the graphics package walks, in the order it walks them.

    `\graphicspath`'s entries first, the document's own directory last -- a
    search list ADDS to TeX's path, it does not replace it. One function because
    two callers ask: `find`, resolving an include, and `search_dirs`, telling a
    watcher which directories this document's renders depend on. Two spellings
    of "and also the manuscript directory" is exactly the divergence that would
    leave a figure resolvable but its arrival unnoticed.
    """
    out: list[Path] = []
    for where in [*dirs, ""]:
        d = root / where
        if d not in out:
            out.append(d)
    return out


def search_dirs(source: str, manuscript_dir: Path | str) -> list[Path]:
    r"""Every directory this document reads figures from, absolute and existing.

    A FIGURE APPEARING IS A CHANGE TO THE RENDER, which is why a watcher needs
    this list and cannot derive it from the manuscript directory alone.
    `\includegraphics{f-did-components}` written before the script that exports
    the figure has run resolves to nothing and is left as the author wrote it;
    when the export lands, the same include resolves to a real file and the
    page's `<img src>` has to change. qutub-ayush's `\graphicspath` names
    `../outputs/`, OUTSIDE the manuscript directory and outside the only tree
    `serve` watches, so that export was not an event and the broken image
    survived every reload (2026-08-17).

    Directories that do not exist are passed over: a `\graphicspath` entry
    naming nothing is nothing to watch, and arming a watch on it would create
    it -- inside the author's tree, from a server that was only asked to read.
    """
    root = Path(manuscript_dir).resolve()
    out: list[Path] = []
    for d in _search_order(graphics_dirs(source), root):
        d = d.resolve()
        if d.is_dir() and d not in out:
            out.append(d)
    return out


def resolve_includes(source: str, manuscript_dir: Path | str) -> str:
    r"""Rewrite every `\includegraphics` argument to a path we can stage.

    In-tree files come out relative to the manuscript directory; out-of-tree
    ones come out under `EXTERNAL_PREFIX`. An argument that resolves to nothing
    is left exactly as the author wrote it, so the missing-asset reporting the
    server already does still names what the manuscript says.
    """
    root = Path(manuscript_dir).resolve()
    dirs = graphics_dirs(source)
    out: list[str] = []
    cursor = 0
    for m in command_re("includegraphics").finditer(source):
        if is_commented(source, m.start()):
            continue
        end = skip_group(source, m.end())
        if end <= m.end():
            continue
        arg = source[m.end() + 1: end - 1].strip()
        found = find(arg, root, dirs)
        if found is None:
            continue
        out.append(source[cursor: m.end() + 1])
        out.append(staged_rel(found, root))
        cursor = end - 1
    out.append(source[cursor:])
    return "".join(out)


def find(arg: str, manuscript_dir: Path, dirs: list[str]) -> Path | None:
    r"""The file `\includegraphics{arg}` names, searching as LaTeX searches.

    The manuscript directory is searched last rather than not at all: a
    `\graphicspath` adds to the places the graphics package looks, and TeX's own
    path always includes the directory the document is in.
    """
    if not arg or "\\" in arg:
        # A macro in the argument -- `\figdir/f` and friends. Expanding it is
        # TeX's job and guessing at it would resolve to the wrong file.
        return None
    root = Path(manuscript_dir).resolve()
    for where in _search_order(dirs, root):
        base = (where / arg) if not Path(arg).is_absolute() else Path(arg)
        for candidate in (base, *(base.with_name(base.name + e) for e in SEARCH_EXTENSIONS)):
            # `is_file` follows a symlink, so a dangling one is passed over
            # rather than resolved to. qutub-ayush's `figures/` is full of them.
            if candidate.is_file():
                return candidate.resolve()
        if Path(arg).is_absolute():
            break
    return None


def staged_rel(path: Path | str, manuscript_dir: Path | str) -> str:
    """The name a resolved figure is staged and served under.

    Relative to the manuscript directory when the file is inside it, and mapped
    under `EXTERNAL_PREFIX` when it is not. Never contains `..`, which is what
    keeps the copier's escape guard effective by construction rather than by a
    check somebody could forget.
    """
    path = Path(path).resolve()
    root = Path(manuscript_dir).resolve()
    try:
        return PurePosixPath(path.relative_to(root)).as_posix()
    except ValueError:
        return f"{EXTERNAL_PREFIX}/{PurePosixPath(path).as_posix().lstrip('/')}"


def is_external(rel: str) -> bool:
    """Whether this staged name is the mapping of an out-of-tree figure."""
    return PurePosixPath(rel).parts[:1] == (EXTERNAL_PREFIX,)


def source_path(rel: str, manuscript_dir: Path | str) -> Path | None:
    """The file on disk a staged name refers to, or None if it may not be read.

    The inverse of `staged_rel`, and the ONE place a served path becomes a
    filesystem path. None for anything that climbs out of the manuscript without
    having been mapped -- that is the guard, and it is why a hostile
    `<img src="../secret">` reaches no file here and stages nothing.
    """
    root = Path(manuscript_dir).resolve()
    if is_external(rel):
        parts = PurePosixPath(rel).parts[1:]
        if not parts:
            return None
        return Path("/", *parts).resolve()
    path = (root / rel).resolve()
    if root not in path.parents:
        return None
    return path
