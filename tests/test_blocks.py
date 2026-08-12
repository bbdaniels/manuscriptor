"""M1 — segmentation, stable ids, and unflattened source reconstruction.

The tests that matter most here are the ones about `source_text`. A block's
`flat_text` is what pandoc rendered; its `source_text` is what the author edits.
qutub-india writes `p=\\input{exhibits/pval}` inline, so if the editor ever shows
the flattened `p=0.096` a save would hardcode a result into the manuscript,
which is the one thing this tool exists to prevent.
"""
from __future__ import annotations

import difflib
from pathlib import Path

import pytest

from manuscriptor.source import blocks as blocks_mod
from manuscriptor.source.blocks import Block, Include, block_id, rematch, segment
from manuscriptor.source.flatten import flatten


def write(root: Path, name: str, text: str) -> Path:
    p = root / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def doc(body: str) -> str:
    return "\\documentclass{article}\n\\begin{document}\n" + body + "\n\\end{document}\n"


def seg(tmp_path: Path, body: str, **extra: str) -> tuple[Block, ...]:
    for name, text in extra.items():
        write(tmp_path, name.replace("__", "/") + ".tex", text)
    main = write(tmp_path, "main.tex", doc(body))
    return segment(flatten(main))


# --------------------------------------------------------------- paragraphs


def test_two_paragraphs_split(tmp_path):
    blocks = seg(tmp_path, "First paragraph here.\n\nSecond paragraph here.")
    assert [b.source_text for b in blocks] == [
        "First paragraph here.",
        "Second paragraph here.",
    ]
    assert {b.kind for b in blocks} == {"paragraph"}


def test_multiple_blank_lines_are_one_boundary(tmp_path):
    blocks = seg(tmp_path, "Alpha.\n\n\n\nBeta.")
    assert [b.source_text for b in blocks] == ["Alpha.", "Beta."]


def test_preamble_and_postamble_are_not_blocks(tmp_path):
    main = write(
        tmp_path,
        "main.tex",
        "\\documentclass{article}\n\\usepackage{natbib}\n"
        "\\begin{document}\nBody text.\n\\end{document}\nTRAILING\n",
    )
    blocks = segment(flatten(main))
    assert [b.source_text for b in blocks] == ["Body text."]


# covet-india's `wlscirep` class takes the abstract as a delimited macro read
# in the PREAMBLE, so its abstract sits above `\begin{document}`. The body
# bounds cut it away entirely: no block, no id, no anchor, and a paragraph on
# the page that nothing could splice. It is one contiguous byte range in the
# root file, so it is admissible as exactly one block.

PREAMBLE_ABSTRACT = (
    "\\documentclass{wlscirep}\n"
    "\\title{A Paper}\n"
    "\\begin{abstract}\n"
    "The abstract lives above the document environment.\n"
    "\\end{abstract}\n"
    "\\begin{document}\n"
    "\\maketitle\n"
    "\n"
    "Body text.\n"
    "\\end{document}\n"
)


def test_a_preamble_abstract_is_its_own_block(tmp_path):
    main = write(tmp_path, "main.tex", PREAMBLE_ABSTRACT)
    blocks = segment(flatten(main))
    abs_blocks = [b for b in blocks if "The abstract lives above" in b.source_text]
    assert len(abs_blocks) == 1
    b = abs_blocks[0]
    assert b.source_text.startswith("\\begin{abstract}")
    assert b.source_text.endswith("\\end{abstract}")
    assert b.id


def test_a_preamble_abstract_comes_before_the_body_and_leaves_it_alone(tmp_path):
    # Written when the abstract was the only preamble region and so was
    # blocks[0]. The title is a preamble region too and is written above it, so
    # the claim being made here is about document order, not about an index.
    main = write(tmp_path, "main.tex", PREAMBLE_ABSTRACT)
    blocks = segment(flatten(main))
    assert "The abstract lives above" in blocks[1].source_text
    assert [b.source_text for b in blocks[2:]] == ["\\maketitle", "Body text."]


def test_a_preamble_abstract_block_maps_to_its_own_source_bytes(tmp_path):
    main = write(tmp_path, "main.tex", PREAMBLE_ABSTRACT)
    b = next(
        x for x in segment(flatten(main))
        if x.source_text.startswith("\\begin{abstract}")
    )
    text = main.read_text(encoding="utf-8")
    lo = text.index("\\begin{abstract}")
    assert text[lo : lo + len(b.source_text)] == b.source_text
    assert b.file == str(main) or Path(b.file).name == "main.tex"
    assert b.editable


def test_a_document_without_a_preamble_abstract_is_unchanged(tmp_path):
    # The addition must not invent a region where there is none.
    blocks = seg(tmp_path, "First paragraph here.\n\nSecond paragraph here.")
    assert [b.source_text for b in blocks] == [
        "First paragraph here.",
        "Second paragraph here.",
    ]


def test_an_in_document_abstract_is_still_one_body_block(tmp_path):
    blocks = seg(
        tmp_path,
        "\\begin{abstract}\nInside the document.\n\\end{abstract}\n\nBody text.",
    )
    assert [b.source_text for b in blocks] == [
        "\\begin{abstract}\nInside the document.\n\\end{abstract}",
        "Body text.",
    ]


