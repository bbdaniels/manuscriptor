"""The one path from a manuscript directory to the blob the page consumes.

flatten -> segment -> anchor -> pandoc -> postprocess, in that order, plus the
bookkeeping the viewer needs: an outline, per-block citation and value lists,
and the chats read off the log.

Kept apart from `app.py` so it can be run headless. `manuscriptor build` uses it
to write a static page, `manuscriptor serve` uses it for the first paint and for
every rebuild afterwards, and a test can call it without opening a socket.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from manuscriptor.render import pandoc, postprocess, refs, tikz
from manuscriptor.source import anchors, blocks as blocks_mod, root
from manuscriptor.source.flatten import flatten
from manuscriptor.server import chat, drafts, feed as feed_mod, manifest, producers


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
    """An explicit `main` wins; otherwise the root rule decides.

    The rule (source/root.py, shared with the shell and the doc switcher):
    `main.tex`, else the sole file declaring an uncommented `\\documentclass`,
    else a sole .tex of any kind. Several declared documents raises with the
    choices named, because the alphabetical fallback this replaces once served
    `abstract.tex` as the paper, silently.
    """
    if main:
        p = manuscript_dir / main
        if not p.exists():
            raise FileNotFoundError(f"main TeX not found: {p}")
        return p
    return manuscript_dir / root.choose_main(manuscript_dir)


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
    # TikZ before pandoc too: each picture compiles standalone against the
    # manuscript's own preamble, rasters into the build directory cached by
    # content, and becomes an \includegraphics the pipeline already handles.
    anchored, _tikz_made, tikz_failed = tikz.replace(
        anchored, preamble=pandoc.extract_preamble(flat.text), out_dir=out)
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
    doc = main_tex.name
    blob = {
        "title": _title(main_tex, post["html"]),
        "path": str(main_tex),
        # The document being served, and the others this directory could
        # serve: every .tex declaring a document class, Overleaf-style. The
        # page's switcher is this list; chats, queue and ticker are scoped to
        # `main` so the appendix's comments never queue against the paper.
        "main": doc,
        "docs": root.candidates(manuscript_dir),
        "read_only": False,
        "html": post["html"],
        "blocks": {b.id: _block_record(b, post["html"], produced, manuscript_dir, values)
                   for b in bl},
        "outline": _outline(bl),
        "chats": reanchor_chats(chat.by_block(log, doc=doc), bl,
                                chat.read_chats(log, doc=doc)),
        # The evidence pass's verdicts, when it has run. The pass is a CLI
        # command (it calls a model, which the server never may); the server
        # only reads the files it wrote into the build directory. No files
        # means every underline stays neutral, claiming nothing.
        "cites": evidence_cites(out),
        # How many pairs the last evidence run could not find fulltext for.
        # Non-zero is what makes the page offer the repair, which is the one
        # step allowed to write the author's Zotero library and therefore a
        # deliberate second click, never a side effect of a run.
        "missing_fulltexts": missing_fulltexts(out),
        # The standing agent state, so a page loading mid-run does not open on
        # "idle" while a session is halfway through the author's third comment.
        "queue": queue_view(log, bl, root=manuscript_dir, doc=doc),
        "ticker": ticker_view(log, bl, root=manuscript_dir, doc=doc),
        "todos": todos_view(log, doc=doc),
        # Unsaved text the server is holding for this document, so a page that
        # opens after a crash, a relaunch, or a server that died mid-paragraph
        # is offered the draft instead of the author being told it is "on disk"
        # somewhere only a debugger can reach.
        "drafts": drafts.for_doc(drafts.path_for(out), doc),
        # What the drain is doing right now, as it does it. The drain writes
        # this file and the server only reads it, so the server still knows
        # nothing about Claude. Absent means no agent has ever run, which
        # renders as an idle feed rather than as an error.
        "agent_feed": feed_mod.read_feed(feed_mod.progress_path(out)),
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
            "tikz_failed": tikz_failed,
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

    Each chat is placed by ITS OWN quote, never by its neighbour's. Findings
    filed from a check carry no block id at all, so they all arrive under the
    same absent key; re-anchoring that key as one group would give every finding
    in a review the first one's paragraph and stack the whole report on it.
    """
    present = {blk.id for blk in blocks}
    quotes = {c.id: c.quote for c in chats}
    out: dict[str, list] = {}
    for block_id, msgs in by_block.items():
        if block_id in present:
            out.setdefault(block_id, []).extend(msgs)
            continue
        # A key the page no longer has, or none at all. Resolve message by
        # message: they only share this key by accident of it being absent.
        fallback = next((quotes.get(m["id"], "") for m in msgs if quotes.get(m["id"])), "")
        for m in msgs:
            match = match_by_quote(quotes.get(m["id"]) or fallback, blocks)
            out.setdefault(match or block_id, []).append(m)
    return out


def flatten_ws(text: str) -> str:
    """Whitespace flattened, so a wrapped source and a typed sentence compare."""
    return " ".join(text.split())


def match_by_quote(quote: str, blocks) -> str | None:
    """The one rule for placing a quote on a block. Used by every re-anchoring.

    Both sides are compared with their whitespace flattened. A quote arrives
    from a person, a rendered page, or a reviewer's highlight, so its words are
    joined by single spaces, while the source it came from is usually hard
    wrapped one clause per line. The two say the same sentence and differ only
    in where the author pressed return, so matching raw source bytes drops the
    anchor on every manuscript written that way, which is most of them.

    This lives in one place on purpose. The rule was implemented twice, here and
    in the drain, and the two disagreed the moment either was touched.
    """
    if not quote:
        return None
    quote = flatten_ws(quote)
    flat = [(blk.id, flatten_ws(blk.source_text)) for blk in blocks]
    head = quote[:60]
    for bid, text in flat:
        if head and text.startswith(head):
            return bid
    part = quote[:40]
    for bid, text in flat:
        if part and part in text:
            return bid
    return None


