"""Compiling the manuscript to PDF and to Word.

Two buttons in the toolbar promised this and did nothing, which is worse than
not offering it: a control that lies sits beside controls that work and the
author cannot tell which is which until he has waited.

Four things are load-bearing here and each has a test that has been watched
failing.

**A compile must not make `git status` grow.** The same rule the build directory
already follows. LaTeX writes `.aux`, `.log`, `.out`, `.bbl` and the PDF itself,
and the reference manuscript has several of those TRACKED, so a compile in the
manuscript directory rewrites files the author has committed. Everything goes to
`build/manuscriptor/compile`, which is covered by the `.gitignore` the build
directory writes for itself.

**A `\\include` in a subdirectory needs that subdirectory to exist under the
output directory.** `\\include{tables/t}` makes TeX open `tables/t.aux` for
writing, relative to the output directory, and TeX does not create the folder:
it stops with `I can't write on file`. Found by running it, not by reading.

**Exit status is not the answer to "did it compile".** pdflatex exits 1 on a
recoverable error while writing a perfectly good PDF. The reference manuscript
does exactly this: twelve `Missing } inserted` and 119 pages of output. Success
is a PDF this run wrote.

**The manuscript is never written to.** A compile reads; the only bytes it lays
down are inside the build directory.
"""
from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path

import pytest

from manuscriptor.server import paths
from manuscriptor.server import compile as compile_mod
from manuscriptor.server import pagination
from tests import minipdf


HAS_LATEX = shutil.which("pdflatex") is not None
HAS_PANDOC = shutil.which("pandoc") is not None
HAS_TEXTUTIL = shutil.which("textutil") is not None
HAS_SKILL = compile_mod.SKILL_DIR.is_dir()


MAIN = r"""\documentclass{article}
\usepackage{array}
\begin{document}
A paragraph of prose that cites \cite{smith2020} and is long enough to be real.

\include{tables/t}

See Table~\ref{tab:one} for the numbers.

\bibliographystyle{plain}
\bibliography{refs}
\end{document}
"""

# The first line is verbatim what the reference manuscript's own tables open
# with, and it is the construct that decides this feature. Pandoc cannot read a
# parameterised `\newcolumntype`: it aborts the WHOLE document with
# `unexpected #1`. `normalize_for_pandoc` strips it -- but only if it can see
# it, and it can only see it once the include has been flattened. This is the
# one place the skill's recipe cannot be followed literally.
TABLE = r"""\newcolumntype{L}[1]{>{\raggedright\arraybackslash}p{#1}}
\begin{table}[h]
\caption{A small table}\label{tab:one}
\begin{tabular}{L{2cm}r}
\hline
alpha & 1 \\
beta & 2 \\
\hline
\end{tabular}
\end{table}
"""

# A header two rows deep, which is what 31 of the corpus's 44 rendered tables
# have. Pandoc promotes at most one row into `<thead>`, so the page needs the
# rows marked in the LaTeX -- and the Word file needs that marking gone.
HEADER_TABLE = r"""\begin{table}[h]
\caption{A small table}\label{tab:one}
\begin{tabular}{lcc}
\toprule
 & \multicolumn{2}{c}{Group} \\
Name & A & B \\
\midrule
alpha & 1 & 2 \\
\bottomrule
\end{tabular}
\end{table}
"""

TITLE = "\\title{A Title}\n\\author{A. Author}\n"

BIB = """@article{smith2020,
  author = {Jane Smith},
  title = {A title worth citing},
  journal = {A journal},
  year = {2020},
}
"""


def tiny(tmp_path: Path, *, table: str = TABLE, title: bool = False) -> Path:
    """A manuscript small enough to compile in a test and shaped like a real one.

    The `\\include` into a subdirectory is the whole point of the fixture: it is
    the construct that fails without the mirrored output tree, and the reference
    manuscript pulls every table in that way. `table` and `title` swap in the
    constructs the front matter and the header marking act on.
    """
    d = tmp_path / "latex"
    (d / "tables").mkdir(parents=True)
    main = MAIN
    if title:
        main = main.replace("\\begin{document}\n", "\\begin{document}\n\\maketitle\n")
        main = main.replace("\\usepackage{array}\n", "\\usepackage{array}\n" + TITLE)
    (d / "main.tex").write_text(main, encoding="utf-8")
    (d / "tables" / "t.tex").write_text(table, encoding="utf-8")
    (d / "refs.bib").write_text(BIB, encoding="utf-8")
    return d


def git_repo(d: Path) -> None:
    """Put the manuscript under git, with everything in it committed."""
    run = lambda *a: subprocess.run(a, cwd=str(d), capture_output=True, text=True)
    run("git", "init", "-q")
    run("git", "config", "user.email", "t@t")
    run("git", "config", "user.name", "t")
    run("git", "add", "-A")
    run("git", "commit", "-qm", "in")


def git_status(d: Path) -> str:
    return subprocess.run(
        ["git", "status", "--porcelain"], cwd=str(d), capture_output=True, text=True
    ).stdout.strip()


# ------------------------------------------------------------ where it writes


def test_output_goes_under_the_build_directory(tmp_path):
    d = tiny(tmp_path)
    out = compile_mod.out_dir(d)
    assert out == paths.compile_dir(d)
    assert out.is_dir()


def test_the_build_directory_hides_itself_from_git(tmp_path):
    """Serving a paper must never make `git status` grow, and neither may
    compiling one. The reference manuscript has `main.aux`, `main.log` and
    `main.bbl` COMMITTED, so writing beside the source would rewrite tracked
    files."""
    d = tiny(tmp_path)
    git_repo(d)
    assert git_status(d) == ""
    compile_mod.out_dir(d)
    (compile_mod.out_dir(d) / "main.pdf").write_bytes(b"%PDF-1.4\n")
    assert git_status(d) == ""


def test_a_subdirectory_holding_tex_is_mirrored(tmp_path):
    """`\\include{tables/t}` opens `tables/t.aux` under the output directory for
    writing. TeX will not create that folder; it stops dead."""
    d = tiny(tmp_path)
    (d / "appendix").mkdir()
    (d / "appendix" / "a.tex").write_text("x\n", encoding="utf-8")
    (d / "figures").mkdir()
    (d / "figures" / "f.png").write_bytes(b"x")
    out = compile_mod.out_dir(d)
    compile_mod.mirror_tex_dirs(d, out)
    assert (out / "tables").is_dir()
    assert (out / "appendix").is_dir()
    assert not (out / "figures").exists(), "only directories holding .tex need mirroring"


