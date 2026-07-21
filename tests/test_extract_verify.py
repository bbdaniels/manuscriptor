"""Unit tests for verbatim quote verification."""
from __future__ import annotations

from manuscriptor.evidence.extract import _normalize_for_match, _verify_quote


def test_exact_substring_is_verbatim():
    ft = "The performance was significantly improved over baseline."
    hn = _normalize_for_match(ft)
    status, offset = _verify_quote("performance was significantly improved", ft, hn)
    assert status == "verbatim"
    assert offset is not None


def test_hyphenation_across_line_break_is_verbatim():
    ft = "Performance was sig-\nnificantly improved over baseline."
    hn = _normalize_for_match(ft)
    status, _ = _verify_quote("Performance was significantly improved", ft, hn)
    assert status == "verbatim"


def test_smart_quote_normalization_is_verbatim():
    ft = "The author called it “a substantial reform” in the paper."
    hn = _normalize_for_match(ft)
    status, _ = _verify_quote('called it "a substantial reform"', ft, hn)
    assert status == "verbatim"


def test_endash_vs_hyphen_is_verbatim():
    ft = "The 2015–2025 period showed gains."
    hn = _normalize_for_match(ft)
    status, _ = _verify_quote("the 2015-2025 period", ft, hn)
    assert status == "verbatim"


def test_unrelated_text_is_paraphrase():
    ft = "The actual paper says one thing."
    hn = _normalize_for_match(ft)
    status, offset = _verify_quote("something completely different here", ft, hn)
    assert status == "paraphrase"
    assert offset is None


def test_collapsed_whitespace_is_verbatim():
    ft = "The   results   show  improvement."
    hn = _normalize_for_match(ft)
    status, _ = _verify_quote("the results show improvement", ft, hn)
    assert status == "verbatim"


def test_too_short_demoted_to_paraphrase():
    ft = "yes."
    hn = _normalize_for_match(ft)
    status, offset = _verify_quote("yes", ft, hn)
    assert status == "paraphrase"
    assert offset is None
