"""The one path from a manuscript directory to the blob the page consumes.

flatten -> segment -> anchor -> pandoc -> postprocess, in that order, plus the
bookkeeping the viewer needs: an outline, per-block citation and value lists,
and the chats read off the log.

Kept apart from `app.py` so it can be run headless. `manuscriptor build` uses it
to write a static page, `manuscriptor serve` uses it for the first paint and for
every rebuild afterwards, and a test can call it without opening a socket.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from manuscriptor.render import pandoc, postprocess, refs
from manuscriptor.source import anchors, blocks as blocks_mod
from manuscriptor.source.flatten import flatten
from manuscriptor.server import chat, producers


@dataclass
class Build:
    """One complete pass over the manuscript."""

    blocks: tuple
    by_id: dict
    blob: dict
    preamble: str
    root: Path
    main_tex: Path


_DATA_MX_RE = re.compile(r'<([a-zA-Z][-\w]*)([^>]*?)\sdata-mx="([^"]+)"([^>]*)>')
_CITE_RE = re.compile(r'data-cites="([^"]+)"')


def find_main_tex(manuscript_dir: Path, main: str | None = None) -> Path:
    if main:
        p = manuscript_dir / main
        if not p.exists():
            raise FileNotFoundError(f"main TeX not found: {p}")
        return p
    named = manuscript_dir / "main.tex"
    if named.exists():
        return named
    candidates = sorted(manuscript_dir.glob("*.tex"))
    if not candidates:
        raise FileNotFoundError(f"no .tex file in {manuscript_dir}; pass --main")
    return candidates[0]


def find_bib(manuscript_dir: Path, bib: str | None = None) -> Path | None:
    if bib:
        p = manuscript_dir / bib
        return p if p.exists() else None
    found = sorted(manuscript_dir.glob("*.bib"))
    return found[0] if found else None


def build(
    manuscript_dir: Path,
    *,
    main: str | None = None,
    bib: str | None = None,
    output_dir: Path | None = None,
) -> Build:
    manuscript_dir = Path(manuscript_dir).resolve()
    main_tex = find_main_tex(manuscript_dir, main)
    bib_path = find_bib(manuscript_dir, bib)
    out = Path(output_dir).resolve() if output_dir else manuscript_dir / "build" / "manuscriptor"
    out.mkdir(parents=True, exist_ok=True)
    _keep_out_of_git(out)

    flat = flatten(main_tex)
    produced = producers.scan(manuscript_dir)
    bl = blocks_mod.segment(flat)
    bl = producers.apply(bl, produced, root_file=main_tex)

    aux = main_tex.with_suffix(".aux")
    labels = refs.load_labels(aux) if aux.exists() else {}

    anchored = anchors.inject(flat.text, bl)
    # Before pandoc, not after. Pandoc drops an unresolved reference outright
    # under --mathml, so resolving on its output would silently lose every
    # cross-reference in the manuscript.
    anchored, unresolved_src = refs.resolve_source(anchored, labels)
    html = pandoc.render_document(anchored, cwd=manuscript_dir, bib=bib_path)

    post = postprocess.postprocess(
        html, blocks=bl, manuscript_dir=manuscript_dir, output_dir=out, labels=labels
    )

    by_id = {b.id: b for b in bl}
    log = manuscript_dir / "comments.jsonl"
    blob = {
        "title": _title(main_tex, post["html"]),
        "path": str(main_tex),
        "html": post["html"],
        "blocks": {b.id: _block_record(b, post["html"], produced, manuscript_dir) for b in bl},
        "outline": _outline(bl),
        "chats": chat.by_block(log),
        "todos": [],
        "activity": [],
        "stats": {
            "files": len({b.file for b in bl}),
            "cites": len(set(_all_cites(post["html"]))),
            "values": sum(len(b.includes) for b in bl),
            "exhibits": sum(1 for b in bl if b.kind in ("table", "figure")),
        },
        "diagnostics": {
            "unanchored": post.get("unanchored", []),
            "unresolved_refs": sorted(set(unresolved_src) | set(post.get("unresolved_refs", []))),
            "missing_includes": list(flat.missing),
        },
    }
    return Build(
        blocks=bl,
        by_id=by_id,
        blob=blob,
        preamble=pandoc.extract_preamble(flat.text),
        root=manuscript_dir,
        main_tex=main_tex,
    )


def _keep_out_of_git(out: Path) -> None:
    """Make the build directory invisible to the manuscript's repository.

    The default output lives inside the manuscript directory, which is almost
    always a git working tree the author cares about. Serving a paper should
    never be the reason `git status` grows a page of untracked files, and the
    author should not have to know to add the entry themselves.
    """
    marker = out / ".gitignore"
    if not marker.exists():
        try:
            marker.write_text("*\n", encoding="utf-8")
        except OSError:
            pass


def _rel(path, root) -> str:
    """A path the reader can hold in their head.

    The title bar already names the manuscript, so repeating its whole absolute
    path on every block turns the one useful part, which file and which line,
    into something to read past.
    """
    p = Path(path)
    if root is None:
        return str(p)
    try:
        return str(p.relative_to(Path(root)))
    except ValueError:
        return str(p)


# ----------------------------------------------------------------- internals


def _block_record(b, html: str, produced: dict, root: Path | None = None) -> dict:
    return {
        "id": b.id,
        "kind": b.kind,
        "file": _rel(b.file, root),
        "line_start": b.line_start,
        "line_end": b.line_end,
        "source": b.source_text,
        "editable": b.editable,
        "parent_heading": b.parent_heading,
        "producer": str(produced[b.file]) if b.file in produced else None,
        "includes": [
            {"directive": inc.directive, "target": str(inc.target),
             "producer": str(produced[inc.target]) if inc.target in produced else None}
            for inc in b.includes
        ],
        "cites": _cites_in_block(html, b.id),
        "values": [
            {
                "key": Path(inc.target).stem,
                "path": str(inc.target),
                "producer": str(produced[inc.target]) if inc.target in produced else None,
                "description": None,
            }
            for inc in b.includes
        ],
    }


def _cites_in_block(html: str, block_id: str) -> list[str]:
    """Citation keys appearing inside one block's rendered element."""
    m = re.search(r'data-mx="' + re.escape(block_id) + r'"', html)
    if not m:
        return []
    window = html[m.end() : m.end() + 6000]
    end = window.find("data-mx=")
    if end != -1:
        window = window[:end]
    keys: list[str] = []
    for hit in _CITE_RE.finditer(window):
        keys.extend(hit.group(1).split())
    return sorted(set(keys))


