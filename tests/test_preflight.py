"""Preflight, and the discipline that a check must be able to fail.

The memo this module came from established the empty-`\\input` defect by
negative control: a *missing* include hard errors and produces no PDF, an
*empty* one compiles clean at the same page count and drops the number out of
the sentence. Every check here is tested the same way -- once on a clean
fixture, and once on a manuscript carrying the defect on purpose. A check that
has never failed proves nothing.

The sharpest tests in this file are not about any one defect. They are that a
check which could not run reports `skipped` and never `ok`, and that a check
which never reported at all is named. Silence read as success is the failure
the whole module exists to prevent.
"""
from __future__ import annotations

import inspect
import subprocess
from pathlib import Path

from manuscriptor.server import preflight

CLEAN_BST = r"""
ENTRY
  { address author booktitle doi journal note number pages publisher
    title volume year url }
  {}
  { label }
FUNCTION {format.doi}
{ doi empty$
    { "" }
    { "\bibdoi{" doi * "}" * }
  if$
}
FUNCTION {begin.bib}
{ "\providecommand{\bibdoi}[1]{\url{https://doi.org/#1}}" write$ newline$ }
FUNCTION {output} { }
READ
"""

NO_DOI_BST = """
% apalike's shape: everything but the one field the reader follows.
ENTRY
  { address author booktitle journal note number pages publisher
    title volume year }
  {}
  { label }
READ
"""

BIB = """
@article{one,
  author = {A. Author},
  title = {A Title},
  journal = {J},
  year = {2020},
  doi = {10.1/xyz},
}

@article{two,
  author = {B. Author},
  title = {Another},
  journal = {J},
  year = {2021},
  doi = {10.1/abc},
}
"""

MAIN = r"""\documentclass{article}
\usepackage{hyperref}
\begin{document}
We measured \input{frag_n} simulated conversations across the sample,
which is reported in Table~\ref{tab:main} of this paper.
\bibliographystyle{style}
\bibliography{refs}
\end{document}
"""


def manuscript(tmp_path: Path, *, main: str = MAIN, bst: str = CLEAN_BST,
               frag: str = "216", git: bool = True) -> Path:
    """A manuscript that passes every check, unless a caller breaks one.

    Its own repository by default, so the scripts sweep is bounded here and the
    tests do not depend on what encloses pytest's temp directory.
    """
    d = tmp_path / "paper"
    d.mkdir()
    if git:
        subprocess.run(["git", "init", "-q"], cwd=d, capture_output=True)
    (d / "main.tex").write_text(main, encoding="utf-8")
    (d / "frag_n.tex").write_text(frag, encoding="utf-8")
    (d / "style.bst").write_text(bst, encoding="utf-8")
    (d / "refs.bib").write_text(BIB, encoding="utf-8")
    return d


def by_check(results, name: str, doc: str = "main.tex"):
    for r in results:
        if r.check == name and r.doc == doc:
            return r
    raise AssertionError(f"no result for {name} on {doc}")


def bodies(result) -> str:
    return " | ".join(f.body for f in result.findings)


def snapshot(d: Path) -> dict:
    return {p: p.read_bytes() for p in sorted(d.rglob("*")) if p.is_file()}


# ------------------------------------------------------- check zero: it ran


def test_a_clean_manuscript_still_names_every_check(tmp_path):
    """Silence is not the report. Every planned check appears by name."""
    d = manuscript(tmp_path)
    planned = preflight.plan(d)
    results = preflight.run(d)

    assert preflight.audit(planned, results) == []
    assert all(r.status == "ok" for r in results), [r.line() for r in results]
    assert preflight.exit_code(planned, results) == 0

    out = preflight.report(d, planned, results)
    for name in preflight.CHECKS:
        assert name in out
    assert "all clean" in out


def test_a_check_that_could_not_run_is_skipped_and_is_not_a_pass(tmp_path):
    """The negative control for check zero.

    A style nothing can resolve is a check that did not happen. It must not
    report `ok`, it must not be silent, and it must outrank findings in the
    exit code, because it is the failure that hides the others.
    """
    d = manuscript(tmp_path)
    (d / "style.bst").unlink()

    planned = preflight.plan(d)
    results = preflight.run(d)
    r = by_check(results, "bib-fields")

    assert r.status == "skipped" and r.ran is False
    assert "style.bst" in r.detail
    assert preflight.exit_code(planned, results) == 2
    assert "A skipped check is not a pass." in preflight.report(d, planned, results)


