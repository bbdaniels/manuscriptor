"""Tree-wide document discovery.

The single-directory root rule (`source/root.py`) answers "which .tex in THIS
directory is the document". This module answers the question one level up: given
a top-level project directory, "which editable documents exist ANYWHERE in the
tree". It is what lets `manuscriptor serve <project>` open a repo whose paper
lives in `latex/main.tex` rather than at the root, and browse every paper,
appendix, response, and slide deck the tree holds from one switcher.

The openable unit is a DOCUMENT -- a `.tex` that declares an uncommented
`\\documentclass`, reusing `root.has_documentclass`. A fragment (an `\\input`
file with no preamble) is deliberately NOT discovered: it renders broken alone,
which is the whole reason `source/root.py` exists. A fragment is reached by
opening its parent document and navigating to its blocks.

Discovery walks with the same skip set the watcher and build use (`.git`,
build/output caches, virtualenvs), and caps depth so a pathological tree cannot
turn a serve into a filesystem crawl.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# The title reader, not a fourth copy of it. `render` imports nothing from
# `source.tree`, so the direction is one-way and there is no cycle.
from manuscriptor.render import pandoc
from manuscriptor.source.root import MAIN_NAME, HEAD_BYTES, has_documentclass

# Directories a document search must never descend into: version control, the
# build/output caches (which mirror `.tex` and would double-count every
# document), dependency trees, and the vault. Mirrors watch.IGNORED_DIRS and the
# plan's skip list, unified here so one edit changes every consumer.
SKIP_DIRS = {
    ".git", ".build", "build", "dist", "node_modules", "output",
    "__pycache__", ".venv", "venv", ".obsidian",
}

# A project tree deeper than this is almost certainly a dependency or data dump,
# not more manuscripts. The cap keeps a serve from crawling the whole disk.
MAX_DEPTH = 5

@dataclass(frozen=True)
class Document:
    """One editable document found in the tree.

    `root_dir` is the directory the document is served from (its flatten root,
    where its `comments.jsonl` and build output live); `main` is the file name
    within it; `rel_main` is the POSIX path from the served top-level root to the
    main file, which is the document's stable identifier in the switcher and the
    `?main=` query. For a document sitting at the served root, `rel_main` is just
    the file name, so a single-directory manuscript's switcher list is unchanged.
    """

    root_dir: str      # absolute directory holding the document's main file
    main: str          # the main file's name, e.g. "main.tex"
    rel_folder: str    # POSIX path of root_dir relative to the served root ("" at root)
    rel_main: str      # POSIX path of the main file relative to the served root
    title: str = ""    # best-effort \title{...}, for display only


def _title_of(path: Path) -> str:
    """A best-effort document title from its `\\title{...}`, or "".

    Reads only the head, like `has_documentclass`, so a large document costs no
    more than the class check that already paged it in. What counts as the title
    is `pandoc.document_title`'s answer and not a fourth one written here; this
    function owns only the decision to read a head rather than a whole file.
    Never fatal.
    """
    try:
        with path.open("rb") as fh:
            head = fh.read(HEAD_BYTES)
    except OSError:
        return ""
    return pandoc.document_title(head.decode("utf-8", errors="replace"))


def _folder_sort_key(doc: Document) -> tuple:
    """Root folder first, then by folder path; `main.tex` first within a folder.

    The default document a top-level open lands on is the first of this order:
    the served root's `main.tex` when there is one, else the shallowest folder's
    principal document. `main.tex`-first mirrors `root.candidates`, so a flat
    manuscript directory produces the identical list it did before the tree walk.
    """
    depth = 0 if not doc.rel_folder else doc.rel_folder.count("/") + 1
    return (depth, doc.rel_folder, doc.main != MAIN_NAME, doc.main.lower())


def discover(root: Path, *, max_depth: int = MAX_DEPTH) -> list[Document]:
    """Every editable document in the tree under `root`, grouped-folder ordered.

    A document is a `.tex` declaring an uncommented `\\documentclass`. Skip
    directories are never descended into; the walk stops at `max_depth`
    directories below `root`. The result is ordered so the first entry is the
    natural default to open (see `_folder_sort_key`) and consumers can group by
    `rel_folder` to show the tree.
    """
    root = Path(root).resolve()
    found: list[Document] = []
    for dirpath, dirnames, filenames in os.walk(root):
        here = Path(dirpath)
        rel = here.relative_to(root)
        depth = 0 if rel == Path(".") else len(rel.parts)
        # Prune: never descend into a skip dir, and never past the depth cap.
        # Editing dirnames in place is how os.walk is told where not to go.
        if depth >= max_depth:
            dirnames[:] = []
        else:
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for name in filenames:
            if not name.endswith(".tex") or name.startswith("."):
                continue
            p = here / name
            if not has_documentclass(p):
                continue
            rel_folder = "" if rel == Path(".") else rel.as_posix()
            rel_main = name if not rel_folder else f"{rel_folder}/{name}"
            found.append(Document(
                root_dir=str(here),
                main=name,
                rel_folder=rel_folder,
                rel_main=rel_main,
                title=_title_of(p),
            ))
    found.sort(key=_folder_sort_key)
    return found


def current_root(served_dir: Path, main: str | None = None) -> Path:
    """The directory the document opened on is served from, for a top-level serve.

    Mirrors the choice `Session._default_current` makes, so a consumer outside
    the server (the drain agent's launch) can bind to the same place the page
    writes its `comments.jsonl`: an explicit `--main` names a file in the served
    directory itself, so its root is the served directory; otherwise the first
    discovered document's `root_dir` -- a subfolder when the paper sits deeper
    than the project root; and when discovery finds nothing the served directory
    stands (the single-directory root rule handles the lone-fragment case
    downstream). For an ordinary flat manuscript this is `served_dir`, so nothing
    about a single-directory serve changes.
    """
    served = Path(served_dir).resolve()
    if main:
        return served
    docs = discover(served)
    return Path(docs[0].root_dir) if docs else served
