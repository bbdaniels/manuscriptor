"""Stage 04 — LLM evidence extraction with prompt caching and verbatim verification.

For each (claim_id, cite_key) pair where the cited paper has indexed fulltext,
ask Claude to extract 1-3 verbatim passages that support the claim sentence.
Each returned quote is then deterministically checked against the fulltext;
quotes that don't match get demoted to status `paraphrase`.

Two backends:
1. `claude -p` subprocess (default) — runs under the user's Claude Code plan,
   no API key required. Works whenever the `claude` CLI is on PATH.
2. Anthropic SDK — used when ANTHROPIC_API_KEY is set OR when the `claude`
   CLI is unavailable. Pays per-call from the user's API budget.

Caches results on disk keyed on (claim_sentence + fulltext_digest + model) so
re-runs are free regardless of backend.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import cache

MAX_FULLTEXT_CHARS = 180_000  # ~45K tokens — comfortable for 200K-context models
QUOTE_MIN_LEN = 25
QUOTE_MAX_LEN = 500

SYSTEM_PROMPT = """You are a research-grade citation-evidence extractor.

Given (a) the indexed fulltext of an academic paper and (b) a CLAIM sentence
from a separate manuscript that cites this paper, return 1-3 verbatim passages
from the paper that DIRECTLY support the claim. A passage is a contiguous
sequence of words copied EXACTLY from the paper text (no paraphrasing, no
ellipses, no insertions). Prefer short, specific passages over long ones.

If no passage in the paper supports the claim, return an empty list with
reasoning explaining why.

Return ONLY a JSON object with this exact shape:
{
  "quotes": [
    {"text": "<verbatim string copied from the paper>", "location_hint": "<section/page hint if discernible from the text, else null>"},
    ...
  ],
  "confidence": <number between 0 and 1 indicating how directly the quotes support the claim>,
  "reasoning": "<one sentence on why these quotes were chosen or why none were found>"
}

