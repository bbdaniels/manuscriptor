"""Insertion: three coordinated multi-file writes that either all land or none do.

The guarantees under test are mostly negative, because that is where the damage
would be.

  * NO FIELD ANYWHERE ACCEPTS A LITERAL NUMBER. A value enters the manuscript
    only as an `\\input` of a file some script wrote, so the planner takes an
    expression and refuses one that is just a number wearing a costume.
  * A citation that fails the identity gate is not inserted, and the failure
    names which check failed rather than saying no.
  * A failed step rolls back the steps before it. The bib is byte-identical, a
    created fragment is gone, and a Zotero record created a moment ago is
    removed.
  * Every write is previewed before it happens, and what lands is what the
    preview said.
"""
from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from manuscriptor.source import insert as ins
from manuscriptor.source.blocks import segment
from manuscriptor.source.flatten import flatten
from manuscriptor.server import producers


# --------------------------------------------------------------- a manuscript

BODY = (
    "\\documentclass{article}\n"
    "\\begin{document}\n"
    "\\section{Results}\n"
    "\n"
    "Enrolment raised statin prescribing in the treated clinics.\n"
    "\n"
    "The effect was concentrated among high-risk patients.\n"
    "\\end{document}\n"
)

SCRIPT = (
    "# 07_table2_cross.R\n"
    "library(data.table)\n"
    "writeLines(tex_output, file.path(project_path, 'latex', 'tables', 'table2_cross.tex'))\n"
)

RUNFILE = (
    "# RUNFILE\n"
    'source("00_global.R")\n'
    'source("07_table2_cross.R")\n'
    "\n"
    'cat("Analysis complete!\\n")\n'
)

BIB = (
    "@article{king2018multimorbidity,\n"
    "  title = {Multimorbidity},\n"
    "  author = {King, Ada},\n"
    "  year = {2018},\n"
    "  doi = {10.1/king},\n"
    "}\n"
)


@pytest.fixture
def repo(tmp_path: Path):
    """A manuscript in `latex/` beside its analysis code in `code/`."""
    latex = tmp_path / "latex"
    (latex / "tables").mkdir(parents=True)
    (latex / "main.tex").write_text(BODY, encoding="utf-8")
    (latex / "references.bib").write_text(BIB, encoding="utf-8")
    (latex / "tables" / "table2_cross.tex").write_text("\\toprule a & b \\\\\n", encoding="utf-8")

    code = tmp_path / "code"
    code.mkdir()
    (code / "07_table2_cross.R").write_text(SCRIPT, encoding="utf-8")
    (code / "runfile.R").write_text(RUNFILE, encoding="utf-8")
    return tmp_path


def blocks_of(repo: Path):
    return segment(flatten(repo / "latex" / "main.tex"))


def para(repo: Path, needle: str = "statin"):
    for b in blocks_of(repo):
        if needle in b.source_text:
            return b
    raise AssertionError(f"no block containing {needle!r}")


def produced_of(repo: Path):
    return producers.scan(repo / "latex")


# ------------------------------------------------------------- fake outsiders


class FakeNet:
    """Crossref and OpenAlex, without the network."""

    def __init__(self, crossref=None, openalex=None, search=None):
        self.crossref = crossref or {}
        self.openalex = openalex or {}
        self.search = search or []

    def crossref_search(self, query):
        return list(self.search)

    def crossref_by_doi(self, doi):
        return self.crossref.get(doi)

    def openalex_by_doi(self, doi):
        return self.openalex.get(doi)


class FakeLibrary:
    def __init__(self, items=None, fail_add=False):
        self.items = list(items or [])
        self.added: list[str] = []
        self.removed: list[str] = []
        self.fail_add = fail_add

    def find(self, *, doi=None, title=None):
        for it in self.items:
            if doi and (it.get("doi") or "").lower() == doi.lower():
                return it
            if title and it.get("title", "").lower() == title.lower():
                return it
        return None

    def add_by_doi(self, doi):
        if self.fail_add:
            raise ins.LibraryError("Zotero refused the import")
        self.added.append(doi)
        item = {"key": "ZKEY" + str(len(self.added)), "doi": doi,
                "title": "Persistence of provider behaviour", "has_fulltext": False}
        self.items.append(item)
        return item

    def remove(self, key):
        self.removed.append(key)
        self.items = [i for i in self.items if i.get("key") != key]


