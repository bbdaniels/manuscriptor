"""The two ways out of the evidence panel: the PDF, and the Zotero item.

The panel could say a quote was found and could not say what it was found IN,
because `evidence_cites` dropped the authors, year, journal, DOI and Zotero key
that `citations.json` had held all along. These tests are the payload carrying
them, the panel rendering them, and the two endpoints that open the source.

The `open` invocation is asserted as a COMMAND here and never executed. A test
that actually opened a PDF would open sixty of them across a suite run, and the
question under test is which command was built, not whether macOS can run it.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from manuscriptor.evidence import zotero as zotero_mod
from manuscriptor.server import build as build_mod
from manuscriptor.server import links as links_mod
from manuscriptor.server import paths
from tests import pagedriver

DOC = r"""\documentclass{article}
\begin{document}
\section{Results}
Effects were large \citep{croke2026sickness} and persistent \citep{andrabi2023human}.
\end{document}
"""

FULL = {
    "cite_key": "croke2026sickness",
    "title": "In Sickness and In Health",
    "authors": ["Croke", "Mwangi"],
    "year": 2026,
    "journal": "Journal of Development Economics",
    "doi": "10.1016/j.jdeveco.2026.01.001",
    "zotero_key": "ABCD1234",
    "has_fulltext": True,
    "fulltext_source": "zotero-indexed",
    "fulltext_chars": 48000,
}


def build_with(tmp_path: Path, citations, evidence=None):
    (tmp_path / "main.tex").write_text(DOC, encoding="utf-8")
    out = paths.cache(tmp_path)
    out.mkdir(parents=True, exist_ok=True)
    (out / "citations.json").write_text(json.dumps(citations), encoding="utf-8")
    if evidence is not None:
        (out / "evidence.json").write_text(json.dumps(evidence), encoding="utf-8")
    return build_mod.build(tmp_path)


# ------------------------------------------------------------------ payload


def test_the_payload_carries_who_wrote_it_and_where_it_lives(tmp_path):
    """All five identity fields survive `evidence_cites` into `window.MS`.

    Watched failing against the state before this change, where the entry held
    a title and a status and nothing else: `KeyError: 'authors'`.
    """
    b = build_with(tmp_path, [FULL])
    rec = b.blob["cites"]["croke2026sickness"]
    assert rec["authors"] == ["Croke", "Mwangi"]
    assert rec["year"] == 2026
    assert rec["journal"] == "Journal of Development Economics"
    assert rec["doi"] == "10.1016/j.jdeveco.2026.01.001"
    assert rec["zotero_key"] == "ABCD1234"


def test_a_citation_with_no_identity_carries_empty_fields_not_missing_ones(tmp_path):
    # The panel omits what is absent; it can only do that if the key is there
    # to be found empty, rather than absent and indistinguishable from a payload
    # built before this feature.
    b = build_with(tmp_path, [{"cite_key": "andrabi2023human", "title": "U"}])
    rec = b.blob["cites"]["andrabi2023human"]
    assert rec["authors"] == [] and rec["journal"] == ""
    assert rec["doi"] == "" and rec["zotero_key"] == "" and rec["year"] is None


def test_the_reason_a_source_has_no_pdf_reaches_the_page(tmp_path):
    """`matched_but_no_attachment` is the one build-time fact that says an
    Open PDF button would do nothing, and the panel suppresses the button on
    it. Resolution still happens on click; this decides only whether to offer
    the click at all."""
    b = build_with(tmp_path, [dict(FULL, has_fulltext=False, fulltext_source="missing",
                                   fulltext_reason="matched_but_no_attachment")])
    assert b.blob["cites"]["croke2026sickness"]["fulltext_reason"] == "matched_but_no_attachment"


# ---------------------------------------------------------------- the panel

WHY = pagedriver.missing()
needs_page = pytest.mark.skipif(bool(WHY), reason=str(WHY))

CITED = r"""\documentclass{article}
\begin{document}
First paragraph, entirely unremarkable, citing \citep{croke2026sickness} and long enough to be a block.