def test_mirroring_does_not_walk_into_the_build_directory(tmp_path):
    d = tiny(tmp_path)
    out = compile_mod.out_dir(d)
    (out / "stale").mkdir()
    (out / "stale" / "x.tex").write_text("x\n", encoding="utf-8")
    compile_mod.mirror_tex_dirs(d, out)
    assert not (out / "build").exists()


# ------------------------------------------------------------------- engine


def test_a_plain_article_compiles_with_pdflatex():
    assert compile_mod.engine_for("\\documentclass{article}\n") == "pdflatex"


def test_fontspec_means_xelatex():
    assert compile_mod.engine_for(
        "\\documentclass{article}\n\\usepackage{fontspec}\n"
    ) == "xelatex"


def test_the_author_saying_so_beats_the_packages():
    """`% !TEX program = xelatex` is the author declaring the engine. A guess
    read off the package list must not override a statement."""
    assert compile_mod.engine_for(
        "% !TEX program = xelatex\n\\documentclass{article}\n"
    ) == "xelatex"
    assert compile_mod.engine_for(
        "% !TEX program = pdflatex\n\\usepackage{fontspec}\n"
    ) == "pdflatex"


# -------------------------------------------------------------- the error line


def test_the_first_error_carries_its_file_and_line():
    log = (
        "[15] (./tables/t.tex\n"
        "[16]\n"
        "./tables/t.tex:114: Missing } inserted.\n"
        "<inserted text>\n"
        "                }\n"
        "l.114 \\end{tablenotes}\n"
    )
    assert compile_mod.first_error(log) == "tables/t.tex:114: Missing } inserted."


def test_a_clean_log_has_no_error():
    assert compile_mod.first_error("[1] [2] Output written on main.pdf (2 pages).\n") is None


def test_an_error_without_a_file_line_still_reads():
    """`-file-line-error` covers most of them; a missing file is reported by TeX
    before it has a line to blame, and dropping it would leave the author with a
    failed compile and nothing to look at."""
    log = "! LaTeX Error: File `nosuch.sty' not found.\n\nType X to quit.\n"
    got = compile_mod.first_error(log)
    assert got and "nosuch.sty" in got


def test_texs_own_epitaph_is_not_the_error():
    """A missing file is the real case. TeX says what is wrong FIRST, with no
    file prefix, and only then stops with `Emergency stop.` on a line that
    carries a file and a number and no information. Reading the shape rather
    than the meaning would report the epitaph and throw away the diagnosis."""
    log = (
        "No file main.aux.\n"
        "\n"
        "! LaTeX Error: File `nosuchfile.tex' not found.\n"
        "\n"
        "Type X to quit or <RETURN> to proceed,\n"
        "./main.tex:3: Emergency stop.\n"
        "./main.tex:3:  ==> Fatal error occurred, no output PDF file produced!\n"
    )
    got = compile_mod.first_error(log)
    assert got and "nosuchfile.tex" in got
    assert "Emergency stop" not in got


def test_an_epitaph_is_used_when_it_is_all_there_is():
    log = "./main.tex:9: Emergency stop.\n"
    assert compile_mod.first_error(log) == "main.tex:9: Emergency stop."


def test_the_error_reported_is_the_one_that_stopped_it():
    """The reference manuscript compiles to 119 pages WITH twelve `Missing }
    inserted` in its esttab tables. Break something else and it dies, and the
    first error is still one of the twelve: the panel then names a table that
    had nothing to do with it. Found by breaking the demo in a browser."""
    log = (
        "./tables/table1_balance_patient_pre.tex:114: Missing } inserted.\n"
        "l.114 \\end{tablenotes}\n"
        "./tables/table2_cross.tex:108: Missing } inserted.\n"
        "\n"
        "! LaTeX Error: File `a_file_that_does_not_exist.tex' not found.\n"
        "\n"
        "./appendix/a_conceptual_framework.tex:1: Emergency stop.\n"
        "l.1 \\input{a_file_that_does_not_exist}\n"
        "./appendix/a_conceptual_framework.tex:1:  ==> Fatal error occurred.\n"
    )
    got = compile_mod.stopping_error(log)
    assert got and "a_file_that_does_not_exist" in got
    assert "table1" not in got
    # and the first error is still the first error, which is a different question
    assert "table1" in compile_mod.first_error(log)


def test_with_nothing_fatal_the_first_error_is_the_answer():
    """`No pages of output` leaves no epitaph: nothing stopped the run, it just
    had nothing to typeset."""
    log = "./main.tex:3: Undefined control sequence.\nl.3 \\nosuchmacro\nNo pages of output.\n"
    assert compile_mod.stopping_error(log) == "main.tex:3: Undefined control sequence."


def test_the_context_line_belongs_to_the_error_being_reported():
    log = (
        "./tables/t.tex:114: Missing } inserted.\n"
        "l.114 \\end{tablenotes}\n"
        "! LaTeX Error: File `gone.tex' not found.\n"
        "l.1 \\input{gone}\n"
        "./main.tex:1: Emergency stop.\n"
    )
    assert compile_mod.error_context(log, near=compile_mod.stopping_error(log)) == "l.1 \\input{gone}"


def test_a_missing_bibliography_style_is_reported():
    log = "I couldn't open style file aea.bst\n---line 4 of file main.aux\n"
    got = compile_mod.first_error(log)
    assert got and "aea.bst" in got


# -------------------------------------------------- success is a file, not a code


def _fake_runner(exits, *, writes=None, pdf=None):
    """A stand-in for the subprocess call, so the orchestration can be tested
    without waiting forty seconds for TeX."""
    calls = []

    def run(cmd, *, cwd, env=None):
        calls.append(cmd)
        if pdf is not None and len(calls) == 1:
            pdf.parent.mkdir(parents=True, exist_ok=True)
            # A REAL PDF, because the pagination gate reads the finished file
            # and a stand-in nothing can parse is a failed compile now -- which
            # is the point of the gate, so it is not weakened to suit a fixture.
            minipdf._pdf(pdf, ["1/2", "2/2"])
        return exits, (writes or "")

    run.calls = calls
    return run


def test_a_pdf_written_with_a_nonzero_exit_is_a_success(tmp_path):
    """pdflatex exits 1 on a recoverable error while writing a good PDF. The
    reference manuscript does exactly that: twelve of them, 119 pages."""
    d = tiny(tmp_path)
    pdf = compile_mod.out_dir(d) / "main.pdf"
    res = compile_mod.compile_pdf(d, runner=_fake_runner(1, pdf=pdf))
    assert res.ok
    assert res.output == pdf


def test_no_pdf_with_a_zero_exit_is_a_failure(tmp_path):
    d = tiny(tmp_path)
    res = compile_mod.compile_pdf(d, runner=_fake_runner(0, writes="./main.tex:3: Undefined control sequence."))
    assert not res.ok
    assert res.error and "main.tex:3" in res.error


