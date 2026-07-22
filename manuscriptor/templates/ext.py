"""Client extensions, loaded into the page after the viewer.

Four features want to add a panel, a toolbar action or a frame handler. Four of
them editing `viewer.js` would be four sets of conflicting edits to one large
module, so each lives in its own script under `templates/static/ext/` and
registers through `MSViewer.extend`. The viewer calls them; they never reach
into its state.

Load order is alphabetical and deliberate: an extension that depended on another
having registered first would be a dependency this contract does not express,
and the fix would be to widen the contract rather than to order the files.
"""
from __future__ import annotations

from importlib import resources


def load() -> dict[str, str]:
    """Every extension script, keyed by name, in a stable order."""
    out: dict[str, str] = {}
    try:
        folder = resources.files("manuscriptor.templates.static.ext")
    except ModuleNotFoundError:
        return out
    for entry in sorted(folder.iterdir(), key=lambda p: p.name):
        if entry.name.endswith(".js"):
            out[entry.name[:-3]] = entry.read_text(encoding="utf-8")
    return out
