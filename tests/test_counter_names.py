r"""The exhibit's number in its NAME, not only in its rendering.

`render/refs.py` learned to print `\thefigure` as the number the `.aux` recorded
for the label its `\refstepcounter` carried, and the headings on the page came
right. Everything that NAMES a block kept reading "Figure ." -- the outline
rail, the queue, the ticker, the inspector title -- because those derive from
source text and never went through the resolver. covet-india's supplement is
eight exhibits set free-standing, so its rail was eight entries deep in
"Figure ." and "Table ." with nothing to tell them apart.

The names cannot ask `resolve_source`, which answers for a whole buffer: the
binding lives in the block BEFORE the one that prints it. They ask the same
module for the state in force at an offset, so there is still exactly one thing
in this repo that knows what number TeX gave a label.

The guard the rest of them reduce to: a resolved counter must name a block
exactly as the same heading with the number typed in by hand would. Anything
else is a second numbering scheme.
"""
from __future__ import annotations

from pathlib import Path

from manuscriptor.server import build as build_mod

PREAMBLE = "\\documentclass{article}\n\\renewcommand{\\thefigure}{S\\arabic{figure}}\n"

STEPPED = PREAMBLE + r"""\begin{document}
\section{Supplementary exhibits}

An opening paragraph, here so the document has some prose in it before anything else.

\refstepcounter{figure}\label{s:fig-sampling}
\subsection*{Figure \thefigure. Facility sample and panel retention}

\noindent\includegraphics[width=\textwidth]{sampling.pdf}

\refstepcounter{figure}\label{s:fig-visits}
\subsection*{Figure \thefigure. Visits per facility, by round}

\noindent\includegraphics[width=\textwidth]{visits.pdf}
\end{document}
"""

# The identical document with the numbers typed in, which is what main.tex does
# and what the resolved names must be indistinguishable from.
TYPED = STEPPED.replace(
    "\\refstepcounter{figure}\\label{s:fig-sampling}\n", ""
).replace(
    "\\refstepcounter{figure}\\label{s:fig-visits}\n", ""
).replace("Figure \\thefigure. Facility", "Figure S1. Facility"
          ).replace("Figure \\thefigure. Visits", "Figure S2. Visits")

AUX = ("\\newlabel{s:fig-sampling}{{S1}{2}{}{figure.1}{}}\n"
       "\\newlabel{s:fig-visits}{{S2}{3}{}{figure.2}{}}\n")


def served(tmp_path: Path, body: str = STEPPED, aux: str | None = AUX):
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "main.tex").write_text(body, encoding="utf-8")
    if aux is not None:
        (tmp_path / "main.aux").write_text(aux, encoding="utf-8")
    return build_mod.build(tmp_path)


def names(b) -> list[str]:
    return [rec["label"] for rec in b.blob["blocks"].values() if rec["label"]]


def exhibit_names(b) -> list[str]:
    """What the exhibit bodies answer to: the heading above them, numbered."""
    return [rec["label"] for rec in b.blob["blocks"].values()
            if "includegraphics" in rec["source"]]


def heading_names(b) -> list[str]:
    return [rec["label"] for rec in b.blob["blocks"].values()
            if rec["kind"] == "heading"]


def test_the_outline_rail_prints_the_number(tmp_path):
    b = served(tmp_path)
    texts = [e["text"] for e in b.blob["outline"]]
    assert "Figure S1. Facility sample and panel retention" in texts, texts
    assert "Figure S2. Visits per facility, by round" in texts, texts
    assert not any("Figure ." in t for t in texts), texts


def test_the_block_labels_print_it_too(tmp_path):
    """The queue, the ticker and the inspector title all read this one string."""
    got = names(served(tmp_path))
    assert "Figure S1. Facility sample and panel retention" in got, got
    assert "Figure S2. Visits per facility, by round" in got, got
    assert not any("\\thefigure" in n for n in got), got


def test_the_heading_names_itself_by_its_number_and_not_by_the_section_above(tmp_path):
    r"""Two exhibits under one section have to be two names.

    A heading printing `\thefigure` is markup end to end until the counter
    resolves, so `label()` threw its own words away and fell back to the section
    above it -- and every exhibit in covet-india's supplement answered to
    "Supplementary exhibits". That is the dsp-bias failure exactly: the ticker
    reports an edit landing on one exhibit in language indistinguishable from
    another.

    Clipped to the number's own sentence, which is `_name`'s standing rule for
    any heading that numbers itself: `\subsection{Table 3. IPC}` names itself
    "Table 3." today and is not touched here. A resolved counter must name a
    block exactly as a typed number does and must change nothing else, or the
    fleet's names move under a change that was supposed to be about counters.
    """
    got = heading_names(served(tmp_path))
    assert got == ["Supplementary exhibits", "Figure S1.", "Figure S2."], got


