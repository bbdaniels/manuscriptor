"""Reading coauthor and reviewer markup back into the manuscript.

The author can comment *to* the tool. This is the other half of the problem: a
referee's marked-up PDF, or a coauthor's `.docx` full of tracked changes, coming
*in*. Both arrive as text somebody marked, plus what they wrote about it.

**Anchoring is by the marked text, never by the page number.** This is the whole
design and it is worth being explicit about why. A page number is a fact about
one compilation of one draft: rewrite a paragraph in section 2 and every page
number after it is wrong, silently, with nothing on the page to say so. The
sentence a referee highlighted is a fact about the manuscript, and it stays
findable for as long as it survives. So the page is recorded for the author to
read and is never consulted when deciding where a comment belongs.

The matching is the same machinery as comment drift: normalize both sides and
score with `difflib`, exactly as `blocks.rematch` and `build.reanchor_chats` do.
The one difference is the shape of the question. `rematch` compares two whole
paragraphs, so a symmetric ratio is right. Here a short quote is being looked for
*inside* a long paragraph, so the score is containment: what fraction of the
quote the paragraph accounts for. A symmetric ratio would rank a long paragraph
below a short one for the same quote, which is backwards.

**Anything that cannot be placed confidently goes to a tray.** A referee comment
attached to the wrong sentence is worse than one the author has to place by hand,
because he will read it as being about that sentence and act on it. Three things
land in the tray: a sticky note that marks no text at all (the page is not an
anchor), a quote too short to be distinctive, and a quote that fits several
paragraphs equally well. The tray offers the paragraphs each one nearly matched,
so placing it is a click rather than a hunt.

**An imported comment is an ordinary comment.** It is a `kind:"comment"` record
in `comments.jsonl` like any other, carrying the commenter's name as its author,
so it queues, shows in the margin, and drains exactly like one the author left
himself. There is no parallel system, and the drain needed no changes to read
these.

Two records are written per mark, and the pair is deliberate. The
`kind:"import"` record is the provenance: who marked what, on which page of
which file, and what score placed it. The `kind:"comment"` record is the work.
They are different facts, and folding them into one would lose the marked text
and the file it came from the moment the comment was answered. The import record
is also what makes a second import of the same file a no-op, since it carries a
fingerprint of the mark that does not include the page.

Nothing here calls a model. This module reads a file and appends to the comment
log, and that is the whole of what it can do.
"""
from __future__ import annotations

import difflib
import hashlib
import io
import re
import unicodedata
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

from manuscriptor.server import chat

# ---------------------------------------------------------------- the dials
#
# Set deliberately low on trust. Every one of these decides between "place it"
# and "the author places it", and the two failures are not symmetric: an unplaced
# comment costs a click, a misplaced one costs a wrong edit to the wrong
# paragraph. Measured against estonia-ecm: a real highlight scores 1.00 when the
# paragraph is untouched and 0.93 when it carries a citation the PDF renders and
# the source does not.

MIN_ANCHOR = 24     # normalized characters. Below this a quote is not distinctive.
STRONG = 0.85       # containment score needed to place a mark at all
MARGIN = 0.08       # how far the best must beat the runner-up to be unambiguous
CANDIDATES = 3      # how many near misses the tray offers
PREFILTER = 24      # blocks scored properly, chosen by cheap token overlap
MIN_RUN = 4         # characters. Shorter agreements than this are coincidence.

MARKUP = {
    "Highlight": "highlight",
    "StrikeOut": "strikeout",
    "Underline": "underline",
    "Squiggly": "squiggly",
    "Caret": "insertion",
    "Text": "note",
    "FreeText": "note",
    "Square": "note",
}

# What a mark means, when the commenter typed nothing alongside it. Stating the
# mark is a fact; turning it into an imperative would be inventing an intent the
# file does not carry, so the body says what was done and the reader decides.
VERB = {
    "highlight": "Highlighted",
    "strikeout": "Struck out",
    "underline": "Underlined",
    "squiggly": "Marked",
    "insertion": "Inserted",
    "deletion": "Deleted",
    "note": "Noted",
    "comment": "Commented",
}


class Unreadable(Exception):
    """A file this importer has no reader for."""


