"""What the evidence pass looked for, and what it actually found.

Two failures, one run. On a real 42-citation manuscript the pass reported
"matched Zotero: 8 (19%)" and then "MISSING: 36" and exited 0, and every one
of those 36 misses carried the identical sentence

    no indexed fulltext available in Zotero or local caches

The library was open the whole time and held a PDF for at least 13 of them.
The sentence was false in two separate ways. Zotero was never asked, because
the title query still carried its BibTeX braces; and even the asking was
skipped, because both Zotero steps in `fetch.py` are gated on `zot_key` and
34 of the 36 had `zotero_key: null`. "Never looked" and "looked and found
nothing" printed the same words.

These guards hold the line on both: the query has to be plain text, and a
step that did not run may never report as a step that ran and came back
empty.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from manuscriptor.evidence import fetch, resolve


# --------------------------------------------------------------------------
# A stand-in for the local Zotero API that matches the way Zotero really
# matches: quicksearch splits the query on whitespace and requires every token
# as a case-insensitive substring of the indexed fields. That is precisely why
# a token spelled `{COVID-19}` finds nothing.
# --------------------------------------------------------------------------

LIBRARY = [
    {
        "key": "ARENTZKEY",
        "title": "The impact of the COVID-19 pandemic and associated suppression "
                 "measures on the burden of tuberculosis in India",
        "creators": [{"creatorType": "author", "lastName": "Arentz"}],
        "date": "2022",
        "DOI": "",
        "publicationTitle": "BMC Infectious Diseases",
        "itemType": "journalArticle",
    },
    {
        "key": "ARISKEY",
        "title": "COVID-19: endemic doesn't mean harmless",
        "creators": [{"creatorType": "author", "lastName": "Aris"}],
        "date": "2022",
        "DOI": "",
        "publicationTitle": "Nature",
        "itemType": "journalArticle",
    },
]

BIB = r"""
@article{arentz2022impact,
  title={The impact of the {COVID-19} pandemic and associated suppression measures on the burden of tuberculosis in {India}},
  author={Arentz, Matthew and Kyu, Hmwe H},
  journal={{BMC} Infectious Diseases},
  year={2022}
}
@article{aris2022world,
  title={{COVID-19}: Endemic doesn't mean harmless},
  author={Aris, Emmanuel and {van der Berg}, Servaas},
  journal={Nature},
  year={2022}
}
"""


class FakeZoteroAPI:
    """Stands in for pyzotero's `zotero.Zotero`."""

    def __init__(self, library=LIBRARY, children=None):
        self.library = library
        self._children = children or {}
        self.queries: list[str] = []

    def items(self, q="", qmode="", itemType="", limit=10):
        self.queries.append(q)
        tokens = [t for t in q.lower().split() if t]
        out = []
        for data in self.library:
            blob = " ".join([
                data["title"],
                " ".join(c.get("lastName", "") for c in data["creators"]),
                data["date"],
                data.get("DOI", ""),
            ]).lower()
            if tokens and all(t in blob for t in tokens):
                out.append({"key": data["key"], "data": data})
        return out[:limit]

    def item(self, key):
        for data in self.library:
            if data["key"] == key:
                return {"key": key, "data": data}
        raise KeyError(key)

    def children(self, key):
        return self._children.get(key, [])


class FakeClient:
    """Wraps the fake API in the real client, so the real matching code runs."""

    def __new__(cls, api=None, available=True):
        from manuscriptor.evidence.zotero import ZoteroClient

        obj = ZoteroClient.__new__(ZoteroClient)
        obj.base_url = "http://localhost:23119/api/users/0"
        obj.zot = api if api is not None else FakeZoteroAPI()
        obj._title_cache = {}
        obj.client_error = None
        obj.is_available = lambda: available
        # Stubbed by default, and that default matters: without it these tests
        # reach the REAL Better BibTeX on this machine and start returning item
        # keys out of the live library. Tests that want the rung set their own.
        obj._bbt_rpc = lambda method, params: None
        return obj


def write_claims(out: Path, keys):
    out.mkdir(parents=True, exist_ok=True)
    (out / "claims.json").write_text(
        json.dumps([{"claim_id": "cl-1", "sentence": "x", "cite_keys": list(keys)}]),
        encoding="utf-8",
    )


# --------------------------------------------------------------------------
# Fix 1 — the reproduction. A real brace-carrying .bib title, a library that
# holds the paper, and zero matches.
# --------------------------------------------------------------------------

def test_a_braced_bib_title_still_finds_its_zotero_item(tmp_path, monkeypatch, capsys):
    bib = tmp_path / "sample.bib"
    bib.write_text(BIB, encoding="utf-8")
    out = tmp_path / "build"
    write_claims(out, ["arentz2022impact", "aris2022world"])

    api = FakeZoteroAPI()
    monkeypatch.setattr(resolve, "ZoteroClient", lambda *a, **k: FakeClient(api))
    resolve.run(bib_file=bib, output_dir=out)

    cits = {c["cite_key"]: c for c in json.loads((out / "citations.json").read_text())}
    assert all("{" not in q and "}" not in q for q in api.queries), \
        f"a BibTeX brace reached the Zotero query: {api.queries}"
    assert cits["arentz2022impact"]["zotero_key"] == "ARENTZKEY"
    assert cits["aris2022world"]["zotero_key"] == "ARISKEY"


def test_every_bib_field_is_de_braced_not_only_the_title(tmp_path, monkeypatch):
    """strip("{}") was written four times. `{van der Berg}` and a journal
    named `{BMC} Infectious Diseases` are the same bug in the other three."""
    bib = tmp_path / "sample.bib"
    bib.write_text(BIB, encoding="utf-8")
    out = tmp_path / "build"
    write_claims(out, ["arentz2022impact", "aris2022world"])
    monkeypatch.setattr(resolve, "ZoteroClient",
                        lambda *a, **k: FakeClient(FakeZoteroAPI(library=[])))
    resolve.run(bib_file=bib, output_dir=out)

    cits = {c["cite_key"]: c for c in json.loads((out / "citations.json").read_text())}
    assert cits["arentz2022impact"]["journal"] == "BMC Infectious Diseases"
    assert cits["aris2022world"]["authors"] == ["Aris", "van der Berg"]
    assert cits["arentz2022impact"]["title"].endswith("tuberculosis in India")


def test_a_catastrophic_match_rate_is_not_reported_as_a_normal_run(tmp_path, monkeypatch, capsys):
    """19% scrolled past and the run finished. A reachable library that matches
    NOTHING is a code fault, not a thin library — the .bib came out of Zotero."""
    bib = tmp_path / "sample.bib"
    entries = "\n".join(
        f"@article{{k{i}, title={{Paper number {i} about nothing at all}}, "
        f"author={{Smith, A}}, year={{2020}}}}" for i in range(12)
    )
    bib.write_text(entries, encoding="utf-8")
    out = tmp_path / "build"
    write_claims(out, [f"k{i}" for i in range(12)])
    monkeypatch.setattr(resolve, "ZoteroClient",
                        lambda *a, **k: FakeClient(FakeZoteroAPI(library=[])))

    with pytest.raises(resolve.ZoteroMatchFailure) as exc:
        resolve.run(bib_file=bib, output_dir=out)
    assert "0 of 12" in str(exc.value)
    # The partial result is still on disk: the alarm reports, it does not erase.
    assert (out / "citations.json").exists()


def test_a_genuinely_thin_library_is_a_warning_and_not_a_failure(tmp_path, monkeypatch, capsys):
    """The alarm must not be so trigger-happy that a .bib assembled elsewhere
    becomes unusable. One match in twelve is thin, and the run completes."""
    bib = tmp_path / "sample.bib"
    entries = [
        "@article{arentz2022impact, title={The impact of the {COVID-19} pandemic and "
        "associated suppression measures on the burden of tuberculosis in {India}}, "
        "author={Arentz, M}, year={2022}}"
    ]
    entries += [
        f"@article{{k{i}, title={{Paper number {i} about nothing at all}}, "
        f"author={{Smith, A}}, year={{2020}}}}" for i in range(11)
    ]
    bib.write_text("\n".join(entries), encoding="utf-8")
    out = tmp_path / "build"
    write_claims(out, ["arentz2022impact"] + [f"k{i}" for i in range(11)])
    monkeypatch.setattr(resolve, "ZoteroClient", lambda *a, **k: FakeClient(FakeZoteroAPI()))

    resolve.run(bib_file=bib, output_dir=out)
    printed = capsys.readouterr().out
    assert "LOW ZOTERO MATCH RATE" in printed


def test_no_alarm_when_zotero_is_closed(tmp_path, monkeypatch, capsys):
    """Zero matches with the library shut is the expected outcome, not a fault."""
    bib = tmp_path / "sample.bib"
    bib.write_text("\n".join(
        f"@article{{k{i}, title={{Paper {i}}}, author={{Smith, A}}, year={{2020}}}}"
        for i in range(12)), encoding="utf-8")
    out = tmp_path / "build"
    write_claims(out, [f"k{i}" for i in range(12)])
    monkeypatch.setattr(resolve, "ZoteroClient",
                        lambda *a, **k: FakeClient(FakeZoteroAPI(), available=False))
    resolve.run(bib_file=bib, output_dir=out)  # must not raise


# --------------------------------------------------------------------------
# Rung 1 — the cite key itself, when Better BibTeX can vouch for it.
# --------------------------------------------------------------------------

def bbt(items):
    """A stand-in for BBT's item.search, which is a TEXT search: it returns
    items whose citation-key merely resembles the query, so the caller has to
    check the returned key rather than trust the result set."""
    def rpc(method, params):
        assert method == "item.search"
        q = params[0].lower()
        return [i for i in items if q in (i.get("citation-key") or "").lower()]
    return rpc


def bbt_item(citekey, item_key, title="A paper"):
    return {"id": f"http://zotero.org/users/local/afdY9zfT/items/{item_key}",
            "citation-key": citekey, "title": title}


def resolve_with(tmp_path, monkeypatch, bib_text, keys, *, rpc=None, library=None):
    bib = tmp_path / "sample.bib"
    bib.write_text(bib_text, encoding="utf-8")
    out = tmp_path / "build"
    write_claims(out, keys)

    def make(*a, **k):
        c = FakeClient(FakeZoteroAPI(library=library if library is not None else []))
        c._bbt_rpc = rpc if rpc is not None else (lambda m, p: None)
        return c

    monkeypatch.setattr(resolve, "ZoteroClient", make)
    resolve.run(bib_file=bib, output_dir=out)
    return {c["cite_key"]: c for c in json.loads((out / "citations.json").read_text())}


BIB_KWAN = ("@article{kwan2018variations, title={Variations in the quality of "
            "tuberculosis care in urban {India}}, author={Kwan, A}, year={2018}}")


def test_an_exact_cite_key_resolves_with_no_fuzzy_matching_at_all(tmp_path, monkeypatch):
    """The cheapest rung and the only exact one. It is free when it works, and
    it works whenever the .bib was exported from this library's Better BibTeX."""
    cits = resolve_with(tmp_path, monkeypatch, BIB_KWAN, ["kwan2018variations"],
                        rpc=bbt([bbt_item("kwan2018variations", "TQE457YC")]))
    assert cits["kwan2018variations"]["zotero_key"] == "TQE457YC"
    assert cits["kwan2018variations"]["match_rung"] == "citekey"


def test_a_text_hit_whose_key_is_not_the_key_is_not_a_match(tmp_path, monkeypatch):
    """`item.search` is a text search. Trusting its result set rather than the
    returned `citation-key` would bind a citation to the wrong paper."""
    cits = resolve_with(tmp_path, monkeypatch, BIB_KWAN, ["kwan2018variations"],
                        rpc=bbt([bbt_item("kwan2018variationsExtended", "WRONG1")]))
    assert cits["kwan2018variations"]["zotero_key"] is None


def test_a_drifted_cite_key_is_reported_as_a_stale_export_not_a_missing_paper(
        tmp_path, monkeypatch):
    """A BBT citekey is DERIVED from author/year/title, so editing an item's
    metadata silently rewrites it and every exported .bib goes stale. The
    library really does hold `das2022two` — under `das2022twoindias`. "Re-export
    your .bib" and "add this paper to Zotero" are different jobs, and the old
    code could say only the second one."""
    bib = ("@article{das2022two, title={Two {Indias}: The structure of primary "
           "health care markets}, author={Das, J}, year={2022}}")
    cits = resolve_with(tmp_path, monkeypatch, bib, ["das2022two"],
                        rpc=bbt([bbt_item("das2022twoindias", "MGP6UIXN")]))
    rec = cits["das2022two"]
    assert rec["citekey_status"] == "stale"
    assert rec["citekey_in_library"] == "das2022twoindias"
    # Reported, NEVER auto-accepted: a silent fuzzy citekey match could bind a
    # citation to the wrong paper, which is worse than leaving it unresolved.
    assert rec["zotero_key"] is None


def test_no_better_bibtex_falls_through_instead_of_failing_the_citation(
        tmp_path, monkeypatch):
    """Plenty of users have no BBT, and plenty of .bib files were never
    exported from Zotero at all. A missing rung must be invisible to them, and
    it must be distinguishable from "BBT is here and does not know this key"."""
    library = [{"key": "ARENTZKEY",
                "title": "Variations in the quality of tuberculosis care in urban India",
                "creators": [{"creatorType": "author", "lastName": "Kwan"}],
                "date": "2018", "DOI": "", "publicationTitle": "PLOS Medicine",
                "itemType": "journalArticle"}]
    cits = resolve_with(tmp_path, monkeypatch, BIB_KWAN, ["kwan2018variations"],
                        rpc=lambda m, p: None, library=library)
    assert cits["kwan2018variations"]["citekey_status"] == "bbt_unavailable"
    assert cits["kwan2018variations"]["zotero_key"] == "ARENTZKEY", "title rung must still run"
    assert cits["kwan2018variations"]["match_rung"] == "title"


def test_bbt_present_and_the_key_is_simply_not_in_it(tmp_path, monkeypatch):
    cits = resolve_with(tmp_path, monkeypatch, BIB_KWAN, ["kwan2018variations"],
                        rpc=bbt([]))
    assert cits["kwan2018variations"]["citekey_status"] == "absent"


def test_duplicates_under_one_cite_key_do_not_refuse_a_paper_that_is_present(
        tmp_path, monkeypatch):
    """`das2022twoindias` and `daniels2019gender` each return two items. A
    "unique answer required" rule would reject both, so the rule is instead:
    a citekey is BBT's own handle, duplicates under it are one paper, and the
    copy carrying a PDF is the useful one."""
    dupes = [bbt_item("kwan2018variations", "EMPTYONE"),
             bbt_item("kwan2018variations", "HASPDF")]
    api = FakeZoteroAPI(children={
        "EMPTYONE": [],
        "HASPDF": [{"key": "AT1", "data": {"itemType": "attachment",
                                           "contentType": "application/pdf"}}]})
    bib = tmp_path / "sample.bib"
    bib.write_text(BIB_KWAN, encoding="utf-8")
    out = tmp_path / "build"
    write_claims(out, ["kwan2018variations"])

    def make(*a, **k):
        c = FakeClient(api)
        c._bbt_rpc = bbt(dupes)
        return c

    monkeypatch.setattr(resolve, "ZoteroClient", make)
    resolve.run(bib_file=bib, output_dir=out)
    cits = {c["cite_key"]: c for c in json.loads((out / "citations.json").read_text())}
    assert cits["kwan2018variations"]["zotero_key"] == "HASPDF"


def test_a_stale_cite_key_is_its_own_reason_in_the_miss_record(tmp_path, monkeypatch):
    """The most actionable reason there is, so it must not be swallowed into
    "no Zotero item matched"."""
    miss = run_fetch(tmp_path, monkeypatch, [
        dict(CIT, zotero_key=None, citekey_status="stale",
             citekey_in_library="das2022twoindias")])["arentz2022impact"]
    assert miss["reason"] == "citekey_stale_export"
    assert "das2022twoindias" in miss["detail"]


def test_zotero_gives_the_braces_back_as_html_and_that_must_not_defeat_the_match(
        tmp_path, monkeypatch):
    """The brace bug's mirror image, found by querying the live library.

    Zotero imports BibTeX brace protection as a `nocase` span and hands the
    title back with the markup in it:

        TB control in <span class="nocase">India in the COVID</span> era

    So the .bib side was de-braced and the Zotero side grew tags instead, and
    the two still could not be compared. Six of the ten citations that were
    still missing after the brace fix were this and nothing else. Both sides
    go through the SAME function, which is the only reason one change fixes
    both directions.
    """
    library = [{
        "key": "BEHERAKEY",
        "title": 'TB control in <span class="nocase">India in the COVID</span> era',
        "creators": [{"creatorType": "author", "lastName": "Behera"}],
        "date": "2021", "DOI": "", "publicationTitle": "IJTLD",
        "itemType": "journalArticle",
    }]
    bib = tmp_path / "sample.bib"
    bib.write_text(
        "@article{behera2021tb, title={{TB} control in {India in the COVID} era}, "
        "author={Behera, D}, year={2021}}", encoding="utf-8")
    out = tmp_path / "build"
    write_claims(out, ["behera2021tb"])
    monkeypatch.setattr(resolve, "ZoteroClient",
                        lambda *a, **k: FakeClient(FakeZoteroAPI(library=library)))
    resolve.run(bib_file=bib, output_dir=out)
    cits = {c["cite_key"]: c for c in json.loads((out / "citations.json").read_text())}
    assert cits["behera2021tb"]["zotero_key"] == "BEHERAKEY"
    assert "<span" not in cits["behera2021tb"]["title"], "markup reached the stored record"


def test_a_hyphen_in_a_different_place_does_not_hide_the_paper(tmp_path, monkeypatch):
    """Zotero quicksearch is an AND over literal substrings, so ONE token
    spelled differently returns nothing at all and the paper is invisible.

    The .bib says "low-and middle-income", the library says "low- and
    middle-income". Retrying with fewer, longer words widens the CANDIDATE
    POOL only. The acceptance test below is untouched, which is the point:
    loosening what counts as a match would buy false positives, and the next
    guard proves it did not happen.
    """
    library = [{
        "key": "KWANKEY",
        "title": "Use of standardised patients for healthcare quality research "
                 "in low- and middle-income countries",
        "creators": [{"creatorType": "author", "lastName": "Kwan"}],
        "date": "2019", "DOI": "", "publicationTitle": "BMJ Global Health",
        "itemType": "journalArticle",
    }]
    bib = tmp_path / "sample.bib"
    bib.write_text(
        "@article{kwan2019use, title={Use of standardised patients for healthcare "
        "quality research in low-and middle-income countries}, "
        "author={Kwan, A}, year={2019}}", encoding="utf-8")
    out = tmp_path / "build"
    write_claims(out, ["kwan2019use"])
    monkeypatch.setattr(resolve, "ZoteroClient",
                        lambda *a, **k: FakeClient(FakeZoteroAPI(library=library)))
    resolve.run(bib_file=bib, output_dir=out)
    cits = {c["cite_key"]: c for c in json.loads((out / "citations.json").read_text())}
    assert cits["kwan2019use"]["zotero_key"] == "KWANKEY"


def test_narrowing_the_query_does_not_start_matching_the_wrong_paper(tmp_path, monkeypatch):
    """The library holds a WHO report whose opening words overlap heavily with
    a different WHO report in the .bib. A three-word query surfaces it; the
    acceptance test must still throw it out. (Observed live: `world2021engaging`
    against "Engaging with the private healthcare sector for the control of
    tuberculosis in India".)"""
    library = [{
        "key": "WRONGKEY",
        "title": "Engaging with the private healthcare sector for the control of "
                 "tuberculosis in India: a landscape review of policy",
        "creators": [{"creatorType": "author", "lastName": "WHO"}],
        "date": "2021", "DOI": "", "publicationTitle": "",
        "itemType": "report",
    }]
    bib = tmp_path / "sample.bib"
    bib.write_text(
        "@article{world2021engaging, title={Engaging private health care providers "
        "in {TB} care and prevention: A landscape analysis}, "
        "author={{World Health Organization}}, year={2021}}", encoding="utf-8")
    out = tmp_path / "build"
    write_claims(out, ["world2021engaging"])
    monkeypatch.setattr(resolve, "ZoteroClient",
                        lambda *a, **k: FakeClient(FakeZoteroAPI(library=library)))
    resolve.run(bib_file=bib, output_dir=out)
    cits = {c["cite_key"]: c for c in json.loads((out / "citations.json").read_text())}
    assert cits["world2021engaging"]["zotero_key"] is None, "a different report was accepted"


# --------------------------------------------------------------------------
# Fix 2 — the reason has to say which of the five things happened.
# --------------------------------------------------------------------------

def run_fetch(tmp_path, monkeypatch, citations, *, api=None, available=True):
    from manuscriptor.evidence import cache

    monkeypatch.setattr(cache, "CACHE_ROOT", tmp_path / "cache")
    monkeypatch.setattr(cache, "FULLTEXT_DIR", tmp_path / "cache" / "fulltext")
    monkeypatch.setattr(cache, "EXTRACT_DIR", tmp_path / "cache" / "extract")
    monkeypatch.setattr(fetch, "CC_PDF_CACHE", tmp_path / "nope")
    monkeypatch.setattr(fetch, "ZOTERO_STORAGE", tmp_path / "nostorage")
    out = tmp_path / "build"
    out.mkdir(parents=True, exist_ok=True)
    (out / "citations.json").write_text(json.dumps(citations), encoding="utf-8")
    monkeypatch.setattr(fetch, "ZoteroClient",
                        lambda *a, **k: FakeClient(api, available=available))
    fetch.run(output_dir=out)
    return {m["cite_key"]: m for m in json.loads((out / "missing.json").read_text())}


CIT = {"cite_key": "arentz2022impact", "doi": None, "zotero_key": None,
       "title": "The impact of the COVID-19 pandemic", "authors": [], "year": 2022}


def test_a_step_that_never_ran_does_not_report_as_a_search_that_came_back_empty(
        tmp_path, monkeypatch):
    """The whole defect in one assertion. With `zotero_key` None both Zotero
    branches are gated off and nothing is asked; the old code still said
    "no indexed fulltext available in Zotero"."""
    miss = run_fetch(tmp_path, monkeypatch, [dict(CIT)])["arentz2022impact"]
    assert miss["reason"] == "no_zotero_match"
    assert "zotero-fulltext" not in miss["searched"], \
        "no Zotero lookup happened, so none may be reported"
    assert "local-pdf" not in miss["searched"]
    # and the steps that DID run are named, so the record stays honest both ways
    assert "fulltext-cache" in miss["searched"]


def test_a_closed_library_says_so(tmp_path, monkeypatch):
    miss = run_fetch(tmp_path, monkeypatch, [dict(CIT)], available=False)["arentz2022impact"]
    assert miss["reason"] == "zotero_unreachable"


def test_nothing_to_look_up_with(tmp_path, monkeypatch):
    cit = dict(CIT, title="", doi=None)
    miss = run_fetch(tmp_path, monkeypatch, [cit])["arentz2022impact"]
    assert miss["reason"] == "no_doi_and_no_title"


def test_matched_but_the_item_carries_no_pdf(tmp_path, monkeypatch):
    """`pai2022covid` / WR4NSS5X in the real library: present, no usable file.
    That is a library gap the repair button exists for, and it is NOT the same
    fact as "Zotero has no such paper"."""
    api = FakeZoteroAPI(children={"ARENTZKEY": []})
    miss = run_fetch(tmp_path, monkeypatch,
                     [dict(CIT, zotero_key="ARENTZKEY")], api=api)["arentz2022impact"]
    assert miss["reason"] == "matched_but_no_attachment"
    assert "zotero-fulltext" in miss["searched"]


def test_the_pdf_is_there_but_zotero_never_indexed_it(tmp_path, monkeypatch):
    """A different fix entirely — reindex, not re-download."""
    api = FakeZoteroAPI(children={"ARENTZKEY": [
        {"key": "ATTACH1", "data": {"itemType": "attachment",
                                    "contentType": "application/pdf",
                                    "path": "storage:paper.pdf"}}]})
    miss = run_fetch(tmp_path, monkeypatch,
                     [dict(CIT, zotero_key="ARENTZKEY")], api=api)["arentz2022impact"]
    assert miss["reason"] == "attachment_not_indexed"
    assert {"zotero-fulltext", "local-pdf"} <= set(miss["searched"])


def test_the_reason_is_never_the_same_string_for_every_failure(tmp_path, monkeypatch):
    """The regression that made the original diagnosis take a live re-query by
    hand: one sentence covering five different situations."""
    api = FakeZoteroAPI(children={"ARENTZKEY": []})
    misses = run_fetch(tmp_path, monkeypatch, [
        dict(CIT, cite_key="a"),
        dict(CIT, cite_key="b", title=""),
        dict(CIT, cite_key="c", zotero_key="ARENTZKEY"),
    ], api=api)
    reasons = {m["reason"] for m in misses.values()}
    assert len(reasons) == 3, f"failures collapsed into {reasons}"


def test_a_zotero_error_is_not_silently_a_library_gap(tmp_path, monkeypatch):
    """Three of four `except Exception` clauses swallowed the error with no
    log, so a broken client read as an empty shelf."""
    class Broken(FakeZoteroAPI):
        def children(self, key):
            raise RuntimeError("connection reset")

    api = Broken()
    miss = run_fetch(tmp_path, monkeypatch,
                     [dict(CIT, zotero_key="ARENTZKEY")], api=api)["arentz2022impact"]
    assert miss["reason"] == "zotero_error"
    assert "connection reset" in miss.get("detail", "")


def test_no_bare_except_exception_swallows_a_zotero_call():
    src = Path(__file__).resolve().parents[1] / "manuscriptor" / "evidence"
    offenders = []
    for path in sorted(src.glob("*.py")):
        lines = path.read_text(encoding="utf-8").splitlines()
        for i, line in enumerate(lines):
            if re.match(r"\s*except\s+Exception(\s+as\s+\w+)?\s*:", line):
                body = "\n".join(lines[i + 1:i + 5])
                if "log" not in body:
                    offenders.append(f"{path.name}:{i+1}")
    assert not offenders, "unlogged blanket excepts: " + ", ".join(offenders)