def test_a_stale_pdf_from_a_previous_run_is_not_a_success(tmp_path):
    """The output directory persists between compiles. A run that produces
    nothing must not be able to claim the last run's PDF."""
    d = tiny(tmp_path)
    pdf = compile_mod.out_dir(d) / "main.pdf"
    pdf.write_bytes(b"%PDF-1.4\nold\n")
    res = compile_mod.compile_pdf(d, runner=_fake_runner(1, writes="./main.tex:9: Emergency stop."))
    assert not res.ok


# ------------------------------------------------------------------- progress


def test_every_step_is_announced_as_it_finishes(tmp_path):
    """A button that goes quiet for forty seconds reads as broken.

    The pass count is no longer in the name because it is no longer known in
    advance: the passes run until the `.aux` stops changing. This fake writes
    no `.aux` at all, so the loop stops after the second pass and says so."""
    d = tiny(tmp_path)
    pdf = compile_mod.out_dir(d) / "main.pdf"
    seen = []
    compile_mod.compile_pdf(d, runner=_fake_runner(0, pdf=pdf), on_step=seen.append)
    names = [s.name for s in seen]
    assert names == ["pass 1", "bibtex", "pass 2"]
    assert all(s.seconds >= 0 for s in seen)


def test_progress_arrives_before_the_result(tmp_path):
    """Announced AS IT FINISHES, not collected and handed over at the end, or
    the page still shows nothing for the whole compile."""
    d = tiny(tmp_path)
    pdf = compile_mod.out_dir(d) / "main.pdf"
    order = []

    def run(cmd, *, cwd, env=None):
        order.append("ran")
        pdf.parent.mkdir(parents=True, exist_ok=True)
        pdf.write_bytes(b"%PDF-1.4\n")
        return 0, ""

    compile_mod.compile_pdf(d, runner=run, on_step=lambda s: order.append("step"))
    assert order[:4] == ["ran", "step", "ran", "step"]


# ----------------------------------------------------------------- the real thing


@pytest.mark.skipif(not HAS_LATEX, reason="pdflatex is not installed")
def test_a_real_manuscript_compiles_to_a_real_pdf(tmp_path):
    d = tiny(tmp_path)
    git_repo(d)
    res = compile_mod.compile_pdf(d)
    assert res.ok, res.error
    assert res.output.exists()
    assert res.output.read_bytes()[:5] == b"%PDF-"
    # The include's own aux landed in the mirrored subdirectory, which is the
    # proof that the mirroring was both needed and used.
    assert (compile_mod.out_dir(d) / "tables" / "t.aux").exists()
    # The PDF is the deliverable and it is meant to appear; nothing else may.
    # Before delivery existed this assertion read `== ""`, which is the right
    # test for every OTHER byte a compile writes and is still what it checks.
    assert git_status(d).split() == ["??", "main.pdf"], \
        "a compile may add the deliverable and nothing else"


@pytest.mark.skipif(not HAS_LATEX, reason="pdflatex is not installed")
def test_the_bibliography_is_resolved(tmp_path):
    """Three passes around a bibtex, because one pass leaves `[?]` where the
    citation belongs and no bibliography at the end."""
    d = tiny(tmp_path)
    res = compile_mod.compile_pdf(d)
    assert res.ok, res.error
    assert (compile_mod.out_dir(d) / "main.bbl").exists()
    text = subprocess.run(
        ["pdftotext", str(res.output), "-"], capture_output=True, text=True
    ).stdout
    assert "Smith" in text, "the bibliography did not make it into the PDF"
    assert "Table 1" in text, "the cross-reference did not resolve"


@pytest.mark.skipif(not HAS_LATEX, reason="pdflatex is not installed")
def test_compiling_changes_nothing_and_leaves_only_the_deliverable(tmp_path):
    """The narrowed invariant.

    A compile still may not touch a byte the author wrote, and still may not
    scatter `.aux`, `.log`, `.bbl` or `.blg` beside the source: the reference
    manuscript has several of those COMMITTED, so writing them there rewrites
    tracked files. What changed on 2026-07-27 is that the PDF now comes back
    out, because a deliverable nobody can find is not delivered.
    """
    d = tiny(tmp_path)
    before = {p: p.read_bytes() for p in sorted(d.rglob("*")) if p.is_file()}
    res = compile_mod.compile_pdf(d)
    for p, was in before.items():
        assert p.read_bytes() == was, f"{p} was modified by a compile"

    beside = {p.name for p in d.iterdir() if p.is_file()}
    assert not {n for n in beside if n.endswith((".aux", ".log", ".bbl", ".blg"))}, \
        "the litter must stay in the cache"
    assert "main.pdf" in beside
    assert res.delivered == d / "main.pdf"


@pytest.mark.skipif(not HAS_LATEX, reason="pdflatex is not installed")
def test_a_read_only_manuscript_gets_no_delivered_pdf(tmp_path):
    """`--read-only` promises nothing reaches the filesystem.

    Delivery is the first thing Manuscriptor ever wrote into the manuscript
    directory on purpose, so it is the first thing that could break that
    promise. The compile still runs and the page can still open the result out
    of the cache; only the copy is withheld.
    """
    d = tiny(tmp_path)
    res = compile_mod.compile_pdf(d, deliver_out=False)
    assert res.ok, res.error
    assert res.delivered is None
    assert not (d / "main.pdf").exists()
    assert res.output.exists(), "the page must still have something to show"


LONG = "a_table_with_a_deliberately_long_file_name_so_the_terminal_would_wrap_it"


@pytest.mark.skipif(not HAS_LATEX, reason="pdflatex is not installed")
def test_the_reported_path_is_the_whole_path(tmp_path):
    """TeX wraps its output at 79 columns, the path included, so the reference
    manuscript reported `_pre.tex:114: Missing } inserted.` -- the tail of
    `tables/table1_balance_patient_pre.tex`, and a file that does not exist. An
    author sent looking for it is worse off than one told nothing."""
    d = tiny(tmp_path)
    (d / "tables" / (LONG + ".tex")).write_text("\\undefinedmacrohere\n", encoding="utf-8")
    (d / "main.tex").write_text(
        MAIN.replace("\\include{tables/t}", "\\include{tables/t}\n\\include{tables/" + LONG + "}"),
        encoding="utf-8",
    )
    res = compile_mod.compile_pdf(d)
    said = " ".join(s.detail for s in res.steps) + " " + (res.error or "")
    assert LONG in said, f"the path came back cut: {said!r}"
    assert "tables/" + LONG + ".tex" in said
    # And relative, which is the path the author would type. Watched in a
    # browser first: putting the manuscript on TEXINPUTS makes TeX find its own
    # files through the search path and report every one of them absolute, so
    # the panel filled with eighty characters of home directory.
    assert str(d) not in said, f"the path came back absolute: {said!r}"