def _all_cites(html: str) -> list[str]:
    keys: list[str] = []
    for hit in _CITE_RE.finditer(html):
        keys.extend(hit.group(1).split())
    return keys


_LEVELS = {
    "part": 1, "chapter": 1, "section": 1,
    "subsection": 2, "subsubsection": 3, "paragraph": 4, "subparagraph": 4,
}
_SECTION_CMD_RE = re.compile(r"\\(part|chapter|section|subsection|subsubsection|paragraph|subparagraph)\*?")


def _outline(bl) -> list[dict]:
    """The rail's nav. Depth comes from the sectioning command, not a guess.

    Every entry was previously emitted at level 1, which flattened a paper with
    three levels of heading into an undifferentiated list.
    """
    out = []
    for b in bl:
        if b.kind != "heading":
            continue
        cmd = _SECTION_CMD_RE.search(b.source_text)
        level = _LEVELS.get(cmd.group(1), 1) if cmd else 1
        text = re.sub(r"\\[a-zA-Z]+\*?", "", b.source_text)
        text = text.replace("{", "").replace("}", "").strip()
        if text:
            out.append({"level": level, "text": re.sub(r"\s+", " ", text)[:70], "id": b.id})
    return out


_TEX_TITLE_RE = re.compile(r"\\title\s*\{(.+?)\}\s*$", re.M | re.S)


def _title(main_tex: Path, html: str) -> str:
    try:
        src = main_tex.read_text(encoding="utf-8")
    except OSError:
        src = ""
    m = _TEX_TITLE_RE.search(src)
    if m:
        text = m.group(1).replace("\\\\", " ")
        text = re.sub(r"\\[a-zA-Z]+\*?", "", text)
        text = re.sub(r"\s+", " ", text.replace("{", "").replace("}", "")).strip()
        if text:
            return text
    m2 = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.S)
    if m2:
        text = re.sub(r"<[^>]+>", "", m2.group(1)).strip()
        if text:
            return text
    return main_tex.stem
