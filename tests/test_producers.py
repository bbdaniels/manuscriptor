"""Which .tex files are written by analysis code, and are therefore not editable.

The first cut of this rule was "generated means the host file is not the root
.tex". That is wrong in a way that matters: on estonia-ecm it marks 283 of 384
blocks uneditable, and almost all of them are hand-written prose appendices. The
tool would refuse to edit three quarters of the manuscript it exists to edit.

What the rule is actually trying to express is "editing this would hardcode a
result". So that is what gets tested here.
"""
from __future__ import annotations

from pathlib import Path


from manuscriptor.server import producers


def w(root: Path, rel: str, text: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


PROSE = r"""
\subsection{Conceptual framework}
Effective primary healthcare requires high-quality curative care, but as the
global burden of non-communicable diseases grows, that care increasingly
requires identification of long-term issues between acute episodes.
"""

TABLE = r"""
\begin{tabular}{lcc}
\toprule
Outcome & Control & ECM \\
\midrule
Any chronic consult & 0.412 & 0.483 \\
Statin prescription & 0.094 & 0.122 \\
\bottomrule
\end{tabular}
"""


# ------------------------------------------------------- the producer scan


def test_a_script_naming_its_output_is_found(tmp_path):
    w(tmp_path, "code/07_table2.R", 'esttab(m1, m2, file = "table2_cross.tex")\n')
    out = w(tmp_path, "latex/tables/table2_cross.tex", TABLE)
    found = producers.scan(tmp_path / "latex")
    assert found.get(out.resolve()) == (tmp_path / "code" / "07_table2.R").resolve()


def test_prose_is_not_claimed_by_a_producer(tmp_path):
    w(tmp_path, "code/07_table2.R", 'esttab(m1, file = "table2_cross.tex")\n')
    prose = w(tmp_path, "latex/appendix/a_framework.tex", PROSE)
    assert prose.resolve() not in producers.scan(tmp_path / "latex")


def test_stata_and_python_producers_count_too(tmp_path):
    w(tmp_path, "do/build.do", 'esttab using "tableA1.tex", replace\n')
    w(tmp_path, "scripts/fig.py", 'open("figure3.tex", "w").write(out)\n')
    a = w(tmp_path, "latex/tables/tableA1.tex", TABLE)
    b = w(tmp_path, "latex/tables/figure3.tex", TABLE)
    found = producers.scan(tmp_path / "latex")
    assert a.resolve() in found and b.resolve() in found


# ------------------------- the content test, for producers we cannot find


def test_a_table_fragment_reads_as_generated_without_any_producer(tmp_path):
    """qutub-india builds filenames by concatenation, so no literal ever names
    them. The file itself still has to be recognisable."""
    t = w(tmp_path, "latex/exhibits/t_learning.tex", TABLE)
    assert producers.looks_generated(t) is True


def test_a_bare_value_fragment_reads_as_generated(tmp_path):
    v = w(tmp_path, "latex/exhibits/correct_p2_wb.tex", "0.096")
    assert producers.looks_generated(v) is True


def test_a_prose_appendix_does_not(tmp_path):
    p = w(tmp_path, "latex/appendix/a_framework.tex", PROSE)
    assert producers.looks_generated(p) is False


def test_prose_containing_a_small_table_is_still_prose(tmp_path):
    p = w(tmp_path, "latex/appendix/mixed.tex", PROSE + TABLE + PROSE)
    assert producers.looks_generated(p) is False


# ---------------------------------------------- applying it back to blocks


class FakeBlock:
    """Only the fields apply() reads."""

    def __init__(self, file, kind="paragraph", editable=True):
        self.file, self.kind, self.editable = file, kind, editable

    def _replace(self, **kw):
        b = FakeBlock(self.file, self.kind, self.editable)
        b.__dict__.update(kw)
        return b


def test_apply_frees_prose_and_locks_output(tmp_path):
    root = w(tmp_path, "latex/main.tex", "\\documentclass{article}")
    prose = w(tmp_path, "latex/appendix/a_framework.tex", PROSE)
    table = w(tmp_path, "latex/tables/t2.tex", TABLE)

    bl = (FakeBlock(root), FakeBlock(prose), FakeBlock(table))
    out = producers.apply(bl, producers.scan(tmp_path / "latex"), root_file=root)

    assert out[0].editable is True, "the root file is always editable"
    assert out[1].editable is True, "a hand-written appendix must be editable"
    assert out[2].editable is False, "a generated table must not be"
    assert out[2].kind == "generated"


def test_apply_never_re_enables_a_block_the_segmenter_refused(tmp_path):
    """segment() also marks blocks uneditable when they straddle an include
    boundary and cannot be expressed as one byte range. That refusal is about
    splicing safety, not provenance, and must survive this pass."""
    prose = w(tmp_path, "latex/appendix/a.tex", PROSE)
    root = w(tmp_path, "latex/main.tex", "x")
    bl = (FakeBlock(prose, editable=False),)
    out = producers.apply(bl, {}, root_file=root)
    assert out[0].editable is False