# The manuscript title could not be edited in ANY of the six manuscripts. Where
# `\title{}` is written in the preamble -- covet-india main and supplement,
# estonia-ecm, dsp-bias -- it fell in no region and got no block at all, so the
# only thing the rendered <h1> could carry was the `\maketitle` block's id and
# clicking the paper's title opened an inspector on `\flushbottom\maketitle`.
# Like the preamble abstract it is one contiguous byte range in the root file,
# so it is admissible as exactly one block.


def test_a_preamble_title_is_its_own_block(tmp_path):
    main = write(tmp_path, "main.tex", PREAMBLE_ABSTRACT)
    blocks = segment(flatten(main))
    got = [b for b in blocks if b.source_text.startswith("\\title")]
    assert len(got) == 1
    assert got[0].source_text == "\\title{A Paper}"
    assert got[0].id


def test_a_preamble_title_comes_before_the_preamble_abstract(tmp_path):
    # Document order, and the title is written above the abstract on covet.
    main = write(tmp_path, "main.tex", PREAMBLE_ABSTRACT)
    blocks = segment(flatten(main))
    assert [b.source_text for b in blocks] == [
        "\\title{A Paper}",
        "\\begin{abstract}\nThe abstract lives above the document environment.\n"
        "\\end{abstract}",
        "\\maketitle",
        "Body text.",
    ]


def test_a_preamble_title_block_maps_to_its_own_source_bytes(tmp_path):
    main = write(tmp_path, "main.tex", PREAMBLE_ABSTRACT)
    b = segment(flatten(main))[0]
    text = main.read_text(encoding="utf-8")
    lo = text.index("\\title{A Paper}")
    assert text[lo : lo + len(b.source_text)] == b.source_text
    assert b.editable


def test_a_preamble_title_is_named_by_its_own_words(tmp_path):
    # `label()` tests the block's cleaned text for surviving markup, and
    # `\title{...}` is markup end to end -- so an unhandled title block reports
    # itself in the queue, the ticker and the inspector as its parent heading,
    # or as nothing at all. Visible today on estonia-qbs and qutub-india, which
    # write `\title` inside the document and so have had a block all along.
    main = write(tmp_path, "main.tex", PREAMBLE_ABSTRACT)
    b = segment(flatten(main))[0]
    assert blocks_mod.label(b) == "A Paper"


def test_a_multiline_title_with_a_forced_break_is_one_block(tmp_path):
    # estonia-ecm and covet-india's supplement both break the title with `\\`.
    src = (
        "\\documentclass{wlscirep}\n"
        "\\title{Care Closer to Home: \\\\ Neighborhood Care Teams in Marovia}\n"
        "\\begin{document}\n\\maketitle\n\nBody text.\n\\end{document}\n"
    )
    main = write(tmp_path, "main.tex", src)
    blocks = segment(flatten(main))
    assert blocks[0].source_text == (
        "\\title{Care Closer to Home: \\\\ Neighborhood Care Teams in Marovia}"
    )
    assert blocks_mod.label(blocks[0]) == "Care Closer to Home: Neighborhood Care Teams in Marovia"


def test_titlespacing_in_the_preamble_is_not_a_title(tmp_path):
    # estonia-ecm sets `\titlespacing{\section}{0pt}{6pt}{6pt}` nineteen lines
    # above its real `\title`. A prefix match would cut the wrong bytes.
    src = (
        "\\documentclass{article}\n"
        "\\titlespacing{\\section}{0pt}{6pt}{6pt}\n"
        "\\title{The Real Title}\n"
        "\\begin{document}\n\\maketitle\n\nBody text.\n\\end{document}\n"
    )
    main = write(tmp_path, "main.tex", src)
    blocks = segment(flatten(main))
    assert blocks[0].source_text == "\\title{The Real Title}"


def test_a_document_without_a_title_is_unchanged(tmp_path):
    blocks = seg(tmp_path, "First paragraph here.\n\nSecond paragraph here.")
    assert [b.source_text for b in blocks] == [
        "First paragraph here.",
        "Second paragraph here.",
    ]


def test_an_in_document_title_is_still_one_body_block(tmp_path):
    # estonia-qbs and qutub-india write `\title` BELOW `\begin{document}`, where
    # the body region already cuts it. No second block may appear.
    blocks = seg(tmp_path, "\\title{Inside the document}\n\nBody text.")
    assert [b.source_text for b in blocks] == [
        "\\title{Inside the document}",
        "Body text.",
    ]
    assert blocks_mod.label(blocks[0]) == "Inside the document"


def test_fragment_without_document_environment_is_all_body(tmp_path):
    main = write(tmp_path, "main.tex", "Just a fragment.\n\nSecond one.\n")
    blocks = segment(flatten(main))
    assert [b.source_text for b in blocks] == ["Just a fragment.", "Second one."]


def test_whitespace_and_comment_only_regions_are_skipped(tmp_path):
    blocks = seg(tmp_path, "Real text.\n\n% a comment on its own\n\n   \n\nMore text.")
    assert [b.source_text for b in blocks] == ["Real text.", "More text."]


# -------------------------------------------------------------- environments


def test_blank_line_inside_environment_does_not_split(tmp_path):
    body = "\\begin{quote}\nfirst line\n\nsecond line\n\\end{quote}"
    blocks = seg(tmp_path, body)
    assert len(blocks) == 1
    assert blocks[0].source_text == body


def test_nested_environment_blank_lines_do_not_split(tmp_path):
    body = (
        "\\begin{quote}\n\\begin{center}\na\n\nb\n\\end{center}\n\nc\n\\end{quote}"
    )
    blocks = seg(tmp_path, body)
    assert len(blocks) == 1


