"""Preflight findings reaching the page, which is the half that was missing.

`preflight` computed `Finding(check, doc, file, line, quote, body)` with an
`as_comment()` shaped for `drain.comment` from the day it was written, and
nothing ever called it. A check whose findings are printed to a terminal nobody
runs is the same silence the module exists to prevent, one level up.

The hazard the wiring has to survive is dedupe. `drain.comment` deduped on the
quote, which fails in both directions at once for a real run: a `bib-fields`
finding carries no quotable site, so a second run filed it again into an
append-only log, and two findings about the SAME bibliography carry the same
quote, so filing both swallowed one. The quote is the anchor and it is not the
identity; the identity is check, document, file, line and the site inside that
line, and it is computed in one place -- `Finding.key` -- rather than per check.

The other rule here is the one `match_by_quote` already states: a finding whose
quote matches nothing waits at the document, exactly as an unplaceable reviewer
note waits in the tray. It is never dropped and never guessed onto a paragraph.
"""
from __future__ import annotations

import asyncio
import inspect
import subprocess
from pathlib import Path

import pytest

from tests import pagedriver
from manuscriptor.server import app, chat, drain, paths, preflight
from manuscriptor.server import build as build_mod

# A style that declares neither locator the bibliography actually carries, so
# `bib-fields` reports TWO findings about one file at one line -- the case a
# quote cannot tell apart, because both anchor on the same `\bibliography`.
BST = r"""
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
READ
"""

BIB = """
@article{one,
  author = {A. Author},
  title = {A Title},
  journal = {J},
  year = {2020},
  doi = {10.1/xyz},
  issn = {1234-5678},
}

@book{two,
  author = {B. Author},
  title = {Another},
  publisher = {P},
  year = {2021},
  isbn = {978-0-00-000000-0},
  issn = {8765-4321},
}
"""

MAIN = r"""\documentclass{article}
\begin{document}
\section{Results}
The screening rate rose in every cohort we followed, and Table~1 of the
appendix reports those counts by round and by city for the whole panel.

A second paragraph, here only so that a finding which guessed at a paragraph
would have somewhere wrong to land and could be caught doing it.

We measured \input{frag_n} conversations in all, which is the number this
sentence exists in order to print for the reader of the results section.

\bibliographystyle{style}
\bibliography{refs}
\end{document}
"""

# An analysis script emitting a hand-typed number: a real finding that belongs
# to no paragraph of any document, which is what makes it the unplaceable case.
SCRIPT = 'label = "Table 4"\nprint(label)\n'


CLEAN_BIB = """
@article{one,
  author = {A. Author},
  title = {A Title},
  journal = {J},
  year = {2020},
  doi = {10.1/xyz},
}
"""


def manuscript(tmp_path: Path, *, main: str = MAIN, bst: str | None = BST,
               frag: str = "", bib: str = BIB) -> Path:
    """A manuscript carrying one finding of each shape, in its own repository.

    Its own repository because the scripts sweep is bounded by the enclosing
    git repo, and pytest's temp directory is not one.
    """
    d = tmp_path / "paper"
    d.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=d, capture_output=True)
    (d / "main.tex").write_text(main, encoding="utf-8")
    (d / "frag_n.tex").write_text(frag, encoding="utf-8")
    if bst is not None:
        (d / "style.bst").write_text(bst, encoding="utf-8")
    (d / "refs.bib").write_text(bib, encoding="utf-8")
    (d / "make_table.py").write_text(SCRIPT, encoding="utf-8")
    return d


def filed(d: Path) -> list[chat.Chat]:
    return sorted(chat.read_chats(paths.comments(d)), key=lambda c: c.id)


def bodies(d: Path) -> list[str]:
    return [c.body for c in filed(d)]


def anchored(d: Path) -> dict[str, str | None]:
    """Where each filed comment sits NOW, through the page's own placement."""
    b = build_mod.build(d)
    return {e["id"]: e["block"] for e in
            build_mod.queue_view(paths.comments(d), b.blocks, doc="main.tex")}


