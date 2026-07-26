"""M3 — the local HTTP and websocket server.

Serves the rendered manuscript, pushes block-level patches on change, and
accepts edits and chat messages from the page.

The server is the product and the shell is a client. Any number of clients may
attach to the same port, which is what lets the author work in a standalone
window while Claude verifies the same page in a browser tab through devtools.
Neither constrains the other.

Driving the page also happens here rather than through browser automation. Both
ends of the websocket are ours, so scrolling to a block, opening a chat, or
flashing a diff are just messages down a channel that already exists for hot
reload. This is why the shell choice is not load-bearing.

THE SERVER HAS ZERO KNOWLEDGE OF CLAUDE, and Claude never talks to the server.
They share a filesystem and communicate through exactly two things: the `.tex`
tree and `comments.jsonl`. Nothing in this module may call an LLM. A Claude Code
session edits files with its ordinary tools and the watcher below notices.
"""
from __future__ import annotations

import asyncio
import json
import re
import webbrowser
from importlib import resources
from pathlib import Path

from aiohttp import WSMsgType, web
from jinja2 import Template

from manuscriptor.templates.ext import load as _extensions

from manuscriptor.server import build as build_mod
from manuscriptor.server import chat
from manuscriptor.server import compile as compile_mod
from manuscriptor.server import drafts as drafts_mod
from manuscriptor.server import ports
from manuscriptor.source import insert as insert_mod
from manuscriptor.source import splice as splice_mod
from manuscriptor.source import tree as tree_mod

HOLDER = "author"


