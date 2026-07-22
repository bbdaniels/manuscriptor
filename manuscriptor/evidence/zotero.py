"""Thin wrapper around the local Zotero HTTP API (port 23119).

Default mode is read-only. Methods that would modify the library (find-pdf,
attach, etc.) live in `repair.py` and are invoked only by the explicit
`cite-evidence repair` subcommand.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

import requests
from pyzotero import zotero

ZOTERO_LOCAL_BASE = "http://localhost:23119/api/users/0"


@dataclass
class ZoteroItem:
    key: str
    doi: Optional[str]
    title: str
    authors: list[str]
    year: Optional[int]
    journal: Optional[str]
    attachment_paths: list[str]


class ZoteroClient:
    """Read-only client against local Zotero. No writes, no downloads."""

    def __init__(self, base_url: str = ZOTERO_LOCAL_BASE):
        self.base_url = base_url
        self.zot = zotero.Zotero(library_id=0, library_type="user", local=True)
        self._title_cache: dict[str, Optional[str]] = {}

    def is_available(self) -> bool:
        try:
            r = requests.get(f"{self.base_url}/items", params={"limit": 1}, timeout=3)
            return r.status_code == 200
        except requests.RequestException:
            return False

    def search_by_doi(self, doi: str) -> Optional[str]:
        """Search Zotero items by DOI. Returns item key or None.

        Uses `qmode=everything` (which matches the DOI field in indexed item
        metadata) plus `itemType=-attachment` to suppress PDF-attachment
        matches and surface only parent items.
        """
        if not doi:
            return None
        normalized = doi.strip().lower()
        try:
            results = self.zot.items(
                q=normalized,
                qmode="everything",
                itemType="-attachment",
                limit=10,
            )
        except Exception:
            return None
        for it in results:
            data = it.get("data", {})
            item_doi = (data.get("DOI") or "").strip().lower()
            if item_doi and item_doi == normalized:
                return it["key"]
        return None

    def search_by_title(self, title: str) -> Optional[str]:
        if not title:
            return None
        norm = _normalize_title(title)
        if norm in self._title_cache:
            return self._title_cache[norm]
        try:
            results = self.zot.items(
                q=title,
                qmode="titleCreatorYear",
                itemType="-attachment",
                limit=10,
            )
        except Exception:
            self._title_cache[norm] = None
            return None
        best_key: Optional[str] = None
        for it in results:
            data = it.get("data", {})
            cand = _normalize_title(data.get("title", ""))
            if cand and (cand == norm or _title_close(cand, norm)):
                best_key = it["key"]
                break
        self._title_cache[norm] = best_key
        return best_key

    def get_item(self, key: str) -> Optional[ZoteroItem]:
        try:
            item = self.zot.item(key)
        except Exception:
            return None
        data = item.get("data", {})
        creators = data.get("creators", []) or []
        authors = [
            (c.get("lastName") or c.get("name") or "").strip()
            for c in creators
            if c.get("creatorType") in (None, "author")
        ]
        authors = [a for a in authors if a]
        year = _extract_year(data.get("date", ""))
        attachments = self._collect_attachment_paths(key)
        return ZoteroItem(
            key=key,
            doi=(data.get("DOI") or "").strip() or None,
            title=data.get("title", "").strip(),
            authors=authors,
            year=year,
            journal=(data.get("publicationTitle") or "").strip() or None,
            attachment_paths=attachments,
        )

    def _collect_attachment_paths(self, parent_key: str) -> list[str]:
        try:
            children = self.zot.children(parent_key)
        except Exception:
            return []
        paths: list[str] = []
        for c in children:
            data = c.get("data", {})
            if data.get("itemType") != "attachment":
                continue
            if data.get("contentType") != "application/pdf":
                continue
            path = data.get("path") or ""
            if path.startswith("attachments:"):
                paths.append(path)
            elif path:
                paths.append(path)
        return paths

    def get_fulltext(self, key: str) -> str:
        """Return concatenated indexed fulltext across all PDF attachments of an item.

        Empty string if no fulltext is available.
        """
        try:
            children = self.zot.children(key)
        except Exception:
            return ""
        chunks: list[str] = []
        for c in children:
            data = c.get("data", {})
            if data.get("itemType") != "attachment":
                continue
            if data.get("contentType") != "application/pdf":
                continue
            child_key = c["key"]
            try:
                r = requests.get(
                    f"{self.base_url}/items/{child_key}/fulltext", timeout=10
                )
            except requests.RequestException:
                continue
            if r.status_code != 200:
                continue
            try:
                body = r.json()
            except ValueError:
                continue
            content = body.get("content") or ""
            if content:
                chunks.append(content)
        return "\n\n".join(chunks).strip()


def _normalize_title(t: str) -> str:
    t = (t or "").lower()
    t = re.sub(r"[^a-z0-9 ]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _title_close(a: str, b: str) -> bool:
    if not a or not b:
        return False
    a_words = a.split()
    b_words = b.split()
    if abs(len(a_words) - len(b_words)) > 3:
        return False
    common = set(a_words) & set(b_words)
    denom = min(len(a_words), len(b_words))
    return denom > 0 and len(common) / denom >= 0.8


_YEAR_RE = re.compile(r"\b(1[89]\d{2}|20\d{2}|21\d{2})\b")


def _extract_year(date_str: str) -> Optional[int]:
    m = _YEAR_RE.search(date_str or "")
    return int(m.group(1)) if m else None