def para(d: Path, text: str) -> str:
    b = build_mod.build(d)
    hits = [x.id for x in b.blocks if text in x.source_text]
    assert len(hits) == 1, f"{text!r} names {len(hits)} blocks"
    return hits[0]


# ------------------------------------------------------------ what gets filed


def test_every_finding_a_run_reports_is_filed_once(tmp_path):
    """The delivery itself: what the terminal prints is what the page holds."""
    d = manuscript(tmp_path)
    results = preflight.run(d)
    want = [f for r in results for f in r.findings]
    assert len(want) >= 4, "the fixture must carry findings of several shapes"

    preflight.deliver(d)

    got = bodies(d)
    for f in want:
        assert f.body in got, f"never filed: {f.body}"


def test_a_second_run_files_nothing_new(tmp_path):
    """The log is append-only, so a check run twice must not say everything
    twice. `bib-fields` carries no quote and was the case that did."""
    d = manuscript(tmp_path)
    first = preflight.deliver(d)
    again = preflight.deliver(d)

    assert first and not again, f"a second run filed {len(again)} comments again"
    assert len(bodies(d)) == len(set(bodies(d))) == len(first)


def test_two_findings_about_one_bibliography_are_both_filed(tmp_path):
    """They share a document, a file, a line AND an anchor, because both are
    about the same `\\bibliography`. Deduping on the quote drops the second."""
    d = manuscript(tmp_path)
    preflight.deliver(d)

    said = " | ".join(bodies(d))
    assert "isbn" in said and "issn" in said, said


def test_the_dedupe_key_is_the_identity_and_the_quote_is_only_the_anchor(tmp_path):
    """Stated on `drain.comment` directly, because that is where it has to hold
    for every check and not only for this one."""
    d = manuscript(tmp_path)
    log = paths.comments(d)

    a = drain.comment(d, body="one", quote=r"\bibliography{refs}",
                      doc="main.tex", key="bib-fields|main.tex|refs.bib|0|isbn",
                      author="preflight", review=True)
    b = drain.comment(d, body="two", quote=r"\bibliography{refs}",
                      doc="main.tex", key="bib-fields|main.tex|refs.bib|0|issn",
                      author="preflight", review=True)
    assert a and b, "one anchor, two findings: both belong on the page"

    same = drain.comment(d, body="one, said differently", quote="somewhere else",
                         doc="main.tex", key="bib-fields|main.tex|refs.bib|0|isbn",
                         author="preflight", review=True)
    assert same is None, "the same finding re-filed under a different quote"
    assert len(chat.read_chats(log)) == 2


def test_a_finding_keeps_its_key_where_the_next_run_can_read_it(tmp_path):
    """Dedupe survives a restart because the key is in the log, not in memory."""
    d = manuscript(tmp_path)
    preflight.deliver(d)
    keys = [c.key for c in filed(d)]
    assert all(keys), f"a filed finding carries no key: {keys}"
    assert len(set(keys)) == len(keys), "two findings filed under one key"


def test_two_hand_typed_numbers_on_one_line_are_two_findings(tmp_path):
    """covet-india's `supplement.tex:192` carries two. `check|doc|file|line` is
    the same for both, so the key needs the site inside the line as well."""
    d = manuscript(tmp_path, main=MAIN.replace(
        "Table~1 of the", "Table~1 and Fig.~2 of the"))
    preflight.deliver(d)

    said = " | ".join(bodies(d))
    assert "Table~1" in said and "Fig.~2" in said, said


# -------------------------------------------------------------- where it lands


def test_a_finding_lands_on_the_paragraph_its_quote_names(tmp_path):
    """Anchored by quote through `match_by_quote`, like every other comment."""
    d = manuscript(tmp_path)
    preflight.deliver(d)
    where = anchored(d)
    by_body = {c.body: c.id for c in filed(d)}

    hand_typed = [b for b in by_body if "Table~1" in b]
    assert hand_typed, by_body
    assert where[by_body[hand_typed[0]]] == para(d, "screening rate rose")