class Session:
    """One served project, its current document's build, and its clients.

    The served directory (`self.dir`) is the top-level the caller handed us; it
    is the tree the watcher watches and the root discovery walks. It is NOT
    necessarily where a document lives: serving a repo whose paper sits in
    `latex/main.tex` is the whole point of the pivot. `self.docs` is every
    editable document in the tree (the switcher list); `self.current` is the one
    being served, and every per-document operation -- build, comment log, build
    output, splice -- is rooted at `self.current.root_dir`, not at `self.dir`.
    """

    def __init__(self, manuscript_dir: Path, *, main: str | None = None,
                 bib: str | None = None, read_only: bool = False,
                 on_switch=None):
        self.dir = Path(manuscript_dir).resolve()
        self.bib = bib
        self.read_only = read_only
        # Fired with the new document root whenever a switch moves it, so the
        # caller (the drain agent in cmd_serve) can follow the current document.
        self._on_switch = on_switch
        self.clients: set[web.WebSocketResponse] = set()
        self.lock = asyncio.Lock()
        self.seen_chats: dict[str, dict] = {}
        # The queue as it was last pushed, reduced to its identity so a repaint
        # is triggered by work moving and not by a second passing.
        self.seen_queue: list[tuple] = []
        # Every editable document in the tree, and the one we open on. An
        # explicit --main names a file in the served directory itself; otherwise
        # the first discovered document is the default, so opening a top-level
        # project never has to resolve "the one manuscript" and never errors.
        self.docs = tree_mod.discover(self.dir)
        self.current = self._default_current(main)
        self.build = None
        self.rebuild()

    def _default_current(self, main: str | None) -> tree_mod.Document | None:
        """Which document to open on. `None` means "no document was discovered".

        An explicit --main is honored against the served directory directly, as
        before. Otherwise the first discovered document (the tree-ordered
        default) opens. When discovery finds nothing -- a lone fragment, or a
        directory whose only .tex has no document class -- `None` falls the
        rebuild back onto the single-directory root rule, which preserves every
        pre-pivot edge case (a lone fragment still serves; a genuinely empty
        directory still raises with the choices named).
        """
        if main:
            return tree_mod.Document(root_dir=str(self.dir), main=main,
                                     rel_folder="", rel_main=main)
        return self.docs[0] if self.docs else None

    @property
    def blob(self) -> dict:
        return self.build.blob

    @property
    def root(self) -> Path:
        """The directory the CURRENT document is served from.

        Everything per-document is rooted here: the flatten, the block sources,
        the comment log, and the build output. It equals `self.dir` for an
        ordinary single-directory manuscript, and is a subdirectory when the
        served tree holds the document deeper down.
        """
        if self.build is not None:
            return self.build.root
        if self.current is not None:
            return Path(self.current.root_dir)
        return self.dir

    @property
    def log(self) -> Path:
        """The current document's comment log, beside the document itself."""
        return self.root / "comments.jsonl"

    @property
    def doc(self) -> str:
        """The document being served, by name. Chats are scoped to it."""
        return self.build.main_tex.name if self.build is not None else ""

    @property
    def drafts_file(self) -> Path:
        """Unsaved text, on disk. In the build directory rather than beside the
        log, because that directory writes its own `.gitignore` and serving a
        paper must never make `git status` grow."""
        return drafts_mod.path_for(self.root / "build" / "manuscriptor")

    @property
    def current_ref(self) -> str:
        """The current document's tree identifier (its `rel_main`)."""
        return self.current.rel_main if self.current is not None else self.doc

    def _overlay_tree_docs(self) -> None:
        """Replace the build's single-directory switcher list with the tree list.

        `build` fills `docs`/`main` from the one directory it flattened, which is
        correct for `manuscriptor build` and the tests. The server sees the whole
        tree, so it overlays the tree-wide document list (each entry's
        `rel_main`) and names the current document by its `rel_main` so the
        switcher can select it. When discovery found nothing, the build's own
        single-directory values stand, preserving lone-fragment behavior.
        """
        if self.build is None or not self.docs:
            return
        self.build.blob["docs"] = [d.rel_main for d in self.docs]
        if self.current is not None:
            self.build.blob["main"] = self.current.rel_main

    def rebuild(self):
        previous = self.build
        if self.current is None:
            self.build = build_mod.build(self.dir, main=None, bib=self.bib)
        else:
            self.build = build_mod.build(
                Path(self.current.root_dir), main=self.current.main, bib=self.bib)
        self._overlay_tree_docs()
        return previous

    def switch(self, rel_main: str) -> None:
        """Serve a different document from anywhere in the tree.

        Only a `rel_main` the tree actually offers (`self.docs`) is accepted: the
        value arrives off a URL query, and a path is how a query walks out of the
        project. The chosen document is served from its own root directory, so a
        switch spans folders, not just siblings. A refused switch changes nothing.
        """
        if rel_main == self.current_ref:
            return
        entry = next((d for d in self.docs if d.rel_main == rel_main), None)
        if entry is None:
            raise ValueError(f"{rel_main!r} is not a document this project serves")
        previous = self.current
        old_root = self.root
        self.current = entry
        try:
            self.rebuild()
        except Exception:
            self.current = previous
            raise
        # The drain agent follows the current document into its own folder.
        if self._on_switch is not None and self.root != old_root:
            try:
                self._on_switch(self.root)
            except Exception:
                pass

    async def broadcast(self, msg: dict) -> None:
        dead = []
        for ws in self.clients:
            try:
                await ws.send_json(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.clients.discard(ws)

    async def on_change(self) -> None:
        """A .tex file changed on disk. Re-render and push only what moved."""
        async with self.lock:
            try:
                previous = self.rebuild()
            except Exception as exc:  # a manuscript mid-edit may not render
                await self.broadcast({"type": "error", "message": str(exc)[:400]})
                return
            patch = _diff(previous, self.build) if previous else None
            # Ids are content-derived, so this rebuild renamed every block it
            # changed. A stored draft left under an old id is a draft nobody
            # will ever be offered again, which is the failure the store exists
            # to prevent, so it moves with the rename.
            if patch and patch.get("renamed") and not self.read_only:
                drafts_mod.rekey(self.drafts_file, patch["renamed"])
        if patch:
            await self.broadcast(patch)

    async def on_assets_change(self) -> None:
        """A figure changed on disk, with no source change beside it.

        The block diff cannot carry this: the LaTeX is untouched, so ids and
        sources match and the patch is empty, while the rasterized PNG in the
        build directory has silently gone stale. Rebuild (the mtime check
        re-rasterizes exactly the changed figures, `copy2` refreshes direct
        images) and tell every client to refetch its images past the browser
        cache. This is how a regenerated figure reaches an open page.
        """
        async with self.lock:
            try:
                self.rebuild()
            except Exception as exc:
                await self.broadcast({"type": "error", "message": str(exc)[:400]})
                return
        await self.broadcast({"type": "assets", "v": chat.now()})

    async def on_log_change(self) -> None:
        """Tell the page what the agent did.

        The agent works by appending to `comments.jsonl`: a state record when it
        picks a comment up, another when it finishes. Nothing watched that file,
        so an open page never learned. The margin sat on `queued` for the whole
        run and the author had no sign anything was happening, which is exactly
        the "you have to see it working" requirement failing in the one place it
        matters.

        Only differences are broadcast. Re-reading a log that has not changed
        must not repaint the margin.
        """
        # Re-anchored FIRST. A comment is keyed to the block id it was written
        # against, and answering it changes that id, so a frame addressed to the
        # original id lands on a block the page no longer has. The margin then
        # freezes on whatever state it saw last, which is exactly what happened:
        # the pin sat on `working` forever because `done` was addressed to a
        # block that had been renamed by the very edit being reported.
        anchored = build_mod.reanchor_chats(
            chat.by_block(self.log, doc=self.doc), self.build.blocks,
            chat.read_chats(self.log, doc=self.doc)
        ) if self.build is not None else chat.by_block(self.log)

        current = {}
        for block_id, msgs in anchored.items():
            for m in msgs:
                current[m["id"]] = dict(m, block=block_id)

        # WHEN THE STATE CHANGED, not when the comment was written. A message
        # carries the comment's timestamp, so timing the frame by it made every
        # ticker entry claim to be as old as the comment it belonged to: a `done`
        # a second old read as five minutes, and the newest line sat above older
        # ones carrying a larger age. Found by watching the page, not the code.
        starts = build_mod.state_starts(self.log)

        for cid, msg in current.items():
            was = self.seen_chats.get(cid)
            if was is None:
                await self.broadcast({
                    "type": "chat", "block": msg["block"],
                    "message": {k: v for k, v in msg.items() if k != "block"},
                })
            if not was or was.get("state") != msg.get("state"):
                await self.broadcast({
                    # `id` names the chat, so the page can move the message's
                    # own label off "working" when the work lands; a frame
                    # keyed only by block cannot say WHICH chat moved.
                    "type": "state", "block": msg["block"], "state": msg["state"],
                    "id": cid,
                    # The ticker reports what happened, so it ages each entry by
                    # the log's own time rather than by the client's clock at the
                    # moment the frame happened to arrive.
                    "at": starts.get(cid) or msg.get("ts") or chat.now(),
                })

        # The standing state, as a list rather than as marks on the page. A pin
        # answers "is anything happening on THIS paragraph"; the author reading
        # page four needs "three waiting, one being worked" without hunting.
        # Re-anchored through the same pass as the frames above, or an entry
        # names a block the page has lost.
        queue = build_mod.queue_view(
            self.log, self.build.blocks, anchored=anchored, root=self.root, doc=self.doc
        ) if self.build is not None else []
        ident = [(e["id"], e["block"], e["state"], e["since"]) for e in queue]
        if ident != self.seen_queue:
            await self.broadcast({"type": "queue", "queue": queue})
            self.seen_queue = ident

        # Connected clients got the frames above. Anyone loading afterwards
        # reads the blob, so it has to carry the same state rather than the one
        # frozen at the last .tex rebuild.
        if self.build is not None:
            self.build.blob["chats"] = anchored
            self.build.blob["queue"] = queue
            self.build.blob["ticker"] = build_mod.ticker_view(
                self.log, self.build.blocks, anchored=anchored, root=self.root,
                doc=self.doc
            )
        self.seen_chats = current

    async def on_edit(self, block_id: str, source: str) -> dict:
        """Splice one block back to disk. The watcher handles the redraw."""
        if self.read_only:
            return {"type": "held", "block": block_id,
                    "reason": "This manuscript is open read-only, so nothing here can write to it."}
        block = self.build.by_id.get(block_id)
        if block is None:
            return {"type": "held", "block": block_id, "reason": "unknown block"}
        try:
            await asyncio.to_thread(
                splice_mod.splice, block, source, root=self.root, holder=HOLDER
            )
        except splice_mod.NotEditable as exc:
            return {"type": "held", "block": block_id, "reason": str(exc)}
        except splice_mod.StaleBlock as exc:
            return {"type": "held", "block": block_id, "reason": str(exc)}
        except splice_mod.BlockLocked as exc:
            return {"type": "held", "block": block_id, "reason": str(exc)}
        # It is on disk in the manuscript now, so the draft has done its job.
        self.forget_draft(block_id)
        return {"type": "saved", "block": block_id, "at": chat.now()}

    def keep_draft(self, block_id: str, source: str) -> dict:
        """Hold unsaved text where a crash cannot take it.

        The page keeps its own copy, but the page is a browser: an ephemeral
        origin, a window that closes, a server that dies mid-paragraph. Two
        edits were lost on 2026-07-26 and both had to be read out of WebKit's
        sqlite by hand. Read-only serving keeps nothing, like everything else.
        """
        if self.read_only or not block_id:
            return {"type": "draft", "block": block_id, "kept": False}
        drafts_mod.put(self.drafts_file, doc=self.doc, block=block_id, text=source)
        return {"type": "draft", "block": block_id, "kept": bool(source),
                "at": chat.now()}

    def forget_draft(self, block_id: str) -> None:
        if self.read_only or not block_id:
            return
        drafts_mod.drop(self.drafts_file, doc=self.doc, block=block_id)

    def _todos_frame(self) -> dict:
        todos = build_mod.todos_view(self.log, doc=self.doc)
        if self.build is not None:
            self.build.blob["todos"] = todos
        return {"type": "todos", "todos": todos}

    async def on_todo(self, text: str) -> dict:
        """Add a to-do. A record in the shared log, like everything else."""
        if self.read_only:
            return {"type": "held", "block": "",
                    "reason": "This manuscript is open read-only, so the log is not written."}
        text = str(text).strip()
        if not text:
            return self._todos_frame()
        n = sum(1 for r in chat.read_records(self.log) if r.get("kind") == "todo")
        chat.append(self.log, {"id": f"t-{n + 1:04d}", "kind": "todo",
                               "text": text, "doc": self.doc, "author": "bb"})
        return self._todos_frame()

    async def on_todo_toggle(self, todo_id: str, done: bool) -> dict:
        """Toggle a to-do: a new state record, never a rewrite."""
        if self.read_only:
            return {"type": "held", "block": "",
                    "reason": "This manuscript is open read-only, so the log is not written."}
        chat.append(self.log, {"id": str(todo_id), "kind": "todo-state",
                               "done": bool(done)})
        return self._todos_frame()

    async def on_dismiss(self, chat_id: str) -> dict:
        """The author closes a review finding. A state record, never a rewrite."""
        if self.read_only:
            return {"type": "held", "block": "",
                    "reason": "This manuscript is open read-only, so the log is not written."}
        chat.append(self.log, {"id": str(chat_id), "kind": "state", "state": "done"})
        return {"type": "state", "block": "", "id": str(chat_id),
                "state": "done", "at": chat.now()}

    async def on_chat(self, block_id: str, body: str, check: str = "") -> dict:
        if self.read_only:
            return {"type": "held", "block": block_id,
                    "reason": "This manuscript is open read-only, so the comment log is not written either."}
        block = self.build.by_id.get(block_id)
        rec = chat.append(
            self.log,
            {
                "id": chat.next_id(self.log),
                "kind": "comment",
                "block": block_id,
                "doc": self.doc,
                **({"check": str(check)} if check else {}),
                "file": str(block.file) if block else "",
                "lines": [block.line_start, block.line_end] if block else [],
                "quote": (block.source_text[:120] if block else ""),
                "body": body,
                "author": "bb",
            },
        )
        return {
            "type": "chat",
            "block": block_id,
            "message": {
                "id": rec["id"], "who": "bb", "body": body,
                "ts": rec["ts"], "state": "queued",
            },
        }


# --------------------------------------------------------------- the diff


_TAG_OPEN_RE = re.compile(r"<([a-zA-Z][-\w]*)")


def _element_end(html: str, start: int) -> int | None:
    """Index just past the element opening at `start`.

    A small scan rather than a parser: read the tag name, then count the same
    name in and out. Returns None when `start` is not an element.
    """
    tag = _TAG_OPEN_RE.match(html, start)
    if not tag:
        return None
    name = tag.group(1)
    open_end = html.find(">", start)
    if open_end < 0:
        return None
    if html[open_end - 1] == "/":
        return open_end + 1

    depth, i = 1, open_end + 1
    pat = re.compile(rf"<(/?){re.escape(name)}\b", re.I)
    while depth and i < len(html):
        nxt = pat.search(html, i)
        if not nxt:
            return len(html)
        depth += -1 if nxt.group(1) else 1
        i = html.find(">", nxt.end())
        if i < 0:
            return len(html)
        i += 1
    return i


def block_html(html: str, block_id: str) -> str | None:
    """Everything the block rendered as, not only the element carrying its anchor.

    Most blocks are one element and this returns exactly that. Some are SEVERAL:
    the front matter renders as a title, a byline, an abstract label, the abstract
    itself and a keywords line, with the anchor on the first of them. Returning
    only the anchored element meant a patch replaced the title and left the
    abstract as it was, so an author editing the abstract watched the manuscript
    ignore them while every ordinary paragraph updated live (reported 2026-07-26).

    The run ends at the next element that carries an anchor of its own, which is
    where the next block starts, or at the closing tag of the container.
    """
    m = re.search(r'data-mx="' + re.escape(block_id) + r'"', html)
    if not m:
        return None
    start = html.rfind("<", 0, m.start())
    if start < 0:
        return None
    end = _element_end(html, start)
    if end is None:
        return None

    while True:
        nxt = html.find("<", end)
        if nxt < 0 or html.startswith("</", nxt):
            break                                   # the container closes here
        nend = _element_end(html, nxt)
        if nend is None or 'data-mx="' in html[nxt:nend]:
            break                                   # the next block starts here
        end = nend
    return html[start:end]


def _diff(old, new) -> dict | None:
    """What changed between two builds, expressed as the viewer's patch frame.

    Ids are derived from content, so EDITING A BLOCK CHANGES ITS ID. Comparing
    id sets directly would therefore report every edit as a delete plus an
    insert, and orphan the draft and the chat keyed to the old id, which is the
    one thing the whole anchoring design exists to prevent. `rematch` is what
    maps an old id onto the block it became, so the diff goes through it and the
    rename travels to the client.
    """
    from manuscriptor.source import blocks as _blocks

    mapping = _blocks.rematch(old.blocks, new.blocks)
    old_src = {b.id: b.source_text for b in old.blocks}
    new_by_id = {b.id: b for b in new.blocks}

    changed: list[str] = []
    renamed: dict[str, str] = {}
    for old_id, new_id in mapping.items():
        if new_id is None:
            continue
        if old_src.get(old_id) != new_by_id[new_id].source_text:
            changed.append(new_id)
        if new_id != old_id:
            renamed[old_id] = new_id

    claimed = {v for v in mapping.values() if v}
    added = sorted(set(new_by_id) - claimed)
    removed = sorted(k for k, v in mapping.items() if v is None)
    if not (changed or added or removed or renamed):
        return None

    html = new.blob["html"]
    frag = {}
    for bid in changed + added:
        piece = block_html(html, bid)
        if piece is not None:
            frag[bid] = piece
    return {
        "type": "patch",
        "blocks": frag,
        "added": added,
        "removed": removed,
        # An edit changes a block's id. The client must carry its draft, its
        # chat and its scroll anchor across, or the author's own save looks to
        # them like their comment vanished.
        "renamed": renamed,
        # Metadata moves with the markup, or the inspector shows a stale source
        # for a block the page has just redrawn.
        "blockdata": {bid: new.blob["blocks"][bid] for bid in frag if bid in new.blob["blocks"]},
    }


# --------------------------------------------------------------- the app


def _page(session: Session) -> str:
    tpl = resources.files("manuscriptor.templates").joinpath("index.html.j2").read_text(encoding="utf-8")
    css = resources.files("manuscriptor.templates.static").joinpath("styles.css").read_text(encoding="utf-8")
    js = resources.files("manuscriptor.templates.static").joinpath("viewer.js").read_text(encoding="utf-8")
    return Template(tpl).render(ms=session.blob, styles_css=css, viewer_js=js, extensions=_extensions())


def make_app(session: Session) -> web.Application:
    app = web.Application()

    async def index(request):
        # `?main=latex/main.tex` serves a document from anywhere in the tree,
        # named by its path relative to the served root. The switch validates
        # against the project's own offer and rebuilds under the lock, so a
        # watcher rebuild cannot interleave with it.
        wanted = request.query.get("main", "")
        if wanted and wanted != session.current_ref:
            async with session.lock:
                try:
                    await asyncio.to_thread(session.switch, wanted)
                except ValueError as exc:
                    return web.Response(text=str(exc), status=404)
        return web.Response(text=_page(session), content_type="text/html")

    async def ws_handler(request):
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        session.clients.add(ws)
        try:
            async for msg in ws:
                if msg.type is not WSMsgType.TEXT:
                    continue
                try:
                    data = json.loads(msg.data)
                except json.JSONDecodeError:
                    continue
                kind = data.get("type")
                if kind == "edit":
                    await ws.send_json(await session.on_edit(data.get("block", ""), data.get("source", "")))
                elif kind == "draft":
                    # Unsaved text, parked on disk. Not a save: it does not touch
                    # the manuscript, and it is what survives the window closing.
                    await ws.send_json(
                        session.keep_draft(data.get("block", ""), data.get("source", "")))
                elif kind == "chat":
                    await session.broadcast(await session.on_chat(
                        data.get("block", ""), data.get("body", ""),
                        check=data.get("check", "")))
                elif kind == "dismiss":
                    frame = await session.on_dismiss(data.get("id", ""))
                    await (ws.send_json(frame) if frame["type"] == "held"
                           else session.broadcast(frame))
                elif kind == "todo":
                    frame = await session.on_todo(data.get("text", ""))
                    await (ws.send_json(frame) if frame["type"] == "held"
                           else session.broadcast(frame))
                elif kind == "todo_toggle":
                    frame = await session.on_todo_toggle(data.get("id", ""), data.get("done"))
                    await (ws.send_json(frame) if frame["type"] == "held"
                           else session.broadcast(frame))
        finally:
            session.clients.discard(ws)
        return ws

    async def import_handler(request):
        """Outside markup, coming in. One route, three actions.

        A POST for all three because two of them write, and a marked-up file
        arrives as bytes rather than as a path: the author picks a referee report
        in a file dialog, and a server that asked him to type its path would be
        asking him to do the browser's job. Nothing is written to the manuscript
        directory but the comment log; the upload is parsed in memory and
        dropped.

        The page is not told the result over the websocket. The log grows, the
        watcher notices, and every attached client gets the same `chat`, `state`
        and `queue` frames a comment typed by hand produces -- which is the whole
        claim of this feature, that an imported comment is an ordinary one.
        """
        from manuscriptor.server import importer

        blocks = session.build.blocks if session.build is not None else ()

        if request.content_type.startswith("multipart/"):
            if session.read_only:
                return web.json_response(
                    {"error": "This manuscript is open read-only, so the comment log is not written."},
                    status=403)
            reader = await request.multipart()
            part = await reader.next()
            while part is not None and part.name != "file":
                part = await reader.next()
            if part is None:
                return web.json_response({"error": "no file in the upload"}, status=400)
            name = part.filename or "markup"
            data = await part.read(decode=False)
            try:
                report = await asyncio.to_thread(
                    importer.ingest, data, name, blocks=blocks, log=session.log,
                    doc=session.doc)
            except importer.Unreadable as exc:
                return web.json_response({"error": str(exc)}, status=415)
            return web.json_response(dict(report, tray=importer.tray(session.log, blocks, doc=session.doc)))

        try:
            data = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"error": "expected JSON or a multipart upload"}, status=400)

        action = data.get("action")
        if action == "place":
            if session.read_only:
                return web.json_response(
                    {"error": "This manuscript is open read-only, so the comment log is not written."},
                    status=403)
            try:
                placed = importer.place_mark(
                    session.log, data.get("import", ""), data.get("block", ""),
                    blocks=blocks, doc=session.doc)
            except (KeyError, ValueError) as exc:
                return web.json_response({"error": str(exc)}, status=400)
            return web.json_response(
                dict(placed, tray=importer.tray(session.log, blocks, doc=session.doc),
                     marks=importer.anchored_marks(session.log, blocks, doc=session.doc)))

        return web.json_response({
            "tray": importer.tray(session.log, blocks, doc=session.doc),
            "marks": importer.anchored_marks(session.log, blocks, doc=session.doc),
            "read_only": session.read_only,
        })

    # The evidence pass, triggered from the toolbar. The pass itself is the
    # CLI command (its extract stage calls a model, which nothing in server/
    # may do); the server spawns it as a subprocess exactly as a compile
    # spawns latex, streams its stdout over the websocket, and re-reads the
    # files it wrote when it finishes. One at a time: a second click while a
    # run is live would race the first over the same record files.
    evidence_running: dict = {"proc": None}

    async def _finish_evidence(rc: int):
        async with session.lock:
            try:
                session.rebuild()
            except Exception:
                pass  # a manuscript mid-edit may not render; the watcher will
        await session.broadcast({"type": "cites",
                                 "cites": session.blob.get("cites", {})})
        await session.broadcast({"type": "evidence", "done": True, "ok": rc == 0,
                                 "missing": session.blob.get("missing_fulltexts", 0)})

    async def _spawn_stream(argv: list, on_exit) -> None:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            cwd=str(session.root),
        )
        evidence_running["proc"] = proc

        async def pump():
            async for raw in proc.stdout:
                line = raw.decode("utf-8", errors="replace").rstrip()
                if line:
                    await session.broadcast({"type": "evidence", "line": line})
            await on_exit(await proc.wait())

        asyncio.ensure_future(pump())

    def _busy():
        live = evidence_running["proc"]
        return live is not None and live.returncode is None

    async def evidence_handler(_request):
        import sys as _sys

        if session.read_only:
            return web.json_response(
                {"error": "This manuscript is open read-only; the evidence pass writes records."},
                status=403)
        if _busy():
            return web.json_response({"error": "a run is already underway"}, status=409)
        await _spawn_stream(
            [_sys.executable, "-m", "manuscriptor.cli", "evidence",
             str(session.root), "--main", session.doc],
            _finish_evidence,
        )
        return web.json_response({"started": True})

    async def repair_handler(_request):
        """Fetch the missing PDFs into Zotero, then re-run the evidence pass.

        The ONLY route that leads to a write of the author's reference
        library, which is why it exists as its own click rather than as part
        of a run: a routine build must never mutate Zotero as a side effect.
        The re-run afterwards is read-only and cheap (everything found before
        is cached), and it is what upgrades the underlines.
        """
        import sys as _sys

        if session.read_only:
            return web.json_response(
                {"error": "This manuscript is open read-only; repair writes your Zotero library."},
                status=403)
        if _busy():
            return web.json_response({"error": "a run is already underway"}, status=409)
        out = session.root / "build" / "manuscriptor"
        if not (out / "missing.json").exists():
            return web.json_response({"error": "nothing to repair; run the evidence pass first"},
                                     status=409)

        async def then_rerun(rc: int):
            if rc != 0:
                await session.broadcast({"type": "evidence", "done": True, "ok": False,
                                         "missing": session.blob.get("missing_fulltexts", 0)})
                return
            await session.broadcast({"type": "evidence",
                                     "line": "repair finished; re-running the evidence pass"})
            await _spawn_stream(
                [_sys.executable, "-m", "manuscriptor.cli", "evidence",
                 str(session.root), "--main", session.doc],
                _finish_evidence,
            )

        await _spawn_stream(
            [_sys.executable, "-m", "manuscriptor.cli", "repair", str(out)],
            then_rerun,
        )
        return web.json_response({"started": True})

    app.router.add_get("/", index)
    app.router.add_get("/ws", ws_handler)
    app.router.add_post("/import", import_handler)
    app.router.add_post("/evidence", evidence_handler)
    app.router.add_post("/repair", repair_handler)
    # A compile is a subprocess, so it is the server's to run. Progress goes
    # back over the websocket above, not down a second channel.
    app.router.add_post("/compile", compile_mod.route(session))
    # An insertion is a coordinated write across three or four files, so it is a
    # request with a body and an answer rather than a frame. The block write
    # inside it still goes through splice, like every other write.
    app.router.add_post("/insert", insert_mod.route(session))

    # The build assets (rasterized figures, copied images) are served from the
    # CURRENT document's build directory, not a fixed mount: switching to a
    # document in another folder moves its `build/manuscriptor` with it, and a
    # static mount frozen at app creation would 404 every figure. Resolved per
    # request against `session.root`, with the same traversal guard `add_static`
    # gave us -- a path escaping the build directory is refused, not served.
    # Registered last so the explicit routes above always win.
    async def assets(request):
        rel = request.match_info.get("path", "")
        base = (session.root / "build" / "manuscriptor").resolve()
        target = (base / rel).resolve()
        if base not in target.parents and target != base:
            raise web.HTTPNotFound()
        if not target.is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(target)

    app.router.add_get("/{path:.*}", assets)
    return app


