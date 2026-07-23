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

from manuscriptor.server import build as build_mod

DOC = r"""\documentclass{article}
\begin{document}
\section{Results}
Effects were large \citep{croke2026sickness} and persistent \citep{andrabi2023human}.
\end{document}
"""


def build_with_records(tmp_path: Path, citations, evidence):
    (tmp_path / "main.tex").write_text(DOC, encoding="utf-8")
    out = tmp_path / "build" / "manuscriptor"
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


def test_a_corrupt_record_file_does_not_take_down_the_build(tmp_path):
    (tmp_path / "main.tex").write_text(DOC, encoding="utf-8")
    out = tmp_path / "build" / "manuscriptor"
    out.mkdir(parents=True)
    (out / "citations.json").write_text("{not json", encoding="utf-8")
    b = build_mod.build(tmp_path)
    assert b.blob["cites"] == {}


def test_the_blob_counts_missing_fulltexts(tmp_path):
    (tmp_path / "main.tex").write_text(DOC, encoding="utf-8")
    out = tmp_path / "build" / "manuscriptor"
    out.mkdir(parents=True)
    (out / "missing.json").write_text(json.dumps(
        [{"cite_key": "a"}, {"cite_key": "b"}]), encoding="utf-8")
    b = build_mod.build(tmp_path)
    assert b.blob["missing_fulltexts"] == 2


def test_no_misses_and_no_run_both_read_as_zero(tmp_path):
    (tmp_path / "main.tex").write_text(DOC, encoding="utf-8")
    b = build_mod.build(tmp_path)
    assert b.blob["missing_fulltexts"] == 0
    out = tmp_path / "build" / "manuscriptor"
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

    answers = ["OK: PDF attached", "NOT_FOUND: no PDF available"]
    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/zotero-cli")
    monkeypatch.setattr(time, "sleep", lambda _: None)
    monkeypatch.setattr(sp, "run",
                        lambda argv, **kw: (calls.append(argv), Done(answers[len(calls) - 1]))[1])
    rc = repair.run(build_dir=tmp_path)
    assert rc == 0
    assert calls, "no lookup was attempted"
    assert calls[0][:3] == ["zotero-cli", "item", "find-pdf"]
    assert calls[0][3] == "KEY123"


def test_repair_reports_not_found_as_not_found(tmp_path, monkeypatch, capsys):
    # `zotero-cli item find-pdf` exits 0 for NOT_FOUND too, so exit status
    # alone over-reports: the first live run printed "ok" for lookups that
    # found nothing. The verdict is the first word of stdout.
    from manuscriptor.evidence import repair
    import shutil
    import subprocess as sp
    import time

    (tmp_path / "missing.json").write_text(json.dumps(
        [{"cite_key": "a", "zotero_key": "K1"},
         {"cite_key": "b", "zotero_key": "K2"}]), encoding="utf-8")

    class Done:
        returncode = 0
        stderr = ""

        def __init__(self, stdout):
            self.stdout = stdout

    answers = iter(["OK: attached", "NOT_FOUND: nothing open-access"])
    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/zotero-cli")
    monkeypatch.setattr(time, "sleep", lambda _: None)
    monkeypatch.setattr(sp, "run", lambda argv, **kw: Done(next(answers)))
    repair.run(build_dir=tmp_path)
    out = capsys.readouterr().out
    assert "fixed: 1   failed: 1" in out
    assert "NOT_FOUND" in out