@pytest.mark.skipif(not HAS_LATEX, reason="pdflatex is not installed")
def test_a_recoverable_error_does_not_mask_the_fatal_one(tmp_path):
    """The whole-manuscript version of the case above, run through real TeX:
    an error TeX walks past, and then one it cannot."""
    d = tiny(tmp_path)
    (d / "tables" / "t.tex").write_text(TABLE + "\n\\undefinedmacrohere\n", encoding="utf-8")
    (d / "main.tex").write_text(
        MAIN.replace("\\include{tables/t}", "\\include{tables/t}\n\\input{no_such_appendix}"),
        encoding="utf-8",
    )
    res = compile_mod.compile_pdf(d)
    assert not res.ok
    assert "no_such_appendix" in res.error, res.error


@pytest.mark.skipif(not HAS_LATEX, reason="pdflatex is not installed")
def test_a_broken_manuscript_reports_the_latex_error(tmp_path):
    """The one thing an author needs from a failed compile is the line."""
    d = tiny(tmp_path)
    (d / "main.tex").write_text(
        "\\documentclass{article}\n\\begin{document}\n\\nosuchmacro\n\\end{document}\n",
        encoding="utf-8",
    )
    res = compile_mod.compile_pdf(d)
    assert not res.ok
    assert res.error and "main.tex:3" in res.error
    assert "nosuchmacro" in res.error
    assert res.log and res.log.exists()


# ----------------------------------------------------------------------- word


def test_the_skill_is_named_rather_than_reimplemented():
    """The Word conversion delegates to `~/.claude/skills/pandoc-docx`, which
    carries a great deal of hard-won work. What this module owns is the running
    order; the transforms are the skill's."""
    assert compile_mod.SKILL_DIR.name == "pandoc-docx"
    assert compile_mod.SKILL_SCRIPTS["format"].name == "format_docx.py"
    assert compile_mod.SKILL_SCRIPTS["merge_bib"].name == "merge_csl_bib.py"


def test_a_missing_skill_is_said_out_loud_not_worked_around(tmp_path, monkeypatch):
    d = tiny(tmp_path)
    monkeypatch.setattr(compile_mod, "SKILL_DIR", tmp_path / "nowhere")
    monkeypatch.setattr(
        compile_mod, "SKILL_SCRIPTS",
        {k: tmp_path / "nowhere" / v.name for k, v in compile_mod.SKILL_SCRIPTS.items()},
    )
    res = compile_mod.compile_docx(d)
    assert not res.ok
    assert res.error and "pandoc-docx" in res.error


@pytest.mark.skipif(not (HAS_PANDOC and HAS_SKILL), reason="pandoc or the skill is missing")
def test_word_gets_the_tables_the_include_holds(tmp_path):
    """The skill's recipe hands `main.tex` to pandoc. Doing that here converts
    nothing at all: pandoc reaches the `\\def\\sym#1` every esttab table carries
    and aborts the whole document with `unexpected #1`. The flattened source
    goes in instead, normalized on the way, which is the same defense the render
    already uses."""
    d = tiny(tmp_path)
    res = compile_mod.compile_docx(d)
    assert res.ok, res.error
    assert res.output.exists() and res.output.suffix == ".docx"
    body = subprocess.run(
        ["unzip", "-p", str(res.output), "word/document.xml"],
        capture_output=True, text=True,
    ).stdout
    assert "alpha" in body and "beta" in body, "the included table did not survive"


@pytest.mark.skipif(not (HAS_PANDOC and HAS_SKILL), reason="pandoc or the skill is missing")
def test_no_sentinel_survives_into_the_word_file(tmp_path):
    """The page's markings are for the page, and the `.docx` is what a journal
    receives.

    `normalize_for_pandoc` used to mint the title-block tokens and the header
    mark unconditionally, and only the viewer's postprocess stripped them, so
    every Word file the button produced carried literal ⟦MXTHEAD⟧ glyphs inside
    its table header cells -- five of them on one two-row header, and 31 of the
    44 tables in the served corpus trigger that marking. The sweep is on the
    brackets, not on the tokens known today.
    """
    d = tiny(tmp_path, table=HEADER_TABLE, title=True)
    res = compile_mod.compile_docx(d)
    assert res.ok, res.error
    out = paths.compile_dir(d)
    for name in ("inter.tex", "inter.html"):
        text = (out / name).read_text(encoding="utf-8", errors="replace")
        assert "⟦" not in text and "⟧" not in text, f"{name} carries a sentinel"
    body = subprocess.run(
        ["unzip", "-p", str(res.output), "word/document.xml"],
        capture_output=True, text=True,
    ).stdout
    assert "⟦" not in body and "⟧" not in body
    assert "Group" in body and "alpha" in body, "the header table did not survive"


@pytest.mark.skipif(
    not (HAS_PANDOC and HAS_SKILL and HAS_TEXTUTIL), reason="pandoc, the skill or textutil is missing"
)
def test_a_docx_word_will_not_open_is_reported_as_a_failure(tmp_path):
    """The whole reason the skill exists is that a pandoc docx can be a valid
    zip full of well-formed XML that Word refuses to open, so a lenient check
    passes and the author finds out in front of an editor. textutil is the
    strict reader, and this is the gate."""
    d = tiny(tmp_path)

    def blind(cmd, *, cwd, env=None):
        code, out = compile_mod._default_runner(cmd, cwd=cwd, env=env)
        return (code, "") if cmd[0] == "textutil" else (code, out)

    res = compile_mod.compile_docx(d, runner=blind)
    assert not res.ok
    assert res.error and "Word" in res.error


@pytest.mark.skipif(
    not (HAS_PANDOC and HAS_SKILL and HAS_TEXTUTIL), reason="pandoc, the skill or textutil is missing"
)
def test_the_word_file_actually_opens(tmp_path):
    """`textutil` is as strict as Word and python-docx is not, so this is the
    only check that means anything. The skill exists because the naive pandoc
    docx does not open at all."""
    d = tiny(tmp_path)
    res = compile_mod.compile_docx(d)
    assert res.ok, res.error
    info = subprocess.run(
        ["textutil", "-info", str(res.output)], capture_output=True, text=True
    )
    assert info.stdout.strip(), "textutil could not read it, so Word will not either"


# ------------------------------------------------------------------- revealing


def test_reveal_refuses_a_path_outside_the_build_directory(tmp_path):
    """The page asks for this, and the page is not trusted with an arbitrary
    path just because it is served on localhost."""
    d = tiny(tmp_path)
    outside = tmp_path / "secret.pdf"
    outside.write_bytes(b"%PDF-1.4\n")
    with pytest.raises(ValueError):
        compile_mod.reveal(outside, root=d)