def serve(
    manuscript_dir: Path,
    *,
    port: int | None = None,
    open_window: bool = True,
    main: str | None = None,
    bib: str | None = None,
    read_only: bool = False,
    on_switch=None,
) -> None:
    from manuscriptor.server.watch import watch_tree

    session = Session(manuscript_dir, main=main, bib=bib, read_only=read_only,
                      on_switch=on_switch)
    app = make_app(session)

    # A manuscript keeps its own port (server/ports.py). The browser keys storage
    # by origin, so an ephemeral port threw away the drafts and the colour
    # preference on every launch, and took the port down with a dying server so
    # the page's retry loop had nothing to reconnect to. `--port 0` still asks
    # for an ephemeral one explicitly.
    want = ports.choose_port(manuscript_dir) if port is None else port
    if port is None and want == 0:
        print("the usual port for this manuscript is taken; using a temporary one, "
              "so drafts in the browser and the colour preference will not carry over")

    async def run():
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", want)
        await site.start()
        actual = site._server.sockets[0].getsockname()[1]
        url = f"http://127.0.0.1:{actual}/"

        loop = asyncio.get_running_loop()
        def changed(paths):
            from manuscriptor.server.watch import ASSET_SUFFIXES

            # The comment log is not source. Re-rendering the manuscript because
            # a comment arrived would be a second of work to repaint a pin. And
            # a figure-only change is an asset refresh, not a re-render diff:
            # the LaTeX did not move, only the image behind it.
            log_only = all(p.name == "comments.jsonl" for p in paths)
            assets_only = not log_only and all(
                p.suffix.lower() in ASSET_SUFFIXES for p in paths)
            coro = (session.on_log_change() if log_only
                    else session.on_assets_change() if assets_only
                    else session.on_change())
            asyncio.run_coroutine_threadsafe(coro, loop)

        stop = watch_tree(session.dir, changed)
        b = session.build.blob
        print(f"manuscriptor  {url}" + ("   [read-only]" if read_only else ""))
        print(f"  {len(b['blocks'])} blocks · {b['stats']['files']} files · "
              f"{b['stats']['cites']} citations · {b['stats']['values']} computed values")
        diag = b.get("diagnostics", {})
        for key, label in (("unanchored", "unanchored blocks"),
                           ("unresolved_refs", "unresolved refs"),
                           ("missing_includes", "missing includes"),
                           ("tikz_failed", "tikz figures not rendered")):
            if diag.get(key):
                print(f"  {len(diag[key])} {label}")
        if open_window:
            webbrowser.open(url)
        try:
            await asyncio.Event().wait()
        finally:
            stop()
            await runner.cleanup()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
