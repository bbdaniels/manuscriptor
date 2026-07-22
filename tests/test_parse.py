"""Unit tests for Stage 01 — parse."""
from __future__ import annotations

from pathlib import Path

from manuscriptor.evidence.parse import (
    _extract_cite_positions,
    _extract_sections,
    _find_section_at,
    _segment_sentences,
    _strip_comments,
)

FIXTURE = Path(__file__).parent / "fixtures" / "mini.tex"


def test_strip_comments_keeps_literal_percent():
    s = _strip_comments("foo % a comment\nbar 50\\% baz\n% line")
    assert "% a comment" not in s
    assert "50\\%" in s
    assert "% line" not in s


def test_extract_cite_positions_finds_all_invocations():
    src = FIXTURE.read_text(encoding="utf-8")
    cites = _extract_cite_positions(src)
    macros = [c["macro"] for c in cites]
    assert macros == ["citep", "citet", "citet", "citep"]
    # First invocation has two keys
    assert cites[0]["keys"] == ["holmstrom1991multitask", "kane2008estimating"]
    # Second invocation has one key
    assert cites[1]["keys"] == ["anderson2026starrating"]


def test_extract_sections_locates_titles_in_order():
    src = FIXTURE.read_text(encoding="utf-8")
    sections = _extract_sections(src)
    assert [s["title"] for s in sections] == ["Background", "Method"]


def test_find_section_at_returns_preceding_section():
    src = FIXTURE.read_text(encoding="utf-8")
    sections = _extract_sections(src)
    cites = _extract_cite_positions(src)
    # First cite is in Background
    assert "Background" in _find_section_at(cites[0]["start"], sections)
    # Last cite is in Method
    assert "Method" in _find_section_at(cites[-1]["start"], sections)


def test_segment_sentences_respects_abbreviations():
    text = "We use e.g. Fig. 3. The next sentence starts here. Another one follows."
    sents = _segment_sentences(text)
    # "e.g." and "Fig." must not break sentences mid-flow
    assert any("e.g." in s and "Fig. 3" in s for s in sents)
    assert any(s.startswith("The next sentence") for s in sents)
    assert any(s.startswith("Another one") for s in sents)


def test_segment_sentences_handles_decimals():
    text = "The threshold of 0.05 is standard. Below 1.96 it fails."
    sents = _segment_sentences(text)
    assert any("0.05" in s and "standard" in s for s in sents)
    assert any("1.96" in s for s in sents)
