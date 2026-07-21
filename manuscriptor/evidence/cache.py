"""Shared cache utilities. Read/write fulltext blobs and LLM responses."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

# Deliberately still the cite-evidence path. Both tools are installed during the
# transition and share this cache, so renaming it would orphan every fulltext and
# extraction already paid for. Rename at M6, when cite-evidence is retired.
CACHE_ROOT = Path.home() / ".cache" / "cite-evidence"
FULLTEXT_DIR = CACHE_ROOT / "fulltext"
EXTRACT_DIR = CACHE_ROOT / "extract"


def _sanitize(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", s).strip("_") or "blank"


def ensure_dirs() -> None:
    FULLTEXT_DIR.mkdir(parents=True, exist_ok=True)
    EXTRACT_DIR.mkdir(parents=True, exist_ok=True)


def fulltext_path(doi: str | None, fallback_key: str | None = None) -> Path:
    ensure_dirs()
    stem = _sanitize(doi) if doi else _sanitize(fallback_key or "unknown")
    return FULLTEXT_DIR / f"{stem}.txt"


def read_fulltext(doi: str | None, fallback_key: str | None = None) -> str | None:
    p = fulltext_path(doi, fallback_key)
    if not p.exists():
        return None
    try:
        return p.read_text(encoding="utf-8")
    except OSError:
        return None


def write_fulltext(doi: str | None, content: str, fallback_key: str | None = None) -> Path:
    ensure_dirs()
    p = fulltext_path(doi, fallback_key)
    p.write_text(content, encoding="utf-8")
    return p


def extract_key(claim_sentence: str, fulltext: str, model: str) -> str:
    h = hashlib.sha256()
    h.update(model.encode("utf-8"))
    h.update(b"\x00")
    h.update(claim_sentence.encode("utf-8"))
    h.update(b"\x00")
    # use a digest of the fulltext rather than the full bytes (cheaper)
    ft_digest = hashlib.sha256(fulltext.encode("utf-8")).hexdigest()
    h.update(ft_digest.encode("utf-8"))
    return h.hexdigest()


def extract_path(key: str) -> Path:
    ensure_dirs()
    return EXTRACT_DIR / f"{key}.json"


def read_extract(key: str) -> dict[str, Any] | None:
    p = extract_path(key)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def write_extract(key: str, payload: dict[str, Any]) -> Path:
    p = extract_path(key)
    p.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return p
