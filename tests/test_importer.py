"""Reading coauthor and reviewer markup back into the manuscript.

The design these tests hold is one sentence: **anchor by the text that was
marked, never by the page it was marked on.** A page number is meaningless the
moment the author rewrites anything above it, whereas the sentence a reviewer
highlighted is findable for as long as it survives. So the tests below move
paragraphs around, renumber pages, and expect every anchor to hold.

The second half of the design is what happens when that fails. **Anything that
cannot be placed confidently goes to a tray**, never onto a guessed paragraph. A
reviewer comment attached to the wrong sentence is worse than one the author has
to place by hand, because he will read it as being about that sentence. So a
sticky note with no marked text, a quote that matches five paragraphs equally
well, and a quote that matches nothing all end up in the same place: waiting.

Nothing here calls a model. The importer reads a file and appends to
`comments.jsonl`, and that is the whole of what it can do.
"""
from __future__ import annotations

import difflib
import io
import zipfile
from pathlib import Path

import pytest

from manuscriptor.server import paths
from manuscriptor.server import chat
from manuscriptor.server import importer
from manuscriptor.source.blocks import segment
from manuscriptor.source.flatten import flatten

# ---------------------------------------------------------------- a manuscript

P1 = ("The treatment raised screening rates substantially across every one of the "
      "three cohorts we were able to follow to the end of the study period.")
P2 = ("We interpret this as evidence that the contract itself, rather than the "
      "payment attached to it, is what drove the change in provider behaviour.")
P3 = ("A third paragraph, written out at length so that the neighbour context has "
      "something of its own to pick up when the section is read as a whole.")
P4 = ("Both arms were balanced at baseline on age, sex and the four comorbidities "
      "the registry records, which the appendix sets out in full.")

DOC = "\n\n".join([
    r"\documentclass{article}",
    r"\begin{document}",
    r"\section{Results}",
    P1,
    P2,
    P3,
    r"\section{Robustness}",
    P4,
    r"\end{document}",
]) + "\n"


def manuscript(tmp_path: Path, body: str = DOC):
    """A directory with a main.tex, plus its segmented blocks."""
    d = tmp_path / "ms"
    d.mkdir(parents=True, exist_ok=True)
    (d / "main.tex").write_text(body, encoding="utf-8")
    return d, segment(flatten(d / "main.tex"))


def block_with(blocks, needle: str) -> str:
    for b in blocks:
        if needle[:40] in b.source_text:
            return b.id
    raise AssertionError(f"no block contains {needle[:40]!r}")


def records(d: Path, kind: str) -> list[dict]:
    return [r for r in chat.read_records(paths.comments(d)) if r.get("kind") == kind]


# ----------------------------------------------------------- a marked-up PDF
#
# Built here rather than shipped as a binary fixture, so what is being tested is
# visible and a reviewer can see exactly which text carries which annotation. It
# is a real PDF: pdfannots renders it through pdfminer and recovers the marked
# text from the quadpoints, which is the same path a reviewer's Acrobat file
# takes.

FONT_SIZE = 11.0
LEADING = 16.0
LEFT = 72.0
TOP = 760.0


def _esc(s: str) -> str:
    return s.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def line_quad(i: int):
    """The rectangle covering line `i` of a page, whole-line."""
    base = TOP - LEADING * i
    return (LEFT - 2.0, base - 3.0, LEFT + 460.0, base + 10.0)