PAPER = {
    "doi": "10.1257/aer.20180338",
    "title": "Persistence of provider behaviour",
    "year": 2019,
    "authors": ["Nishtar", "Habicht"],
    "journal": "American Economic Review",
    "volume": "109",
    "pages": "1--40",
}


def agreeing_net():
    return FakeNet(
        crossref={PAPER["doi"]: dict(PAPER)},
        openalex={PAPER["doi"]: dict(PAPER)},
        search=[dict(PAPER)],
    )


def cite_plan(repo, *, net=None, library=None, caret=None, base=None, query="provider behaviour"):
    b = para(repo)
    return ins.plan_citation(
        query=query,
        block=b,
        caret=len(b.source_text) - 1 if caret is None else caret,
        base=base,
        root=repo / "latex",
        bib_path=repo / "latex" / "references.bib",
        net=net or agreeing_net(),
        library=library if library is not None else FakeLibrary(),
    )


# ============================================================ the literal rule


def test_no_planner_accepts_a_literal_value_field():
    """There must be no door for a typed number. Not a style rule: the only way
    a value may enter the manuscript is as an \\input of a file code wrote."""
    banned = {"value", "number", "literal", "result", "amount", "figure_value"}
    for name in ("plan_citation", "plan_value", "plan_exhibit"):
        params = set(inspect.signature(getattr(ins, name)).parameters)
        assert not (params & banned), f"{name} exposes {params & banned}"


@pytest.mark.parametrize(
    "expr",
    ["0.32", " 0.32 ", "32%", "$0.32$", "\\num{0.32}", "-1.4", "1,024", "12 percent", "0.32\\%"],
)
def test_a_value_that_is_just_a_number_is_refused(repo, expr):
    plan = ins.plan_value(
        key="statin_control_mean", description="control mean of statin prescribing",
        expression=expr, script=repo / "code" / "07_table2_cross.R",
        block=para(repo), caret=5, root=repo / "latex", produced=produced_of(repo),
    )
    assert not plan.ok
    assert "literal" in plan.blocked.lower()
    assert plan.writes == ()


def test_an_expression_that_computes_is_accepted(repo):
    plan = ins.plan_value(
        key="statin_control_mean", description="control mean of statin prescribing",
        expression="round(mean(dta[treat == 0]$statin), 3)",
        script=repo / "code" / "07_table2_cross.R",
        block=para(repo), caret=5, root=repo / "latex", produced=produced_of(repo),
    )
    assert plan.ok, plan.blocked
    assert len(plan.writes) == 3


def test_the_fragment_a_value_creates_carries_no_digit(repo):
    """It is a placeholder awaiting the script, not a result. A digit in it
    would be a hardcoded value by another name."""
    plan = ins.plan_value(
        key="statin_control_mean", description="control mean",
        expression="mean(x)", script=repo / "code" / "07_table2_cross.R",
        block=para(repo), caret=5, root=repo / "latex", produced=produced_of(repo),
    )
    frag = [w for w in plan.writes if w.kind == "create"][0]
    # Everything a reader would ever see: TeX comments render nothing, so a
    # script name with digits in it is invisible, but the payload must not be a
    # number of any kind.
    payload = "\n".join(l for l in frag.preview.split("\n") if not l.lstrip().startswith("%"))
    assert not any(c.isdigit() for c in payload), payload


# ============================================================== citation gate


def test_a_candidate_without_a_doi_is_blocked_and_says_so(repo):
    net = FakeNet(search=[{"title": "No identifier here", "year": 2020}])
    plan = cite_plan(repo, net=net)
    assert not plan.ok
    assert plan.failed_check == "doi"
    assert plan.writes == ()


def test_crossref_not_holding_the_doi_is_blocked(repo):
    net = FakeNet(crossref={}, openalex={PAPER["doi"]: dict(PAPER)}, search=[dict(PAPER)])
    plan = cite_plan(repo, net=net)
    assert not plan.ok
    assert plan.failed_check == "crossref"


def test_openalex_not_holding_the_doi_is_blocked(repo):
    net = FakeNet(crossref={PAPER["doi"]: dict(PAPER)}, openalex={}, search=[dict(PAPER)])
    plan = cite_plan(repo, net=net)
    assert not plan.ok
    assert plan.failed_check == "openalex"