# ------------------------------------------------------------- the evidence


def evidence_cites(out: Path) -> dict:
    """The evidence pass's verdicts, in the shape the page consumes.

    `manuscriptor evidence` writes `citations.json` (one entry per key) and
    `evidence.json` (one entry per claim-and-key pair) into the build
    directory. Folded per key: the strongest status across pairs wins the
    underline, because "supported verbatim somewhere" is the claim the colour
    makes. A key the pass examined and could not support is `missing` (red);
    a key the pass never saw has no record at all and stays neutral, which is
    the difference between "checked, nothing found" and "not checked".

    A corrupt or absent file yields no records rather than no build: the
    manuscript must render whether or not the evidence pass has ever run.
    """
    try:
        citations = json.loads((Path(out) / "citations.json").read_text(encoding="utf-8"))
        ev_path = Path(out) / "evidence.json"
        evidence = json.loads(ev_path.read_text(encoding="utf-8")) if ev_path.exists() else []
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    if not isinstance(citations, list) or not isinstance(evidence, list):
        return {}

    RANK = {"verbatim": 2, "paraphrase": 1}
    cites: dict[str, dict] = {}
    for c in citations:
        key = c.get("cite_key")
        if not key:
            continue
        # The fulltext facts travel with the verdict, because "red" covers two
        # very different situations and the page was reporting neither. A source
        # with no fulltext could not be read at all, which is a library problem
        # and what the repair button exists for. A source that WAS read and still
        # supported nothing is a writing problem, and that sentence belongs on a
        # review list. Both were rendering as "no evidence loaded ... the
        # underline stays neutral", while the underline was red and the pass had
        # in fact finished.
        cites[key] = {
            "status": "missing",
            "title": c.get("title") or "",
            "quotes": [],
            "fulltext": bool(c.get("has_fulltext")),
            "fulltext_chars": int(c.get("fulltext_chars") or 0),
            "fulltext_source": c.get("fulltext_source") or "",
        }
    for r in evidence:
        rec = cites.get(r.get("cite_key"))
        if rec is None:
            continue
        for q in r.get("quotes", []):
            if not q.get("text"):
                continue
            status = q.get("status") or "paraphrase"
            rec["quotes"].append({"text": q["text"], "status": status})
            if RANK.get(status, 0) > RANK.get(rec["status"], 0):
                rec["status"] = status
    return cites


def missing_fulltexts(out: Path) -> int:
    """How many entries the evidence pass logged to `missing.json`."""
    try:
        missing = json.loads((Path(out) / "missing.json").read_text(encoding="utf-8"))
        return len(missing) if isinstance(missing, list) else 0
    except (OSError, json.JSONDecodeError, ValueError):
        return 0


# --------------------------------------------------------------- the to-dos


def todos_view(log: Path, *, doc: str | None = None) -> list[dict]:
    """The rail's to-do list, folded from the log.

    A `todo` record creates one, a `todo-state` record toggles it, and the
    latest state wins: append-only like everything else the page and the
    agent share, so neither writer can conflict with the other. Scoped to the
    document the way comments are, with doc-less records belonging to
    whichever document is being read.
    """
    base: dict[str, dict] = {}
    done: dict[str, bool] = {}
    for rec in chat.read_records(log):
        if rec.get("kind") == "todo" and rec.get("text"):
            if doc is not None and rec.get("doc", "") not in ("", doc):
                continue
            base.setdefault(rec["id"], rec)
        elif rec.get("kind") == "todo-state":
            done[rec.get("id", "")] = bool(rec.get("done"))
    return [{"id": tid, "text": rec.get("text", ""), "done": done.get(tid, False)}
            for tid, rec in base.items()]


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


def queue_view(log: Path, blocks, *, anchored: dict | None = None, root=None,
               now=None, doc: str | None = None) -> list[dict]:
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
    chats = chat.read_chats(log, doc=doc)
    if not chats:
        return []
    present = {b.id for b in blocks}
    where = _anchor_of(anchored if anchored is not None
                       else reanchor_chats(chat.by_block(log, doc=doc), blocks, chats),
                       present)
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
                anchored: dict | None = None, root=None,
                doc: str | None = None) -> list[dict]:
    """Recent agent activity, newest first, named the way the author names it.

    Read off the state records the agent actually appended, so it reports what
    happened rather than what was asked for. A handful, because this is a status
    line and not a scrollback. The live half of the ticker is assembled on the
    page from the `state` and `patch` frames; this is the seed, so a page opened
    mid-run does not start blank.
    """
    chats = chat.read_chats(log, doc=doc)
    present = {b.id for b in blocks}
    where = _anchor_of(anchored if anchored is not None
                       else reanchor_chats(chat.by_block(log, doc=doc), blocks, chats),
                       present)
    heads = {b.id: b.parent_heading for b in blocks}
    wheres = _wheres(blocks, root)
    # State records carry no doc of their own; they belong to whatever comment
    # they answer, so the in-scope chat ids are the filter.
    in_scope = {c.id for c in chats} if doc is not None else None

    out: list[dict] = []
    for rec in chat.read_records(log):
        if rec.get("kind") != "state" or not rec.get("state"):
            continue
        if in_scope is not None and rec.get("id") not in in_scope:
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
