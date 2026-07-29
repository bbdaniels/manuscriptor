"""Stage 03 — fetch Zotero indexed fulltext (read-only).

For each resolved Zotero item, retrieve its indexed fulltext and cache to
~/.cache/cite-evidence/fulltext/{sanitized-doi}.txt. Passive fallbacks
(consistency-check cache + local PDF re-extract) never reach the network and
never add anything to Zotero. Misses are logged to build/missing.json.

Every miss records WHY, from a fixed set, plus the list of steps that
actually ran. One sentence used to cover all of them:

    no indexed fulltext available in Zotero or local caches

and it was reached from a single terminal `if not fulltext:` below branches
that were not exhaustive. Both Zotero steps were gated on a resolved item
key, so a citation that never matched anything skipped them entirely and
then reported that Zotero had been searched and had nothing. Diagnosing a
run of 36 such misses required querying the library again by hand, item by
item; 13 of the first 16 checked turned out to hold a readable PDF the whole
time.
"""
from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

from . import cache
from .zotero import ZoteroClient, ZoteroError

log = logging.getLogger(__name__)

CC_PDF_CACHE = Path.home() / ".cache" / "consistency-check" / "pdfs"
ZOTERO_STORAGE = Path.home() / "Zotero" / "storage"

# Why a citation has no fulltext. Each one implies a different fix, which is
# the entire point of not collapsing them into a sentence.
REASONS = {
    # the library could not be asked at all — Zotero closed, or client broken
    "zotero_unreachable": "Zotero was not reachable; no lookup was attempted",
    # the library was asked and errored — a fault, not a gap
    "zotero_error": "the Zotero API raised while looking this up",
    # nothing to look up with: no DOI and no title in the .bib
    "no_doi_and_no_title": "the .bib entry carries neither a DOI nor a title",
    # the paper IS in the library under a different cite key — re-export the
    # .bib. Nothing needs adding, downloading or reindexing, which makes this
    # the most actionable reason of the set and the one most easily mistaken
    # for a missing paper.
    "citekey_stale_export": "the library holds this cite key under a different "
                            "spelling; the .bib export is stale",
    # asked, answered, no such item — add it to the library
    "no_zotero_match": "no Zotero item matched this cite key, DOI or title",
    # the item is there, no PDF hangs off it — this is what repair is for
    "matched_but_no_attachment": "the Zotero item has no PDF attachment",
    # the PDF is there and Zotero has no text for it — reindex, not re-download
    "attachment_not_indexed": "the PDF attachment has no indexed text and no readable local file",
}