def test_reveal_accepts_what_the_compile_produced(tmp_path):
    d = tiny(tmp_path)
    made = compile_mod.out_dir(d) / "main.pdf"
    made.write_bytes(b"%PDF-1.4\n")
    calls = []
    compile_mod.reveal(made, root=d, runner=lambda cmd: calls.append(cmd))
    assert calls and calls[0][:2] == ["open", "-R"]


# -------------------------------------------------------------------- opening
#
# "Open it" is the button beside "Reveal in Finder", and until now it was an
# `<a target="_blank">` -- which the shell's WKWebView cannot honour, because
# opening a second web view needs `WKUIDelegate.createWebViewWith` and the
# shell has no UI delegate at all. The click did nothing and said nothing.
# So it goes through the server, exactly the way revealing already does.


def test_open_refuses_a_path_outside_anything_a_compile_produced(tmp_path):
    """Same reasoning as reveal: served on localhost is not a licence to hand
    the page an arbitrary path and have `open` run on it."""
    d = tiny(tmp_path)
    outside = tmp_path / "secret.pdf"
    outside.write_bytes(b"%PDF-1.4\n")
    with pytest.raises(ValueError):
        compile_mod.open_file(outside, root=d)


def test_open_accepts_what_the_compile_produced(tmp_path):
    d = tiny(tmp_path)
    made = compile_mod.out_dir(d) / "main.pdf"
    made.write_bytes(b"%PDF-1.4\n")
    calls = []
    compile_mod.open_file(made, root=d, runner=lambda cmd: calls.append(cmd))
    assert calls == [["open", str(made.resolve())]]


def test_open_accepts_the_delivered_copy_beside_the_tex(tmp_path):
    """The one the author means. `deliver` copies the finished PDF out of the
    cache and beside the `.tex`, and THAT is the file "open it" should open --
    it outlives `manuscriptor clean` and it is where he already looks for it.
    A gate written only around the cache would refuse it."""
    d = tiny(tmp_path)
    built = compile_mod.out_dir(d) / "main.pdf"
    built.write_bytes(b"%PDF-1.4\n")
    out = compile_mod.deliver(built, d)
    assert out == d / "main.pdf" and out.exists()
    calls = []
    compile_mod.open_file(out, root=d, runner=lambda cmd: calls.append(cmd))
    assert calls == [["open", str(out.resolve())]]


def test_open_says_so_when_the_file_is_gone(tmp_path):
    """A compile from an hour ago, then `manuscriptor clean`. The button must
    not fail silently; the route turns this into a reason the panel prints."""
    d = tiny(tmp_path)
    gone = compile_mod.out_dir(d) / "main.pdf"
    gone.parent.mkdir(parents=True, exist_ok=True)
    with pytest.raises(ValueError) as exc:
        compile_mod.open_file(gone, root=d)
    assert "not there" in str(exc.value)


@pytest.mark.skipif(not HAS_PANDOC, reason="the session needs pandoc to build")
def test_the_open_route_runs_open_and_reports_a_refusal(tmp_path, monkeypatch):
    from aiohttp.test_utils import TestClient, TestServer

    from manuscriptor.server.app import make_app

    session = _session(tmp_path)
    d = session.root
    made = compile_mod.out_dir(d) / "main.pdf"
    made.parent.mkdir(parents=True, exist_ok=True)
    made.write_bytes(b"%PDF-1.4\n")

    calls = []
    monkeypatch.setattr(compile_mod, "_reveal_runner", lambda cmd: calls.append(cmd))

    async def go():
        client = TestClient(TestServer(make_app(session)))
        await client.start_server()
        ok = await client.post("/compile", json={"action": "open", "path": str(made)})
        assert ok.status == 200, await ok.text()
        assert calls == [["open", str(made.resolve())]]

        bad = await client.post(
            "/compile", json={"action": "open", "path": str(tmp_path / "elsewhere.pdf")})
        assert bad.status == 400
        body = await bad.json()
        assert body["error"], "a refusal with no reason is the bug being fixed"
        await client.close()

    asyncio.run(go())


# ---------------------------------------------------------------- the route


def _session(tmp_path):
    from manuscriptor.server.app import Session

    d = tiny(tmp_path)
    return Session(d)


@pytest.mark.skipif(not HAS_PANDOC, reason="the session needs pandoc to build")
def test_the_button_starts_a_compile_and_the_page_hears_about_it(tmp_path, monkeypatch):
    from aiohttp.test_utils import TestClient, TestServer

    from manuscriptor.server.app import make_app

    session = _session(tmp_path)
    sent = []

    async def capture(msg):
        sent.append(msg)

    session.broadcast = capture

    def fake(manuscript_dir, *, main=None, bib=None, on_step=None, **kw):
        on_step(compile_mod.Step("pass 1 of 3", True, 0.1))
        return compile_mod.Result(
            kind="pdf", ok=True, output=compile_mod.out_dir(manuscript_dir) / "main.pdf",
            seconds=0.2, steps=[], error=None, log=None,
        )

    monkeypatch.setattr(compile_mod, "compile_pdf", fake)

    async def go():
        client = TestClient(TestServer(make_app(session)))
        await client.start_server()
        resp = await client.post("/compile", json={"action": "pdf"})
        assert resp.status == 202
        for _ in range(200):
            if any(m.get("phase") == "done" for m in sent):
                break
            await asyncio.sleep(0.01)
        await client.close()

    asyncio.run(go())
    kinds = [(m["type"], m["phase"]) for m in sent if m.get("type") == "compile"]
    assert ("compile", "start") in kinds
    assert ("compile", "step") in kinds
    assert ("compile", "done") in kinds
    done = [m for m in sent if m.get("phase") == "done"][0]
    assert done["ok"] is True
    assert done["output"].endswith("main.pdf")


@pytest.mark.skipif(not HAS_PANDOC, reason="the session needs pandoc to build")
def test_a_second_compile_while_one_is_running_is_refused(tmp_path, monkeypatch):
    from aiohttp.test_utils import TestClient, TestServer

    from manuscriptor.server.app import make_app

    session = _session(tmp_path)

    async def capture(msg):
        return None

    session.broadcast = capture
    started = asyncio.Event()

    def slow(manuscript_dir, *, main=None, bib=None, on_step=None, **kw):
        import time
        time.sleep(0.4)
        return compile_mod.Result("pdf", True, None, 0.4, [], None, None)

    monkeypatch.setattr(compile_mod, "compile_pdf", slow)

    async def go():
        client = TestClient(TestServer(make_app(session)))
        await client.start_server()
        first = await client.post("/compile", json={"action": "pdf"})
        assert first.status == 202
        await asyncio.sleep(0.05)
        second = await client.post("/compile", json={"action": "pdf"})
        assert second.status == 409
        body = await second.json()
        assert "already" in body["error"].lower()
        await client.close()

    asyncio.run(go())


