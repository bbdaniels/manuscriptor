"""Cross-references are resolved on the LaTeX, before pandoc sees them.

They used to be resolved on pandoc's html, which worked only because
`--mathjax` happens to emit an unresolved `\\ref` inside a math span where a
regex could find it. Under `--mathml` pandoc drops the reference and its text
entirely, so the manuscript reads "See Table  and Section ." with nothing where
the numbers should be, and nothing raises.

Resolving on the source removes the dependency on a rendering backend's
incidental behaviour. It is also the only place `\\pageref` could ever work,
since HTML has no pages and the number has to come from the .aux.
"""
from __future__ import annotations

from manuscriptor.render.refs import resolve_source

LABELS = {"tab:foo": "2", "sec:x": "3", "fig:y": "1", "tab:foo@@page": "14"}


def test_a_reference_becomes_its_number():
    out = resolve_source("See Table \\ref{tab:foo} and Section \\ref{sec:x}.", LABELS)[0]
    assert "See Table 2 and Section 3." == out


def test_an_unknown_label_is_marked_and_reported():
    out, missing = resolve_source("See Table \\ref{tab:nope}.", LABELS)
    assert "tab:nope" in missing
    assert "\\ref" not in out, "a bare \\ref must not reach pandoc"
    assert "??" in out, "and the reader must see that it failed"


def test_pageref_resolves_from_the_page_sentinel():
    out = resolve_source("on page \\pageref{tab:foo}", LABELS)[0]
    assert out == "on page 14"


def test_prose_and_math_are_untouched():
    src = "Math $\\alpha_i$ and a cite \\citep{key} and \\input{exhibits/p}."
    assert resolve_source(src, LABELS)[0] == src


def test_a_reference_inside_math_still_resolves():
    """esttab table notes write `$\\ref{tab:foo}$` sometimes."""
    out = resolve_source("see $\\ref{tab:foo}$", LABELS)[0]
    assert "2" in out and "\\ref" not in out


def test_every_occurrence_is_replaced():
    out = resolve_source("\\ref{tab:foo} then \\ref{tab:foo} again", LABELS)[0]
    assert out == "2 then 2 again"
