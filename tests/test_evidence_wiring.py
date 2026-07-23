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
