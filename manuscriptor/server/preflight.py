"""What a clean compile does not tell you about a manuscript.

Written off a real submission where roughly a dozen defects surfaced on the
morning it went out. In a list they look like a dozen unrelated bugs; they are
one bug wearing a dozen costumes. In every case the artifact looked current and
was not, or the verification passed for the wrong reason, and **not one of them
would have failed a compile, a timestamp check, or a page count.**

Three checks live here, the three the memo ranks highest per unit of effort:

`fragments` — every `\\input` and `\\include` target resolves AND actually
contributes something. Proved by negative control: a *missing* `\\input` hard
errors and produces no PDF, but an *empty* one compiles clean, holds the same
page count, and silently drops the number out of the sentence. An abstract
rendered "we measured  simulated conversations" with no error anywhere. A clean
compile is good evidence against missing files and NO evidence against empty
ones.

`exhibit-numbers` — a hand-typed `Table~S22` or `Figure~3`, in prose or in an
analysis script. Eighteen such sites survived into that submission and two were
wrong, pointing at exhibits that had moved. Also the `Table~S\\ref{}` doubling
that ships "Table SS19" when the document has already redefined `\\thetable`.

`bib-fields` — every field present in the `.bib` is declared by the `.bst`.
`apalike.bst` silently discarded 39 DOIs: the field was on 39 of 40 entries and
all resolved, but the style does not declare `doi`, so BibTeX dropped them
without a word.

**Check zero, and it is the reason the module is shaped this way.** The sharpest
finding in that memo is that three agents verifying the submission "finished
their work and went idle without reporting anything", and the silence was read
as success. So a check here does not return findings, it returns a `Result` that
says whether it RAN. A check that could not run is `skipped` and is never
rendered as a pass; `audit()` fails when a planned check produced no result at
all. `n/a` is a separate answer and requires a positive determination -- a
document that declares no bibliography has nothing to check, which is not the
same as being unable to tell.

**It reports and does not modify anything**, the same posture as `tidy`. Every
finding carries the fields `drain.comment(..., review=True)` wants, so a run can
later be delivered as anchored review comments without reshaping anything.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from manuscriptor.server import gitcmd, paths
from manuscriptor.source import flatten as flat_mod
from manuscriptor.source import root as root_mod

CHECKS = ("fragments", "exhibit-numbers", "bib-fields")

# The scripts sweep belongs to `exhibit-numbers` but not to any one document:
# an R file that prints "Table~S19" is a defect of the project, not of a `.tex`.
SCRIPTS = "(analysis scripts)"

SCRIPT_SUFFIXES = (".r", ".do", ".py", ".jl", ".sh", ".rmd", ".qmd")

# Directories a manuscript keeps beside it that are nobody's source. Manuscriptor's
# own directory is named by `paths`, never spelled here: this module writes
# nothing, so it needs no tier of its own, only the name to walk past.
SKIP_DIRS = {".git", paths.HOME, "node_modules", "renv", "__pycache__",
             ".venv", "venv", ".Rproj.user", "_minted"}

# `Table 1`, `Table~S22`, `Figures S3`, and `see table 1` mid-sentence, which is
# why this ignores case. At least one separator is then REQUIRED, and that is
# what carries the whole burden of not crying wolf: without it, ignoring case
# makes every `figures/figure1.pdf` a claim about figure one.
EXHIBIT_RE = re.compile(r"\b(Tables?|Figures?|Figs?\.)[~\s]+(S?\d+)", re.IGNORECASE)

# `Table~S\ref{tab:x}` in a document whose counter already carries the S.
DOUBLED_RE = re.compile(r"\b(Tables?|Figures?)[~\s]+S\\ref\b")
S_COUNTER_RE = re.compile(r"\\renewcommand\s*\{?\s*\\the(?:table|figure)\s*\}?\s*\{[^}]*S")

STYLE_RE = re.compile(r"\\bibliographystyle\s*\{([^}]*)\}")
CLASS_RE = re.compile(r"\\documentclass\s*(?:\[[^\]]*\])?\s*\{([^}]*)\}")
PACKAGE_RE = re.compile(r"\\usepackage\s*(?:\[[^\]]*\])?\s*\{([^}]*)\}")
BIBFILES_RE = re.compile(r"\\bibliography\s*\{([^}]*)\}")
ADDRESOURCE_RE = re.compile(r"\\addbibresource\s*\{([^}]*)\}")
BIBLATEX_RE = re.compile(r"\\usepackage\s*(\[[^\]]*\])?\s*\{[^}]*\bbiblatex\b[^}]*\}")

# BibTeX hands these to the style without the style declaring them.
BIB_BUILTIN = {"crossref", "key"}

# Pointers a reader follows out of the bibliography. A style that drops one of
# these has removed something from the page; a style that drops `abstract` or
# `keywords` has removed reference-manager exhaust nobody meant to typeset.
# Both are reported, but only these get a line of their own, because a check
# whose output is mostly noise gets ignored, which fails the same way a skipped
# check does.
BIB_LOCATORS = ("doi", "url", "eprint", "pmid", "pmcid", "arxiv",
                "isbn", "issn", "note", "howpublished")


# --------------------------------------------------------------- the findings


@dataclass(frozen=True)
class Finding:
    """One defect, shaped so it can be filed as an anchored review comment.

    `quote` is what a comment would anchor on: text from the neighbourhood of
    the defect, with LaTeX markup stripped, so the server's re-anchoring can
    place it on the paragraph the reader is looking at. Best effort by
    construction -- an empty fragment leaves a hole exactly where its number
    should be, so the quote is the sentence around the hole.

    `doc` and `file` are NOT the same question and must not be folded together.
    `doc` is the document being served, which is what a comment is filed
    against; `file` is where the offending bytes actually sit, which is what a
    person needs in order to go and fix it. A hand-typed number in
    `outputs/tab_results.tex` belongs to a comment on `main.tex`, and reporting
    either one in place of the other is useless in a different way.
    """

    check: str
    doc: str            # the served document a comment would be filed against
    file: str           # where the text is, relative to the manuscript
    line: int           # 1-indexed in `file`, 0 when there is no one line
    quote: str
    body: str

    def as_comment(self) -> dict:
        """The keyword arguments `drain.comment` takes for a review finding.

        Kept here rather than at the call site so the shape is asserted by a
        test instead of discovered later by a UI that does not fit it.
        """
        return {"body": self.body, "quote": self.quote,
                "doc": self.doc, "check": self.check, "review": True}

    def line_text(self) -> str:
        where = f"{self.file}:{self.line}" if self.line else self.file or "-"
        return f"   {where}  {self.body}"


@dataclass(frozen=True)
class Result:
    """Whether one check, on one document, actually ran.

    `status` is one of:
      ok      it ran and found nothing
      findings it ran and found something
      skipped  it COULD NOT run, and this is not a pass
      n/a      it ran far enough to establish there is nothing to check
    """

    check: str
    doc: str
    status: str
    detail: str
    examined: int = 0
    findings: tuple[Finding, ...] = ()

    @property
    def ran(self) -> bool:
        return self.status in ("ok", "findings", "n/a")

    def line(self) -> str:
        mark = {"ok": " ", "findings": "!", "skipped": "?", "n/a": "-"}[self.status]
        head = f" {mark} {self.check:<16} {self.doc:<24} {self.status:<9} {self.detail}"
        return "\n".join([head] + [f.line_text() for f in self.findings])


# ------------------------------------------------------------------ the plan


def documents(manuscript_dir: Path, main: str | None = None) -> list[str]:
    """Every document in this directory preflight owes an answer about.

    All of them by default, not just `main.tex`: on the submission this module
    came from, the supplement was built by a different rule that skipped bibtex
    entirely, and checking only the main paper would have reported the paper
    healthy while the supplement shipped a frozen `.bbl`.
    """
    if main:
        return [main]
    d = Path(manuscript_dir)
    names = root_mod.candidates(d)
    if not names:
        try:
            names = [root_mod.choose_main(d)]
        except (LookupError, OSError):
            names = []

    # `root.candidates` asks each `.tex` whether it declares a document class,
    # and answers "no" when it cannot open the file at all. For a doc switcher
    # that is right; here it means an unreadable manuscript is not merely
    # unchecked, it is never mentioned. "I could not tell" is not "no".
    for p in root_mod.tex_files(d):
        if p.name in names:
            continue
        try:
            p.read_bytes()
        except OSError:
            names.append(p.name)
    return names


def plan(manuscript_dir: Path | str, main: str | None = None) -> list[tuple[str, str]]:
    """Every (check, document) pair that must produce a result.

    `audit` compares this against what `run` returned. A check that threw, or
    that a future refactor forgot to call, is then a loud failure rather than a
    quiet absence -- which is the whole point of the module.
    """
    d = Path(manuscript_dir).resolve()
    pairs = [(c, doc) for doc in documents(d, main) for c in CHECKS]
    pairs.append(("exhibit-numbers", SCRIPTS))
    return pairs


def audit(planned: list[tuple[str, str]], results: list[Result]) -> list[str]:
    """What the run failed to say anything about at all. Empty is the good case."""
    got = {(r.check, r.doc) for r in results}
    return [f"{c} on {doc}" for c, doc in planned if (c, doc) not in got]


# ------------------------------------------------------------------- the run


def run(manuscript_dir: Path | str, main: str | None = None) -> list[Result]:
    """Every check on every document. Reads; writes nothing, ever."""
    d = Path(manuscript_dir).resolve()
    results: list[Result] = []
    for doc in documents(d, main):
        # Read the root ourselves first. `flatten` treats an unreadable file as
        # an empty one by design, which is right for rendering and disastrous
        # here: every check would run against an empty buffer and report a
        # document with no includes, no exhibit numbers and no bibliography.
        # Three passes, all green, none of which read the manuscript.
        try:
            (d / doc).read_bytes()
        except OSError as exc:
            for c in CHECKS:
                results.append(Result(c, doc, "skipped", f"could not read {doc}: {exc}"))
            continue
        flat = flat_mod.flatten(d / doc)
        results.append(check_fragments(d, doc, flat))
        results.append(check_exhibit_numbers(d, doc, flat))
        results.append(check_bib_fields(d, doc, flat))
    results.append(check_scripts(d))
    return results


# ------------------------------------------------------------ check: fragments


def check_fragments(d: Path, doc: str, flat: flat_mod.FlatSource) -> Result:
    """Every include target exists and actually says something.

    The measure is what the target CONTRIBUTED to the flattened buffer, taken
    from the flattening walk itself rather than from a second reading of the
    file. That is the only measure that catches all three shapes of nothing: a
    zero-byte fragment, a fragment holding one comment, and a fragment that
    inputs two other empty fragments.
    """
    findings: list[Finding] = []
    for dr in flat.directives:
        rel = _rel(d, dr.file)
        if dr.resolved is None:
            why = f"\\{dr.kind}{{{dr.target}}} resolves to no file"
        elif dr.cyclic:
            why = f"\\{dr.kind}{{{dr.target}}} includes a file already open above it"
        elif dr.contributed == 0:
            why = (f"\\{dr.kind}{{{dr.target}}} exists but contributes nothing; "
                   "this compiles clean at the same page count and drops "
                   "whatever it should have said")
        elif not flat.text[dr.flat_start:dr.flat_start + dr.contributed].strip():
            why = (f"\\{dr.kind}{{{dr.target}}} contributes only whitespace; "
                   "the compile will not complain")
        else:
            continue
        findings.append(Finding("fragments", doc, rel, dr.line,
                                _quote(flat.text, dr.flat_start), why))
    return _result("fragments", doc, findings,
                   f"{len(flat.directives)} include directives",
                   len(flat.directives))


# ------------------------------------------------------ check: exhibit numbers


def check_exhibit_numbers(d: Path, doc: str, flat: flat_mod.FlatSource) -> Result:
    """A hand-typed exhibit number in prose is a hardcoded result.

    It goes stale the moment the exhibit order changes, silently and with no
    compile error. The rule this enforces is already written down in the
    repository: within one document always `\\ref` a `\\label`, and across
    documents use a generated label map.
    """
    findings: list[Finding] = []
    text = flat.text
    doubling_matters = bool(S_COUNTER_RE.search(text))

    for m in EXHIBIT_RE.finditer(text):
        if flat_mod.is_commented(text, m.start()):
            continue
        where, line = _locate(flat, m.start())
        findings.append(Finding(
            "exhibit-numbers", doc, _rel(d, where), line,
            _quote(text, m.start()), f"hand-typed \u201c{m.group(0)}\u201d; use \\ref to a \\label, or a "
            "generated label-map macro across documents"))

    if doubling_matters:
        for m in DOUBLED_RE.finditer(text):
            if flat_mod.is_commented(text, m.start()):
                continue
            where, line = _locate(flat, m.start())
            findings.append(Finding(
                "exhibit-numbers", doc, _rel(d, where), line,
                _quote(text, m.start()), f"\u201c{m.group(0)}\u201d doubles the S prefix: this document "
                "redefines the counter to carry it already, so this renders "
                "\u201cTable SS19\u201d"))

    detail = f"{len(text)} characters of prose"
    if doubling_matters:
        detail += "; counter redefined with an S prefix"
    return _result("exhibit-numbers", doc, findings, detail, len(text))


def check_scripts(d: Path) -> Result:
    """The same rule, in the code that writes the numbers.

    The eighteenth hand-typed site on that submission was an R script emitting
    a literal `Table~S19`, under a TODO acknowledging the hazard. A script is
    where a stale number is hardest to see, because nothing about it looks like
    prose.
    """
    findings: list[Finding] = []
    scanned = 0
    base = _project_root(d)
    for path in _scripts(base):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        scanned += 1
        rel = _rel(base, path)
        for m in EXHIBIT_RE.finditer(text):
            line_start = text.rfind("\n", 0, m.start()) + 1
            if _script_comment(path, text[line_start:m.start()]):
                continue
            line = text.count("\n", 0, m.start()) + 1
            findings.append(Finding(
                "exhibit-numbers", "", rel, line,
                _quote(text, m.start()), f"analysis script emits a hand-typed \u201c{m.group(0)}\u201d; "
                "have it write a label-map macro instead"))
    return _result("exhibit-numbers", SCRIPTS, findings,
                   f"{scanned} script{'s' if scanned != 1 else ''} under "
                   f"{base.name}/", scanned)


def _project_root(d: Path) -> Path:
    """The repository the manuscript sits in, or the manuscript directory.

    The analysis lives one level up from the paper -- dsp-bias keeps its `.tex`
    in `paper/` and every R script in `analysis/` -- so a sweep bounded by the
    manuscript directory would miss the exact site the memo names, an R script
    printing a literal `Table~S19`.
    """
    done = gitcmd.run(["rev-parse", "--show-toplevel"], cwd=d)
    if done is None:
        return d
    top = done.stdout.strip()
    return Path(top) if top and Path(top).is_dir() else d


def _scripts(d: Path) -> list[Path]:
    out: list[Path] = []
    stack = [d]
    while stack:
        here = stack.pop()
        try:
            entries = sorted(here.iterdir())
        except OSError:
            continue
        for p in entries:
            if p.is_dir():
                if p.name not in SKIP_DIRS and not p.name.startswith("."):
                    stack.append(p)
            elif p.suffix.lower() in SCRIPT_SUFFIXES:
                out.append(p)
    return sorted(out)


def _script_comment(path: Path, before: str) -> bool:
    """True when the match sits after this language's comment marker.

    A TODO in a comment saying the number is hardcoded is a note about the
    defect, not the defect; the literal in the code is what ships.
    """
    if path.suffix.lower() == ".do":
        # Stata's `*` opens a comment only at the start of a line; anywhere else
        # it is multiplication, and treating it as a comment would silently
        # excuse every literal on a line that happens to multiply.
        return before.lstrip().startswith("*") or "//" in before
    return "#" in before


# ----------------------------------------------------------- check: bib fields


def check_bib_fields(d: Path, doc: str, flat: flat_mod.FlatSource) -> Result:
    """Every field the `.bib` carries, the `.bst` must declare.

    A style that does not declare a field does not warn about it; BibTeX simply
    never hands it over. `apalike.bst` swallowed 39 DOIs that way, in a
    bibliography where every one of them was present and correct.
    """
    text = _uncommented(flat.text)
    styles = [s.strip() for m in STYLE_RE.finditer(text) for s in [m.group(1)] if s.strip()]
    bibs = [b.strip() for m in BIBFILES_RE.finditer(text)
            for b in m.group(1).split(",") if b.strip()]
    bibs += [b.strip() for m in ADDRESOURCE_RE.finditer(text)
             for b in m.group(1).split(",") if b.strip()]

    via = doc
    if not styles:
        # A journal class routinely sets the style for you: estonia-qbs names no
        # style at all in `main.tex` because `wlscirep.cls` says
        # `\bibliographystyle{aer}` on its own line 53. Reading only the .tex
        # tree reported that check as unrunnable, which is honest but wrong --
        # the class is an input of the document like any other.
        styles, via = _style_from_class(d, text)

    if not styles and not bibs:
        return Result("bib-fields", doc, "n/a",
                      "the document declares no bibliography")
    if not styles and (ADDRESOURCE_RE.search(text) or BIBLATEX_RE.search(text)):
        return Result("bib-fields", doc, "n/a",
                      "biblatex: field support is the .bbx's, not a .bst's")
    if not styles:
        return Result("bib-fields", doc, "skipped",
                      "a bibliography is declared but no \\bibliographystyle is, "
                      "here or in the class")

    bst = _find_bst(d, styles[0])
    if bst is None:
        return Result("bib-fields", doc, "skipped",
                      f"{styles[0]}.bst not beside the manuscript and kpsewhich "
                      "could not find it")
    try:
        declared = _bst_fields(bst.read_text(encoding="utf-8", errors="replace"))
    except OSError as exc:
        return Result("bib-fields", doc, "skipped", f"could not read {bst}: {exc}")
    if not declared:
        return Result("bib-fields", doc, "skipped",
                      f"{bst.name} has no readable ENTRY declaration")

    present: dict[str, int] = {}
    entries = 0
    read: list[str] = []
    for name in bibs:
        path = _find_bib(d, name)
        if path is None:
            return Result("bib-fields", doc, "skipped",
                          f"bibliography {name!r} is declared but no such .bib exists")
        try:
            n, fields = _bib_fields(path.read_text(encoding="utf-8", errors="replace"))
        except OSError as exc:
            return Result("bib-fields", doc, "skipped", f"could not read {path}: {exc}")
        entries += n
        for k, v in fields.items():
            present[k] = present.get(k, 0) + v
        read.append(path.name)

    dropped = [f for f in sorted(present)
               if f not in declared and f not in BIB_BUILTIN]
    where = ", ".join(read)
    findings = [
        Finding("bib-fields", doc, where, 0, "",
                f"{bst.name} does not declare \u201c{f}\u201d, so BibTeX drops it "
                f"from {present[f]} of {entries} entries without a warning")
        for f in dropped if f in BIB_LOCATORS]
    rest = [f for f in dropped if f not in BIB_LOCATORS]
    if rest:
        findings.append(Finding(
            "bib-fields", doc, where, 0, "",
            f"{bst.name} also drops {len(rest)} field"
            f"{'s' if len(rest) != 1 else ''} no style typesets "
            f"({', '.join(rest)}); harmless unless one of them was carrying "
            "something you meant to print"))
    detail = (f"{bst.name} declares {len(declared)} fields; "
              f"{entries} entries carry {len(present)}")
    if via != doc:
        detail += f"; style named by {via}"
    return _result("bib-fields", doc, findings, detail, entries)


def _style_from_class(d: Path, text: str) -> tuple[list[str], str]:
    """The style a document class or a local package sets on the author's behalf.

    Only files sitting beside the manuscript are read. A class installed in the
    TeX tree is not opened: guessing at a distribution's copy is how a check
    starts reporting on a file the compile never used.
    """
    names = [n.strip() for m in CLASS_RE.finditer(text) for n in [m.group(1)]]
    names += [n.strip() for m in PACKAGE_RE.finditer(text)
              for n in m.group(1).split(",")]
    for name in names:
        for suffix in (".cls", ".sty"):
            p = d / (name + suffix)
            if not p.is_file():
                continue
            try:
                body = _uncommented(p.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                continue
            found = [m.group(1).strip() for m in STYLE_RE.finditer(body)
                     if m.group(1).strip()]
            if found:
                return found, p.name
    return [], ""


def _find_bst(d: Path, style: str) -> Path | None:
    """Beside the manuscript first, then wherever TeX would find it."""
    local = d / (style if style.endswith(".bst") else style + ".bst")
    if local.is_file():
        return local
    if shutil.which("kpsewhich") is None:
        return None
    try:
        done = subprocess.run(["kpsewhich", f"{style}.bst"], cwd=str(d),
                              capture_output=True, timeout=30,
                              encoding="utf-8", errors="replace")
    except (OSError, subprocess.SubprocessError):
        return None
    found = done.stdout.strip().splitlines()
    return Path(found[0]) if found and Path(found[0]).is_file() else None


def _find_bib(d: Path, name: str) -> Path | None:
    for candidate in (name, name + ".bib"):
        p = (d / candidate)
        if p.is_file():
            return p
    return None


def _bst_fields(text: str) -> set[str]:
    """The field list a `.bst` declares, from its ENTRY block."""
    body = _uncommented(text)
    m = re.search(r"\bENTRY\b\s*\{([^}]*)\}", body)
    if not m:
        return set()
    return {w.lower() for w in m.group(1).split() if w}


def _bib_fields(text: str) -> tuple[int, dict[str, int]]:
    """How many entries a `.bib` holds and how many carry each field.

    Brace- and quote-aware rather than line-based, because a `.bib` written on
    one line per entry would otherwise report no fields at all -- which would
    make this check pass for exactly the wrong reason.
    """
    entries = 0
    counts: dict[str, int] = {}
    i, n = 0, len(text)
    while i < n:
        if text[i] != "@":
            i += 1
            continue
        m = re.compile(r"@\s*([A-Za-z]+)\s*[{(]").match(text, i)
        if not m:
            i += 1
            continue
        kind = m.group(1).lower()
        i = m.end()
        depth = 1
        seen: set[str] = set()
        expect_field = False       # after the citation key's comma
        while i < n and depth:
            c = text[i]
            if c == "\\":
                i += 2
                continue
            if c in "{(":
                depth += 1
            elif c in "})":
                depth -= 1
            elif c == '"' and depth == 1:
                i += 1
                while i < n and text[i] != '"':
                    i += 2 if text[i] == "\\" else 1
            elif c == "," and depth == 1:
                expect_field = True
            elif expect_field and not c.isspace():
                f = re.compile(r"([A-Za-z][A-Za-z0-9_:.-]*)\s*=").match(text, i)
                if f:
                    seen.add(f.group(1).lower())
                    i = f.end() - 1
                expect_field = False
            i += 1
        if kind in ("string", "comment", "preamble"):
            continue
        entries += 1
        for f in seen:
            counts[f] = counts.get(f, 0) + 1
    return entries, counts


# ------------------------------------------------------------------ shared bits


def _result(check: str, doc: str, findings: list[Finding], detail: str,
            examined: int) -> Result:
    return Result(check, doc, "findings" if findings else "ok", detail,
                  examined, tuple(findings))


def _rel(d: Path, path: Path) -> str:
    try:
        return str(Path(path).resolve().relative_to(d))
    except ValueError:
        return str(path)


def _locate(flat: flat_mod.FlatSource, offset: int) -> tuple[Path, int]:
    try:
        return flat.locate(offset)
    except IndexError:
        return flat.root, 0


def _uncommented(text: str) -> str:
    """`text` with every LaTeX comment blanked, lengths preserved.

    Offsets must survive, because callers map them back to a file and a line.
    """
    out = list(text)
    for i, line in _lines(text):
        at = flat_mod.comment_at(text, i, i + len(line))
        if at is not None:
            for j in range(at, i + len(line)):
                out[j] = " "
    return "".join(out)


def _lines(text: str):
    i = 0
    for line in text.splitlines(keepends=True):
        yield i, line.rstrip("\n")
        i += len(line)


_CMD = re.compile(r"\\[A-Za-z@]+\s*(\[[^\]]*\])?")


def _quote(text: str, at: int, width: int = 220) -> str:
    """Plain words around `at`, for a review comment to anchor on.

    The paragraph is trimmed to its blank lines, LaTeX commands and their
    optional arguments are dropped, and braces are removed, which leaves
    something close to what the reader sees on the page. Best effort by
    construction: the anchoring machinery re-places a quote that has drifted,
    and a quote that matches nothing lands the comment unanchored rather than
    wrong.
    """
    lo = max(text.rfind("\n\n", 0, at) + 2, at - width, 0)
    hi = text.find("\n\n", at)
    hi = min(len(text) if hi < 0 else hi, at + width)
    chunk = _uncommented(text[lo:hi])
    chunk = _CMD.sub(" ", chunk)
    chunk = chunk.replace("{", " ").replace("}", " ").replace("$", " ")
    return " ".join(chunk.split())[:200]


# --------------------------------------------------------------- the reporting


def report(manuscript_dir: Path | str, planned: list[tuple[str, str]],
           results: list[Result]) -> str:
    """The whole message, including the checks that did not happen.

    Silence is the defect this module exists to prevent, so a run that found
    nothing still prints every check by name and says it ran.
    """
    d = Path(manuscript_dir).resolve()
    missing = audit(planned, results)
    lines = [f"preflight on {d.name}:"]
    lines += [r.line() for r in results]
    lines.append("")

    for name in missing:
        lines.append(f" ? {name:<41} NO RESULT  the check did not report at all")
    if missing:
        lines.append("")

    found = sum(len(r.findings) for r in results)
    skipped = [r for r in results if r.status == "skipped"]
    if missing or skipped:
        lines.append(f"{len(skipped) + len(missing)} check"
                     f"{'s' if len(skipped) + len(missing) != 1 else ''} did not run. "
                     "A skipped check is not a pass.")
    if found:
        lines.append(f"{found} finding{'s' if found != 1 else ''}. "
                     "Nothing was modified.")
    elif not skipped and not missing:
        lines.append(f"{len(results)} checks ran, all clean.")
    return "\n".join(lines)


def exit_code(planned: list[tuple[str, str]], results: list[Result]) -> int:
    """0 clean, 1 findings, 2 a check did not run.

    A check that could not run outranks a finding, because it is the failure
    that hides the others.
    """
    if audit(planned, results) or any(r.status == "skipped" for r in results):
        return 2
    return 1 if any(r.findings for r in results) else 0
