"""Two ways out of the evidence panel: the PDF, and the Zotero item record.

The panel can say a quote was found; these are how the author goes and looks.
Both run `open` SERVER-SIDE, which is not a preference. `compile.py:reveal` has
always been routed this way and it is the one mechanism in this app that
reliably opens an external thing: the shell installs no `WKUIDelegate`, so an
`<a target="_blank">` is swallowed in silence, and a `zotero://` link is dropped
inside the content process with a flicker and no Zotero. A second mechanism
would be the divergence, so there is one.

Resolution happens ON CLICK and never at build. The storage directory is keyed
by the ATTACHMENT's key, so a path costs a `children` call -- and a path baked
into a page payload goes stale the moment the author re-attaches a file, which
a page open for an afternoon would never notice.

THE PATH GATE IS THE ONE SECURITY-SHAPED THING HERE. A cite key is
author-controlled input arriving over HTTP and `open` on an arbitrary path is an
arbitrary-file-open, so the resolved PDF must lie inside Zotero's storage --
checked AFTER resolution, on what would actually be opened, which is the same
argument `compile.py` makes about its build directory.

Nothing here calls a model, and nothing here writes to the library.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from ..evidence import zotero as zotero_mod
from ..evidence.zotero import ZoteroClient, ZoteroError
from . import paths


class LinkRefused(Exception):
    """A refusal the panel can print, carrying the status it deserves.

    Every path out of here that is not a success ends in one of these, with a
    reason that names WHICH of the outcomes it is -- the discipline the ISBN
    bridge established in `evidence/zotero.py:lookup_identifier`. "Zotero is not
    running" and "this item has no PDF" are different sentences because they are
    different actions for the author, and collapsing them is the defect.
    """

    def __init__(self, reason: str, status: int = 409):
        super().__init__(reason)
        self.reason = reason
        self.status = status


def _open_runner(cmd: list[str]) -> None:
    subprocess.run(cmd, capture_output=True, check=False)


def citation(cite_key: str, *, root) -> dict:
    """The evidence pass's record for one key, or a 404 saying so."""
    path = paths.cache(root) / "citations.json"
    try:
        records = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        records = []
    if isinstance(records, list):
        for r in records:
            if isinstance(r, dict) and r.get("cite_key") == cite_key:
                return r
    raise LinkRefused(
        f"no citation record for {cite_key}; run the evidence pass and try again", 404)


def _zotero_key(cite_key: str, *, root) -> str:
    rec = citation(cite_key, root=root)
    key = str(rec.get("zotero_key") or "").strip()
    if not key:
        raise LinkRefused(
            f"{cite_key} matched no item in your Zotero library, so there is "
            f"nothing to open; its DOI is the way in", 409)
    return key


def resolve_pdf(cite_key: str, *, root, client=None) -> Path:
    """Where this citation's PDF is, right now, or why it cannot be opened."""
    key = _zotero_key(cite_key, root=root)
    zot = client if client is not None else ZoteroClient()
    if not zot.is_available():
        raise LinkRefused(
            "Zotero is not running, so its library cannot be asked where the "
            "file is; start Zotero and try again", 503)
    try:
        attachments = zot.pdf_attachments(key)
    except ZoteroError as exc:
        raise LinkRefused(
            f"Zotero could not be asked for {cite_key}'s attachments: {exc}", 502) from exc
    if not attachments:
        raise LinkRefused(
            f"Zotero holds {cite_key} and no PDF is attached to it; "
            f"Fetch missing PDFs in the toolbar is the one action that writes "
            f"to your library", 409)
    found = zotero_mod.stored_pdfs(attachments)
    if not found:
        raise LinkRefused(
            f"{cite_key}'s attachment is recorded in Zotero and its file is "
            f"not on disk; the library knows about it but never synced it down", 409)
    target = found[0]
    store = zotero_mod.ZOTERO_STORAGE
    if not _inside(target, store):
        raise LinkRefused(
            f"{target} resolves outside {store} and will not be opened", 403)
    return target.resolve()


def open_pdf(cite_key: str, *, root, client=None, runner=None) -> Path:
    target = resolve_pdf(cite_key, root=root, client=client)
    if not target.exists():
        raise LinkRefused(f"{target} is not there any more", 409)
    (runner or _open_runner)(["open", str(target)])
    return target


def open_zotero(cite_key: str, *, root, runner=None) -> str:
    """Select the item in Zotero. `open` launches Zotero if it is not running,
    which is why this one does not ask whether it is."""
    key = _zotero_key(cite_key, root=root)
    url = f"zotero://select/library/items/{key}"
    (runner or _open_runner)(["open", url])
    return url


def _inside(path: Path, parent: Path) -> bool:
    try:
        Path(path).resolve().relative_to(Path(parent).resolve())
        return True
    except ValueError:
        return False


# --------------------------------------------------------------- the routes


def route(session, action: str):
    """An aiohttp handler bound to one session and one of the two opens."""
    from aiohttp import web

    async def handler(request):
        try:
            data = await request.json()
        except Exception:  # noqa: BLE001 - a bodyless post is a bad request
            data = {}
        cite_key = str(data.get("cite_key") or "").strip()
        if not cite_key:
            return web.json_response({"error": "no cite_key was sent"}, status=400)
        try:
            if action == "pdf":
                opened = str(open_pdf(cite_key, root=session.root))
            else:
                opened = open_zotero(cite_key, root=session.root)
        except LinkRefused as exc:
            return web.json_response({"error": exc.reason}, status=exc.status)
        return web.json_response({"opened": opened})

    return handler
