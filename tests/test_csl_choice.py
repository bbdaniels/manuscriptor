"""Which citation style a manuscript gets, and the one module that decides it.

There were three copies of this lookup on 2026-08-11: `render/pandoc.py`,
`server/compile.py` and `evidence/parse.py`. They had already diverged. Two
sorted the directory glob and one did not, so a manuscript directory holding
more than one `.csl` got a deterministic pick in the renderer and a
filesystem-order pick in the evidence pass -- the same manuscript, the same
question, two answers, and neither path raises because a citation still renders
either way. It only shows up as a reference list formatted one way on the page
and another in the evidence export.

That is the shape the guard below exists for. A test that only checked the
chooser returns the right file would not have caught it, because all three
copies returned "a right answer". The defect was that there were three.

Adding the `MANUSCRIPTOR_CSL` override to one copy would have made a third
distinct behavior, which is exactly how the divergence was found.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from manuscriptor.render import pandoc

SRC = Path(__file__).resolve().parent.parent / "manuscriptor"


# --------------------------------------------------------------- the behavior


def test_a_style_beside_the_manuscript_wins(tmp_path, monkeypatch):
    beside = tmp_path / "econ.csl"
    beside.write_text("<style/>", encoding="utf-8")
    other = tmp_path / "elsewhere.csl"
    other.write_text("<style/>", encoding="utf-8")
    monkeypatch.setenv(pandoc.CSL_ENV, str(other))
    assert pandoc.find_csl(tmp_path) == beside


def test_the_pick_is_deterministic_when_the_directory_holds_two(tmp_path):
    """The divergence that started this file: one copy globbed unsorted."""
    for name in ("zeta.csl", "alpha.csl", "middle.csl"):
        (tmp_path / name).write_text("<style/>", encoding="utf-8")
    picks = {pandoc.find_csl(tmp_path) for _ in range(5)}
    assert picks == {tmp_path / "alpha.csl"}


def test_the_environment_answers_when_the_directory_does_not(tmp_path, monkeypatch):
    style = tmp_path / "mine.csl"
    style.write_text("<style/>", encoding="utf-8")
    empty = tmp_path / "manuscript"
    empty.mkdir()
    monkeypatch.setenv(pandoc.CSL_ENV, str(style))
    assert pandoc.find_csl(empty) == style


def test_an_environment_pointing_at_nothing_is_not_used(tmp_path, monkeypatch):
    empty = tmp_path / "manuscript"
    empty.mkdir()
    monkeypatch.setenv(pandoc.CSL_ENV, str(tmp_path / "gone.csl"))
    monkeypatch.setattr(pandoc, "DEFAULT_CSL", str(tmp_path / "also-gone.csl"))
    assert pandoc.find_csl(empty) is None


def test_the_environment_is_read_at_call_time(tmp_path, monkeypatch):
    """Import-time resolution is what makes an override untestable and unusable
    in a process that is already running."""
    empty = tmp_path / "manuscript"
    empty.mkdir()
    first = tmp_path / "first.csl"
    first.write_text("<style/>", encoding="utf-8")
    second = tmp_path / "second.csl"
    second.write_text("<style/>", encoding="utf-8")
    monkeypatch.setenv(pandoc.CSL_ENV, str(first))
    assert pandoc.find_csl(empty) == first
    monkeypatch.setenv(pandoc.CSL_ENV, str(second))
    assert pandoc.find_csl(empty) == second


def test_nothing_anywhere_still_lets_pandoc_choose(tmp_path, monkeypatch):
    empty = tmp_path / "manuscript"
    empty.mkdir()
    monkeypatch.delenv(pandoc.CSL_ENV, raising=False)
    monkeypatch.setattr(pandoc, "DEFAULT_CSL", str(tmp_path / "nope.csl"))
    assert pandoc.find_csl(empty) is None


# ------------------------------------------------------------------ the guard


def _strip_comments(text: str) -> str:
    text = re.sub(r'"""(?:.|\n)*?"""', "", text)
    text = re.sub(r"'''(?:.|\n)*?'''", "", text)
    return "\n".join(line.split("#", 1)[0] for line in text.splitlines())


def test_only_pandoc_decides_which_csl_a_manuscript_gets():
    """A second derivation is the defect, not a missing one."""
    offenders = []
    for path in sorted(SRC.rglob("*.py")):
        if path.name == "pandoc.py":
            continue
        body = _strip_comments(path.read_text(encoding="utf-8"))
        for line in body.splitlines():
            if (re.search(r'glob\(\s*[\'"]\*\.csl[\'"]\s*\)', line)
                    or re.search(r'[\'"]\.csl[\'"]\s*/', line)
                    or re.search(r'econ\.csl', line)):
                offenders.append(f"{path.relative_to(SRC.parent)}: {line.strip()}")
    assert offenders == [], (
        "the citation style is chosen outside render/pandoc.py, which is how the "
        "renderer and the evidence pass came to sort the same directory "
        "differently: " + "; ".join(offenders))


@pytest.mark.parametrize("module_name, attr", [
    ("manuscriptor.server.compile", "_find_csl"),
    ("manuscriptor.evidence.parse", "_find_csl"),
    ("manuscriptor.render.pandoc", "_find_csl"),
])
def test_the_old_private_copies_are_gone(module_name, attr):
    """Named individually so a reintroduction says which module brought it back."""
    import importlib
    module = importlib.import_module(module_name)
    assert not hasattr(module, attr), (
        f"{module_name}.{attr} is back; call pandoc.find_csl instead")