def test_float_environment_is_one_block(tmp_path):
    body = (
        "Lead-in paragraph.\n\n"
        "\\begin{table}[htbp]\n\\caption{A caption}\n\n"
        "\\begin{tabular}{ll}a & b\\\\\\end{tabular}\n\\end{table}\n\n"
        "Trailing paragraph."
    )
    blocks = seg(tmp_path, body)
    kinds = [b.kind for b in blocks]
    assert kinds == ["paragraph", "table", "paragraph"]
    assert blocks[1].source_text.startswith("\\begin{table}")
    assert blocks[1].source_text.endswith("\\end{table}")


def test_starred_float_is_one_block(tmp_path):
    body = "\\begin{figure*}\n\\includegraphics{x}\n\n\\caption{C}\n\\end{figure*}"
    blocks = seg(tmp_path, body)
    assert [b.kind for b in blocks] == ["figure"]


def test_float_directly_adjacent_to_prose_still_splits(tmp_path):
    body = "Prose before.\n\\begin{figure}\n\\caption{C}\n\\end{figure}\nProse after."
    blocks = seg(tmp_path, body)
    assert [b.kind for b in blocks] == ["paragraph", "figure", "paragraph"]
    assert blocks[0].source_text == "Prose before."
    assert blocks[2].source_text == "Prose after."


def test_display_math_environment_is_one_block(tmp_path):
    body = "Text.\n\n\\begin{align}\nx &= y \\\\\nz &= w\n\\end{align}\n\nMore."
    blocks = seg(tmp_path, body)
    assert [b.kind for b in blocks] == ["paragraph", "equation", "paragraph"]


def test_bracket_display_math_is_one_block(tmp_path):
    body = "Text.\n\n\\[\n  x = y\n\\]\n\nMore."
    blocks = seg(tmp_path, body)
    assert [b.kind for b in blocks] == ["paragraph", "equation", "paragraph"]
    assert blocks[1].source_text == "\\[\n  x = y\n\\]"


def test_list_items_are_separate_blocks(tmp_path):
    body = "\\begin{itemize}\n\\item first item\n\\item second item\n\\end{itemize}"
    blocks = seg(tmp_path, body)
    assert [b.kind for b in blocks] == ["list_item", "list_item"]
    assert blocks[0].source_text == "\\item first item"
    assert blocks[1].source_text == "\\item second item"


def test_list_inside_float_does_not_break_the_float(tmp_path):
    body = "\\begin{figure}\n\\begin{itemize}\n\\item a\n\\item b\n\\end{itemize}\n\\end{figure}"
    blocks = seg(tmp_path, body)
    assert [b.kind for b in blocks] == ["figure"]


def test_escaped_percent_is_not_a_comment(tmp_path):
    blocks = seg(tmp_path, "Rates rose by 5\\% overall.")
    assert blocks[0].source_text == "Rates rose by 5\\% overall."


def test_commented_out_environment_does_not_open_depth(tmp_path):
    body = "% \\begin{table}\nA real paragraph.\n\nAnother paragraph."
    blocks = seg(tmp_path, body)
    assert [b.source_text for b in blocks] == [
        "% \\begin{table}\nA real paragraph.",
        "Another paragraph.",
    ]


# ------------------------------------------------------------------ headings


def test_section_is_its_own_block_and_sets_parent_heading(tmp_path):
    body = "\\section{Background}\n\nFirst body paragraph.\n\n\\section{Method}\n\nSecond body paragraph."
    blocks = seg(tmp_path, body)
    assert [b.kind for b in blocks] == ["heading", "paragraph", "heading", "paragraph"]
    assert blocks[0].source_text == "\\section{Background}"
    assert blocks[1].parent_heading == "Background"
    assert blocks[3].parent_heading == "Method"


def test_starred_section_is_a_heading(tmp_path):
    blocks = seg(tmp_path, "\\section*{Acknowledgements}\n\nThanks to everyone.")
    assert blocks[0].kind == "heading"
    assert blocks[1].parent_heading == "Acknowledgements"


def test_subsection_nests_under_its_section(tmp_path):
    body = (
        "\\section{Results}\n\nOverview text.\n\n"
        "\\subsection{Balance}\n\nBalance text.\n\n"
        "\\section{Discussion}\n\nDiscussion text."
    )
    blocks = seg(tmp_path, body)
    by_text = {b.source_text: b for b in blocks}
    assert by_text["Overview text."].parent_heading == "Results"
    assert by_text["Balance text."].parent_heading == "Balance"
    assert by_text["\\subsection{Balance}"].parent_heading == "Results"
    assert by_text["Discussion text."].parent_heading == "Discussion"


def test_heading_before_any_section_has_no_parent(tmp_path):
    blocks = seg(tmp_path, "Front matter text.\n\n\\section{One}")
    assert blocks[0].parent_heading is None
    assert blocks[1].parent_heading is None


# ------------------------------------------------------- source vs flat text


def test_inline_input_keeps_the_directive_in_source_text(tmp_path):
    write(tmp_path, "exhibits/pval.tex", "0.096\n")
    main = write(
        tmp_path,
        "main.tex",
        doc("The effect is significant ($p=\\input{exhibits/pval}$) overall."),
    )
    (block,) = segment(flatten(main))
    assert "\\input{exhibits/pval}" in block.source_text
    assert "0.096" not in block.source_text
    assert "0.096" in block.flat_text
    assert "\\input" not in block.flat_text
    assert block.file == main
    assert block.editable is True


