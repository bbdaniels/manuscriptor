"""M2 — turn pandoc output into an addressable document.

Harvests block markers onto `data-mx` attributes, wires citation spans to their
evidence records, and copies referenced assets alongside the output so the page
stands on its own.

`_augment_html` in `manuscriptor/evidence/render.py` already does the citation
half of this, and `_copy_assets` already does the asset half. Generalize both
rather than starting over.
"""
from __future__ import annotations

from pathlib import Path


def postprocess(html: str, *, manuscript_dir: Path, output_dir: Path) -> str:
    raise NotImplementedError("M2")