@pytest.mark.skipif(not (HAS_PANDOC and HAS_LATEX), reason="pandoc or pdflatex is missing")
def test_a_read_only_manuscript_can_still_be_compiled(tmp_path):
    """`--read-only` promises the manuscript and the comment log are never
    written to, and a compile writes to neither: everything it lays down is
    inside the build directory, which the render already writes to in read-only
    mode. Reading a real paper and wanting a PDF of it is the ordinary case."""
    from manuscriptor.server.app import Session

    d = tiny(tmp_path)
    session = Session(d, read_only=True)
    before = {p: p.read_bytes() for p in sorted(d.rglob("*.tex"))}
    res = compile_mod.compile_pdf(session.dir)
    assert res.ok, res.error
    for p, was in before.items():
        assert p.read_bytes() == was
    assert not (paths.comments(d)).exists()


@pytest.mark.skipif(not HAS_PANDOC, reason="the session needs pandoc to build")
def test_an_unknown_action_is_refused(tmp_path):
    from aiohttp.test_utils import TestClient, TestServer

    from manuscriptor.server.app import make_app

    session = _session(tmp_path)

    async def go():
        client = TestClient(TestServer(make_app(session)))
        await client.start_server()
        resp = await client.post("/compile", json={"action": "rm -rf"})
        assert resp.status == 400
        await client.close()

    asyncio.run(go())


# ------------------------------------------------------------------- the CLI


def test_the_cli_compiles_to_pdf(tmp_path, monkeypatch, capsys):
    from manuscriptor import cli

    d = tiny(tmp_path)
    seen = {}

    def fake(manuscript_dir, *, main=None, bib=None, on_step=None, **kw):
        seen["dir"] = Path(manuscript_dir)
        on_step(compile_mod.Step("pass 1 of 3", True, 1.0))
        return compile_mod.Result("pdf", True, d / "out.pdf", 3.0, [], None, None)

    monkeypatch.setattr(compile_mod, "compile_pdf", fake)
    assert cli.main(["compile", str(d), "--pdf"]) == 0
    out = capsys.readouterr().out
    assert "pass 1 of 3" in out
    assert "out.pdf" in out
    assert seen["dir"] == d


def test_the_cli_reports_a_failure_as_a_failure(tmp_path, monkeypatch, capsys):
    from manuscriptor import cli

    d = tiny(tmp_path)

    def fake(manuscript_dir, **kw):
        return compile_mod.Result("pdf", False, None, 1.0, [], "main.tex:3: boom", None)

    monkeypatch.setattr(compile_mod, "compile_pdf", fake)
    assert cli.main(["compile", str(d), "--pdf"]) == 1
    assert "main.tex:3" in capsys.readouterr().out


def test_the_cli_compiles_to_word(tmp_path, monkeypatch):
    from manuscriptor import cli

    d = tiny(tmp_path)
    called = {}

    def fake(manuscript_dir, **kw):
        called["docx"] = True
        return compile_mod.Result("docx", True, d / "out.docx", 4.0, [], None, None)

    monkeypatch.setattr(compile_mod, "compile_docx", fake)
    assert cli.main(["compile", str(d), "--docx"]) == 0
    assert called["docx"]


# ------------------------------------------------------------ the client script


def ext_source() -> str:
    from manuscriptor.templates.ext import load

    return load()["compile"]


def test_the_extension_is_picked_up_by_the_loader():
    from manuscriptor.templates.ext import load

    assert "compile" in load()


def test_the_extension_claims_both_buttons():
    src = ext_source()
    assert "'compile:pdf'" in src or '"compile:pdf"' in src
    assert "'compile:docx'" in src or '"compile:docx"' in src


def test_the_client_carries_every_field_of_the_done_frame():
    """Found in a browser: the `done` handler rebuilt the result object field by
    field and left `log` out, so a failed compile offered no transcript to go
    and read. A frame the server fills and the client silently drops is the
    quietest kind of defect there is."""
    import re

    src = ext_source()
    at = src.index("phase === 'done'")
    body = src[at:at + 700]
    for f in ("kind", "ok", "seconds", "output", "url", "error", "log", "notes", "steps"):
        assert re.search(r"\b" + f + r"\s*:\s*[^,}]*msg\." + f + r"\b", body), \
            f"the client drops `{f}` out of the done frame"


def test_the_extension_does_not_touch_the_viewer_or_the_template():
    """Three other agents are in this tree. The feature lives in its own file."""
    root = Path(__file__).resolve().parent.parent
    viewer = (root / "manuscriptor/templates/static/viewer.js").read_text(encoding="utf-8")
    assert "MSViewer.extend" in ext_source()
    assert "fetch('/compile'" in ext_source() or 'fetch("/compile"' in ext_source()
    assert "/compile" not in viewer, "the route belongs to the extension, not the viewer"


def test_the_page_carries_the_extension(tmp_path):
    """The registration has to actually reach the rendered page, not merely
    exist on disk."""
    from jinja2 import Template
    from importlib import resources

    from manuscriptor.templates.ext import load

    tpl = resources.files("manuscriptor.templates").joinpath("index.html.j2").read_text(encoding="utf-8")
    page = Template(tpl).render(ms={"title": "t", "html": "", "blocks": {}, "outline": [],
                                   "chats": {}, "queue": [], "ticker": [], "todos": [],
                                   "activity": [], "stats": {}},
                                styles_css="", viewer_js="", extensions=load())
    assert 'data-ext="compile"' in page
    assert "compile:pdf" in page


# ------------------------------------------------ the deliverable comes back out
#
# Everything Manuscriptor writes is hidden, because it is either regenerable or
# private. The PDF is neither: it is what the author was compiling for, and a
# deliverable nobody can find is not delivered.


def test_deliver_copies_the_pdf_beside_the_tex(tmp_path):
    d = tiny(tmp_path)
    built = compile_mod.out_dir(d) / "main.pdf"
    built.write_bytes(b"%PDF-1.7\nbuilt\n")

    got = compile_mod.deliver(built, d)
    assert got == d / "main.pdf"
    assert got.read_bytes() == b"%PDF-1.7\nbuilt\n"
    # and the original stays put, because that is what the page serves
    assert built.exists()