def test_inline_input_is_recorded_as_an_include(tmp_path):
    write(tmp_path, "exhibits/pval.tex", "0.096\n")
    main = write(
        tmp_path, "main.tex", doc("The effect is $p=\\input{exhibits/pval}$ here.")
    )
    (block,) = segment(flatten(main))
    assert len(block.includes) == 1
    inc = block.includes[0]
    assert isinstance(inc, Include)
    assert inc.directive == "\\input{exhibits/pval}"
    assert inc.target == (tmp_path / "exhibits" / "pval.tex").resolve()
    flat = flatten(main)
    assert flat.text[inc.flat_start:inc.flat_end] == "0.096\n"


def test_two_inline_inputs_in_one_paragraph(tmp_path):
    write(tmp_path, "a.tex", "1.1\n")
    write(tmp_path, "b.tex", "2.2\n")
    main = write(
        tmp_path, "main.tex", doc("From \\input{a} to \\input{b} in one sentence.")
    )
    (block,) = segment(flatten(main))
    assert block.source_text == "From \\input{a} to \\input{b} in one sentence."
    assert [i.directive for i in block.includes] == ["\\input{a}", "\\input{b}"]


def test_input_at_the_very_end_of_a_paragraph(tmp_path):
    """The common qutub-india shape. Fragment files end with a newline, so the
    paragraph's flat range stops *inside* the expansion; the directive still has
    to come back whole, and the block stays editable because everything past the
    cut is whitespace."""
    write(tmp_path, "frag.tex", "0.096\n")
    main = write(
        tmp_path, "main.tex", doc("The p-value is \\input{frag}\n\nNext paragraph.")
    )
    blocks = segment(flatten(main))
    assert blocks[0].source_text == "The p-value is \\input{frag}"
    assert blocks[0].flat_text == "The p-value is 0.096"
    assert blocks[0].editable is True
    assert [i.directive for i in blocks[0].includes] == ["\\input{frag}"]
    assert blocks[1].source_text == "Next paragraph."


def test_source_text_is_a_verbatim_slice_of_the_host_file(tmp_path):
    write(tmp_path, "frag.tex", "42\n")
    main = write(
        tmp_path,
        "main.tex",
        doc("One paragraph.\n\nA value of \\input{frag} appears here.\n\nThird."),
    )
    host = main.read_text(encoding="utf-8")
    for block in segment(flatten(main)):
        assert block.source_text in host, block.source_text


def test_flat_text_matches_the_flat_buffer(tmp_path):
    write(tmp_path, "frag.tex", "42\n")
    main = write(tmp_path, "main.tex", doc("A value of \\input{frag} appears.\n\nNext."))
    flat = flatten(main)
    for block in segment(flat):
        assert block.flat_text == flat.text[block.flat_start:block.flat_end]


def test_included_file_hosts_its_own_blocks_and_refuses_edits(tmp_path):
    write(
        tmp_path,
        "tables/t1.tex",
        "\\begin{table}\n\\caption{Balance}\n\\end{table}\n",
    )
    main = write(
        tmp_path, "main.tex", doc("Lead-in.\n\n\\input{tables/t1}\n\nTrailing.")
    )
    blocks = segment(flatten(main))
    hosted = [b for b in blocks if b.file != main]
    assert len(hosted) == 1
    assert hosted[0].file == (tmp_path / "tables" / "t1.tex").resolve()
    # segment() reports WHAT the block is and whether it can be spliced as one
    # byte range. It deliberately does not decide whether the file is machine
    # written; that needs the analysis code, which this module cannot see.
    # See tests/test_producers.py for the rule that does decide it.
    assert hosted[0].kind == "table"
    assert hosted[0].editable is True
    assert all(b.editable for b in blocks if b.file == main)


def test_paragraph_running_out_of_an_included_file_refuses_edits(tmp_path):
    """estonia-ecm writes `\\include{tables/t1}\\label{tab}` with no blank line, so
    the table file's last line and main.tex's `\\label` are one paragraph. No
    single byte range can express an edit to it, so it must not offer one."""
    write(tmp_path, "sub.tex", "Included prose ending here.\n")
    main = write(
        tmp_path, "main.tex", doc("Before.\n\n\\input{sub}\\label{tab}\n\nAfter.")
    )
    blocks = segment(flatten(main))
    spanning = next(b for b in blocks if b.file.name == "sub.tex")
    assert "Included prose ending here." in spanning.source_text
    assert "\\label{tab}" in spanning.source_text  # the tail from the parent file
    assert spanning.editable is False


def test_block_holding_only_part_of_an_include_refuses_edits(tmp_path):
    """The include's expansion straddles the paragraph break, so the block owns
    the directive but only some of what it produced. Its source text is
    `Prose before \\input{sub}`, and splicing that would replace the whole
    directive and delete `beta` from the manuscript without anyone asking."""
    write(tmp_path, "sub.tex", "alpha\n\nbeta\n")
    main = write(tmp_path, "main.tex", doc("Prose before \\input{sub} and after."))
    first = segment(flatten(main))[0]
    assert first.file == main  # hosted in the root file, and still refused
    assert first.source_text == "Prose before \\input{sub}"
    assert first.flat_text == "Prose before alpha"
    assert first.editable is False