def test_crossref_and_openalex_disagreeing_is_blocked(repo):
    other = dict(PAPER, title="An entirely different paper about something else")
    net = FakeNet(crossref={PAPER["doi"]: dict(PAPER)},
                  openalex={PAPER["doi"]: other}, search=[dict(PAPER)])
    plan = cite_plan(repo, net=net)
    assert not plan.ok
    assert plan.failed_check == "agreement"
    assert plan.writes == ()


def test_a_candidate_that_has_nothing_to_do_with_the_query_is_blocked(repo):
    """Found by running the real thing: Crossref answers EVERY query with
    something. A nonsense search returned a real, resolvable, agreed-upon DOI
    for a book chapter about colonial legacies, and every identity check passed
    on it. Identity is not relevance, and inserting a plausible wrong citation
    is worse than inserting none."""
    other = dict(PAPER, title="Colonial legacies and the administration of empire",
                 authors=["Someone"], year=2021)
    net = FakeNet(crossref={PAPER["doi"]: other}, openalex={PAPER["doi"]: other},
                  search=[other])
    plan = cite_plan(repo, net=net, query="statin prescribing after enrolment in Estonia")
    assert not plan.ok
    assert plan.failed_check == "match"
    assert plan.writes == ()


def test_a_doi_typed_by_hand_is_not_asked_to_match_anything(repo):
    """He typed the identifier. There is no query left to be relevant to."""
    net = agreeing_net()
    plan = cite_plan(repo, net=net, query=PAPER["doi"])
    assert plan.ok, plan.blocked


def test_an_author_and_year_query_matches(repo):
    plan = cite_plan(repo, query="Nishtar 2019 provider behaviour")
    assert plan.ok, plan.blocked


def test_every_check_is_reported_not_just_the_failing_one(repo):
    plan = cite_plan(repo, net=FakeNet(search=[{"title": "No identifier here"}]))
    names = [c.name for c in plan.checks]
    assert names[:5] == ["doi", "crossref", "openalex", "agreement", "match"]
    assert "zotero" in names


# ========================================================== citation, writing


def test_a_paper_already_in_zotero_is_not_added_again(repo):
    lib = FakeLibrary(items=[{"key": "ABC", "doi": PAPER["doi"],
                              "title": PAPER["title"], "has_fulltext": True}])
    plan = cite_plan(repo, library=lib)
    assert plan.ok, plan.blocked
    ins.apply(plan, root=repo / "latex", library=lib)
    assert lib.added == []


def test_a_new_paper_lands_in_zotero_the_bib_and_the_paragraph(repo):
    lib = FakeLibrary()
    plan = cite_plan(repo, library=lib)
    assert plan.ok, plan.blocked
    result = ins.apply(plan, root=repo / "latex", library=lib)
    assert result.ok, result.error

    assert lib.added == [PAPER["doi"]]
    bib = (repo / "latex" / "references.bib").read_text(encoding="utf-8")
    assert PAPER["doi"] in bib
    assert bib.startswith(BIB)                     # appended, never rewritten
    main = (repo / "latex" / "main.tex").read_text(encoding="utf-8")
    assert "\\citep{" + plan.cite_key + "}" in main
    assert "Enrolment raised statin prescribing" in main


def test_the_citation_lands_at_the_caret_not_at_the_end(repo):
    b = para(repo)
    at = b.source_text.index(" in the treated")
    plan = cite_plan(repo, caret=at)
    assert plan.ok, plan.blocked
    ins.apply(plan, root=repo / "latex", library=FakeLibrary())
    main = (repo / "latex" / "main.tex").read_text(encoding="utf-8")
    assert "statin prescribing\\citep{" in main
    assert "clinics\\citep{" not in main, "it went to the end of the block, not the caret"


def test_a_key_already_in_the_bib_is_not_duplicated(repo):
    key = ins.bib_key(PAPER, taken={})
    bib_path = repo / "latex" / "references.bib"
    bib_path.write_text(
        BIB + "@article{%s,\n  doi = {%s},\n}\n" % (key, PAPER["doi"]), encoding="utf-8"
    )
    before = bib_path.read_text(encoding="utf-8")
    plan = cite_plan(repo)
    assert plan.ok, plan.blocked
    assert not any(w.kind == "append" and w.path == bib_path for w in plan.writes)
    ins.apply(plan, root=repo / "latex", library=FakeLibrary())
    assert bib_path.read_text(encoding="utf-8") == before


