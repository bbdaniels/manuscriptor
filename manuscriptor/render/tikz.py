"""TikZ figures, compiled and rasterized for the page.

Pandoc cannot draw a tikzpicture, so one simply vanished from the render.
Each picture is compiled standalone against the manuscript's OWN preamble,
because that is where the tikz libraries, styles, and colour definitions
live; rasterized at 300dpi; cached by a hash of preamble plus picture so an
unchanged figure never recompiles; and substituted with an ordinary
`\\includegraphics` that the rest of the pipeline (pandoc, the asset routes,
the figure CSS) already handles.

A picture that fails to compile is left exactly as it was and reported,
never replaced with a guess: invisible-and-reported beats wrong.

Nothing here runs on the hot path. `render_block` never sees a tikzpicture
recompile because the cache key is content, and typing in a paragraph does
not change the picture.
"""
from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

_TIKZ_RE = re.compile(r"\\begin\{tikzpicture\}.*?\\end\{tikzpicture\}", re.S)
_DOCCLASS_RE = re.compile(r"\\documentclass\s*(\[[^\]]*\])?\s*\{[^}]*\}")

COMPILE_TIMEOUT = 60.0


def replace(text: str, *, preamble: str, out_dir: Path) -> tuple[str, list[str], list[str]]:
    """Substitute each tikzpicture with an `\\includegraphics` of its raster.

    Returns the rewritten text, the relative paths written under `out_dir`,
    and the failures (one message per picture left in place).
    """
    if not _TIKZ_RE.search(text):
        return text, [], []
    if not (shutil.which("pdflatex") and shutil.which("pdftoppm")):
        return text, [], ["pdflatex or pdftoppm not installed; tikz figures not rendered"]

    out_dir = Path(out_dir)
    made: list[str] = []
    failed: list[str] = []

    def one(m: re.Match) -> str:
        body = m.group(0)
        digest = hashlib.sha256((preamble + body).encode("utf-8")).hexdigest()[:16]
        rel = f"tikz/{digest}.png"
        png = out_dir / rel
        if not png.exists():
            err = _compile(body, preamble=preamble, png=png)
            if err:
                failed.append(err)
                return body
        made.append(rel)
        return f"\\includegraphics{{{rel}}}"

    return _TIKZ_RE.sub(one, text), made, failed


def _compile(body: str, *, preamble: str, png: Path) -> str | None:
    """One standalone compile and raster. Returns an error message, or None.

    The `standalone` class crops to the picture; the manuscript's preamble
    rides along minus its own `\\documentclass`, so `\\usetikzlibrary` calls
    and colour definitions resolve exactly as they do in the paper.
    """
    pre = _DOCCLASS_RE.sub("", preamble, count=1)
    doc = (
        "\\documentclass[preview,border=4pt]{standalone}\n"
        + pre
        + "\n\\begin{document}\n" + body + "\n\\end{document}\n"
    )
    png.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="tikz-", dir=str(png.parent)) as tmp:
        tex = Path(tmp) / "pic.tex"
        tex.write_text(doc, encoding="utf-8")
        try:
            run = subprocess.run(
                ["pdflatex", "-interaction=nonstopmode", "-halt-on-error", "pic.tex"],
                cwd=tmp, capture_output=True, timeout=COMPILE_TIMEOUT,
                encoding="utf-8", errors="replace",
            )
        except subprocess.TimeoutExpired:
            return f"tikz compile timed out after {int(COMPILE_TIMEOUT)}s"
        pdf = Path(tmp) / "pic.pdf"
        if run.returncode != 0 or not pdf.exists():
            line = next((ln for ln in run.stdout.splitlines() if ln.startswith("!")),
                        "pdflatex failed")
            return f"tikz compile failed: {line[:120]}"
        raster = subprocess.run(
            ["pdftoppm", "-png", "-r", "300", "-singlefile", str(pdf),
             str(png.with_suffix(""))],
            capture_output=True, timeout=COMPILE_TIMEOUT,
        )
        if raster.returncode != 0 or not png.exists():
            return "tikz raster failed"
    return None
