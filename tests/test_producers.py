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


# ------------------------------------------- where the code is allowed to live
#
# `_CODE_DIRS` is not a guess at what a script looks like -- `_CODE_SUFFIXES`
# answers that -- it is the list of subdirectories `scan` is willing to RECURSE
# into. Everything at the top level of the repo and of the manuscript directory
# is read regardless. So a gap in the list is invisible: the scan runs, finds
# the top-level scripts, and silently misses a whole directory of producers.
#
# covet-india keeps eleven Python producers in `py/`, which was not on the list.
# `do/` was, which is why its two Stata outputs were correctly refused while ten
# generated `exhibits/*-note.tex` files -- f-strings carrying twenty to thirty
# interpolated estimates each -- stayed editable on a live manuscript.


def test_a_producer_in_a_python_directory_is_found(tmp_path):
    """The bug. `py/` is as ordinary a name for Python as `do/` is for Stata."""
    s = w(tmp_path, "py/fig1_rounds_cases.py",
          'NOTE_OUT = "f1-rounds-cases-note.tex"\n'
          'NOTE_OUT.write_text(note)\n')
    note = w(tmp_path, "manuscript/exhibits/f1-rounds-cases-note.tex", TABLE)
    assert producers.scan(tmp_path / "manuscript").get(note.resolve()) == s.resolve()


def test_code_directories_match_whatever_their_case(tmp_path):
    """`Analysis/` and `PY/` house the same code `analysis/` and `py/` do.

    macOS hides this: its filesystem is case-insensitive, so `root / "R"` finds
    a directory named `r` here and finds nothing on the Linux box that runs the
    same manuscript. A case-sensitive list is a portability bug that cannot be
    reproduced on the machine it is written on.
    """
    w(tmp_path, "Analysis/mk.R", 'writeLines(rows, "tab_upper.tex")\n')
    w(tmp_path, "PY/mk.py", 'open("tab_shouty.tex", "w").write(out)\n')
    a = w(tmp_path, "latex/tables/tab_upper.tex", TABLE)
    b = w(tmp_path, "latex/tables/tab_shouty.tex", TABLE)
    found = producers.scan(tmp_path / "latex")
    assert a.resolve() in found and b.resolve() in found


def test_a_directory_that_is_not_code_is_not_walked(tmp_path):
    """The list is a boundary, not decoration.

    An unbounded walk would reach `ado/` -- third-party Stata packages, which
    write files constantly and own none of this manuscript's exhibits.
    """
    w(tmp_path, "ado/plus/e/estout.ado", 'file write handle "tableA1.tex"\n')
    w(tmp_path, "ado/plus/e/helper.do", 'esttab using "tableA1.tex", replace\n')
    t = w(tmp_path, "latex/tables/tableA1.tex", TABLE)
    assert t.resolve() not in producers.scan(tmp_path / "latex")


# --------------------------------- reading a .tex is not producing it

# dsp-bias's `paper/make_word_submission.py`, reduced to the four lines that
# matter. It READS main.tex and WRITES main_anonymous.tex, and the first cut of
# `scan` claimed both, because it looked for the filename and not for what was
# being done to it.
BLINDER = '''\
import os
PAPER = os.path.dirname(__file__)


def main():
    src = os.path.join(PAPER, "main.tex")
    anon_tex = os.path.join(PAPER, "main_anonymous.tex")
    anon = anonymize(open(src).read())
    if leaked:
        sys.exit("unterminated \\\\author{} block in main.tex")
    open(anon_tex, "w").write(anon)
'''


def test_a_script_that_only_reads_the_manuscript_does_not_own_it(tmp_path):
    """The bug. A Word converter reads main.tex; that is not authorship.

    Claiming it marks every block in the manuscript generated, which is the
    "74% uneditable" failure the invariants name, and it is invisible only for
    as long as the root file has a special case rescuing it."""
    w(tmp_path, "paper/make_word_submission.py", BLINDER)
    main = w(tmp_path, "paper/main.tex", PROSE)
    assert main.resolve() not in producers.scan(tmp_path / "paper")


