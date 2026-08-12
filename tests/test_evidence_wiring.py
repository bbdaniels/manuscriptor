"""The evidence pass's results, read into the served page.

The pipeline (`manuscriptor evidence`) writes `citations.json` and
`evidence.json` into the build directory; the viewer was always wired to
consume `blob["cites"]` (the underline colours and the Evidence tab) but
nothing ever populated it, so the flagship status-in-text idea was invisible
on every served manuscript. The server READS files the CLI wrote; nothing in
the server calls a model.
"""
from __future__ import annotations

import json
from pathlib import Path

from manuscriptor.server import paths
from manuscriptor.server import build as build_mod

DOC = r"""\documentclass{article}
\begin{document}
\section{Results}
Effects were large \citep{croke2026sickness} and persistent \citep{andrabi2023human}.
\end{document}
"""


def build_with_records(tmp_path: Path, citations, evidence):
    (tmp_path / "main.tex").write_text(DOC, encoding="utf-8")
    out = paths.cache(tmp_path)
    out.mkdir(parents=True)
    if citations is not None:
        (out / "citations.json").write_text(json.dumps(citations), encoding="utf-8")
    if evidence is not None:
        (out / "evidence.json").write_text(json.dumps(evidence), encoding="utf-8")
    return build_mod.build(tmp_path)


def test_no_evidence_run_means_no_records_and_neutral_underlines(tmp_path):
    b = build_with_records(tmp_path, None, None)
    assert b.blob["cites"] == {}


def test_evidence_records_reach_the_blob(tmp_path):
    b = build_with_records(
        tmp_path,
        [{"cite_key": "croke2026sickness", "title": "In Sickness and In Health"}],
        [{"claim_id": "cl-1", "cite_key": "croke2026sickness",
          "quotes": [{"text": "mortality fell by a fifth", "status": "verbatim"}]}],
    )
    rec = b.blob["cites"]["croke2026sickness"]
    assert rec["status"] == "verbatim"
    assert rec["title"] == "In Sickness and In Health"
    assert rec["quotes"][0]["text"] == "mortality fell by a fifth"


def test_the_best_status_across_pairs_wins(tmp_path):
    # A key cited twice: one pair verified verbatim, one only paraphrased.
    # The underline reports the strongest support that exists.
    b = build_with_records(
        tmp_path,
        [{"cite_key": "croke2026sickness", "title": "T"}],
        [{"claim_id": "cl-1", "cite_key": "croke2026sickness",
          "quotes": [{"text": "a", "status": "paraphrase"}]},
         {"claim_id": "cl-2", "cite_key": "croke2026sickness",
          "quotes": [{"text": "b", "status": "verbatim"}]}],
    )
    assert b.blob["cites"]["croke2026sickness"]["status"] == "verbatim"
    assert len(b.blob["cites"]["croke2026sickness"]["quotes"]) == 2


def test_a_key_the_pass_saw_but_could_not_support_is_missing(tmp_path):
    # The pass ran and found nothing: that is a claim about the pair, red,
    # different from a key the pass never examined, which stays neutral.
    b = build_with_records(
        tmp_path,
        [{"cite_key": "croke2026sickness", "title": "T"},
         {"cite_key": "andrabi2023human", "title": "U"}],
        [{"claim_id": "cl-1", "cite_key": "croke2026sickness",
          "quotes": [{"text": "a", "status": "verbatim"}]}],
    )
    assert b.blob["cites"]["andrabi2023human"]["status"] == "missing"


def test_a_red_key_carries_why_it_is_red(tmp_path):
    """Red covers two very different situations and the page could tell neither.

    A source with no fulltext could not be checked either way, which is a library
    gap and what the repair button exists for. A source that WAS read and still
    supported nothing is a claim to revisit. Both reported themselves as "no
    evidence loaded", which reads as "the pass has not run" on a manuscript where
    it had just finished.
    """
    b = build_with_records(
        tmp_path,
        [{"cite_key": "croke2026sickness", "title": "T",
          "has_fulltext": False, "fulltext_source": "missing", "fulltext_chars": 0},
         {"cite_key": "andrabi2023human", "title": "U",
          "has_fulltext": True, "fulltext_source": "zotero", "fulltext_chars": 48000}],
        [],
    )
    unreadable = b.blob["cites"]["croke2026sickness"]
    unsupported = b.blob["cites"]["andrabi2023human"]
    assert unreadable["status"] == unsupported["status"] == "missing", "both are red"
    assert unreadable["fulltext"] is False and unreadable["fulltext_chars"] == 0
    assert unsupported["fulltext"] is True and unsupported["fulltext_chars"] == 48000
    assert unreadable["fulltext_source"] == "missing"


