"""M1 — the source map must be byte-exact.

These are the highest-stakes tests in the project. Everything downstream splices
edits by byte range, so an offset that is wrong by two characters writes into
the middle of a word in a real manuscript, silently. There is no later stage
that would catch it.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from manuscriptor.source.flatten import flatten


def write(root: Path, name: str, text: str) -> Path:
    p = root / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


# --------------------------------------------------------------- no includes


def test_plain_file_is_unchanged(tmp_path):
    main = write(tmp_path, "main.tex", "alpha\n\nbeta\n")
    flat = flatten(main)
    assert flat.text == "alpha\n\nbeta\n"
    assert len(flat.segments) == 1
    assert flat.segments[0].file == main


def test_locate_finds_the_right_line(tmp_path):
    main = write(tmp_path, "main.tex", "one\ntwo\nthree\n")
    flat = flatten(main)
    assert flat.locate(0) == (main, 1)
    assert flat.locate(4) == (main, 2)
    assert flat.locate(8) == (main, 3)


# ------------------------------------------------------------------ includes


def test_input_is_inlined(tmp_path):
    write(tmp_path, "sub.tex", "INNER\n")
    main = write(tmp_path, "main.tex", "before\n\\input{sub}\nafter\n")
    flat = flatten(main)
    assert "INNER" in flat.text
    assert "\\input" not in flat.text


def test_include_is_inlined(tmp_path):
    write(tmp_path, "sub.tex", "INNER\n")
    main = write(tmp_path, "main.tex", "before\n\\include{sub}\nafter\n")
    assert "INNER" in flatten(main).text


def test_explicit_tex_extension_resolves(tmp_path):
    write(tmp_path, "sub.tex", "INNER\n")
    main = write(tmp_path, "main.tex", "\\input{sub.tex}\n")
    assert "INNER" in flatten(main).text


def test_subdirectory_paths_resolve_against_the_root(tmp_path):
    write(tmp_path, "tables/t1.tex", "TABLE\n")
    main = write(tmp_path, "main.tex", "\\include{tables/t1}\n")
    assert "TABLE" in flatten(main).text


def test_nested_includes_resolve(tmp_path):
    write(tmp_path, "deep.tex", "DEEP\n")
    write(tmp_path, "mid.tex", "midtop\n\\input{deep}\nmidbottom\n")
    main = write(tmp_path, "main.tex", "\\input{mid}\n")
    text = flatten(main).text
    assert "DEEP" in text and "midtop" in text and "midbottom" in text


# ------------------------------------------------------- the map across files


def test_offsets_map_back_to_the_originating_file(tmp_path):
    sub = write(tmp_path, "sub.tex", "INNER\n")
    main = write(tmp_path, "main.tex", "before\n\\input{sub}\nafter\n")
    flat = flatten(main)

    assert flat.locate(flat.text.index("before")) == (main, 1)
    assert flat.locate(flat.text.index("INNER")) == (sub, 1)
    assert flat.locate(flat.text.index("after"))[0] == main


def test_lines_after_an_include_still_count_in_the_parent(tmp_path):
    write(tmp_path, "sub.tex", "a\nb\nc\nd\n")
    main = write(tmp_path, "main.tex", "L1\nL2\n\\input{sub}\nL4\nL5\n")
    flat = flatten(main)
    # The included file contributes 4 lines, but L4 is still line 4 of main.tex.
    file, line = flat.locate(flat.text.index("L4"))
    assert (file, line) == (main, 4)


def test_every_offset_is_locatable(tmp_path):
    write(tmp_path, "sub.tex", "x\ny\n")
    main = write(tmp_path, "main.tex", "p\n\\input{sub}\nq\n")
    flat = flatten(main)
    for i in range(len(flat.text)):
        file, line = flat.locate(i)
        assert file.exists()
        assert line >= 1


def test_located_line_actually_contains_the_text(tmp_path):
    """The strongest check: read the line back and confirm it matches."""
    write(tmp_path, "sub.tex", "alpha\nbravo\ncharlie\n")
    main = write(tmp_path, "main.tex", "one\n\\input{sub}\ntwo\nthree\n")
    flat = flatten(main)
    for needle in ["one", "alpha", "bravo", "charlie", "two", "three"]:
        file, line = flat.locate(flat.text.index(needle))
        assert needle in file.read_text(encoding="utf-8").splitlines()[line - 1], needle


# ----------------------------------------------------------------- edge cases


def test_missing_file_is_left_verbatim_and_reported(tmp_path):
    main = write(tmp_path, "main.tex", "before\n\\input{nope}\nafter\n")
    flat = flatten(main)
    assert "\\input{nope}" in flat.text
    assert "nope" in " ".join(flat.missing)


def test_a_cycle_terminates(tmp_path):
    write(tmp_path, "b.tex", "B\n\\input{a}\n")
    main = write(tmp_path, "a.tex", "A\n\\input{b}\n")
    flat = flatten(main)  # must not recurse forever
    assert "A" in flat.text and "B" in flat.text


def test_commented_include_is_not_followed(tmp_path):
    write(tmp_path, "sub.tex", "INNER\n")
    main = write(tmp_path, "main.tex", "% \\input{sub}\nafter\n")
    flat = flatten(main)
    assert "INNER" not in flat.text
    assert "\\input{sub}" in flat.text


def test_escaped_percent_does_not_hide_an_include(tmp_path):
    write(tmp_path, "sub.tex", "INNER\n")
    main = write(tmp_path, "main.tex", "100\\% \\input{sub}\n")
    assert "INNER" in flatten(main).text


def test_adjacent_includes_do_not_drop_content(tmp_path):
    write(tmp_path, "a.tex", "AAA\n")
    write(tmp_path, "b.tex", "BBB\n")
    main = write(tmp_path, "main.tex", "\\input{a}\\input{b}\n")
    text = flatten(main).text
    assert "AAA" in text and "BBB" in text


def test_inline_result_fragment_splits_the_line(tmp_path):
    """The pattern that dominates qutub-india: a result inlined mid-sentence.

    `\\input{}` of an auto-exported number is how this author keeps results out
    of the manuscript by hand, so a single sentence routinely spans three files.
    The map must place each piece in its own file, and the resulting flat text
    must read as continuous prose.
    """
    frag = write(tmp_path, "results/pval.tex", "0.096")
    main = write(tmp_path, "main.tex", "effect was significant (p=\\input{results/pval}).\n")
    flat = flatten(main)

    assert flat.text.startswith("effect was significant (p=0.096).")
    assert flat.locate(flat.text.index("effect")) == (main, 1)
    assert flat.locate(flat.text.index("0.096")) == (frag, 1)
    assert flat.locate(flat.text.index(").")) == (main, 1)


def test_segments_tile_the_text_exactly(tmp_path):
    """No gaps, no overlaps, and the concatenation is the whole buffer."""
    write(tmp_path, "sub.tex", "INNER\n")
    main = write(tmp_path, "main.tex", "before\n\\input{sub}\nafter\n")
    flat = flatten(main)
    assert flat.segments[0].flat_start == 0
    assert flat.segments[-1].flat_end == len(flat.text)
    for a, b in zip(flat.segments, flat.segments[1:]):
        assert a.flat_end == b.flat_start


def test_locate_rejects_an_out_of_range_offset(tmp_path):
    main = write(tmp_path, "main.tex", "short\n")
    flat = flatten(main)
    with pytest.raises(IndexError):
        flat.locate(len(flat.text) + 10)
