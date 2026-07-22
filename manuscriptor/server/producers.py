"""Which `.tex` files are written by analysis code.

The first cut of this rule lived in `segment()` and said "generated means the
host file is not the root .tex". That is wrong in a way that matters: on
estonia-ecm it marks 283 of 384 blocks uneditable, and almost all of them are
hand-written prose appendices. The editor would refuse to edit three quarters of
the manuscript it exists to edit.

What the rule is trying to express is "editing this would hardcode a result", so
that is what is computed here, two ways.

**A producer match is definitive.** An analysis script that names the output
file tells us both that the file is generated and which script owns it, which is
also the beginning of the provenance graph phase 2 needs.

**A content test covers the rest.** Producer matching cannot be complete, and
the corpus shows exactly why: estonia-ecm's R scripts name outputs by basename
(`file = "table2_cross.tex"`), which is matchable, while qutub-india builds them
by concatenation (`paste0(stub, "_pp.tex")`), so no literal ever contains the
filename. A file whose own contents are a table fragment or a bare number is
recognisable regardless of how it was named.

Both are conservative in the same direction: an unrecognised file stays
editable, because wrongly refusing to edit prose is the failure that makes the
tool useless, while wrongly permitting an edit to a generated file is caught
downstream when the next run of the script overwrites it.
"""
from __future__ import annotations

import re
from pathlib import Path

# Where analysis code tends to live, relative to the repo root.
_CODE_DIRS = ("code", "scripts", "do", "R", "src", "analysis", "stata", "dofiles")
_CODE_SUFFIXES = (".R", ".r", ".do", ".py", ".Rmd", ".qmd")

_SKIP_DIRS = {".git", "renv", "node_modules", "__pycache__", ".venv", "venv", "build", "data"}

_TEX_LITERAL_RE = re.compile(r"""['"]([^'"\n]*?\.tex)['"]""")

# The markup that only ever appears in machine-written exhibits.
_TABULAR_RE = re.compile(r"\\(?:toprule|midrule|bottomrule|hline|cmidrule|multicolumn|multirow)\b")
# A sentence: some words, then a full stop followed by a space or a line end.
_SENTENCE_RE = re.compile(r"[A-Za-z][a-z]+(?:[ ,;][A-Za-z][a-z]+){4,}[.?!](?:\s|$)")
_PROSE_STRUCTURE_RE = re.compile(r"\\(?:section|subsection|subsubsection|paragraph)\b")


def scan(manuscript_dir: Path) -> dict[Path, Path]:
    """Map each generated `.tex` file to the script that writes it.

    Looks for analysis code beside the manuscript directory and up one level,
    since a manuscript usually sits in `latex/` or `manuscript/` next to `code/`.
    """
    manuscript_dir = Path(manuscript_dir).resolve()
    repo = manuscript_dir.parent
    scripts = _find_scripts(repo) + _find_scripts(manuscript_dir)
    if not scripts:
        return {}

    # Index every .tex under the manuscript directory by basename, so a script
    # naming only `table2_cross.tex` still resolves to `latex/tables/table2_cross.tex`.
    by_name: dict[str, list[Path]] = {}
    for tex in manuscript_dir.rglob("*.tex"):
        if _skipped(tex, manuscript_dir):
            continue
        by_name.setdefault(tex.name, []).append(tex.resolve())

    produced: dict[Path, Path] = {}
    for script in scripts:
        try:
            text = script.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for hit in _TEX_LITERAL_RE.finditer(text):
            name = Path(hit.group(1).strip()).name
            for target in by_name.get(name, ()):
                produced.setdefault(target, script.resolve())
    return produced


def looks_generated(path: Path) -> bool:
    """True when a file's own contents read as analysis output rather than prose.

    Used for the files no producer scan can claim. Deliberately blunt: prose
    wins any tie, because refusing to edit prose is the expensive mistake.
    """
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False

    stripped = re.sub(r"(?m)^\s*%.*$", "", text).strip()
    if not stripped:
        return False

    # A fragment holding a single value: the `\input{exhibits/foo}` pattern.
    if len(stripped) < 120 and not _SENTENCE_RE.search(stripped):
        return True

    # Anything carrying prose structure is a written section, whatever else is in it.
    if _PROSE_STRUCTURE_RE.search(stripped):
        return False

    sentences = len(_SENTENCE_RE.findall(stripped))
    if sentences >= 2:
        return False

    return bool(_TABULAR_RE.search(stripped)) or stripped.count("&") >= 4


def apply(blocks, produced: dict[Path, Path], *, root_file: Path):
    """Re-derive `editable` and `kind` from provenance rather than from path.

    Never re-enables a block the segmenter already refused: that refusal is about
    whether the block can be expressed as one byte range, which is a splicing
    question and nothing to do with where the text came from.
    """
    root_file = Path(root_file).resolve()
    out = []
    cache: dict[Path, bool] = {}
    for b in blocks:
        host = Path(b.file).resolve()
        if host == root_file:
            generated = False
        elif host in produced:
            generated = True
        else:
            if host not in cache:
                cache[host] = looks_generated(host)
            generated = cache[host]

        kind = "generated" if generated else b.kind
        if b.kind == "generated" and not generated:
            kind = "paragraph"

        out.append(_replace(b, kind=kind, editable=b.editable and not generated))
    return tuple(out)


# ----------------------------------------------------------------- internals


def _replace(block, **changes):
    """dataclasses.replace, but tolerant of the test doubles too."""
    try:
        import dataclasses

        if dataclasses.is_dataclass(block):
            return dataclasses.replace(block, **changes)
    except TypeError:
        pass
    return block._replace(**changes)


def _find_scripts(root: Path) -> list[Path]:
    found: list[Path] = []
    for name in _CODE_DIRS:
        d = root / name
        if d.is_dir():
            found.extend(_walk_scripts(d))
    found.extend(p for p in root.glob("*") if p.is_file() and p.suffix in _CODE_SUFFIXES)
    return found


def _walk_scripts(d: Path) -> list[Path]:
    out: list[Path] = []
    for p in d.rglob("*"):
        if not p.is_file() or p.suffix not in _CODE_SUFFIXES:
            continue
        if any(part in _SKIP_DIRS for part in p.parts):
            continue
        out.append(p)
    return out


def _skipped(path: Path, base: Path) -> bool:
    return any(part in _SKIP_DIRS for part in path.relative_to(base).parts)
