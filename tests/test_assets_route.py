r"""The one route that serves cached artifacts, and its two staleness gaps.

Both are gaps at SERVE time, which is why neither can be closed at build time.

  * A raster's name is not derived from its content. `fig.pdf.png` is the same
    string before and after the figure is regenerated -- only the `.sha` sidecar
    beside it knows the difference -- so a browser that is allowed to guess how
    long the file stays fresh will guess wrong. `viewer.js` busts the cache with
    a `?v=` on the frame that announces a rebuild, but a later source patch
    re-renders the block from the server's HTML and the `src` comes back plain,
    which drops the bust. With only `Last-Modified` on the response the browser
    then applies heuristic freshness (a fraction of the document's age, no
    revalidation at all) and a stale figure can survive even a reload.
    `no-cache` is the honest policy: keep the copy, always ask. The answer is a
    304 with no body, so it costs a round trip and not a transfer. A long
    `max-age` would be a lie about a filename that carries no version.

    And what the revalidation COMPARES has to be the content. A staged image
    carries its source's mtime, deliberately, so new bytes under an older
    timestamp -- a restore from backup, a `cp -p`, an `rsync -a` -- refresh the
    cache and then 304 forever to every browser holding the old picture. The
    response carries a strong ETag derived from the bytes instead.

  * A running server whose cache no longer holds what its page names has no way
    to say so. On 2026-08-04 a later build renamed the rasters away underneath a
    server that was still serving the old page; every `img` 404'd, the browser
    reported it to nobody, and the author read it as his figures being gone. The
    build that produced the page was self-consistent when it ran, so no
    build-time check could have caught it -- the file vanished afterwards. The
    miss is only observable at the moment the browser asks.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from pathlib import Path
from unittest import mock

import pytest

from tests import pagedriver
from manuscriptor.server import app, paths

PNG = (b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)

BODY = r"""\documentclass{article}
\begin{document}
First paragraph, entirely unremarkable, and long enough to be a block of its own.