Critical rules:
- Each `text` value MUST appear verbatim in the paper fulltext. Do not paraphrase.
- Do not invent quotes. If the paper does not contain a directly supporting passage, return quotes: [].
- Prefer factual/empirical statements over framing/motivational language.
- Keep each quote between 25 and 500 characters."""


def run(*, output_dir: Path, model: str, dry_run: bool = False, backend: str = "auto") -> None:
    claims_path = output_dir / "claims.json"
    citations_path = output_dir / "citations.json"
    if not (claims_path.exists() and citations_path.exists()):
        raise FileNotFoundError("Run parse, resolve, and fetch before extract.")
    claims: list[dict] = json.loads(claims_path.read_text(encoding="utf-8"))
    citations: list[dict] = json.loads(citations_path.read_text(encoding="utf-8"))
    citations_by_key = {c["cite_key"]: c for c in citations}

    work: list[tuple[dict, str, dict]] = []
    for claim in claims:
        for k in claim["cite_keys"]:
            cit = citations_by_key.get(k)
            if not cit or not cit.get("has_fulltext"):
                continue
            work.append((claim, k, cit))

    print(f"  pairs to extract: {len(work)}")
    if not work:
        (output_dir / "evidence.json").write_text("[]", encoding="utf-8")
        return

    # Pre-load fulltexts so we can estimate cost and avoid re-reads.
    fulltext_cache: dict[str, str] = {}
    for cit in citations:
        if cit.get("has_fulltext"):
            ft = cache.read_fulltext(cit.get("doi"), cit.get("cite_key")) or ""
            fulltext_cache[cit["cite_key"]] = ft[:MAX_FULLTEXT_CHARS]

    total_chars = sum(len(fulltext_cache.get(k, "")) for _, k, _ in work)
    approx_tokens = total_chars // 4
    print(f"  approx input tokens (no caching): {approx_tokens:,}")
    if dry_run:
        print("  --dry-run: skipping API calls")
        return

    backend_choice = _select_backend(backend)
    print(f"  backend: {backend_choice}")
    caller = _make_caller(backend_choice, model)

    evidence_records: list[dict[str, Any]] = []
    cache_hits = 0
    api_calls = 0

    # Group by cite_key so the model sees the same fulltext consecutively.
    work.sort(key=lambda w: w[1])
    for i, (claim, cite_key, cit) in enumerate(work, start=1):
        fulltext = fulltext_cache.get(cite_key, "")
        if not fulltext:
            evidence_records.append(_missing_record(claim, cite_key, "fulltext empty"))
            continue

        cache_key = cache.extract_key(claim["sentence"], fulltext, model)
        cached = cache.read_extract(cache_key)
        if cached:
            cache_hits += 1
            evidence_records.append(_finalize_record(claim, cite_key, cached, fulltext, model))
            continue

        print(f"  [{i}/{len(work)}] {claim['claim_id']} ← {cite_key} ...", end=" ", flush=True)
        try:
            raw = caller(fulltext, claim, cit)
        except Exception as e:
            print(f"FAIL ({e})")
            evidence_records.append(_missing_record(claim, cite_key, f"backend error: {e}"))
            continue

        api_calls += 1
        parsed = _parse_response_text(raw)
        n_quotes = len(parsed.get("quotes") or [])
        print(f"{n_quotes} quote(s)")
        payload = {
            "parsed": parsed,
            "model": model,
            "backend": backend_choice,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
        cache.write_extract(cache_key, payload)
        evidence_records.append(_finalize_record(claim, cite_key, payload, fulltext, model))

    (output_dir / "evidence.json").write_text(
        json.dumps(evidence_records, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    print(f"  api calls: {api_calls}   cache hits: {cache_hits}")
    n_verbatim = sum(1 for r in evidence_records for q in r["quotes"] if q["status"] == "verbatim")
    n_para = sum(1 for r in evidence_records for q in r["quotes"] if q["status"] == "paraphrase")
    n_pairs_missing = sum(1 for r in evidence_records if not r["quotes"])
    print(f"  verbatim quotes : {n_verbatim}")
    print(f"  paraphrase      : {n_para}")
    print(f"  pairs w/o quote : {n_pairs_missing}")


def _select_backend(choice: str) -> str:
    if choice == "claude-p":
        if not shutil.which("claude"):
            raise SystemExit("backend=claude-p requested but `claude` not on PATH")
        return "claude-p"
    if choice == "anthropic":
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise SystemExit("backend=anthropic requested but ANTHROPIC_API_KEY is unset")
        return "anthropic"
    # auto: prefer claude -p (runs under user's plan, no API key needed)
    if shutil.which("claude"):
        return "claude-p"
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    raise SystemExit(
        "no LLM backend available: install `claude` CLI or set ANTHROPIC_API_KEY"
    )


def _make_caller(backend: str, model: str):
    if backend == "claude-p":
        return lambda ft, c, cit: _call_claude_cli(model, ft, c, cit)
    import anthropic
    client = anthropic.Anthropic()
    return lambda ft, c, cit: _call_anthropic(client, model, ft, c, cit)


def _build_user_prompt(fulltext: str, claim: dict, citation: dict) -> str:
    return (
        f"# Cited paper\n"
        f"Title: {citation.get('title','')}\n"
        f"Authors: {', '.join(citation.get('authors') or [])}\n"
        f"Year: {citation.get('year') or 'unknown'}\n"
        f"Journal: {citation.get('journal') or 'unknown'}\n"
        f"DOI: {citation.get('doi') or 'unknown'}\n\n"
        f"# Fulltext (indexed)\n"
        f"{fulltext}\n\n"
        f"# Manuscript claim\n"
        f'"{claim["sentence"]}"\n\n'
        f"# Task\n"
        "Return 1-3 verbatim passages from the cited paper above that directly support this claim. "
        "Respond with only the JSON object specified in the system prompt."
    )


def _call_anthropic(client, model: str, fulltext: str, claim: dict, citation: dict):
    user_text_prefix = (
        f"# Cited paper\n"
        f"Title: {citation.get('title','')}\n"
        f"Authors: {', '.join(citation.get('authors') or [])}\n"
        f"Year: {citation.get('year') or 'unknown'}\n"
        f"Journal: {citation.get('journal') or 'unknown'}\n"
        f"DOI: {citation.get('doi') or 'unknown'}\n\n"
        f"# Fulltext (indexed)\n{fulltext}"
    )
    user_content = [
        {"type": "text", "text": user_text_prefix, "cache_control": {"type": "ephemeral"}},
        {
            "type": "text",
            "text": (
                f"# Manuscript claim\n"
                f'"{claim["sentence"]}"\n\n'
                f"# Task\nReturn 1-3 verbatim passages that directly support this claim, as JSON only."
            ),
        },
    ]
    response = client.messages.create(
        model=model,
        max_tokens=1000,
        system=[{"type": "text", "text": SYSTEM_PROMPT}],
        messages=[{"role": "user", "content": user_content}],
    )
    text = ""
    for block in getattr(response, "content", []) or []:
        if getattr(block, "type", None) == "text":
            text += block.text
    return text


def _call_claude_cli(model: str, fulltext: str, claim: dict, citation: dict) -> str:
    """Invoke `claude -p` for the user's plan-billed inference path."""
    prompt = _build_user_prompt(fulltext, claim, citation)
    cmd = [
        "claude",
        "-p",
        "--model", model,
        "--output-format", "json",
        "--no-session-persistence",
        "--system-prompt", SYSTEM_PROMPT,
        "--disable-slash-commands",
        "--exclude-dynamic-system-prompt-sections",
    ]
    result = subprocess.run(
        cmd,
        input=prompt,
        capture_output=True,
        text=True,
        timeout=180,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"claude -p exited {result.returncode}: {result.stderr.strip()[:200]}"
        )
    try:
        envelope = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"could not parse claude envelope: {e}") from None
    if envelope.get("is_error"):
        raise RuntimeError(f"claude reported error: {envelope.get('result')}")
    text = envelope.get("result") or ""
    if not text.strip():
        raise RuntimeError("claude returned empty result")
    return text


