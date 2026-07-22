"""Stage 05 — render the interactive index.html viewer.

Wraps pandoc's citation spans with click handlers and per-cite status badges,
then renders the Jinja2 template with embedded JSON data.
"""
from __future__ import annotations

import json
import re
import shutil
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path

from jinja2 import Template


def run(*, output_dir: Path, main_tex: Path) -> None:
    manuscript_html = (output_dir / "manuscript.html").read_text(encoding="utf-8")
    claims: list[dict] = json.loads((output_dir / "claims.json").read_text(encoding="utf-8"))
    citations: list[dict] = json.loads((output_dir / "citations.json").read_text(encoding="utf-8"))
    evidence_path = output_dir / "evidence.json"
    evidence: list[dict] = (
        json.loads(evidence_path.read_text(encoding="utf-8")) if evidence_path.exists() else []
    )

    citations_by_key = {c["cite_key"]: c for c in citations}
    evidence_by_pair = _index_evidence(evidence)

    augmented_html = _augment_html(
        manuscript_html, claims, citations_by_key, evidence_by_pair
    )

    title = _extract_title(main_tex, manuscript_html, fallback=main_tex.stem)

    n_pairs = sum(len(c["cite_keys"]) for c in claims)
    n_verbatim = sum(1 for r in evidence for q in r.get("quotes", []) if q.get("status") == "verbatim")
    n_paraphrase = sum(1 for r in evidence for q in r.get("quotes", []) if q.get("status") == "paraphrase")
    n_missing_pairs = n_pairs - sum(
        1 for c in claims for k in c["cite_keys"]
        if evidence_by_pair.get(c["claim_id"], {}).get(k, {}).get("quotes")
    )

    template_src = resources.files("manuscriptor.templates").joinpath("index.html.j2").read_text(encoding="utf-8")
    styles_css = resources.files("manuscriptor.templates.static").joinpath("styles.css").read_text(encoding="utf-8")
    viewer_js = resources.files("manuscriptor.templates.static").joinpath("viewer.js").read_text(encoding="utf-8")

    rendered = Template(template_src).render(
        title=title,
        title_json=json.dumps(title),
        manuscript_html=augmented_html,
        styles_css=styles_css,
        viewer_js=viewer_js,
        claims_json=json.dumps(claims, ensure_ascii=False),
        citations_by_key_json=json.dumps(citations_by_key, ensure_ascii=False),
        evidence_by_pair_json=json.dumps(evidence_by_pair, ensure_ascii=False),
        n_pairs=n_pairs,
        n_verbatim=n_verbatim,
        n_paraphrase=n_paraphrase,
        n_missing=n_missing_pairs,
        generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    )
    (output_dir / "index.html").write_text(rendered, encoding="utf-8")

    _copy_assets(main_tex.parent, output_dir, augmented_html)

    print(f"  n cite-instances: {n_pairs}")
    print(f"  verbatim quotes : {n_verbatim}")
    print(f"  paraphrase      : {n_paraphrase}")
    print(f"  pairs w/o quote : {n_missing_pairs}")


def _copy_assets(manuscript_dir: Path, output_dir: Path, html: str) -> None:
    """Copy local images referenced in the rendered HTML into output_dir.

    Pandoc emits `<img src="exhibits/foo.png">` (or similar relative paths).
    We mirror those relative paths under output_dir so the page renders
    standalone.
    """
    seen: set[str] = set()
    for m in re.finditer(r'<img\s[^>]*src="([^"]+)"', html):
        src = m.group(1)
        if src.startswith(("http://", "https://", "data:", "/")):
            continue
        if src in seen:
            continue
        seen.add(src)
        source = (manuscript_dir / src).resolve()
        if not source.exists():
            continue
        dest = (output_dir / src).resolve()
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, dest)
        except OSError:
            continue


# ----------------------------------------------------------------------------- helpers


def _index_evidence(evidence: list[dict]) -> dict[str, dict[str, dict]]:
    by_pair: dict[str, dict[str, dict]] = {}
    for r in evidence:
        by_pair.setdefault(r["claim_id"], {})[r["cite_key"]] = r
    return by_pair


_CITATION_SPAN_RE = re.compile(
    r'(<span\s+class="citation"\s+data-cites="([^"]+)")([^>]*)(>)'
)