def test_every_editable_block_is_a_verbatim_slice_of_its_own_file(tmp_path):
    """The invariant splice depends on. If it ever fails, a save writes bytes
    into the wrong file."""
    write(tmp_path, "frag.tex", "0.096\n")
    write(tmp_path, "sub.tex", "Appendix prose that runs on.\n")
    main = write(
        tmp_path,
        "main.tex",
        doc(
            "Opening paragraph.\n\n"
            "A p-value of \\input{frag} sits inline.\n\n"
            "Ending on a value: \\input{frag}\n\n"
            "\\input{sub}\\label{tab}\n\n"
            "\\input{nowhere} is missing.\n\n"
            "Closing paragraph."
        ),
    )
    blocks = segment(flatten(main))
    assert any(b.editable for b in blocks) and any(not b.editable for b in blocks)
    for b in blocks:
        if b.editable:
            assert b.source_text in b.file.read_text(encoding="utf-8"), b.source_text


def test_a_file_included_twice_locates_both_copies(tmp_path):
    write(tmp_path, "sub.tex", "Repeated appendix text.\n")
    main = write(
        tmp_path, "main.tex", doc("One.\n\n\\input{sub}\n\nTwo.\n\n\\input{sub}\n\nThree.")
    )
    blocks = segment(flatten(main))
    copies = [b for b in blocks if b.file.name == "sub.tex"]
    assert len(copies) == 2
    assert [b.source_text for b in copies] == ["Repeated appendix text."] * 2
    assert [b.line_start for b in copies] == [1, 1]
    assert copies[0].id != copies[1].id
    assert copies[0].flat_start < copies[1].flat_start


def test_unresolvable_include_stays_in_source_text(tmp_path):
    main = write(tmp_path, "main.tex", doc("A value of \\input{nowhere} appears."))
    (block,) = segment(flatten(main))
    assert block.source_text == "A value of \\input{nowhere} appears."
    assert block.includes == ()
    assert block.editable is True


def test_line_numbers_point_into_the_host_file(tmp_path):
    main = write(
        tmp_path, "main.tex", doc("Para one.\n\nPara two.\n\nPara three.")
    )
    blocks = segment(flatten(main))
    lines = main.read_text(encoding="utf-8").splitlines()
    for block in blocks:
        assert lines[block.line_start - 1].startswith(block.source_text.split("\n")[0])


# ------------------------------------------------------------------- block ids


def test_block_id_shape():
    bid = block_id("some paragraph text")
    assert bid.startswith("b-")
    assert len(bid) == 12
    assert all(c in "0123456789abcdef" for c in bid[2:])


def test_block_id_ignores_whitespace_differences():
    assert block_id("one  two\nthree") == block_id("one two three")


def test_block_id_differs_on_content():
    assert block_id("alpha") != block_id("beta")


def test_ids_survive_an_edit_above(tmp_path):
    body = "First paragraph.\n\nSecond paragraph.\n\nThird paragraph."
    before = seg(tmp_path, body)
    edited = "First paragraph, now rewritten entirely.\n\nSecond paragraph.\n\nThird paragraph."
    after = segment(flatten(write(tmp_path, "main.tex", doc(edited))))
    assert before[0].id != after[0].id
    assert before[1].id == after[1].id
    assert before[2].id == after[2].id
    assert before[1].line_start == after[1].line_start  # sanity: same line here


def test_duplicate_content_gets_distinct_ids(tmp_path):
    blocks = seg(tmp_path, "Same text.\n\nSame text.\n\nSame text.")
    ids = [b.id for b in blocks]
    assert len(set(ids)) == 3
    assert ids[1] == ids[0] + "-2"
    assert ids[2] == ids[0] + "-3"


# -------------------------------------------------------------------- rematch


def test_rematch_exact_ids(tmp_path):
    body = "Alpha paragraph.\n\nBeta paragraph."
    old = seg(tmp_path, body)
    new = seg(tmp_path, body)
    assert rematch(old, new) == {b.id: b.id for b in old}


def test_rematch_finds_an_edited_block_by_similarity(tmp_path):
    old = seg(tmp_path, "The quick brown fox jumps over the lazy dog.\n\nUnchanged tail.")
    new = seg(tmp_path, "The quick brown fox leaps over the lazy dog.\n\nUnchanged tail.")
    mapping = rematch(old, new)
    assert mapping[old[0].id] == new[0].id
    assert mapping[old[1].id] == new[1].id


def test_rematch_returns_none_for_a_deleted_block(tmp_path):
    """The deleted paragraph is a near-twin of the surviving one, so a matcher
    that ignored what it had already claimed would hand this block's comments to
    a paragraph the author never commented on."""
    old = seg(
        tmp_path,
        "The quick brown fox jumps over the lazy dog.\n\n"
        "The quick brown fox jumps over the lazy cat.",
    )
    new = seg(tmp_path, "The quick brown fox jumps over the lazy dog.")
    assert difflib.SequenceMatcher(
        None, old[1].source_text, new[0].source_text
    ).ratio() > 0.9
    mapping = rematch(old, new)
    assert mapping[old[0].id] == new[0].id
    assert mapping[old[1].id] is None


def test_rematch_gives_a_contested_block_to_the_closest_match(tmp_path):
    """Two near-identical paragraphs, one deleted and the survivor edited, so
    both compete for the same new block on similarity. The loser must orphan,
    not share: a block can only be one paragraph."""
    old = seg(
        tmp_path,
        "The quick brown fox jumps over the lazy dog.\n\n"
        "The quick brown fox jumps over the lazy cat.",
    )
    new = seg(tmp_path, "The quick brown fox jumps over the lazy dogs.")
    mapping = rematch(old, new)
    assert mapping[old[0].id] == new[0].id
    assert mapping[old[1].id] is None


