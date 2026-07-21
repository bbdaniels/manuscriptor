"""Stage 03 — fetch Zotero indexed fulltext (read-only).

For each resolved Zotero item, retrieve its indexed fulltext and cache to
~/.cache/cite-evidence/fulltext/{sanitized-doi}.txt. Passive fallbacks
(consistency-check cache + local PDF re-extract) never reach the network and
never add anything to Zotero. Misses are logged to build/missing.json.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from . import cache
from .zotero import ZoteroClient

CC_PDF_CACHE = Path.home() / ".cache" / "consistency-check" / "pdfs"


def run(*, output_dir: Path) -> None:
    citations_path = output_dir / "citations.json"
    if not citations_path.exists():
        raise FileNotFoundError(f"citations.json not found — run resolve first. ({citations_path})")
    citations: list[dict] = json.loads(citations_path.read_text(encoding="utf-8"))
    cache.ensure_dirs()
    zot = ZoteroClient()
    zot_ok = zot.is_available()

    missing: list[dict] = []
    n_cache_hit = n_zotero = n_local_pdf = n_cc_cache = n_miss = 0

    for cit in citations:
        if cit.get("fulltext_source") and cache.read_fulltext(cit.get("doi"), cit.get("cite_key")):
            # Already done in a prior run, nothing to refresh on the read-only path.
            n_cache_hit += 1
            continue

        doi = cit.get("doi")
        cite_key = cit["cite_key"]
        zot_key = cit.get("zotero_key")

        fulltext = cache.read_fulltext(doi, cite_key)
        source = None
        if fulltext:
            source = "cache"
            n_cache_hit += 1
        elif zot_ok and zot_key:
            try:
                fulltext = zot.get_fulltext(zot_key)
            except Exception:
                fulltext = ""
            if fulltext:
                cache.write_fulltext(doi, fulltext, fallback_key=cite_key)
                source = "zotero-indexed"
                n_zotero += 1
        if not fulltext and zot_ok and zot_key:
            fulltext, hit = _try_local_pdf(zot, zot_key)
            if hit:
                cache.write_fulltext(doi, fulltext, fallback_key=cite_key)
                source = "local-pdf"
                n_local_pdf += 1
        if not fulltext and doi:
            ft = _try_cc_cache(doi)
            if ft:
                fulltext = ft
                cache.write_fulltext(doi, fulltext, fallback_key=cite_key)
                source = "cc-cache"
                n_cc_cache += 1

        if not fulltext:
            cit["has_fulltext"] = False
            cit["fulltext_source"] = "missing"
            cit["fulltext_chars"] = 0
            missing.append({
                "cite_key": cite_key,
                "doi": doi,
                "zotero_key": zot_key,
                "title": cit.get("title", ""),
                "reason": "no indexed fulltext available in Zotero or local caches",
            })
            n_miss += 1
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
        print(f"  → missing.json ({output_dir / 'missing.json'})")
        print(f"  → run `manuscriptor repair {output_dir}` to attempt fetches (writes to Zotero)")


def _try_local_pdf(zot: ZoteroClient, zot_key: str) -> tuple[str, bool]:
    """Look for a local PDF attachment with a resolvable file path and run pdftotext on it.

    Zotero stores attachments either at a known path under the storage dir or
    via linked file paths. We try the storage dir first.
    """
    try:
        item = zot.get_item(zot_key)
    except Exception:
        return "", False
    if not item:
        return "", False
    storage = Path.home() / "Zotero" / "storage"
    if not storage.exists():
        return "", False
    # Iterate child attachments via the API to get attachment keys.
    try:
        children = zot.zot.children(zot_key)
    except Exception:
        return "", False
    for c in children:
        d = c.get("data", {})
        if d.get("contentType") != "application/pdf":
            continue
        attach_key = c["key"]
        attach_dir = storage / attach_key
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