Second paragraph, which is here so the page has something to render below the first.
\end{document}
"""


def served(tmp_path: Path):
    """A session on a real manuscript, with one artifact in its cache."""
    (tmp_path / "main.tex").write_text(BODY, encoding="utf-8")
    session = app.Session(tmp_path)
    fig = paths.cache(session.root) / "figures" / "fig.pdf.png"
    fig.parent.mkdir(parents=True, exist_ok=True)
    fig.write_bytes(PNG)
    return session


def get(session, path: str, *, record=False):
    """Ask the real route over real HTTP. Returns (response fields, frames)."""
    from aiohttp.test_utils import TestClient, TestServer

    frames: list[dict] = []

    async def go():
        async with TestClient(TestServer(app.make_app(session))) as client:
            if record:
                with pagedriver.record(session) as sent:
                    r = await client.get(path)
                    body = await r.read()
                frames.extend(sent)
            else:
                r = await client.get(path)
                body = await r.read()
            # A copy of the real multidict, not `dict(...)`: header names are
            # case-insensitive on the wire and aiohttp answers `Etag`.
            return {"status": r.status, "headers": r.headers.copy(), "body": body}

    return asyncio.run(go()), frames


# --------------------------------------------------------------- (a) revalidation


def test_a_served_asset_tells_the_browser_to_revalidate(tmp_path):
    """The response carries `no-cache`, so the copy is kept and never trusted blind."""
    session = served(tmp_path)
    res, _ = get(session, "/figures/fig.pdf.png")
    assert res["status"] == 200
    assert res["body"] == PNG
    cc = res["headers"].get("Cache-Control", "")
    assert "no-cache" in cc, (
        "a raster's filename is stable across content changes, so a response "
        f"that does not force revalidation lets a stale figure survive a reload; got {cc!r}")
    assert "max-age" not in cc, (
        f"a max-age on a name that carries no version is a promise nothing keeps; got {cc!r}")


def revalidate(session, path: str, headers: dict):
    from aiohttp.test_utils import TestClient, TestServer

    async def go():
        async with TestClient(TestServer(app.make_app(session))) as client:
            r = await client.get(path, headers=headers)
            return {"status": r.status, "body": await r.read(),
                    "headers": r.headers.copy()}

    return asyncio.run(go())


def test_revalidating_still_costs_nothing_when_the_asset_has_not_moved(tmp_path):
    """`no-cache` is cheap because the conditional request answers 304, not 200.

    This is the argument for choosing it over a long `max-age`, so it is asserted
    rather than claimed in a comment. The conditional is on the ETAG, which is
    what the response carries; see the test below for why it cannot be the clock.
    """
    session = served(tmp_path)
    first, _ = get(session, "/figures/fig.pdf.png")
    tag = first["headers"]["ETag"]
    assert tag and not tag.startswith("W/"), f"a weak validator settles nothing: {tag!r}"

    again = revalidate(session, "/figures/fig.pdf.png", {"If-None-Match": tag})
    assert again["status"] == 304, "the revalidation has to be answerable without a transfer"
    assert again["body"] == b""


def test_new_bytes_under_an_older_timestamp_are_served_and_not_revalidated_away(tmp_path):
    """The freshness key on the wire has to be the CONTENT, never the clock.

    `mirror` copies with `copy2`, so a staged image carries its SOURCE's mtime --
    which is right, because that is the staleness key the copier establishes. A
    response conditioned on that same mtime then makes new content with an OLDER
    timestamp permanently invisible: restore a figure from a backup, `cp -p` it
    in, `rsync -a` a directory of exhibits, and the cache refreshes correctly
    while every browser holding the old picture 304s forever.
    """
    session = served(tmp_path)
    src = tmp_path / "figures" / "pic.png"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(PNG)
    os.utime(src, (2_000_000_000, 2_000_000_000))

    first, _ = get(session, "/figures/pic.png")
    assert first["status"] == 200 and first["body"] == PNG
    tag = first["headers"]["ETag"]

    # Different bytes, older timestamp: a restore from backup.
    newer = b"\x89PNG\r\n\x1a\n" + b"\x11" * 64
    src.write_bytes(newer)
    os.utime(src, (1_000_000_000, 1_000_000_000))

    again = revalidate(session, "/figures/pic.png",
                       {"If-None-Match": tag,
                        "If-Modified-Since": first["headers"].get(
                            "Last-Modified", "Thu, 01 Jan 2037 00:00:00 GMT")})
    assert again["status"] == 200, (
        "a figure restored from a backup revalidated as unchanged, and the "
        "browser will hold the old picture until its timestamp happens to move")
    assert again["body"] == newer
    assert again["headers"]["ETag"] != tag, "the validator did not follow the content"


def test_a_raster_that_did_not_change_keeps_its_validator(tmp_path):
    """The other half: the ETag is stable while the bytes are, or `no-cache`
    turns every revalidation into a transfer."""
    session = served(tmp_path)
    src = tmp_path / "figures" / "steady.png"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(PNG)
    one, _ = get(session, "/figures/steady.png")
    two, _ = get(session, "/figures/steady.png")
    assert one["headers"]["ETag"] == two["headers"]["ETag"]


# ------------------------------------------------------------------- (b) the misses


def test_an_asset_the_cache_no_longer_holds_is_recorded(tmp_path):
    """A 404 to the browser is silent; the session records what was asked for."""
    session = served(tmp_path)
    assert session.blob["diagnostics"].get("missing_assets") in (None, [])
    res, _ = get(session, "/figures/renamed-away.png")
    assert res["status"] == 404
    assert session.blob["diagnostics"]["missing_assets"] == ["figures/renamed-away.png"]


def test_the_miss_is_pushed_to_the_open_page_and_only_once_each(tmp_path):
    """The page is already open, so a boot-time render cannot carry this.

    Once per distinct asset: a page holding twenty broken images asks twenty
    times on every reload and must not produce twenty frames for one fact.
    """
    session = served(tmp_path)
    _, first = get(session, "/figures/renamed-away.png", record=True)
    _, again = get(session, "/figures/renamed-away.png", record=True)
    _, other = get(session, "/figures/also-gone.png", record=True)

    assert [f["type"] for f in first] == ["diagnostics"]
    assert first[0]["diagnostics"]["missing_assets"] == ["figures/renamed-away.png"]
    assert [f["type"] for f in again] == [], "the same miss said twice is one fact"
    assert [f["type"] for f in other] == ["diagnostics"]
    assert other[0]["diagnostics"]["missing_assets"] == [
        "figures/renamed-away.png", "figures/also-gone.png"]


def test_an_escape_from_the_cache_is_refused_without_being_recorded(tmp_path):
    """A path climbing out of the build directory is a different failure.

    Recording it would let anything that can reach the port write lines into the
    author's diagnostics, and it says nothing about a stale cache.

    Called directly rather than over HTTP, and that is a finding rather than a
    convenience: both the client and the server resolve `..` out of a URL path
    before the handler is reached, encoded or not, so the traversal branch cannot
    be provoked through a real request. Reaching it needs the handler itself.
    """
    from aiohttp import web

    session = served(tmp_path)
    (tmp_path / "escaped.png").write_bytes(PNG)
    handler = list(app.make_app(session).router.routes())[-1].handler

    class Request:
        match_info = {"path": "../escaped.png"}

    with pytest.raises(web.HTTPNotFound):
        asyncio.run(handler(Request()))
    assert session.blob["diagnostics"].get("missing_assets") in (None, [])


def test_a_stray_request_that_is_not_an_asset_is_not_counted(tmp_path):
    """This route is the catch-all, and a browser asks for things nobody wrote.

    `/favicon.ico` arrives unprompted on every page load. Counting it would put
    "1 asset missing" over a manuscript whose figures are all in place.
    """
    session = served(tmp_path)
    for stray in ("/favicon.ico", "/apple-touch-icon.png".replace(".png", ".ico"), "/robots.txt"):
        res, frames = get(session, stray, record=True)
        assert res["status"] == 404
        assert frames == [], f"{stray} produced a frame"
    assert session.blob["diagnostics"].get("missing_assets") in (None, [])


def test_a_rebuild_clears_the_misses_and_says_so(tmp_path):
    """The build that restores the file has to take the warning down with it."""
    session = served(tmp_path)
    get(session, "/figures/renamed-away.png")
    assert session.blob["diagnostics"]["missing_assets"]

    async def go():
        with pagedriver.record(session) as sent:
            await session.on_change()
        return sent

    sent = asyncio.run(go())
    assert session.blob["diagnostics"].get("missing_assets") in (None, [])
    frame = pagedriver.one(sent, "diagnostics")
    assert not frame["diagnostics"].get("missing_assets")

    # And the account behind it is cleared too, not just the blob it feeds.
    # Carrying the old paths forward would make the "say it once" rule swallow
    # the SECOND report of the same asset -- the rebuild wiped the warning off
    # the page, and nothing would ever put it back.
    _, again = get(session, "/figures/renamed-away.png", record=True)
    assert [f["type"] for f in again] == ["diagnostics"], (
        "a rebuild that did not restore the file has to warn again")
    assert session.blob["diagnostics"]["missing_assets"] == ["figures/renamed-away.png"]


def test_a_miss_whose_source_is_still_in_the_manuscript_is_a_different_diagnostic(tmp_path):
    r""""Restart the server" is the wrong instruction for a figure on disk.

    `.pdf` is an asset suffix and the route refuses to STAGE a PDF -- a PDF
    figure reaches the page as its raster and never as itself. On a machine with
    no poppler nothing makes that raster: `_pdf_figures_to_png` no-ops, the
    `<embed src="...pdf">` survives into the page, and every exhibit 404s. The
    author was then told nine assets were missing and the server needed a
    restart, about nine files sitting in his own figures directory, when the fix
    was `brew install poppler`.

    Both are still recorded; they are recorded as different things.
    """
    session = served(tmp_path)
    (tmp_path / "figures").mkdir(exist_ok=True)
    (tmp_path / "figures" / "onpage.pdf").write_bytes(b"%PDF-1.4\n%%EOF")

    res, frames = get(session, "/figures/onpage.pdf", record=True)
    assert res["status"] == 404
    diag = session.blob["diagnostics"]
    assert diag.get("missing_assets") in (None, []), (
        "a figure whose source is in the manuscript was reported as one the "
        f"server has lost: {diag}")
    assert diag["unstageable_assets"] == ["figures/onpage.pdf"], diag
    assert [f["type"] for f in frames] == ["diagnostics"]

    # And the vanished one still lands where it always did.
    get(session, "/figures/renamed-away.png")
    assert session.blob["diagnostics"]["missing_assets"] == ["figures/renamed-away.png"]
    assert session.blob["diagnostics"]["unstageable_assets"] == ["figures/onpage.pdf"]


def test_a_raster_whose_pdf_is_still_there_is_unstageable_rather_than_missing(tmp_path):
    """The same fact reached by the name the PAGE uses.

    When the rasterizer ran, the page names `fig.pdf.png`; when it did not, the
    page still names the PDF. Either name has a source in the manuscript, so
    either is the same news: nothing here can build it.
    """
    session = served(tmp_path)
    (tmp_path / "figures").mkdir(exist_ok=True)
    (tmp_path / "figures" / "unbuilt.pdf").write_bytes(b"%PDF-1.4\n%%EOF")
    with mock.patch.object(shutil, "which", lambda name: None):
        res, _ = get(session, "/figures/unbuilt.pdf.png")
    assert res["status"] == 404
    diag = session.blob["diagnostics"]
    assert diag["unstageable_assets"] == ["figures/unbuilt.pdf.png"], diag
    assert diag.get("missing_assets") in (None, [])


def test_a_rebuild_clears_both_accounts(tmp_path):
    session = served(tmp_path)
    (tmp_path / "figures").mkdir(exist_ok=True)
    (tmp_path / "figures" / "onpage.pdf").write_bytes(b"%PDF-1.4\n%%EOF")
    get(session, "/figures/onpage.pdf")
    get(session, "/figures/renamed-away.png")
    assert session.blob["diagnostics"]["unstageable_assets"]

    asyncio.run(session.on_change())
    assert session.blob["diagnostics"].get("unstageable_assets") in (None, [])
    assert session.blob["diagnostics"].get("missing_assets") in (None, [])


def test_what_was_last_pushed_is_a_copy_all_the_way_down(tmp_path):
    """`seen_derived` records what the clients were told, so it may not share a
    single object with the blob it is compared against.

    The copy was one level deep: `dict(...)` per kind, with every VALUE still
    the blob's own object. A verdict changed in place then moved on both sides
    of the comparison at once, the two dicts came back equal, and the page was
    told nothing -- the same failure a whole-object reference caused for
    `diagnostics`, one level further in.
    """
    import json

    (tmp_path / "main.tex").write_text(BODY, encoding="utf-8")
    out = paths.cache(tmp_path)
    out.mkdir(parents=True, exist_ok=True)
    (out / "citations.json").write_text(json.dumps([
        {"cite_key": "k1", "title": "A paper", "has_fulltext": True},
    ]), encoding="utf-8")
    session = app.Session(tmp_path)
    assert "k1" in session.blob["cites"], session.blob["cites"]

    async def go():
        with pagedriver.record(session) as sent:
            session.blob["cites"]["k1"]["status"] = "verbatim"
            await session.push_derived()
        return sent

    sent = asyncio.run(go())
    kinds = [f["type"] for f in sent]
    assert "cites" in kinds, (
        "a verdict changed in place moved the record of what was last pushed "
        f"with it, so the page was never told: {kinds}")


# ------------------------------------------------------------------ (b) on the page

WHY = pagedriver.missing()


@pytest.mark.skipif(bool(WHY), reason=str(WHY))
def test_the_open_page_says_how_many_assets_are_missing(tmp_path):
    """The frame the route produced, delivered to the real page.

    The page under test is the one captured BEFORE the miss, which is the page
    the author is looking at: he opened it while the rasters were still there.
    """
    session = served(tmp_path)
    page = pagedriver.page(session)
    _, frames = get(session, "/figures/renamed-away.png", record=True)
    _, more = get(session, "/figures/also-gone.png", record=True)

    out = pagedriver.drive(page, frames + more, tmp_path=tmp_path)
    assert "2 assets missing" in (out["diagnostics"] or ""), out["diagnostics"]
    assert "restart" in out["diagnostics"]


@pytest.mark.skipif(bool(WHY), reason=str(WHY))
def test_the_page_sends_the_author_to_the_right_place_for_each_kind(tmp_path):
    """The instruction is the whole point of splitting the account in two.

    A figure whose PDF is on disk is not fixed by restarting anything, and the
    author who was told to restart went looking in the wrong place for nine
    exhibits that were never gone.
    """
    session = served(tmp_path)
    (tmp_path / "figures").mkdir(exist_ok=True)
    (tmp_path / "figures" / "onpage.pdf").write_bytes(b"%PDF-1.4\n%%EOF")
    page = pagedriver.page(session)
    _, frames = get(session, "/figures/onpage.pdf", record=True)

    out = pagedriver.drive(page, frames, tmp_path=tmp_path)
    said = out["diagnostics"] or ""
    assert "poppler" in said, said
    assert "restart" not in said, (
        f"a figure sitting in the manuscript was reported as a lost file: {said}")

    _, gone = get(session, "/figures/renamed-away.png", record=True)
    both = pagedriver.drive(pagedriver.page(session), gone, tmp_path=tmp_path)
    assert "restart" in (both["diagnostics"] or ""), both["diagnostics"]
    assert "poppler" in (both["diagnostics"] or ""), both["diagnostics"]


@pytest.mark.skipif(bool(WHY), reason=str(WHY))
def test_a_page_born_after_the_misses_says_it_too(tmp_path):
    """A reload must not lose the warning: the seed path reads the same field."""
    session = served(tmp_path)
    get(session, "/figures/renamed-away.png")
    out = pagedriver.drive(pagedriver.page(session), [], tmp_path=tmp_path)
    assert "1 asset missing" in (out["diagnostics"] or ""), out["diagnostics"]


@pytest.mark.skipif(bool(WHY), reason=str(WHY))
def test_a_page_with_every_asset_in_place_says_nothing(tmp_path):
    """The bar costs reading height, so it is not there when there is no news."""
    session = served(tmp_path)
    out = pagedriver.drive(pagedriver.page(session), [], tmp_path=tmp_path)
    assert not (out["diagnostics"] or "").strip(), out["diagnostics"]


# ------------------------------------------------------- (c) the third staleness gap
#
# A rebuild refreshes the figures of the document it rendered, and of no other.
# One directory holding `main.tex` and `supplement.tex` has ONE cache, so a
# session serving main re-rasterizes `f1`, `f2`, `f3` on every change and leaves
# every `sf*` exactly as some earlier build of the supplement left it. Measured
# live in covet-india on 2026-08-04: `f1..f3` rewritten at 20:16, every `sf*`
# still at 20:10, and nothing anywhere would ever have caught up until the
# supplement happened to be built again.
#
# The `.sha` sidecar already knows -- it holds the bytes the raster was made
# from. Nothing was ASKING it outside a rebuild, so the answer is to ask at
# serve time, in the one place that knows a browser wants the file.

MINI_PDF = (b"%PDF-1.4\n"
            b"1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n"
            b"2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj\n"
            b"3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 24 24] >> endobj\n"
            b"trailer << /Root 1 0 R >>\n%%EOF")
REDRAWN = MINI_PDF.replace(b"[0 0 24 24]", b"[0 0 96 48]")

NEEDS_PDFTOPPM = pytest.mark.skipif(
    shutil.which("pdftoppm") is None, reason="pdftoppm not installed")


def _doc(fig: str) -> str:
    return ("\\documentclass{article}\n\\begin{document}\n"
            "A paragraph long enough to be a block of its own, here so the build "
            "has something to render above the figure.\n\n"
            f"\\includegraphics{{{fig}}}\n\\end{{document}}\n")


def two_documents(tmp_path):
    """One directory, two documents, one cache. The live shape of the bug.

    Both documents are built once, which is what put both rasters in the cache;
    the session handed back is serving `main.tex`, so nothing it ever does will
    look at `sf1.pdf` again.
    """
    (tmp_path / "exhibits").mkdir()
    (tmp_path / "exhibits" / "f1.pdf").write_bytes(MINI_PDF)
    (tmp_path / "exhibits" / "sf1.pdf").write_bytes(MINI_PDF)
    (tmp_path / "main.tex").write_text(_doc("exhibits/f1.pdf"), encoding="utf-8")
    (tmp_path / "supp.tex").write_text(_doc("exhibits/sf1.pdf"), encoding="utf-8")
    app.Session(tmp_path, main="supp.tex")  # the other window, which made sf1's raster
    return app.Session(tmp_path, main="main.tex")


def raster_of(pdf_bytes: bytes, where: Path) -> bytes:
    """What `pdftoppm` makes of these bytes, produced independently of the route."""
    where.mkdir(parents=True, exist_ok=True)
    (where / "x.pdf").write_bytes(pdf_bytes)
    subprocess.run(["pdftoppm", "-png", "-r", "200", "-singlefile",
                    str(where / "x.pdf"), str(where / "x.pdf")],
                   capture_output=True, timeout=60)
    return (where / "x.pdf.png").read_bytes()


@NEEDS_PDFTOPPM
def test_a_figure_of_another_document_is_refreshed_before_it_is_served(tmp_path):
    """The document being served is the only one a rebuild ever refreshes.

    So the check cannot live in the rebuild. Asked for over real HTTP, the
    route hands back the raster of the PDF THAT IS ON DISK NOW, not the one the
    supplement's last build happened to leave behind.
    """
    session = two_documents(tmp_path)
    cached = paths.cache(session.root) / "exhibits" / "sf1.pdf.png"
    stale = cached.read_bytes()

    (tmp_path / "exhibits" / "sf1.pdf").write_bytes(REDRAWN)
    fresh = raster_of(REDRAWN, tmp_path / "expected")
    assert fresh != stale, "the fixture did not actually change the picture"

    res, _ = get(session, "/exhibits/sf1.pdf.png")
    assert res["status"] == 200
    assert res["body"] == fresh, (
        "the route served the raster of a PDF that no longer exists; a second "
        "window on the supplement gets a stale figure for as long as main is "
        "the document being rebuilt")


@NEEDS_PDFTOPPM
def test_the_refresh_reads_the_pdfs_bytes_and_not_its_mtime(tmp_path):
    """Same key as the build: content. A figure restored from git, or copied with
    `copy2`, arrives with an mtime that does not move forward, and an mtime gate
    would serve the old picture with nothing on the page to say so."""
    session = two_documents(tmp_path)
    pdf = tmp_path / "exhibits" / "sf1.pdf"
    was = pdf.stat()
    pdf.write_bytes(REDRAWN)
    os.utime(pdf, ns=(was.st_atime_ns, was.st_mtime_ns))

    res, _ = get(session, "/exhibits/sf1.pdf.png")
    assert res["body"] == raster_of(REDRAWN, tmp_path / "expected")


@NEEDS_PDFTOPPM
def test_an_unchanged_figure_is_not_re_rasterized_on_every_request(tmp_path):
    """This runs on the request path, so the no-news case has to be a stat and a
    hash and nothing else. Forking `pdftoppm` per GET would put a subprocess
    behind every image on the page, on every reload."""
    session = two_documents(tmp_path)
    cached = paths.cache(session.root) / "exhibits" / "sf1.pdf.png"
    before = cached.stat().st_mtime_ns
    for _ in range(3):
        res, _ = get(session, "/exhibits/sf1.pdf.png")
        assert res["status"] == 200
    assert cached.stat().st_mtime_ns == before, "an unchanged figure re-rasterized"


@NEEDS_PDFTOPPM
def test_a_figure_this_cache_never_held_is_rasterized_rather_than_missed(tmp_path):
    """A raster the cache does not hold but whose PDF is right there is not news
    about a lost file: it is a document this cache has not built yet. Recording
    it as missing would put a warning on the page over a figure the server can
    produce in the time the browser spends asking for it."""
    session = two_documents(tmp_path)
    (paths.cache(session.root) / "exhibits" / "sf1.pdf.png").unlink()
    (paths.cache(session.root) / "exhibits" / "sf1.pdf.png.sha").unlink()

    res, frames = get(session, "/exhibits/sf1.pdf.png", record=True)
    assert res["status"] == 200
    assert res["body"] == raster_of(MINI_PDF, tmp_path / "expected")
    assert frames == [], "a figure the server just made is not a missing asset"
    assert session.blob["diagnostics"].get("missing_assets") in (None, [])


@NEEDS_PDFTOPPM
def test_a_pdf_that_vanished_is_a_miss_and_not_an_error(tmp_path):
    """The source is gone and the cache no longer holds the raster either.

    The author deleted the figure, or renamed it, and the page still names it.
    That is the diagnostics path -- the same 404 as any other lost asset -- and
    emphatically not a traceback out of the rasterizer.
    """
    session = two_documents(tmp_path)
    (tmp_path / "exhibits" / "sf1.pdf").unlink()
    (paths.cache(session.root) / "exhibits" / "sf1.pdf.png").unlink()

    res, _ = get(session, "/exhibits/sf1.pdf.png")
    assert res["status"] == 404
    assert session.blob["diagnostics"]["missing_assets"] == ["exhibits/sf1.pdf.png"]


@NEEDS_PDFTOPPM
def test_a_pdf_that_vanished_still_serves_the_raster_that_outlived_it(tmp_path):
    """Nothing can be refreshed from a file that is gone, and the cached copy is
    still what the last build of that document produced. Blanking the figure
    would be strictly worse than showing it."""
    session = two_documents(tmp_path)
    cached = paths.cache(session.root) / "exhibits" / "sf1.pdf.png"
    kept = cached.read_bytes()
    (tmp_path / "exhibits" / "sf1.pdf").unlink()

    res, _ = get(session, "/exhibits/sf1.pdf.png")
    assert res["status"] == 200 and res["body"] == kept


@NEEDS_PDFTOPPM
def test_a_copied_image_is_refreshed_too(tmp_path):
    """Staging is one job with two halves, and the gap is in both of them.

    A `.png` the LaTeX names directly is mirrored into the cache by the same
    pass that rasterizes the PDFs, and a rebuild of the OTHER document refreshes
    it exactly as rarely. `copy2` carries the source's mtime onto the copy, so
    the two are current precisely when their (size, mtime) agree -- no second
    staleness key is invented here.
    """
    (tmp_path / "exhibits").mkdir()
    (tmp_path / "exhibits" / "f1.pdf").write_bytes(MINI_PDF)
    (tmp_path / "exhibits" / "sf1.png").write_bytes(PNG)
    (tmp_path / "main.tex").write_text(_doc("exhibits/f1.pdf"), encoding="utf-8")
    (tmp_path / "supp.tex").write_text(_doc("exhibits/sf1.png"), encoding="utf-8")
    app.Session(tmp_path, main="supp.tex")
    session = app.Session(tmp_path, main="main.tex")
    assert (paths.cache(session.root) / "exhibits" / "sf1.png").exists()

    redrawn = PNG + b"redrawn"
    (tmp_path / "exhibits" / "sf1.png").write_bytes(redrawn)
    res, _ = get(session, "/exhibits/sf1.png")
    assert res["status"] == 200
    assert res["body"] == redrawn, "the cache served the picture the author replaced"


def test_an_unchanged_image_is_not_re_copied_on_every_request(tmp_path, monkeypatch):
    """The mtime `copy2` carries over is what makes the no-news case free, and
    that is a claim about this filesystem, not only about the code: if the copy
    came back with a time that did not round-trip, every GET of every figure
    would copy the file again and nothing would ever say so."""
    from manuscriptor.render import postprocess

    (tmp_path / "exhibits").mkdir()
    (tmp_path / "exhibits" / "sf1.png").write_bytes(PNG)
    (tmp_path / "main.tex").write_text(_doc("exhibits/sf1.png"), encoding="utf-8")
    session = app.Session(tmp_path)

    copies: list = []
    real = postprocess.shutil.copy2
    monkeypatch.setattr(postprocess.shutil, "copy2",
                        lambda s, d, *a, **k: (copies.append(s), real(s, d, *a, **k))[1])
    for _ in range(3):
        assert get(session, "/exhibits/sf1.png")[0]["status"] == 200
    assert copies == [], f"an unchanged image was copied again, {len(copies)} times"


@NEEDS_PDFTOPPM
def test_the_figures_own_pdf_is_not_staged_into_the_cache_on_demand(tmp_path):
    """`.pdf` is an asset to the watcher, and this route is the catch-all, so a
    request for one arrives here. It is still a miss: a PDF figure reaches the
    page as its raster and never as itself, so no build has ever put one in the
    cache, and mirroring it on demand would fill the cache with a second copy of
    every exhibit -- staging a class of file no build produces."""
    session = two_documents(tmp_path)
    res, _ = get(session, "/exhibits/sf1.pdf")
    assert res["status"] == 404
    assert not (paths.cache(session.root) / "exhibits" / "sf1.pdf").exists()
    # Recorded as what it is: the PDF is right there in the manuscript, so this
    # is "nothing staged it", never "the server lost it, restart".
    assert session.blob["diagnostics"]["unstageable_assets"] == ["exhibits/sf1.pdf"]
    assert session.blob["diagnostics"].get("missing_assets") in (None, [])


@NEEDS_PDFTOPPM
def test_the_refresh_will_not_read_a_source_outside_the_manuscript(tmp_path):
    """The route's own guard refuses an escaping path before this is reached --
    both aiohttp and the client normalize `..` out of a URL, so the branch is
    only reachable by calling in directly. Guarded here as well because the
    refresh is a SECOND thing that turns a request path into a filesystem read.

    `refresh_asset` checks both ends, and the mutation check says so precisely:
    removing EITHER the cache-side or the manuscript-side containment leaves
    this passing, and removing both fails it. In the real layout the cache is
    inside the manuscript, so a path that climbs out of one climbs out of the
    other and each check alone holds the line. That is the reason to keep both
    rather than the reason to drop one -- a read-only serve already moves the
    cache to another filesystem entirely.
    """
    from manuscriptor.render import postprocess

    manuscript = tmp_path / "ms"
    (manuscript / "exhibits").mkdir(parents=True)
    (tmp_path / "secret.pdf").write_bytes(MINI_PDF)
    cache = tmp_path / "cache"
    cache.mkdir()

    assert postprocess.refresh_asset("../secret.pdf.png", manuscript, cache) is False
    assert not (cache / ".." / "secret.pdf.png").exists()
    assert not list(cache.iterdir()), "a source outside the manuscript was staged"


# Each end, alone. The test above passes with either check removed, because a
# `..` climbs out of both at once; these two do not. A symlink is what pulls
# them apart, and it is not a contrivance -- a figures directory pointed at a
# shared drive, or a cache directory holding one, is a thing people have.


def test_the_cache_side_containment_holds_on_its_own(tmp_path):
    """The destination resolves outside the cache while the source is a perfectly
    ordinary file inside the manuscript, so only the cache-side check can refuse
    this. Nothing may be written through the link."""
    from manuscriptor.render import postprocess

    manuscript = tmp_path / "ms"
    (manuscript / "figures").mkdir(parents=True)
    (manuscript / "figures" / "f.png").write_bytes(PNG)
    cache = tmp_path / "cache"
    cache.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (cache / "figures").symlink_to(outside, target_is_directory=True)

    assert postprocess.refresh_asset("figures/f.png", manuscript, cache) is False
    assert not list(outside.iterdir()), (
        "the refresh wrote through a link that leaves the cache directory")


def test_the_manuscript_side_containment_holds_on_its_own(tmp_path):
    """The mirror image: the destination is squarely inside the cache, and only
    the source resolves out of the manuscript."""
    from manuscriptor.render import postprocess

    manuscript = tmp_path / "ms"
    manuscript.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "f.png").write_bytes(PNG)
    (manuscript / "figures").symlink_to(elsewhere, target_is_directory=True)
    cache = tmp_path / "cache"
    (cache / "figures").mkdir(parents=True)

    assert postprocess.refresh_asset("figures/f.png", manuscript, cache) is False
    assert not (cache / "figures" / "f.png").exists(), (
        "a file reached through a link out of the manuscript was staged")