def _augment_html(
    html: str,
    claims: list[dict],
    citations_by_key: dict[str, dict],
    evidence_by_pair: dict[str, dict[str, dict]],
) -> str:
    """Wire pandoc's citation spans to claim_ids and tag them with status classes.

    We zip pandoc's spans (in document order) with our claims (in document
    order) on matching cite_key sets. When the sets don't match, we fall back
    to per-key best-match.
    """
    claims_in_order = list(claims)
    claim_iter = iter(claims_in_order)
    used_claim_ids: set[str] = set()

    def assign(match: re.Match) -> str:
        nonlocal claim_iter
        cite_keys_attr = match.group(2)
        keys = cite_keys_attr.split()
        # Walk forward in claims until we find one whose cite_keys overlap
        # significantly. Pandoc occasionally splits a multi-key cite or
        # reorders keys, so we accept any non-empty intersection in order.
        chosen: dict | None = None
        sentinel: list[dict] = []
        while True:
            try:
                cand = next(claim_iter)
            except StopIteration:
                break
            sentinel.append(cand)
            if cand["claim_id"] in used_claim_ids:
                continue
            if _keysets_overlap(cand["cite_keys"], keys):
                chosen = cand
                break
        if chosen is None:
            # Restart from the beginning to try once more (handles spans whose
            # claim appears earlier than the cursor).
            claim_iter = iter(claims_in_order)
            for cand in claim_iter:
                if cand["claim_id"] in used_claim_ids:
                    continue
                if _keysets_overlap(cand["cite_keys"], keys):
                    chosen = cand
                    break
        # Reset iterator to continue past chosen, if any
        if chosen:
            used_claim_ids.add(chosen["claim_id"])
            claim_iter = iter([c for c in claims_in_order if c["claim_id"] not in used_claim_ids])

        status = _aggregate_status(chosen, keys, evidence_by_pair, citations_by_key)
        claim_id = chosen["claim_id"] if chosen else ""
        extra = (
            f' data-claim-id="{claim_id}"'
            f' data-status="{status}"'
            f' tabindex="0" role="button"'
            f' title="Click for evidence"'
        )
        prefix = match.group(1)
        rest = match.group(3)
        # Add status class to the existing class attr
        new_prefix = prefix.replace('class="citation"', f'class="citation status-{status}"')
        return f"{new_prefix}{rest}{extra}>"

    return _CITATION_SPAN_RE.sub(assign, html)


def _keysets_overlap(a: list[str], b: list[str]) -> bool:
    if not a or not b:
        return False
    return bool(set(a) & set(b))


def _aggregate_status(
    claim: dict | None,
    span_keys: list[str],
    evidence_by_pair: dict[str, dict[str, dict]],
    citations_by_key: dict[str, dict],
) -> str:
    """Combine status across all cite_keys in a span.

    verbatim if any cite_key has at least one verbatim quote.
    paraphrase if no verbatim but at least one paraphrase quote.
    missing otherwise.
    """
    if claim is None:
        return "missing"
    has_verbatim = False
    has_paraphrase = False
    keys_to_check = claim["cite_keys"] or span_keys
    for key in keys_to_check:
        ev = evidence_by_pair.get(claim["claim_id"], {}).get(key)
        if not ev:
            continue
        for q in ev.get("quotes") or []:
            if q.get("status") == "verbatim":
                has_verbatim = True
            elif q.get("status") == "paraphrase":
                has_paraphrase = True
    if has_verbatim:
        return "verbatim"
    if has_paraphrase:
        return "paraphrase"
    return "missing"


_TITLE_H1_RE = re.compile(r"<h1[^>]*>(.*?)</h1>", re.IGNORECASE | re.DOTALL)
_TEX_TITLE_RE = re.compile(r"\\title\s*\{([^}]+)\}", re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")


def _extract_title(main_tex: Path, html: str, fallback: str) -> str:
    # Prefer the LaTeX \title{...} directive
    try:
        src = main_tex.read_text(encoding="utf-8")
    except OSError:
        src = ""
    m_tex = _TEX_TITLE_RE.search(src)
    if m_tex:
        text = m_tex.group(1).strip()
        # Strip LaTeX commands within the title (e.g., \\, \emph{})
        text = re.sub(r"\\[a-zA-Z]+\*?", "", text)
        text = text.replace("{", "").replace("}", "").strip()
        text = re.sub(r"\s+", " ", text)
        if text:
            return text
    m = _TITLE_H1_RE.search(html)
    if m:
        text = _TAG_RE.sub("", m.group(1)).strip()
        if text:
            return text
    return fallback