def test_a_zotero_import_that_fails_blocks_and_writes_nothing(repo):
    lib = FakeLibrary(fail_add=True)
    bib_before = (repo / "latex" / "references.bib").read_text(encoding="utf-8")
    main_before = (repo / "latex" / "main.tex").read_text(encoding="utf-8")
    plan = cite_plan(repo, library=lib)
    result = ins.apply(plan, root=repo / "latex", library=lib)
    assert not result.ok
    assert "zotero" in result.error.lower()
    assert (repo / "latex" / "references.bib").read_text(encoding="utf-8") == bib_before
    assert (repo / "latex" / "main.tex").read_text(encoding="utf-8") == main_before


# =================================================================== rollback


def test_a_stale_block_rolls_back_the_bib_and_the_zotero_record(repo):
    lib = FakeLibrary()
    plan = cite_plan(repo, library=lib)
    bib_before = (repo / "latex" / "references.bib").read_text(encoding="utf-8")

    # Somebody else rewrote the paragraph between the preview and the write.
    main = repo / "latex" / "main.tex"
    main.write_text(
        BODY.replace("Enrolment raised statin prescribing in the treated clinics.",
                     "Someone else rewrote this paragraph entirely."),
        encoding="utf-8",
    )
    result = ins.apply(plan, root=repo / "latex", library=lib)

    assert not result.ok
    assert (repo / "latex" / "references.bib").read_text(encoding="utf-8") == bib_before
    assert lib.removed == ["ZKEY1"], "the record created a moment ago must be removed"


def test_the_manuscript_is_never_touched_when_an_earlier_step_fails(repo):
    """The splice runs last, and this is the consequence that shows it.

    Restoring bytes is not enough on its own. The manuscript is watched, so a
    write that is later rolled back still fires a rebuild and redraws the page
    on a state that is about to be undone. Ordering the splice last means the
    file is never opened at all when something ahead of it refuses.
    """
    main = repo / "latex" / "main.tex"
    bib = repo / "latex" / "references.bib"
    plan = cite_plan(repo, library=FakeLibrary())
    assert [w.kind for w in plan.writes] == ["append", "splice"]

    before = main.stat().st_mtime_ns
    bib.chmod(0o444)
    try:
        result = ins.apply(plan, root=repo / "latex", library=FakeLibrary())
    finally:
        bib.chmod(0o644)

    assert not result.ok
    assert main.stat().st_mtime_ns == before, "the splice ran before the step that failed"


def test_a_failed_script_write_leaves_no_fragment_behind(repo):
    plan = ins.plan_value(
        key="statin_control_mean", description="control mean",
        expression="mean(x)", script=repo / "code" / "07_table2_cross.R",
        block=para(repo), caret=5, root=repo / "latex", produced=produced_of(repo),
    )
    frag = [w for w in plan.writes if w.kind == "create"][0]
    main_before = (repo / "latex" / "main.tex").read_text(encoding="utf-8")

    script = repo / "code" / "07_table2_cross.R"
    script_before = script.read_text(encoding="utf-8")
    script.chmod(0o444)
    try:
        result = ins.apply(plan, root=repo / "latex")
    finally:
        script.chmod(0o644)

    assert not result.ok
    assert not frag.path.exists(), "the fragment must not survive a failed run"
    assert script.read_text(encoding="utf-8") == script_before
    assert (repo / "latex" / "main.tex").read_text(encoding="utf-8") == main_before


# ================================================================ value, writing


def test_a_value_writes_three_places(repo):
    plan = ins.plan_value(
        key="statin_control_mean", description="the control-group mean of statin prescribing",
        expression="round(mean(dta[treat == 0]$statin), 3)",
        script=repo / "code" / "07_table2_cross.R",
        block=para(repo), caret=len(para(repo).source_text) - 1,
        root=repo / "latex", produced=produced_of(repo),
    )
    assert plan.ok, plan.blocked
    result = ins.apply(plan, root=repo / "latex")
    assert result.ok, result.error

    frag = repo / "latex" / "exhibits" / "statin_control_mean.tex"
    assert frag.exists()
    script = (repo / "code" / "07_table2_cross.R").read_text(encoding="utf-8")
    assert script.startswith(SCRIPT)                              # appended
    assert "round(mean(dta[treat == 0]$statin), 3)" in script
    assert "statin_control_mean.tex" in script
    assert "the control-group mean of statin prescribing" in script
    main = (repo / "latex" / "main.tex").read_text(encoding="utf-8")
    assert "\\input{exhibits/statin_control_mean}" in main


