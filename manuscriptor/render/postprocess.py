"""M2 — turn pandoc output into an addressable document.

Harvests block markers onto `data-mx` attributes, wires citation spans to their
evidence records, and copies referenced assets alongside the output so the page
stands on its own.

`_augment_html` in `manuscriptor/evidence/render.py` already does the citation
half of this, and `_copy_assets` already does the asset half. Generalize both
rather than starting over.

Two decisions here are worth stating because the obvious alternative is wrong
in both cases.

**The unanchored set is computed here, not taken from the harvester.** A marker
can go missing for reasons the harvester never sees — pandoc dropped the
enclosing environment, the block fell inside a float that did not survive — and
in those cases there is no marker left to report. `postprocess` is the only
party holding both the list of blocks that were supposed to be there and the
html that came back, so it checks one against the other. When the harvester
does report, that report is merged in rather than replacing this.

**A citation id is derived from its content, not from its position.** The
project invariant is that identity never comes from position, and a citation is
no exception: numbering the spans in document order would renumber every one of
them the moment a paragraph above gains a reference, and every evidence record
keyed to them would drift. The id is a hash of the enclosing block's id and the
cite keys, with a `-2`, `-3` suffix when the same citation appears twice inside
one block, exactly mirroring the block id rule.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import threading
import unicodedata
import uuid
from pathlib import Path
from urllib.parse import unquote

from manuscriptor.source import anchors
from manuscriptor.render import cards, graphics
from manuscriptor.render.refs import resolve
# The header rows were IDENTIFIED in the LaTeX stage, in `render/tables.py`,
# where the rules that delimit a header are still visible. This module only
# carries the marking into `<thead>`; see `promote_marked_headers`.
from manuscriptor.render.tables import HEADER_TOKEN

_DATA_MX_RE = re.compile(r'data-mx="([^"]*)"')
_CITATION_SPAN_RE = re.compile(r'<span\s+class="citation"\s+data-cites="([^"]*)"')
_IMG_SRC_RE = re.compile(r'<img\s[^>]*?src="([^"]+)"', re.IGNORECASE)


_SIMPLE_MATH_RE = re.compile(
    r'<span class="math inline">\\\(([_^])\{([^{}\\]{1,12})\}\\\)</span>'
)


def _simple_math_to_html(html: str) -> str:
    """Turn trivially simple math into real HTML.

    Pandoc emits `\\(^{*}\\)` for a significance star and expects MathJax to
    render it. The page is deliberately self-contained and carries no MathJax,
    so the reader sees the markup instead of the stars, in every cell of every
    regression table.

    Only a bare superscript or subscript of plain characters is converted.
    Anything with structure is left as math, because a half-guess at real
    notation is worse than notation that has not rendered.
    """
    return _SIMPLE_MATH_RE.sub(
        lambda m: f"<{'sup' if m.group(1) == '^' else 'sub'}>{m.group(2)}"
                  f"</{'sup' if m.group(1) == '^' else 'sub'}>",
        html,
    )


# A paragraph pandoc built out of nothing but markers. Bare `<p>` only: the
# shape pandoc actually emits for one, and duplicating a `<p id=...>` across the
# split would mint duplicate ids in the document.
_MARKER_RUN_RE = re.compile(
    r"<p>\s*((?:" + anchors.MARKER_RE.pattern + r"\s*){2,})</p>"
)


def _split_marker_runs(html: str) -> str:
    r"""Give each marker in an all-marker paragraph a paragraph of its own.

    `harvest` hands one element to one block, by design -- two ids on one
    element is two blocks fighting over one click. So a paragraph holding two
    markers is a paragraph in which one block loses its anchor, and it is the
    LATER one that loses, which is the worst of the two to lose: the hoist below
    then promotes the earlier marker onto the following visible element, and the
    author clicks a heading and selects the page break in front of it.

    covet-india writes every one of its six exhibits as `\newpage` with
    `\subsection*{Fig. 2. ...}` on the very next line. No blank line between
    them, so LaTeX reads one paragraph and `\newpage` renders nothing inside it;
    pandoc emits `<p>MXnewpage\nMXheading</p><h2>`. All six headings came back
    unanchored and all six `<h2>`s carried the id of the page break above them.
    Reported 2026-08-03 as an inspector showing `\newpage` while the highlight
    sat on Fig. 2.

    Splitting restores the shape the rest of the pass was written for -- a run
    of consecutive empty anchors, which `_hoist_empty_anchors` already resolves
    correctly by giving the element to the NEAREST preceding one. Nothing
    downstream needs to learn about coalescing, and document order is preserved,
    so the margin still pins in the order the manuscript has.
    """
    return _MARKER_RUN_RE.sub(
        lambda m: "".join(f"<p>⟦MX{mid}⟧</p>"
                          for mid in anchors.MARKER_RE.findall(m.group(1))),
        html,
    )


_EMPTY_ANCHOR_RE = re.compile(
    r'<p\s+data-mx="([^"]+)"\s*>\s*</p>\s*(<([a-zA-Z][-\w]*)\b([^>]*)>)'
)


def _hoist_empty_anchors(html: str) -> str:
    """Move a marker that landed in its own empty paragraph onto what it anchors.

    A marker placed before a heading or a float comes out of pandoc as its own
    empty `<p>`, which is how it was designed to work: the orphan paragraph is a
    usable anchor. But it is also a visibly empty block sitting in the
    manuscript, and there are 123 of them in estonia-ecm, mostly before section
    headings.

    Hoisting fixes both halves. The empty block disappears, and the heading,
    figure or table becomes directly clickable, which is what a reader would
    expect to be able to select anyway.

    An element that already carries a block id is never overwritten; in that
    case the empty paragraph stays, because two blocks fighting over one element
    is worse than one empty line.

    The scan resumes AT the following tag rather than past it, because the
    following tag may itself be an empty anchor. `\\newpage` then
    `\\section{Introduction}` render as two empty anchors before one <h1>; a
    match that consumed the second anchor as its "following element" left the
    heading with no id at all, and the Introduction was unclickable on the
    live page. The nearest preceding anchor wins the element; the farther one
    stays as an empty anchor, which the viewer collapses.
    """
    out: list[str] = []
    i = 0
    while True:
        m = _EMPTY_ANCHOR_RE.search(html, i)
        if m is None:
            out.append(html[i:])
            return "".join(out)
        block_id, open_tag, attrs = m.group(1), m.group(2), m.group(4)
        if "data-mx=" in attrs:
            out.append(html[i: m.start(2)])
            i = m.start(2)
            continue
        out.append(html[i: m.start()])
        out.append(open_tag[:-1].rstrip() + f' data-mx="{block_id}">')
        i = m.end()


# The render pass rewrites \maketitle and the abstract into constructs pandoc
# keeps (render/pandoc.py, front matter), tagging each with a token. The token
# becomes a class on its element so the stylesheet can set the title apart
# from a section heading; and whatever happens, no token reaches the reader.
_FRONTMATTER_CLASSES = (
    ("⟦MXTITLE⟧", "doc-title"),
    ("⟦MXBYLINE⟧", "doc-byline"),
    ("⟦MXABSTRACT⟧", "doc-abstract-label"),
)


def _frontmatter_classes(html: str) -> str:
    for token, cls in _FRONTMATTER_CLASSES:
        pattern = re.compile(
            r"(<(?:h[1-6]|p)\b[^>]*?)(\s*>)\s*" + re.escape(token) + r"\s*"
        )

        def one(m: re.Match, cls: str = cls) -> str:
            open_tag = m.group(1)
            if 'class="' in open_tag:
                open_tag = open_tag.replace('class="', f'class="{cls} ', 1)
            else:
                open_tag += f' class="{cls}"'
            return open_tag + ">"

        html = pattern.sub(one, html)
        # A token that landed anywhere else is stripped rather than shipped.
        html = html.replace(token, "")
    return html


def stage_assets(html: str, manuscript_dir, output_dir) -> tuple[str, list[str]]:
    """Get this document's figures onto a page rendered from `output_dir`.

    ONE implementation, and every renderer calls it. Rasterizing a PDF figure
    and mirroring the images into the build directory are halves of a single
    job, and they were written twice: this pass, and the evidence viewer's own
    copier, which knew only `<img>`. Pandoc emits `<embed>` for a PDF, and a
    browser paints nothing for an unsized PDF embed, so every PDF figure was a
    blank rectangle in `index.html` -- all nine of covet-india's exhibits, all
    five of dsp-bias's -- with the caption beneath it rendering perfectly and
    the build exiting 0. The viewer's copy also percent-decoded nothing and
    refused nothing, so `../` in an image path wrote wherever the pass could
    reach.

    Rasterize first: it REWRITES `<embed src=...pdf>` into an `<img>`, and the
    copier reads the `<img>` elements it leaves behind.
    """
    manuscript_dir, output_dir = Path(manuscript_dir), Path(output_dir)
    html, rasterized = _pdf_figures_to_png(html, manuscript_dir, output_dir)
    return html, _copy_assets(html, manuscript_dir, output_dir) + rasterized


# --------------------------------------------------------- staging one asset
#
# A BUILD STAGES THE FIGURES OF THE DOCUMENT IT RENDERED, AND OF NO OTHER, so a
# build is the wrong moment to ask whether a staged file is still current. One
# directory holding `main.tex` and `supplement.tex` has ONE cache: a session
# serving main re-stages `f1`, `f2`, `f3` every time anything changes and never
# looks at an `sf*` again. Measured in covet-india on 2026-08-04 -- `f1..f3`
# rewritten at 20:16, every `sf*` left at 20:10 -- and no amount of rebuild
# scheduling closes it, because the document that would do the refreshing is the
# one nobody is rebuilding.
#
# So the question is asked where it can always be answered: at the moment a
# browser asks for the file. `refresh_asset` is that entry point, and it is the
# SAME two operations `stage_assets` runs, called one file at a time rather than
# reimplemented -- staging a figure and knowing whether a staged figure is stale
# are one piece of knowledge, and this repo has already paid for splitting one
# of those in two (see `render/tables.py`).


def raster_source(rel: str) -> str | None:
    """The manuscript path a raster's NAME claims, or None if it is not a raster.

    Reversible only because the raster keeps the PDF's own suffix:
    `exhibits/f1.pdf` stages as `exhibits/f1.pdf.png`, so the cache path names
    its own source exactly. That is a second reason for the naming rule above,
    beyond the collision it was introduced to prevent -- dropping the suffix
    would leave `f1.png` ambiguous between a rasterized `f1.pdf` and a `f1.png`
    the author wrote, and a serve-time refresh would have to guess.
    """
    if not rel.lower().endswith(".png"):
        return None
    source = rel[: -len(".png")]
    return source if source.lower().endswith(".pdf") else None


# One writer per destination, and a name no other writer can be holding.
#
# Both stagers below used to build their temp file from the destination's own
# name, which was safe for exactly as long as there was one caller: the build,
# under the session lock. The assets route now calls them from
# `asyncio.to_thread` on the REQUEST path, so two browsers asking for the same
# stale figure are two threads inside the same function -- writing the same
# temp, `os.replace`ing the interleaving into the file being served, stamping
# the sidecar fresh over it, and unlinking each other's output in `finally`.
# Reproduced as 20 lines in a raster one clean run writes 10 into.
#
# Both halves are needed and neither is sufficient. The unique name keeps two
# writers off one file (including writers in another process, which no lock of
# ours can see); the lock keeps the second caller from redoing work the first
# has already finished and from swapping an older rasterization in behind a
# newer one. The dict holds one lock per asset path, so it is bounded by the
# number of assets in the manuscript and never cleaned.
_staging_locks: dict[str, threading.Lock] = {}
_staging_locks_guard = threading.Lock()


def _staging_lock(dest: Path) -> threading.Lock:
    key = str(dest)
    with _staging_locks_guard:
        lock = _staging_locks.get(key)
        if lock is None:
            lock = _staging_locks[key] = threading.Lock()
        return lock


def _staging_name(dest: Path, stem: str) -> Path:
    """A temp path in the DESTINATION'S OWN DIRECTORY, unique to this call.

    Same directory so `os.replace` stays a rename within one filesystem, which
    is what makes the swap atomic. `mkstemp` is not usable for the rasterizer's
    half: `pdftoppm -singlefile` picks the final name itself by appending
    `.png` to the prefix it is handed, so the caller cannot be handed an
    already-open file descriptor.
    """
    return dest.with_name(f"{stem}.new-{uuid.uuid4().hex[:12]}")


def ensure_raster(pdf: Path, dest: Path) -> bool:
    """Rasterize `pdf` into `dest` unless the sidecar already names its bytes.

    Returns whether the cache holds a raster afterwards -- which may be an OLD
    one, when the rasterizer is absent or failed. Serving the picture from the
    last successful build beats serving nothing.

    THE SIDECAR IS ONLY WRITTEN WHEN `pdftoppm` ACTUALLY SUCCEEDED. It used to
    be written whenever a file was present afterwards, so a failed run over an
    existing raster stamped the new PDF's digest onto the old picture and pinned
    it fresh forever: the one state this cache must never be able to reach.

    THE RASTER IS SWAPPED IN, NOT WRITTEN IN PLACE. A request being served out
    of this cache reads the same path the rasterizer writes, and `pdftoppm`
    writes it progressively, so writing in place hands a browser a half-drawn
    PNG whenever the two coincide. `os.replace` is atomic on one filesystem, and
    the temp file is in the destination's own directory to keep it there.
    """
    stamp = dest.with_name(dest.name + ".sha")
    try:
        digest = hashlib.sha256(pdf.read_bytes()).hexdigest()
    except OSError:
        return dest.is_file()
    # Everything from the freshness check to the swap is one writer's, so the
    # second caller for the same figure reads the first one's finished work
    # instead of rasterizing over it. See `_staging_lock`.
    with _staging_lock(dest):
        was = stamp.read_text().strip() if stamp.exists() else ""
        if dest.is_file() and was == digest:
            return True
        if not shutil.which("pdftoppm"):
            return dest.is_file()
        # `-singlefile` appends `.png` to the prefix it is handed, so the prefix
        # is the destination with that suffix taken back off, plus a marker no
        # other call can be using.
        stem = dest.name[: -len(".png")] if dest.name.lower().endswith(".png") else dest.name
        prefix = _staging_name(dest, stem)
        made = prefix.with_name(prefix.name + ".png")
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            done = subprocess.run(
                ["pdftoppm", "-png", "-r", "200", "-singlefile", str(pdf), str(prefix)],
                capture_output=True, timeout=60,
            )
            if done.returncode == 0 and made.is_file():
                os.replace(made, dest)
                stamp.write_text(digest)
                return True
        except (OSError, subprocess.SubprocessError):
            pass
        finally:
            if made.is_file():
                made.unlink(missing_ok=True)
        return dest.is_file()


def mirror(source: Path, dest: Path) -> bool:
    """Put `source` in the cache at `dest`, unless the copy there is already it.

    `copy2` carries the source's mtime onto the copy, so the two are current
    exactly when their size and mtime agree. That is not a new staleness key
    invented for this check -- it is a reading of the one the copier already
    establishes, which is why nothing has to be recorded alongside the file.
    """
    try:
        now = source.stat()
    except OSError:
        return dest.is_file()
    # One writer per destination, and a staging name unique to this call: the
    # assets route calls this from a request thread, so two browsers asking for
    # the same stale image are two callers here. See `_staging_lock`.
    with _staging_lock(dest):
        try:
            have = dest.stat()
            if (have.st_size, have.st_mtime_ns) == (now.st_size, now.st_mtime_ns):
                return True
        except OSError:
            pass
        tmp = _staging_name(dest, dest.name)
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, tmp)
            os.replace(tmp, dest)
            return True
        except OSError:
            if tmp.is_file():
                tmp.unlink(missing_ok=True)
            return dest.is_file()


def manuscript_source(rel: str, manuscript_dir) -> Path | None:
    """The file in the MANUSCRIPT that a request for `rel` is asking about.

    `figures/f1.pdf.png` is asking about `figures/f1.pdf`; `figures/f1.png` is
    asking about itself. None when nothing of that name exists, or when the path
    climbs out of the manuscript.

    It answers two questions, which is why it is one function. `refresh_asset`
    needs it to know what to stage from; the assets route needs it to know what
    KIND of miss a 404 is -- a source still sitting on disk means the file was
    never staged here, which is a different fact from a figure the author
    renamed away, and a different instruction to give him.

    Which file a served name refers to is `graphics.source_path`'s question and
    not a second answer to it: an out-of-tree figure reaches the page under a
    mapped name (`\\graphicspath{{../outputs/}}` is ordinary in the corpus), and
    a copy of the arithmetic here would resolve that name to nothing while the
    copier staged it happily.
    """
    manuscript_dir = Path(manuscript_dir).resolve()
    source = graphics.source_path(unquote(raster_source(rel) or rel), manuscript_dir)
    if source is None or not source.is_file():
        return None
    return source


def refresh_asset(rel: str, manuscript_dir, cache_dir) -> bool:
    """Bring ONE staged asset up to date with the manuscript. Serve time.

    `rel` is the path the page named, relative to the cache. Returns whether the
    cache holds a file for it afterwards; the caller decides what a False means,
    because a missing asset is news on the request path and nothing at all on
    the build path.

    A source that has VANISHED is not an error and does not clear the cache: the
    author deleted or renamed a figure that a page still names, and whatever the
    last build staged is the best answer available. It goes on being served
    until a rebuild stops naming it, at which point it is a missing asset like
    any other.

    Containment is checked on BOTH ends, and this is the second place a request
    path is turned into a filesystem read. `..` cannot survive a real URL, but a
    rule that holds only because of something two layers away is a rule that
    stops holding when that layer changes.

    This refreshes only what a build STAGES, which is why a request for a `.pdf`
    is refused rather than mirrored. A PDF figure reaches the page as its raster
    and never as itself, so the build has never put one in the cache; copying it
    here on demand would make the route stage a class of file no build produces,
    and fill the cache with second copies of every exhibit.
    """
    manuscript_dir = Path(manuscript_dir).resolve()
    cache_dir = Path(cache_dir).resolve()
    dest = (cache_dir / rel).resolve()
    if cache_dir not in dest.parents:
        return False
    # A BUILD AUTHORIZES AN OUT-OF-TREE READ; A REQUEST NEVER DOES. An external
    # figure is served under a name that maps back to an absolute path, and a
    # browser can spell one of those as easily as the build can. Refreshing only
    # what the cache already holds keeps the route reading exactly the files a
    # render of the author's own `\graphicspath` resolved, and no others.
    if graphics.is_external(rel) and not dest.is_file():
        return False
    from_pdf = raster_source(rel)
    if from_pdf is None and rel.lower().endswith(".pdf"):
        return dest.is_file()
    source = manuscript_source(rel, manuscript_dir)
    if source is None:
        return dest.is_file()
    return ensure_raster(source, dest) if from_pdf else mirror(source, dest)


# Pandoc emits <embed> for a PDF figure (and <img> only for raster formats),
# the asset copier only knew <img>, and a browser paints nothing for an
# unsized PDF embed: dsp-bias served with no figures at all. PDF figures are
# rasterized into the build directory at 200dpi (the docx recipe) and the
# element rewritten to a real <img>, which the figure CSS already styles.
_PDF_FIG_RE = re.compile(
    r'<(?:embed|img)\b[^>]*?\bsrc="([^"]+\.pdf)"[^>]*?/?>', re.I
)


def _pdf_figures_to_png(html: str, manuscript_dir: Path, output_dir: Path) -> tuple[str, list[str]]:
    r"""Rasterize every PDF figure into the cache and point the page at it.

    Two things here are load-bearing, and both were learned the hard way against
    covet-india, which ships a `.png` beside every one of its nine `.pdf`
    exhibits.

    THE RASTER'S NAME KEEPS THE PDF'S OWN SUFFIX -- `fig.pdf` becomes
    `fig.pdf.png`, not `fig.png`. Dropping the suffix put the raster and the
    asset copier's mirror of a same-stem `fig.png` on ONE cache path, and the
    copier runs second, so the page served a file the LaTeX never named. Names
    that cannot collide are the fix; a rule about which pass wins would leave
    the two operations sharing a namespace and depending on their order forever.

    THE STALENESS KEY IS THE PDF'S CONTENT, not its mtime. The author
    regenerates figures constantly and a rebuilt PDF does not reliably arrive
    with a later mtime -- a restore from git, a `copy2`, or (before the renaming
    above) an mtime the asset copier had overwritten with another file's, which
    pinned the comparison false so the figure could never refresh again. A cache
    that serves a stale figure is worse than no cache, because the page shows a
    wrong picture and says nothing.

    Both of those rules live in `ensure_raster`, which the ASSETS ROUTE also
    calls: a build stages the figures of the document it rendered and of no
    other, so the same question has to be asked again when a browser asks for a
    file. One function answers it in both places.
    """
    if not shutil.which("pdftoppm"):
        return html, []
    manuscript_dir = Path(manuscript_dir).resolve()
    made: list[str] = []

    def one(m: re.Match) -> str:
        src = m.group(1)
        if src.startswith(("http:", "https:", "data:", "/")):
            return m.group(0)
        rel = unquote(src)
        # Same question, same answer, same module as the asset copier: a src
        # that walks out of the manuscript directory is left alone and never
        # followed, while the mapped name of an out-of-tree figure resolves.
        pdf = graphics.source_path(rel, manuscript_dir)
        if pdf is None or not pdf.is_file():
            return m.group(0)
        rel_png = rel + ".png"
        if not ensure_raster(pdf, Path(output_dir) / rel_png):
            return m.group(0)
        made.append(rel_png)
        return f'<img src="{rel_png}" />'

    return _PDF_FIG_RE.sub(one, html), made


# --------------------------------------------------------------- table heads


_TABLE_RE = re.compile(r"<table\b[^>]*>.*?</table>", re.S)
_TBODY_RE = re.compile(r"(<tbody[^>]*>)(.*?)(</tbody>)", re.S)
_TR_RE = re.compile(r"<tr\b[^>]*>.*?</tr>", re.S)


def promote_marked_headers(html: str) -> str:
    r"""Move the rows the LaTeX marked as header into `<thead>`, as `<th>`.

    Pandoc's LaTeX reader promotes at most ONE row, so a table with a two-deep
    header comes back as `<tbody>` and nothing else -- 72 of the corpus's 243
    tables. `render/tables.mark_header_rows` marks those rows before pandoc sees
    them, while the rules that delimit a header are still in the source. This
    only CARRIES that marking; it decides nothing.

    That division is the whole design. Nothing here may look at what a row
    contains and conclude it is a header: covet-india's Table 1 writes
    `\multicolumn{6}{l}{\textit{Patna}}` as a panel label in the middle of the
    body, which is a full-span row of bold-ish text and is not a header. In the
    LaTeX the rules say which is which; in the HTML they are gone.

    A table pandoc already gave a `<thead>` is left exactly as it is -- the
    single-header-row case, which has always worked -- and only has its tokens
    stripped. That branch is belt-and-braces rather than load-bearing, and it is
    written down as such: pandoc promotes a row only when it finds exactly one,
    and the marking finds exactly one in the same case, so the marked rows are
    already inside the `<thead>` and the fallback below would do nothing anyway.
    Deleting it changes no output today and would let a future pandoc that
    promotes two rows promote them twice.

    A table whose marked rows are ALL of its rows is also left alone: a table
    that is nothing but header is not a table, and emptying its body would be a
    worse answer.
    """
    def one(m: re.Match) -> str:
        table = m.group(0)
        if HEADER_TOKEN not in table:
            return table
        if "<thead" in table:
            return table.replace(HEADER_TOKEN, "")
        body = _TBODY_RE.search(table)
        if body is None:
            return table.replace(HEADER_TOKEN, "")
        inner = body.group(2)
        rows = list(_TR_RE.finditer(inner))
        head_rows = []
        for row in rows:
            if HEADER_TOKEN not in row.group(0):
                break
            head_rows.append(row)
        if not head_rows or len(head_rows) == len(rows):
            return table.replace(HEADER_TOKEN, "")
        thead = ("<thead>\n"
                 + "\n".join(_as_header_cells(r.group(0)) for r in head_rows)
                 + "\n</thead>\n")
        rebuilt = (
            table[: body.start()]
            + thead
            + body.group(1)
            + inner[head_rows[-1].end():].lstrip("\n")
            + body.group(3)
            + table[body.end():]
        )
        return rebuilt.replace(HEADER_TOKEN, "")

    # The sweep is the backstop, and it is not redundant. A table pandoc could
    # not read comes back as prose, with no `<table>` for the loop above to work
    # inside -- and a mark left in it is VISIBLE TEXT in the middle of the
    # manuscript. Nothing may reach the reader on the strength of a repair that
    # did not happen.
    return _TABLE_RE.sub(one, html).replace(HEADER_TOKEN, "")


def _as_header_cells(row: str) -> str:
    """`<td …>` becomes `<th …>` inside one promoted row, and nothing else moves.

    Attributes are untouched, which matters: `colspan` and the alignment style
    are what a spanning header row is made of, and rebuilding the tag rather
    than renaming it is how those get dropped.
    """
    row = re.sub(r"<td\b", "<th", row)
    return row.replace("</td>", "</th>")


_TABLE_OPEN_RE = re.compile(r"<table\b")


_CAPTION_RE = re.compile(r"<caption>(.*?)</caption>", re.S)


def _wrap_tables(html: str) -> str:
    r"""Give every table its own horizontal scroll container, and lift its notes out.

    A regression table with eight columns is wider than the reading measure. It
    must scroll inside itself; the manuscript column scrolling sideways would take
    the prose with it.

    Its CAPTION is a different matter, and it was wrong. A `<caption>` belongs to
    the table, so its width is the table's width: inside the scroll container the
    notes were as wide as eight columns of numbers, and reading the last sentence
    meant scrolling sideways to find it. A figure's caption has always sat below
    the image at the column's own measure. The notes are where the F-statistics
    are explained and the q-values are defined, so they are the part most likely
    to be read and were the part hardest to reach.

    So a captioned table becomes a `<figure>`: the scrolling part scrolls, and the
    caption sits under it as a `<figcaption>`, at the measure, exactly like a
    figure's. The anchor moves to the wrapper, because the block is now the whole
    thing rather than the table alone.
    """
    def one(m: re.Match) -> str:
        table = m.group(0)
        cap = _CAPTION_RE.search(table)
        # The anchor rides on the outer element, or a patch would replace the
        # table and leave the old notes sitting under the new numbers.
        attrs = m.group(1)
        anchor = ""
        mx = re.search(r'\sdata-mx="[^"]*"', attrs)
        if mx:
            anchor = mx.group(0)
            table = table.replace(anchor, "", 1)
        if cap is None:
            return f'<div class="table-scroll"{anchor}>{table}</div>'
        table = table.replace(cap.group(0), "", 1)
        return (f'<figure class="ms-table"{anchor}>'
                f'<div class="table-scroll">{table}</div>'
                f"<figcaption>{cap.group(1)}</figcaption>"
                "</figure>")

    return re.sub(r"<table([^>]*)>.*?</table>", one, html, flags=re.S)


def postprocess(
    html: str,
    *,
    blocks,
    manuscript_dir: Path,
    output_dir: Path,
    labels: dict[str, str],
) -> dict:
    """Make a rendered document addressable, resolved, and self-contained.

    Returns `html`, the block ids that could not be anchored, the reference
    keys that could not be resolved, and the asset paths copied. The two lists
    of failures are the point: an unanchored block is a paragraph the margin
    cannot address, and an unresolved reference is a `??` in a manuscript, and
    neither may be discovered by reading the page.
    """
    ids = [_block_id(b) for b in blocks]

    # Before the harvest, because the harvest is what loses the second marker
    # of a coalesced pair and it cannot get it back afterwards.
    html = _split_marker_runs(html)
    html, reported = _harvest(html)
    html = promote_marked_headers(html)
    html = _hoist_empty_anchors(html)
    html = _frontmatter_classes(html)
    html, unresolved = resolve(html, labels)
    # Split before tagging, so each key's span gets its own `data-cite-id`.
    html = _split_citation_groups(html)
    html = _tag_citations(html)
    html, assets = stage_assets(html, manuscript_dir, output_dir)

    # Report in the caller's own vocabulary. The marker contract writes ids
    # without the `b-` prefix, so the harvester's orphans arrive in that form;
    # handing a mixture of the two shapes back would give the margin two names
    # for the same block and no way to tell they are one.
    by_normalized = {_normalize_id(i): i for i in ids}
    present = {_normalize_id(v) for v in _DATA_MX_RE.findall(html)}
    unanchored = [i for i in ids if _normalize_id(i) not in present]
    for extra in reported:
        named = by_normalized.get(extra, extra)
        if named not in unanchored:
            unanchored.append(named)

    # Last, so the anchor check above counts data-mx on the real elements
    # rather than on wrappers this adds.
    #
    # The card shaping runs AFTER the table wrap and never before it: a
    # captioned table only becomes a card at that step, and folding first would
    # have the notes to put somewhere and no card to put them in. What a card is
    # made of -- what belongs in it, and which of that is the title -- is
    # `render/cards.py`'s question and this pass only calls it.
    drawn, _, _ = cards.shape_cards(_wrap_tables(_simple_math_to_html(html)))
    return {
        "html": drawn,
        "unanchored": unanchored,
        "unresolved_refs": unresolved,
        "assets": assets,
    }


# ------------------------------------------------------------------- anchors


def _harvest(html: str) -> tuple[str, list[str]]:
    """Call the harvester and normalize what it hands back.

    `anchors.harvest` belongs to another track. Its documented job is to move
    each marker onto its enclosing element and report any it could not attach,
    and it may express that as html alone or as html plus the report. Both are
    accepted; nothing downstream depends on which, because the unanchored set
    is recomputed from the blocks either way.
    """
    result = anchors.harvest(html)
    if isinstance(result, tuple):
        return result[0], [_normalize_id(x) for x in (result[1] or [])]
    return result, []


def _block_id(block) -> str:
    return getattr(block, "id", block)


def _normalize_id(value: str) -> str:
    """The marker contract writes ids without the `b-` prefix; block ids carry
    it. Compare on the bare hex so either form addresses the same block."""
    return value[2:] if value.startswith("b-") else value


# ----------------------------------------------------------------- citations


_CITATION_GROUP_RE = re.compile(
    r'<span\s+class="citation"\s+data-cites="([^"]*)"([^>]*)>(.*?)</span>', re.S
)
_SURNAME_RE = re.compile(r"^([A-Za-z]+)")


def _fold(text: str) -> str:
    """Lowercase and strip accents, so the key `grevisse2024` can be recognised
    in the name it rendered as, `Grévisse 2024`."""
    stripped = unicodedata.normalize("NFKD", text)
    return "".join(c for c in stripped if not unicodedata.combining(c)).lower()


def _contradicted(keys: list[str], chunks: list[str]) -> bool:
    """Does any key's own surname turn up in somebody else's rendered name?

    Splitting a group positionally assumes citeproc rendered the keys in the
    order they were cited, which is true of the styles in this corpus and NOT
    guaranteed: a CSL that sorts a group would pair every key with the wrong
    name. This is the check that catches that, and it is deliberately one-sided.
    A surname that appears nowhere (`who2019` renders as "World Health
    Organization 2019") is unverifiable, not contradicted, and does not block a
    split. A surname that appears somewhere else does block it.
    """
    folded = [_fold(c) for c in chunks]
    for i, key in enumerate(keys):
        m = _SURNAME_RE.match(key)
        if not m:
            continue
        surname = _fold(m.group(1))
        if len(surname) < 3:
            continue           # `li2025systematic`: too short to mean anything
        found = [j for j, c in enumerate(folded) if re.search(r"\b" + re.escape(surname), c)]
        if found and i not in found:
            return True
    return False


def _split_citation_groups(html: str) -> str:
    r"""One span per key, so each citation can carry its own evidence colour.

    Pandoc emits `\citep{a,b,c}` as a single span with three keys, and the page
    can only underline a span: the first key's status coloured the whole
    parenthetical and the other keys were invisible, supported or not. A
    five-key stack in dsp-bias read as verbatim on the strength of one key.

    The rendered names are separated by "; " and wrapped in the style's
    parentheses. The punctuation is left outside the new spans, so an underline
    covers a citation and not the bracket beside it. Anything that does not
    divide cleanly (a narrative `\citet` joined by "and", nested markup, a count
    that disagrees, or a pairing the surnames contradict) is left exactly as
    pandoc wrote it, because one colour over a stack is wrong and the wrong name
    against a key is worse.
    """
    def one(m: re.Match) -> str:
        keys = m.group(1).split()
        attrs, inner = m.group(2), m.group(3)
        if len(keys) < 2 or "<" in inner:
            return m.group(0)
        lead = trail = ""
        body = inner
        if body.startswith("(") and body.endswith(")"):
            lead, trail, body = "(", ")", body[1:-1]
        chunks = body.split("; ")
        if len(chunks) != len(keys) or _contradicted(keys, chunks):
            return m.group(0)
        spans = [
            f'<span class="citation" data-cites="{k}"{attrs}>{c}</span>'
            for k, c in zip(keys, chunks)
        ]
        return lead + "; ".join(spans) + trail

    return _CITATION_GROUP_RE.sub(one, html)


def _tag_citations(html: str) -> str:
    """Give every `<span class="citation">` a stable `data-cite-id`.

    Pandoc has already emitted the span and its `data-cites`; all this adds is
    an identity the evidence pass and the margin can both hold onto.
    """
    anchors_at = [(m.start(), m.group(1)) for m in _DATA_MX_RE.finditer(html)]
    seen: dict[str, int] = {}
    out: list[str] = []
    cursor = 0

    for m in _CITATION_SPAN_RE.finditer(html):
        block = _enclosing_block(anchors_at, m.start())
        keys = " ".join(sorted(m.group(1).split()))
        base = _hash(f"{block}|{keys}")
        seen[base] = seen.get(base, 0) + 1
        cite_id = base if seen[base] == 1 else f"{base}-{seen[base]}"
        out.append(html[cursor:m.end()])
        out.append(f' data-cite-id="{cite_id}"')
        cursor = m.end()

    out.append(html[cursor:])
    return "".join(out)


def _enclosing_block(anchors_at: list[tuple[int, str]], at: int) -> str:
    """The nearest `data-mx` opening before this point.

    Blocks do not nest in pandoc's output, so the most recent one to open is
    the one this citation sits in. A citation outside every block hashes
    against an empty string, which still gives it a content-derived id.
    """
    block = ""
    for pos, value in anchors_at:
        if pos > at:
            break
        block = _normalize_id(value)
    return block


def _hash(text: str) -> str:
    return "c-" + hashlib.sha256(" ".join(text.split()).encode("utf-8")).hexdigest()[:10]


# -------------------------------------------------------------------- assets


def _copy_assets(html: str, manuscript_dir: Path, output_dir: Path) -> list[str]:
    """Copy local images referenced in the rendered HTML into output_dir.

    Pandoc emits `<img src="exhibits/foo.png">` verbatim from the LaTeX, so the
    relative paths are mirrored under output_dir and the page renders
    standalone. Remote, absolute, and data URIs are already self-sufficient.

    Two things the ported version did not do. The src is percent-decoded before
    it is opened, because pandoc encodes spaces and the literal string names no
    file. And a path that resolves outside output_dir is refused: `../` in an
    image path would otherwise let a manuscript write anywhere the server can.

    The copy itself is `mirror`, which the assets route calls too -- a direct
    image goes stale for a document nobody is rebuilding exactly as a rasterized
    one does, and it is the same copy either way.
    """
    copied: list[str] = []
    seen: set[str] = set()
    out_root = output_dir.resolve()

    for m in _IMG_SRC_RE.finditer(html):
        raw = m.group(1)
        if raw.startswith(("http://", "https://", "data:", "//", "/")):
            continue
        rel = unquote(raw)
        if rel in seen:
            continue
        seen.add(rel)

        # What file this names is `graphics.source_path`'s answer, everywhere.
        # It refuses a path climbing out of the manuscript -- which is what the
        # `../secret.txt` guard has always meant -- and it accepts the mapped
        # name an out-of-tree figure was rewritten to on the way into pandoc.
        source = graphics.source_path(rel, manuscript_dir)
        if source is None or not source.is_file():
            continue
        dest = (output_dir / rel).resolve()
        if out_root not in dest.parents:
            continue
        if mirror(source, dest):
            copied.append(rel)

    return copied