def test_a_bibliography_finding_lands_on_the_bibliography(tmp_path):
    """The bytes to fix are in the `.bst`, which is not addressable on the page,
    so the comment goes where the reader SEES it."""
    d = manuscript(tmp_path)
    preflight.deliver(d)
    where = anchored(d)
    by_body = {c.body: c.id for c in filed(d)}

    bib = [b for b in by_body if "issn" in b]
    assert bib, by_body
    assert where[by_body[bib[0]]] == para(d, r"\bibliography{refs}")


def test_a_finding_with_nowhere_to_land_waits_at_the_document(tmp_path):
    """A number hand-typed in an analysis script belongs to no paragraph of any
    document. It must be visible and it must not be guessed onto one: the
    document chat is where a comment with no block is read, which is the same
    answer the tray gives an unplaceable reviewer note."""
    d = manuscript(tmp_path)
    preflight.deliver(d)
    by_body = {c.body: c.id for c in filed(d)}

    script = [b for b in by_body if "analysis script" in b]
    assert script, by_body
    cid = by_body[script[0]]
    assert anchored(d)[cid] is None, "a script's finding was placed on a paragraph"

    b = build_mod.build(d)
    at_document = build_mod.reanchor_chats(
        chat.by_block(paths.comments(d), doc="main.tex"), b.blocks,
        chat.read_chats(paths.comments(d), doc="main.tex"))
    assert cid in [m["id"] for m in at_document.get("", [])], \
        "the finding is nowhere the author reads"


# --------------------------------------------------- the state, and the drain


def test_findings_arrive_in_review_and_the_drain_never_works_them(tmp_path):
    """A `--with-agent` session must not start working its own review."""
    d = manuscript(tmp_path)
    recs = preflight.deliver(d)
    states = {c.id: c.state for c in filed(d)}

    assert recs and all(states[r["id"]] == "review" for r in recs), states
    assert not [i for i in drain.collect(d) if i.chat_id in states]


def test_a_dismissed_finding_is_raised_again_by_a_later_run(tmp_path):
    """The check is telling the author it still thinks so. Deliberate, and it
    is the reason dedupe looks only at comments that are still open."""
    d = manuscript(tmp_path)
    first = preflight.deliver(d)
    for rec in first:
        drain.mark(d, rec["id"], "done")

    again = preflight.deliver(d)
    assert len(again) == len(first)


# ---------------------------------------------------- a check that did not run


def test_a_check_that_could_not_run_is_filed_too(tmp_path):
    """A skipped check is not a pass, and delivering only findings renders it as
    one: the author reads an empty margin and calls the bibliography clean."""
    d = manuscript(tmp_path, bst=None)          # the style is nowhere to be found
    results = preflight.run(d)
    assert [r for r in results if r.status == "skipped"], "the fixture must skip"

    preflight.deliver(d)
    said = " | ".join(bodies(d))
    assert "bib-fields" in said and "not a pass" in said, said


def test_a_check_that_did_not_run_is_not_filed_twice_either(tmp_path):
    d = manuscript(tmp_path, bst=None)
    first = preflight.deliver(d)
    assert not preflight.deliver(d), "the second run said it all again"
    assert first


# --------------------------------------------------------------- the toolbar


def post(session, path: str, *, record=False):
    """Ask the real route over real HTTP. Returns (json, frames)."""
    from aiohttp.test_utils import TestClient, TestServer

    frames: list[dict] = []

    async def go():
        async with TestClient(TestServer(app.make_app(session))) as client:
            if record:
                with pagedriver.record(session) as sent:
                    r = await client.post(path)
                    body = await r.json()
                frames.extend(sent)
            else:
                r = await client.post(path)
                body = await r.json()
            return {"status": r.status, "body": body}

    return asyncio.run(go()), frames


