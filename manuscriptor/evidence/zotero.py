"""Thin wrapper around the local Zotero HTTP API (port 23119).

Default mode is read-only. Methods that would modify the library (find-pdf,
attach, etc.) live in `repair.py` and are invoked only by the explicit
`cite-evidence repair` subcommand.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Optional

import requests

from .titles import normalize_title, plain_title, titles_close

log = logging.getLogger(__name__)

ZOTERO_LOCAL_ROOT = "http://localhost:23119"
ZOTERO_LOCAL_BASE = f"{ZOTERO_LOCAL_ROOT}/api/users/0"
BBT_RPC = f"{ZOTERO_LOCAL_ROOT}/better-bibtex/json-rpc"

# How alike two cite keys must be before one is called a drifted spelling of
# the other rather than a different paper. A BBT key is derived from author,
# year and title, so drift is usually a suffix (`das2022two` ->
# `das2022twoindias`), not a scramble.
CITEKEY_NEAR_MISS = 0.85


@dataclass
class CitekeyLookup:
    """What Better BibTeX had to say about a cite key.

    `status` is one of:
      bbt_unavailable — Better BibTeX is not installed or not answering
      exact           — the library holds an item under exactly this key
      stale           — a near spelling exists; the .bib export has drifted
      absent          — BBT answered and does not know this key
    """
    status: str
    item_key: Optional[str] = None
    library_citekey: Optional[str] = None


class ZoteroError(RuntimeError):
    """A call to the local Zotero API failed.

    Distinct from "Zotero answered and had nothing", which is an empty result.
    Collapsing the two is what let a broken client read as an empty shelf.
    """


@dataclass
class ZoteroItem:
    key: str
    doi: Optional[str]
    title: str
    authors: list[str]
    year: Optional[int]
    journal: Optional[str]
    attachment_paths: list[str]
    # What identifies a work that has no DOI, and what kind of entry it is.
    # A book is identified by its ISBN and by this record; discarding the record
    # for want of a DOI is how a search for one book returned three different
    # wrong ones. Optional with defaults so every existing construction stands.
    item_type: Optional[str] = None
    book_title: Optional[str] = None
    isbn: Optional[str] = None
    publisher: Optional[str] = None
    edition: Optional[str] = None
    place: Optional[str] = None
    pages: Optional[str] = None
    volume: Optional[str] = None
    issue: Optional[str] = None
    citation_key: Optional[str] = None


class ZoteroClient:
    """Read-only client against local Zotero. No writes, no downloads."""

    def __init__(self, base_url: str = ZOTERO_LOCAL_BASE):
        self.base_url = base_url
        self._title_cache: dict[str, Optional[str]] = {}
        # Imported here rather than at module scope. pyzotero pulls a compiled
        # extension, and when that extension is missing or built for the wrong
        # architecture the import raises — which used to take down every
        # `manuscriptor evidence` invocation at import time, including the
        # stages that never touch Zotero. An unusable client is a Zotero that
        # is unavailable, and that is a state this module already reports.
        try:
            from pyzotero import zotero as _pyzotero

            self.zot = _pyzotero.Zotero(library_id=0, library_type="user", local=True)
            self.client_error: Optional[str] = None
        except Exception as exc:  # noqa: BLE001 - reported, not swallowed
            log.warning("Zotero client unusable (%s: %s)", type(exc).__name__, exc)
            self.zot = None
            self.client_error = f"{type(exc).__name__}: {exc}"

    def is_available(self) -> bool:
        if self.zot is None:
            return False
        try:
            r = requests.get(f"{self.base_url}/items", params={"limit": 1}, timeout=3)
            return r.status_code == 200
        except requests.RequestException as exc:
            log.info("Zotero local API not reachable at %s (%s)", self.base_url, exc)
            return False

    # ---- rung 1: the cite key itself -------------------------------------

    def _bbt_rpc(self, method: str, params: list) -> Optional[list]:
        """Call Better BibTeX's JSON-RPC. None means BBT is not there.

        None and [] are deliberately different answers: "there is no such rung
        on this machine" and "the rung ran and found nothing" need different
        advice, and the .bib of a user without BBT must not be blamed for a
        key that was never going to resolve.
        """
        try:
            r = requests.post(
                BBT_RPC, json={"jsonrpc": "2.0", "method": method,
                               "params": params, "id": 1}, timeout=8)
        except requests.RequestException as exc:
            log.info("Better BibTeX RPC not reachable (%s)", exc)
            return None
        if r.status_code in (404, 501):
            log.info("Better BibTeX is not installed (HTTP %s)", r.status_code)
            return None
        if r.status_code != 200:
            raise ZoteroError(f"Better BibTeX RPC returned HTTP {r.status_code}")
        try:
            return r.json().get("result") or []
        except ValueError as exc:
            raise ZoteroError(f"Better BibTeX RPC returned non-JSON: {exc}") from exc

    def search_by_citekey(self, citekey: str) -> CitekeyLookup:
        """Resolve a .bib cite key against Better BibTeX's own keyspace.

        The exact rung, and the cheap one: no normalization, no fuzz, nothing
        that can go wrong in the way the title path did. It only works when
        the .bib was exported from THIS library, which is why it is a first
        rung rather than a replacement for the others.

        `item.search` is a TEXT search, so a result is only a match when the
        item's own `citation-key` equals the query. Anything else is at best a
        drifted spelling and is reported, never accepted: a BBT key is derived
        from author/year/title, so a metadata edit rewrites it and silently
        staleness every exported .bib. Binding a citation to a paper on a fuzzy
        key match would be worse than leaving it unresolved.
        """
        if not citekey:
            return CitekeyLookup("absent")
        results = self._bbt_rpc("item.search", [citekey])
        if results is None:
            return CitekeyLookup("bbt_unavailable")

        exact, near = [], None
        for it in results:
            cand = (it.get("citation-key") or it.get("citekey") or "").strip()
            if not cand:
                continue
            if cand == citekey:
                exact.append(it)
            elif near is None and _citekey_near(citekey, cand):
                near = cand
        if exact:
            return CitekeyLookup("exact", self._pick_item(exact), citekey)
        if near:
            return CitekeyLookup("stale", None, near)
        return CitekeyLookup("absent")

    def _pick_item(self, items: list[dict]) -> Optional[str]:
        """One item key out of possibly several under the same cite key.

        Duplicates are real in this library — `das2022twoindias` and
        `daniels2019gender` each return two. A "unique answer required" rule
        would refuse a paper that is plainly present, so the rule is instead
        that a cite key is BBT's own handle and everything under it is one
        paper. Of those, the copy carrying a PDF is the one worth returning;
        the choice is otherwise made by sorted key so it never varies between
        runs.
        """
        keys = sorted({k for k in (_item_key(it) for it in items) if k})
        if not keys:
            return None
        if len(keys) > 1:
            log.info("cite key resolves to %d duplicate items: %s", len(keys), keys)
            for k in keys:
                try:
                    if self.pdf_attachments(k):
                        return k
                except ZoteroError:
                    continue
        return keys[0]

    def search_by_doi(self, doi: str) -> Optional[str]:
        """Search Zotero items by DOI. Returns item key or None.

        Uses `qmode=everything` (which matches the DOI field in indexed item
        metadata) plus `itemType=-attachment` to suppress PDF-attachment
        matches and surface only parent items.
        """
        if not doi or self.zot is None:
            return None
        normalized = doi.strip().lower()
        try:
            results = self.zot.items(
                q=normalized,
                qmode="everything",
                itemType="-attachment",
                limit=10,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("Zotero DOI search failed for %r (%s: %s)",
                        normalized, type(exc).__name__, exc)
            raise ZoteroError(f"DOI search failed: {exc}") from exc
        for it in results:
            data = it.get("data", {})
            item_doi = (data.get("DOI") or "").strip().lower()
            if item_doi and item_doi == normalized:
                return it["key"]
        return None

    def search_by_title(self, title: str) -> Optional[str]:
        """Find an item by title. `title` must already be plain text.

        Both the query and the candidate go through `normalize_title`, the one
        function that decides what "the same title" means. Nothing here may
        grow a second opinion about braces or accents.
        """
        if not title or self.zot is None:
            return None
        norm = normalize_title(title)
        if norm in self._title_cache:
            return self._title_cache[norm]

        best_key: Optional[str] = None
        for query, limit in _query_variants(title):
            try:
                results = self.zot.items(
                    q=query,
                    qmode="titleCreatorYear",
                    itemType="-attachment",
                    limit=limit,
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("Zotero title search failed for %r (%s: %s)",
                            query, type(exc).__name__, exc)
                raise ZoteroError(f"title search failed: {exc}") from exc
            for it in results:
                data = it.get("data", {})
                cand = normalize_title(data.get("title", ""))
                if cand and (cand == norm or titles_close(cand, norm)):
                    best_key = it["key"]
                    break
            if best_key:
                break
        self._title_cache[norm] = best_key
        return best_key

    def get_item(self, key: str) -> Optional[ZoteroItem]:
        if self.zot is None:
            return None
        try:
            item = self.zot.item(key)
        except Exception as exc:  # noqa: BLE001
            log.warning("Zotero item fetch failed for %s (%s: %s)",
                        key, type(exc).__name__, exc)
            raise ZoteroError(f"item fetch failed: {exc}") from exc
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
            # Through the same reader as the .bib side. Zotero hands back
            # `<span class="nocase">` where BibTeX used braces, and storing it
            # raw put markup into citations.json and into the viewer.
            title=plain_title(data.get("title")),
            authors=authors,
            year=year,
            journal=plain_title(data.get("publicationTitle")) or None,
            attachment_paths=attachments,
            item_type=(data.get("itemType") or "").strip() or None,
            book_title=plain_title(data.get("bookTitle")) or None,
            isbn=_first_isbn(data.get("ISBN")),
            publisher=(data.get("publisher") or "").strip() or None,
            edition=(data.get("edition") or "").strip() or None,
            place=(data.get("place") or "").strip() or None,
            pages=(data.get("pages") or "").strip() or None,
            volume=(data.get("volume") or "").strip() or None,
            issue=(data.get("issue") or "").strip() or None,
            citation_key=_citation_key(data),
        )

    def pdf_attachments(self, parent_key: str) -> list[dict]:
        """The PDF attachment records of an item. THE place children are filtered.

        This loop — fetch children, keep itemType "attachment" with
        contentType "application/pdf" — was written three times: once to
        collect paths, once to pull fulltext, once to find a file on disk. A
        caller could not ask "does this item have a PDF at all", which is
        exactly the question that separates a library gap from an unindexed
        file, so the answer to both was the same sentence.

        Raises ZoteroError when the library cannot be asked.
        """
        if self.zot is None:
            raise ZoteroError(self.client_error or "no Zotero client")
        try:
            children = self.zot.children(parent_key)
        except Exception as exc:  # noqa: BLE001
            log.warning("Zotero children fetch failed for %s (%s: %s)",
                        parent_key, type(exc).__name__, exc)
            raise ZoteroError(str(exc)) from exc
        out = []
        for c in children:
            data = c.get("data", {})
            if data.get("itemType") == "attachment" and \
                    data.get("contentType") == "application/pdf":
                out.append(c)
        return out

    def _collect_attachment_paths(self, parent_key: str) -> list[str]:
        try:
            children = self.pdf_attachments(parent_key)
        except ZoteroError:
            return []
        return [p for p in ((c.get("data", {}).get("path") or "") for c in children) if p]

    def fulltext_of(self, attachments: list[dict]) -> str:
        """Concatenated indexed fulltext across already-fetched PDF attachments.

        Empty string means Zotero holds the file but has no text indexed for
        it, which is a reindex, not a re-download. Passing the attachments in
        rather than a key keeps `pdf_attachments` a single query whose result
        the caller can also inspect.
        """
        chunks: list[str] = []
        for c in attachments:
            child_key = c["key"]
            try:
                r = requests.get(
                    f"{self.base_url}/items/{child_key}/fulltext", timeout=10
                )
            except requests.RequestException as exc:
                log.info("fulltext request failed for attachment %s (%s)", child_key, exc)
                continue
            if r.status_code != 200:
                log.info("fulltext HTTP %s for attachment %s", r.status_code, child_key)
                continue
            try:
                body = r.json()
            except ValueError:
                log.info("fulltext response for attachment %s was not JSON", child_key)
                continue
            content = body.get("content") or ""
            if content:
                chunks.append(content)
        return "\n\n".join(chunks).strip()

    def get_fulltext(self, key: str) -> str:
        """Indexed fulltext for an item key. Empty string if there is none."""
        try:
            return self.fulltext_of(self.pdf_attachments(key))
        except ZoteroError:
            return ""


def _item_key(bbt_item: dict) -> Optional[str]:
    """BBT returns CSL-JSON whose `id` is a Zotero item URI; the key is its last
    segment. `.../items/TQE457YC` -> `TQE457YC`."""
    for field in ("itemKey", "key"):
        if bbt_item.get(field):
            return str(bbt_item[field])
    raw = str(bbt_item.get("id") or "")
    tail = raw.rsplit("/", 1)[-1].strip()
    return tail or None


def _citekey_near(a: str, b: str) -> bool:
    """Is `b` a drifted spelling of `a` rather than a different paper?

    Drift from a metadata edit almost always extends or truncates the title
    stem, so a prefix relation is the common case; the ratio catches the rest.
    """
    a, b = a.lower(), b.lower()
    if a == b:
        return False
    if len(a) >= 6 and len(b) >= 6 and (a.startswith(b) or b.startswith(a)):
        return True
    return SequenceMatcher(None, a, b).ratio() >= CITEKEY_NEAR_MISS


def _query_variants(title: str) -> list[tuple[str, int]]:
    """Progressively shorter queries for Zotero quicksearch.

    Quicksearch is an AND over literal substrings, so ONE token spelled
    differently makes the paper invisible: the .bib writes "low-and
    middle-income" where the library writes "low- and middle-income", and the
    full-title query returns nothing at all.

    This widens the CANDIDATE POOL only. What counts as a match —
    `normalize_title` equality, or `titles_close` — is untouched, so the
    fallback cannot admit a paper the strict test would have rejected. That
    distinction is the whole point: loosening the acceptance test would trade
    misses for false matches, which is a worse failure because it is silent.
    """
    variants: list[tuple[str, int]] = [(title, 10)]
    words = [w for w in title.split() if len(w) > 3]
    if len(words) >= 5:
        variants.append((" ".join(words[:5]), 25))
    if len(words) >= 3:
        variants.append((" ".join(words[:3]), 25))
    seen, out = set(), []
    for q, limit in variants:
        if q and q not in seen:
            seen.add(q)
            out.append((q, limit))
    return out


_ISBN_RE = re.compile(r"[\dXx][\dXx -]{8,}")
# Better BibTeX writes the key into `citationKey` on newer Zotero, and into the
# Extra field on older ones. Both are read, because a key invented instead of
# read is one an export gated on the library can never resolve.
_EXTRA_KEY_RE = re.compile(r"(?im)^\s*Citation Key\s*:\s*(\S+)\s*$")


def _first_isbn(value) -> Optional[str]:
    """Zotero stores several ISBNs in one field, space or comma separated."""
    m = _ISBN_RE.search(str(value or ""))
    return m.group(0).strip() if m else None


def _citation_key(data: dict) -> Optional[str]:
    key = (data.get("citationKey") or "").strip()
    if key:
        return key
    m = _EXTRA_KEY_RE.search(str(data.get("extra") or ""))
    return m.group(1) if m else None


_YEAR_RE = re.compile(r"\b(1[89]\d{2}|20\d{2}|21\d{2})\b")


def _extract_year(date_str: str) -> Optional[int]:
    m = _YEAR_RE.search(date_str or "")
    return int(m.group(1)) if m else None
