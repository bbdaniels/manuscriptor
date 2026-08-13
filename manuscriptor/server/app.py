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
import hashlib
import json
import mimetypes
import re
import traceback
import webbrowser
from importlib import resources
from pathlib import Path

from aiohttp import WSMsgType, web
from jinja2 import Template

from manuscriptor.templates.ext import load as _extensions

# The assets route re-stages one file at a time through the SAME pass the build
# stages them with; see `refresh_asset`. A build only ever refreshes the figures
# of the document it rendered, so the route is where the question gets asked for
# every other document sharing the cache.
from manuscriptor.render import postprocess
from manuscriptor.server import build as build_mod
from manuscriptor.server import chat
from manuscriptor.server import compile as compile_mod
from manuscriptor.server import drafts as drafts_mod
from manuscriptor.server import links as links_mod
from manuscriptor.server import migrate as migrate_mod
from manuscriptor.server import paths
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
                 auto_compile: bool = True, on_switch=None):
        self.dir = Path(manuscript_dir).resolve()
        self.bib = bib
        self.read_only = read_only
        # Whether a stale set of cross-reference numbers may compile itself.
        # ON by default: the author of a fast manuscript should never have to
        # press a button to see a number LaTeX already knows. `--no-auto-compile`
        # is for the paper that takes minutes, where a background run is a cost
        # rather than a convenience.
        self.auto_compile = auto_compile
        # The numbering signature of the last auto-compile ATTEMPTED, successful
        # or not. In memory on purpose: it records what this server has already
        # tried, and a `\ref{typo}` that no compile can satisfy must be attempted
        # once and then left alone until the source moves.
        self.auto_sig = None
        # Fired with the new document root whenever a switch moves it, so the
        # caller (the drain agent in cmd_serve) can follow the current document.
        self._on_switch = on_switch
        self.clients: set[web.WebSocketResponse] = set()
        self.lock = asyncio.Lock()
        self.seen_chats: dict[str, dict] = {}
        # The queue as it was last pushed, reduced to its identity so a repaint
        # is triggered by work moving and not by a second passing.
        self.seen_queue: list[tuple] = []
        # The threaded history as last pushed. Compared whole rather than by an
        # identity tuple: a work item changes by GAINING A LINE, which no
        # summary of its ids and states can see.
        self.seen_history: list[dict] | None = None
        # What the clients have been told about the state derived from the build
        # rather than from any one block: the evidence verdicts, and the header
        # counts. Seeded below from the first build, because a page is BORN
        # holding these -- pushing them again on the first change would be a
        # repaint of something already correct.
        self.seen_derived: dict[str, dict] = {}
        # Every editable document in the tree, and the one we open on. An
        # explicit --main names a file in the served directory itself; otherwise
        # the first discovered document is the default, so opening a top-level
        # project never has to resolve "the one manuscript" and never errors.
        self.docs = tree_mod.discover(self.dir)
        self.current = self._default_current(main)
        self.build = None
        # Roots already moved into the hidden layout this session, and the
        # running account of what was moved, for the terminal and the page.
        self._migrated: set[Path] = set()
        self.notices: list[str] = []
        # Assets the page asked for that this build's cache does not hold, in
        # the order they were first asked for. Serve time, not build time: the
        # build that produced the open page was self-consistent when it ran.
        self.asset_misses: list[str] = []
        # Assets the page asked for whose SOURCE is still in the manuscript --
        # nothing staged them here, which is a different fact and a different
        # instruction. See `note_asset_miss`.
        self.unstageable_assets: list[str] = []
        # Tasks started by `spawn` and awaited by nobody. Held because asyncio
        # keeps only a weak reference to a running task, so an untracked one can
        # be collected mid-run and simply stop -- the same reason
        # `compile._RUNNING` exists.
        self._spawned: set = set()
        self.rebuild()
        self.seen_derived = self._derived()
        # A page is BORN holding the history, the same way it is born holding
        # the counts: pushing it again on the first watcher event would repaint
        # something already correct.
        self.seen_history = self.blob.get("history")

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
        """The current document's comment log, inside the hidden directory."""
        return paths.comments(self.root)

    @property
    def doc(self) -> str:
        """The document being served, by name. Chats are scoped to it."""
        return self.build.main_tex.name if self.build is not None else ""

    @property
    def drafts_file(self) -> Path:
        """Unsaved text, on disk. Durable but private, so it sits beside the log
        in the hidden directory and outside the cache the `clean` command may
        remove: no rebuild can reconstruct a paragraph the author never saved."""
        return paths.drafts(self.root)

    @property
    def feed_file(self) -> Path:
        """The drain's live feed, which this session reads and never writes.

        A property beside `log` and `drafts_file` rather than a path built at
        each use, because it was built at each use and the two uses disagreed:
        `on_feed_change` read `paths.agent_dir`, and `serve` armed its watcher
        on `build/manuscriptor`, the pre-2026-07-27 layout. The frozen file
        never fired, so the handler that had the right path was never called
        and the panel never moved. It also follows a document switch, which a
        path captured once at serve time cannot do.
        """
        from manuscriptor.server import feed as feed_mod

        return feed_mod.progress_path(paths.agent_dir(self.root))

    @property
    def asset_root(self) -> Path:
        """The cache this session's build wrote, and the assets route serves.

        Follows the read-only redirect, because that is where the rasters ARE:
        a read-only serve renders into a scratch directory under the system
        temp, and a route pointed at the hidden directory instead would 404
        every figure on a manuscript whose page renders perfectly.
        """
        return paths.cache(self.root, read_only=self.read_only)

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
        # Whatever was missing belonged to the build being replaced. This one
        # re-rasterizes and re-copies, so the account starts empty and refills
        # only if the page asks for something this cache still does not hold.
        self.asset_misses = []
        self.unstageable_assets = []
        target = self.dir if self.current is None else Path(self.current.root_dir)
        self._migrate(target)
        if self.current is None:
            self.build = build_mod.build(self.dir, main=None, bib=self.bib,
                                         read_only=self.read_only)
        else:
            self.build = build_mod.build(
                target, main=self.current.main, bib=self.bib,
                read_only=self.read_only)
        self._overlay_tree_docs()
        return previous

    def _migrate(self, root: Path) -> None:
        """Move a pre-2026-07-27 manuscript into `.manuscriptor/`, once.

        Here rather than in `build()` because this is the only place that knows
        whether the serve is read-only, and migrating a manuscript the author
        opened to READ would break the promise that nothing reaches the disk.
        Here rather than in `cmd_serve` because a document switch reaches a root
        the command line never named, and that root needs moving too.

        Never silent: a migration that says nothing is indistinguishable from
        files going missing.
        """
        if self.read_only or root in self._migrated:
            return
        self._migrated.add(root)
        try:
            report = migrate_mod.run(root)
        except OSError as exc:
            self.notices.append(f"could not reorganize {root}: {exc}")
            return
        if not report:
            return
        self.notices.append(f"{root.name}: {report.summary()}")
        for src, why in report.skipped:
            self.notices.append(f"  left {src.name} where it was: {why}")
        for line in self.notices[-(1 + len(report.skipped)):]:
            print(line, flush=True)

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

    def spawn(self, coro, what: str, *, loop=None):
        """Start a coroutine nobody will await, and report it if it fails.

        THE ONE LAUNCHER FOR EVERYTHING THE WATCHERS START. `run_coroutine_
        threadsafe` hands back a `concurrent.futures.Future`, and unlike an
        `asyncio` one it does not log the exception it is left holding when
        nobody retrieves it -- so for as long as the watcher called it bare,
        every failure in a rebuild, an assets refresh or a history push was
        discarded in complete silence. The observed shape on qutub-ayush: the
        author deleted a word, the splice reached disk, the server rebuilt, its
        own page said the new text, the websocket stayed open, and the paragraph
        on screen never changed. Nothing was written to any log, because there
        was nothing anywhere whose job was to write it.

        Callers hand a coroutine and the words for what it was doing, and that
        is the whole contract; `fail` decides what saying so looks like.
        """
        import concurrent.futures

        if loop is None:
            fut = asyncio.get_running_loop().create_task(coro)
            self._spawned.add(fut)
            fut.add_done_callback(self._spawned.discard)
        else:
            fut = asyncio.run_coroutine_threadsafe(coro, loop)

        def report(f):
            try:
                exc = f.exception()
            except (asyncio.CancelledError, concurrent.futures.CancelledError):
                return
            if exc is not None:
                # Back onto the loop: `fail` broadcasts, and this callback may
                # be running on the watcher's own thread.
                asyncio.run_coroutine_threadsafe(
                    self.fail(what, exc), loop or asyncio.get_event_loop())

        fut.add_done_callback(report)
        return fut

    async def fail(self, what: str, exc: BaseException) -> None:
        """Say out loud that something the page was waiting for did not happen.

        BOTH CHANNELS, EVERY TIME. The page gets a frame because the author is
        looking at the page, and stderr gets the traceback because the author
        is not the one who can fix it. Reporting to only one of them is how a
        redraw that never arrived came to look exactly like a redraw that was
        never needed.
        """
        print(f"manuscriptor: {what}: {exc}", flush=True)
        traceback.print_exception(type(exc), exc, exc.__traceback__)
        try:
            await self.broadcast({"type": "error", "message": f"{what}: {exc}"[:400]})
        except Exception:                       # the socket is not the point
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

    # The state a page holds that belongs to no block, and therefore to no patch.
    # Both are recomputed on EVERY rebuild and neither was ever pushed by one:
    # `cites` went out only when the evidence pass finished, `stats` went out
    # never. So a tab was right at the moment it opened and frozen from then on
    # -- the header still said "1 citations" over a manuscript with two, and an
    # underline kept the colour it was born with whatever the records said. One
    # place decides when they move, so the evidence route and the watcher cannot
    # come to disagree about it.
    #
    # `diagnostics` joined them for the same reason and one more: an asset miss
    # is recorded while the page is already open, so no boot-time render can
    # carry it. Routing it through here rather than broadcasting from the route
    # keeps the "what changed" comparison in one place.
    DERIVED = ("cites", "stats", "diagnostics")

    def _derived(self) -> dict[str, dict]:
        # COPIES, ALL THE WAY DOWN. `seen_derived` is what was last pushed, and
        # holding a reference to the live object makes that record follow every
        # change made to it: the comparison in `push_derived` then finds one
        # dict equal to itself and pushes nothing, forever. A rebuild replaces
        # these wholesale so it never bit, and the first thing to change one IN
        # PLACE -- an asset miss -- was silent.
        #
        # `dict(...)` alone fixes only the outer layer. `cites` is a dict of
        # per-key records, so a verdict changed in place moves on both sides of
        # the comparison at once and the page is told nothing -- the same
        # failure, one level further in. Serialized rather than `deepcopy`d
        # because every one of these is JSON on its way to the socket: what is
        # compared is exactly what would be sent.
        if self.build is None:
            return {}
        return {k: json.loads(json.dumps(self.blob.get(k) or {}))
                for k in self.DERIVED}

    def note_asset_miss(self, rel: str, *, stageable: bool = True) -> bool:
        """Record an asset the page asked for and the cache does not hold.

        True when this is news. A page holding twenty broken images asks for all
        twenty on every reload, and one fact stated twenty times is noise.

        TWO ACCOUNTS, because they are two different pieces of news with two
        different instructions. A miss whose source is GONE from the manuscript
        is a figure renamed under a running server, and a restart fixes it. A
        miss whose source is still sitting on disk was never staged here at all:
        a PDF figure reaches the page as its raster, nothing rasterizes without
        poppler, and the author was being told nine assets were missing and to
        restart the server -- about nine files in his own figures directory,
        where the fix was installing poppler. Both are recorded; neither is
        allowed to speak in the other's words.
        """
        if self.build is None:
            return False
        seen = self.asset_misses if stageable else self.unstageable_assets
        if rel in seen:
            return False
        seen.append(rel)
        diag = self.blob.setdefault("diagnostics", {})
        diag["missing_assets"] = list(self.asset_misses)
        diag["unstageable_assets"] = list(self.unstageable_assets)
        return True

    async def push_derived(self) -> None:
        """Tell the clients about derived state that moved, and only that."""
        for kind, value in self._derived().items():
            if self.seen_derived.get(kind) == value:
                continue
            self.seen_derived[kind] = value
            await self.broadcast({"type": kind, kind: value})

    async def on_change(self, *, refresh_assets: bool = False) -> None:
        """A .tex file changed on disk. Re-render and push only what moved.

        `refresh_assets` when a figure changed in the same batch. A block is
        patched on a SOURCE difference, and a regenerated figure is not one: its
        LaTeX is identical and only the bytes behind the image moved, so the page
        keeps the raster the browser already has. That is how a rebuilt figure
        stayed stale on 2026-07-26 after the agent edited `main.tex` and
        regenerated three PDFs in one go: the batch held both kinds of change, the
        classification allowed only one, and the source path won.
        """
        async with self.lock:
            try:
                # OFF THE EVENT LOOP, ALWAYS. On a real manuscript this is half
                # a second of pandoc, hashing and file IO, and called inline it
                # stops the entire server for that whole stretch -- no `saved`
                # ack, no frame, no image, no route. The lock still serializes
                # it against the other writers, so nothing about the ordering
                # changes; only the thread it burns does. A page with no
                # figures never noticed, because it asks the server for nothing
                # while the author types. qutub-ayush's page asks for eight.
                previous = await asyncio.to_thread(self.rebuild)
            except Exception as exc:  # a manuscript mid-edit may not render
                await self.broadcast({"type": "error", "message": str(exc)[:400]})
                return
            # ANYTHING BELOW THIS LINE THAT RAISES IS REPORTED BY `spawn`, which
            # is what starts this coroutine, and it is reported nowhere else.
            # For as long as the watcher started it with a bare
            # `run_coroutine_threadsafe` there was no reporter at all: nobody
            # retrieves that future, and a `concurrent.futures.Future` -- unlike
            # an `asyncio` one -- never logs the exception it ends up holding.
            # So a failure here left the build advanced, the socket open, the
            # page still showing the paragraph the author had just rewritten,
            # and no line of output anywhere saying why.
            patch = _diff(previous, self.build) if previous else None
            # Ids are content-derived, so this rebuild renamed every block it
            # changed. A stored draft left under an old id is a draft nobody
            # will ever be offered again, which is the failure the store exists
            # to prevent, so it moves with the rename.
            if patch and patch.get("renamed") and not self.read_only:
                drafts_mod.rekey(self.drafts_file, patch["renamed"])
        if patch:
            await self.broadcast(patch)
        # After the patch and not with it: the counts and the verdicts move
        # whether or not any block did, which is the whole point of them being
        # their own frame. A `.bib` fixed on its own patches nothing and still
        # changes what the header says.
        await self.push_derived()
        if refresh_assets:
            # The src path is stable, so the version query is the only thing that
            # can force the browser past the copy it already has.
            await self.broadcast({"type": "assets", "v": chat.now()})
        # LAST, AND HERE RATHER THAN ANYWHERE ELSE. This is the one point every
        # source change converges on -- the watcher, a splice, the drain, an
        # import -- and the only one where a rebuild has just finished, so the
        # `.aux` the page is reading and the source it was read against are both
        # current. Deciding it in the watcher would be deciding it without the
        # build; deciding it in `rebuild()` would decide it inside the lock and
        # on a document switch as well. After the patch, never before it: the
        # author's text must reach his screen without waiting on LaTeX, and the
        # numbers follow when they are ready.
        await compile_mod.auto_compile(self)

    async def on_assets_change(self) -> None:
        """A figure changed on disk, with no source change beside it.

        The block diff cannot carry this: the LaTeX is untouched, so ids and
        sources match and the patch is empty, while the rasterized PNG in the
        build directory has silently gone stale. Rebuild (the `.sha` sidecar --
        NOT an mtime, as this said until 2026-08-05 -- re-rasterizes exactly the
        changed figures, and the copier refreshes direct images) and tell every
        client to refetch its images past the browser cache. This is how a
        regenerated figure reaches an open page.

        It reaches an open page on ANOTHER document by a different road: this
        rebuild renders the current document only, so it re-stages that
        document's figures and no others. Everything else in the shared cache is
        brought up to date by the assets route, one file at a time, as it is
        asked for.
        """
        async with self.lock:
            try:
                self.rebuild()
            except Exception as exc:
                await self.broadcast({"type": "error", "message": str(exc)[:400]})
                return
        await self.broadcast({"type": "assets", "v": chat.now()})

    @property
    def history_file(self) -> Path:
        """The drain's append-only ledger, which this session reads and never
        writes. Beside `feed_file` and derived the same way, so a document
        switch moves both together."""
        from manuscriptor.server import feed as feed_mod

        return feed_mod.history_path(paths.agent_dir(self.root))

    def _history(self) -> list[dict]:
        """The threaded history for the document being served.

        Both halves of the join are re-read here: the ledger, because the drain
        appends to it as it works, and the comment log, because that is where
        the outcome is. Scoped by `doc` -- the ledger is per directory since the
        drain session is, and this payload is per document.
        """
        from manuscriptor.server import feed as feed_mod

        if self.build is None:
            return []
        return build_mod.history_view(
            self.log, self.build.blocks, feed_mod.read_history(self.history_file),
            root=self.root, doc=self.doc)

    async def push_history(self) -> None:
        """Tell the clients what the agent has done, when it has changed.

        Called from BOTH watchers, because the row is a join of two files that
        move independently: the drain appends a line to the ledger, and the
        outcome that closes the row is a state record in the comment log.
        """
        if self.build is None:
            return
        fresh = self._history()
        self.build.blob["history"] = fresh
        if fresh == self.seen_history:
            return
        self.seen_history = fresh
        await self.broadcast({"type": "history", "history": fresh})

    async def on_feed_change(self) -> None:
        """Push what the drain is doing. The file is written by the drain; this
        only reads it, which is the whole of the server's relationship with
        Claude. Only differences go out, so a feed rewritten with the same
        contents does not repaint the panel."""
        from manuscriptor.server import feed as feed_mod

        if self.build is None:
            return
        fresh = feed_mod.read_feed(self.feed_file)
        if fresh != self.build.blob.get("agent_feed"):
            self.build.blob["agent_feed"] = fresh
            await self.broadcast({"type": "feed", "feed": fresh})
        # After the live frame and unconditionally: the ledger is appended to on
        # every entry while the ring is COALESCED, so the two files move at
        # different moments and an unchanged envelope does not mean an unchanged
        # history. `push_history` is what decides whether anything goes out.
        await self.push_history()

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
                    # WHAT WAS ASKED, because that is what the ticker line is
                    # about. Without it the line falls back to naming the block,
                    # and a block is named by its own first words, so the author
                    # watched his paragraph quote itself instead of telling him
                    # which of his two requests had been picked up. The seed
                    # (`ticker_view`) has always carried this; the live frame
                    # never did, so the line changed under him on every reload.
                    # Clipped by the server through the one clip both paths use.
                    "asked": build_mod.asked_of(msg.get("body")) or None,
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
        # The outcome half of a history row lives in this log, so a `done`
        # appended here is what turns a row from working into finished. Without
        # this the row would carry the agent's lines and then sit on `working`
        # until something else happened to make the feed move.
        await self.push_history()

    async def on_edit(self, block_id: str, source: str) -> dict:
        """Splice one block back to disk. The watcher handles the redraw.

        THE BLOCK IS RE-CUT FROM THE FILE BEFORE IT IS WRITTEN, never taken
        from the build as it stands. A build is a snapshot, and the only thing
        that advances it is the tree watcher -- a filesystem notification, which
        arrives late, arrives after a rebuild that takes a second on a real
        manuscript, and sometimes does not arrive at all. Between the splice and
        the rebuild the build describes a file that has already moved, and the
        page keeps sending the id it last heard, so the SECOND save of a run
        hands `splice` a byte range that is one save out of date.

        That is what wrote `... ALPHA. BETA BETA.` into a manuscript during
        ordinary typing on 2026-07-28: the block still existed, its text was
        still in the file as a prefix of the paragraph it had become, and the
        splice landed on the prefix. `splice` now refuses that -- but refusing
        the author's own keystrokes every other save is not a fix, so the range
        is derived from the file itself and the refusal is left to mean what it
        says, that somebody ELSE rewrote the paragraph.

        Ids are content-derived, so re-cutting renames the block the page is
        naming. `rematch` is what carries the page's id onto the block it has
        become; comparing ids across two cuts any other way is the bug this
        codebase has already shipped once.

        `cli.py` has always rebuilt immediately before splicing a stored draft.
        The server was the one writer working from a snapshot.
        """
        if self.read_only:
            return {"type": "held", "block": block_id,
                    "reason": "This manuscript is open read-only, so nothing here can write to it."}
        block = self.build.by_id.get(block_id)
        if block is None:
            return {"type": "held", "block": block_id, "reason": "unknown block"}
        try:
            block, following = await asyncio.to_thread(self._current_block, block)
        except Exception as exc:                    # a manuscript mid-edit
            return {"type": "held", "block": block_id, "reason": str(exc)[:400]}
        if block is None:
            return {"type": "held", "block": block_id,
                    "reason": "this paragraph is no longer in the manuscript, so there is "
                              "nothing to write it over"}
        try:
            await asyncio.to_thread(
                splice_mod.splice, block, source, root=self.root, holder=HOLDER,
                following=following,
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

    def _current_block(self, block):
        """`block` as the files on disk have it now, and what follows it there.

        The second half is for `splice`: an editor box can hold a passage the
        file has since split into several blocks, and only the blocks AFTER the
        target, in this same fresh cut, can say whether the save is still
        carrying them. `(None, ())` when the paragraph is gone.

        Blocks only, not a build: a save needs to know where a paragraph starts
        and ends, and paying pandoc for that would put a second of rendering in
        front of every keystroke pause. The rebuild that redraws the page still
        happens, once, when the watcher reports the write.

        NOTHING IS ACCEPTED ON ITS ID ALONE, and the file it lives in is why. A
        manuscript that repeats a stanza -- estonia-ecm's appendix opens six
        table files with the same two `\\newcolumntype` lines -- disambiguates
        the copies with a `-2`, `-3`, `-4` suffix counted in document order. Edit
        the fourth and it stops being a copy, so the fifth BECOMES `-4` in the
        next cut: the same id string, a different paragraph, in a different
        file. Following it wrote the author's edit into a table he had not
        opened.

        So the whole question is asked INSIDE THE BLOCK'S OWN FILE. `rematch`
        already refuses to pair two blocks across files on similarity; it is its
        exact-id pass that does not ask, and that pass is the one a renumber
        fools. Given only this file's blocks it cannot reach the wrong copy, and
        the renumbered stanza resolves on similarity to the paragraph it
        actually became -- which is what lets the author keep typing into a
        paragraph his manuscript happens to repeat, rather than being refused
        from his second keystroke onward.
        """
        from manuscriptor.source import blocks as _blocks

        fresh = build_mod.source_blocks(self.root, self.build.main_tex)
        here = tuple(b for b in fresh if b.file == block.file)
        def with_tail(b):
            return b, tuple(here[here.index(b) + 1:])

        # Untouched since the build: same name, same words, same file.
        for b in here:
            if b.id == block.id and b.source_text == block.source_text:
                return with_tail(b)
        # Otherwise it was rewritten -- by this page's own last save, most
        # likely -- and an edit renames its block, so follow it through the one
        # thing that maps ids across two cuts.
        was = tuple(b for b in self.build.blocks if b.file == block.file)
        moved = _blocks.rematch(was, here).get(block.id)
        hit = next((b for b in here if b.id == moved), None) if moved else None
        return with_tail(hit) if hit is not None else (None, ())

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
                               "text": text, "doc": self.doc, "author": chat.AUTHOR})
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
                # Long enough to identify this block and no other, so the chat
                # is re-found after the edit that answers it changes the id.
                "quote": (build_mod.quote_for(block, self.build.blocks) if block else ""),
                "body": body,
                "author": chat.AUTHOR,
            },
        )
        return {
            "type": "chat",
            "block": block_id,
            "message": {
                "id": rec["id"], "who": rec["author"], "body": body,
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



def classify(paths) -> tuple[str, bool]:
    """What a batch of changed files means: ("log" | "source" | "assets", assets_too).

    A batch can be BOTH kinds of change at once, and treating it as one choice was
    a real defect. On 2026-07-26 the drain moved a figure in `main.tex` and
    regenerated three PDFs in the same second; the batch was classified as source,
    the source path patches a block only when its LaTeX differs, and every figure
    whose LaTeX had not moved kept the raster the browser already had. The author
    asked for a new figure, the file on disk was new, and the page showed the old
    one. So this answers two questions rather than picking one answer.

    The comment log is not source: re-rendering a manuscript because a comment
    arrived would be a second of work to repaint a pin.
    """
    from manuscriptor.server.watch import ASSET_SUFFIXES

    paths = list(paths)
    if paths and all(p.name == "comments.jsonl" for p in paths):
        return "log", False
    assets = any(p.suffix.lower() in ASSET_SUFFIXES for p in paths)
    source = any(p.suffix.lower() not in ASSET_SUFFIXES and p.name != "comments.jsonl"
                 for p in paths)
    if source:
        return "source", assets
    return "assets", assets


def block_span(html: str, block_id: str) -> tuple[int, int] | None:
    """Where a block's markup starts and ends in the rendered document.

    Most blocks are one element and this returns exactly that. Some are SEVERAL:
    the front matter renders as a title, a byline, an abstract label, the abstract
    itself and a keywords line, with the anchor on the first of them. Returning
    only the anchored element meant a patch replaced the title and left the
    abstract as it was, so an author editing the abstract watched the manuscript
    ignore them while every ordinary paragraph updated live (reported 2026-07-26).

    The run ends at the next element that carries an anchor of its own, which is
    where the next block starts, or at the closing tag of the container.

    Separate from `block_html` because two callers want different things from the
    same answer -- one wants the markup, one wants to know what is NOT covered by
    any block -- and finding a block's run twice, in two functions, is how the
    two would come to disagree about where a block ends.
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
    return (start, end)


def block_html(html: str, block_id: str) -> str | None:
    """Everything the block rendered as, ready to be put on the page."""
    span = block_span(html, block_id)
    return None if span is None else html[span[0]:span[1]]


def _rendered(build) -> tuple[dict[str, str], str]:
    """What each block LOOKS like, and everything no block covers.

    The markup carries the block's own `data-mx`, and that is left in it. An id
    is derived from content, so a block cannot be renamed without its source
    moving -- `rematch` resolves a shifted duplicate suffix by id and never
    reports it as a rename -- which means the id in the value can only differ
    when the caller has already found a difference in the source. Stripping it
    first would be an unreachable branch dressed as a safeguard.

    The remainder is every byte of the document outside a block run. It should be
    whitespace -- a run absorbs the unanchored elements after it, so everything
    from the first anchor to the end of the container belongs to some block --
    and `tests/test_live_frames.py` holds it to that. It is returned rather than
    assumed because it is the one part of the document that no frame can address,
    and a silent stale patch of it is exactly the class of bug this comparison
    was written to end.
    """
    html = build.blob["html"]
    spans: list[tuple[int, int]] = []
    out: dict[str, str] = {}
    for b in build.blocks:
        span = block_span(html, b.id)
        if span is None:
            continue
        spans.append(span)
        out[b.id] = html[span[0]:span[1]]
    spans.sort()

    rest: list[str] = []
    at = 0
    for start, end in spans:
        if start > at:
            rest.append(html[at:start])
        at = max(at, end)
    rest.append(html[at:])
    return out, "".join(rest)


def _diff(old, new) -> dict | None:
    """What changed between two builds, expressed as the viewer's patch frame.

    Ids are derived from content, so EDITING A BLOCK CHANGES ITS ID. Comparing
    id sets directly would therefore report every edit as a delete plus an
    insert, and orphan the draft and the chat keyed to the old id, which is the
    one thing the whole anchoring design exists to prevent. `rematch` is what
    maps an old id onto the block it became, so the diff goes through it and the
    rename travels to the client.

    ONE RULE DECIDES WHETHER A BLOCK MOVED: anything the client holds about it.
    That is its markup OR its LaTeX, and it has to be both because the two come
    apart in each direction. This asked only about the LaTeX, and a block's
    render depends on inputs its own LaTeX never mentions -- the `.bib` behind
    a `\\citep`, the `.aux` behind a `\\ref`. The bibliography's source is the
    unchanging line `\\bibliography{references}`, so adding a citation patched
    the paragraph and left the reference list below it as it was, and a fix to
    a journal name in the `.bib` produced no frame AT ALL: the file is watched
    and the rebuild was right, and the whole of it was discarded in silence.
    The reverse also happens -- a whitespace or comment-only edit changes the
    LaTeX and not the render -- and the inspector's source editor would go
    stale on it, so the source comparison stays alongside rather than instead.
    """
    from manuscriptor.source import blocks as _blocks

    mapping = _blocks.rematch(old.blocks, new.blocks)
    old_src = {b.id: b.source_text for b in old.blocks}
    new_by_id = {b.id: b for b in new.blocks}
    old_render, old_rest = _rendered(old)
    new_render, new_rest = _rendered(new)

    changed: list[str] = []
    renamed: dict[str, str] = {}
    for old_id, new_id in mapping.items():
        if new_id is None:
            continue
        if (old_src.get(old_id) != new_by_id[new_id].source_text
                or old_render.get(old_id) != new_render.get(new_id)):
            changed.append(new_id)
        if new_id != old_id:
            renamed[old_id] = new_id

    # Nothing outside a block can be addressed by a frame, so if the document
    # moved OUT THERE the only honest answer is to redraw all of it. Compared on
    # its non-whitespace content, because the remainder is whitespace in every
    # document this renders and a repaint on a shifted newline would be the
    # "re-emit everything" failure the narrow patch exists to avoid.
    if "".join(old_rest.split()) != "".join(new_rest.split()):
        changed = [b.id for b in new.blocks]

    claimed = {v for v in mapping.values() if v}
    order = [b.id for b in new.blocks]
    at = {bid: i for i, bid in enumerate(order)}
    # Document order, not id order. Each new block is positioned after the one
    # before it, so a run of them has to arrive in the order the document reads
    # or the second lands before the first is on the page.
    added_ids = sorted(set(new_by_id) - claimed, key=lambda b: at[b])
    removed = sorted(k for k, v in mapping.items() if v is None)
    if not (changed or added_ids or removed or renamed):
        return None

    html = new.blob["html"]
    # An added block goes through `added` ALONE. `blocks` is applied by replacing
    # an element already on the page, and an id with no element there falls
    # through to an append at the end of the document -- which is how a figure,
    # moved and recaptioned so `rematch` could not map it, rendered below the
    # bibliography on 2026-07-27 while the source and the PDF both had it in place.
    frag = {}
    for bid in changed:
        piece = block_html(html, bid)
        if piece is not None:
            frag[bid] = piece

    # `after` is the id of the preceding block in the new document, or None when
    # this block is now the first thing in it.
    added = []
    for bid in added_ids:
        piece = block_html(html, bid)
        if piece is None:
            continue
        i = at[bid]
        added.append({
            "id": bid,
            "html": piece,
            "after": order[i - 1] if i else None,
            "block": new.blob["blocks"].get(bid),
        })
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


def _asset_identity(target: Path) -> tuple[str, bytes]:
    """A strong ETag for a staged asset, and the bytes it identifies.

    Derived from the CONTENT, because none of the other candidates says what the
    browser is holding. The filename carries no version, and the mtime is the
    source's own -- `mirror` copies with `copy2` on purpose, so the cache can
    tell staleness from a stat -- which makes new bytes under an older timestamp
    indistinguishable from no change at all.

    The raster's `.sha` sidecar answers it for free where it exists: it holds
    the digest of the PDF the picture was made from, and the picture is a
    deterministic function of those bytes. Everything else is hashed here, over
    the bytes already being read to answer the request, which is the same order
    of cost as the source hash `refresh_asset` takes on every request anyway.
    """
    body = target.read_bytes()
    sidecar = target.with_name(target.name + ".sha")
    try:
        digest = sidecar.read_text(encoding="utf-8").strip()
    except OSError:
        digest = ""
    if not digest:
        digest = hashlib.sha256(body).hexdigest()
    return f'"{len(body):x}-{digest}"', body


def _etag_matches(header: str, etag: str) -> bool:
    """RFC 7232 `If-None-Match`, strong comparison. `*` matches anything held."""
    candidates = [c.strip() for c in header.split(",") if c.strip()]
    if not candidates:
        return False
    return "*" in candidates or etag in candidates


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
        # Through the one derived-state push, not a `cites` broadcast of its
        # own. This route sending the verdicts and the watcher sending nothing
        # was two implementations of one job, and the half that was missing is
        # why an underline could only ever be recoloured by clicking the button.
        await session.push_derived()
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
        out = paths.cache(session.root)
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

    # One run at a time. Dedupe is decided by reading the log, so two runs
    # overlapping each other both read it before either has written, and every
    # finding is filed twice into a file that cannot be rewritten. A double
    # click is not an unusual way to use a menu.
    preflight_lock = asyncio.Lock()

    async def preflight_handler(_request):
        """The built-in preflight, filed as review comments on the open page.

        Unlike the evidence pass this spawns nothing and streams nothing: the
        checks are deterministic Python in this package, they call no model, and
        they finish in a tenth of a second on a real manuscript, so the click
        answers with the count rather than with a promise. In a thread all the
        same, because the scripts sweep walks the whole repository and the event
        loop is also serving the page.

        Every document in the directory, not only the one being served: the
        submission this module was written off had a supplement built by a
        different rule, and checking the paper alone reported it healthy. A
        finding is filed against its own document, so it surfaces when that
        document is.

        The findings go into `comments.jsonl` and the page learns through
        `on_log_change`, the same path the drain's own records travel. Nothing
        here knows what a check found; it only says how many were new, and it
        SAYS SO EVEN WHEN THERE WERE NONE, because silence read as success is
        the failure the whole module exists to prevent.
        """
        if session.read_only:
            # Says what did NOT happen. The refusal used to read "findings are
            # written to the comment log", which is a description of the thing
            # it is refusing to do and reads on the page as a report of success.
            return web.json_response(
                {"error": "not run: this manuscript is open read-only, and the "
                          "findings would have to be written to the comment log."},
                status=403)
        from manuscriptor.server import preflight as preflight_mod

        def work():
            planned = preflight_mod.plan(session.root)
            results = preflight_mod.run(session.root)
            owed = preflight_mod.deliverable(planned, results)
            filed = preflight_mod.deliver(session.root, planned=planned,
                                          results=results)
            return planned, results, owed, filed

        async with preflight_lock:
            planned, results, owed, filed = await asyncio.to_thread(work)
        await session.on_log_change()
        return web.json_response({
            "filed": len(filed),
            "already": len(owed) - len(filed),
            "findings": sum(len(r.findings) for r in results),
            "not_run": sum(1 for r in results if r.status == "skipped")
            + len(preflight_mod.missing(planned, results)),
            "checks": len(planned),
        })

    app.router.add_get("/", index)
    app.router.add_get("/ws", ws_handler)
    app.router.add_post("/import", import_handler)
    app.router.add_post("/evidence", evidence_handler)
    app.router.add_post("/repair", repair_handler)
    app.router.add_post("/preflight", preflight_handler)
    # The evidence panel's two ways out: the PDF, and the Zotero item record.
    # Both run `open` server-side, the way Reveal in Finder does, because the
    # shell installs no `WKUIDelegate` and a `zotero://` link is dropped inside
    # the content process with no error. The path a PDF resolves to is gated to
    # Zotero's storage in `links`, since a cite key is author-controlled input.
    app.router.add_post("/evidence/open-pdf", links_mod.route(session, "pdf"))
    app.router.add_post("/evidence/open-zotero", links_mod.route(session, "zotero"))
    # A compile is a subprocess, so it is the server's to run. Progress goes
    # back over the websocket above, not down a second channel.
    app.router.add_post("/compile", compile_mod.route(session))
    # An insertion is a coordinated write across three or four files, so it is a
    # request with a body and an answer rather than a frame. The block write
    # inside it still goes through splice, like every other write.
    app.router.add_post("/insert", insert_mod.route(session))

    # The build assets (rasterized figures, copied images) are served from the
    # CURRENT document's build directory, not a fixed mount: switching to a
    # document in another folder moves its cache directory with it, and a
    # static mount frozen at app creation would 404 every figure. Resolved per
    # request against `session.root`, with the same traversal guard `add_static`
    # gave us -- a path escaping the build directory is refused, not served.
    # Registered last so the explicit routes above always win.
    async def assets(request):
        rel = request.match_info.get("path", "")
        base = session.asset_root
        target = (base / rel).resolve()
        if base not in target.parents and target != base:
            # A path climbing out of the build directory is an attack or a bug,
            # never a stale cache, so it is refused WITHOUT being recorded --
            # anything that can reach the port would otherwise be able to write
            # lines into the author's diagnostics.
            raise web.HTTPNotFound()
        # Only what the watcher already calls an asset. This route is the
        # catch-all, so every stray GET lands here -- a browser asks for
        # `/favicon.ico` on its own, unprompted, and neither refreshing nor
        # counting one of those means anything. The set is imported rather than
        # restated so the halves cannot come to disagree about what an asset is.
        from manuscriptor.server.watch import ASSET_SUFFIXES

        is_asset = Path(rel).suffix.lower() in ASSET_SUFFIXES
        if is_asset:
            # A REBUILD REFRESHES THE FIGURES OF THE DOCUMENT IT RENDERED AND OF
            # NO OTHER, and one directory holding `main.tex` and `supplement.tex`
            # has one cache. Serving main, the session re-stages `f1..f3` on
            # every change and never looks at an `sf*` again -- covet-india on
            # 2026-08-04 had `f1..f3` rewritten at 20:16 and every `sf*` still at
            # 20:10, with no scheduling of rebuilds that would ever have caught
            # up, because the document that would do it is the one nobody is
            # rebuilding. Asking here makes staleness impossible instead: the
            # only file that can be served is one that agrees with the source it
            # came from. The no-news case is a stat and a sha256 of the source,
            # measured over covet-india's nine exhibits at 0.13ms per figure --
            # 1.2ms for a page holding all of them -- so nothing here needs an
            # mtime gate in front of the hash, which would only reintroduce the
            # key the sidecar exists to replace. `pdftoppm` is forked only when
            # the answer is no, and that fork is the one branch that is not
            # instant (89ms on a real exhibit), which is why this is off the loop.
            await asyncio.to_thread(
                postprocess.refresh_asset, rel, session.root, base)
        if not target.is_file():
            # The page names an artifact this cache no longer holds AND the
            # manuscript no longer holds its source either -- a figure deleted or
            # renamed under a server still serving the old page. The browser
            # reports that to nobody, so it goes blank in silence and reads as
            # the figure being gone. Record it and let `push_derived` decide
            # whether the clients need telling.
            #
            # WHICH miss it is depends on the manuscript, not on the cache. A
            # source still on disk means nothing staged this file rather than
            # that the author lost it -- a PDF figure on a machine with no
            # poppler is the live case -- and telling him to restart the server
            # about a file in his own figures directory sends him looking in the
            # wrong place. Asked through `postprocess`, which already owns the
            # arithmetic that turns a request path into a manuscript path.
            if is_asset:
                source = postprocess.manuscript_source(rel, session.root)
                if session.note_asset_miss(rel, stageable=source is None):
                    await session.push_derived()
            raise web.HTTPNotFound()
        # A raster's name is not derived from its content -- `fig.pdf.png` is
        # the same string before and after the figure is regenerated, and only
        # the `.sha` sidecar knows the difference. `viewer.js` busts the cache
        # with `?v=` on the frame that announces a rebuild, but a later source
        # patch re-renders the block from the server's HTML with a plain `src`
        # and the bust is dropped. With only `Last-Modified`, the browser then
        # applies heuristic freshness and never asks again, so a stale figure
        # can survive even a reload. `no-cache` keeps the copy and always
        # revalidates; the answer is a 304 with no body, so it costs a round
        # trip rather than a transfer. A long `max-age` would be a promise
        # about a filename that carries no version.
        #
        # AND THE VALIDATOR IS THE CONTENT, NOT THE CLOCK, which is why this is
        # not `web.FileResponse`: that answers a conditional request from the
        # file's mtime, and a staged image carries its SOURCE's mtime because
        # `mirror` copies with `copy2`. New bytes under an older timestamp --
        # a figure restored from a backup, a `cp -p`, an `rsync -a` -- then
        # refresh the cache correctly and 304 to every browser holding the old
        # picture, for as long as that timestamp stays where it is. A strong
        # ETag derived from the bytes cannot say that.
        etag, body = await asyncio.to_thread(_asset_identity, target)
        if _etag_matches(request.headers.get("If-None-Match", ""), etag):
            return web.Response(status=304,
                                headers={"Cache-Control": "no-cache", "ETag": etag})
        ctype, _enc = mimetypes.guess_type(target.name)
        return web.Response(
            body=body,
            headers={"Cache-Control": "no-cache", "ETag": etag},
            content_type=ctype or "application/octet-stream",
        )

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
    auto_compile: bool = True,
    on_switch=None,
) -> None:
    from manuscriptor.server.watch import watch_file, watch_tree

    session = Session(manuscript_dir, main=main, bib=bib, read_only=read_only,
                      auto_compile=auto_compile, on_switch=on_switch)
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
            kind, assets = classify(paths)
            coro = (session.on_log_change() if kind == "log"
                    else session.on_change(refresh_assets=assets) if kind == "source"
                    else session.on_assets_change())
            session.spawn(coro, f"redraw after a {kind} change", loop=loop)

        stop = watch_tree(session.dir, changed)
        # The drain's live feed is generated and hidden, so the tree watcher
        # skips it on purpose. Watched by name instead, at the path the session
        # itself names -- never a path spelled out here, which is how this came
        # to watch `build/manuscriptor` for months after the drain stopped
        # writing there.
        # `create=` follows the mode: arming a watch makes the directory it
        # watches, and on a read-only serve that directory is inside the
        # author's tree. A read-only serve starts no drain, so there is nothing
        # to watch until some other process makes the directory itself.
        stop_feed = watch_file(
            session.feed_file,
            lambda: session.spawn(session.on_feed_change(),
                                  "push the agent's feed", loop=loop),
            create=not read_only)
        # And the ledger by its own name. The two files move at different
        # moments -- the ring is coalesced to at most one write every 0.4s while
        # every entry is appended to the ledger at once -- so watching only the
        # feed would leave the last lines of a quiet turn unpushed until
        # something else happened to shift the envelope.
        stop_history = watch_file(
            session.history_file,
            lambda: session.spawn(session.push_history(),
                                  "push the agent's history", loop=loop),
            create=not read_only)
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
        # A page that OPENS on stale numbers is the case the author actually
        # met: he had not edited anything, the `??` were simply there. So the
        # check is armed once at boot as well, after the URL is printed and the
        # window is open, so nothing about it delays either. It goes through the
        # identical gates -- fresh numbers cost one `.aux` read and start
        # nothing.
        compile_mod.arm(session)
        try:
            await asyncio.Event().wait()
        finally:
            stop()
            stop_feed()
            stop_history()
            await runner.cleanup()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