def test_a_check_that_never_reported_at_all_is_named(tmp_path):
    """The other half of check zero: absence, not merely failure."""
    d = manuscript(tmp_path)
    planned = preflight.plan(d)
    results = [r for r in preflight.run(d) if r.check != "fragments"]

    assert preflight.audit(planned, results) == ["fragments on main.tex"]
    assert preflight.exit_code(planned, results) == 2
    out = preflight.report(d, planned, results)
    assert "NO RESULT" in out and "fragments on main.tex" in out


def test_not_applicable_is_a_third_answer_and_requires_a_reason(tmp_path):
    """A document with no bibliography has nothing to check. That is not the
    same as being unable to tell, and it does not fail the run."""
    d = manuscript(tmp_path, main=r"""\documentclass{article}
\begin{document}
Nothing here cites anything at all.
\end{document}
""")
    planned = preflight.plan(d)
    results = preflight.run(d)
    r = by_check(results, "bib-fields")

    assert r.status == "n/a" and r.ran is True
    assert "declares no bibliography" in r.detail
    assert preflight.exit_code(planned, results) == 0


def test_an_unreadable_document_skips_its_checks_rather_than_passing_them(tmp_path):
    """`flatten` treats an unreadable file as an empty one, which is right for
    rendering and disastrous here: every check would run against an empty
    buffer and report a document with no includes, no exhibit numbers and no
    bibliography. Three green passes, none of which read the manuscript."""
    d = manuscript(tmp_path)
    (d / "second.tex").write_text(
        "\\documentclass{article}\n\\begin{document}\nTable~S9\n\\end{document}\n",
        encoding="utf-8")
    (d / "second.tex").chmod(0o000)
    try:
        planned = preflight.plan(d)
        results = preflight.run(d)
        second = [r for r in results if r.doc == "second.tex"]

        assert preflight.audit(planned, results) == []
        assert {r.status for r in second} == {"skipped"}
        assert all("could not read" in r.detail for r in second)
        assert preflight.exit_code(planned, results) == 2
    finally:
        (d / "second.tex").chmod(0o644)


# ------------------------------------------------------------ check: fragments


def test_a_fragment_that_says_something_is_clean(tmp_path):
    d = manuscript(tmp_path)
    assert by_check(preflight.run(d), "fragments").status == "ok"


def test_an_empty_fragment_is_caught_where_the_compiler_is_silent(tmp_path):
    """The flagship case, and the whole reason this module exists.

    `frag_n.tex` emptied leaves "We measured  simulated conversations" on the
    page. LaTeX does not complain, the page count does not move, and the
    sentence has lost its number.
    """
    d = manuscript(tmp_path, frag="")
    r = by_check(preflight.run(d), "fragments")

    assert r.status == "findings"
    assert "contributes nothing" in bodies(r)
    assert "frag_n" in bodies(r)


def test_a_missing_fragment_is_caught_too_and_says_something_different(tmp_path):
    """The contrast that proved the case. A missing include hard errors at
    compile time; an empty one does not. Both are findings here, and the
    message must not conflate them."""
    d = manuscript(tmp_path)
    (d / "frag_n.tex").unlink()
    r = by_check(preflight.run(d), "fragments")

    assert r.status == "findings"
    assert "resolves to no file" in bodies(r)


def test_a_fragment_holding_only_whitespace_is_caught(tmp_path):
    d = manuscript(tmp_path, frag="   \n\n  ")
    assert "whitespace" in bodies(by_check(preflight.run(d), "fragments"))


def test_a_fragment_holding_only_a_comment_contributes_nothing(tmp_path):
    """The shape a byte-count check misses. The file is 30 bytes long and hands
    the document not one character of them."""
    d = manuscript(tmp_path, frag="% this number is coming soon\n")
    r = by_check(preflight.run(d), "fragments")
    assert r.status == "findings" and "contributes nothing" in bodies(r)


def test_a_fragment_of_nothing_but_empty_fragments_contributes_nothing(tmp_path):
    """Emptiness one level down. The outer file is not empty and still says
    nothing, which is why the measure is what reached the document."""
    d = manuscript(tmp_path)
    (d / "frag_n.tex").write_text("\\input{inner}%\n", encoding="utf-8")
    (d / "inner.tex").write_text("", encoding="utf-8")
    r = by_check(preflight.run(d), "fragments")

    assert "frag_n" in bodies(r), r.line()