def test_a_corrupt_record_file_does_not_take_down_the_build(tmp_path):
    (tmp_path / "main.tex").write_text(DOC, encoding="utf-8")
    out = paths.cache(tmp_path)
    out.mkdir(parents=True)
    (out / "citations.json").write_text("{not json", encoding="utf-8")
    b = build_mod.build(tmp_path)
    assert b.blob["cites"] == {}


def test_the_blob_counts_missing_fulltexts(tmp_path):
    (tmp_path / "main.tex").write_text(DOC, encoding="utf-8")
    out = paths.cache(tmp_path)
    out.mkdir(parents=True)
    (out / "missing.json").write_text(json.dumps(
        [{"cite_key": "a"}, {"cite_key": "b"}]), encoding="utf-8")
    b = build_mod.build(tmp_path)
    assert b.blob["missing_fulltexts"] == 2


def test_no_misses_and_no_run_both_read_as_zero(tmp_path):
    (tmp_path / "main.tex").write_text(DOC, encoding="utf-8")
    b = build_mod.build(tmp_path)
    assert b.blob["missing_fulltexts"] == 0
    out = paths.cache(tmp_path)
    (out / "missing.json").write_text("{corrupt", encoding="utf-8")
    assert build_mod.build(tmp_path).blob["missing_fulltexts"] == 0


def test_repair_invokes_the_item_subcommand(tmp_path, monkeypatch):
    # `zotero-cli find-pdf` is a usage error; the command is `zotero-cli item
    # find-pdf`. The bare form shipped from cite-evidence and every lookup in
    # the first live repair run failed on it, silently counted as "no PDF".
    from manuscriptor.evidence import repair
    import shutil
    import subprocess as sp
    import time

    (tmp_path / "missing.json").write_text(json.dumps(
        [{"cite_key": "barrows1993", "doi": "10.1/x", "zotero_key": "KEY123"},
         {"cite_key": "king2019", "doi": "10.1/y", "zotero_key": "KEY456"}]),
        encoding="utf-8")
    calls = []

    class Done:
        returncode = 0
        stderr = ""

        def __init__(self, stdout):
            self.stdout = stdout

    answers = ["FOUND: ATT123", "NOT_FOUND: no PDF available"]
    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/zotero-cli")
    monkeypatch.setattr(time, "sleep", lambda _: None)
    monkeypatch.setattr(sp, "run",
                        lambda argv, **kw: (calls.append(argv), Done(answers[len(calls) - 1]))[1])
    rc = repair.run(build_dir=tmp_path)
    assert rc == 0
    assert calls, "no lookup was attempted"
    assert calls[0][:3] == ["zotero-cli", "item", "find-pdf"]
    assert calls[0][3] == "KEY123"


class _FakeRun:
    """One `zotero-cli item find-pdf` invocation's result."""
    stderr = ""

    def __init__(self, stdout, returncode=0):
        self.stdout = stdout
        self.returncode = returncode


def _repair_over(tmp_path, monkeypatch, entries, answers):
    from manuscriptor.evidence import repair
    import shutil
    import subprocess as sp
    import time

    (tmp_path / "missing.json").write_text(json.dumps(entries), encoding="utf-8")
    it = iter(answers)
    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/zotero-cli")
    monkeypatch.setattr(time, "sleep", lambda _: None)
    monkeypatch.setattr(sp, "run", lambda argv, **kw: next(it))
    return repair.run(build_dir=tmp_path)