def test_the_new_input_survives_the_next_parse_as_an_include(repo):
    """The point of the whole exercise: the manuscript now carries a directive
    the block map tracks, not a number."""
    plan = ins.plan_value(
        key="statin_control_mean", description="control mean", expression="mean(x)",
        script=repo / "code" / "07_table2_cross.R",
        block=para(repo), caret=len(para(repo).source_text) - 1,
        root=repo / "latex", produced=produced_of(repo),
    )
    ins.apply(plan, root=repo / "latex")
    hit = para(repo, "statin prescribing")
    assert any("statin_control_mean" in inc.directive for inc in hit.includes)


def test_a_script_outside_the_known_producers_is_refused(repo, tmp_path_factory):
    """The path comes off a form. Anything not already an analysis script of
    this project is somewhere the page has no business writing."""
    stray = tmp_path_factory.mktemp("outside") / "hack.R"
    stray.write_text("# not part of this project\n", encoding="utf-8")
    plan = ins.plan_value(
        key="k", description="d", expression="mean(x)", script=stray,
        block=para(repo), caret=5, root=repo / "latex", produced=produced_of(repo),
    )
    assert not plan.ok
    assert "script" in plan.blocked.lower()


def test_a_script_in_a_language_with_no_recipe_is_refused_not_guessed(repo):
    odd = repo / "code" / "07_table2_cross.pl"
    odd.write_text("# perl\n", encoding="utf-8")
    plan = ins.plan_value(
        key="k", description="d", expression="mean(x)", script=odd,
        block=para(repo), caret=5, root=repo / "latex",
        produced={repo / "latex" / "tables" / "table2_cross.tex": odd},
    )
    assert not plan.ok
    assert ".pl" in plan.blocked or "language" in plan.blocked.lower()


# ============================================================== exhibit, writing


def test_an_exhibit_writes_four_places_including_the_runfile(repo):
    runfile = repo / "code" / "runfile.R"
    new_script = repo / "code" / "11_tableX_sex.R"
    new_script.write_text("# new\n", encoding="utf-8")

    plan = ins.plan_exhibit(
        key="tableX_sex", caption="Effects by patient sex", expression="tex_output",
        script=new_script, block=para(repo), root=repo / "latex",
        produced=produced_of(repo), runfile=runfile,
    )
    assert plan.ok, plan.blocked
    assert {w.kind for w in plan.writes} == {"create", "append", "runfile", "splice"}

    result = ins.apply(plan, root=repo / "latex")
    assert result.ok, result.error
    rf = runfile.read_text(encoding="utf-8")
    assert 'source("11_tableX_sex.R")' in rf
    # Placed with the other sources, not after the closing banner.
    assert rf.index('source("11_tableX_sex.R")') < rf.index("Analysis complete")


def test_an_exhibit_whose_script_is_already_in_the_runfile_writes_no_runfile_line(repo):
    runfile = repo / "code" / "runfile.R"
    before = runfile.read_text(encoding="utf-8")
    plan = ins.plan_exhibit(
        key="table2_cross_sex", caption="Effects by sex", expression="tex_output",
        script=repo / "code" / "07_table2_cross.R", block=para(repo),
        root=repo / "latex", produced=produced_of(repo), runfile=runfile,
    )
    assert plan.ok, plan.blocked
    assert not any(w.kind == "runfile" for w in plan.writes)
    assert any(c.name == "runfile" and c.ok for c in plan.checks)
    ins.apply(plan, root=repo / "latex")
    assert runfile.read_text(encoding="utf-8") == before


def test_a_fragment_that_already_exists_is_refused_rather_than_clobbered(repo):
    plan = ins.plan_exhibit(
        key="table2_cross", caption="Effects", expression="tex_output",
        script=repo / "code" / "07_table2_cross.R", block=para(repo),
        root=repo / "latex", produced=produced_of(repo),
        runfile=repo / "code" / "runfile.R",
    )
    assert not plan.ok
    assert "table2_cross.tex" in plan.blocked