def test_the_finding_quotes_the_block_the_hole_is_in(tmp_path):
    """So the finding can be filed as a comment anchored where the reader is.

    The quote is the block's SOURCE, markup and all. It used to be the prose a
    reader sees, with the LaTeX stripped out, and that was wrong in the only
    way that matters: `match_by_quote` -- the one rule that places every comment
    in this program -- compares against block source, so a stripped quote
    matched only when the paragraph happened to open with plain words, and a
    paragraph sitting under a `\\section` had the heading folded into its quote
    and anchored nowhere at all. Asserted here by placing it, not by inspecting
    it, because the shape of the string is not the claim.
    """
    from manuscriptor.server import build as build_mod

    d = manuscript(tmp_path, frag="")
    r = by_check(preflight.run(d), "fragments")
    quote = r.findings[0].quote
    assert "We measured" in quote and "simulated conversations" in quote

    b = build_mod.build(d)
    hit = build_mod.match_by_quote(quote, b.blocks)
    assert hit is not None, f"the quote places nothing: {quote!r}"
    assert "We measured" in b.by_id[hit].source_text


# ------------------------------------------------------ check: exhibit numbers


def test_a_document_that_refs_its_labels_is_clean(tmp_path):
    d = manuscript(tmp_path)
    assert by_check(preflight.run(d), "exhibit-numbers").status == "ok"


def test_a_hand_typed_supplement_number_is_caught(tmp_path):
    """Eighteen of these survived into the submission and two were wrong."""
    d = manuscript(tmp_path, main=MAIN.replace(
        "Table~\\ref{tab:main}", "Table~S22"))
    r = by_check(preflight.run(d), "exhibit-numbers")

    assert r.status == "findings"
    assert "Table~S22" in bodies(r)
    assert r.findings[0].line > 0


def test_a_hand_typed_figure_number_is_caught(tmp_path):
    d = manuscript(tmp_path, main=MAIN.replace(
        "Table~\\ref{tab:main}", "Figure 3"))
    assert "Figure 3" in bodies(by_check(preflight.run(d), "exhibit-numbers"))


def test_a_commented_out_number_is_not_a_finding(tmp_path):
    d = manuscript(tmp_path, main=MAIN.replace(
        "which is reported in", "% see Table~S22 --"))
    assert by_check(preflight.run(d), "exhibit-numbers").status == "ok"


def test_a_filename_that_ends_in_a_digit_is_not_a_sentence(tmp_path):
    """`figures/Figure1.pdf` is not a claim about figure one. Requiring a
    separator is what keeps this check from crying wolf on every graphic, and
    it carries the whole burden now that the match ignores case."""
    d = manuscript(tmp_path, main=MAIN.replace(
        "which is reported in", "\\includegraphics{figures/Figure1.pdf} and"))
    assert by_check(preflight.run(d), "exhibit-numbers").status == "ok"


def test_a_lowercase_reference_in_prose_is_still_a_hand_typed_number(tmp_path):
    """Authors write "see table 3", and it goes stale exactly as fast."""
    d = manuscript(tmp_path, main=MAIN.replace(
        "Table~\\ref{tab:main}", "table 3"))
    assert "table 3" in bodies(by_check(preflight.run(d), "exhibit-numbers"))


def test_the_doubled_s_prefix_is_caught_when_the_counter_already_carries_it(tmp_path):
    """This shipped. `\\thetable` was already `S\\arabic{table}`, so eleven
    sites writing `Table~S\\ref{}` rendered "Table SS19" in the submitted PDF."""
    broken = MAIN.replace(
        r"\begin{document}",
        "\\renewcommand{\\thetable}{S\\arabic{table}}\n\\begin{document}"
    ).replace("Table~\\ref{tab:main}", "Table~S\\ref{tab:main}")
    d = manuscript(tmp_path, main=broken)
    r = by_check(preflight.run(d), "exhibit-numbers")

    assert r.status == "findings" and "SS19" in bodies(r)


def test_the_same_s_ref_is_not_flagged_when_the_counter_does_not_carry_it(tmp_path):
    """The negative control for the doubling. Without the redefinition,
    `Table~S\\ref{}` is how a main text points into a supplement."""
    d = manuscript(tmp_path, main=MAIN.replace(
        "Table~\\ref{tab:main}", "Table~S\\ref{tab:main}"))
    assert by_check(preflight.run(d), "exhibit-numbers").status == "ok"


