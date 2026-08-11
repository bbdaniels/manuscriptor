"""Stage 05's export, and the payload it spent the whole pipeline computing.

`manuscriptor evidence` reads PDFs, calls a model, verifies every quote against
the cited fulltext -- and then `evidence/render.py` writes `index.html`. For
every release between 1af705e and this file, that last step threw the results
away.

The export and the served editor were once the same three filenames:
`templates/index.html.j2` over `static/styles.css` and `static/viewer.js`.
1af705e wrote the editor over all three. `render.py` was never repointed, so it
went on handing `claims_json`, `citations_by_key_json`, `evidence_by_pair_json`
and four counts to a template that consumes exactly one variable, `ms`. None of
them were read. The page still rendered -- the editor's compatibility fallback
picked up `manuscript_html`, so the prose and the coloured citation underlines
appeared and the export looked broadly right -- while every quote, every
resolved title, and all four counts were dropped in silence. Nothing raised,
nothing logged, and the stdout summary printed the correct numbers the page did
not contain.

So these tests assert on the bytes of `index.html`, never on the values
`render.run` computed on its way there. The distinction is the entire bug: the
old code computed all of it correctly.
"""
from __future__ import annotations

import json
from pathlib import Path

from manuscriptor.evidence import render

MANUSCRIPT_HTML = (
    '<h1>Named Nurses and the Annual Check</h1>\n'
    '<p>Effects were large '
    '<span class="citation" data-cites="doe2020example">(Doe et al. 2020)</span> '
    'and persistent '
    '<span class="citation" data-cites="roe2019sample">(Roe 2019)</span>.</p>\n'
)

MAIN_TEX = r"""\documentclass{article}
\title{Named Nurses and the Annual Check}
\begin{document}
\section{Results}
Effects were large \citep{doe2020example} and persistent \citep{roe2019sample}.
\end{document}
"""

CLAIMS = [
    {"claim_id": "cl-1", "sentence": "Effects were large.",
     "section": "sec:Results", "cite_keys": ["doe2020example"]},
    {"claim_id": "cl-2", "sentence": "Effects were persistent.",
     "section": "sec:Results", "cite_keys": ["roe2019sample"]},
]

CITATIONS = [
    {"cite_key": "doe2020example", "title": "A Check Nobody Is Asked to Keep",
     "authors": ["Doe, Jane"], "year": "2020", "journal": "Journal of Example Studies",
     "doi": "10.5555/example.2020.0114", "zotero_key": "ZKEY9",
     "has_fulltext": True, "fulltext_source": "zotero"},
    {"cite_key": "roe2019sample", "title": "Rosters, Queues, and Who Gets Seen",
     "authors": ["Roe, Alex"], "year": "2019", "journal": "Rev. Placeholder Econ.",
     "has_fulltext": False, "fulltext_source": "missing"},
]

EVIDENCE = [
    {"claim_id": "cl-1", "cite_key": "doe2020example",
     "quotes": [{"text": "completion of the annual check rose in every treated clinic",
                 "status": "verbatim", "location_hint": "p. 12"}]},
]


def export(tmp_path: Path, *, claims=None, citations=None, evidence=None,
           manuscript_html: str = MANUSCRIPT_HTML, main_tex: str = MAIN_TEX) -> str:
    """Run stage 05 over a fixture and hand back the page it wrote."""
    out = tmp_path / "build"
    out.mkdir(parents=True, exist_ok=True)
    main = tmp_path / "main.tex"
    main.write_text(main_tex, encoding="utf-8")
    (out / "manuscript.html").write_text(manuscript_html, encoding="utf-8")
    (out / "claims.json").write_text(
        json.dumps(CLAIMS if claims is None else claims), encoding="utf-8")
    (out / "citations.json").write_text(
        json.dumps(CITATIONS if citations is None else citations), encoding="utf-8")
    (out / "evidence.json").write_text(
        json.dumps(EVIDENCE if evidence is None else evidence), encoding="utf-8")
    render.run(output_dir=out, main_tex=main)
    return (out / "index.html").read_text(encoding="utf-8")


# --------------------------------------------------------------- the payload


def test_the_extracted_quote_reaches_the_page(tmp_path):
    """The one thing the whole pipeline exists to produce.

    This is the assertion the old export failed. The quote was read, verified
    verbatim against the fulltext, indexed into `evidence_by_pair`, serialized,
    and passed to a template with no `evidence_by_pair` in it. It never left the
    process.
    """
    page = export(tmp_path)
    assert "completion of the annual check rose in every treated clinic" in page


def test_the_resolved_citation_metadata_reaches_the_page(tmp_path):
    """Resolving a cite key to a real paper is stage 02's whole job.

    A page that says a quote was found but cannot say what it was found in
    answers nothing.
    """
    page = export(tmp_path)
    assert "A Check Nobody Is Asked to Keep" in page
    assert "doe2020example" in page
    assert "10.5555/example.2020.0114" in page


def test_the_four_counts_reach_the_page(tmp_path):
    """The counts printed to stdout and the counts on the page were different
    numbers living in different places. Only stdout ever had them."""
    page = export(tmp_path)
    assert "2 cite-instances" in page
    assert "1 verbatim" in page
    assert "0 paraphrase" in page
    assert "1 missing" in page