def test_deliver_leaves_the_latex_litter_behind(tmp_path):
    d = tiny(tmp_path)
    out = compile_mod.out_dir(d)
    for name in ("main.aux", "main.log", "main.bbl", "main.blg"):
        (out / name).write_text("x", encoding="utf-8")
    (out / "main.pdf").write_bytes(b"%PDF")

    compile_mod.deliver(out / "main.pdf", d)
    beside = {p.name for p in d.iterdir() if p.is_file()}
    assert "main.pdf" in beside
    assert not beside & {"main.aux", "main.log", "main.bbl", "main.blg"}


def test_a_missing_build_delivers_nothing(tmp_path):
    d = tiny(tmp_path)
    assert compile_mod.deliver(compile_mod.out_dir(d) / "main.pdf", d) is None
    assert not (d / "main.pdf").exists()


def test_a_failed_compile_leaves_this_mornings_pdf_alone(tmp_path):
    """The reason delivery is gated on `ok`.

    Overwriting a good PDF with the wreckage of a compile that died in pass 2
    is worse than producing no PDF: the file is still there, it still opens,
    and it is quietly wrong.
    """
    d = tiny(tmp_path)
    good = d / "main.pdf"
    good.write_bytes(b"%PDF-1.7\nthis mornings good build\n")

    def failing(cmd, cwd, env):
        return 1, "! LaTeX Error: something went wrong.\n==> Fatal error occurred"

    res = compile_mod.compile_pdf(d, runner=failing)
    assert res.ok is False
    assert res.delivered is None
    assert good.read_bytes() == b"%PDF-1.7\nthis mornings good build\n"


def test_the_frame_names_the_file_the_author_opens(tmp_path):
    d = tiny(tmp_path)
    built = compile_mod.out_dir(d) / "main.pdf"
    built.write_bytes(b"%PDF")
    res = compile_mod.Result(kind="pdf", ok=True, output=built, seconds=1.0, steps=[],
                             error=None, log=None, delivered=compile_mod.deliver(built, d))
    frame = res.as_frame(root=d)
    assert frame["delivered"] == str(d / "main.pdf")
    # the served URL still resolves against the cache, or the page loses its link
    assert frame["url"] == "/compile/main.pdf"


# ------------------------------------------------- passes, and how many of them
#
# THE BUG THESE EXIST FOR SHIPPED AT EXIT 0. `\pageref{LastPage}` is a backward
# reference resolved from the PREVIOUS run's `.aux`, so a document whose page
# count changes on the LAST pass that runs prints a total nobody ever computed.
# covet-india did exactly that: 17 pages, then 21 once the bibliography was
# typeset, then 22 once the citation superscripts rendered and reflowed -- on
# pass three of three. Every footer read "/21" and the last page read "22/21".
#
# A fourth pass would have fixed that instance and left the next one armed. The
# `.aux` is the fixed point the whole cross-reference mechanism converges to, so
# the passes run until it stops changing.


def _aux_runner(page_counts, *, pdf=None, aux=None):
    """A pdflatex whose `.aux` settles after a stated number of passes.

    `page_counts` is what each pdflatex pass would write as `LastPage`. The
    `.aux` is the only thing the loop reads, so writing a plausible one is
    enough to drive it.
    """
    calls = []

    def run(cmd, *, cwd, env=None):
        calls.append(cmd)
        if cmd[0] == "bibtex":
            return 0, ""
        passes = sum(1 for c in calls if c[0] != "bibtex")
        n = page_counts[min(passes, len(page_counts)) - 1]
        if aux is not None:
            aux.parent.mkdir(parents=True, exist_ok=True)
            aux.write_text(f"\\newlabel{{LastPage}}{{{{}}{{{n}}}}}\n", encoding="utf-8")
        if pdf is not None:
            minipdf._pdf(pdf, [f"{i}/{n}" for i in range(1, n + 1)])
        return 0, ""

    run.calls = calls
    return run


def test_the_passes_stop_when_the_aux_stops_changing(tmp_path):
    """Three stay three when three is enough. The loop costs nothing on a
    document that already converged, which is the argument for it over a
    counted fourth pass."""
    d = tiny(tmp_path)
    out = compile_mod.out_dir(d)
    res = compile_mod.compile_pdf(d, runner=_aux_runner(
        [2, 3, 3], pdf=out / "main.pdf", aux=out / "main.aux"))
    assert res.ok, res.error
    assert [s.name for s in res.steps] == ["pass 1", "bibtex", "pass 2", "pass 3"]


def test_a_document_that_needs_a_fourth_pass_gets_one(tmp_path):
    """covet-india's shape: the page count changes on pass three, so pass three
    typesets against a total that is already stale and a fourth pass is what
    makes the footers right."""
    d = tiny(tmp_path)
    out = compile_mod.out_dir(d)
    res = compile_mod.compile_pdf(d, runner=_aux_runner(
        [17, 21, 22, 22], pdf=out / "main.pdf", aux=out / "main.aux"))
    assert res.ok, res.error
    assert [s.name for s in res.steps] == ["pass 1", "bibtex", "pass 2", "pass 3", "pass 4"]


def test_a_manuscript_that_never_settles_is_a_failure(tmp_path):
    """Bounded at eight, and the bound is a FAILURE rather than a delivery.

    Returning the last attempt would ship the same wrong footers the loop
    exists to prevent, with the additional insult of having noticed.
    """
    d = tiny(tmp_path)
    out = compile_mod.out_dir(d)
    good = d / "main.pdf"
    good.write_bytes(b"%PDF-1.7\nthis mornings good build\n")
    res = compile_mod.compile_pdf(d, runner=_aux_runner(
        list(range(1, 40)), pdf=out / "main.pdf", aux=out / "main.aux"))
    assert res.ok is False
    assert res.delivered is None
    assert good.read_bytes() == b"%PDF-1.7\nthis mornings good build\n"
    assert "never settled" in (res.error or "") or "still changing" in (res.error or "")
    assert str(compile_mod.MAX_PASSES) in (res.error or "")
    passes = [s for s in res.steps if s.name.startswith("pass")]
    assert len(passes) == compile_mod.MAX_PASSES


# --------------------------------------------------------- the pagination gate


def test_footers_that_disagree_with_the_document_fail_the_compile(tmp_path):
    """The gate, because the failure is invisible without one.

    A PDF of four pages whose every footer says three is what a three-pass
    build of covet-india produced, and every other check in this module passes
    it: the exit code is 0, the PDF exists, and it was written by this run.
    """
    d = tiny(tmp_path)
    out = compile_mod.out_dir(d)
    good = d / "main.pdf"
    good.write_bytes(b"%PDF-1.7\nthis mornings good build\n")

    def stale(cmd, *, cwd, env=None):
        if cmd[0] != "bibtex":
            minipdf._pdf(out / "main.pdf", ["1/3", "2/3", "3/3", "4/3"])
        return 0, ""

    res = compile_mod.compile_pdf(d, runner=stale)
    assert res.ok is False
    assert "4/3" in (res.error or ""), res.error
    assert "4 pages" in (res.error or "")
    # And the wrong-footer PDF must not have replaced the good one.
    assert res.delivered is None
    assert good.read_bytes() == b"%PDF-1.7\nthis mornings good build\n"