def test_a_number_inside_an_included_fragment_names_both_the_file_and_the_doc(tmp_path):
    """Two different questions. The comment is filed against the document being
    read; the fix has to happen in the fragment the bytes are in, and reporting
    either one in place of the other is useless in a different way."""
    d = manuscript(tmp_path, frag="216 (Table~S7)")
    finding = by_check(preflight.run(d), "exhibit-numbers").findings[0]

    assert finding.file == "frag_n.tex"
    assert finding.doc == "main.tex"
    assert finding.as_comment()["doc"] == "main.tex"


# ------------------------------------------------- check: numbers in scripts


def scripts_result(d: Path):
    return by_check(preflight.run(d), "exhibit-numbers", preflight.SCRIPTS)


def test_a_script_that_emits_a_hand_typed_number_is_caught(tmp_path):
    """The eighteenth site: an R script printing a literal `Table~S19`."""
    d = manuscript(tmp_path)
    (d / "make_table.R").write_text(
        'add("Conversation-level results are in Table~S19; outcomes are")\n',
        encoding="utf-8")
    assert "Table~S19" in bodies(scripts_result(d))


def test_a_script_with_no_literals_is_clean(tmp_path):
    d = manuscript(tmp_path)
    (d / "make_table.R").write_text(
        'add("Results are in Table~\\\\ref{tab:main}")\n', encoding="utf-8")
    assert scripts_result(d).status == "ok"


def test_a_todo_comment_about_the_hazard_is_not_the_hazard(tmp_path):
    """The real script carries `# TODO: "Table~S19" is a hand-typed exhibit
    number`. The comment is a note about the defect; the literal is what ships."""
    d = manuscript(tmp_path)
    (d / "make_table.R").write_text(
        '# TODO: "Table~S19" is a hand-typed exhibit number\nx <- 1\n',
        encoding="utf-8")
    assert scripts_result(d).status == "ok"


def test_stata_multiplication_does_not_excuse_a_literal(tmp_path):
    """`*` opens a Stata comment only at the start of a line. Reading it as a
    comment anywhere would silently excuse every literal on a line that
    multiplies."""
    d = manuscript(tmp_path)
    (d / "tables.do").write_text(
        'file write f "`=2*3\' rows appear in Table 4" _n\n', encoding="utf-8")
    assert "Table 4" in bodies(scripts_result(d))

    (d / "tables.do").write_text('* Table 4 is built here\n', encoding="utf-8")
    assert scripts_result(d).status == "ok"