def test_repair_counts_each_verdict_the_bridge_can_return(tmp_path, monkeypatch, capsys):
    # The success token is FOUND, not OK. `find_pdf` in the zotero-cli bridge
    # (cli_anything/zotero/core/jsbridge.py:300) returns FOUND/NOT_FOUND, and
    # its timeout re-check returns TIMEOUT; only the sibling `attach_pdf`
    # returns 'OK: '. Matching OK meant a successful fetch was printed as
    # FAILED and n_fixed could never leave zero.
    rc = _repair_over(
        tmp_path, monkeypatch,
        [{"cite_key": "a", "zotero_key": "K1"},
         {"cite_key": "b", "zotero_key": "K2"},
         {"cite_key": "c", "zotero_key": "K3"},
         {"cite_key": "d", "zotero_key": "K4"},
         {"cite_key": "e"}],
        [_FakeRun("FOUND: ATT1"),
         _FakeRun("NOT_FOUND: nothing open-access"),
         _FakeRun("ERROR: item K3 not found"),
         _FakeRun("TIMEOUT: PDF lookup timed out after 30s")])
    out = capsys.readouterr().out
    assert "fixed: 1" in out
    assert "no open-access copy: 1" in out
    assert "errors: 2" in out                       # ERROR and TIMEOUT both
    assert "cannot be fetched (no Zotero item): 1" in out
    assert "ERROR (ERROR: item K3 not found)" in out
    assert "TIMEOUT" in out
    assert "FAILED" not in out                      # none of these is unparsed
    assert rc == 0                                  # one fetch succeeded


def test_repair_distinguishes_a_bridge_fault_from_an_absent_copy(tmp_path, monkeypatch, capsys):
    # A run where the bridge itself failed must not read as "no open-access
    # copy" — the author's action differs (retry vs. accept).
    _repair_over(tmp_path, monkeypatch,
                 [{"cite_key": "a", "zotero_key": "K1"}],
                 [_FakeRun("", returncode=1)])
    out = capsys.readouterr().out
    assert "no open-access copy: 0" in out
    assert "errors: 1" in out


def test_repair_that_fetched_nothing_returns_nonzero(tmp_path, monkeypatch, capsys):
    # Zero fetched means nothing downstream can have changed, so the server
    # must not announce a re-run of the evidence pass (app.py then_rerun
    # skips it on a non-zero rc).
    rc = _repair_over(tmp_path, monkeypatch,
                      [{"cite_key": "a", "zotero_key": "K1"}],
                      [_FakeRun("NOT_FOUND: nothing open-access")])
    assert rc == 1
    assert "nothing was fetched" in capsys.readouterr().out


def test_repair_never_attemptable_entries_are_not_failures(tmp_path, monkeypatch, capsys):
    # gelman2014beyond, world2021engaging and world2022tb have neither a DOI
    # nor a Zotero key; no lookup is possible, which is not the same as one
    # that was tried and failed.
    _repair_over(tmp_path, monkeypatch,
                 [{"cite_key": "gelman2014beyond"},
                  {"cite_key": "world2021engaging"},
                  {"cite_key": "world2022tb"}],
                 [])
    out = capsys.readouterr().out
    assert "cannot be fetched (no Zotero item): 3" in out
    assert "errors: 0" in out
    assert "no open-access copy: 0" in out


def test_repair_never_passes_a_doi_as_the_find_pdf_target(tmp_path, monkeypatch, capsys):
    # `item find-pdf` attaches to an item, so it takes a Zotero item key and
    # nothing else; a DOI returns "ERROR: item 10.x/y not found" with exit 0,
    # so the failure is invisible in the exit status. The old target expression
    # was `zot_key or doi`, which sent every keyless entry down that path.
    #
    # Nor does repair re-resolve the DOI: resolve.py already ran that lookup
    # (rung 2, search_by_doi) before writing missing.json, so a DOI arriving
    # here without a key means the search already came back empty.
    # An empty answers list makes any subprocess call raise StopIteration, so
    # a clean run is itself the proof that no lookup was attempted.
    rc = _repair_over(tmp_path, monkeypatch,
                      [{"cite_key": "onlydoi", "doi": "10.1/x"}],
                      [])
    out = capsys.readouterr().out
    assert "cannot be fetched (no Zotero item): 1" in out
    assert "errors: 0" in out
    assert "10.1/x" not in out                       # never used as a target
    assert rc == 1                                   # nothing was fetched
