"""Resolving an identifier through Zotero's OWN translators, saving nothing.

A book the author does not already hold cannot be identified by DOI, and the
DOI-shaped catalogues are no help: three tries at Rogers' *Diffusion of
Innovations* through Crossref returned three different wrong works. Zotero
already ships the translators that "Add Item by Identifier" uses, and the
cli-bridge add-on can run one with `libraryID: false` -- the same translation,
returning the item INSTEAD of saving it.

What is under test here is mostly the three ways the answer can arrive, because
collapsing them is the failure that matters: "the bridge is not installed" must
never render as "there is no such book". Same discipline as `_bbt_rpc`, which
already separates "no such rung on this machine" from "the rung ran and found
nothing".

Nothing here touches the network or a running Zotero.
"""
from __future__ import annotations

import json

import pytest
import requests

from manuscriptor.evidence import zotero as zt


# The live bridge's answer for 978-0-7432-5823-4, recorded verbatim on
# 2026-08-03 and trimmed only of the 2KB abstract, which this module must not
# carry into a bib entry and which is asserted against below.
ROGERS_RAW = {
    "itemType": "book",
    "creators": [{"firstName": "Everett M.", "lastName": "Rogers", "creatorType": "author"}],
    "notes": [{"note": "Description based on publisher supplied metadata"}],
    "tags": [],
    "seeAlso": [],
    "attachments": [],
    "ISBN": "9780743258234",
    "language": "eng",
    "abstractNote": "Intro -- Dedication -- Preface -- Chapter 1: Elements of Diffusion",
    "title": "Diffusion of innovations",
    "edition": "Fifth edition",
    "numPages": "1",
    "place": "New York London Toronto Sydney",
    "publisher": "Free Press",
    "date": "2003",
    "libraryCatalog": "K10plus ISBN",
}

# Sen's *Poverty and Famines* is a 1981 book. This is what the catalogue holds.
SEN_RAW = {
    "itemType": "book",
    "creators": [{"firstName": "Amartya", "lastName": "Sen", "creatorType": "author"}],
    "ISBN": "9780198284635",
    "title": "Poverty and famines: an essay on entitlement and deprivation",
    "edition": "Reprinted",
    "publisher": "Oxford Univ. Press",
    "date": "2010",
}


class FakePost:
    """`requests.post`, with the bridge's own envelopes and no socket.

    `reply` is either an exception to raise, or a `(status, body)` pair.
    """

    def __init__(self, reply):
        self.reply = reply
        self.calls: list[str] = []

    def __call__(self, url, **kw):
        body = kw.get("data") or kw.get("json") or ""
        self.calls.append(body.decode("utf-8") if isinstance(body, bytes) else body)
        if isinstance(self.reply, Exception):
            raise self.reply
        status, body = self.reply
        return _Resp(status, body)


class _Resp:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body

    def json(self):
        if isinstance(self._body, str):
            return json.loads(self._body)   # raises ValueError on non-JSON
        return self._body


def a_client(monkeypatch, reply):
    post = FakePost(reply)
    monkeypatch.setattr(zt.requests, "post", post)
    # The bridge path must not need pyzotero: the identifier lookup speaks HTTP
    # to Zotero directly, and a client that could not import the compiled
    # extension is still able to ask.
    c = zt.ZoteroClient.__new__(zt.ZoteroClient)
    c.base_url = zt.ZOTERO_LOCAL_BASE
    c.zot = None
    c.client_error = None
    c._title_cache = {}
    return c, post


# ------------------------------------------------------- the three outcomes


def test_a_record_comes_back_in_the_vocabulary_the_insert_path_speaks(monkeypatch):
    c, _ = a_client(monkeypatch, (200, [ROGERS_RAW]))
    look = c.lookup_identifier("978-0-7432-5823-4")
    assert look.status == "found", look
    r = look.record
    assert r["title"] == "Diffusion of innovations"
    assert r["authors"] == ["Rogers, Everett M."]
    assert r["year"] == 2003
    assert r["publisher"] == "Free Press"
    assert r["edition"] == "Fifth edition"
    assert r["isbn"] == "9780743258234"
    assert r["type"] == "book"
    assert r["address"] == "New York London Toronto Sydney"
    assert not r.get("doi")


def test_the_abstract_and_the_notes_are_left_behind(monkeypatch):
    """A 2KB abstract in a `.bib` field is a corrupt entry, not a rich one."""
    c, _ = a_client(monkeypatch, (200, [ROGERS_RAW]))
    r = c.lookup_identifier("9780743258234").record
    assert "abstractNote" not in r
    assert "Dedication" not in json.dumps(r)


