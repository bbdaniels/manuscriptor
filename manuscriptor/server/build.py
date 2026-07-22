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
from datetime import datetime, timezone
from pathlib import Path

from manuscriptor.render import pandoc, postprocess, refs
from manuscriptor.source import anchors, blocks as blocks_mod
from manuscriptor.source.flatten import flatten
from manuscriptor.server import chat, manifest, producers


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
    keep_out_of_git(out)

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
    # What each computed value IS, derived from the code that writes it and
    # cached in the build directory. Nothing here calls a model.
    values = manifest.describe(
        manuscript_dir, [inc.target for b in bl for inc in b.includes], cache_dir=out
    )
    log = manuscript_dir / "comments.jsonl"
    blob = {
        "title": _title(main_tex, post["html"]),
        "path": str(main_tex),
        "read_only": False,
        "html": post["html"],
        "blocks": {b.id: _block_record(b, post["html"], produced, manuscript_dir, values)
                   for b in bl},
        "outline": _outline(bl),
        "chats": reanchor_chats(chat.by_block(log), bl, chat.read_chats(log)),
        # The standing agent state, so a page loading mid-run does not open on
        # "idle" while a session is halfway through the author's third comment.
        "queue": queue_view(log, bl, root=manuscript_dir),
        "ticker": ticker_view(log, bl, root=manuscript_dir),
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


def keep_out_of_git(out: Path) -> None:
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


def reanchor_chats(by_block: dict, blocks, chats) -> dict:
    """Move each chat onto the block its paragraph has become.

    A chat is keyed to the block id it was written against, and ids are derived
    from content, so answering a comment changes the id and the chat is left
    pointing at a block that no longer exists. On the page that reads as the
    comment vanishing at the exact moment it was addressed.

    Re-found by the quote recorded with the comment, which is what the quote is
    for. A chat whose paragraph is genuinely gone keeps its original key and is
    simply not shown against any block, rather than being attached to the wrong
    one.
    """
    present = {blk.id for blk in blocks}
    quotes = {c.id: c.quote for c in chats}
    out: dict[str, list] = {}
    for block_id, msgs in by_block.items():
        target = block_id
        if block_id not in present:
            quote = next((quotes.get(m["id"], "") for m in msgs if quotes.get(m["id"])), "")
            if quote:
                head = quote[:60]
                match = next((blk.id for blk in blocks if blk.source_text.startswith(head)), None)
                if match is None:
                    part = quote[:40]
                    match = next((blk.id for blk in blocks if part and part in blk.source_text), None)
                if match:
                    target = match
        out.setdefault(target, []).extend(msgs)
    return out


# --------------------------------------------------------------- the queue
#
# The margin shows one pin per comment, which answers "is anything happening on
# THIS paragraph" and nothing else. The author reading page four has no way to
# know that three comments are waiting and one is being worked. That is the
# queue: the same records, read as a list rather than as marks on the page.
#
# It carries no knowledge of Claude. It reads `comments.jsonl` and the block map
# and that is all it can do.

TICKER_LIMIT = 8


def queue_view(log: Path, blocks, *, anchored: dict | None = None, root=None, now=None) -> list[dict]:
    """Every chat still awaiting work, oldest first.

    Oldest first because that is the order a drain should work them, so the list
    is a plan rather than an inventory.

    EVERY ENTRY IS RE-ANCHORED, through the same `reanchor_chats` the state and
    chat frames go through. Ids are content-derived, so answering a comment
    renames its block: an entry still carrying the id it was written against
    names nothing the page has, and the header would then be counting work
    against a paragraph that no longer exists. A chat whose paragraph is
    genuinely gone is listed with no block rather than dropped or attached to
    the wrong one.
    """
    chats = chat.read_chats(log)
    if not chats:
        return []
    present = {b.id for b in blocks}
    where = _anchor_of(anchored if anchored is not None
                       else reanchor_chats(chat.by_block(log), blocks, chats), present)
    heads = {b.id: b.parent_heading for b in blocks}
    wheres = _wheres(blocks, root)
    starts = state_starts(log)
    at = _parse_ts(now) or datetime.now(timezone.utc)

    out: list[dict] = []
    for c in sorted(chats, key=lambda c: c.ts):
        if c.state in chat.TERMINAL:
            continue
        block = where.get(c.id)
        since = starts.get(c.id) or c.ts
        out.append({
            "id": c.id,
            "block": block,
            "section": heads.get(block) if block else None,
            "where": wheres.get(block) if block else None,
            "body": _one_line(c.body),
            "state": c.state,
            "since": since,
            "waited": _seconds_between(since, at),
        })
    return out


def ticker_view(log: Path, blocks, *, limit: int = TICKER_LIMIT,
                anchored: dict | None = None, root=None) -> list[dict]:
    """Recent agent activity, newest first, named the way the author names it.

    Read off the state records the agent actually appended, so it reports what
    happened rather than what was asked for. A handful, because this is a status
    line and not a scrollback. The live half of the ticker is assembled on the
    page from the `state` and `patch` frames; this is the seed, so a page opened
    mid-run does not start blank.
    """
    chats = chat.read_chats(log)
    present = {b.id for b in blocks}
    where = _anchor_of(anchored if anchored is not None
                       else reanchor_chats(chat.by_block(log), blocks, chats), present)
    heads = {b.id: b.parent_heading for b in blocks}
    wheres = _wheres(blocks, root)

    out: list[dict] = []
    for rec in chat.read_records(log):
        if rec.get("kind") != "state" or not rec.get("state"):
            continue
        # `queued` is the standing state, and the header already counts it.
        # Found in a browser: three comments filled the ticker with three lines
        # reading "queued" and pushed the agent's actual work off the end of it.
        if rec["state"] == "queued":
            continue
        block = where.get(rec.get("id"))
        out.append({
            "kind": "state",
            "id": rec.get("id"),
            "state": rec["state"],
            "block": block,
            "section": heads.get(block) if block else None,
            "where": wheres.get(block) if block else None,
            "when": rec.get("ts", ""),
        })
    out.reverse()
    return out[:limit]


def _wheres(blocks, root) -> dict:
    """block id -> the place a reader can go and look, `file:line`.

    The section is the right name for a paragraph, and some blocks have none: an
    abstract sits above every heading. Watched live, one of those reported "the
    manuscript · working", which told the author nothing. A file and a line is
    somewhere he can actually go.
    """
    return {b.id: f"{_rel(b.file, root)}:{b.line_start}" for b in blocks}


def _anchor_of(anchored: dict, present: set) -> dict:
    """chat id -> the live block it sits on, or None when it has none."""
    out: dict[str, str | None] = {}
    for block_id, msgs in anchored.items():
        live = block_id if block_id in present else None
        for m in msgs:
            out[m["id"]] = live
    return out


def state_starts(log: Path) -> dict[str, str]:
    """When each chat entered the state it is in now.

    Not when the comment was written. A comment that waited an hour and was
    picked up ten seconds ago has been *working* for ten seconds, and reporting
    the hour would say the agent had been stuck on it.
    """
    state: dict[str, str] = {}
    since: dict[str, str] = {}
    for rec in chat.read_records(log):
        cid = rec.get("id")
        if not cid:
            continue
        if rec.get("kind") == "comment":
            state[cid] = "queued"
            since[cid] = rec.get("ts", "")
        elif rec.get("kind") == "state" and rec.get("state"):
            if state.get(cid) != rec["state"]:
                since[cid] = rec.get("ts", since.get(cid, ""))
            state[cid] = rec["state"]
    return since


def _parse_ts(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _seconds_between(since, at) -> int:
    then = _parse_ts(since)
    if then is None:
        return 0
    return max(0, int((at - then).total_seconds()))


def _one_line(text: str, n: int = 140) -> str:
    """The body as the header can show it: one line, clipped."""
    flat = " ".join(str(text or "").split())
    return flat[:n] + "…" if len(flat) > n else flat


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


def _block_record(b, html: str, produced: dict, root: Path | None = None,
                  values: dict | None = None) -> dict:
    values = values or {}
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
        "footnotes": _footnotes_in(b.source_text),
        "values": [
            {
                "key": Path(inc.target).stem,
                "path": str(inc.target),
                "producer": str(produced[inc.target]) if inc.target in produced else None,
                "description": (values.get(Path(inc.target).stem) or {}).get("description"),
            }
            for inc in b.includes
        ],
    }


def _footnotes_in(source: str) -> list[str]:
    """The footnotes a block carries, in order.

    Pandoc lifts footnotes to the end of the document, so a reader looking at a
    paragraph has no way to see what hangs off it without hunting. They belong
    in the paragraph's own list of references, which is what they are.
    """
    out: list[str] = []
    i = 0
    while True:
        at = source.find("\\footnote", i)
        if at < 0:
            return out
        j = source.find("{", at)
        if j < 0:
            return out
        depth, k = 0, j
        while k < len(source):
            if source[k] == "\\":
                k += 2
                continue
            if source[k] == "{":
                depth += 1
            elif source[k] == "}":
                depth -= 1
                if depth == 0:
                    break
            k += 1
        out.append(re.sub(r"\s+", " ", source[j + 1 : k]).strip())
        i = k + 1


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