def test_a_script_that_writes_through_a_variable_still_owns_the_output(tmp_path):
    """The other half, and why the fix cannot just be "ignore top-level .tex".

    `main_anonymous.tex` is a literal bound to a name and written three lines
    later. It IS generated -- editing it loses the edit at the next run -- so
    the write context has to be found through the binding, not only on the line
    the filename appears on."""
    s = w(tmp_path, "paper/make_word_submission.py", BLINDER)
    anon = w(tmp_path, "paper/main_anonymous.tex", PROSE)
    assert producers.scan(tmp_path / "paper").get(anon.resolve()) == s.resolve()


def test_a_filename_inside_a_message_is_not_a_producer_match(tmp_path):
    """`sys.exit("...block in main.tex")` names the file in prose, not in code."""
    w(tmp_path, "code/check.py", 'sys.exit("no author block in main.tex")\n')
    main = w(tmp_path, "latex/main.tex", PROSE)
    assert main.resolve() not in producers.scan(tmp_path / "latex")


def test_a_progress_line_announcing_a_path_does_not_claim_it(tmp_path):
    """The hard version of the same rule: the message sits inside a statement
    that really does write, and really does end in a real basename. Only the
    space in front of it says it is prose."""
    w(tmp_path, "R/99_log.R",
      'cat("wrote tables/tableA1.tex", file = logfile, append = TRUE)\n')
    t = w(tmp_path, "latex/tables/tableA1.tex", TABLE)
    assert t.resolve() not in producers.scan(tmp_path / "latex")


def test_a_write_verb_that_is_only_in_a_comment_writes_nothing(tmp_path):
    """The literal is live code; the only verb beside it is commented out."""
    w(tmp_path, "R/20_attrition.R",
      'out_path <- file.path(d, "tab_x.tex")  # writeLines() happens in 21_export.R\n')
    t = w(tmp_path, "manuscript/exhibits/tab_x.tex", TABLE)
    assert t.resolve() not in producers.scan(tmp_path / "manuscript")


def test_r_reading_a_section_is_not_r_writing_it(tmp_path):
    w(tmp_path, "code/lint.R", 'txt <- readLines("intro.tex")\ncheck(txt)\n')
    intro = w(tmp_path, "latex/sections/intro.tex", PROSE)
    assert intro.resolve() not in producers.scan(tmp_path / "latex")


def test_r_writelines_owns_its_output(tmp_path):
    s = w(tmp_path, "code/mk.R", 'out <- "t_main.tex"\nwriteLines(rows, out)\n')
    t = w(tmp_path, "latex/tables/t_main.tex", TABLE)
    assert producers.scan(tmp_path / "latex").get(t.resolve()) == s.resolve()


def test_stata_reading_is_not_stata_writing(tmp_path):
    w(tmp_path, "do/lint.do", 'file open h using "appendix.tex", read\n')
    a = w(tmp_path, "latex/appendix.tex", PROSE)
    assert a.resolve() not in producers.scan(tmp_path / "latex")


def test_stata_using_clauses_still_own_their_outputs(tmp_path):
    s = w(tmp_path, "do/build.do",
          'esttab using "tabA.tex", replace\n'
          'file open h using "tabB.tex", write replace\n'
          'export delimited using "tabC.tex", replace\n')
    for name in ("tabA.tex", "tabB.tex", "tabC.tex"):
        w(tmp_path, f"latex/tables/{name}", TABLE)
    found = producers.scan(tmp_path / "latex")
    for name in ("tabA.tex", "tabB.tex", "tabC.tex"):
        t = (tmp_path / "latex" / "tables" / name).resolve()
        assert found.get(t) == s.resolve(), name


def test_a_scripts_own_wrapper_around_a_write_is_still_a_write(tmp_path):
    """qutub-india's `20_attrition.R` names thirteen exhibits through a helper
    it defines itself, and beside no built-in verb at all."""
    s = w(tmp_path, "R/20_attrition.R",
          'exp_frag <- function(fname, value) {\n'
          '  write_frag(value, file.path(exh_dir, fname))\n'
          '}\n'
          'exp_frag("attr_n_base.tex", as.character(B_C + B_T))\n')
    t = w(tmp_path, "manuscript/exhibits/attr_n_base.tex", "1204")
    assert producers.scan(tmp_path / "manuscript").get(t.resolve()) == s.resolve()