def test_the_scripts_sweep_reaches_the_analysis_beside_the_paper(tmp_path):
    """The `.tex` lives in `paper/` and the R lives in `analysis/`. A sweep
    bounded by the manuscript directory would miss every script there."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, capture_output=True)
    d = manuscript(repo, git=False)
    (repo / "analysis").mkdir()
    (repo / "analysis" / "make_table.R").write_text(
        'add("in Table~S19 we show")\n', encoding="utf-8")

    assert "Table~S19" in bodies(scripts_result(d))


# ----------------------------------------------------------- check: bib fields


def test_a_style_that_declares_every_field_is_clean(tmp_path):
    d = manuscript(tmp_path)
    r = by_check(preflight.run(d), "bib-fields")
    assert r.status == "ok" and "style.bst declares" in r.detail


def test_a_style_that_swallows_the_doi_is_caught(tmp_path):
    """`apalike.bst` discarded 39 DOIs this way. The field was present on 39 of
    40 entries and every one resolved; the style simply never asked for it."""
    d = manuscript(tmp_path, bst=NO_DOI_BST)
    r = by_check(preflight.run(d), "bib-fields")

    assert r.status == "findings"
    assert "doi" in bodies(r) and "2 of 2 entries" in bodies(r)
    assert "without a warning" in bodies(r)


def test_the_style_a_journal_class_sets_for_you_is_still_read(tmp_path):
    """estonia-qbs names no style in `main.tex`; `wlscirep.cls` sets it. Reading
    only the .tex tree reported the check unrunnable, which was honest and
    wrong -- the class is an input like any other."""
    d = manuscript(tmp_path, main=MAIN.replace(
        "\\bibliographystyle{style}\n", "").replace(
        "\\documentclass{article}", "\\documentclass{journal}"))
    (d / "journal.cls").write_text(
        "\\ProvidesClass{journal}\n\\bibliographystyle{style}\n", encoding="utf-8")
    (d / "style.bst").write_text(NO_DOI_BST, encoding="utf-8")

    r = by_check(preflight.run(d), "bib-fields")
    assert r.status == "findings" and "journal.cls" in r.detail


def test_a_declared_bibliography_with_no_style_anywhere_is_skipped(tmp_path):
    d = manuscript(tmp_path, main=MAIN.replace("\\bibliographystyle{style}\n", ""))
    r = by_check(preflight.run(d), "bib-fields")
    assert r.status == "skipped" and "no \\bibliographystyle" in r.detail


def test_a_bib_written_on_one_line_still_reports_its_fields(tmp_path):
    """The guard against passing for the wrong reason. A line-based reader finds
    no fields in this file and would call a doi-swallowing style clean."""
    d = manuscript(tmp_path, bst=NO_DOI_BST)
    (d / "refs.bib").write_text(
        '@article{one, author = {A}, title = {T}, year = {2020}, '
        'doi = {10.1/x}}\n', encoding="utf-8")
    assert "doi" in bodies(by_check(preflight.run(d), "bib-fields"))


def test_a_field_a_brace_group_only_looks_like_is_not_counted(tmp_path):
    """`{Fixed = Effects}` inside a title is not a field named `Fixed`."""
    entries, fields = preflight._bib_fields(
        "@article{k, title = {A {Study = Of} Things}, year = {2020}}")
    assert entries == 1
    assert set(fields) == {"title", "year"}


def test_the_fields_bibtex_supplies_itself_are_not_reported(tmp_path):
    d = manuscript(tmp_path)
    (d / "refs.bib").write_text(
        "@article{one, author = {A}, crossref = {two}, key = {k}}\n",
        encoding="utf-8")
    assert by_check(preflight.run(d), "bib-fields").status == "ok"


# ------------------------------------------------------- check: bib doi links

# The defect, exactly as it stood in covet-india's `naturemag-doi.bst`: `\url`
# applied to a BARE doi. With hyperref loaded that is a `/URI` annotation with
# no scheme, and a PDF viewer resolves it against the PDF's own directory. All
# 53 links in the compiled bibliography went to `file:///Users/.../10.1016/...`.
BARE_URL = r'\doiprefix\url{'
SEMANTIC = r'\bibdoi{'


def doi_bst(literal: str = SEMANTIC, *, bbl_def: str | None = None,
            entry_doi: bool = True) -> str:
    """A `.bst` that emits its doi through `literal`, and maybe defines it."""
    fields = "author doi journal title year" if entry_doi else "author journal title year"
    body = "ENTRY\n  { %s }\n  {}\n  { label }\n" % fields
    body += ('FUNCTION {format.doi}\n{ doi empty$\n    { "" }\n'
             '    { "%s" doi * "}" * }\n  if$\n}\n' % literal)
    if bbl_def is not None:
        body += 'FUNCTION {begin.bib}\n{ "%s" write$ newline$ }\n' % bbl_def
    return body + "READ\n"


def doi_result(d, doc: str = "main.tex"):
    return by_check(preflight.run(d), "bib-doi-links", doc)


def test_a_style_that_carries_the_resolver_prefix_is_clean(tmp_path):
    """`aer-doi.bst` emits `\\bibdoi{}` and writes the `\\providecommand` that
    defines it with `https://doi.org/`. That is the shape that works."""
    d = manuscript(tmp_path)
    r = doi_result(d)
    assert r.status == "ok", r.detail
    assert "bibdoi" in r.detail


def test_url_around_a_bare_doi_is_caught(tmp_path):
    """The covet-india defect. Nothing about it fails a compile: the links are
    there, they are blue, they are clickable, and every one of them resolves
    against the directory the PDF happens to be sitting in."""
    d = manuscript(tmp_path, bst=doi_bst(BARE_URL))
    r = doi_result(d)

    assert r.status == "findings"
    body = bodies(r)
    assert "\\url" in body and "scheme" in body
    assert "https://doi.org/" in body          # it says what to do instead
    assert "2 of 2" in body                    # and how many are affected


def test_a_macro_defined_in_the_document_preamble_with_the_prefix_is_clean(tmp_path):
    """dsp-bias's shape: `apalike-doi.bst` emits `\\doi{}`, and `main.tex:11`
    defines it. The style alone cannot be judged -- the definition decides."""
    d = manuscript(tmp_path, bst=doi_bst(r"\doi{"), main=MAIN.replace(
        r"\usepackage{hyperref}",
        "\\usepackage{hyperref}\n"
        r"\providecommand{\doi}[1]{\url{https://doi.org/#1}}"))
    r = doi_result(d)
    assert r.status == "ok", r.detail
    assert "main.tex" in r.detail


def test_the_same_macro_defined_without_the_prefix_is_caught(tmp_path):
    """`\\doi{}` is only correct BECAUSE something defines it with the prefix.
    Defined as `\\url{#1}` it is the identical defect wearing a nicer name."""
    d = manuscript(tmp_path, bst=doi_bst(r"\doi{"), main=MAIN.replace(
        r"\usepackage{hyperref}",
        "\\usepackage{hyperref}\n" + r"\providecommand{\doi}[1]{\url{#1}}"))
    r = doi_result(d)

    assert r.status == "findings"
    assert "main.tex" in bodies(r) and "\\doi" in bodies(r)


def test_a_definition_written_into_the_bbl_preamble_is_found(tmp_path):
    """The definition need not be in the document at all: `aer-doi.bst:1218`
    and the fixed `naturemag-doi.bst` both `write$` it into the `.bbl`."""
    d = manuscript(tmp_path, bst=doi_bst(
        r"\bibdoi{",
        bbl_def=r"\providecommand{\bibdoi}[1]{\url{https://doi.org/#1}}"))
    r = doi_result(d)
    assert r.status == "ok" and "style.bst" in r.detail


def test_a_macro_nothing_defines_cannot_be_judged_and_is_skipped(tmp_path):
    """Honest, and not a pass. A `\\doi{}` whose definition lives in a class in
    the TeX tree may be right or may be `\\url{#1}`; this cannot tell, and
    saying "ok" here is how a check passes for the wrong reason."""
    d = manuscript(tmp_path, bst=doi_bst(r"\doi{"))
    planned, results = preflight.plan(d), preflight.run(d)
    r = by_check(results, "bib-doi-links")

    assert r.status == "skipped" and r.ran is False
    assert "\\doi" in r.detail and "no definition" in r.detail
    assert preflight.exit_code(planned, results) == 2


def test_a_style_that_never_declares_a_doi_has_nothing_to_link(tmp_path):
    """estonia-ecm: `aea.bst` does not name `doi` in its ENTRY block, so BibTeX
    never hands it one. A positive determination, not an inability to tell."""
    d = manuscript(tmp_path, bst=doi_bst(entry_doi=False))
    r = doi_result(d)
    assert r.status == "n/a" and "does not declare" in r.detail


def test_a_style_that_declares_the_doi_but_emits_it_unrecognisably_is_skipped(tmp_path):
    """The guard against the n/a above being handed out for free. `doi` is
    declared, so it may well reach the page by an idiom this does not parse."""
    d = manuscript(tmp_path, bst=CLEAN_BST.replace(
        r'{ "\bibdoi{" doi * "}" * }', "{ doi bibinfo.check }"))
    r = doi_result(d)
    assert r.status == "skipped" and "recognis" in r.detail.lower()


def test_a_bibliography_carrying_no_doi_at_all_is_not_applicable(tmp_path):
    """No entry carries one, so the broken style prints nothing to break."""
    d = manuscript(tmp_path, bst=doi_bst(BARE_URL))
    (d / "refs.bib").write_text(
        "@article{one, author = {A}, title = {T}, year = {2020}}\n",
        encoding="utf-8")
    r = doi_result(d)
    assert r.status == "n/a" and "no entry" in r.detail


def test_without_hyperref_the_same_style_is_a_lesser_finding(tmp_path):
    """dsp-bias and qutub-india load no hyperref, so `\\url` typesets and links
    nothing: no annotation exists to be broken, and reporting 53 broken links
    would be a lie. The style is still wrong, and adding hyperref -- which a
    journal class does for you -- arms every one of them silently, so this is
    reported and not waved through."""
    d = manuscript(tmp_path, bst=doi_bst(BARE_URL),
                   main=MAIN.replace("\\usepackage{hyperref}\n", ""))
    r = doi_result(d)

    assert r.status == "findings"
    assert "no hyperref" in bodies(r)
    assert "nothing is clickable" in bodies(r)

    (tmp_path / "b").mkdir()
    armed = doi_result(manuscript(tmp_path / "b", bst=doi_bst(BARE_URL)))
    assert "no hyperref" not in bodies(armed)


def test_hyperref_loaded_by_the_class_arms_the_defect_too(tmp_path):
    """covet-india's shape end to end: `wlscirep.cls` names the style on line 53
    AND loads hyperref on line 46, and `main.tex` mentions neither."""
    d = manuscript(tmp_path, main=MAIN.replace(
        "\\bibliographystyle{style}\n", "").replace(
        "\\usepackage{hyperref}\n", "").replace(
        "\\documentclass{article}", "\\documentclass{journal}"))
    (d / "journal.cls").write_text(
        "\\ProvidesClass{journal}\n"
        "\\RequirePackage[colorlinks=true, allcolors=blue]{hyperref}\n"
        "\\bibliographystyle{style}\n", encoding="utf-8")
    (d / "style.bst").write_text(doi_bst(BARE_URL), encoding="utf-8")

    r = doi_result(d)
    assert r.status == "findings"
    assert "no hyperref" not in bodies(r)
    assert "journal.cls" in r.detail


def test_the_finding_names_the_bst_as_the_file_and_the_document_as_the_doc(tmp_path):
    """The two are not the same question. The comment is filed against the
    document a reader is looking at; the bytes to fix are in the `.bst`."""
    d = manuscript(tmp_path, bst=doi_bst(BARE_URL))
    f = doi_result(d).findings[0]

    assert f.doc == "main.tex"
    assert f.file == "style.bst"
    assert f.line == 8                      # the emission, not the top of the file


def test_the_finding_carries_a_quote_so_it_cannot_be_filed_twice(tmp_path):
    """`drain.comment` dedupes only on a NON-EMPTY quote. `bib-fields` findings
    carry none and would be filed again on every run; this one anchors on the
    bibliography, which is the part of the page the broken links are on."""
    from manuscriptor.server import chat, drain

    d = manuscript(tmp_path, bst=doi_bst(BARE_URL))
    finding = doi_result(d).findings[0]
    assert finding.quote

    first = drain.comment(d, author="preflight", **finding.as_comment())
    again = drain.comment(d, author="preflight", **finding.as_comment())
    assert first is not None and again is None
    assert len(list(chat.read_chats(drain.paths.comments(d)))) == 1


def test_one_finding_per_document_however_many_emission_sites(tmp_path):
    """Two findings sharing a quote would dedupe each other away in `drain`."""
    d = manuscript(tmp_path, bst=doi_bst(BARE_URL) + doi_bst(r"\href{"))
    r = doi_result(d)

    assert r.status == "findings" and len(r.findings) == 1
    assert "2 emission sites" in bodies(r)


def test_a_site_that_cannot_be_judged_does_not_bury_one_that_can(tmp_path):
    """The reverse of this module's usual order, and deliberately so. Reporting
    `skipped` for the unreadable site would produce no finding at all, and the
    definitely-broken bibliography beside it would go unfiled. Nothing is lost:
    the undetermined site is named in both the detail and the finding."""
    d = manuscript(tmp_path, bst=doi_bst(BARE_URL) + doi_bst(r"\doi{"))
    r = doi_result(d)

    assert r.status == "findings"
    assert "could not be judged" in r.detail
    assert "\\doi" in bodies(r) and "by hand" in bodies(r)


def test_a_document_whose_class_names_a_style_but_prints_no_bibliography(tmp_path):
    """covet-india's `supplement.tex` cites nothing and prints nothing, but
    `wlscirep.cls` still names the style, so the resolver found a `.bst` and
    both bibliography checks reported on a bibliography that does not exist --
    one of them filing a finding about 0 broken links. A style a document never
    invokes is nothing to check, which is n/a and not ok."""
    d = manuscript(tmp_path, main=MAIN.replace("\\bibliography{refs}\n", ""))
    results = preflight.run(d)

    for check in ("bib-doi-links", "bib-fields"):
        r = by_check(results, check)
        assert r.status == "n/a", f"{check}: {r.status} {r.detail}"
        assert "no bibliography" in r.detail


def test_the_quote_is_the_directive_itself_not_the_words_around_it(tmp_path):
    """`match_by_quote` compares against block SOURCE, so the anchor is the
    `\\bibliography` line. Stripped to prose it was the single word "sample",
    which is a word this manuscript uses on nearly every page -- a quote that
    matches nothing lands unanchored, but one that matches the wrong paragraph
    lands the comment wrong."""
    d = manuscript(tmp_path, bst=doi_bst(BARE_URL))
    q = doi_result(d).findings[0].quote

    assert q == r"\bibliography{refs}"


def test_the_check_is_planned_for_every_document_and_reports(tmp_path):
    d = manuscript(tmp_path)
    planned = preflight.plan(d)
    assert ("bib-doi-links", "main.tex") in planned
    assert preflight.audit(planned, preflight.run(d)) == []


# ---------------------------------------------- the shape a comment can take


def test_a_finding_carries_exactly_what_a_review_comment_wants(tmp_path):
    """So a run can be delivered through the existing `comment --review` path
    without reshaping anything. Asserted against the real signature, because a
    shape that merely looks right is how this drifts."""
    from manuscriptor.server import drain

    d = manuscript(tmp_path, frag="")
    finding = by_check(preflight.run(d), "fragments").findings[0]
    kwargs = finding.as_comment()
    accepted = inspect.signature(drain.comment).parameters

    assert set(kwargs) <= set(accepted)
    assert kwargs["review"] is True
    assert kwargs["check"] == "fragments"
    assert kwargs["body"] and kwargs["quote"]


def test_a_finding_could_be_filed_and_would_land_in_review(tmp_path):
    """Not the UI wiring, just proof the shape survives contact with `drain`."""
    from manuscriptor.server import chat, drain

    d = manuscript(tmp_path, frag="")
    finding = by_check(preflight.run(d), "fragments").findings[0]
    rec = drain.comment(d, author="preflight", **finding.as_comment())

    assert rec is not None
    states = {c.id: c.state for c in chat.read_chats(drain.paths.comments(d))}
    assert states[rec["id"]] == "review"


# ------------------------------------------------------------------- the CLI


def test_the_command_reports_and_modifies_nothing(tmp_path, capsys):
    """Same posture as `tidy`. This runs against real manuscripts."""
    from manuscriptor import cli

    d = manuscript(tmp_path, frag="")
    before = snapshot(d)
    assert cli.main(["preflight", str(d)]) == 1
    assert snapshot(d) == before

    out = capsys.readouterr().out
    assert "contributes nothing" in out and "Nothing was modified" in out


def test_the_command_exits_two_when_a_check_could_not_run(tmp_path, capsys):
    from manuscriptor import cli

    d = manuscript(tmp_path, frag="")
    (d / "style.bst").unlink()
    assert cli.main(["preflight", str(d)]) == 2
    assert "not a pass" in capsys.readouterr().out


def test_the_command_exits_zero_on_a_clean_manuscript(tmp_path, capsys):
    from manuscriptor import cli

    d = manuscript(tmp_path)
    assert cli.main(["preflight", str(d)]) == 0
    assert "all clean" in capsys.readouterr().out


def test_every_document_in_the_directory_is_checked_not_only_the_main_one(tmp_path):
    """The supplement was built by a rule that skipped bibtex entirely. Checking
    only the main paper reported the paper healthy while the supplement shipped
    a frozen bibliography."""
    d = manuscript(tmp_path)
    (d / "supplement.tex").write_text(
        "\\documentclass{article}\n\\begin{document}\n"
        "See Table~S22 for the rest.\n\\end{document}\n", encoding="utf-8")

    results = preflight.run(d)
    assert by_check(results, "exhibit-numbers", "supplement.tex").status == "findings"
    assert by_check(results, "exhibit-numbers", "main.tex").status == "ok"


def test_one_document_can_be_asked_for_on_its_own(tmp_path):
    d = manuscript(tmp_path)
    (d / "supplement.tex").write_text(
        "\\documentclass{article}\n\\begin{document}\nTable~S22\n\\end{document}\n",
        encoding="utf-8")
    planned = preflight.plan(d, "supplement.tex")

    assert {doc for _, doc in planned} == {"supplement.tex", preflight.SCRIPTS}
    assert preflight.audit(planned, preflight.run(d, "supplement.tex")) == []
