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

from manuscriptor.render.refs import counter_map, resolve_source

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


# ------------------------------------------------- \thefigure in a heading
#
# An exhibit set free-standing rather than as a float opens with an explicit
# `\refstepcounter{figure}\label{...}` and prints its own number with
# `\thefigure`, because a starred heading steps no counter. Pandoc does not
# execute TeX: it expands the `\renewcommand{\thefigure}{S\arabic{figure}}` it
# was given and drops `\arabic`, so covet-india's supplement headings rendered
# "Figure S." -- the S survived and the number vanished, at exit 0.
#
# The number TeX computed is in the .aux, against that very label, and reading
# it out is what this module is for. `\refstepcounter` is what writes it there,
# so the pairing is not a heuristic: `\label` right after `\refstepcounter{X}`
# records exactly what `\theX` would print until the counter next moves.

STEP_LABELS = {"s:fig-a": "S1", "s:fig-b": "S2", "s:tab-a": "S1"}


def test_a_stepped_counter_prints_the_number_from_the_aux():
    src = ("\\refstepcounter{figure}\\label{s:fig-a}\n"
           "\\subsection*{Figure \\thefigure. Sampling}")
    out, missing = resolve_source(src, STEP_LABELS)
    assert "Figure S1. Sampling" in out
    assert "\\thefigure" not in out
    assert missing == []


def test_each_exhibit_gets_its_own_number():
    src = ("\\refstepcounter{figure}\\label{s:fig-a}\n"
           "\\subsection*{Figure \\thefigure. One}\n\n"
           "\\refstepcounter{figure}\\label{s:fig-b}\n"
           "\\subsection*{Figure \\thefigure. Two}")
    out = resolve_source(src, STEP_LABELS)[0]
    assert "Figure S1. One" in out
    assert "Figure S2. Two" in out


def test_figure_and_table_counters_do_not_cross():
    src = ("\\refstepcounter{table}\\label{s:tab-a}\n"
           "\\subsection*{Table \\thetable. T}\n\n"
           "\\refstepcounter{figure}\\label{s:fig-b}\n"
           "\\subsection*{Figure \\thefigure. F}")
    out = resolve_source(src, STEP_LABELS)[0]
    assert "Table S1. T" in out
    assert "Figure S2. F" in out


def test_the_counter_definition_is_never_rewritten():
    """`\\renewcommand{\\thefigure}{...}` is the definition, not a use."""
    src = ("\\renewcommand{\\thefigure}{S\\arabic{figure}}\n"
           "\\refstepcounter{figure}\\label{s:fig-a}\n"
           "\\subsection*{Figure \\thefigure.}\n"
           "\\renewcommand{\\thefigure}{X}")
    out = resolve_source(src, STEP_LABELS)[0]
    assert out.count("\\renewcommand{\\thefigure}") == 2
    assert "Figure S1." in out


def test_a_counter_the_aux_does_not_carry_is_reported():
    src = "\\refstepcounter{figure}\\label{s:fig-nope}\n\\subsection*{Figure \\thefigure.}"
    out, missing = resolve_source(src, STEP_LABELS)
    assert "s:fig-nope" in missing
    assert "\\thefigure" not in out, "a bare \\the macro must not reach pandoc"
    assert "??" in out


def test_an_unbound_counter_is_left_alone():
    """No `\\refstepcounter\\label` in scope means nothing here knows the number.

    Inventing one would be worse than pandoc's own guess, so the macro is left
    exactly as it was and the renderer keeps whatever it made of it.
    """
    src = "\\subsection*{Figure \\thefigure. Orphan}"
    out, missing = resolve_source(src, STEP_LABELS)
    assert out == src
    assert missing == []


def test_a_float_ends_the_binding():
    """`\\caption` inside a float steps the counter, and this module cannot
    follow that. Past a float the printed number is unknown, so it is not
    guessed."""
    src = ("\\refstepcounter{figure}\\label{s:fig-a}\n"
           "\\subsection*{Figure \\thefigure. Free-standing}\n"
           "\\begin{figure}\\caption{A float}\\end{figure}\n"
           "\\subsection*{Figure \\thefigure. After the float}")
    out = resolve_source(src, STEP_LABELS)[0]
    assert "Figure S1. Free-standing" in out
    assert "Figure \\thefigure. After the float" in out


def test_a_refstepcounter_without_a_label_binds_nothing():
    src = "\\refstepcounter{figure}\n\\subsection*{Figure \\thefigure.}"
    out = resolve_source(src, STEP_LABELS)[0]
    assert out == src


# ---------------------------------------- the same number, one block at a time
#
# `resolve_source` answers for the whole buffer at once, which is all the
# renderer needs. Naming a block cannot ask that way: the binding lives in the
# PREVIOUS block, because `\refstepcounter{figure}\label{k}` is its own block
# and the heading that prints `\thefigure` is the next one. So the number has to
# be askable at an OFFSET -- and refs.py stays the only thing that answers "what
# number did TeX give this", rather than a second scanner growing in blocks.py.

DOC = ("\\refstepcounter{figure}\\label{s:fig-a}\n\n"
       "\\subsection*{Figure \\thefigure. One}\n\n"
       "\\refstepcounter{figure}\\label{s:fig-b}\n\n"
       "\\subsection*{Figure \\thefigure. Two}\n")


def _heading(n: int) -> tuple[str, int]:
    """The n-th heading of DOC, and where it starts."""
    at = -1
    for _ in range(n):
        at = DOC.index("\\subsection", at + 1)
    return DOC[at:DOC.index("}", at) + 1], at


def test_a_binding_in_an_earlier_block_reaches_a_later_one():
    text, at = _heading(1)
    out = counter_map(DOC, STEP_LABELS).render(text, at)
    assert out == "\\subsection*{Figure S1. One}"


def test_the_state_is_the_one_in_force_where_the_block_starts():
    """Not the document's first binding, and not its last."""
    cmap = counter_map(DOC, STEP_LABELS)
    assert cmap.render(*_heading(2)) == "\\subsection*{Figure S2. Two}"
    assert cmap.render(*_heading(1)) == "\\subsection*{Figure S1. One}"


def test_a_block_carrying_its_own_step_needs_no_help():
    src = "\\refstepcounter{figure}\\label{s:fig-b}\\subsection*{Figure \\thefigure.}"
    out = counter_map(src, STEP_LABELS).render(src, 0)
    assert out == "\\refstepcounter{figure}\\label{s:fig-b}\\subsection*{Figure S2.}"


def test_a_float_between_the_binding_and_the_block_ends_it():
    src = ("\\refstepcounter{figure}\\label{s:fig-a}\n\n"
           "\\begin{figure}\\caption{A float}\\end{figure}\n\n"
           "\\subsection*{Figure \\thefigure. After}")
    at = src.index("\\subsection")
    assert counter_map(src, STEP_LABELS).render(src[at:], at) == src[at:]


def test_without_an_aux_the_macro_is_left_exactly_as_it_was():
    """A name is not a rendering.

    `resolve_source` prints `??` for a label the .aux does not carry, because
    the reader has to see that a number failed. A NAME has no such duty and no
    room to say it: `??` in the outline rail would be a worse answer than the
    macro's own absence, and every block of a manuscript that has never been
    compiled would carry one.
    """
    text, at = _heading(1)
    assert counter_map(DOC, {}).render(text, at) == text
    assert "??" not in counter_map(DOC, {}).render(DOC, 0)
