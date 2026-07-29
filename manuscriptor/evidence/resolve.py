"""Stage 02 — resolve cite_keys against the .bib and the local Zotero library.

Output: build/citations.json with one record per unique cite_key.

Every value taken out of the `.bib` is LaTeX and passes through
`titles.plain_title` before it is stored, queried or compared. That is the
whole of the fix for the run that matched zero of 42 citations against an
open library holding most of them.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

import bibtexparser

from .titles import plain_title
from .zotero import CitekeyLookup, ZoteroClient, ZoteroError, ZoteroItem

log = logging.getLogger(__name__)

# A reachable library that matches NOTHING is a fault in this code, not a thin
# library: the .bib of a manuscript written by a Zotero user is drawn FROM that
# library. Below this many candidates the sample is too small to draw that
# conclusion, so the alarm stays quiet.
ALARM_MIN_CANDIDATES = 10
# Between zero and this, say so loudly and carry on — a bibliography really can
# be assembled from elsewhere, and refusing to run would make it unusable.
LOW_MATCH_RATE = 0.25
ALARM_OVERRIDE_ENV = "MANUSCRIPTOR_ALLOW_ZERO_ZOTERO_MATCHES"


class ZoteroMatchFailure(RuntimeError):
    """Zotero was open, had plenty to match against, and matched nothing.

    Raised so the pass STOPS. The run that prompted this printed
    "matched Zotero: 8 (19%)", proceeded through fetch and extract, and
    produced an evidence report built on nothing. The number was the alarm and
    nothing treated it as one.
    """


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
    n_stale = 0

    zot_available = zot.is_available()

    for key in unique_keys:
        entry = bib_index.get(key, {})
        doi = plain_title(entry.get("doi"))
        title = plain_title(entry.get("title"))
        authors = _parse_bib_authors(entry.get("author", ""))
        year = _bib_year(entry)
        journal = plain_title(entry.get("journal") or entry.get("booktitle"))

        zot_item: ZoteroItem | None = None
        zot_key: str | None = None
        rung: str | None = None
        ck = CitekeyLookup("bbt_unavailable")
        if zot_available:
            try:
                # Rung 1, the cite key. Exact, cheap, and free of every failure
                # mode the other two have — but it is only a handle on this
                # library when the .bib was exported from its Better BibTeX,
                # so it leads the chain rather than replacing it.
                ck = zot.search_by_citekey(key)
                if ck.status == "exact" and ck.item_key:
                    zot_key, rung = ck.item_key, "citekey"
                # Rung 2, the DOI. Also exact, when the .bib carries one.
                if not zot_key and doi:
                    zot_key = zot.search_by_doi(doi)
                    rung = "doi" if zot_key else rung
                # Rung 3, the title. The only inexact rung, and the one that
                # needed both sides normalized identically to work at all.
                if not zot_key and title:
                    zot_key = zot.search_by_title(title)
                    rung = "title" if zot_key else rung
                if zot_key:
                    zot_item = zot.get_item(zot_key)
            except ZoteroError as exc:
                # One bad lookup must not abort the pass, but it must not be
                # indistinguishable from "not in the library" either. fetch
                # reports it as zotero_error off the null key plus this log.
                log.warning("Zotero lookup failed for %s: %s", key, exc)
        if ck.status == "stale":
            n_stale += 1

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
            "match_rung": rung,
            # Recorded whatever the outcome: a stale cite key is worth telling
            # the author about even when the title rung went on to find the
            # paper, because the .bib is the thing that needs re-exporting.
            "citekey_status": ck.status,
            "citekey_in_library": ck.library_citekey if ck.status == "stale" else None,
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
    rungs: dict[str, int] = {}
    for c in citations:
        if c["match_rung"]:
            rungs[c["match_rung"]] = rungs.get(c["match_rung"], 0) + 1
    for name in ("citekey", "doi", "title"):
        if rungs.get(name):
            print(f"      via {name:8s}: {rungs[name]}")
    if n_stale:
        print(f"  !! {n_stale} cite key(s) exist in the library under a DIFFERENT "
              f"spelling — the .bib export has gone stale. Re-export it; these are "
              f"not missing papers. See citekey_in_library in citations.json.")
    if unresolved:
        print(f"  unresolved     : {len(unresolved)} → unresolved.json")

    _alarm_on_match_rate(citations, n_zotero, zot_available)


def _alarm_on_match_rate(citations: list[dict[str, Any]], n_zotero: int,
                         zot_available: bool) -> None:
    """Refuse to let a catastrophic match rate scroll past as a normal line.

    Everything downstream — fetch, extract, the underline colours in the
    viewer — is built on these matches, and all of it degrades silently and
    completes successfully when the matching is broken. The only signal was a
    percentage in a progress line.
    """
    if not zot_available:
        return  # zero matches with the library shut is the expected outcome
    candidates = [c for c in citations if c["bib_present"] and (c["title"] or c["doi"])]
    if len(candidates) < ALARM_MIN_CANDIDATES:
        return
    rate = n_zotero / len(candidates)
    if n_zotero == 0:
        msg = (f"Zotero is reachable and matched 0 of {len(candidates)} citations "
               f"that carry a title or a DOI.")
        if os.environ.get(ALARM_OVERRIDE_ENV):
            print(f"\n  !! {msg}\n     continuing because {ALARM_OVERRIDE_ENV} is set.\n")
            return
        raise ZoteroMatchFailure(
            f"{msg}\n"
            f"A bibliography drawn from a Zotero library does not overlap it by "
            f"zero, so this is a fault in the lookup rather than a gap in the "
            f"library. Check that titles reach Zotero as plain text.\n"
            f"Set {ALARM_OVERRIDE_ENV}=1 to proceed anyway."
        )
    if rate < LOW_MATCH_RATE:
        print(f"\n  !! LOW ZOTERO MATCH RATE: {n_zotero} of {len(candidates)} "
              f"({100*rate:.0f}%).")
        print(f"     Evidence for the other {len(candidates)-n_zotero} will come from "
              f"caches or nothing at all. If the library holds these papers, the "
              f"lookup is at fault.\n")


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
        # Same function as the title. `{van der Berg}` is a braced group in the
        # middle of a field exactly like `{COVID-19}`, and strip("{}") missed
        # it for the same reason.
        p = plain_title(p)
        if "," in p:
            last = p.split(",", 1)[0].strip()
        else:
            tokens = p.split()
            last = tokens[-1] if tokens else p
        if last:
            authors.append(last)
    return authors


def _bib_year(entry: dict[str, str]) -> int | None:
    y = plain_title(entry.get("year"))
    if y.isdigit() and 1500 < int(y) < 2200:
        return int(y)
    return None