def run(*, output_dir: Path) -> None:
    citations_path = output_dir / "citations.json"
    if not citations_path.exists():
        raise FileNotFoundError(f"citations.json not found — run resolve first. ({citations_path})")
    citations: list[dict] = json.loads(citations_path.read_text(encoding="utf-8"))
    cache.ensure_dirs()
    zot = ZoteroClient()
    zot_ok = zot.is_available()

    missing: list[dict] = []
    reason_counts: dict[str, int] = {}
    n_cache_hit = n_zotero = n_local_pdf = n_cc_cache = n_miss = 0

    for cit in citations:
        if cit.get("fulltext_source") and cache.read_fulltext(cit.get("doi"), cit.get("cite_key")):
            # Already done in a prior run, nothing to refresh on the read-only path.
            n_cache_hit += 1
            continue

        doi = cit.get("doi")
        cite_key = cit["cite_key"]
        zot_key = cit.get("zotero_key")

        # Every step that actually ran, in order. A step absent from this list
        # did not happen and may not be reported as having found nothing.
        searched: list[str] = []
        detail = ""
        attachments: list[dict] | None = None
        zot_failed = False

        searched.append("fulltext-cache")
        fulltext = cache.read_fulltext(doi, cite_key)
        source = None
        if fulltext:
            source = "cache"
            n_cache_hit += 1

        if not fulltext and zot_ok and zot_key:
            try:
                attachments = zot.pdf_attachments(zot_key)
                searched.append("zotero-fulltext")
                fulltext = zot.fulltext_of(attachments)
            except ZoteroError as exc:
                log.warning("Zotero fulltext lookup failed for %s (%s): %s",
                            cite_key, zot_key, exc)
                zot_failed = True
                detail = str(exc)
            if fulltext:
                cache.write_fulltext(doi, fulltext, fallback_key=cite_key)
                source = "zotero-indexed"
                n_zotero += 1

        if not fulltext and attachments:
            searched.append("local-pdf")
            fulltext, hit = _try_local_pdf(attachments)
            if hit:
                cache.write_fulltext(doi, fulltext, fallback_key=cite_key)
                source = "local-pdf"
                n_local_pdf += 1

        if not fulltext and doi:
            searched.append("cc-cache")
            ft = _try_cc_cache(doi)
            if ft:
                fulltext = ft
                cache.write_fulltext(doi, fulltext, fallback_key=cite_key)
                source = "cc-cache"
                n_cc_cache += 1

        if not fulltext:
            reason = _why_missing(
                zot_ok=zot_ok, zot_failed=zot_failed, zot_key=zot_key,
                doi=doi, title=cit.get("title", ""), attachments=attachments,
                citekey_status=cit.get("citekey_status"),
            )
            if reason == "citekey_stale_export":
                detail = (f"the library spells this cite key "
                          f"{cit.get('citekey_in_library')!r}")
            cit["has_fulltext"] = False
            cit["fulltext_source"] = "missing"
            cit["fulltext_reason"] = reason
            cit["fulltext_chars"] = 0
            missing.append({
                "cite_key": cite_key,
                "doi": doi,
                "zotero_key": zot_key,
                "title": cit.get("title", ""),
                "reason": reason,
                "explanation": REASONS[reason],
                "searched": searched,
                "detail": detail,
            })
            n_miss += 1
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
        else:
            cit["has_fulltext"] = True
            cit["fulltext_source"] = source
            cit["fulltext_chars"] = len(fulltext)

    citations_path.write_text(json.dumps(citations, indent=2, ensure_ascii=False), encoding="utf-8")
    (output_dir / "missing.json").write_text(json.dumps(missing, indent=2, ensure_ascii=False), encoding="utf-8")

    total = len(citations)
    print(f"  cached         : {n_cache_hit}")
    print(f"  zotero indexed : {n_zotero}")
    print(f"  local PDF      : {n_local_pdf}")
    print(f"  cc-cache       : {n_cc_cache}")
    print(f"  MISSING        : {n_miss}  ({100*n_miss/max(total,1):.0f}%)")
    if n_miss:
        # The breakdown, so that "the library is thin" and "the lookup is
        # broken" are told apart from the console, without a re-query by hand.
        for reason, count in sorted(reason_counts.items(), key=lambda kv: -kv[1]):
            print(f"      {count:4d}  {reason}  — {REASONS[reason]}")
        if reason_counts.get("no_zotero_match", 0) > n_miss / 2:
            print("  !! most misses never matched a Zotero item at all. If these "
                  "papers are in the library, the lookup is at fault, not the library.")
        print(f"  → missing.json ({output_dir / 'missing.json'})")
        print(f"  → run `manuscriptor repair {output_dir}` to attempt fetches (writes to Zotero)")


def _why_missing(*, zot_ok: bool, zot_failed: bool, zot_key: str | None,
                 doi: str | None, title: str, attachments: list[dict] | None,
                 citekey_status: str | None = None) -> str:
    """Which of the six situations this is. Ordered from "could not look" outward.

    The old code had no such function: one terminal `if not fulltext` produced
    one string, so a step that was gated off reported the same result as a step
    that ran and came back empty.
    """
    if not zot_ok:
        return "zotero_unreachable"
    if zot_failed:
        return "zotero_error"
    if not zot_key:
        # Ahead of no_zotero_match: the paper is not missing, the .bib is.
        if citekey_status == "stale":
            return "citekey_stale_export"
        if not (doi or title):
            return "no_doi_and_no_title"
        return "no_zotero_match"
    if not attachments:
        return "matched_but_no_attachment"
    return "attachment_not_indexed"


def _try_local_pdf(attachments: list[dict]) -> tuple[str, bool]:
    """Run pdftotext over the PDF attachments Zotero has already named.

    Takes the attachment records rather than an item key so that listing an
    item's PDFs happens exactly once per citation, in `pdf_attachments`, and
    the caller can also use that list to tell "no PDF" from "PDF not indexed".
    """
    if not attachments or not ZOTERO_STORAGE.exists():
        return "", False
    for c in attachments:
        attach_key = c["key"]
        attach_dir = ZOTERO_STORAGE / attach_key
        if not attach_dir.is_dir():
            continue
        pdfs = list(attach_dir.glob("*.pdf"))
        if not pdfs:
            continue
        text = _pdftotext(pdfs[0])
        if text.strip():
            return text, True
    return "", False


def _try_cc_cache(doi: str) -> str:
    """Look for a PDF in consistency-check's cache and re-extract text from it.

    Read-only — only reads what consistency-check has already stored.
    """
    if not doi:
        return ""
    safe = doi.replace("/", "_")
    candidates = [
        CC_PDF_CACHE / f"{safe}.pdf",
        CC_PDF_CACHE / f"{doi}.pdf",
    ]
    for path in candidates:
        if path.exists():
            text = _pdftotext(path)
            if text.strip():
                return text
    return ""


def _pdftotext(pdf_path: Path) -> str:
    try:
        result = subprocess.run(
            ["pdftotext", "-layout", str(pdf_path), "-"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode == 0:
            return result.stdout
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return ""
