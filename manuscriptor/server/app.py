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
from manuscriptor.source import insert as insert_mod
from manuscriptor.source import splice as splice_mod

HOLDER = "author"


class Session:
    """One manuscript, its current build, and the clients watching it."""

    def __init__(self, manuscript_dir: Path, *, main: str | None = None,
                 bib: str | None = None, read_only: bool = False):
        self.dir = Path(manuscript_dir).resolve()
        self.main = main
        self.bib = bib
        self.read_only = read_only
        self.log = self.dir / "comments.jsonl"
        self.clients: set[web.WebSocketResponse] = set()
        self.lock = asyncio.Lock()
        self.seen_chats: dict[str, dict] = {}
        # The queue as it was last pushed, reduced to its identity so a repaint
        # is triggered by work moving and not by a second passing.
        self.seen_queue: list[tuple] = []
        self.build = None
        self.rebuild()

    @property
    def blob(self) -> dict:
        return self.build.blob

    def rebuild(self):
        previous = self.build
        self.build = build_mod.build(self.dir, main=self.main, bib=self.bib)
        return previous

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
        if patch:
            await self.broadcast(patch)

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
            chat.by_block(self.log), self.build.blocks, chat.read_chats(self.log)
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
                    "type": "state", "block": msg["block"], "state": msg["state"],
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
            self.log, self.build.blocks, anchored=anchored, root=self.dir
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
                self.log, self.build.blocks, anchored=anchored, root=self.dir
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
                splice_mod.splice, block, source, root=self.dir, holder=HOLDER
            )
        except splice_mod.NotEditable as exc:
            return {"type": "held", "block": block_id, "reason": str(exc)}
        except splice_mod.StaleBlock as exc:
            return {"type": "held", "block": block_id, "reason": str(exc)}
        except splice_mod.BlockLocked as exc:
            return {"type": "held", "block": block_id, "reason": str(exc)}
        return {"type": "saved", "block": block_id, "at": chat.now()}

    async def on_chat(self, block_id: str, body: str) -> dict:
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


def block_html(html: str, block_id: str) -> str | None:
    """The outer HTML of the element carrying `data-mx="<block_id>"`.

    A small scan rather than a parser: find the attribute, walk back to its
    tag's `<`, then forward counting the same tag name in and out.
    """
    m = re.search(r'data-mx="' + re.escape(block_id) + r'"', html)
    if not m:
        return None
    start = html.rfind("<", 0, m.start())
    if start < 0:
        return None
    tag = _TAG_OPEN_RE.match(html, start)
    if not tag:
        return None
    name = tag.group(1)
    open_end = html.find(">", m.end())
    if open_end < 0:
        return None
    if html[open_end - 1] == "/":
        return html[start : open_end + 1]

    depth, i = 1, open_end + 1
    pat = re.compile(rf"<(/?){re.escape(name)}\b", re.I)
    while depth and i < len(html):
        nxt = pat.search(html, i)
        if not nxt:
            return html[start:]
        depth += -1 if nxt.group(1) else 1
        i = html.find(">", nxt.end())
        if i < 0:
            return html[start:]
        i += 1
    return html[start:i]


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

    async def index(_request):
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
                elif kind == "chat":
                    await session.broadcast(await session.on_chat(data.get("block", ""), data.get("body", "")))
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
                    importer.ingest, data, name, blocks=blocks, log=session.log)
            except importer.Unreadable as exc:
                return web.json_response({"error": str(exc)}, status=415)
            return web.json_response(dict(report, tray=importer.tray(session.log, blocks)))

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
                    session.log, data.get("import", ""), data.get("block", ""), blocks=blocks)
            except (KeyError, ValueError) as exc:
                return web.json_response({"error": str(exc)}, status=400)
            return web.json_response(
                dict(placed, tray=importer.tray(session.log, blocks),
                     marks=importer.anchored_marks(session.log, blocks)))

        return web.json_response({
            "tray": importer.tray(session.log, blocks),
            "marks": importer.anchored_marks(session.log, blocks),
            "read_only": session.read_only,
        })

    app.router.add_get("/", index)
    app.router.add_get("/ws", ws_handler)
    app.router.add_post("/import", import_handler)
    # A compile is a subprocess, so it is the server's to run. Progress goes
    # back over the websocket above, not down a second channel.
    app.router.add_post("/compile", compile_mod.route(session))
    # An insertion is a coordinated write across three or four files, so it is a
    # request with a body and an answer rather than a frame. The block write
    # inside it still goes through splice, like every other write.
    app.router.add_post("/insert", insert_mod.route(session))

    out = session.dir / "build" / "manuscriptor"
    if out.is_dir():
        app.router.add_static("/", out, show_index=False, follow_symlinks=False)
    return app


def serve(
    manuscript_dir: Path,
    *,
    port: int = 0,
    open_window: bool = True,
    main: str | None = None,
    bib: str | None = None,
    read_only: bool = False,
) -> None:
    from manuscriptor.server.watch import watch_tree

    session = Session(manuscript_dir, main=main, bib=bib, read_only=read_only)
    app = make_app(session)

    async def run():
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", port)
        await site.start()
        actual = site._server.sockets[0].getsockname()[1]
        url = f"http://127.0.0.1:{actual}/"

        loop = asyncio.get_running_loop()
        def changed(paths):
            # The comment log is not source. Re-rendering the manuscript because
            # a comment arrived would be a second of work to repaint a pin.
            log_only = all(p.name == "comments.jsonl" for p in paths)
            coro = session.on_log_change() if log_only else session.on_change()
            asyncio.run_coroutine_threadsafe(coro, loop)

        stop = watch_tree(session.dir, changed)
        b = session.build.blob
        print(f"manuscriptor  {url}" + ("   [read-only]" if read_only else ""))
        print(f"  {len(b['blocks'])} blocks · {b['stats']['files']} files · "
              f"{b['stats']['cites']} citations · {b['stats']['values']} computed values")
        diag = b.get("diagnostics", {})
        for key, label in (("unanchored", "unanchored blocks"),
                           ("unresolved_refs", "unresolved refs"),
                           ("missing_includes", "missing includes")):
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
