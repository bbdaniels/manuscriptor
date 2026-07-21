"""Stage 02 — resolve cite_keys against the .bib and the local Zotero library.

Output: build/citations.json with one record per unique cite_key.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import bibtexparser

from .zotero import ZoteroClient, ZoteroItem


def run(*, bib_file: Path, output_dir: Path) -> None:
    claims_path = output_dir / "claims.json"
    if not claims_path.exists():
        raise FileNotFoundError(f"claims.json not found — run parse first. ({claims_path})")
    claims = json.loads(claims_path.read_text(encoding="utf-8"))
    unique_keys = sorted({k for c in claims for k in c["cite_keys"]})

    bib_index = _load_bib_index(bib_file)

    zot = ZoteroClient()
    if not zot.is_available():
        print("  warning: Zotero local API not reachable at http://localhost:23119 — citations will resolve only from bib")

    citations: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []

    for key in unique_keys:
        entry = bib_index.get(key, {})
        doi = (entry.get("doi") or "").strip()
        title = (entry.get("title") or "").strip().strip("{}")
        authors_raw = entry.get("author", "")
        authors = _parse_bib_authors(authors_raw)
        year = _bib_year(entry)
        journal = (entry.get("journal") or entry.get("booktitle") or "").strip().strip("{}")

        zot_item: ZoteroItem | None = None
        zot_key: str | None = None
        if zot.is_available():
            if doi:
                zot_key = zot.search_by_doi(doi)
            if not zot_key and title:
                zot_key = zot.search_by_title(title)
            if zot_key:
                zot_item = zot.get_item(zot_key)

        if zot_item is None and not entry:
            unresolved.append({
                "cite_key": key,
                "reason": "missing from .bib and Zotero",
            })

        citation = {
            "cite_key": key,
            "doi": (zot_item.doi if zot_item and zot_item.doi else doi) or None,
            "zotero_key": zot_key,
            "title": (zot_item.title if zot_item else title) or "",
            "authors": (zot_item.authors if zot_item else authors),
            "year": (zot_item.year if zot_item and zot_item.year else year),
            "journal": (zot_item.journal if zot_item and zot_item.journal else journal) or None,
            "bib_present": key in bib_index,
            "zotero_present": zot_item is not None,
        }
        citations.append(citation)

    (output_dir / "citations.json").write_text(
        json.dumps(citations, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    if unresolved:
        (output_dir / "unresolved.json").write_text(
            json.dumps(unresolved, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    n_zotero = sum(1 for c in citations if c["zotero_present"])
    n_doi = sum(1 for c in citations if c["doi"])
    print(f"  unique cite_keys: {len(citations)}")
    print(f"  with DOI       : {n_doi}")
    print(f"  matched Zotero : {n_zotero}  ({100*n_zotero/max(len(citations),1):.0f}%)")
    if unresolved:
        print(f"  unresolved     : {len(unresolved)} → unresolved.json")


def _load_bib_index(bib_file: Path) -> dict[str, dict[str, str]]:
    with bib_file.open(encoding="utf-8") as f:
        db = bibtexparser.load(f)
    return {e["ID"]: e for e in db.entries}


def _parse_bib_authors(s: str) -> list[str]:
    if not s:
        return []
    # BibTeX uses " and " as the author separator. Each author is "Last, First"
    # or "First Last".
    parts = [p.strip() for p in s.split(" and ") if p.strip()]
    authors: list[str] = []
    for p in parts:
        p = p.strip("{}").strip()
        if "," in p:
            last = p.split(",", 1)[0].strip()
        else:
            tokens = p.split()
            last = tokens[-1] if tokens else p
        if last:
            authors.append(last)
    return authors


def _bib_year(entry: dict[str, str]) -> int | None:
    y = (entry.get("year") or "").strip().strip("{}")
    if y.isdigit() and 1500 < int(y) < 2200:
        return int(y)
    return None
