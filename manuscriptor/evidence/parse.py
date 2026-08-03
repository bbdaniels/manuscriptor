"""Stage 01 — parse LaTeX into claims.json + a pandoc-rendered manuscript.html.

Walks main.tex with pylatexenc to find every `\cite*` command, extracts the
enclosing sentence and section context, and runs pandoc separately to produce
the HTML body for the renderer.
"""
from __future__ import annotations

import json
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Iterable

from pylatexenc.latexwalker import LatexWalker, LatexMacroNode

CITE_MACROS = {
    "cite",
    "citep",
    "citet",
    "citealp",
    "citealt",
    "citeauthor",
    "citeyear",
    "citeyearpar",
    "Citep",
    "Citet",
    "autocite",
    "parencite",
    "textcite",
    "footcite",
    "fullcite",
}

SECTION_MACROS = {"part", "chapter", "section", "subsection", "subsubsection", "paragraph", "subparagraph"}

# Abbreviations that contain periods but should not break sentences.
ABBREVS = [
    "e.g.", "i.e.", "et al.", "cf.", "vs.",
    "Fig.", "Figs.", "Eq.", "Eqs.", "Ref.", "Refs.",
    "Tab.", "Tabs.", "approx.", "Inc.", "Ltd.", "St.",
    "U.S.", "U.K.", "U.N.", "Dr.", "Mr.", "Mrs.", "Ms.",
    "No.", "Nos.", "Prof.",
]


