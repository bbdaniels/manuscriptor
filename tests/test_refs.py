"""M2 — cross-reference resolution out of the compiled .aux.

A reference that cannot be resolved must come back named, not dropped and not
silently rendered as `??`. The author has to be able to tell "this label does
not exist" from "this manuscript has never been compiled", and both from "this
reference is fine".

The non-obvious half is that pandoc does not leave `\\ref` as bare text. Under
`--from=latex+raw_tex` it emits it inside a MathJax span:

    <span class="math inline">\\(\\ref{tab:foo}\\)</span>

so a substitution that only looks at text nodes finds nothing at all. Verified
against pandoc 3.1.1, 2026-07-22.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from manuscriptor.render.refs import load_labels, resolve

ESTONIA_AUX = Path("/Users/bbdaniels/Projects/estonia-ecm/latex/main.aux")


def write(root: Path, name: str, text: str) -> Path:
    p = root / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


# ------------------------------------------------------------- load_labels


def test_simple_newlabel_yields_the_printed_number(tmp_path):
    aux = write(tmp_path, "main.aux", r"\newlabel{sec:results}{{5}{15}{Results}{section.5}{}}" + "\n")
    assert load_labels(aux)["sec:results"] == "5"


def test_two_field_newlabel_is_parsed(tmp_path):
    """Without hyperref the value block is just {number}{page}."""
    aux = write(tmp_path, "main.aux", "\\newlabel{eq:one}{{3}{7}}\n")
    assert load_labels(aux)["eq:one"] == "3"


def test_nested_braces_in_the_title_do_not_break_parsing(tmp_path):
    """Real aux files carry markup in the title field: \\textbf {Consultations}."""
    aux = write(
        tmp_path,
        "main.aux",
        "\\newlabel{subfig:a}{{2a}{24}{\\textbf {Consultations}: Doctor visits}{figure.caption.2}{}}\n"
        "\\newlabel{sec:next}{{6}{30}{Discussion}{section.6}{}}\n",
    )
    labels = load_labels(aux)
    assert labels["subfig:a"] == "2a"
    # The parser must not have swallowed the following entry.
    assert labels["sec:next"] == "6"


def test_nested_braces_inside_the_number_field_are_flattened(tmp_path):
    aux = write(tmp_path, "main.aux", "\\newlabel{tab:x}{{{A}.1}{5}{Cap}{table.1}{}}\n")
    assert load_labels(aux)["tab:x"] == "A.1"


def test_relax_and_markup_are_stripped_from_the_number(tmp_path):
    aux = write(tmp_path, "main.aux", "\\newlabel{tab:y}{{\\relax 2.1}{5}{Cap}{table.1}{}}\n")
    assert load_labels(aux)["tab:y"] == "2.1"


def test_at_input_pulls_in_child_aux_files(tmp_path):
    write(tmp_path, "tables/t2.aux", "\\newlabel{table:cross}{{2}{19}{ECM impacts}{table.2}{}}\n")
    aux = write(
        tmp_path,
        "main.aux",
        "\\newlabel{sec:results}{{5}{15}{Results}{section.5}{}}\n"
        "\\@input{tables/t2.aux}\n",
    )
    labels = load_labels(aux)
    assert labels["sec:results"] == "5"
    assert labels["table:cross"] == "2"


def test_at_input_of_a_missing_file_is_survivable(tmp_path):
    aux = write(
        tmp_path,
        "main.aux",
        "\\@input{tables/never_written.aux}\n"
        "\\newlabel{sec:only}{{1}{1}{Intro}{section.1}{}}\n",
    )
    labels = load_labels(aux)
    assert labels["sec:only"] == "1"
    assert [k for k in labels if not k.endswith("@@page")] == ["sec:only"]


def test_a_cycle_in_at_input_terminates(tmp_path):
    write(tmp_path, "b.aux", "\\@input{a.aux}\n\\newlabel{from_b}{{9}{9}{B}{s.9}{}}\n")
    aux = write(tmp_path, "a.aux", "\\@input{b.aux}\n\\newlabel{from_a}{{8}{8}{A}{s.8}{}}\n")
    labels = load_labels(aux)
    assert labels["from_a"] == "8"
    assert labels["from_b"] == "9"


def test_a_missing_aux_yields_no_labels_rather_than_an_exception(tmp_path):
    """A never-compiled manuscript has no .aux. That must degrade to 'every
    reference unresolved', which is visible, not to a traceback."""
    assert load_labels(tmp_path / "nothing_here.aux") == {}


@pytest.mark.skipif(not ESTONIA_AUX.exists(), reason="estonia-ecm checkout not present")
def test_the_real_estonia_aux_parses():
    labels = load_labels(ESTONIA_AUX)
    # main.aux alone carries 70 \newlabel lines; the \@input children add more.
    named = [k for k in labels if not k.endswith("@@page")]
    assert len(named) > 70
    assert labels["sec:results"] == "5"
    assert labels["table:baseline"] == "5.1"
    assert labels["fig:fig1_randomization_consort"] == "1"
    # Every value must be a printable number, never leftover LaTeX.
    assert not [v for v in labels.values() if "\\" in v or "{" in v]