def pdf(pages: list[list[str]], annots: list[dict]) -> bytes:
    objs: list[bytes] = []

    def add(body: bytes) -> int:
        objs.append(body)
        return len(objs)

    font = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    pages_id = add(b"")
    page_ids: list[int] = []

    for pno, lines in enumerate(pages):
        stream = ["BT", f"/F1 {FONT_SIZE} Tf", f"{LEADING} TL", f"1 0 0 1 {LEFT} {TOP} Tm"]
        for line in lines:
            stream.append(f"({_esc(line)}) Tj T*")
        stream.append("ET")
        content = "\n".join(stream).encode("latin-1")
        cid = add(b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream")

        aids = []
        for a in [x for x in annots if x["page"] == pno]:
            quads: list[float] = []
            for (x0, y0, x1, y1) in a.get("quads", []):
                quads += [x0, y1, x1, y1, x0, y0, x1, y0]
            parts = [
                b"<< /Type /Annot /Subtype /" + a["subtype"].encode(),
                b"/Rect [ %s ]" % " ".join(f"{v:.2f}" for v in a["rect"]).encode(),
                b"/Contents (%s)" % _esc(a.get("contents", "")).encode("latin-1"),
                b"/T (%s)" % _esc(a.get("author", "")).encode("latin-1"),
            ]
            if quads:
                parts.append(b"/QuadPoints [ %s ]" % " ".join(f"{v:.2f}" for v in quads).encode())
            parts.append(b">>")
            aids.append(add(b" ".join(parts)))

        annot_ref = b"/Annots [ %s ]" % b" ".join(b"%d 0 R" % i for i in aids) if aids else b""
        page_ids.append(add(
            b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 %d 0 R >> >> /Contents %d 0 R %s >>"
            % (pages_id, font, cid, annot_ref)))

    objs[pages_id - 1] = (b"<< /Type /Pages /Count %d /Kids [ %s ] >>"
                          % (len(page_ids), b" ".join(b"%d 0 R" % i for i in page_ids)))
    root = add(b"<< /Type /Catalog /Pages %d 0 R >>" % pages_id)

    out = io.BytesIO()
    out.write(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for n, body in enumerate(objs, start=1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n" % n + body + b"\nendobj\n")
    xref = out.tell()
    out.write(b"xref\n0 %d\n" % (len(objs) + 1))
    out.write(b"0000000000 65535 f \n")
    for off in offsets:
        out.write(b"%010d 00000 n \n" % off)
    out.write(b"trailer\n<< /Size %d /Root %d 0 R >>\nstartxref\n%d\n%%%%EOF\n"
              % (len(objs) + 1, root, xref))
    return out.getvalue()


def wrap(text: str, width: int = 66) -> list[str]:
    """Break a paragraph into PDF lines the way a typesetter would."""
    words, lines, cur = text.split(), [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > width:
            lines.append(cur)
            cur = w
        else:
            cur = (cur + " " + w).strip()
    if cur:
        lines.append(cur)
    return lines


def marked_pdf(paragraphs, marks) -> bytes:
    """Lay paragraphs out one per page, then annotate whole lines by index."""
    pages = [wrap(p) for p in paragraphs]
    annots = []
    for m in marks:
        quads = [line_quad(i) for i in m.get("lines", [])]
        rect = quads[0] if quads else (500.0, 700.0, 512.0, 712.0)
        annots.append({
            "page": m["page"], "subtype": m["subtype"], "rect": rect, "quads": quads,
            "contents": m.get("contents", ""), "author": m.get("author", ""),
        })
    return pdf(pages, annots)


# ------------------------------------------------------ a .docx with markup

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _runs(text: str) -> str:
    return f'<w:r><w:t xml:space="preserve">{text}</w:t></w:r>'


def docx(paragraphs: list[str], comments: list[dict]) -> bytes:
    """paragraphs: raw XML fragments for <w:p> bodies. comments: comments.xml entries."""
    body = "".join(f"<w:p>{p}</w:p>" for p in paragraphs)
    document = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                f'<w:document xmlns:w="{W}"><w:body>{body}</w:body></w:document>')
    cx = "".join(
        f'<w:comment w:id="{c["id"]}" w:author="{c["author"]}" w:date="2026-07-22T00:00:00Z">'
        f'<w:p>{_runs(c["text"])}</w:p></w:comment>'
        for c in comments)
    comments_xml = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                    f'<w:comments xmlns:w="{W}">{cx}</w:comments>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                   '<Default Extension="xml" ContentType="application/xml"/>'
                   '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                   '</Types>')
        z.writestr("_rels/.rels",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="rId1" Target="word/document.xml" '
                   'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"/>'
                   '</Relationships>')
        z.writestr("word/document.xml", document)
        z.writestr("word/comments.xml", comments_xml)
    return buf.getvalue()


# ================================================================== the tests
#
# 1. the anchoring promise


def test_a_highlight_lands_on_the_paragraph_it_marks(tmp_path):
    d, blocks = manuscript(tmp_path)
    data = marked_pdf([P1, P2, P3], [
        {"page": 1, "lines": [0, 1, 2], "subtype": "Highlight",
         "contents": "this overstates what the design can show", "author": "Reviewer 2"},
    ])

    report = importer.ingest(data, "referee-report.pdf", blocks=blocks, log=paths.comments(d))

    assert report["anchored"] == 1
    assert report["unplaced"] == 0
    assert report["file"] == "referee-report.pdf"
    assert report["items"][0]["block"] == block_with(blocks, P2)


def test_the_page_number_is_not_what_places_it(tmp_path):
    """The mark sits on page 3; its text is the manuscript's FIRST paragraph.

    A page-based importer would drop it on the third paragraph and be wrong in a
    way nothing on the page would reveal.
    """
    d, blocks = manuscript(tmp_path)
    data = marked_pdf(["filler one", "filler two", P1], [
        {"page": 2, "lines": [0, 1, 2], "subtype": "Highlight",
         "contents": "say how many were lost to follow-up", "author": "Reviewer 1"},
    ])

    report = importer.ingest(data, "r1.pdf", blocks=blocks, log=paths.comments(d))

    assert report["anchored"] == 1
    assert report["items"][0]["block"] == block_with(blocks, P1)


def test_an_anchor_survives_the_manuscript_being_rewritten_above_it(tmp_path):
    """Two paragraphs inserted at the top, so every page number below moves.

    This is the case the whole design exists for. The reviewer's PDF was
    compiled before the rewrite and still has to land on the right paragraph.
    """
    d, blocks = manuscript(tmp_path)
    data = marked_pdf([P1, P2, P3], [
        {"page": 1, "lines": [0, 1, 2], "subtype": "Highlight",
         "contents": "not what the coefficient shows", "author": "Reviewer 2"},
    ])

    grown = DOC.replace(
        r"\section{Results}",
        "\\section{Introduction}\n\nAn opening paragraph that did not exist when the referee "
        "read this, pushing everything below it onto a later page.\n\n"
        "A second new paragraph, for good measure, so the shift is more than one line.\n\n"
        r"\section{Results}")
    d2, moved = manuscript(tmp_path / "after", grown)

    report = importer.ingest(data, "r2.pdf", blocks=moved, log=paths.comments(d2))

    assert report["anchored"] == 1
    assert report["items"][0]["block"] == block_with(moved, P2)


def test_a_mark_on_a_computed_value_finds_the_paragraph_that_inputs_it(tmp_path):
    """The referee read `0.096`; the source says `\\input{exhibits/pval}`.

    Matching on the unflattened source would miss every sentence carrying a
    result, which is the kind referees mark most.
    """
    d = tmp_path / "ms"
    (d / "exhibits").mkdir(parents=True)
    (d / "exhibits" / "pval.tex").write_text("0.096", encoding="utf-8")
    body = "\n\n".join([
        r"\documentclass{article}", r"\begin{document}",
        r"The pooled specification rejects the null at p=\input{exhibits/pval}, which we "
        r"read as support for the mechanism set out above.",
        r"\end{document}"]) + "\n"
    (d / "main.tex").write_text(body, encoding="utf-8")
    blocks = segment(flatten(d / "main.tex"))

    data = marked_pdf(["The pooled specification rejects the null at p=0.096, which we "
                       "read as support for the mechanism set out above."],
                      [{"page": 0, "lines": [0, 1], "subtype": "Highlight",
                        "contents": "report the test statistic too", "author": "Reviewer 1"}])

    report = importer.ingest(data, "r1.pdf", blocks=blocks, log=paths.comments(d))

    assert report["anchored"] == 1
    assert report["items"][0]["score"] == 1.0


def test_a_re_exported_pdf_with_new_pagination_is_the_same_markup(tmp_path):
    """The author rewrote section 2 and the referee's file was re-exported. Every
    page number moved and not one mark is new."""
    d, blocks = manuscript(tmp_path)
    log = paths.comments(d)
    mark = {"lines": [0, 1], "subtype": "Highlight",
            "contents": "this overstates it", "author": "Reviewer 2"}

    first = marked_pdf([P2], [dict(mark, page=0)])
    later = marked_pdf(["filler", "more filler", P2], [dict(mark, page=2)])

    importer.ingest(first, "r2.pdf", blocks=blocks, log=log)
    again = importer.ingest(later, "r2.pdf", blocks=blocks, log=log)

    assert again["already"] == 1
    assert again["anchored"] == 0
    assert len(records(d, "comment")) == 1


def test_a_strikeout_carries_the_struck_text_and_is_placed_by_it(tmp_path):
    d, blocks = manuscript(tmp_path)
    data = marked_pdf([P4], [
        {"page": 0, "lines": [0, 1], "subtype": "StrikeOut", "author": "Reviewer 1"},
    ])

    report = importer.ingest(data, "r1.pdf", blocks=blocks, log=paths.comments(d))

    assert report["anchored"] == 1
    item = report["items"][0]
    assert item["block"] == block_with(blocks, P4)
    assert item["kind"] == "strikeout"
    # No note was typed, so the body has to say what the mark was, or the drain
    # is handed an empty instruction.
    body = records(d, "comment")[0]["body"]
    assert "struck" in body.lower()
    assert "balanced at baseline" in body


# 2. the tray


def test_a_sticky_note_with_no_marked_text_goes_to_the_tray(tmp_path):
    """It sits on a page and marks nothing. The page is not an anchor."""
    d, blocks = manuscript(tmp_path)
    data = marked_pdf([P1, P2], [
        {"page": 1, "lines": [], "subtype": "Text",
         "contents": "the whole section needs restructuring", "author": "Reviewer 2"},
    ])

    report = importer.ingest(data, "r2.pdf", blocks=blocks, log=paths.comments(d))

    assert report["anchored"] == 0
    assert report["unplaced"] == 1
    assert report["items"][0]["block"] is None
    assert not records(d, "comment")


def test_text_that_matches_nothing_goes_to_the_tray(tmp_path):
    d, blocks = manuscript(tmp_path)
    data = marked_pdf(
        ["Wholly unrelated prose about the migratory habits of arctic terns, which "
         "appears nowhere in this manuscript at all."],
        [{"page": 0, "lines": [0, 1], "subtype": "Highlight",
          "contents": "check this", "author": "Reviewer 3"}])

    report = importer.ingest(data, "r3.pdf", blocks=blocks, log=paths.comments(d))

    assert report["unplaced"] == 1
    assert report["items"][0]["block"] is None
    assert report["items"][0]["reason"]


def test_a_mark_matching_several_paragraphs_equally_goes_to_the_tray(tmp_path):
    """Three paragraphs open identically. Picking one would be a coin toss."""
    same = "The estimate is reported in the table below, with standard errors clustered."
    body = "\n\n".join([r"\documentclass{article}", r"\begin{document}",
                        same + " One.", same + " Two.", same + " Three.",
                        r"\end{document}"]) + "\n"
    d, blocks = manuscript(tmp_path, body)
    data = marked_pdf([same], [
        {"page": 0, "lines": [0, 1], "subtype": "Highlight",
         "contents": "which table?", "author": "Reviewer 2"}])

    report = importer.ingest(data, "r2.pdf", blocks=blocks, log=paths.comments(d))

    assert report["unplaced"] == 1
    assert report["items"][0]["block"] is None
    assert "more than one" in report["items"][0]["reason"].lower()


def test_the_tray_lets_him_place_it_by_hand(tmp_path):
    d, blocks = manuscript(tmp_path)
    log = paths.comments(d)
    data = marked_pdf([P1, P2], [
        {"page": 1, "lines": [], "subtype": "Text",
         "contents": "restructure the section", "author": "Reviewer 2"}])
    importer.ingest(data, "r2.pdf", blocks=blocks, log=log)

    waiting = importer.tray(log)
    assert len(waiting) == 1
    target = block_with(blocks, P3)

    placed = importer.place_mark(log, waiting[0]["id"], target, blocks=blocks)

    assert placed["block"] == target
    assert importer.tray(log) == []
    comments = records(d, "comment")
    assert len(comments) == 1
    assert comments[0]["block"] == target
    assert comments[0]["author"] == "Reviewer 2"
    assert "restructure the section" in comments[0]["body"]


def test_a_tray_item_offers_the_paragraphs_it_nearly_matched(tmp_path):
    """Placing by hand from a list beats hunting for the paragraph."""
    d, blocks = manuscript(tmp_path)
    partial = ("We interpret this as evidence that the contract itself is what changed "
               "how providers behave, though the mechanism is not pinned down here.")
    data = marked_pdf([partial], [
        {"page": 0, "lines": [0, 1], "subtype": "Highlight",
         "contents": "mechanism?", "author": "Reviewer 2"}])

    report = importer.ingest(data, "r2.pdf", blocks=blocks, log=paths.comments(d))
    item = report["items"][0]

    assert item["block"] is None
    assert item["candidates"], "a near miss must offer somewhere to put it"
    assert item["candidates"][0]["block"] == block_with(blocks, P2)


# 3. imported comments are ordinary comments


def test_an_imported_comment_queues_like_any_other(tmp_path):
    d, blocks = manuscript(tmp_path)
    log = paths.comments(d)
    data = marked_pdf([P2], [
        {"page": 0, "lines": [0, 1], "subtype": "Highlight",
         "contents": "this overstates it", "author": "Reviewer 2"}])

    importer.ingest(data, "r2.pdf", blocks=blocks, log=log)

    waiting = chat.pending(log)
    assert len(waiting) == 1
    assert waiting[0].author == "Reviewer 2"
    assert waiting[0].body == "this overstates it"
    assert waiting[0].state == "queued"
    assert waiting[0].block == block_with(blocks, P2)
    # And it shows in the margin exactly like one of his own.
    margin = chat.by_block(log)
    assert margin[block_with(blocks, P2)][0]["who"] == "Reviewer 2"


def test_an_imported_comment_survives_its_block_being_edited(tmp_path):
    """It carries the block's own SOURCE as its quote, like every other comment,
    so `reanchor_chats` re-finds it after the id changes underneath.

    The paragraph carries a citation and the mark starts mid-sentence, which is
    what makes this a real test rather than a coincidence: the referee's quote is
    typeset text containing "(Nishtar 2018)", and that string appears nowhere in
    the LaTeX. Store it as the quote and re-anchoring silently stops working for
    exactly the comments most likely to cause an edit.
    """
    from manuscriptor.server import build as build_mod

    cited = (r"Provider behaviour changed sharply once the contract was signed "
             r"\citep{nishtar2018time}, and the effect persisted for at least two "
             r"further years of follow-up in both arms.")
    typeset = ("Provider behaviour changed sharply once the contract was signed "
               "(Nishtar 2018), and the effect persisted for at least two "
               "further years of follow-up in both arms.")
    d, blocks = manuscript(tmp_path, DOC.replace(P2, cited))
    log = paths.comments(d)

    data = marked_pdf([typeset], [
        {"page": 0, "lines": [1, 2], "subtype": "Highlight",
         "contents": "tighten this", "author": "Reviewer 2"}])
    report = importer.ingest(data, "r2.pdf", blocks=blocks, log=log)
    assert report["anchored"] == 1
    was = block_with(blocks, cited)

    edited = DOC.replace(P2, cited + " We return to this in the discussion.")
    _, after = manuscript(tmp_path / "edited", edited)
    now = block_with(after, cited)
    assert now != was, "the edit has to rename the block or this proves nothing"

    moved = build_mod.reanchor_chats(chat.by_block(log), after, chat.read_chats(log))
    assert now in moved
    assert moved[now][0]["who"] == "Reviewer 2"


# -------------------------------- re-anchoring must not merge two chats
#
# Reported 2026-07-27: the Chat tab on one generated table showed messages that
# had been written against a different table in the same file. Every LaTeX float
# opens with the same bytes, so the truncated keys `match_by_quote` falls back to
# are boilerplate: six of dsp-bias's figure comments shared the 40-character key
# `\begin{figure}[h!] \centering \includegr`, and three of its table comments
# shared `\begin{table}[h!] \centering \caption{De`. A tie resolved by document
# order stacks every exhibit's conversation onto the first exhibit.
#
# A chat belongs to one block. Where that block cannot be identified, the chat
# waits -- the same rule the tray is built on, applied to re-anchoring.

TWO_TABLES = "\n\n".join([
    r"\documentclass{article}",
    r"\begin{document}",
    r"\section{Results}",
    P1,
    "\n".join([
        r"\begin{table}[h!]",
        r"\centering",
        r"\caption{Demographic variation in conversation responses. Panel A: scripted.}",
        r"\begin{tabular}{lcc}\toprule A & 1 & 2 \\ \bottomrule\end{tabular}",
        r"\end{table}",
    ]),
    "\n".join([
        r"\begin{table}[h!]",
        r"\centering",
        r"\caption{Demographic variation in case generation. Panel B: all 16 characteristics.}",
        r"\begin{tabular}{lcc}\toprule B & 3 & 4 \\ \bottomrule\end{tabular}",
        r"\end{table}",
    ]),
    r"\end{document}",
]) + "\n"


def _tables(tmp_path: Path):
    d, blocks = manuscript(tmp_path, TWO_TABLES)
    tabs = [b for b in blocks if r"\begin{table}" in b.source_text]
    assert len(tabs) == 2, "the fixture must give two separately addressable tables"
    return d, blocks, tabs


def test_a_chat_is_not_moved_onto_a_different_exhibit_with_the_same_opening(tmp_path):
    """The second table's chat must never resolve to the first."""
    from manuscriptor.server import build as build_mod

    _, blocks, tabs = _tables(tmp_path)
    first, second = tabs
    assert build_mod.flatten_ws(first.source_text)[:40] == \
        build_mod.flatten_ws(second.source_text)[:40], \
        "precondition: the fallback key really is shared, or this proves nothing"

    assert build_mod.match_by_quote(second.source_text[:120], blocks) == second.id
    assert build_mod.match_by_quote(first.source_text[:120], blocks) == first.id


def test_an_ambiguous_quote_leaves_a_chat_unanchored_rather_than_guessing(tmp_path):
    """Boilerplate alone identifies nothing, and a guess reads to the author as a
    statement about the paragraph it landed on."""
    from manuscriptor.server import build as build_mod

    _, blocks, _ = _tables(tmp_path)
    assert build_mod.match_by_quote(r"\begin{table}[h!] \centering \caption{De", blocks) is None


def test_a_reply_is_placed_by_its_own_comment_never_a_neighbours(tmp_path):
    """A reply carries no quote of its own -- it belongs wherever its comment
    belongs. Resolving it from whatever quote happened to be first in the group
    is how an agent's answer about Panel A appeared under Panel B."""
    from manuscriptor.server import build as build_mod

    _, blocks, tabs = _tables(tmp_path)
    first, second = tabs
    # Both chats were written against ids that no longer exist, so they share one
    # absent key -- exactly the state the log reaches after a table is rebuilt.
    by_block = {"": [
        {"id": "c-0001", "who": "bb", "body": "about the first", "ts": "1", "state": "done"},
        {"id": "c-0001#r1", "who": "claude", "body": "answer about the first",
         "ts": "2", "state": None},
        {"id": "c-0002", "who": "bb", "body": "about the second", "ts": "3", "state": "queued"},
        {"id": "c-0002#r1", "who": "claude", "body": "answer about the second",
         "ts": "4", "state": None},
    ]}

    class Q:
        def __init__(self, cid, quote):
            self.id, self.quote, self.file = cid, quote, ""

    chats = [Q("c-0001", first.source_text[:120]), Q("c-0002", second.source_text[:120])]
    moved = build_mod.reanchor_chats(by_block, blocks, chats)

    assert [m["id"] for m in moved.get(first.id, [])] == ["c-0001", "c-0001#r1"]
    assert [m["id"] for m in moved.get(second.id, [])] == ["c-0002", "c-0002#r1"]


SHARED = (r"\newcolumntype{m}[1]{>{\centering\arraybackslash}p{#1}}" "\n"
          r"\setlength{\LTleft}{-20cm plus -1fill}" "\n"
          r"\setlength{\LTright}{\LTleft}" "\n")

TWIN_TABLES = "\n\n".join([
    r"\documentclass{article}", r"\begin{document}", r"\section{Results}", P1,
    SHARED + r"\begin{tabular}{mm}\toprule Screening & 1 \\ \bottomrule\end{tabular}",
    SHARED + r"\begin{tabular}{mm}\toprule Mortality & 2 \\ \bottomrule\end{tabular}",
    r"\end{document}",
]) + "\n"


def test_a_recorded_quote_identifies_the_block_it_came_from(tmp_path):
    """Two blocks opening with the same boilerplate preamble.

    Measured on estonia-ecm: 30 of 384 blocks have a 120-character opening that
    appears in another block as well, one longtable preamble run matching eleven
    others. A comment on any of them records a quote that does not say which
    block it means, so it can never be re-found -- the failure is upstream of the
    matcher, in what gets written to the log.
    """
    from manuscriptor.server import build as build_mod

    _, blocks = manuscript(tmp_path, TWIN_TABLES)
    twins = [b for b in blocks if r"\newcolumntype" in b.source_text]
    assert len(twins) == 2, "the fixture must give two blocks sharing an opening"

    flat = [build_mod.flatten_ws(b.source_text) for b in blocks]
    fixed = build_mod.flatten_ws(twins[0].source_text)[:120]
    assert sum(fixed in t for t in flat) > 1, \
        "precondition: a fixed 120 characters really is ambiguous here"

    for twin in twins:
        q = build_mod.quote_for(twin, blocks)
        assert build_mod.match_by_quote(q, blocks) == twin.id
        assert twin.source_text.startswith(q), "still a prefix, which stage one needs"
        assert len(q) <= build_mod.QUOTE_MAX


def test_two_files_opening_with_identical_bytes_are_told_apart_by_file(tmp_path):
    """estonia-ecm opens twelve table files with the same 113-byte
    `\\newcolumntype` preamble run, byte for byte. No quote can separate them at
    any length, and growing one is not the answer -- the comment record already
    carries the file it was written in, and that is what distinguishes them."""
    from manuscriptor.server import build as build_mod

    d = tmp_path / "ms"
    d.mkdir(parents=True, exist_ok=True)
    twin = SHARED + r"\begin{tabular}{mm}\toprule A & 1 \\ \bottomrule\end{tabular}" + "\n"
    (d / "t_one.tex").write_text(twin, encoding="utf-8")
    (d / "t_two.tex").write_text(twin, encoding="utf-8")
    (d / "main.tex").write_text("\n\n".join([
        r"\documentclass{article}", r"\begin{document}", P1,
        r"\input{t_one}", r"\input{t_two}", r"\end{document}"]) + "\n", encoding="utf-8")
    doc = flatten(d / "main.tex")
    blocks = segment(doc)

    homes = {}
    for b in blocks:
        if r"\newcolumntype" in b.source_text:
            homes[Path(b.file).name] = b
    assert set(homes) == {"t_one.tex", "t_two.tex"}, "the fixture must span two files"
    a, b2 = homes["t_one.tex"], homes["t_two.tex"]
    assert a.source_text == b2.source_text, "precondition: identical bytes"

    q = build_mod.quote_for(a, blocks)
    assert build_mod.match_by_quote(q, blocks) is None, "content alone cannot decide"
    assert build_mod.match_by_quote(q, blocks, file=str(a.file)) == a.id
    assert build_mod.match_by_quote(q, blocks, file=str(b2.file)) == b2.id
    # And by basename, so moving the manuscript on disk orphans nothing.
    assert build_mod.match_by_quote(q, blocks, file="elsewhere/t_two.tex") == b2.id


def test_a_quote_is_not_grown_past_what_it_needs(tmp_path):
    """Ordinary prose is distinctive from its first words, and a quote that
    swallowed the whole paragraph would break on any edit inside it."""
    from manuscriptor.server import build as build_mod

    _, blocks = manuscript(tmp_path)
    target = block_with(blocks, P2)
    blk = next(b for b in blocks if b.id == target)
    assert len(build_mod.quote_for(blk, blocks)) == min(len(blk.source_text), 120)


def test_a_block_with_nothing_distinctive_is_not_grown_forever(tmp_path):
    """`\\clearpage` cannot be made identifiable by taking more of it."""
    from manuscriptor.server import build as build_mod

    body = "\n\n".join([
        r"\documentclass{article}", r"\begin{document}", P1,
        r"\clearpage", P2, r"\clearpage", P3, r"\end{document}"]) + "\n"
    _, blocks = manuscript(tmp_path, body)
    breaks = [b for b in blocks if b.source_text.strip() == r"\clearpage"]
    assert len(breaks) == 2, "the fixture must give two identical break blocks"
    q = build_mod.quote_for(breaks[0], blocks)
    assert q.strip() == r"\clearpage", "returned as-is, not grown into its neighbour"


def _figure(spec: str, asset: str, caption: str) -> str:
    return "\n".join([
        rf"\begin{{figure}}{spec}",
        r"\centering",
        rf"\includegraphics[width=\textwidth]{{outputs/{asset}}}",
        rf"\caption{{{caption}}}",
        r"\end{figure}",
    ])


COEF = _figure("[h!]", "fig3_coef_plot.pdf",
               "Demographic effects by measurement channel. Points are OLS coefficients.")
ITEMS = _figure("[!htb]", "fig2_gender_items.pdf",
                "Nine risk-factor items, men against women, in both role-play channels.")

TWO_FIGURES = "\n\n".join([
    r"\documentclass{article}", r"\begin{document}", r"\section{Results}", P1,
    COEF, ITEMS, r"\end{document}",
]) + "\n"


def _figures(tmp_path: Path):
    d, blocks = manuscript(tmp_path, TWO_FIGURES)
    figs = {}
    for b in blocks:
        if "fig3_coef_plot" in b.source_text:
            figs["coef"] = b
        elif "fig2_gender_items" in b.source_text:
            figs["items"] = b
    assert len(figs) == 2, "the fixture must give two separately addressable figures"
    return blocks, figs


def test_a_chat_on_a_deleted_exhibit_does_not_land_on_a_surviving_one(tmp_path):
    """The figure a comment was written against was replaced outright.

    Its quote's first 60 bytes are `\\begin{figure}[h!] \\centering
    \\includegraphics[width=\\textwi` -- identical to the surviving figure's,
    because the asset path that says WHICH figure this is falls past every
    truncation. The key is unique here (the other float opens `[!htb]`) and still
    wrong, which is how a comment on the retired six-panel figure appeared on the
    coefficient plot. There is no block it belongs to, so it waits.
    """
    from manuscriptor.server import build as build_mod

    blocks, figs = _figures(tmp_path)
    retired = _figure("[h!]", "fig3_heatmap.pdf",
                      "One demographic factor per panel, all three measurement channels.")
    quote = build_mod.flatten_ws(retired)[:120]

    # Precondition: the truncated key really does single out the wrong figure.
    flat = [(b.id, build_mod.flatten_ws(b.source_text)) for b in blocks]
    head_hits = [bid for bid, t in flat if t.startswith(quote[:60])]
    assert head_hits == [figs["coef"].id], "or this test proves nothing"

    assert build_mod.match_by_quote(quote, blocks) is None


def test_a_renamed_asset_still_finds_its_own_exhibit(tmp_path):
    """The other half. Renaming a figure's file changes about one character in
    120, and the chat must follow it rather than be cast adrift."""
    from manuscriptor.server import build as build_mod

    blocks, figs = _figures(tmp_path)
    old_name = COEF.replace("fig3_coef_plot.pdf", "fig2_coef_plot.pdf")
    quote = build_mod.flatten_ws(old_name)[:120]
    assert build_mod.match_by_quote(quote, blocks) == figs["coef"].id


def test_prose_is_not_subject_to_the_exhibit_rule(tmp_path):
    """A typeset quote carries no asset path, and matching it on a distinctive
    40-character phrase is how an imported reviewer note anchors at all."""
    from manuscriptor.server import build as build_mod

    d, blocks = manuscript(tmp_path)
    target = block_with(blocks, P2)
    assert build_mod.match_by_quote(P2[:44], blocks) == target


def test_a_verbatim_match_is_not_lost_behind_paragraphs_that_merely_resemble_it(tmp_path):
    """Scoring every paragraph properly is quadratic, so a cheap pass picks who
    gets scored, ranking by shared vocabulary.

    A paragraph built from the SAME WORDS in a different order ties with the one
    that contains the sentence verbatim, so with enough boilerplate around it the
    real paragraph sorts out of the window. The precondition is asserted below
    rather than assumed, or this test could quietly stop testing anything.
    """
    quoted = ("the estimate for the pooled sample is reported in the table below with the "
              "standard errors clustered at the level of the facility")
    words = quoted.split()
    # Rotated, never by a whole turn: a decoy that came back to the original
    # order would BE the quote, and the test would be measuring a tie.
    decoys = []
    for i in range(200):
        r = 1 + i % (len(words) - 1)
        decoys.append(" ".join(words[r:] + words[:r]) + f" in wave {i}.")
    real = "We note that " + quoted + ", and we return to this in the discussion."
    body = "\n\n".join([r"\documentclass{article}", r"\begin{document}"]
                       + decoys + [real, r"\end{document}"]) + "\n"
    d, blocks = manuscript(tmp_path, body)

    # The precondition: far more than PREFILTER blocks share every word of the
    # quote and sort ahead of the real one on the cheap pass alone.
    texts = importer.block_texts(blocks)
    target = block_with(blocks, real)
    ahead = sum(1 for bid, t in texts.items()
                if bid < target and set(importer.norm(quoted).split()) <= set(t.split()))
    assert ahead > importer.PREFILTER, f"only {ahead} decoys outrank it; the test proves nothing"

    data = marked_pdf([quoted], [{"page": 0, "lines": [0, 1, 2], "subtype": "Highlight",
                                  "contents": "which sample?", "author": "Reviewer 1"}])
    report = importer.ingest(data, "r1.pdf", blocks=blocks, log=paths.comments(d))

    assert report["items"][0]["candidates"], "the verbatim paragraph must at least be offered"
    assert report["items"][0]["candidates"][0]["block"] == target


def test_an_accent_and_a_ligature_normalize_to_the_same_letters():
    """The contract `norm` is holding, stated directly.

    A PDF extractor returns one `ﬁ` glyph and a precomposed `é`; the source
    writes `fi` and `\\'e`. Compared as characters those differ, and two
    different passes are what make them agree: case folding splits the ligature,
    compatibility decomposition separates the accent.
    """
    assert importer.norm("Montréal") == importer.norm("Montreal")
    assert importer.norm("ﬁve conﬁrmed ﬁndings") == importer.norm("five confirmed findings")
    assert importer.norm("naïve café") == importer.norm("naive cafe")


def test_an_accent_and_a_ligature_are_the_same_letters_on_both_sides(tmp_path):
    """The source writes `\\'e` and a PDF extractor hands back a precomposed `é`
    and a single `ﬁ` glyph. Compared naively those are different characters and a
    paragraph loses its own comment."""
    tex = (r"The trial was fielded across five sites in Montr\'eal, and the findings "
           r"were confirmed in a second wave the following year.")
    read_back = ("The trial was ﬁelded across ﬁve sites in Montréal, and the "
                 "ﬁndings were conﬁrmed in a second wave the following year.")
    d, blocks = manuscript(tmp_path, DOC.replace(P3, tex))

    data = docx([
        ('<w:commentRangeStart w:id="1"/>' + _runs(read_back)
         + '<w:commentRangeEnd w:id="1"/><w:r><w:commentReference w:id="1"/></w:r>'),
    ], [{"id": "1", "author": "Sam Okoro", "text": "which five?"}])

    report = importer.ingest(data, "coauthor.docx", blocks=blocks, log=paths.comments(d))

    assert report["anchored"] == 1
    assert report["items"][0]["block"] == block_with(blocks, tex)


def test_the_log_is_only_ever_appended_to(tmp_path):
    d, blocks = manuscript(tmp_path)
    log = paths.comments(d)
    chat.append(log, {"id": "c-0001", "kind": "comment", "block": "b-existing",
                      "body": "mine", "author": "bb"})
    before = log.read_bytes()

    data = marked_pdf([P2], [{"page": 0, "lines": [0, 1], "subtype": "Highlight",
                              "contents": "tighten", "author": "Reviewer 2"}])
    importer.ingest(data, "r2.pdf", blocks=blocks, log=log)
    importer.place_mark  # placement writes too

    assert log.read_bytes().startswith(before)
    # And a fresh comment id, not a reuse of c-0001.
    ids = [r["id"] for r in records(d, "comment")]
    assert len(set(ids)) == len(ids)
    assert "c-0001" in ids


def test_importing_writes_the_comment_log_and_nothing_else(tmp_path):
    """It may not touch the manuscript. A reviewer's note is a request, not an
    edit, and the no-hardcoded-results rule holds because no path from a marked
    up file reaches a `.tex`."""
    d, blocks = manuscript(tmp_path)
    before = {p: p.read_bytes() for p in sorted(d.rglob("*")) if p.is_file()}

    data = marked_pdf([P2], [{"page": 0, "lines": [0, 1], "subtype": "Highlight",
                              "contents": "3.14 is the number I get", "author": "Reviewer 2"}])
    importer.ingest(data, "r2.pdf", blocks=blocks, log=paths.comments(d))

    after = {p: p.read_bytes() for p in sorted(d.rglob("*")) if p.is_file()}
    changed = {p for p in after if before.get(p) != after[p]}
    assert changed == {paths.comments(d)}


def test_importing_the_same_file_twice_does_not_duplicate(tmp_path):
    d, blocks = manuscript(tmp_path)
    log = paths.comments(d)
    data = marked_pdf([P1, P2], [
        {"page": 0, "lines": [0, 1], "subtype": "Highlight",
         "contents": "how many dropped out?", "author": "Reviewer 1"},
        {"page": 1, "lines": [], "subtype": "Text",
         "contents": "restructure", "author": "Reviewer 1"}])

    first = importer.ingest(data, "r1.pdf", blocks=blocks, log=log)
    second = importer.ingest(data, "r1.pdf", blocks=blocks, log=log)

    assert first["anchored"] == 1 and first["unplaced"] == 1
    assert second["already"] == 2
    assert second["anchored"] == 0 and second["unplaced"] == 0
    assert len(records(d, "comment")) == 1
    assert len(importer.tray(log)) == 1


# 4. .docx


def test_a_tracked_deletion_is_read_with_its_author(tmp_path):
    d, blocks = manuscript(tmp_path)
    data = docx([
        _runs(P1),
        (_runs("We interpret this as evidence that the contract itself, rather than the ")
         + '<w:del w:id="1" w:author="Anna Lee" w:date="2026-07-22T00:00:00Z">'
           '<w:r><w:delText xml:space="preserve">payment attached to it, </w:delText></w:r></w:del>'
         + _runs("is what drove the change in provider behaviour.")),
    ], [])

    report = importer.ingest(data, "coauthor.docx", blocks=blocks, log=paths.comments(d))

    assert report["anchored"] == 1
    item = report["items"][0]
    assert item["kind"] == "deletion"
    assert item["author"] == "Anna Lee"
    assert item["block"] == block_with(blocks, P2)
    assert "payment attached to it" in records(d, "comment")[0]["body"]


def test_a_tracked_insertion_anchors_on_the_paragraph_not_on_the_new_words(tmp_path):
    """The inserted words are by definition not in the manuscript yet, so the
    only thing that can place them is the paragraph around them."""
    d, blocks = manuscript(tmp_path)
    data = docx([
        (_runs(P1[:-1] + ", ")
         + '<w:ins w:id="2" w:author="Anna Lee" w:date="2026-07-22T00:00:00Z">'
           '<w:r><w:t xml:space="preserve">though attrition was higher in the control arm</w:t></w:r></w:ins>'
         + _runs(".")),
    ], [])

    report = importer.ingest(data, "coauthor.docx", blocks=blocks, log=paths.comments(d))

    assert report["anchored"] == 1
    item = report["items"][0]
    assert item["kind"] == "insertion"
    assert item["block"] == block_with(blocks, P1)
    assert "attrition was higher" in records(d, "comment")[0]["body"]


def test_a_docx_comment_anchors_on_the_text_it_covers(tmp_path):
    d, blocks = manuscript(tmp_path)
    data = docx([
        _runs(P1),
        ('<w:commentRangeStart w:id="7"/>' + _runs(P3) + '<w:commentRangeEnd w:id="7"/>'
         '<w:r><w:commentReference w:id="7"/></w:r>'),
    ], [{"id": "7", "author": "Sam Okoro", "text": "cut this paragraph entirely"}])

    report = importer.ingest(data, "coauthor.docx", blocks=blocks, log=paths.comments(d))

    assert report["anchored"] == 1
    item = report["items"][0]
    assert item["kind"] == "comment"
    assert item["author"] == "Sam Okoro"
    assert item["block"] == block_with(blocks, P3)
    assert records(d, "comment")[0]["body"] == "cut this paragraph entirely"


def test_a_docx_comment_on_text_the_manuscript_lost_goes_to_the_tray(tmp_path):
    d, blocks = manuscript(tmp_path)
    data = docx([
        ('<w:commentRangeStart w:id="1"/>'
         + _runs("A paragraph the author has since deleted from the manuscript entirely, "
                 "about something else altogether.")
         + '<w:commentRangeEnd w:id="1"/><w:r><w:commentReference w:id="1"/></w:r>'),
    ], [{"id": "1", "author": "Sam Okoro", "text": "this contradicts section 2"}])

    report = importer.ingest(data, "coauthor.docx", blocks=blocks, log=paths.comments(d))

    assert report["unplaced"] == 1
    assert report["items"][0]["block"] is None


# 5. reporting and refusals


def test_the_report_says_what_happened_and_which_file_it_came_from(tmp_path):
    d, blocks = manuscript(tmp_path)
    data = marked_pdf([P1, P2, "arctic terns migrate a very long way indeed each year"], [
        {"page": 0, "lines": [0, 1], "subtype": "Highlight", "contents": "a", "author": "R2"},
        {"page": 1, "lines": [0, 1], "subtype": "Highlight", "contents": "b", "author": "R2"},
        {"page": 2, "lines": [0], "subtype": "Highlight", "contents": "c", "author": "R2"},
    ])

    report = importer.ingest(data, "reviewer-2.pdf", blocks=blocks, log=paths.comments(d))

    assert report["file"] == "reviewer-2.pdf"
    assert report["marks"] == 3
    assert report["anchored"] == 2
    assert report["unplaced"] == 1
    assert report["anchored"] + report["unplaced"] + report["already"] == report["marks"]
    assert all("where" in it for it in report["items"])


def test_an_unreadable_kind_of_file_is_refused_by_name(tmp_path):
    d, blocks = manuscript(tmp_path)
    with pytest.raises(importer.Unreadable) as exc:
        importer.ingest(b"nope", "notes.txt", blocks=blocks, log=paths.comments(d))
    assert ".txt" in str(exc.value)


def test_nothing_in_the_importer_calls_a_model(tmp_path):
    """The server has zero knowledge of Claude. Held in a test rather than in a
    comment, because this is the invariant the whole two-process design rests
    on."""
    src = Path(importer.__file__).read_text(encoding="utf-8")
    for forbidden in ("anthropic", "claude", "openai", "completion", "subprocess"):
        assert forbidden not in src.lower(), f"{forbidden!r} has no business in the importer"


def test_a_mark_too_short_to_be_distinctive_is_not_guessed_at(tmp_path):
    """Two words could be anywhere. Placing them is a coin toss dressed up as a
    match."""
    d, blocks = manuscript(tmp_path)
    data = marked_pdf(["the contract"], [
        {"page": 0, "lines": [0], "subtype": "Highlight",
         "contents": "which one?", "author": "Reviewer 2"}])

    report = importer.ingest(data, "r2.pdf", blocks=blocks, log=paths.comments(d))

    assert report["unplaced"] == 1
    assert report["items"][0]["block"] is None


def test_letters_in_the_right_order_are_not_a_match():
    """The score must measure agreement, not the size of what it scores against.

    Found by importing into the reference manuscript, not by reading the code: a
    4,100-character regression table scored 0.917 against a prose sentence
    containing none of its words, by supplying the letters one and two at a
    time, and that was enough to crowd the real paragraph at 0.952 into the tray
    as ambiguous. Counting only runs a word long dropped the table to 0.571 and
    left the paragraph at 0.929.

    The fixture is that defect at its smallest: text agreeing with the quote on
    nothing longer than a letter. The first assertion is the test checking
    itself. Without it, a fixture that stopped exhibiting the problem would sail
    through the second assertion while proving nothing.
    """
    needle = importer.norm(
        "changes in the utilization of care services at locations other than the ECM provider")
    scattered = " ".join(needle.replace(" ", ""))

    ungated = sum(b.size for b in difflib.SequenceMatcher(
        None, scattered, needle, autojunk=False).get_matching_blocks()) / len(needle)
    assert ungated > 0.3, "the fixture no longer exhibits the defect it exists to catch"
    assert importer.containment(scattered, needle) < 0.05

    prose = importer.norm(
        "The second and third panels investigate changes in the utilization of care services "
        "at locations other than the ECM provider, which we report separately.")
    assert importer.containment(prose, needle) > importer.STRONG


def test_latex_and_typeset_text_are_compared_on_equal_terms(tmp_path):
    """The reviewer read a PDF; the manuscript is markup. `plain` is what makes
    those two comparable, and it has to survive citations, emphasis and math."""
    tex = (r"The effect was \textbf{large} \citep{smith2020}, at "
           r"$0.41$ standard deviations~\citep{jones2019}.")
    got = importer.plain(tex)
    assert "citep" not in got and "smith2020" not in got
    assert "large" in got
    assert importer.norm("The effect was large, at 0.41 standard deviations.") in importer.norm(got)


# 6. the route
#
# The one route this feature adds. Exercised through a real aiohttp client
# rather than by calling the handler, because the parts most likely to be wrong
# are the multipart parse and the refusals, and neither of those exists until
# something speaks HTTP.


def route_case(tmp_path, *, read_only=False):
    import asyncio
    from aiohttp.test_utils import TestClient, TestServer
    from manuscriptor.server.app import Session

    d, blocks = manuscript(tmp_path)
    session = Session(d, read_only=read_only)
    return d, blocks, session, TestClient, TestServer, asyncio


def test_the_route_reads_a_file_and_places_from_the_tray(tmp_path):
    d, blocks, session, TestClient, TestServer, asyncio = route_case(tmp_path)
    from manuscriptor.server.app import make_app
    from aiohttp import FormData

    data = marked_pdf([P2, P1], [
        {"page": 0, "lines": [0, 1], "subtype": "Highlight",
         "contents": "this overstates it", "author": "Reviewer 2"},
        {"page": 1, "lines": [], "subtype": "Text",
         "contents": "restructure the section", "author": "Reviewer 2"}])

    async def go():
        async with TestClient(TestServer(make_app(session))) as client:
            form = FormData()
            form.add_field("file", data, filename="r2.pdf", content_type="application/pdf")
            report = await (await client.post("/import", data=form)).json()

            state = await (await client.post("/import", json={"action": "tray"})).json()
            waiting = state["tray"][0]["id"]
            target = block_with(blocks, P3)
            placed = await (await client.post(
                "/import", json={"action": "place", "import": waiting, "block": target})).json()
            return report, state, placed, target

    report, state, placed, target = asyncio.run(go())

    assert report["anchored"] == 1 and report["unplaced"] == 1
    assert report["file"] == "r2.pdf"
    assert len(state["tray"]) == 1
    assert placed["block"] == target
    assert placed["tray"] == []
    # It landed as an ordinary comment, attributed, queued, in the margin.
    bodies = {c.author: c.body for c in chat.pending(paths.comments(d))}
    assert bodies["Reviewer 2"]
    assert len(chat.pending(paths.comments(d))) == 2


def test_a_read_only_manuscript_refuses_to_read_markup_in(tmp_path):
    """`--read-only` means no path reaches the filesystem, and the comment log is
    part of the filesystem. A referee report is still a write."""
    d, blocks, session, TestClient, TestServer, asyncio = route_case(tmp_path, read_only=True)
    from manuscriptor.server.app import make_app
    from aiohttp import FormData

    data = marked_pdf([P2], [{"page": 0, "lines": [0, 1], "subtype": "Highlight",
                              "contents": "no", "author": "Reviewer 2"}])

    async def go():
        async with TestClient(TestServer(make_app(session))) as client:
            form = FormData()
            form.add_field("file", data, filename="r2.pdf", content_type="application/pdf")
            up = await client.post("/import", data=form)
            place = await client.post(
                "/import", json={"action": "place", "import": "i-0001", "block": "b-x"})
            tray = await client.post("/import", json={"action": "tray"})
            return up.status, place.status, tray.status, await tray.json()

    up, place, tray, body = asyncio.run(go())

    assert up == 403 and place == 403
    assert tray == 200 and body["read_only"] is True
    assert not paths.comments(d).exists()


def test_a_file_the_importer_cannot_read_is_refused_over_the_route(tmp_path):
    d, blocks, session, TestClient, TestServer, asyncio = route_case(tmp_path)
    from manuscriptor.server.app import make_app
    from aiohttp import FormData

    async def go():
        async with TestClient(TestServer(make_app(session))) as client:
            form = FormData()
            form.add_field("file", b"just some notes", filename="notes.txt",
                           content_type="text/plain")
            r = await client.post("/import", data=form)
            return r.status, await r.json()

    status, body = asyncio.run(go())
    assert status == 415
    assert ".txt" in body["error"]
    assert not paths.comments(d).exists()


def test_a_mark_that_marked_nothing_is_offered_no_candidates(tmp_path):
    """The tray and the placer have to agree about what counts as an anchor.

    `place` refuses to anchor a sticky note on anything, because a note marks a
    page rather than a sentence. A tray that ranked the note's own words against
    the manuscript and offered "place on the Discussion, 59%" would contradict
    that, and would dress a coin toss as evidence. Seen in a browser doing
    exactly this before it was fixed.
    """
    d, blocks = manuscript(tmp_path)
    log = paths.comments(d)
    data = marked_pdf([P1, P2], [
        {"page": 1, "lines": [], "subtype": "Text",
         "contents": "the whole section needs restructuring, compare the 2019 trial",
         "author": "Reviewer 2"}])
    importer.ingest(data, "r2.pdf", blocks=blocks, log=log)

    waiting = importer.tray(log, blocks)
    assert len(waiting) == 1
    assert waiting[0]["candidates"] == []


def test_the_tray_re_scores_on_the_text_the_placer_matched(tmp_path):
    """A strikeout is anchored by the sentence around it, not by the two struck
    words, so the tray has to rank on the same widened text or it would offer
    a different answer than the one the importer refused."""
    same = "The estimate is reported in the table below, with standard errors clustered."
    body = "\n\n".join([r"\documentclass{article}", r"\begin{document}",
                        same + " One.", same + " Two.", same + " Three.",
                        r"\end{document}"]) + "\n"
    d, blocks = manuscript(tmp_path, body)
    log = paths.comments(d)
    data = marked_pdf([same], [{"page": 0, "lines": [0, 1], "subtype": "Highlight",
                                "contents": "which table?", "author": "Reviewer 2"}])
    report = importer.ingest(data, "r2.pdf", blocks=blocks, log=log)
    assert report["unplaced"] == 1, "this fixture has to reach the tray or it tests nothing"

    rec = next(r for r in chat.read_records(log) if r.get("kind") == "import")
    assert rec["anchor"], "the record has to carry what was matched on"

    waiting = importer.tray(log, blocks)
    assert len(waiting) == 1
    # All three paragraphs contain it word for word, so all three are offered.
    assert len(waiting[0]["candidates"]) == 3
    assert all(c["score"] == 1.0 for c in waiting[0]["candidates"])