def test_an_exhibit_mints_a_new_block_after_the_paragraph(repo):
    plan = ins.plan_exhibit(
        key="table2_cross_sex", caption="Effects by patient sex", expression="tex_output",
        script=repo / "code" / "07_table2_cross.R", block=para(repo),
        root=repo / "latex", produced=produced_of(repo),
        runfile=repo / "code" / "runfile.R",
    )
    ins.apply(plan, root=repo / "latex")
    main = (repo / "latex" / "main.tex").read_text(encoding="utf-8")

    assert "Enrolment raised statin prescribing in the treated clinics." in main
    assert "\\caption{Effects by patient sex}" in main
    assert "\\label{tab:table2_cross_sex}" in main
    assert "\\input{tables/table2_cross_sex}" in main
    assert main.index("statin prescribing") < main.index("\\caption{")
    assert main.index("\\caption{") < main.index("The effect was concentrated")

    kinds = {b.kind for b in blocks_of(repo)}
    assert "table" in kinds


# ================================================================ the preview


def test_every_write_previews_the_exact_text_it_will_add(repo):
    plan = cite_plan(repo)
    assert plan.writes
    for w in plan.writes:
        assert w.preview.strip(), w.label
        assert w.label
    ins.apply(plan, root=repo / "latex", library=FakeLibrary())
    for w in plan.writes:
        assert w.preview.strip() in w.path.read_text(encoding="utf-8"), w.label


def test_a_plan_is_written_once_and_only_once(repo):
    """The token is the confirmation. A second click must not write twice."""
    plan = cite_plan(repo)
    token = ins.stash(plan)
    assert ins.take(token) is plan
    assert ins.take(token) is None


def test_a_blocked_plan_cannot_be_applied(repo):
    plan = cite_plan(repo, net=FakeNet(search=[{"title": "no doi"}]))
    result = ins.apply(plan, root=repo / "latex", library=FakeLibrary())
    assert not result.ok
    assert "doi" in result.error.lower()


def test_a_block_that_moved_under_the_form_is_refused_at_plan_time(repo):
    plan = cite_plan(repo, base="Some text this block never had.")
    assert not plan.ok
    assert "changed" in plan.blocked.lower() or "moved" in plan.blocked.lower()


# ================================================================== the route


class FakeBuild:
    def __init__(self, blocks):
        self.blocks = tuple(blocks)
        self.by_id = {b.id: b for b in self.blocks}


def a_build(repo):
    return FakeBuild(blocks_of(repo))


def test_a_read_only_session_refuses_to_plan_anything(repo):
    out = ins.handle(
        {"stage": "plan", "kind": "citation", "block": para(repo).id, "query": "anything"},
        root=repo / "latex", build=a_build(repo), read_only=True,
    )
    assert not out["ok"]
    assert "read-only" in out["error"]
    assert out["status"] == 403


def test_context_offers_the_scripts_that_already_write_this_manuscript(repo):
    out = ins.handle({"stage": "context", "block": para(repo).id},
                     root=repo / "latex", build=a_build(repo), read_only=False)
    names = [s["name"] for s in out["scripts"]]
    assert "07_table2_cross.R" in names
    assert out["runfile"] == "runfile.R"
    assert out["table_dir"] == "tables"
    owner = [s for s in out["scripts"] if s["name"] == "07_table2_cross.R"][0]
    assert owner["outputs"] == ["table2_cross.tex"]


def test_plan_then_apply_over_the_route_writes_the_files(repo):
    lib = FakeLibrary()
    planned = ins.handle(
        {"stage": "plan", "kind": "citation", "block": para(repo).id,
         "query": "provider behaviour", "caret": 5, "base": para(repo).source_text},
        root=repo / "latex", build=a_build(repo), read_only=False,
        bib_path=repo / "latex" / "references.bib", net=agreeing_net(), library=lib,
    )
    assert planned["ok"], planned
    assert planned["plan"]["writes"]
    assert planned["token"]

    done = ins.handle({"stage": "apply", "token": planned["token"]},
                      root=repo / "latex", build=a_build(repo), read_only=False, library=lib)
    assert done["ok"], done
    assert "\\citep{" in (repo / "latex" / "main.tex").read_text(encoding="utf-8")

    again = ins.handle({"stage": "apply", "token": planned["token"]},
                       root=repo / "latex", build=a_build(repo), read_only=False, library=lib)
    assert not again["ok"], "a token must not be redeemable twice"


