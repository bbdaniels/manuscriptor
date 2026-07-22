"""M3 — the local HTTP and websocket server.

Serves the rendered manuscript, pushes block-level patches on change, and
accepts comments and direct edits from the page.

The server is the product and the shell is a client. Any number of clients may
attach to the same port, which is what lets the author work in a standalone
`Manuscriptor.app` window while Claude verifies the same page in a browser tab
through devtools. Neither constrains the other.

Driving the page also happens here rather than through browser automation. Both
ends of the websocket are ours, so scrolling to a block, opening a chat, or
flashing a diff are just messages down a channel that already exists for hot
reload. This is why the shell choice is not load-bearing.

The window never scrolls; each of the three columns does. So a patch must
restore THREE scroll positions, not one, plus any half-typed chat box. Losing
the reader's place in the manuscript is obvious; losing their place in the
inspector is worse, because they were probably mid-way through the code that
caused the change. A live redraw that forgets either is worse than a batch
rebuild.
"""
from __future__ import annotations

from pathlib import Path


def serve(manuscript_dir: Path, *, port: int = 0, open_window: bool = True) -> None:
    raise NotImplementedError("M3")