@dataclass(frozen=True)
class Mark:
    """One annotation, as the file gave it.

    `anchor` is what gets matched and `marked` is what the commenter actually
    marked; they differ for a tracked insertion, whose new words are by
    definition not in the manuscript yet, so the only thing that can place it is
    the paragraph around it.

    `page` and `where` are for the author to read. Nothing in `place` may look at
    them, which is the point of keeping them off the matching path entirely.
    """

    kind: str
    anchor: str
    marked: str
    note: str
    author: str
    where: str
    page: int | None = None
    fingerprint: str = ""


@dataclass(frozen=True)
class Placement:
    """Where a mark landed, and why."""

    mark: Mark
    block: str | None
    score: float
    reason: str
    candidates: tuple = field(default_factory=tuple)


# ------------------------------------------------------------- normalization


_DROP_WITH_ARG = re.compile(
    r"\\(?:label|ref|autoref|cref|Cref|eqref|pageref|nameref"
    r"|cite[a-zA-Z]*|nocite|bibliography[a-z]*|bibliographystyle"
    r"|input|include|includegraphics|usepackage|documentclass"
    r"|index|vspace|hspace|setlength|addtolength|renewcommand|newcommand"
    r"|graphicspath|geometry|hypersetup)\*?"
    r"(?:\s*\[[^\]]*\])*\s*\{[^{}]*\}(?:\s*\{[^{}]*\})?"
)
_ENVIRONMENT = re.compile(r"\\(?:begin|end)\s*\{[^{}]*\}(?:\s*\[[^\]]*\])?")
_COMMENT = re.compile(r"(?<!\\)%.*")
_CONTROL = re.compile(r"\\(?:[a-zA-Z@]+\*?|.)")


def plain(tex: str) -> str:
    """LaTeX reduced to the words a reader sees in the PDF.

    The referee read a typeset document and the manuscript is markup, so
    something has to make the two comparable. Commands whose argument is
    invisible in print (`\\label`, `\\citep`, `\\input`) go with their argument;
    everything else loses its name and keeps its content, because `\\textbf{
    large}` and `\\caption{...}` both reach the reader as words.

    Math delimiters are removed rather than the math dropped. In these
    manuscripts inline math is very often a single number, and dropping it would
    lose exactly the part of a sentence a referee is most likely to mark.
    """
    s = _COMMENT.sub(" ", tex)
    s = _DROP_WITH_ARG.sub(" ", s)
    s = _ENVIRONMENT.sub(" ", s)
    s = s.replace("\\(", " ").replace("\\)", " ").replace("\\[", " ").replace("\\]", " ")
    s = _CONTROL.sub(" ", s)
    for ch in "${}&~^_":
        s = s.replace(ch, " ")
    return s


def norm(s: str) -> str:
    """Letters, digits and single spaces, case-folded and compatibility-decomposed.

    Both passes earn their place, on different characters. Case folding is what
    splits the `ﬁ` ligature a PDF extractor hands back, because full folding maps
    it to two letters. NFKD is what separates an accent from its letter, so
    `Montr\\'eal` in the source and `Montréal` out of a PDF reduce to the same
    string; folding alone leaves `é` intact and the two stop matching.
    """
    out: list[str] = []
    space = True
    for ch in unicodedata.normalize("NFKD", s or ""):
        if ch.isalnum():
            out.append(ch.casefold())
            space = False
        elif unicodedata.combining(ch):
            # The accent NFKD just split off is part of the letter before it,
            # not a break between words. Treating it as one turned `Montréal`
            # into `montre al` and cost the paragraph its own comment.
            continue
        elif not space:
            out.append(" ")
            space = True
    return "".join(out).strip()


# ------------------------------------------------------------------ matching


