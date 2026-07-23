"""Tree-wide document discovery.

The pivot's crux: `serve <top-level dir>` must find every editable document
across the tree, not just the ones in the served directory, and must never open
a fragment as if it were a document. These tests pin that contract -- documents
in subfolders are found, fragments and skip-dirs and too-deep files are not, and
a flat manuscript directory still yields exactly the list it did before.
"""
from __future__ import annotations

from pathlib import Path

from manuscriptor.source.tree import SKIP_DIRS, discover

DOC = "\\documentclass{article}\n\\begin{document}\nhi\n\\end{document}\n"
DOC_TITLED = ("\\documentclass{article}\n\\title{A Real Paper}\n"
              "\\begin{document}\nhi\n\\end{document}\n")
FRAG = "Prose that is only ever \\input into something else.\n"
COMMENTED = "% \\documentclass{article}\nA fragment with a dead template header.\n"


def write(root: Path, rel: str, text: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


# ------------------------------------------------------ the new capability


def test_finds_documents_across_the_tree(tmp_path):
    # The paper is not at the served root; it lives in latex/, with an appendix.
    write(tmp_path, "latex/main.tex", DOC)
    write(tmp_path, "latex/appendix.tex", DOC)
    write(tmp_path, "response/response.tex", DOC)
    docs = discover(tmp_path)
    assert {d.rel_main for d in docs} == {
        "latex/main.tex", "latex/appendix.tex", "response/response.tex"}


def test_rel_folder_and_rel_main_are_relative_to_the_served_root(tmp_path):
    write(tmp_path, "latex/main.tex", DOC)
    doc = discover(tmp_path)[0]
    assert doc.rel_folder == "latex"
    assert doc.rel_main == "latex/main.tex"
    assert Path(doc.root_dir) == (tmp_path / "latex").resolve()
    assert doc.main == "main.tex"


def test_a_document_at_the_served_root_has_no_folder_prefix(tmp_path):
    # Preserves the single-directory identity: rel_main is just the file name,
    # so the switcher list and the ?main= query read exactly as before.
    write(tmp_path, "main.tex", DOC)
    doc = discover(tmp_path)[0]
    assert doc.rel_folder == ""
    assert doc.rel_main == "main.tex"


# --------------------------------------------------------- what is excluded


def test_fragments_are_not_documents(tmp_path):
    write(tmp_path, "latex/main.tex", DOC)
    write(tmp_path, "latex/section1.tex", FRAG)          # an \input fragment
    write(tmp_path, "latex/tables/t1.tex", FRAG)         # a produced table
    docs = discover(tmp_path)
    assert {d.rel_main for d in docs} == {"latex/main.tex"}


def test_a_commented_out_documentclass_is_not_a_document(tmp_path):
    write(tmp_path, "paper.tex", DOC)
    write(tmp_path, "notes.tex", COMMENTED)
    assert {d.rel_main for d in discover(tmp_path)} == {"paper.tex"}


def test_skip_dirs_are_never_descended(tmp_path):
    write(tmp_path, "main.tex", DOC)
    for skip in SKIP_DIRS:
        write(tmp_path, f"{skip}/buried.tex", DOC)
    # The build cache in particular mirrors real .tex; it must not double-count.
    write(tmp_path, "build/manuscriptor/main.tex", DOC)
    assert {d.rel_main for d in discover(tmp_path)} == {"main.tex"}


def test_depth_is_capped(tmp_path):
    write(tmp_path, "main.tex", DOC)
    deep = "/".join(f"d{i}" for i in range(8)) + "/buried.tex"
    write(tmp_path, deep, DOC)
    found = {d.rel_main for d in discover(tmp_path, max_depth=3)}
    assert "main.tex" in found
    assert not any("buried.tex" in r for r in found)


# --------------------------------------------------------------- ordering


def test_root_folder_first_and_main_tex_first_within_a_folder(tmp_path):
    write(tmp_path, "main.tex", DOC)
    write(tmp_path, "appendix.tex", DOC)
    write(tmp_path, "latex/main.tex", DOC)
    write(tmp_path, "latex/aardvark.tex", DOC)
    order = [d.rel_main for d in discover(tmp_path)]
    # Root folder (depth 0) precedes the subfolder; main.tex leads each folder.
    assert order == ["main.tex", "appendix.tex", "latex/main.tex", "latex/aardvark.tex"]


def test_a_flat_manuscript_directory_matches_the_old_candidate_list(tmp_path):
    # The document switcher for an ordinary single-directory manuscript is the
    # same list it always was: main.tex first, then the rest by name.
    write(tmp_path, "main.tex", DOC)
    write(tmp_path, "appendix.tex", DOC)
    assert [d.rel_main for d in discover(tmp_path)] == ["main.tex", "appendix.tex"]


# ---------------------------------------------------------------- the title


def test_title_is_best_effort_from_the_documentclass_file(tmp_path):
    write(tmp_path, "main.tex", DOC_TITLED)
    assert discover(tmp_path)[0].title == "A Real Paper"


def test_a_document_without_a_title_has_an_empty_one(tmp_path):
    write(tmp_path, "main.tex", DOC)
    assert discover(tmp_path)[0].title == ""


# -------------------------------------------------------------- edge cases


def test_an_empty_tree_yields_no_documents(tmp_path):
    write(tmp_path, "notes.tex", FRAG)
    assert discover(tmp_path) == []