# ----------------------------------------------------------------- resolve


def test_bare_ref_in_text_is_substituted():
    html, unresolved = resolve("<p>See Table \\ref{tab:x} now.</p>", {"tab:x": "2"})
    assert html == "<p>See Table 2 now.</p>"
    assert unresolved == []


def test_a_ref_wrapped_in_a_math_span_is_resolved_and_unwrapped():
    """This is the case that matters: pandoc's actual output shape."""
    src = '<p>See Table <span class="math inline">\\(\\ref{tab:foo}\\)</span> now.</p>'
    html, unresolved = resolve(src, {"tab:foo": "3"})
    assert "math" not in html
    assert "\\ref" not in html
    assert html == "<p>See Table 3 now.</p>"
    assert unresolved == []


def test_a_ref_inside_larger_math_is_substituted_without_unwrapping():
    src = '<p><span class="math inline">\\(x = \\ref{eq:z} + 1\\)</span></p>'
    html, unresolved = resolve(src, {"eq:z": "4"})
    assert 'class="math inline"' in html
    assert "\\ref" not in html
    assert "x = 4 + 1" in html
    assert unresolved == []


def test_display_math_is_handled_too():
    src = '<span class="math display">\\[y = \\ref{eq:q}\\]</span>'
    html, unresolved = resolve(src, {"eq:q": "7"})
    assert "\\ref" not in html
    assert "7" in html
    assert unresolved == []


def test_page_numbers_are_carried_alongside_the_printed_number(tmp_path):
    """`\\pageref` needs the page, which lives in the same aux entry. Keeping the
    declared dict[str, str] shape means it rides under a sentinel suffix."""
    aux = write(tmp_path, "main.aux", r"\newlabel{tab:x}{{2}{18}{Cap}{table.1}{}}" + "\n")
    labels = load_labels(aux)
    assert labels["tab:x"] == "2"
    assert labels["tab:x@@page"] == "18"


def test_pageref_resolves_to_the_page_number():
    html, unresolved = resolve("<p>on page \\pageref{tab:x}</p>", {"tab:x": "2", "tab:x@@page": "18"})
    assert html == "<p>on page 18</p>"
    assert unresolved == []


def test_pageref_without_a_page_is_reported():
    _, unresolved = resolve("<p>page \\pageref{tab:x}</p>", {"tab:x": "2"})
    assert unresolved == ["tab:x"]


def test_eqref_is_parenthesized():
    html, _ = resolve('<span class="math inline">\\(\\eqref{eq:m}\\)</span>', {"eq:m": "5"})
    assert html == "(5)"


def test_pandocs_anchor_form_is_resolved():
    """Without raw_tex pandoc emits a link instead. Same job, different shape."""
    src = '<a href="#tab:foo" data-reference-type="ref" data-reference="tab:foo">[tab:foo]</a>'
    html, unresolved = resolve(src, {"tab:foo": "2"})
    assert ">2<" in html
    assert "[tab:foo]" not in html
    assert unresolved == []


def test_an_unresolved_ref_is_reported_and_left_visible():
    src = '<p>See <span class="math inline">\\(\\ref{tab:ghost}\\)</span>.</p>'
    html, unresolved = resolve(src, {"tab:other": "1"})
    assert unresolved == ["tab:ghost"]
    # Not dropped, and not left for MathJax to render as "???".
    assert "tab:ghost" in html
    assert "math" not in html
    assert "unresolved" in html


def test_an_unresolved_ref_is_marked_exactly_once():
    """The marker this function emits still contains `\\ref{...}`. Rescanning
    the output would wrap it a second time, nesting the spans and leaving the
    page with two copies of the same warning."""
    src = '<p>See <span class="math inline">\\(\\ref{tab:ghost}\\)</span>.</p>'
    html, _ = resolve(src, {})
    assert html.count("ref-unresolved") == 1
    assert html.count("\\ref{tab:ghost}") == 1


def test_a_resolved_number_is_not_rescanned():
    """A label whose printed number happens to read like LaTeX must not be
    re-examined after substitution."""
    html, unresolved = resolve("<p>\\ref{a}</p>", {"a": "\\ref{b}"})
    assert html == "<p>\\ref{b}</p>"
    assert unresolved == []


def test_unresolved_ids_are_reported_once_each_in_document_order():
    src = "<p>\\ref{b} \\ref{a} \\ref{b}</p>"
    _, unresolved = resolve(src, {})
    assert unresolved == ["b", "a"]


def test_html_without_references_is_returned_untouched():
    src = '<p>Nothing to do here.</p><span class="math inline">\\(x^2\\)</span>'
    html, unresolved = resolve(src, {"tab:x": "2"})
    assert html == src
    assert unresolved == []


def test_resolution_survives_an_empty_label_table():
    """The uncompiled-manuscript case: everything comes back named."""
    src = '<p>Table <span class="math inline">\\(\\ref{tab:a}\\)</span> and \\ref{tab:b}.</p>'
    _, unresolved = resolve(src, {})
    assert unresolved == ["tab:a", "tab:b"]