def _parse_response_text(text: str) -> dict:
    """Pull JSON out of a plain-text LLM response, tolerating fences and prose."""
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{[\s\S]*\}", text)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
    return {"quotes": [], "confidence": 0.0, "reasoning": "failed to parse model output"}


def _finalize_record(claim: dict, cite_key: str, payload: dict, fulltext: str, model: str) -> dict:
    parsed = payload.get("parsed") or payload
    raw_quotes = parsed.get("quotes") or []
    quotes_out: list[dict] = []
    haystack_norm = _normalize_for_match(fulltext)
    for q in raw_quotes:
        text = (q.get("text") or "").strip()
        if not text:
            continue
        status, offset = _verify_quote(text, fulltext, haystack_norm)
        quotes_out.append({
            "text": text,
            "status": status,
            "char_offset": offset,
            "location_hint": q.get("location_hint"),
        })
    return {
        "claim_id": claim["claim_id"],
        "cite_key": cite_key,
        "quotes": quotes_out,
        "confidence": parsed.get("confidence", 0.0),
        "reasoning": parsed.get("reasoning", ""),
        "model": payload.get("model", model),
        "generated_at": payload.get("generated_at"),
    }


def _missing_record(claim: dict, cite_key: str, reason: str) -> dict:
    return {
        "claim_id": claim["claim_id"],
        "cite_key": cite_key,
        "quotes": [],
        "confidence": 0.0,
        "reasoning": reason,
        "model": None,
        "generated_at": None,
    }


def _normalize_for_match(s: str) -> str:
    s = s.replace("\xa0", " ")
    s = re.sub(r"-\n", "", s)        # de-hyphenate across line breaks
    s = re.sub(r"\s+", " ", s)
    return s.lower()


def _verify_quote(quote: str, fulltext: str, haystack_norm: str) -> tuple[str, int | None]:
    needle = _normalize_for_match(quote)
    if len(needle) < 12:
        return ("paraphrase", None)
    pos = haystack_norm.find(needle)
    if pos != -1:
        return ("verbatim", pos)
    # Try a more lenient match: collapse all quote characters and dashes.
    needle2 = _aggressive_normalize(needle)
    haystack2 = _aggressive_normalize(haystack_norm)
    pos2 = haystack2.find(needle2)
    if pos2 != -1:
        return ("verbatim", pos2)
    return ("paraphrase", None)


def _aggressive_normalize(s: str) -> str:
    s = re.sub(r"['‘’\"“”`´]", "'", s)
    s = re.sub(r"[‐-―−–—-]", "-", s)
    s = re.sub(r"\s+", " ", s)
    return s
