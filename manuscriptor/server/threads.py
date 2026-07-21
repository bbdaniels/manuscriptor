"""M4 — the comment log.

`comments.jsonl` lives in the manuscript repo and is git-tracked. It is append
only, and that is structural rather than stylistic: two processes write it, the
server on the author's behalf and Claude on its own. If either rewrote the file
there would be conflicts; if both only append there can never be one. It also
leaves a complete audit trail of what changed in a manuscript and why.

A comment, a direct edit, and a state change are all records in the same log,
so there is one history rather than two.

    {"id":"c-0007","kind":"comment","block":"b-3f2a","file":"main.tex",
     "lines":[212,212],"quote":"first 120 chars of the anchored source",
     "body":"you say this twice, tighten","author":"bb","ts":"..."}
    {"id":"c-0007","kind":"state","state":"working","ts":"..."}
    {"id":"c-0007","kind":"state","state":"done","edit":{"before":"...","after":"..."},"ts":"..."}
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

State = Literal["queued", "working", "done", "orphaned"]


@dataclass(frozen=True)
class Thread:
    """A comment plus every state record that followed it, folded."""

    id: str
    block: str
    file: Path
    body: str
    quote: str
    state: State


def append(log: Path, record: dict) -> None:
    """Append one record. Must open in append mode and flush, never rewrite."""
    raise NotImplementedError("M4")


def read_threads(log: Path) -> tuple[Thread, ...]:
    """Fold the append-only log into current thread state."""
    raise NotImplementedError("M4")


def pending(log: Path) -> tuple[Thread, ...]:
    """Threads awaiting work. This is what a drain reads."""
    raise NotImplementedError("M5")