def test_the_toolbar_files_the_findings_and_tells_the_page(tmp_path):
    """The Checks menu is the whole point: the findings are in the log and on
    the open page before the click finishes, with no agent involved."""
    d = manuscript(tmp_path)
    session = app.Session(d)
    res, frames = post(session, "/preflight", record=True)

    assert res["status"] == 200, res
    assert res["body"]["filed"] == len(filed(d)) > 0, res["body"]
    kinds = [f["type"] for f in frames]
    assert "chat" in kinds and "queue" in kinds, kinds
    states = {e["id"]: e["state"] for f in frames if f["type"] == "queue"
              for e in f["queue"]}
    assert set(states.values()) == {"review"}, states


def test_the_toolbar_says_so_when_a_run_finds_nothing(tmp_path):
    """Silence is never the report. A clean run answers with what it ran."""
    d = manuscript(tmp_path, main=MAIN.replace("Table~1 of the", "the"),
                   frag="216", bib=CLEAN_BIB)
    (d / "make_table.py").unlink()
    session = app.Session(d)
    res, _ = post(session, "/preflight")

    assert res["status"] == 200, res
    assert res["body"]["checks"] >= 4, res["body"]
    assert res["body"]["filed"] == 0 and res["body"]["not_run"] == 0, res["body"]


def test_two_clicks_at_once_still_file_each_finding_once(tmp_path):
    """Dedupe is decided by reading the log, so two runs that overlap both read
    it before either has written and file everything twice -- into a file that
    cannot be rewritten. A double click on a menu is not exotic."""
    from aiohttp.test_utils import TestClient, TestServer

    d = manuscript(tmp_path)
    session = app.Session(d)

    async def go():
        async with TestClient(TestServer(app.make_app(session))) as client:
            a, b = await asyncio.gather(client.post("/preflight"),
                                        client.post("/preflight"))
            return [await a.json(), await b.json()]

    out = asyncio.run(go())
    keys = [c.key for c in filed(d)]
    assert len(set(keys)) == len(keys), f"filed twice: {keys}"
    assert sum(o["filed"] for o in out) == len(keys), out


def test_a_read_only_serve_files_nothing(tmp_path):
    """The log is a write, and a read-only serve makes none."""
    d = manuscript(tmp_path)
    session = app.Session(d, read_only=True)
    res, _ = post(session, "/preflight")

    assert res["status"] == 403, res
    assert not paths.comments(d).exists()
    # And it says what did not happen. "Findings are written to the comment
    # log" is a description of the refused operation, and the page prints the
    # refusal verbatim, so the author read a refusal as a report of success.
    said = res["body"]["error"]
    assert said.startswith("not run"), said
    assert "read-only" in said, said


# ------------------------------------------------------------------- the CLI


def test_the_command_still_modifies_nothing_unless_asked(tmp_path):
    from manuscriptor import cli

    d = manuscript(tmp_path)
    assert cli.main(["preflight", str(d)]) == 1
    assert not paths.comments(d).exists()


def test_the_command_can_file_what_it_found(tmp_path, capsys):
    from manuscriptor import cli

    d = manuscript(tmp_path)
    assert cli.main(["preflight", str(d), "--review"]) == 1
    assert len(filed(d)) > 0
    assert "review comment" in capsys.readouterr().out


def test_the_shape_a_comment_takes_still_matches_the_signature(tmp_path):
    """`as_comment` grew a key, so the guard that it is spendable as kwargs has
    to be re-asked of the real signature."""
    d = manuscript(tmp_path)
    f = [x for r in preflight.run(d) for x in r.findings][0]
    accepted = inspect.signature(drain.comment).parameters

    assert set(f.as_comment()) <= set(accepted)
    assert f.as_comment()["key"] == f.key


# --------------------------------------------------------------- on the page


WHY = pagedriver.missing()


@pytest.mark.skipif(bool(WHY), reason=str(WHY))
def test_the_finding_reaches_the_open_page_with_its_triage(tmp_path):
    """The live path, through the real `viewer.js`: a finding filed by the
    toolbar route reaches a page that is already open, in the `review` state,
    carrying the two buttons that triage it."""
    d = manuscript(tmp_path)
    session = app.Session(d)
    page = pagedriver.page(session)
    _, frames = post(session, "/preflight", record=True)

    out = pagedriver.drive(page, frames, tmp_path=tmp_path,
                           steps=["select:" + para(d, "screening rate rose"),
                                  "frames", "tab:1"])
    panel = out["panel"] or ""
    assert "review" in panel, panel[:800]
    assert "finding:fix:" in panel and "finding:dismiss:" in panel, panel[:800]
    assert [e["state"] for e in out["queue"]] and \
        set(e["state"] for e in out["queue"]) == {"review"}, out["queue"]