def test_a_helper_that_only_reads_does_not_lend_its_callers_a_write(tmp_path):
    """The same hop must not run in reverse: `20_attrition.R` also reads two
    fragments another script owns, to check they still agree."""
    # The helper must NOT be named `read...`, or the call site reads as a read
    # on its own and the hop is never asked the question.
    w(tmp_path, "R/20_attrition.R",
      'frag_value <- function(path) {\n'
      '  as.numeric(readLines(path)[1])\n'
      '}\n'
      'v <- frag_value(file.path(exh_dir, "robust_ipw_b3.tex"))\n')
    t = w(tmp_path, "manuscript/exhibits/robust_ipw_b3.tex", "0.031")
    assert t.resolve() not in producers.scan(tmp_path / "manuscript")


def test_a_fragment_named_only_in_a_comment_has_no_producer(tmp_path):
    # Two shapes, and they are defended separately. A commented-out write is a
    # statement that carries no verb once the comment is blanked; a note beside
    # a LIVE write sits in a statement whose verb is real, so only the offset of
    # the literal itself distinguishes the two names.
    w(tmp_path, "R/09_frags.R",
      '# writeLines(hi, file.path(exh_dir, "correct_b3_ci_hi.tex"))  # superseded\n'
      'writeLines(lo, file.path(exh_dir, "correct_b3_ci_lo.tex"))  # was "b3_old.tex"\n')
    hi = w(tmp_path, "manuscript/exhibits/correct_b3_ci_hi.tex", "0.21")
    old = w(tmp_path, "manuscript/exhibits/b3_old.tex", "0.07")
    lo = w(tmp_path, "manuscript/exhibits/correct_b3_ci_lo.tex", "0.08")
    found = producers.scan(tmp_path / "manuscript")
    assert hi.resolve() not in found, "a commented-out write writes nothing"
    assert old.resolve() not in found, "a note beside a live write writes nothing"
    assert lo.resolve() in found


def test_writes_inside_a_block_are_all_found_not_just_the_last(tmp_path):
    """Counting braces as if they were parentheses swallowed an entire R
    `if/else` into one statement, and eleven of qutub-india's twelve fragments
    in that block stopped being claimed."""
    # The block has to contain a read as well as writes, which is the shape
    # qutub-india has: merged into one statement it reads as `copy(src, dst)`
    # and only the LAST name in it survives.
    s = w(tmp_path, "R/09_frags.R",
          'if (file.exists(src)) {\n'
          '  base <- readLines(src)\n'
          '  writeLines(a, file.path(d, "frag_a.tex"))\n'
          '  writeLines(b, file.path(d, "frag_b.tex"))\n'
          '} else {\n'
          '  writeLines(c, file.path(d, "frag_c.tex"))\n'
          '}\n')
    for n in ("frag_a.tex", "frag_b.tex", "frag_c.tex"):
        w(tmp_path, f"manuscript/exhibits/{n}", "0.5")
    found = producers.scan(tmp_path / "manuscript")
    for n in ("frag_a.tex", "frag_b.tex", "frag_c.tex"):
        assert found.get((tmp_path / "manuscript/exhibits" / n).resolve()) == s.resolve(), n


def test_a_stata_command_continued_over_lines_still_writes(tmp_path):
    """estonia-qbs writes three of its seven exhibits with the filename on a
    different physical line from the `esttab` that writes it."""
    s = w(tmp_path, "do/analysis.do",
          'esttab reg1 reg2 ///\n'
          '  using "${exhibits}/T5-shrinkage.tex" , replace ///\n'
          '  booktabs\n')
    t = w(tmp_path, "manuscript/exhibits/T5-shrinkage.tex", TABLE)
    assert producers.scan(tmp_path / "manuscript").get(t.resolve()) == s.resolve()


def test_the_read_only_manuscript_stays_editable_end_to_end(tmp_path):
    """The symptom, not just the map: prose a converter reads must stay editable
    without needing to be the root file."""
    w(tmp_path, "paper/make_word_submission.py", BLINDER)
    root = w(tmp_path, "paper/supplement.tex", "\\documentclass{article}")
    main = w(tmp_path, "paper/main.tex", PROSE)

    out = producers.apply((FakeBlock(main),), producers.scan(tmp_path / "paper"),
                          root_file=root)
    assert out[0].editable is True
    assert out[0].kind != "generated"


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