def test_a_pdf_that_cannot_be_read_is_not_a_successful_compile(tmp_path):
    """A deliverable nothing can open is not a deliverable. The gate refusing
    to run is not the gate passing."""
    d = tiny(tmp_path)
    out = compile_mod.out_dir(d)

    def junk(cmd, *, cwd, env=None):
        (out / "main.pdf").write_bytes(b"%PDF-1.4\nnot really a pdf\n")
        return 0, ""

    res = compile_mod.compile_pdf(d, runner=junk)
    assert res.ok is False
    assert "read" in (res.error or "").lower()


def test_a_manuscript_with_no_total_footer_still_compiles(tmp_path):
    """Most classes print a bare `\\thepage`. A gate that fails those has
    replaced a rare silent bug with a loud wrong one."""
    d = tiny(tmp_path)
    out = compile_mod.out_dir(d)

    def plain(cmd, *, cwd, env=None):
        minipdf._pdf(out / "main.pdf", ["1", "2", "3"])
        return 0, ""

    res = compile_mod.compile_pdf(d, runner=plain)
    assert res.ok, res.error
    assert res.delivered == d / "main.pdf"


# --------------------------------------------------------- against real LaTeX

# Two pages of prose, a bibliography that appears only once bibtex has run, and
# a page that appears only once the bibliography has moved LastPage past 2. The
# page count therefore changes on pass THREE -- covet-india's shape, reduced to
# something that compiles in a second. `refcount` is what makes `\pageref`
# readable by `\ifnum`; without it the growth cannot be made to depend on the
# page count at all.
GROWS = r"""\documentclass[11pt]{article}
\usepackage{lastpage}
\usepackage{refcount}
\usepackage{fancyhdr}
\pagestyle{fancy}
\fancyhf{}
\cfoot{\thepage/\pageref{LastPage}}
\renewcommand{\headrulewidth}{0pt}
\begin{document}
\section{One}
Prose that cites \cite{smith2020}.
\newpage
\section{Two}
More prose on a second page.
\newpage
\bibliographystyle{plain}
\bibliography{refs}
\ifnum\getpagerefnumber{LastPage}>2\relax
  \clearpage\mbox{}
\fi
\end{document}
"""


def growing(tmp_path: Path) -> Path:
    d = tmp_path / "grows"
    d.mkdir(parents=True)
    (d / "main.tex").write_text(GROWS, encoding="utf-8")
    (d / "refs.bib").write_text(BIB, encoding="utf-8")
    return d


@pytest.mark.skipif(not HAS_LATEX, reason="pdflatex is not installed")
def test_three_passes_would_have_shipped_the_wrong_footers(tmp_path):
    """The bug, reproduced against real LaTeX before the fix is trusted.

    This is the old recipe -- one pass, bibtex, two more -- run by hand on a
    document that grows on the third. It exits 0 and writes a four-page PDF
    every footer of which says three.
    """
    d = growing(tmp_path)
    out = compile_mod.out_dir(d)
    cmd = ["pdflatex", *compile_mod.ENGINE_FLAGS, f"-output-directory={out}", "main.tex"]
    env = compile_mod._tex_env(d, out)
    for step in (cmd, ["bibtex", "main"], cmd, cmd):
        compile_mod._default_runner(step, cwd=(out if step[0] == "bibtex" else d), env=env)

    total, found = pagination.footers(out / "main.pdf")
    assert total == 4, f"the fixture no longer grows on the third pass: {total} pages"
    assert found[4] == (4, 3), f"the fixture no longer reproduces the bug: {found}"
    problems = pagination.check(out / "main.pdf")
    assert problems, "three passes shipped wrong footers and the gate said nothing"
    assert "but the document is 4 pages" in " ".join(problems)


@pytest.mark.skipif(not HAS_LATEX, reason="pdflatex is not installed")
def test_the_loop_gets_the_footers_right_on_the_same_manuscript(tmp_path):
    """And the fix, on the identical document: a fourth pass, right footers."""
    d = growing(tmp_path)
    res = compile_mod.compile_pdf(d)
    assert res.ok, res.error
    assert [s.name for s in res.steps] == ["pass 1", "bibtex", "pass 2", "pass 3", "pass 4"]
    total, found = pagination.footers(res.output)
    assert total == 4
    assert found[4] == (4, 4)
    assert pagination.check(res.output) == []


# ------------------------------------------------- what Word inherits from this


def test_word_says_where_its_cross_reference_numbers_came_from(tmp_path, monkeypatch):
    """`compile_docx` has no pass count of its own -- it gets the `.aux` from
    `compile_pdf` and therefore inherits the loop. What it did NOT inherit was
    any interest in whether that pre-compile succeeded, so a run whose page
    totals never settled handed its numbers to Word in silence."""
    d = tiny(tmp_path)
    monkeypatch.setattr(compile_mod, "compile_pdf", lambda *a, **k: compile_mod.Result(
        kind="pdf", ok=False, output=None, seconds=0.1, steps=[],
        error="the cross-references never settled: main.aux was still changing",
        log=None))
    res = compile_mod.compile_docx(d, runner=_fake_runner(1))
    assert any("never settled" in n for n in res.notes), res.notes


def test_word_says_when_the_aux_is_the_authors_own(tmp_path):
    """An `.aux` beside the manuscript is used because its numbers are far
    better than none, but nothing here knows how old it is."""
    d = tiny(tmp_path)
    (d / "main.aux").write_text("\\relax\n", encoding="utf-8")
    res = compile_mod.compile_docx(d, runner=_fake_runner(1))
    assert any("beside the manuscript" in n for n in res.notes), res.notes


def test_a_word_compile_does_not_quietly_replace_the_pdf(tmp_path):
    """The pre-compile exists to produce an `.aux`, not a deliverable.

    It used to run with delivery on, so asking for Word overwrote this
    morning's PDF beside the manuscript without being asked -- and did it in a
    `--read-only` serve too, which promises that nothing reaches the
    filesystem at all.
    """
    d = tiny(tmp_path)
    good = d / "main.pdf"
    good.write_bytes(b"%PDF-1.7\nthis mornings good build\n")
    compile_mod.compile_docx(
        d, runner=_fake_runner(1, pdf=compile_mod.out_dir(d) / "main.pdf"),
        deliver_out=False)
    assert good.read_bytes() == b"%PDF-1.7\nthis mornings good build\n"