def containment(hay: str, needle: str) -> float:
    """How much of `needle` the `hay` accounts for, in runs of real words.

    Not a symmetric ratio. The question is whether a quote is inside a
    paragraph, and `SequenceMatcher.ratio` divides by the length of both, so the
    same quote would score lower against a long paragraph than a short one for
    no reason connected to whether it is there.

    ONLY RUNS OF AT LEAST `MIN_RUN` CHARACTERS COUNT, and that is not a tuning
    detail: without it the score rewards the size of the haystack. A 4000
    character regression table can supply almost every letter of an 84 character
    sentence one and two characters at a time, and on the reference manuscript
    it did -- `table2_cross.tex` scored 0.92 against a prose sentence it does not
    contain a word of, which was enough to crowd the real paragraph at 0.95 into
    the tray as ambiguous. Counting only word-length agreements drops the table
    to 0.57 and leaves the real match at 0.93.
    """
    if not needle:
        return 0.0
    if needle in hay:
        return 1.0
    sm = difflib.SequenceMatcher(None, hay, needle, autojunk=False)
    run = sum(b.size for b in sm.get_matching_blocks() if b.size >= MIN_RUN)
    return min(1.0, run / len(needle))


def block_texts(blocks) -> dict[str, str]:
    """block id -> its normalized rendered words.

    Built from `flat_text`, not `source_text`, and that matters. A computed value
    reaches the page as `\\input{exhibits/pval}` in the source and as `0.096` in
    the PDF, so matching on the source would fail on precisely the sentences
    carrying results, which is what referees comment on most.
    """
    return {b.id: norm(plain(b.flat_text)) for b in blocks}


def rank(anchor: str, texts: dict[str, str]) -> list[tuple[str, float]]:
    """Every plausible block for this anchor, best first.

    A cheap token-overlap pass first, because scoring 400 paragraphs properly
    against every mark in a referee report is quadratic in a way nobody would
    notice until a real manuscript made it slow.
    """
    needle = norm(anchor)
    want = set(needle.split())
    if not needle or not want:
        return []

    # A VERBATIM MATCH IS NEVER SUBJECT TO THE PREFILTER. The cheap pass ranks by
    # shared vocabulary, and a paragraph sharing the same words in a different
    # order ties with the one that contains the sentence word for word, so with
    # enough boilerplate around it the real paragraph can be sorted out of the
    # window and lost. Merged in rather than short-circuited, so there is one
    # scoring path and ambiguity among verbatim matches is still seen.
    exact = [bid for bid, t in texts.items() if needle in t]
    cheap = sorted(
        ((len(want & set(t.split())) / len(want), bid) for bid, t in texts.items()),
        key=lambda pair: (-pair[0], pair[1]),
    )
    pool = dict.fromkeys(exact)
    pool.update(dict.fromkeys(bid for share, bid in cheap[:PREFILTER] if share > 0))

    scored = [(bid, containment(texts[bid], needle)) for bid in pool]
    scored.sort(key=lambda pair: (-pair[1], pair[0]))
    return scored


def place(marks, blocks) -> list[Placement]:
    """Decide where each mark goes, or that it goes to the tray.

    Four ways to end up unplaced, and each says which one it was, because "could
    not place it" with no reason leaves the author guessing whether the importer
    is broken or the paragraph is gone.
    """
    texts = block_texts(blocks)
    out: list[Placement] = []
    for m in marks:
        needle = norm(m.anchor)
        if not needle:
            out.append(Placement(m, None, 0.0, "nothing was marked, only a page"))
            continue
        if len(needle) < MIN_ANCHOR:
            out.append(Placement(m, None, 0.0, "the marked text is too short to be distinctive"))
            continue

        scored = rank(m.anchor, texts)
        near = tuple({"block": bid, "score": round(s, 3)} for bid, s in scored[:CANDIDATES])
        if not scored:
            out.append(Placement(m, None, 0.0, "no paragraph matched the marked text", near))
            continue

        best_id, best = scored[0]
        second = scored[1][1] if len(scored) > 1 else 0.0
        if best < STRONG:
            out.append(Placement(m, None, best, "no paragraph matched the marked text closely enough", near))
        elif best - second < MARGIN:
            out.append(Placement(m, None, best, "the marked text fits more than one paragraph equally well", near))
        else:
            out.append(Placement(m, best_id, best, "", near))
    return out


# -------------------------------------------------------------- PDF markup


