"""Stage 05 — render the interactive index.html viewer.

Wraps pandoc's citation spans with click handlers and per-cite status badges,
then renders `templates/evidence.html.j2` with embedded JSON data.

THE TEMPLATE IS THIS STAGE'S OWN, and the export shares no file with the served
editor. It used to: both were `index.html.j2` over `static/styles.css` and
`static/viewer.js`, and 1af705e wrote the editor over all three names. This
module kept passing evidence variables to a template that had stopped consuming
any of them, so the export silently shed every quote, every piece of citation
metadata and all four counts, painted the manuscript through the editor's
compatibility fallback, and inlined 200KB of websocket client into a page with
no server behind it. It rendered without error the whole time, which is why it
survived. Anything added here must stay inside this stage's own files.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path

from jinja2 import Template

from manuscriptor.render import pandoc, postprocess


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

    # Decide each pair's status ONCE, here, and stamp it into the record every
    # surface reads. The page has two of them -- the click-through panel and the
    # server-rendered index under the manuscript -- and a second derivation in
    # either is how they come to disagree about the same pair.
    for by_key in evidence_by_pair.values():
        for record in by_key.values():
            record["pair_status"] = _pair_status(record)

    augmented_html = _augment_html(
        manuscript_html, claims, citations_by_key, evidence_by_pair
    )

    # Before the template, not after: staging rewrites a PDF figure's `<embed>`
    # into an `<img>`, and it is the rewritten html that has to reach the page.
    augmented_html, _assets = postprocess.stage_assets(
        augmented_html, main_tex.parent, output_dir
    )

    title = _extract_title(main_tex, manuscript_html, fallback=main_tex.stem)

    n_pairs = sum(len(c["cite_keys"]) for c in claims)
    n_verbatim = sum(1 for r in evidence for q in r.get("quotes", []) if q.get("status") == "verbatim")
    n_paraphrase = sum(1 for r in evidence for q in r.get("quotes", []) if q.get("status") == "paraphrase")
    n_missing_pairs = n_pairs - sum(
        1 for c in claims for k in c["cite_keys"]
        if evidence_by_pair.get(c["claim_id"], {}).get(k, {}).get("quotes")
    )

    template_src = resources.files("manuscriptor.templates").joinpath("evidence.html.j2").read_text(encoding="utf-8")

    # autoescape, because a title or a quote is arbitrary text off a PDF and a
    # `<` in one of them would otherwise rewrite the page. The manuscript body
    # and the pre-serialized JSON are marked `| safe` in the template, which is
    # the whole list of things allowed through raw.
    rendered = Template(template_src, autoescape=True).render(
        title=title,
        title_json=_json_for_script(title),
        manuscript_html=augmented_html,
        claims_json=_json_for_script(claims),
        citations_by_key_json=_json_for_script(citations_by_key),
        evidence_by_pair_json=_json_for_script(evidence_by_pair),
        pairs=_index_rows(claims, citations_by_key, evidence_by_pair),
        n_pairs=n_pairs,
        n_verbatim=n_verbatim,
        n_paraphrase=n_paraphrase,
        n_missing=n_missing_pairs,
        generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    )
    (output_dir / "index.html").write_text(rendered, encoding="utf-8")

    print(f"  n cite-instances: {n_pairs}")
    print(f"  verbatim quotes : {n_verbatim}")
    print(f"  paraphrase      : {n_paraphrase}")
    print(f"  pairs w/o quote : {n_missing_pairs}")


# ----------------------------------------------------------------------------- helpers


def _index_evidence(evidence: list[dict]) -> dict[str, dict[str, dict]]:
    by_pair: dict[str, dict[str, dict]] = {}
    for r in evidence:
        by_pair.setdefault(r["claim_id"], {})[r["cite_key"]] = r
    return by_pair


def _json_for_script(value) -> str:
    """Serialize for embedding inside a `<script>` element.

    `json.dumps` leaves `</` alone, so a quote containing a literal `</script>`
    -- arbitrary text lifted out of a PDF, which is exactly what these payloads
    are -- would close the block early and take the rest of the page with it.
    Escaping the slash is invisible to `JSON.parse` and to the JS parser.
    """
    return json.dumps(value, ensure_ascii=False).replace("</", "<\\/")


def _pair_status(evidence: dict | None) -> str:
    """The status of ONE claim/cite pair: the strongest quote it carries.

    The single answer to "how well is this pair supported". The page's panel,
    its server-rendered index, and the underline colour of every citation span
    all read this one function's verdict.
    """
    quotes = (evidence or {}).get("quotes") or []
    if any(q.get("status") == "verbatim" for q in quotes):
        return "verbatim"
    if any(q.get("status") == "paraphrase" for q in quotes):
        return "paraphrase"
    return "missing"


def _index_rows(
    claims: list[dict],
    citations_by_key: dict[str, dict],
    evidence_by_pair: dict[str, dict[str, dict]],
) -> list[dict]:
    """Flatten the payload into the rows the page renders server-side.

    Same data as the JSON blob, shaped for the template rather than for the
    script, so the export is a complete record of the pass even when nothing
    runs -- opened off disk, printed, or read by anything that is not a browser.
    """
    rows: list[dict] = []
    for claim in claims:
        cites = []
        for key in claim.get("cite_keys") or []:
            cit = citations_by_key.get(key) or {}
            ev = evidence_by_pair.get(claim["claim_id"], {}).get(key)
            if ev:
                reason = ev.get("reasoning") or "Model returned no supporting passage."
            elif cit.get("has_fulltext") is False:
                reason = "No indexed fulltext available for this paper."
            else:
                reason = "No evidence record; the extract stage may not have run."
            cites.append({
                "cite_key": key,
                "title": cit.get("title") or "(no title)",
                "authors": ", ".join((cit.get("authors") or [])[:4]),
                "year": cit.get("year") or "",
                "journal": cit.get("journal") or "",
                "doi": cit.get("doi") or "",
                "status": _pair_status(ev),
                "quotes": (ev or {}).get("quotes") or [],
                "reason": reason,
            })
        rows.append({
            "claim_id": claim["claim_id"],
            "sentence": claim.get("sentence") or "",
            "section": _humanize_section(claim.get("section") or ""),
            "cites": cites,
        })
    return rows


def _humanize_section(section: str) -> str:
    """`sec:Results` reads as `Results`. The label prefix is a LaTeX artefact."""
    _, sep, tail = section.partition(":")
    return tail if sep else section


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

    Per-pair the verdict is `_pair_status`'s, never re-derived here: this is the
    strongest of them, and nothing more. A span whose underline disagreed with
    the panel it opens would be the same defect the two templates were.
    """
    if claim is None:
        return "missing"
    keys_to_check = claim["cite_keys"] or span_keys
    seen = {
        _pair_status(evidence_by_pair.get(claim["claim_id"], {}).get(key))
        for key in keys_to_check
    }
    if "verbatim" in seen:
        return "verbatim"
    if "paraphrase" in seen:
        return "paraphrase"
    return "missing"


_TITLE_H1_RE = re.compile(r"<h1[^>]*>(.*?)</h1>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")


def _extract_title(main_tex: Path, html: str, fallback: str) -> str:
    # Prefer the LaTeX \title{...} directive, read by the one function that
    # knows what a \title is -- this was a fifth copy of a `\title\s*\{([^}]+)\}`
    # that could not see `\title[Short]{Long}` and stopped at the first brace.
    try:
        src = main_tex.read_text(encoding="utf-8")
    except OSError:
        src = ""
    got = pandoc.document_title(src)
    if got:
        return got
    m = _TITLE_H1_RE.search(html)
    if m:
        text = _TAG_RE.sub("", m.group(1)).strip()
        if text:
            return text
    return fallback