def test_a_resolved_counter_names_a_block_as_a_typed_number_would(tmp_path):
    """One numbering scheme. If these two disagree there are two."""
    stepped = served(tmp_path / "a", STEPPED)
    typed = served(tmp_path / "b", TYPED, aux=None)
    assert [e["text"] for e in stepped.blob["outline"]] == \
           [e["text"] for e in typed.blob["outline"]]
    assert exhibit_names(stepped) == exhibit_names(typed) == [
        "Figure S1. Facility sample and panel retention",
        "Figure S2. Visits per facility, by round",
    ]
    assert heading_names(stepped) == heading_names(typed)


def test_a_manuscript_with_no_aux_is_named_exactly_as_it_was(tmp_path):
    """Nothing is invented. No compile means no bindings means no numbers.

    This is the mutation check on all of the above: a resolver that filled in
    `??`, or one that counted the `\\refstepcounter`s itself instead of reading
    what TeX wrote, would pass every test above and fail this one.
    """
    b = served(tmp_path, STEPPED, aux=None)
    texts = [e["text"] for e in b.blob["outline"]]
    assert "Figure . Facility sample and panel retention" in texts, texts
    assert not any("??" in t for t in texts), texts
    assert not any("S1" in n or "??" in n for n in names(b)), names(b)


def test_the_editor_is_still_given_the_bytes_and_not_the_number(tmp_path):
    r"""A resolved number names a block. It is never what a save round-trips.

    The record's `source` is what the inspector shows, what the drain hands a
    worker as the text to rewrite, and what comes back through `splice`. A
    number arriving there would be written over `\thefigure` on the first save,
    hardcoding into the manuscript a number TeX computes -- the same defect as
    typing a coefficient into a results sentence, and permanent.
    """
    b = served(tmp_path)
    src = "".join(rec["source"] for rec in b.blob["blocks"].values())
    assert "\\thefigure" in src, "the block's own bytes stopped being its source"
    assert "Figure S1." not in src, "a resolved number reached the editable text"


# A block inherits the heading ABOVE it, so the heading's number has to be read
# where the HEADING is. Reading it at the child's offset means every
# `\refstepcounter` between the two is already in force, and the child answers
# to the next exhibit's number under the previous exhibit's words -- a confident
# wrong number, where the unresolved macro at least showed itself.

STRAY = PREAMBLE + r"""\begin{document}

\refstepcounter{figure}\label{s:fig-one}
\subsection*{Figure \thefigure. The first exhibit}

Some notes about the first exhibit, long enough to be a block of its own here.

\refstepcounter{figure}\label{s:fig-two}

A stray note that sits between the counter step and the heading it belongs to.

\subsection*{Figure \thefigure. The second exhibit}

Some notes about the second exhibit, also long enough to be its own block here.
\end{document}
"""

STRAY_AUX = ("\\newlabel{s:fig-one}{{S1}{1}{}{figure.1}{}}\n"
             "\\newlabel{s:fig-two}{{S2}{2}{}{figure.2}{}}\n")


def test_a_block_is_named_by_its_headings_number_and_not_by_a_later_one(tmp_path):
    r"""The stray note sits under "Figure S1." and after `\refstepcounter` two."""
    b = served(tmp_path, STRAY, STRAY_AUX)
    stray = [rec for rec in b.blob["blocks"].values()
             if rec["source"].startswith("A stray note")]
    assert len(stray) == 1, [r["source"][:40] for r in b.blob["blocks"].values()]
    assert stray[0]["parent_heading"] == "Figure S1. The first exhibit", (
        "the heading was numbered at the CHILD's offset, so a counter step "
        f"between the two renamed it: {stray[0]['parent_heading']!r}")
    assert stray[0]["label"].startswith("A stray note"), stray[0]["label"]


def test_the_exhibit_bodies_still_answer_to_their_own_headings(tmp_path):
    """The same document read from the other end: nothing inherits a wrong number."""
    b = served(tmp_path, STRAY, STRAY_AUX)
    got = {rec["source"][:32]: rec["parent_heading"]
           for rec in b.blob["blocks"].values() if rec["parent_heading"]}
    assert got.get("Some notes about the first exhib") == "Figure S1. The first exhibit", got
    assert got.get("Some notes about the second exhi") == "Figure S2. The second exhibit", got


def test_a_typed_number_is_never_touched(tmp_path):
    """main.tex numbers its exhibits by hand, and a build must not rewrite it."""
    b = served(tmp_path, TYPED, aux=AUX)
    texts = [e["text"] for e in b.blob["outline"]]
    assert "Figure S1. Facility sample and panel retention" in texts, texts
    assert (tmp_path / "main.tex").read_text(encoding="utf-8") == TYPED