Second paragraph, which exists so the first is not the only block on the page here.
\end{document}
"""


def served(tmp_path: Path, citations):
    from manuscriptor.server import app as app_mod

    (tmp_path / "main.tex").write_text(CITED, encoding="utf-8")
    out = paths.cache(tmp_path)
    out.mkdir(parents=True, exist_ok=True)
    (out / "citations.json").write_text(json.dumps(citations), encoding="utf-8")
    session = app_mod.Session(tmp_path)
    return session, pagedriver.page(session)


def open_the_cite_panel(page, tmp_path, *, key="croke2026sickness", steps=(), replies=None):
    """Click the paragraph, its References tab, and then the citation on it.

    The route the author takes. There is no "show me this panel" in the page's
    contract, so a test that reached past these clicks would be asserting on a
    panel nobody opened.
    """
    import re

    ids = re.findall(r'data-mx="([^"]+)"', page)
    assert ids, "no blocks on the page"
    plan = ["select:" + ids[0], "tab:2", "open:cite:" + key] + list(steps)
    return pagedriver.drive(page, [], steps=plan, replies=replies, tmp_path=tmp_path)


@needs_page
def test_the_panel_names_the_paper_and_shows_its_doi_as_a_url(tmp_path):
    """Authors, journal, year and the DOI, with the DOI's href EQUAL to what it
    displays. The bibliography two inches to the left renders every DOI that
    way; a panel hiding them behind friendly text would be visibly inconsistent
    with the page it annotates."""
    session, page = served(tmp_path, [FULL])
    out = open_the_cite_panel(page, tmp_path)
    panel = out["panel"]
    assert "Croke" in panel and "Mwangi" in panel
    assert "Journal of Development Economics" in panel
    assert "2026" in panel
    url = "https://doi.org/10.1016/j.jdeveco.2026.01.001"
    assert 'href="' + url + '"' in panel, "the DOI is not a link"
    assert ">" + url + "<" in panel, "the DOI link hides its URL behind other text"


@needs_page
def test_an_absent_field_is_omitted_rather_than_rendered_blank(tmp_path):
    session, page = served(tmp_path, [{"cite_key": "croke2026sickness", "title": "T",
                                       "zotero_key": "ABCD1234"}])
    panel = open_the_cite_panel(page, tmp_path)["panel"]
    assert "Journal</dt>" not in panel and "Authors</dt>" not in panel
    assert "DOI</dt>" not in panel


@needs_page
def test_an_entry_with_no_attachment_offers_zotero_and_no_dead_pdf_button(tmp_path):
    session, page = served(tmp_path, [dict(FULL, has_fulltext=False,
                                           fulltext_source="missing",
                                           fulltext_reason="matched_but_no_attachment")])
    panel = open_the_cite_panel(page, tmp_path)["panel"]
    assert 'data-act="cite:zotero:croke2026sickness"' in panel
    assert 'data-act="cite:pdf:croke2026sickness"' not in panel, \
        "a button that cannot work is worse than no button"


@needs_page
def test_an_entry_with_no_zotero_item_offers_neither_button(tmp_path):
    session, page = served(tmp_path, [dict(FULL, zotero_key=None)])
    panel = open_the_cite_panel(page, tmp_path)["panel"]
    assert "cite:zotero:" not in panel and "cite:pdf:" not in panel
    assert "10.1016/j.jdeveco.2026.01.001" in panel, "the DOI is all that is left"


@needs_page
def test_clicking_open_pdf_posts_the_cite_key_to_the_server(tmp_path):
    session, page = served(tmp_path, [FULL])
    out = open_the_cite_panel(page, tmp_path,
                              steps=["act:cite:pdf:croke2026sickness"])
    posts = [f for f in out["fetched"] if "/evidence/open-pdf" in f["url"]]
    assert len(posts) == 1, f"the button posted {len(posts)} times"
    assert posts[0]["method"] == "POST"
    assert json.loads(posts[0]["body"]) == {"cite_key": "croke2026sickness"}


@needs_page
def test_clicking_open_in_zotero_posts_the_cite_key(tmp_path):
    session, page = served(tmp_path, [FULL])
    out = open_the_cite_panel(page, tmp_path,
                              steps=["act:cite:zotero:croke2026sickness"])
    posts = [f for f in out["fetched"] if "/evidence/open-zotero" in f["url"]]
    assert len(posts) == 1
    assert json.loads(posts[0]["body"]) == {"cite_key": "croke2026sickness"}


@needs_page
def test_a_refusal_is_shown_in_the_panel_rather_than_swallowed(tmp_path):
    """The server's reason, on the panel, beside the button that failed.

    A control that says nothing when the server refuses is indistinguishable
    from one that worked, which is the whole reason `replies` exists in the
    harness.
    """
    session, page = served(tmp_path, [FULL])
    out = open_the_cite_panel(
        page, tmp_path,
        steps=["act:cite:pdf:croke2026sickness"],
        replies=[{"url": "/evidence/open-pdf", "status": 409,
                  "body": {"error": "Zotero holds this item and no PDF is attached to it"}}])
    assert "no PDF is attached to it" in out["panel"]


# ------------------------------------------------------------- the endpoints


class FakeZotero:
    """Enough of `ZoteroClient` for the resolver, and nothing that talks."""

    def __init__(self, children=None, *, available=True, error=None):
        self.children = children if children is not None else []
        self.available = available
        self.error = error
        self.asked = []

    def is_available(self):
        return self.available

    def pdf_attachments(self, parent_key):
        self.asked.append(parent_key)
        if self.error:
            raise zotero_mod.ZoteroError(self.error)
        return list(self.children)


def cited(tmp_path, records):
    out = paths.cache(tmp_path)
    out.mkdir(parents=True, exist_ok=True)
    (out / "citations.json").write_text(json.dumps(records), encoding="utf-8")
    return tmp_path


def storage(tmp_path, monkeypatch):
    store = tmp_path / "zstorage"
    store.mkdir()
    monkeypatch.setattr(zotero_mod, "ZOTERO_STORAGE", store)
    return store


def test_the_happy_path_resolves_a_stored_pdf_and_issues_open(tmp_path, monkeypatch):
    store = storage(tmp_path, monkeypatch)
    (store / "ATT9").mkdir()
    pdf = store / "ATT9" / "croke-2026.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    root = cited(tmp_path, [FULL])
    ran = []
    got = links_mod.open_pdf("croke2026sickness", root=root,
                             client=FakeZotero([{"key": "ATT9"}]),
                             runner=ran.append)
    assert got == pdf.resolve()
    assert ran == [["open", str(pdf.resolve())]]


def test_an_unknown_cite_key_is_a_404_and_says_so(tmp_path, monkeypatch):
    storage(tmp_path, monkeypatch)
    root = cited(tmp_path, [FULL])
    with pytest.raises(links_mod.LinkRefused) as exc:
        links_mod.open_pdf("nosuchkey", root=root, client=FakeZotero(), runner=lambda c: None)
    assert exc.value.status == 404
    assert "nosuchkey" in exc.value.reason


def test_a_known_key_with_no_attachment_refuses_cleanly(tmp_path, monkeypatch):
    storage(tmp_path, monkeypatch)
    root = cited(tmp_path, [FULL])
    with pytest.raises(links_mod.LinkRefused) as exc:
        links_mod.open_pdf("croke2026sickness", root=root,
                           client=FakeZotero([]), runner=lambda c: None)
    assert exc.value.status == 409
    assert "no PDF is attached" in exc.value.reason


def test_an_attachment_whose_file_is_not_on_disk_says_that_and_not_no_pdf(tmp_path, monkeypatch):
    # Zotero knows about the attachment; the file was never synced down. That is
    # a different action for the author than "attach a PDF".
    storage(tmp_path, monkeypatch)
    root = cited(tmp_path, [FULL])
    with pytest.raises(links_mod.LinkRefused) as exc:
        links_mod.open_pdf("croke2026sickness", root=root,
                           client=FakeZotero([{"key": "ATT9"}]), runner=lambda c: None)
    assert "is not on disk" in exc.value.reason


def test_zotero_not_running_gets_its_own_message(tmp_path, monkeypatch):
    """The three outcomes the ISBN bridge established: could not look, looked
    and found nothing, found it. "Zotero is not running" must never render as
    "no PDF", because one of them is about the library and the other is about
    the app."""
    storage(tmp_path, monkeypatch)
    root = cited(tmp_path, [FULL])
    with pytest.raises(links_mod.LinkRefused) as exc:
        links_mod.open_pdf("croke2026sickness", root=root,
                           client=FakeZotero(available=False), runner=lambda c: None)
    assert exc.value.status == 503
    assert "Zotero is not running" in exc.value.reason
    assert "no PDF" not in exc.value.reason


def test_a_library_error_is_not_an_absent_pdf_either(tmp_path, monkeypatch):
    storage(tmp_path, monkeypatch)
    root = cited(tmp_path, [FULL])
    with pytest.raises(links_mod.LinkRefused) as exc:
        links_mod.open_pdf("croke2026sickness", root=root,
                           client=FakeZotero(error="connection reset"),
                           runner=lambda c: None)
    assert exc.value.status == 502
    assert "connection reset" in exc.value.reason


def test_a_citation_with_no_zotero_item_cannot_be_opened_anywhere(tmp_path, monkeypatch):
    storage(tmp_path, monkeypatch)
    root = cited(tmp_path, [dict(FULL, zotero_key=None)])
    for fn in (links_mod.open_pdf, links_mod.open_zotero):
        with pytest.raises(links_mod.LinkRefused) as exc:
            fn("croke2026sickness", root=root, runner=lambda c: None)
        assert "no item in your Zotero library" in exc.value.reason


def test_open_in_zotero_builds_the_select_url(tmp_path, monkeypatch):
    storage(tmp_path, monkeypatch)
    root = cited(tmp_path, [FULL])
    ran = []
    url = links_mod.open_zotero("croke2026sickness", root=root, runner=ran.append)
    assert url == "zotero://select/library/items/ABCD1234"
    assert ran == [["open", url]]


# ----------------------------------------------------------------- the gate


def test_a_resolved_path_outside_zotero_storage_is_refused(tmp_path, monkeypatch):
    """THE HOSTILE CASE. A cite key is author-controlled input arriving over
    HTTP, and `open` on an arbitrary path is an arbitrary-file-open. The gate is
    checked AFTER resolution, on the same argument as `compile.py:666`, because
    what is resolved is what would be opened.

    Watched failing with the gate removed: the traversal resolved to the file
    outside storage and `open` was issued on it --
    `Failed: DID NOT RAISE <class 'manuscriptor.server.links.LinkRefused'>`.
    """
    store = storage(tmp_path, monkeypatch)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    secret = elsewhere / "private.pdf"
    secret.write_bytes(b"%PDF-1.4\n")
    root = cited(tmp_path, [FULL])
    ran = []
    # The attachment key is what names the storage directory, and it comes back
    # from the library rather than from the page -- so the traversal is written
    # where it could really arrive.
    hostile = FakeZotero([{"key": "../elsewhere"}])
    with pytest.raises(links_mod.LinkRefused) as exc:
        links_mod.open_pdf("croke2026sickness", root=root, client=hostile,
                           runner=ran.append)
    assert exc.value.status == 403
    assert str(store) in exc.value.reason
    assert ran == [], "the gate refused and something opened the file anyway"


def test_the_gate_is_not_fooled_by_a_symlink_out_of_storage(tmp_path, monkeypatch):
    store = storage(tmp_path, monkeypatch)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "private.pdf").write_bytes(b"%PDF-1.4\n")
    (store / "ATT9").symlink_to(elsewhere, target_is_directory=True)
    root = cited(tmp_path, [FULL])
    ran = []
    with pytest.raises(links_mod.LinkRefused) as exc:
        links_mod.open_pdf("croke2026sickness", root=root,
                           client=FakeZotero([{"key": "ATT9"}]), runner=ran.append)
    assert exc.value.status == 403
    assert ran == []


# ---------------------------------------------------------------- the routes


def test_the_routes_answer_with_the_reason_the_panel_shows(tmp_path, monkeypatch):
    from aiohttp.test_utils import TestClient, TestServer

    from manuscriptor.server.app import Session, make_app

    (tmp_path / "main.tex").write_text(DOC, encoding="utf-8")
    session = Session(tmp_path)
    store = storage(tmp_path, monkeypatch)
    (store / "ATT9").mkdir()
    pdf = store / "ATT9" / "croke.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    cited(tmp_path, [FULL])

    ran = []
    monkeypatch.setattr(links_mod, "_open_runner", ran.append)
    monkeypatch.setattr(links_mod, "ZoteroClient",
                        lambda *a, **k: FakeZotero([{"key": "ATT9"}]))

    async def go():
        client = TestClient(TestServer(make_app(session)))
        await client.start_server()
        ok = await client.post("/evidence/open-pdf", json={"cite_key": "croke2026sickness"})
        assert ok.status == 200, await ok.text()
        assert ran == [["open", str(pdf.resolve())]]

        zot = await client.post("/evidence/open-zotero", json={"cite_key": "croke2026sickness"})
        assert zot.status == 200
        assert ran[-1] == ["open", "zotero://select/library/items/ABCD1234"]

        bad = await client.post("/evidence/open-pdf", json={"cite_key": "nope"})
        assert bad.status == 404
        assert (await bad.json())["error"], "a refusal with no reason is the bug"
        await client.close()

    asyncio.run(go())