def test_similarity_matching_does_not_cross_files(tmp_path):
    """Exact ids are content-derived and so deliberately file-agnostic: a
    paragraph moved between files keeps its comments. The fuzzy pass is not,
    because a near-miss in another file is a different paragraph."""
    write(tmp_path, "sub.tex", "A wholly distinctive appendix paragraph here.\n")
    old = segment(
        flatten(write(tmp_path, "main.tex", doc("Root text.\n\n\\input{sub}")))
    )
    new = segment(
        flatten(
            write(
                tmp_path,
                "main.tex",
                doc("A wholly distinctive appendix paragraph there."),
            )
        )
    )
    sub_block = next(b for b in old if b.file.name == "sub.tex")
    assert difflib.SequenceMatcher(
        None, sub_block.source_text, new[0].source_text
    ).ratio() > 0.6
    assert rematch(old, new)[sub_block.id] is None


def test_exact_id_follows_a_paragraph_moved_between_files(tmp_path):
    write(tmp_path, "sub.tex", "A wholly distinctive appendix paragraph here.\n")
    old = segment(
        flatten(write(tmp_path, "main.tex", doc("Root text.\n\n\\input{sub}")))
    )
    new = segment(
        flatten(
            write(
                tmp_path,
                "main.tex",
                doc("A wholly distinctive appendix paragraph here."),
            )
        )
    )
    sub_block = next(b for b in old if b.file.name == "sub.tex")
    assert rematch(old, new)[sub_block.id] == new[0].id


def test_rematch_will_not_match_below_the_threshold(tmp_path):
    old = seg(tmp_path, "Completely unrelated first content.")
    new = seg(tmp_path, "Nothing whatsoever alike in here.")
    assert rematch(old, new)[old[0].id] is None


# ------------------------------------------------------------------- ordering


def test_blocks_are_in_document_order(tmp_path):
    blocks = seg(tmp_path, "One.\n\nTwo.\n\nThree.\n\nFour.")
    starts = [b.flat_start for b in blocks]
    assert starts == sorted(starts)
    for a, b in zip(blocks, blocks[1:]):
        assert a.flat_end <= b.flat_start


def test_blocks_are_frozen(tmp_path):
    (block,) = seg(tmp_path, "Only paragraph.")
    with pytest.raises(Exception):
        block.id = "b-0000000000"  # type: ignore[misc]


# ------------------------------------------------------- what a block is called


def test_a_float_is_named_by_its_own_caption(tmp_path):
    """The enclosing heading is not the name of an exhibit.

    dsp-bias, 2026-07-27: `\\input{outputs/tab_results.tex}` sits under a
    `\\paragraph{Socioeconomic status.}` fifty lines above it, so BOTH tables in
    that file inherited that heading. The ticker names an entry by its section,
    so an edit landing on the second table read "Socioeconomic status. edited"
    -- word for word what the first table, open in the inspector and still
    queued, was also called. The author read the two as one item.
    """
    blocks = seg(
        tmp_path,
        "\\paragraph{Socioeconomic status.}\nProse under the run-in heading.\n\n"
        "\\input{outputs/tab_results}",
        outputs__tab_results=(
            "\\begin{table}[h!]\n\\caption{Team assignment: household variation.\n"
            "Cells report means.}\n\\label{tab:one}\n\\begin{tabular}{ll}\nA & B \\\\\n"
            "\\end{tabular}\n\\end{table}\n\n"
            "\\begin{table}[h!]\n\\caption{Household variation in visit length.}\n"
            "\\begin{tabular}{ll}\nC & D \\\\\n\\end{tabular}\n\\end{table}\n"
        ),
    )
    tables = [b for b in blocks if b.file.name == "tab_results.tex"]
    assert len(tables) == 2, [b.source_text[:40] for b in tables]
    assert [b.parent_heading for b in tables] == \
        ["Socioeconomic status.", "Socioeconomic status."], "the heading is shared"
    assert [blocks_mod.label(b) for b in tables] == [
        "Team assignment: household variation.",
        "Household variation in visit length.",
    ]


def test_two_paragraphs_under_one_heading_are_named_differently(tmp_path):
    """A paragraph has no caption, so before this it answered to its heading.

    Reported live 2026-07-28. The caption fix of the day before named an
    EXHIBIT by its own words, but a paragraph has no `\\caption` to be named by,
    so every paragraph under one `\\section` still came back as that section --
    which is the same "two blocks, one name" the caption fix was written to
    kill, still armed for the large majority of blocks in any manuscript.
    """
    blocks = seg(
        tmp_path,
        "\\section{Methods}\n"
        "We drew the sample from the national register.\n\n"
        "Attrition was balanced across the two arms.",
    )
    prose = [b for b in blocks if b.kind == "paragraph"]
    assert len(prose) == 2
    assert [b.caption for b in prose] == [None, None]
    assert [b.parent_heading for b in prose] == ["Methods", "Methods"], "shared"
    names = [blocks_mod.label(b) for b in prose]
    assert names[0] != names[1], names
    assert names == [
        "We drew the sample from the national register.",
        "Attrition was balanced across the two arms.",
    ]