def test_the_root_file_is_exempt_from_the_content_guess_only(tmp_path):
    """A root that is a preamble and a column of `\\input` lines has no
    sentences in it, and `looks_generated` would call that a fragment. The
    exemption stops there: a producer that really writes the served file still
    refuses it, which is dsp-bias's `main_anonymous.tex`."""
    thin = w(tmp_path, "paper/thin.tex",
             "\\documentclass{article}\n\\begin{document}\n\\input{s1}\n\\end{document}")
    assert producers.looks_generated(thin) is True, "the guess alone would refuse it"
    out = producers.apply((FakeBlock(thin),), {}, root_file=thin)
    assert out[0].editable is True

    w(tmp_path, "paper/make_word_submission.py", BLINDER)
    anon = w(tmp_path, "paper/main_anonymous.tex", PROSE)
    out = producers.apply((FakeBlock(anon),), producers.scan(tmp_path / "paper"),
                          root_file=anon)
    assert out[0].editable is False, "being served does not make a generated file editable"
    assert out[0].kind == "generated"


def test_apply_never_re_enables_a_block_the_segmenter_refused(tmp_path):
    """segment() also marks blocks uneditable when they straddle an include
    boundary and cannot be expressed as one byte range. That refusal is about
    splicing safety, not provenance, and must survive this pass."""
    prose = w(tmp_path, "latex/appendix/a.tex", PROSE)
    root = w(tmp_path, "latex/main.tex", "x")
    bl = (FakeBlock(prose, editable=False),)
    out = producers.apply(bl, {}, root_file=root)
    assert out[0].editable is False


# ------------------------------------------- provenance beyond the .tex tree
#
# A `.bib` is written by analysis code as surely as a table is, and appending to
# a generated one loses the entry at the next regeneration while the `\citep`
# that needs it survives. The decision is still made HERE and nowhere else.

GENERATED_BIB = """\
%% ==========================================================================
%% sample.bib -- GENERATED FILE. DO NOT EDIT BY HAND.
%%
%% Regenerate: cd manuscript && make bib
%% Generator: manuscript/make-bib.py
%% ==========================================================================

@article{rowe2018effectiveness,
  title = {Effectiveness of strategies},
}
"""

HAND_BIB = """\
@article{rowe2018effectiveness,
  title = {Effectiveness of strategies},
}
"""


def test_a_script_that_writes_a_bib_is_found(tmp_path):
    w(tmp_path, "manuscript/make-bib.py",
      'open("sample.bib", "w").write(body)\n')
    out = w(tmp_path, "manuscript/sample.bib", HAND_BIB)
    found = producers.scan(tmp_path / "manuscript")
    assert found.get(out.resolve()) == (tmp_path / "manuscript" / "make-bib.py").resolve()


def test_a_generated_header_is_provenance_even_with_no_script_in_reach(tmp_path):
    """The generator may not be a file this scan can see -- a Makefile rule, a
    script in another repo. The header the generator wrote is still evidence."""
    bib = w(tmp_path, "manuscript/sample.bib", GENERATED_BIB)
    p = producers.provenance(bib, {})
    assert p.generated is True
    assert p.producer == "manuscript/make-bib.py"
    assert p.remedy == "cd manuscript && make bib"


def test_a_hand_maintained_bib_is_not_claimed(tmp_path):
    bib = w(tmp_path, "manuscript/sample.bib", HAND_BIB)
    p = producers.provenance(bib, {})
    assert p.generated is False
    assert p.producer is None


def test_a_producer_match_outranks_the_header_sniff(tmp_path):
    """The script that names the file is definitive; the header is a second
    signal for the files no scan can claim."""
    w(tmp_path, "manuscript/make-bib.py", 'open("sample.bib", "w").write(body)\n')
    bib = w(tmp_path, "manuscript/sample.bib", HAND_BIB)
    produced = producers.scan(tmp_path / "manuscript")
    p = producers.provenance(bib, produced)
    assert p.generated is True
    assert p.producer == str((tmp_path / "manuscript" / "make-bib.py").resolve())
    assert p.signal == "producer"


def test_a_generated_header_in_a_tex_file_refuses_the_block_too(tmp_path):
    """The same decision, one function, whatever the suffix."""
    root = w(tmp_path, "latex/main.tex", "x")
    frag = w(tmp_path, "latex/appendix/a.tex",
             "% GENERATED FILE -- DO NOT EDIT\n% Generator: code/mk.R\n" + PROSE)
    out = producers.apply((FakeBlock(frag),), {}, root_file=root)
    assert out[0].editable is False
    assert out[0].kind == "generated"
