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
from pathlib import Path
from urllib.parse import unquote

from manuscriptor.source import anchors
from manuscriptor.render.refs import resolve

_DATA_MX_RE = re.compile(r'data-mx="([^"]*)"')
_CITATION_SPAN_RE = re.compile(r'<span\s+class="citation"\s+data-cites="([^"]*)"')
_IMG_SRC_RE = re.compile(r'<img\s[^>]*?src="([^"]+)"', re.IGNORECASE)


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
    html, unresolved = resolve(html, labels)
    html = _tag_citations(html)
    assets = _copy_assets(html, Path(manuscript_dir), Path(output_dir))

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

    return {
        "html": html,
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
