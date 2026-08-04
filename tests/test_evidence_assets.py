"""The evidence viewer stages figures the same way the served page does.

There were two implementations of "get this document's figures onto the page".
`render/postprocess.py` rasterizes a PDF figure and mirrors the raster into the
build directory; `evidence/render.py` had its own copier that only knew `<img>`.
Pandoc emits `<embed>` for a PDF, and a browser paints nothing for an unsized
PDF embed, so every PDF figure in the manuscript was a blank rectangle in
`index.html` -- all nine of covet-india's exhibits and all five of dsp-bias's,
at exit 0, with the caption underneath rendering perfectly.

The duplication is the defect, not the missing half. This asserts the evidence
viewer goes through the shared staging, so the next thing that pass learns is
not something it has to learn twice.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from manuscriptor.evidence import render as evrender

MINI_PDF = (b"%PDF-1.4\n"
            b"1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n"
            b"2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj\n"
            b"3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 24 24] >> endobj\n"
            b"trailer << /Root 1 0 R >>\n%%EOF")


def _project(tmp_path: Path, body: str) -> tuple[Path, Path]:
    # The manuscript and the build directory sit in different parents, so a
    # `../` src names two DIFFERENT paths and the containment guard is actually
    # exercised rather than short-circuited by both sides being one file.
    ms = tmp_path / "src" / "ms"
    (ms / "exhibits").mkdir(parents=True)
    (ms / "exhibits" / "fig1.pdf").write_bytes(MINI_PDF)
    main_tex = ms / "main.tex"
    main_tex.write_text("\\documentclass{article}\\begin{document}x\\end{document}\n")

    out = tmp_path / "build" / "out"
    out.mkdir(parents=True)
    (out / "manuscript.html").write_text(body, encoding="utf-8")
    (out / "claims.json").write_text("[]", encoding="utf-8")
    (out / "citations.json").write_text("[]", encoding="utf-8")
    return main_tex, out


def test_a_pdf_figure_is_rasterized_into_the_viewer(tmp_path):
    main_tex, out = _project(
        tmp_path,
        '<figure><embed src="exhibits/fig1.pdf" /><figcaption>F</figcaption></figure>',
    )
    evrender.run(output_dir=out, main_tex=main_tex)
    page = (out / "index.html").read_text(encoding="utf-8")
    assert "<embed" not in page, "a PDF figure reaches the viewer as an unpaintable embed"
    src = re.search(r'<img src="([^"]+)"', page).group(1)
    assert (out / src).is_file()
    assert (out / src).stat().st_size > 0


def test_a_plain_image_is_still_mirrored(tmp_path):
    main_tex, out = _project(tmp_path, '<p><img src="exhibits/plain.png" /></p>')
    (main_tex.parent / "exhibits" / "plain.png").write_bytes(b"\x89PNG mirrored")
    evrender.run(output_dir=out, main_tex=main_tex)
    assert (out / "exhibits" / "plain.png").read_bytes() == b"\x89PNG mirrored"


def test_an_asset_may_not_escape_the_viewer_directory(tmp_path):
    """The shared copier refuses a src that resolves outside the output
    directory. The viewer's own copier did not, so `../` in an image path let a
    manuscript write anywhere the pass could reach."""
    main_tex, out = _project(tmp_path, '<p><img src="../victim.png" /></p>')
    (main_tex.parent.parent / "victim.png").write_bytes(b"\x89PNG source")
    evrender.run(output_dir=out, main_tex=main_tex)
    assert not (out.parent / "victim.png").exists(), \
        "an asset was written outside the viewer directory"