def test_a_blocked_plan_hands_back_no_token(repo):
    out = ins.handle(
        {"stage": "plan", "kind": "value", "block": para(repo).id,
         "key": "k", "description": "d", "expression": "0.32",
         "script": str(repo / "code" / "07_table2_cross.R"), "caret": 3},
        root=repo / "latex", build=a_build(repo), read_only=False,
    )
    assert not out["ok"]
    assert out["token"] is None
    assert "literal" in out["plan"]["blocked"].lower()


# ============================================================== the real Zotero


@pytest.mark.parametrize(
    "payload, key",
    [
        # What `zotero-cli --json import doi` actually returns: a JSON STRING,
        # not an object. Parsing it as an object found no key, so a successful
        # import reported failure, the whole plan rolled back, and the page said
        # nothing had been written -- while the record sat in the library. Found
        # by running it, not by reading the CLI's help.
        ('"OK: imported Spatial competition and quality (key: IUD3RBPZ)"', "IUD3RBPZ"),
        ('{"key": "ABCD2345"}', "ABCD2345"),
        ('{"item": {"itemKey": "ZZZZ9999"}}', "ZZZZ9999"),
        ("OK: imported something (key: PLAIN123)", "PLAIN123"),
        ('"failed: nothing imported"', None),
    ],
)
def test_the_zotero_key_is_read_out_of_every_shape_the_cli_returns(payload, key):
    assert ins._first_item_key(payload) == key


def test_an_import_whose_answer_is_unreadable_is_recovered_from_the_library(monkeypatch):
    """An import that succeeded but was reported as failed leaves a record
    nothing can roll back, because rollback needs the key it never learned. It
    happened: a live run put an orphan into the library. The library itself
    knows, so it is asked."""
    lib = ins.ZoteroLibrary()
    monkeypatch.setattr(lib, "_cli", lambda args: "a sentence with no key in it")
    monkeypatch.setattr(lib, "find", lambda **kw: {"key": "RECOV123", "doi": kw.get("doi")})
    assert lib.add_by_doi("10.1/x")["key"] == "RECOV123"


def test_an_import_that_really_failed_still_raises(monkeypatch):
    lib = ins.ZoteroLibrary()
    monkeypatch.setattr(lib, "_cli", lambda args: "a sentence with no key in it")
    monkeypatch.setattr(lib, "find", lambda **kw: None)
    with pytest.raises(ins.LibraryError):
        lib.add_by_doi("10.1/x")


# ================================================================== the client


EXT_JS = Path(__file__).resolve().parents[1] / "manuscriptor" / "templates" / "static" / "ext" / "insert.js"


def test_the_extension_is_picked_up_by_the_loader():
    from manuscriptor.templates.ext import load

    assert "insert" in load()


@pytest.mark.skipif(not __import__("shutil").which("node"), reason="node not installed")
def test_the_extension_parses():
    import subprocess

    p = subprocess.run(["node", "--check", str(EXT_JS)], capture_output=True, text=True)
    assert p.returncode == 0, p.stderr


def test_the_extension_claims_the_preview_button_and_leaves_the_footnote_alone():
    """The footnote already splices directly and the new-paragraph form still
    goes to chat. Claiming either would break a control that works."""
    js = EXT_JS.read_text(encoding="utf-8")
    assert "'ins:go': function" in js
    assert "'ins:footnote'" not in js
    assert "MSViewer.extend" in js


def test_the_client_never_posts_a_plan_back():
    """The token is the confirmation. A plan travelling back through the page
    would be the literal-number field this whole design exists to not have."""
    js = EXT_JS.read_text(encoding="utf-8")
    assert "post({ stage: 'apply', token: out.token })" in js, (
        "the apply body must carry the token and nothing else")


def test_the_route_is_mounted_on_the_app(repo):
    """The page reaches this over HTTP, so a handler nothing routes to is a
    feature that does not exist."""
    from manuscriptor.server.app import make_app

    class FakeSession:
        def __init__(self, root, build):
            self.dir, self.build = root, build
            self.read_only, self.bib, self.main = False, None, None
            self.clients = set()

    app = make_app(FakeSession(repo / "latex", a_build(repo)))
    routes = {(r.method, r.resource.canonical) for r in app.router.routes()}
    assert ("POST", "/insert") in routes