def run(*, main_tex: Path, bib_file: Path, output_dir: Path) -> None:
    """Entry point. Reads main_tex, writes claims.json + manuscript.html to output_dir."""
    src = main_tex.read_text(encoding="utf-8")
    src_clean = _strip_comments(src)

    cites = _extract_cite_positions(src_clean)
    sections = _extract_sections(src_clean)

    plain, marker_to_cite = _build_plain_with_markers(src_clean, cites)
    sentences = _segment_sentences(plain)

    claims: list[dict] = []
    for idx, cite in enumerate(cites):
        section = _find_section_at(cite["start"], sections)
        marker = f"__CITE_{idx}__"
        sentence_plain = _sentence_for_marker(marker, sentences) or ""
        sentence_plain = sentence_plain.replace(marker, _bracketed_keys(cite["keys"])).strip()
        sentence_plain = _restore_markers_to_text(sentence_plain, marker_to_cite)
        source_line = src_clean.count("\n", 0, cite["start"]) + 1
        claim_id = f"c{idx:04d}"
        claims.append({
            "claim_id": claim_id,
            "cite_keys": cite["keys"],
            "macro": cite["macro"],
            "sentence": sentence_plain,
            "section": section,
            "source_line": source_line,
            "source_offset": cite["start"],
        })

    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "claims.json").write_text(
        json.dumps(claims, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    html_body = _run_pandoc(main_tex, bib_file)
    (output_dir / "manuscript.html").write_text(html_body, encoding="utf-8")
    (output_dir / "parse.summary.json").write_text(
        json.dumps({
            "n_claims": len(claims),
            "unique_cite_keys": sorted({k for c in claims for k in c["cite_keys"]}),
            "n_unique_cite_keys": len({k for c in claims for k in c["cite_keys"]}),
            "n_sections": len(sections),
        }, indent=2),
        encoding="utf-8",
    )


def _strip_comments(src: str) -> str:
    """Remove LaTeX line comments (%) but preserve \\% literal percents."""
    out_lines: list[str] = []
    for line in src.split("\n"):
        result = []
        i = 0
        while i < len(line):
            ch = line[i]
            if ch == "\\" and i + 1 < len(line) and line[i + 1] == "%":
                result.append("\\%")
                i += 2
                continue
            if ch == "%":
                break
            result.append(ch)
            i += 1
        out_lines.append("".join(result))
    return "\n".join(out_lines)


def _extract_cite_positions(src: str) -> list[dict]:
    """Walk the LaTeX AST and return one record per `\\cite*` invocation."""
    walker = LatexWalker(src)
    cites: list[dict] = []
    nodelist, _, _ = walker.get_latex_nodes()

    def visit(nodes: Iterable):
        for node in nodes:
            if node is None:
                continue
            if isinstance(node, LatexMacroNode) and node.macroname in CITE_MACROS:
                keys = _extract_macro_keys(node, src)
                if keys:
                    cites.append({
                        "macro": node.macroname,
                        "keys": keys,
                        "start": node.pos,
                        "end": node.pos + node.len,
                    })
            # Recurse into any nodelist-bearing children
            for attr in ("nodelist", "nodeargs", "nodeargd"):
                child = getattr(node, attr, None)
                if child is None:
                    continue
                if hasattr(child, "argnlist"):
                    for ar in child.argnlist:
                        if ar is None:
                            continue
                        if hasattr(ar, "nodelist") and ar.nodelist:
                            visit(ar.nodelist)
                elif isinstance(child, list):
                    visit(child)

    visit(nodelist)
    cites.sort(key=lambda c: c["start"])
    return cites


def _extract_macro_keys(node: LatexMacroNode, src: str) -> list[str]:
    """Pull the comma-separated cite keys out of a cite macro's mandatory argument."""
    # Some cite variants have optional args: \citep[pre][post]{key}
    raw = src[node.pos:node.pos + node.len]
    # Find the LAST {...} group — that's the cite-keys arg.
    last_open = raw.rfind("{")
    last_close = raw.rfind("}")
    if last_open == -1 or last_close == -1 or last_close < last_open:
        return []
    inner = raw[last_open + 1:last_close]
    keys = [k.strip() for k in inner.split(",")]
    return [k for k in keys if k and re.match(r"^[A-Za-z0-9_\-:./+]+$", k)]


def _extract_sections(src: str) -> list[dict]:
    """Locate section/subsection headers and their starting offsets."""
    sections: list[dict] = []
    pattern = re.compile(r"\\(" + "|".join(SECTION_MACROS) + r")\*?\{([^}]+)\}")
    for m in pattern.finditer(src):
        sections.append({
            "level": m.group(1),
            "title": m.group(2).strip(),
            "start": m.start(),
        })
    return sections


def _find_section_at(offset: int, sections: list[dict]) -> str:
    """Return the most-specific section header preceding offset."""
    current = ""
    for sec in sections:
        if sec["start"] > offset:
            break
        current = f"{sec['level']}:{sec['title']}"
    return current


def _build_plain_with_markers(src: str, cites: list[dict]) -> tuple[str, dict[str, dict]]:
    """Replace each cite invocation with a unique marker, then strip remaining LaTeX commands to a rough plain text."""
    out_parts: list[str] = []
    cursor = 0
    marker_to_cite: dict[str, dict] = {}
    for idx, cite in enumerate(cites):
        out_parts.append(src[cursor:cite["start"]])
        marker = f"__CITE_{idx}__"
        marker_to_cite[marker] = cite
        out_parts.append(marker)
        cursor = cite["end"]
    out_parts.append(src[cursor:])
    intermediate = "".join(out_parts)
    plain = _latex_to_plain(intermediate)
    return plain, marker_to_cite


def _latex_to_plain(text: str) -> str:
    """Strip LaTeX markup to a plain-ish text suitable for sentence segmentation.

    Keeps cite markers (__CITE_n__) intact. Drops most commands. Replaces math
    with a placeholder so sentence segmentation doesn't break on equation
    periods. This is intentionally coarse — we use it only to find sentence
    boundaries, not for display.
    """
    # Remove math environments (display + inline) — coarse but adequate.
    text = re.sub(r"\\\[(.*?)\\\]", " EQUATION ", text, flags=re.DOTALL)
    text = re.sub(r"\$\$(.*?)\$\$", " EQUATION ", text, flags=re.DOTALL)
    text = re.sub(r"(?<!\\)\$(.*?)(?<!\\)\$", " EQUATION ", text, flags=re.DOTALL)
    # Drop \begin{...} ... \end{...} blocks for non-prose environments.
    for env in ("equation", "align", "figure", "table", "tabular", "lstlisting", "verbatim"):
        text = re.sub(
            rf"\\begin\{{{env}\*?\}}.*?\\end\{{{env}\*?\}}",
            f" {env.upper()} ",
            text,
            flags=re.DOTALL,
        )
    # Drop \input{...}, \include{...}, \label{...}, \ref{...}-family — keep cite markers.
    text = re.sub(r"\\(input|include|label|bibliography|bibliographystyle|usepackage|documentclass|cite\w*style)\s*\{[^}]*\}", " ", text)
    # Strip \footnote{...} — we lose the footnote text, but it preserves sentence flow.
    text = _strip_balanced_macro(text, "footnote")
    # Strip \emph{X}, \textit{X}, \textbf{X}, etc. → keep contents.
    text = re.sub(r"\\(emph|textit|textbf|textsf|textrm|texttt|underline)\s*\{([^{}]*)\}", r"\2", text)
    # Strip \ref{...}, \eqref{...}, \pageref{...}, \autoref{...} → drop the macro.
    text = re.sub(r"\\(ref|eqref|pageref|autoref|nameref|cref|Cref|Ref)\*?\{[^}]*\}", " (ref) ", text)
    # Strip section commands themselves — keep title text.
    text = re.sub(r"\\" + "|".join(SECTION_MACROS) + r"\*?\{([^}]*)\}", r"\1.", text)
    # Strip remaining macros with one mandatory arg (greedy fallback).
    text = re.sub(r"\\[A-Za-z]+\*?\s*\{([^{}]*)\}", r"\1", text)
    # Strip remaining bare macros.
    text = re.sub(r"\\[A-Za-z]+\*?", " ", text)
    # Strip leftover braces.
    text = text.replace("{", " ").replace("}", " ")
    # Collapse whitespace.
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _strip_balanced_macro(text: str, macro: str) -> str:
    """Remove `\\macro{...}` even when the body contains nested braces."""
    out: list[str] = []
    needle = f"\\{macro}"
    i = 0
    while i < len(text):
        j = text.find(needle, i)
        if j == -1:
            out.append(text[i:])
            break
        out.append(text[i:j])
        # Skip optional arg [..]
        k = j + len(needle)
        while k < len(text) and text[k] in (" ", "\t"):
            k += 1
        if k < len(text) and text[k] == "[":
            depth = 1
            k += 1
            while k < len(text) and depth > 0:
                if text[k] == "[":
                    depth += 1
                elif text[k] == "]":
                    depth -= 1
                k += 1
        # Eat the mandatory {...}
        if k < len(text) and text[k] == "{":
            depth = 1
            k += 1
            while k < len(text) and depth > 0:
                if text[k] == "{":
                    depth += 1
                elif text[k] == "}":
                    depth -= 1
                k += 1
            i = k
        else:
            out.append(text[j])
            i = j + 1
    return "".join(out)


def _segment_sentences(text: str) -> list[str]:
    """Split text into sentences. Protects abbreviations and decimals."""
    protected = text
    placeholders: dict[str, str] = {}
    for idx, abbr in enumerate(ABBREVS):
        key = f"__ABBR_{idx}__"
        placeholders[key] = abbr
        protected = protected.replace(abbr, key)
    # Protect decimals: 0.05, 2.5%, etc.
    protected = re.sub(r"(\d)\.(\d)", r"\1__DOT__\2", protected)
    # Split on sentence-ending punctuation followed by whitespace + capital/quote
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z\"\(\[`])", protected)
    sentences: list[str] = []
    for p in parts:
        s = p
        for key, val in placeholders.items():
            s = s.replace(key, val)
        s = s.replace("__DOT__", ".")
        s = re.sub(r"\s+", " ", s).strip()
        if s:
            sentences.append(s)
    return sentences


def _sentence_for_marker(marker: str, sentences: list[str]) -> str | None:
    for s in sentences:
        if marker in s:
            return s
    return None


def _bracketed_keys(keys: list[str]) -> str:
    return "[" + ", ".join(keys) + "]"


_MARKER_RE = re.compile(r"__CITE_(\d+)__")


def _restore_markers_to_text(s: str, marker_to_cite: dict[str, dict]) -> str:
    def sub(m: re.Match) -> str:
        marker = m.group(0)
        cite = marker_to_cite.get(marker)
        if not cite:
            return marker
        return _bracketed_keys(cite["keys"])

    return _MARKER_RE.sub(sub, s)


def _run_pandoc(main_tex: Path, bib_file: Path) -> str:
    """Render main_tex to HTML via pandoc. Falls back to article class if pandoc errors."""
    try:
        return _invoke_pandoc(main_tex, bib_file)
    except subprocess.CalledProcessError:
        # Build a temp copy of main_tex with documentclass swapped for article
        return _invoke_pandoc_with_fallback(main_tex, bib_file)


def _invoke_pandoc(tex_path: Path, bib_path: Path) -> str:
    csl = _find_csl(tex_path.parent)
    cmd = [
        "pandoc",
        str(tex_path),
        "--from=latex+raw_tex",
        "--to=html5",
        "--citeproc",
        f"--bibliography={bib_path}",
        "--mathjax",
        "--wrap=preserve",
    ]
    if csl:
        cmd.append(f"--csl={csl}")
    result = subprocess.run(
        cmd, check=True, capture_output=True,
        encoding="utf-8", errors="replace", cwd=tex_path.parent
    )
    return result.stdout


def _invoke_pandoc_with_fallback(tex_path: Path, bib_path: Path) -> str:
    src = tex_path.read_text(encoding="utf-8")
    swapped = re.sub(r"\\documentclass(\[[^\]]*\])?\{[^}]+\}", r"\\documentclass{article}", src, count=1)
    # Drop usage of project-specific class commands we can't satisfy.
    swapped = re.sub(r"\\usepackage\{wlscirep\}", "", swapped)
    with tempfile.NamedTemporaryFile("w", suffix=".tex", dir=tex_path.parent, delete=False) as tmp:
        tmp.write(swapped)
        tmp_path = Path(tmp.name)
    try:
        return _invoke_pandoc(tmp_path, bib_path)
    finally:
        try:
            tmp_path.unlink()
        except OSError:
            pass


def _find_csl(directory: Path) -> Path | None:
    candidates = list(directory.glob("*.csl"))
    if not candidates:
        # Try ~/.csl/ econ.csl as a global fallback
        global_csl = Path.home() / ".csl" / "econ.csl"
        if global_csl.exists():
            return global_csl
        return None
    return candidates[0]