def test_a_missing_bridge_is_not_a_missing_book(monkeypatch):
    c, _ = a_client(monkeypatch, requests.ConnectionError("refused"))
    look = c.lookup_identifier("9780743258234")
    assert look.status == "bridge_unavailable", look
    assert look.record is None


def test_an_uninstalled_add_on_answers_404_and_is_still_not_a_missing_book(monkeypatch):
    c, _ = a_client(monkeypatch, (404, {}))
    assert c.lookup_identifier("9780743258234").status == "bridge_unavailable"


def test_the_bridge_ran_and_no_catalogue_held_it(monkeypatch):
    c, _ = a_client(monkeypatch, (200, []))
    look = c.lookup_identifier("9780000000002")
    assert look.status == "absent", look


def test_a_string_that_is_not_an_identifier_says_so(monkeypatch):
    """Distinct from `absent`: nothing was looked up, so nothing was not found."""
    c, _ = a_client(monkeypatch, (200, {"not_an_identifier": True}))
    look = c.lookup_identifier("diffusion of innovations rogers")
    assert look.status == "not_an_identifier", look


def test_a_bridge_that_threw_is_an_error_and_never_an_empty_shelf(monkeypatch):
    c, _ = a_client(monkeypatch, (500, {"error": "boom"}))
    with pytest.raises(zt.ZoteroError) as e:
        c.lookup_identifier("9780743258234")
    assert "boom" in str(e.value)


def test_non_json_from_the_bridge_is_an_error(monkeypatch):
    c, _ = a_client(monkeypatch, (200, "<html>not json</html>"))
    with pytest.raises(zt.ZoteroError):
        c.lookup_identifier("9780743258234")


# ------------------------------------------------------------ nothing is saved


def test_the_script_translates_into_no_library_at_all(monkeypatch):
    """`libraryID: false` is the whole of the read-only guarantee.

    The author's `zotero-write-guard.py` blocks `zotero-cli import` and connector
    saves by design; a save routed through the bridge would be exactly the
    workaround that guard exists to prevent.
    """
    c, post = a_client(monkeypatch, (200, [ROGERS_RAW]))
    c.lookup_identifier("9780743258234")
    script = post.calls[0]
    assert "libraryID: false" in script
    for banned in ("saveItems", "Zotero.Items.", "libraryID: 1", "itemSaver"):
        assert banned not in script, script


def test_the_identifier_is_encoded_and_cannot_close_the_string(monkeypatch):
    c, post = a_client(monkeypatch, (200, []))
    c.lookup_identifier('9780743258234"); Zotero.Items.erase(1); //')
    script = post.calls[0]
    # The injected quote never closes the literal: it survives only escaped, so
    # `erase(1)` is string data inside the argument and not a statement.
    assert '\\"); Zotero' in script, script
    import re as _re
    assert not _re.search(r'(?<!\\)"\); Zotero', script), script


# ------------------------------------------------- printing years, the one trap


@pytest.mark.parametrize("edition", [
    "Reprinted", "reprinted", "Repr.", "1. ed., 6th print", "6th printing",
    "Nachdruck", "2. Nachdr.", "Ristampa", "Reimpresión",
])
def test_an_edition_string_that_marks_a_printing_is_caught(edition):
    assert zt.printing_marker(edition), edition


@pytest.mark.parametrize("edition", [
    "Fifth edition", "4th ed", "2. ed", "1. Aufl", "Revised edition",
    "Second edition, reprint of the 1962 edition",   # see below
    "", None,
])
def test_a_real_edition_is_not_mistaken_for_a_printing(edition):
    if edition == "Second edition, reprint of the 1962 edition":
        # This one DOES carry the word, and catching it is correct: the year the
        # catalogue holds is still the printing's. Kept in this list only to be
        # explicit that the exception is deliberate.
        assert zt.printing_marker(edition)
        return
    assert zt.printing_marker(edition) is None, edition


def test_the_reprint_record_arrives_intact_and_carries_its_tell(monkeypatch):
    """Sen 1981, held by the catalogue as 2010. The guard lives one layer up;
    what the lookup owes is the edition string that gives it away."""
    c, _ = a_client(monkeypatch, (200, [SEN_RAW]))
    r = c.lookup_identifier("9780198284635").record
    assert r["year"] == 2010
    assert zt.printing_marker(r["edition"])
