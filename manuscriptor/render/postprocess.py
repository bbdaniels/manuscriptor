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
import re
import shutil
import unicodedata
from pathlib import Path
from urllib.parse import unquote

from manuscriptor.source import anchors
from manuscriptor.render.refs import resolve

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


# Pandoc emits <embed> for a PDF figure (and <img> only for raster formats),
# the asset copier only knew <img>, and a browser paints nothing for an
# unsized PDF embed: dsp-bias served with no figures at all. PDF figures are
# rasterized into the build directory at 200dpi (the docx recipe) and the
# element rewritten to a real <img>, which the figure CSS already styles.
_PDF_FIG_RE = re.compile(
    r'<(?:embed|img)\b[^>]*?\bsrc="([^"]+\.pdf)"[^>]*?/?>', re.I
)


def _pdf_figures_to_png(html: str, manuscript_dir: Path, output_dir: Path) -> tuple[str, list[str]]:
    import shutil
    import subprocess
    from urllib.parse import unquote

    if not shutil.which("pdftoppm"):
        return html, []
    manuscript_dir = Path(manuscript_dir).resolve()
    made: list[str] = []

    def one(m: re.Match) -> str:
        src = m.group(1)
        if src.startswith(("http:", "https:", "data:", "/")):
            return m.group(0)
        pdf = (manuscript_dir / unquote(src)).resolve()
        # Same containment rule as the asset copier: a src that walks out of
        # the manuscript directory is left alone, never followed.
        if manuscript_dir not in pdf.parents or not pdf.exists():
            return m.group(0)
        rel_png = str(Path(unquote(src)).with_suffix(".png"))
        dest = Path(output_dir) / rel_png
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.exists() or dest.stat().st_mtime < pdf.stat().st_mtime:
            subprocess.run(
                ["pdftoppm", "-png", "-r", "200", "-singlefile",
                 str(pdf), str(dest.with_suffix(""))],
                capture_output=True, timeout=60,
            )
        if not dest.exists():
            return m.group(0)
        made.append(rel_png)
        return f'<img src="{rel_png}" />'

    return _PDF_FIG_RE.sub(one, html), made


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

    html, reported = _harvest(html)
    html = _hoist_empty_anchors(html)
    html = _frontmatter_classes(html)
    html, unresolved = resolve(html, labels)
    # Split before tagging, so each key's span gets its own `data-cite-id`.
    html = _split_citation_groups(html)
    html = _tag_citations(html)
    html, rasterized = _pdf_figures_to_png(html, Path(manuscript_dir), Path(output_dir))
    assets = _copy_assets(html, Path(manuscript_dir), Path(output_dir)) + rasterized

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
    return {
        "html": _wrap_tables(_simple_math_to_html(html)),
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

        source = (manuscript_dir / rel).resolve()
        if not source.is_file():
            continue
        dest = (output_dir / rel).resolve()
        if out_root not in dest.parents:
            continue
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, dest)
        except OSError:
            continue
        copied.append(rel)

    return copied