def read_pdf(data: bytes, source: str) -> list[Mark]:
    """Annotations out of a marked-up PDF.

    `pdfannots` does the reading, for the reason the author's own pdf-comments
    skill gives: it maps quadpoints back to text and handles line wrapping and
    hyphenation, and a hand-rolled extraction gets both of those subtly wrong.
    It sits on `pdfminer.six`, which this project already depends on.
    """
    import pdfannots

    # `process_file` reads `file.name` to label its progress output, before
    # checking whether any was asked for, so a bare BytesIO raises. Naming the
    # buffer is what the library wants and keeps the referee's PDF out of the
    # filesystem: a server that scattered uploaded files would be a worse
    # answer than an attribute.
    fh = io.BytesIO(data)
    fh.name = source
    with fh:
        doc = pdfannots.process_file(fh)

    marks: list[Mark] = []
    for a in doc.iter_annots():
        kind = MARKUP.get(a.subtype.name, "note")
        marked = (a.gettext(remove_hyphens=True) or "").strip()
        notes = [t for t in [a.contents] + [r.contents for r in a.replies] if t]
        note = "\n\n".join(t.strip() for t in notes)

        # A strikeout is often two words, which places nothing on its own.
        # pdfannots captures the surrounding text for exactly this case, so the
        # anchor widens to the sentence while the body still quotes the strike.
        anchor = marked
        if len(norm(anchor)) < MIN_ANCHOR and (a.pre_context or a.post_context):
            anchor = f"{a.pre_context or ''}{marked}{a.post_context or ''}"

        page = a.pos.page.pageno + 1
        marks.append(Mark(
            kind=kind, anchor=anchor, marked=marked, note=note,
            author=(a.author or "").strip() or _who(source),
            where=f"page {page}", page=page,
        ))
    return _fingerprint(marks, source)


# ------------------------------------------------------------- .docx markup
#
# Read as OOXML rather than through python-docx, which exposes neither tracked
# changes nor comments. A .docx is a zip of XML and the standard library opens
# both, so this costs no dependency and reaches the parts that matter.

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _tag(el) -> str:
    return el.tag.split("}")[-1] if isinstance(el.tag, str) else ""


def _text_of(el) -> str:
    """Every run of visible text under an element, in order."""
    out = []
    for node in el.iter():
        if _tag(node) in ("t", "delText"):
            out.append(node.text or "")
    return "".join(out)


class _Reader:
    """One walk of the document body, in reading order.

    A comment range is opened and closed by empty elements that are siblings of
    the runs they bracket, and it may cross paragraphs, so the text it covers
    can only be collected by walking the body once and remembering which ranges
    are open. Text inside a `w:ins` is excluded from the anchors, because it is
    not in the manuscript yet: matching on it would place every insertion in the
    tray.
    """

    def __init__(self) -> None:
        self.paragraphs: list[str] = []
        self.ranges: dict[str, list[str]] = {}
        self.range_para: dict[str, int] = {}
        self._open: set[str] = set()
        self._cur: list[str] | None = None

    def walk(self, el, in_ins: bool = False) -> None:
        tag = _tag(el)
        if tag == "p":
            self._cur = []
            self.paragraphs.append("")
        elif tag == "ins":
            in_ins = True
        elif tag == "commentRangeStart":
            cid = el.get(_W + "id")
            if cid is not None:
                self._open.add(cid)
                self.ranges.setdefault(cid, [])
                self.range_para.setdefault(cid, max(1, len(self.paragraphs)))
        elif tag == "commentRangeEnd":
            self._open.discard(el.get(_W + "id") or "")
        elif tag in ("t", "delText") and not in_ins:
            text = el.text or ""
            if self._cur is not None:
                self._cur.append(text)
            for cid in self._open:
                self.ranges[cid].append(text)

        for kid in el:
            self.walk(kid, in_ins)

        if tag == "p" and self._cur is not None:
            self.paragraphs[-1] = "".join(self._cur)
            self._cur = None


