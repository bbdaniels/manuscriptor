"""TikZ figures, compiled and rasterized for the page.

Pandoc cannot draw a tikzpicture; it vanished from the render. Each one is
compiled standalone against the manuscript's own preamble (so styles and
libraries resolve), rasterized, cached by content hash, and substituted with
an ordinary \\includegraphics the rest of the pipeline already handles. A
picture that fails to compile is left alone and reported, never guessed at.
"""
from __future__ import annotations

import shutil

import pytest

from manuscriptor.render import tikz

PREAMBLE = "\\documentclass{article}\n\\usepackage{tikz}\n"
PICTURE = "\\begin{tikzpicture}\n\\draw (0,0) -- (1,1);\n\\end{tikzpicture}"
HAS_TEX = shutil.which("pdflatex") and shutil.which("pdftoppm")


@pytest.mark.skipif(not HAS_TEX, reason="pdflatex or pdftoppm not installed")
def test_a_tikzpicture_becomes_an_image(tmp_path):
    text = "before\n" + PICTURE + "\nafter"
    out, made, failed = tikz.replace(text, preamble=PREAMBLE, out_dir=tmp_path)
    assert failed == []
    assert len(made) == 1
    assert "tikzpicture" not in out
    assert f"\\includegraphics{{{made[0]}}}" in out
    assert (tmp_path / made[0]).exists()
    assert "before" in out and "after" in out


@pytest.mark.skipif(not HAS_TEX, reason="pdflatex or pdftoppm not installed")
def test_the_raster_is_cached_by_content(tmp_path):
    text = "x " + PICTURE
    _, made, _ = tikz.replace(text, preamble=PREAMBLE, out_dir=tmp_path)
    png = tmp_path / made[0]
    first = png.stat().st_mtime_ns
    _, made2, _ = tikz.replace(text, preamble=PREAMBLE, out_dir=tmp_path)
    assert made2 == made
    assert png.stat().st_mtime_ns == first, "an unchanged picture recompiled"


@pytest.mark.skipif(not HAS_TEX, reason="pdflatex or pdftoppm not installed")
def test_a_picture_that_cannot_compile_is_left_alone_and_reported(tmp_path):
    bad = "\\begin{tikzpicture}\n\\draw (0,0 -- ;\n\\end{tikzpicture}"
    out, made, failed = tikz.replace("a\n" + bad + "\nb", preamble=PREAMBLE, out_dir=tmp_path)
    assert made == []
    assert failed, "a silent failure is indistinguishable from an empty page"
    assert bad in out, "a failed picture must not be replaced with a guess"


def test_without_a_tex_installation_nothing_changes(tmp_path, monkeypatch):
    import shutil as _sh
    monkeypatch.setattr(_sh, "which", lambda _: None)
    text = "a " + PICTURE
    out, made, failed = tikz.replace(text, preamble=PREAMBLE, out_dir=tmp_path)
    assert out == text and made == [] and failed