def test_a_heading_is_named_by_its_own_words(tmp_path):
    """Not by the heading ABOVE it, which is what `parent_heading` holds."""
    blocks = seg(tmp_path, "\\section{Results}\n\\subsection{Take-up}\nProse.")
    heads = [b for b in blocks if b.kind == "heading"]
    assert [b.parent_heading for b in heads] == [None, "Results"]
    assert [blocks_mod.label(b) for b in heads] == ["Results", "Take-up"]


def test_a_paragraph_label_drops_an_inline_input(tmp_path):
    """An `\\input` is a path, and a path is not what the author called this.

    qutub-india writes auto-exported values mid-sentence. `source_text` holds
    the directive verbatim -- that is the whole point of it -- so the naming
    pass has to drop it rather than print `exhibits/pval` at the author.
    """
    blocks = seg(
        tmp_path,
        "Referrals reached \\input{exhibits/pval} of the sampled cases.",
        exhibits__pval="0.372",
    )
    (prose,) = [b for b in blocks if b.kind == "paragraph"]
    assert "\\input" in prose.source_text, "the directive must survive in source"
    name = blocks_mod.label(prose)
    assert name == "Referrals reached of the sampled cases."
    assert "exhibits" not in name and "\\" not in name and "{" not in name


def test_a_paragraph_opening_with_an_input_is_named_by_its_words(tmp_path):
    """Built directly, so this is about naming and not about flatten: a block
    whose source begins with a directive must still come back as words."""
    prose = Block(
        id="b-0000000000", kind="paragraph", file=tmp_path / "main.tex",
        line_start=1, line_end=1, flat_start=0, flat_end=0,
        source_text="\\input{exhibits/pval} of the sampled cases were referred onward.",
        flat_text="", parent_heading="Results", editable=True,
    )
    name = blocks_mod.label(prose)
    assert name == "of the sampled cases were referred onward."
    assert "exhibits" not in name and "pval" not in name


def test_a_paragraph_label_is_stripped_the_way_a_caption_is(tmp_path):
    blocks = seg(
        tmp_path,
        "Take-up reached $64$\\% \\citep{doe2020example}, well above \\textbf{target}.",
    )
    (prose,) = [b for b in blocks if b.kind == "paragraph"]
    assert blocks_mod.label(prose) == "Take-up reached 64% , well above target."


def test_a_long_paragraph_label_is_clipped_in_python(tmp_path):
    """The ticker clamps at 26ch in CSS, but the queue and the agent work item
    read the same string and neither has a stylesheet."""
    body = " ".join(f"word{n}" for n in range(60)) + "."
    (prose,) = [b for b in seg(tmp_path, body) if b.kind == "paragraph"]
    name = blocks_mod.label(prose)
    assert len(name) <= blocks_mod.LABEL_CHARS
    assert name.endswith("\u2026")
    assert name.startswith("word0 word1 ")


def test_a_paragraph_label_drops_a_leading_latex_comment(tmp_path):
    """estonia-ecm's Introduction, verbatim in shape: several paragraphs open
    with a shouted drafting note that prints nothing and named the block."""
    blocks = seg(
        tmp_path,
        "\\section{Introduction}\n%ROSTER CITATIONS STILL MISSING HERE\n"
        "The NCT program moved routine visits into the neighborhood.",
    )
    (prose,) = [b for b in blocks if b.kind == "paragraph"]
    assert prose.source_text.startswith("%"), "the comment must be in the block"
    assert blocks_mod.label(prose) == \
        "The NCT program moved routine visits into the neighborhood."


def test_grouping_braces_go_even_when_a_command_survives_further_on(tmp_path):
    """The brace rule is local to the command holding them.

    Deciding it over the whole string -- keep every brace if any backslash is
    left anywhere -- is what put `Y_{ik,t}` in the first line of estonia-ecm's
    model paragraphs, because they close with an `\\alpha` forty words later.
    """
    (prose,) = [b for b in seg(
        tmp_path,
        "The share {of} patients rose. Formally, \\textcolor{red}{note} $\\alpha$ governs it.",
    ) if b.kind == "paragraph"]
    assert blocks_mod.label(prose) == "The share of patients rose."


def test_a_paragraph_that_is_only_markup_falls_back_to_its_heading(tmp_path):
    """Its own words are the right name only when it has words. `\\maketitle`
    names nothing, and printing it at the author names nothing either."""
    blocks = seg(tmp_path, "\\section{Front matter}\n\\maketitle\n\nReal prose here.")
    markup = next(b for b in blocks
                  if b.kind == "paragraph" and "maketitle" in b.source_text)
    assert blocks_mod.label(markup) == "Front matter"


def test_a_page_break_is_named_for_itself_not_for_the_exhibit_above_it(tmp_path):
    r"""A page break sits BETWEEN two headings and belongs inside neither.

    `parent_heading` is the heading above, and what the reader sees against a
    page break is whatever follows it, so the fallback names it after the wrong
    one of the two -- reliably, every time, in the one place a manuscript puts
    page breaks. covet-india sets each exhibit as `\newpage` then
    `\subsection*{Fig. 2. ...}`, so the break before Fig. 2 answered to
    "Fig. 1. Technical quality of TB care by study round and case" while the
    highlight sat on Fig. 2's heading. The author read the inspector as telling
    him he had Fig. 1 selected (2026-08-03).

    A name borrowed from a neighbour is worse than a plain one, because it says
    something false about what an edit would touch. So a break names itself.
    """
    blocks = seg(
        tmp_path,
        "\\subsection{Fig. 1. Quality of care}\nProse.\n\n\\newpage\n"
        "\\subsection{Fig. 2. Item curves}\nMore prose.",
    )
    brk = next(b for b in blocks if b.source_text.strip() == "\\newpage")
    assert brk.parent_heading == "Fig. 1. Quality of care"
    assert blocks_mod.label(brk) == "\\newpage"