def read_docx(data: bytes, source: str) -> list[Mark]:
    """Tracked changes and comments out of a `.docx`."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise Unreadable(f"{source} is not a readable .docx: {exc}") from exc

    with zf:
        names = set(zf.namelist())
        if "word/document.xml" not in names:
            raise Unreadable(f"{source} has no word/document.xml; it is not a Word file")
        body = ET.fromstring(zf.read("word/document.xml"))
        notes: dict[str, dict] = {}
        if "word/comments.xml" in names:
            for c in ET.fromstring(zf.read("word/comments.xml")).iter(_W + "comment"):
                notes[c.get(_W + "id") or ""] = {
                    "author": (c.get(_W + "author") or "").strip() or _who(source),
                    "text": _text_of(c).strip(),
                }

    reader = _Reader()
    reader.walk(body)

    marks: list[Mark] = []

    # Tracked changes, paragraph by paragraph. The anchor is the paragraph, not
    # the change: an insertion's words are new, and a deletion is a request about
    # the sentence it sits in rather than about the struck fragment alone.
    for n, para in enumerate(body.iter(_W + "p"), start=1):
        around = reader.paragraphs[n - 1] if n - 1 < len(reader.paragraphs) else _text_of(para)
        for el in para.iter():
            tag = _tag(el)
            if tag not in ("ins", "del"):
                continue
            text = _text_of(el).strip()
            if not text:
                continue      # a formatting-only change carries no words
            marks.append(Mark(
                kind="insertion" if tag == "ins" else "deletion",
                anchor=around, marked=text, note="",
                author=(el.get(_W + "author") or "").strip() or _who(source),
                where=f"paragraph {n}", page=None,
            ))

    for cid, note in notes.items():
        covered = "".join(reader.ranges.get(cid, [])).strip()
        para = reader.range_para.get(cid, 0)
        if not covered and 0 < para <= len(reader.paragraphs):
            covered = reader.paragraphs[para - 1]
        marks.append(Mark(
            kind="comment", anchor=covered, marked=covered, note=note["text"],
            author=note["author"],
            where=f"paragraph {para}" if para else "the document", page=None,
        ))
    return _fingerprint(marks, source)


# ------------------------------------------------------------------ ingest


READERS = {".pdf": read_pdf, ".docx": read_docx}


def read_marks(data: bytes, source: str) -> list[Mark]:
    suffix = Path(source).suffix.lower()
    reader = READERS.get(suffix)
    if reader is None:
        raise Unreadable(
            f"nothing here reads {suffix or 'a file with no extension'}: "
            f"bring markup in as a .pdf or a .docx"
        )
    return reader(data, source)


def ingest(data: bytes, source: str, *, blocks, log) -> dict:
    """Read a marked-up file and write what it said into the comment log.

    Returns the report the page and the CLI both show: how many anchored, how
    many are waiting in the tray, and which file they came from. A mark already
    read in from this file is counted and skipped, so importing the same referee
    report twice does not double every comment.
    """
    log = Path(log)
    marks = read_marks(data, source)
    placements = place(marks, blocks)

    by_id = {b.id: b for b in blocks}
    seen = {r["fp"]: iid for iid, r in _import_records(log)[0].items() if r.get("fp")}
    n = _next_import(log)

    items: list[dict] = []
    anchored = unplaced = already = 0

    for p in placements:
        if p.mark.fingerprint in seen:
            already += 1
            items.append(_item(seen[p.mark.fingerprint], p, None, source, already=True))
            continue

        iid = f"i-{n:04d}"
        n += 1
        seen[p.mark.fingerprint] = iid
        comment_id = None
        if p.block is not None and p.block in by_id:
            comment_id = chat.next_id(log)
        record = {
            "id": iid,
            "kind": "import",
            "source": source,
            "mark": p.mark.kind,
            "author": p.mark.author,
            "marked": p.mark.marked,
            "note": p.mark.note,
            "where": p.mark.where,
            # What was matched on, which is not always what was marked: a
            # strikeout of two words is anchored by the sentence around it, and
            # a tracked insertion by the paragraph it was inserted into. Stored
            # so the tray re-scores on the same text the placer used, rather
            # than on a different one that would rank the candidates differently.
            "anchor": p.mark.anchor,
            "fp": p.mark.fingerprint,
            "block": p.block,
            "score": round(p.score, 3),
            "reason": p.reason,
            "candidates": list(p.candidates),
            "comment": comment_id,
        }
        chat.append(log, record)

        if comment_id is not None:
            b = by_id[p.block]
            chat.append(log, {
                "id": comment_id,
                "kind": "comment",
                "block": p.block,
                "file": str(b.file),
                "lines": [b.line_start, b.line_end],
                # The block's own source, exactly as `on_chat` records it, so an
                # imported comment is re-found by `reanchor_chats` through an
                # edit like every other comment. Recording the referee's quote
                # here instead would look right and would break re-anchoring for
                # precisely the comments most likely to cause an edit.
                "quote": b.source_text[:120],
                "body": body_of(p.mark),
                "author": p.mark.author,
                "from": iid,
            })
            anchored += 1
        else:
            unplaced += 1
        items.append(_item(iid, p, comment_id, source))

    return {
        "file": source,
        "marks": len(marks),
        "anchored": anchored,
        "unplaced": unplaced,
        "already": already,
        "items": items,
    }


def body_of(mark: Mark) -> str:
    """What the comment says.

    A typed note is used verbatim, because a referee's words are an instruction
    and paraphrasing one is how a revision goes wrong. A bare mark has no words,
    so the body reports the mark and quotes what it covered.
    """
    if mark.note and mark.marked and mark.kind in ("insertion", "deletion"):
        return f'{VERB[mark.kind]} by {mark.author}: "{mark.marked}"\n\n{mark.note}'
    if mark.note:
        return mark.note
    if mark.marked:
        return f'{VERB.get(mark.kind, "Marked")} by {mark.author}: "{_clip(mark.marked)}"'
    return f"{VERB.get(mark.kind, 'Marked')} by {mark.author}, with nothing written alongside it"


# -------------------------------------------------------------------- tray


def tray(log, blocks=None) -> list[dict]:
    """Marks still waiting to be placed, oldest first.

    Candidates are re-scored against the manuscript as it is now when blocks are
    passed. A mark imported yesterday against a paragraph that has since been
    rewritten should offer where that paragraph went, not where it used to be.
    """
    base, state = _import_records(Path(log))
    texts = block_texts(blocks) if blocks is not None else None

    out: list[dict] = []
    for iid, rec in base.items():
        if rec.get("comment") or state.get(iid, {}).get("comment"):
            continue
        item = {
            "id": iid,
            "source": rec.get("source", ""),
            "kind": rec.get("mark", "note"),
            "author": rec.get("author", ""),
            "marked": rec.get("marked", ""),
            "note": rec.get("note", ""),
            "where": rec.get("where", ""),
            "reason": rec.get("reason", ""),
            "candidates": rec.get("candidates", []),
            "ts": rec.get("ts", ""),
        }
        if texts is not None:
            # ONLY ever what the placer matched on, never the commenter's own
            # words. A mark that marked nothing has nothing to rank against, and
            # `rank` returns nothing for it, which is the answer: offering
            # "place on the Discussion, 59%" off the note's own text reads as
            # evidence, is noise, and contradicts `place`, which refuses to
            # anchor a note at all. Seen doing exactly that in a browser.
            anchor = rec.get("anchor") or rec.get("marked", "")
            item["candidates"] = [
                {"block": bid, "score": round(s, 3)}
                for bid, s in rank(anchor, texts)[:CANDIDATES]
            ]
        out.append(item)
    out.sort(key=lambda e: (e["ts"], e["id"]))
    return out


def anchored_marks(log, blocks) -> dict[str, list[dict]]:
    """Placed marks, keyed by the block their comment sits on NOW.

    Not by the block id recorded at import. Ids are content-derived, so the first
    edit answering a referee renames the paragraph and a map keyed to the old id
    would show the paragraph's own markup vanishing at the moment it was
    addressed. The comment is already re-anchored by the same pass the margin
    uses, so the mark follows its comment rather than being re-matched.
    """
    from manuscriptor.server import build as build_mod

    base, state = _import_records(Path(log))
    by_comment: dict[str, dict] = {}
    for iid, rec in base.items():
        cid = rec.get("comment") or state.get(iid, {}).get("comment")
        if cid:
            by_comment[cid] = rec

    if not by_comment:
        return {}

    log = Path(log)
    where = build_mod.reanchor_chats(chat.by_block(log), blocks, chat.read_chats(log))
    live = {b.id for b in blocks}
    out: dict[str, list[dict]] = {}
    for block_id, msgs in where.items():
        if block_id not in live:
            continue
        for m in msgs:
            rec = by_comment.get(m["id"])
            if rec is None:
                continue
            out.setdefault(block_id, []).append({
                "id": rec["id"], "comment": m["id"], "source": rec.get("source", ""),
                "kind": rec.get("mark", "note"), "author": rec.get("author", ""),
                "marked": rec.get("marked", ""), "note": rec.get("note", ""),
                "where": rec.get("where", ""), "score": rec.get("score", 0),
            })
    return out


def place_mark(log, import_id: str, block_id: str, *, blocks) -> dict:
    """Put a waiting mark on the paragraph the author chose.

    Appends, never rewrites: a comment record for the work and an import record
    saying the mark is placed. The pair is what keeps the tray correct across a
    restart without anything having to be edited in place.
    """
    log = Path(log)
    base, state = _import_records(log)
    rec = base.get(import_id)
    if rec is None:
        raise KeyError(f"no imported mark {import_id}")
    if rec.get("comment") or state.get(import_id, {}).get("comment"):
        raise ValueError(f"{import_id} was already placed")

    block = next((b for b in blocks if b.id == block_id), None)
    if block is None:
        raise KeyError(f"no block {block_id}")

    mark = Mark(
        kind=rec.get("mark", "note"), anchor=rec.get("marked", ""),
        marked=rec.get("marked", ""), note=rec.get("note", ""),
        author=rec.get("author", "") or "a reviewer", where=rec.get("where", ""),
    )
    cid = chat.next_id(log)
    chat.append(log, {
        "id": cid, "kind": "comment", "block": block.id, "file": str(block.file),
        "lines": [block.line_start, block.line_end],
        "quote": block.source_text[:120], "body": body_of(mark),
        "author": mark.author, "from": import_id,
    })
    chat.append(log, {"id": import_id, "kind": "import", "state": "placed",
                      "comment": cid, "block": block.id})
    return {"id": import_id, "block": block.id, "comment": cid, "author": mark.author}


# --------------------------------------------------------------- internals


def _import_records(log: Path) -> tuple[dict[str, dict], dict[str, dict]]:
    """The import log, folded: base records by id, and the last state for each.

    A base record is the one carrying the fingerprint. Everything else with the
    same id is a later state, which is how an append-only log expresses a change
    without rewriting anything.
    """
    base: dict[str, dict] = {}
    state: dict[str, dict] = {}
    for rec in chat.read_records(log):
        if rec.get("kind") != "import":
            continue
        if rec.get("fp"):
            base.setdefault(rec["id"], rec)
        else:
            state[rec["id"]] = rec
    return base, state


def _next_import(log: Path) -> int:
    return len(_import_records(log)[0]) + 1


def _fingerprint(marks: list[Mark], source: str) -> list[Mark]:
    """A stable identity for each mark, so a second import is a no-op.

    The page is deliberately not in it. A referee report re-exported after the
    author rewrote section 2 carries the same marks on different pages, and
    hashing the page would read every one of them as new.
    """
    counts: dict[str, int] = {}
    out: list[Mark] = []
    for m in marks:
        raw = "\x1f".join([source, m.kind, m.author, norm(m.marked), norm(m.note)])
        counts[raw] = counts.get(raw, 0) + 1
        digest = hashlib.sha256(f"{raw}\x1f{counts[raw]}".encode("utf-8")).hexdigest()[:16]
        out.append(Mark(**{**m.__dict__, "fingerprint": digest}))
    return out


def _item(iid: str, p: Placement, comment_id: str | None, source: str, *, already: bool = False) -> dict:
    return {
        "id": iid,
        "kind": p.mark.kind,
        "author": p.mark.author,
        "marked": _clip(p.mark.marked),
        "note": p.mark.note,
        "where": p.mark.where,
        "source": source,
        "block": None if already else p.block,
        "score": round(p.score, 3),
        "reason": "read in already" if already else p.reason,
        "candidates": [] if already else list(p.candidates),
        "comment": comment_id,
        "already": already,
    }


def _who(source: str) -> str:
    """A name for a commenter the file did not name.

    Referee PDFs routinely carry no author on the annotations, and "unknown" in
    the margin is worse than the filename, which is usually exactly who it was.
    """
    stem = Path(source).stem.replace("_", " ").replace("-", " ").strip()
    return stem or "a reviewer"


def _clip(text: str, n: int = 240) -> str:
    flat = " ".join(str(text or "").split())
    return flat[:n] + "…" if len(flat) > n else flat