def test_the_manuscript_and_its_wired_spans_reach_the_page(tmp_path):
    """The prose survived the old bug, via a fallback in the editor's template.

    It is asserted here anyway: it is now carried by this stage's own template
    and would otherwise be the one part of the export with no test behind it.
    """
    page = export(tmp_path)
    assert "Effects were large" in page
    assert 'data-claim-id="cl-1"' in page
    assert "status-verbatim" in page


def test_every_computed_value_is_consumed_by_the_template(tmp_path):
    """The failure was never a wrong value, it was an unread one.

    So the guard is coverage, not correctness: each variable `run()` computes
    must show up in the bytes. A variable nothing reads is how this started.
    """
    page = export(tmp_path)
    for fragment, what in [
        ("Named Nurses and the Annual Check", "title"),
        ("Effects were large", "manuscript_html"),
        ('"cl-1"', "claims_json"),
        ('"A Check Nobody Is Asked to Keep"', "citations_by_key_json"),
        ("completion of the annual check rose", "evidence_by_pair_json"),
        ("2 cite-instances", "n_pairs"),
        ("1 verbatim", "n_verbatim"),
        ("0 paraphrase", "n_paraphrase"),
        ("1 missing", "n_missing"),
        (" UTC", "generated_at"),
    ]:
        assert fragment in page, f"{what} never reached the page"


# ------------------------------------------------- the export is not the app


def test_the_export_does_not_ship_the_served_editor(tmp_path):
    """The collision guard, and the reason this file exists.

    Rendering the editor's template produced a page that carried `window.MS`
    and inlined its 154KB websocket client -- into a static file opened off
    disk, where there is no socket to open. If either of these ever reappears,
    stage 05 is pointed back at `index.html.j2`.
    """
    page = export(tmp_path)
    assert "window.MS" not in page
    assert "window.CITE_EVIDENCE" in page


def test_the_export_stays_small_enough_to_read(tmp_path):
    """A two-paragraph manuscript exported at 218KB, of which 208KB was the
    editor's CSS and script. Size is the cheapest tell that the wrong template
    is being rendered, and it is the one a human notices first."""
    page = export(tmp_path)
    assert len(page) < 60_000, f"export is {len(page):,} bytes; is it the editor's template?"


# ---------------------------------------------------------------- the status


def test_a_pair_the_model_could_not_support_reads_as_missing(tmp_path):
    """`roe2019sample` has no fulltext, so no quote and no evidence record.

    The page has to say so rather than omit the pair, which would read as a
    citation nobody checked.
    """
    page = export(tmp_path)
    assert "roe2019sample" in page
    assert "Rosters, Queues, and Who Gets Seen" in page
    assert "No supporting passage found" in page
    assert "status-missing" in page


def test_a_paraphrase_only_pair_reads_as_paraphrase_everywhere(tmp_path):
    """One status function, so the span underline and the panel agree.

    They were derived separately -- `_aggregate_status` in Python for the span,
    `cardStatus` in JS for the card -- which is two implementations of "how well
    is this pair supported".
    """
    page = export(
        tmp_path,
        evidence=[{"claim_id": "cl-1", "cite_key": "doe2020example",
                   "quotes": [{"text": "a broadly similar finding",
                               "status": "paraphrase"}]}],
    )
    assert "status-paraphrase" in page
    assert '"pair_status": "paraphrase"' in page
    assert "1 paraphrase" in page
    assert "0 verbatim" in page


def test_the_strongest_quote_sets_a_pair_status(tmp_path):
    page = export(
        tmp_path,
        evidence=[{"claim_id": "cl-1", "cite_key": "doe2020example",
                   "quotes": [{"text": "loosely put", "status": "paraphrase"},
                              {"text": "exactly put", "status": "verbatim"}]}],
    )
    assert '"pair_status": "verbatim"' in page


# ----------------------------------------------------------------- escaping


def test_a_quote_cannot_close_the_script_block(tmp_path):
    """Quotes are arbitrary text lifted out of a PDF.

    A literal `</script>` in one would end the data block early and drop the
    rest of the page, at exit 0, on a manuscript that cites a paper about HTML.
    """
    page = export(
        tmp_path,
        evidence=[{"claim_id": "cl-1", "cite_key": "doe2020example",
                   "quotes": [{"text": "authors wrote </script><b>oops</b> inline",
                               "status": "verbatim"}]}],
    )
    assert "</script><b>oops</b>" not in page
    assert "<\\/script>" in page, "the closing tag was not neutralised"
    # The script element still ends exactly where the template ends it.
    assert page.count("</script>") == 2


def test_a_title_with_markup_in_it_is_escaped_not_injected(tmp_path):
    page = export(
        tmp_path,
        main_tex=MAIN_TEX.replace(
            "Named Nurses and the Annual Check", "Effects of <b>Deworming</b>"),
    )
    assert "<b>Deworming</b>" not in page
    assert "&lt;b&gt;Deworming&lt;/b&gt;" in page


# -------------------------------------------------------------- degenerate


def test_a_manuscript_with_no_citations_still_exports(tmp_path):
    page = export(
        tmp_path, claims=[], citations=[], evidence=[],
        manuscript_html="<h1>Untitled</h1><p>No citations here.</p>",
    )
    assert "No citations here." in page
    assert "0 cite-instances" in page
    assert "nothing to check" in page


def test_a_pass_that_never_ran_extract_still_exports(tmp_path):
    """`evidence --skip-extract` is a supported flag; the page it makes is a
    reading copy with every pair red, not a crash."""
    page = export(tmp_path, evidence=[])
    assert "2 cite-instances" in page
    assert "0 verbatim" in page
    assert "2 missing" in page
