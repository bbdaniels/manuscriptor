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
