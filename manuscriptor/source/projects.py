"""Vault-sourced project list for the app's "Open Project" surface.

Reads the Obsidian vault's canonical project model: each <Project>/Tasks.md
frontmatter declares `cwds:` (the working directories the project owns). For
each project we find its manuscript root(s) with the shared root rule and emit
{name, root, main}. Stdlib only. A missing vault yields [] — never an error —
so the app degrades to Recent + Open Folder.
"""
from __future__ import annotations

import os
from pathlib import Path

from manuscriptor.source.root import find_root, has_documentclass

# Directories never worth walking when hunting for a manuscript root.
_SKIP = {".git", ".build", "node_modules", "__pycache__", ".venv", "venv",
         "build", "dist", ".obsidian"}
_MAX_DEPTH = 4  # a paper root sits near the top of a repo, not buried deep


def _cwds_from_frontmatter(text: str) -> list[str]:
    """Parse the `cwds:` block-sequence out of YAML frontmatter, stdlib only."""
    if not text.startswith("---"):
        return []
    end = text.find("\n---", 3)
    if end == -1:
        return []
    fm = text[3:end].splitlines()
    out: list[str] = []
    in_cwds = False
    for raw in fm:
        line = raw.rstrip()
        if not line.strip():
            continue
        if not line.startswith(" ") and line.rstrip().endswith(":"):
            in_cwds = line.strip() == "cwds:"
            continue
        if in_cwds and line.lstrip().startswith("- "):
            out.append(line.lstrip()[2:].strip())
        elif in_cwds and not line.startswith(" "):
            in_cwds = False
    return out


def _base_dir(cwd: str) -> Path:
    """A cwds entry may be a glob (`/a/b/**`); take the concrete base."""
    stripped = cwd.split("*", 1)[0].rstrip("/")
    return Path(os.path.expanduser(stripped)).resolve()


def _manuscript_roots(base: Path) -> list[Path]:
    """Dirs under `base` (incl. base) holding a .tex with \\documentclass."""
    roots: list[Path] = []
    seen: set[Path] = set()
    if not base.is_dir():
        return roots
    base = base.resolve()
    for dirpath, dirnames, filenames in os.walk(base):
        d = Path(dirpath)
        depth = len(d.relative_to(base).parts)
        if depth >= _MAX_DEPTH:
            dirnames[:] = []
        dirnames[:] = [x for x in dirnames if x not in _SKIP and not x.startswith(".")]
        for f in filenames:
            if f.endswith(".tex") and has_documentclass(d / f):
                if d not in seen:
                    seen.add(d)
                    roots.append(d)
                break
    return roots


def list_projects(vault: Path) -> list[dict]:
    vault = Path(os.path.expanduser(str(vault)))
    if not vault.is_dir():
        return []
    out: list[dict] = []
    seen_roots: set[Path] = set()
    for tasks in sorted(vault.glob("*/Tasks.md")):
        name = tasks.parent.name
        try:
            cwds = _cwds_from_frontmatter(tasks.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        bases: list[Path] = []
        for c in cwds:
            b = _base_dir(c)
            if b not in bases:
                bases.append(b)
        for b in bases:
            for root in _manuscript_roots(b):
                if root in seen_roots:
                    continue
                seen_roots.add(root)
                _, main = find_root(root)
                out.append({"name": name, "root": root.as_posix(), "main": main})
    return out
