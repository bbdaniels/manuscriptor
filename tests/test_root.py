"""The root rule, now written once for the server, the switcher, and the shell.

The defect that forced the unification: `find_main_tex` fell back to the
alphabetically first .tex, so a directory holding `abstract.tex` and
`paper.tex` served the abstract, silently. A root is a file that declares
itself one.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from manuscriptor.server.build import find_main_tex
from manuscriptor.source.root import (
    AmbiguousRoot,
    candidates,
    choose_main,
    has_documentclass,
)

DOC = "\\documentclass{article}\n\\begin{document}\nhi\n\\end{document}\n"
FRAG = "Prose that is only ever \\input into something else.\n"


def write(root: Path, name: str, text: str) -> Path:
    p = root / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


# ----------------------------------------------------------- the defect case


def test_the_declared_document_beats_the_alphabet(tmp_path):
    write(tmp_path, "abstract.tex", FRAG)
    write(tmp_path, "paper.tex", DOC)
    assert find_main_tex(tmp_path).name == "paper.tex"


def test_main_tex_still_wins_when_present(tmp_path):
    write(tmp_path, "main.tex", DOC)
    write(tmp_path, "paper.tex", DOC)
    assert find_main_tex(tmp_path).name == "main.tex"


def test_two_documents_and_no_main_is_a_question_not_a_guess(tmp_path):
    write(tmp_path, "appendix.tex", DOC)
    write(tmp_path, "paper.tex", DOC)
    with pytest.raises(Exception) as exc:
        find_main_tex(tmp_path)
    msg = str(exc.value)
    assert "appendix.tex" in msg and "paper.tex" in msg
    assert "--main" in msg


def test_a_lone_fragment_still_serves(tmp_path):
    # A fragment rendered alone is more useful than an error.
    write(tmp_path, "notes.tex", FRAG)
    assert find_main_tex(tmp_path).name == "notes.tex"


def test_an_explicit_main_is_never_second_guessed(tmp_path):
    write(tmp_path, "main.tex", DOC)
    write(tmp_path, "response.tex", DOC)
    assert find_main_tex(tmp_path, "response.tex").name == "response.tex"


# ------------------------------------------------------------- the switcher


def test_candidates_lists_declared_documents_main_first(tmp_path):
    write(tmp_path, "appendix.tex", DOC)
    write(tmp_path, "main.tex", DOC)
    write(tmp_path, "response.tex", DOC)
    write(tmp_path, "notes.tex", FRAG)  # not a document, not offered
    assert candidates(tmp_path) == ["main.tex", "appendix.tex", "response.tex"]


def test_a_commented_documentclass_is_not_a_document(tmp_path):
    write(tmp_path, "template.tex", "% \\documentclass{jelcodes}\n" + FRAG)
    write(tmp_path, "paper.tex", DOC)
    assert not has_documentclass(tmp_path / "template.tex")
    assert candidates(tmp_path) == ["paper.tex"]
    assert choose_main(tmp_path) == "paper.tex"


def test_choose_main_raises_with_the_choices_named(tmp_path):
    write(tmp_path, "appendix.tex", DOC)
    write(tmp_path, "paper.tex", DOC)
    with pytest.raises(AmbiguousRoot) as exc:
        choose_main(tmp_path)
    assert exc.value.names == ["appendix.tex", "paper.tex"]