def test_spacing_and_clearpage_name_themselves_too(tmp_path):
    r"""The same argument, and covet-india uses all three between exhibits."""
    blocks = seg(
        tmp_path,
        "\\subsection{Table 3. IPC}\nProse.\n\n\\clearpage\n\n\\vspace{0.5em}\n\n"
        "\\subsection{Fig. 1. Quality}\nMore.",
    )
    named = {b.source_text.strip(): blocks_mod.label(b) for b in blocks}
    assert named["\\clearpage"] == "\\clearpage"
    assert named["\\vspace{0.5em}"] == "\\vspace{0.5em}"


def test_a_markup_paragraph_that_is_not_a_break_still_takes_its_heading(tmp_path):
    r"""The narrowing is deliberate. `\noindent\includegraphics{f2.pdf}` is the
    body of the exhibit its heading names, so the heading IS its name, and
    naming it `\noindent` would be a plain answer that is also a worse one."""
    blocks = seg(
        tmp_path,
        "\\subsection{Fig. 2. Item curves}\n"
        "\\noindent\\includegraphics[width=\\textwidth]{exhibits/f2.pdf}",
    )
    fig = next(b for b in blocks if "includegraphics" in b.source_text)
    assert blocks_mod.label(fig) == "Fig. 2. Item curves"


def test_a_captionless_exhibit_still_falls_back_to_its_heading(tmp_path):
    """Its own words are column specs and ampersands, which name nothing."""
    blocks = seg(
        tmp_path,
        "\\section{Results}\n\\begin{table}\n\\begin{tabular}{ll}\nA & B \\\\\n"
        "\\end{tabular}\n\\end{table}",
    )
    (table,) = [b for b in blocks if b.kind == "table"]
    assert table.caption is None
    assert blocks_mod.label(table) == "Results"


def test_a_caption_is_read_to_its_own_closing_brace(tmp_path):
    """Nested braces are the normal case: a caption carries `\\textbf{}` and math."""
    (table,) = [b for b in seg(
        tmp_path,
        "\\begin{table}\n\\caption{Effects on \\textbf{women} ($N = 184$).}\n"
        "\\begin{tabular}{l}\nA \\\\\n\\end{tabular}\n\\end{table}",
    ) if b.kind != "heading"]
    assert table.caption == "Effects on women (N = 184)."


@pytest.mark.parametrize(
    "body,want",
    [
        # The short form is the author's own name for the exhibit; prefer it.
        ("\\caption[Short name]{A much longer explanatory caption.}", "Short name"),
        ("\\caption*{Unnumbered but still named.}", "Unnumbered but still named."),
        ("\\captionof{table}{Named through captionof.}", "Named through captionof."),
        # A label inside the caption is machinery, not words.
        ("\\caption{Named well.\\label{tab:x}}", "Named well."),
        # Only the title sentence. The rest is the note under the table.
        ("\\caption{The title sentence. Cells report means; stars are $p$-values.}",
         "The title sentence."),
        # An abbreviation is not the end of a sentence.
        ("\\caption{Compared with Fig. 2 across arms.}", "Compared with Fig. 2 across arms."),
        ("\\caption{Results for U.S. patients only.}", "Results for U.S. patients only."),
        # Hard-wrapped, which every one of these manuscripts is.
        ("\\caption{A caption broken\nacross three\nlines.}", "A caption broken across three lines."),
        # Math keeps its content and loses its delimiters: a `$` on screen is
        # LaTeX leaking into a label, and dropping the group loses the sample.
        ("\\caption{Roster characteristics ($N = 184$).}", "Roster characteristics (N = 184)."),
        ("\\caption{Stars are $p$-values, 90\\% of cases.}", "Stars are p-values, 90% of cases."),
        # Grouping braces print nothing. estonia-ecm's third table wrote its
        # caption this way, a braced run-in bold label followed by the subject;
        # the fixture below is that shape with invented wording.
        ("\\caption{{NCT Impact:} On admissions and referral counts.}",
         "NCT Impact: On admissions and referral counts."),
        # A citation is attribution, not a name, and cannot be rendered here.
        # The shape estonia-ecm's mediation flowchart caption used.
        ("\\caption{Referral pathway flowchart (based on \\citealt{roe2019sample})}",
         "Referral pathway flowchart"),
        ("\\caption{Replicating \\citep{doe2020example} across arms.}", "Replicating across arms."),
        # But not when a command is still holding one of them.
        ("\\caption{Effects, \\textcolor{red}{flagged}.}", "Effects, \\textcolor{red}{flagged}."),
        # An unpaired dollar must not eat the rest of the name.
        ("\\caption{Costs in $ per visit.}", "Costs in $ per visit."),
        ("\\captionsetup{width=.8\\textwidth}", None),
    ],
)
def test_caption_shapes(tmp_path, body, want):
    (table,) = [b for b in seg(
        tmp_path,
        "\\begin{table}\n" + body + "\n\\begin{tabular}{l}\nA \\\\\n\\end{tabular}\n\\end{table}",
    ) if b.kind != "heading"]
    assert table.caption == want
