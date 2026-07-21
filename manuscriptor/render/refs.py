"""M2 — resolve \\ref and \\pageref out of the compiled .aux.

Pandoc leaves cross-references as raw LaTeX; estonia-ecm leaked 59 of them. The
`pandoc-docx` skill already carries a working recipe for reading `main.aux`,
so reuse it rather than reinventing.

Resolution needs a compiled `.aux`, which a never-compiled manuscript will not
have. That case must degrade visibly (a marked unresolved reference) rather
than silently, so the author can tell the difference between a broken reference
and an uncompiled one.
"""
from __future__ import annotations

from pathlib import Path


def load_labels(aux: Path) -> dict[str, str]:
    raise NotImplementedError("M2")


def resolve(html: str, labels: dict[str, str]) -> tuple[str, list[str]]:
    """Substitute references; return the html and any keys left unresolved."""
    raise NotImplementedError("M2")