@pytest.mark.skipif(bool(WHY), reason=str(WHY))
def test_an_unplaceable_finding_is_readable_at_the_document(tmp_path):
    """It has no paragraph, so it has to be legible somewhere: the panel the
    author reads when nothing is selected, with the same two buttons. A finding
    that reached the log and nowhere on the page is a finding nobody has."""
    d = manuscript(tmp_path)
    session = app.Session(d)
    page = pagedriver.page(session)
    _, frames = post(session, "/preflight", record=True)

    out = pagedriver.drive(page, frames, tmp_path=tmp_path)
    panel = out["panel"] or ""
    assert "analysis script" in panel, panel[-1200:]
    assert "finding:dismiss:" in panel, panel[-1200:]


@pytest.mark.skipif(bool(WHY), reason=str(WHY))
def test_the_menu_offers_it_and_the_page_knows_where_to_send_it(tmp_path):
    """The wiring the author actually touches, driven rather than read.

    This asserted `"'preflight': '/preflight'" in viewer` and nothing else,
    which a COMMENT satisfies: commenting the mapping out left the whole suite
    green while the menu entry did nothing at all. What the entry is for is a
    POST, so the guard picks the entry and looks at what the page sent.
    """
    d = manuscript(tmp_path)
    session = app.Session(d)
    page = pagedriver.page(session)

    out = pagedriver.drive(page, [], tmp_path=tmp_path,
                           steps=["pick:checks-menu:preflight"])
    posts = [f for f in out["fetched"]
             if f["method"] == "POST" and "/preflight" in f["url"]]
    assert posts, (
        "the Checks menu's preflight entry sent nothing to the server: "
        f"{out['fetched']}")
    # And it is the SERVER's own check, not a comment asking an agent to run one.
    assert not [m for m in out["sent"] if (m or {}).get("type") == "chat"], out["sent"]


@pytest.mark.skipif(bool(WHY), reason=str(WHY))
def test_a_run_the_server_refused_is_never_reported_as_a_clean_bill(tmp_path):
    """`fetch` does not reject on an HTTP error, and `.json()` on an error body
    throws, so `catch(() => ({}))` turned a 500 into `{}` and `{}` renders as
    "0 checks ran, nothing found" -- a clean bill for a check that never ran.
    Live-reachable: an old server process answers the route with 405, the page
    reloads against it, and the author is told his manuscript is clean."""
    d = manuscript(tmp_path)
    session = app.Session(d)
    page = pagedriver.page(session)

    out = pagedriver.drive(page, [], tmp_path=tmp_path,
                           steps=["pick:checks-menu:preflight"],
                           replies=[{"url": "/preflight", "status": 500,
                                     "body": {}}])
    said = " ".join(line["text"] or "" for line in out["tickerLines"])
    assert "nothing found" not in said, (
        f"a refused run was reported as a clean bill: {said}")
    assert "500" in said, f"the failure has to name what came back: {said}"


@pytest.mark.skipif(bool(WHY), reason=str(WHY))
def test_a_read_only_refusal_says_what_it_is(tmp_path):
    """403 is the read-only serve refusing to write the log. It is not a finding
    count, and it is not a clean bill either."""
    d = manuscript(tmp_path)
    session = app.Session(d)
    page = pagedriver.page(session)

    out = pagedriver.drive(page, [], tmp_path=tmp_path,
                           steps=["pick:checks-menu:preflight"],
                           replies=[{"url": "/preflight", "status": 403,
                                     "body": {"error": "read-only"}}])
    said = " ".join(line["text"] or "" for line in out["tickerLines"])
    assert "nothing found" not in said, said
    assert "read-only" in said, said
