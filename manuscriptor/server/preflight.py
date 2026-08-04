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

`bib-doi-links` — the mirror of that one. A style that KEEPS the doi and then
breaks it. `naturemag-doi.bst` wrapped the bare `10.1016/S0140-6736(71)92410-X`
in `\\url{}`, and the class loads hyperref with `colorlinks`, so all 53 entries
became live `/URI` annotations with no scheme -- which a PDF viewer resolves
against the directory the PDF is sitting in. Clicking one asked for
`file:///Users/bbdaniels/Desktop/10.1016/...`. One check catches a style that
throws the DOI away; this one catches a style that keeps it and breaks it, and
neither is visible in a compile that exits 0.

**Check zero, and it is the reason the module is shaped this way.** The sharpest
finding in that memo is that three agents verifying the submission "finished
their work and went idle without reporting anything", and the silence was read
as success. So a check here does not return findings, it returns a `Result` that
says whether it RAN. A check that could not run is `skipped` and is never
rendered as a pass; `audit()` fails when a planned check produced no result at
all. `n/a` is a separate answer and requires a positive determination -- a
document that declares no bibliography has nothing to check, which is not the
same as being unable to tell.

**It reports and does not modify anything**, the same posture as `tidy` --
except when asked, and then it writes in exactly one place. `deliver()` files a
run as review comments in `comments.jsonl`: anchored on the block each finding
concerns, in the `review` state the drain never works, so a session cannot end
up acting on a review it wrote itself. That is what the Checks menu asks for and
what `preflight --review` does; a plain run still touches nothing.

Two things about that delivery are load-bearing and easy to get wrong.
**Identity is not the anchor.** `Finding.key` says which finding this is, so a
second run raises nothing new; the quote says where the comment goes, and
several findings legitimately share one. Deduping on the quote, as this once
did, both re-filed every finding that had no quotable site AND swallowed the
second of two findings about one bibliography. **And the checks that did not run
are delivered too**, or the whole discipline above evaporates at the last step:
findings alone on the page render a skipped check as a clean bill.
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

CHECKS = ("fragments", "exhibit-numbers", "bib-fields", "bib-doi-links")

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

# A BibTeX style builds a field's output by pushing a literal and concatenating
# the field onto it: `"\bibdoi{" doi * "}" *`. The wrapper is in that literal,
# and it is the whole question -- `\bibdoi{` is fine and `\doiprefix\url{` is
# the defect, in styles that are otherwise the same file.
DOI_EMIT_RE = re.compile(r'"([^"]*)"\s*doi\s+\*')
WRAPPER_RE = re.compile(r"\\([A-Za-z@]+)\s*\{\s*$")
DEFINES_RE = (r"\\(?:provide|renew|new)command\s*\*?\s*\{?\s*\\%s\s*\}?"
              r"\s*(?:\[[^\]]*\]\s*)*\{", r"\\def\s*\\%s\s*(?:#\d)*\s*\{")
LOADS_RE = re.compile(r"\\(?:usepackage|RequirePackage)\s*(?:\[[^\]]*\])?\s*\{([^}]*)\}")

# Commands that turn their argument into a link when hyperref is loaded. `\url`
# is the one that did the damage; the others break identically.
LINKING = {"url", "href", "nolinkurl", "path", "hyperref"}

DOI_FIX = r"\providecommand{\bibdoi}[1]{\url{https://doi.org/#1}}"


# --------------------------------------------------------------- the findings


@dataclass(frozen=True)
class Finding:
    """One defect, shaped so it can be filed as an anchored review comment.

    `quote` is what a comment ANCHORS on, and it is block source rather than
    prose, because `match_by_quote` compares against block source (see
    `_Anchors`). It is not the finding's identity: several findings anchor on
    one bibliography, and a paragraph's quote changes the moment it is edited.
    `key` is the identity, and the two are decided in different places on
    purpose. A defect with no site in any document -- a number hand-typed in an
    R script -- carries no quote and waits at the document rather than being
    guessed onto a paragraph.

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
    # What was found at that place: the hand-typed number, the dropped field,
    # the include target. Two findings can share a check, a document, a file
    # and a line -- covet-india's `supplement.tex:192` carries two hand-typed
    # numbers, and every `bib-fields` finding sits at line 0 of the same `.bib`
    # -- so this is the last thing that tells them apart, and without it the
    # second of each pair would be deduped away as a re-filing of the first.
    site: str = ""

    @property
    def key(self) -> str:
        """This finding's identity, for deciding it has already been filed.

        NOT the quote. The quote is where a comment ANCHORS, and the two
        questions come apart in both directions on a real run: a `bib-fields`
        finding has no quotable site at all, so a quote-keyed dedupe filed it
        again on every run into an append-only log, and two findings about one
        bibliography share an anchor, so a quote-keyed dedupe swallowed one of
        them. Composed here, once, rather than by each check: a key invented per
        check is a key that disagrees with itself.

        The body is deliberately not in it. A body carries counts ("39 of 40
        entries"), so keying on it would re-file the same finding as new every
        time the bibliography grew.
        """
        return "|".join((self.check, self.doc, self.file, str(self.line), self.site))

    def as_comment(self) -> dict:
        """The keyword arguments `drain.comment` takes for a review finding.

        Kept here rather than at the call site so the shape is asserted by a
        test instead of discovered later by a UI that does not fit it.
        """
        return {"body": self.body, "quote": self.quote, "key": self.key,
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


def missing(planned: list[tuple[str, str]],
            results: list[Result]) -> list[tuple[str, str]]:
    """Every (check, document) pair the run never reported on at all."""
    got = {(r.check, r.doc) for r in results}
    return [(c, doc) for c, doc in planned if (c, doc) not in got]


def audit(planned: list[tuple[str, str]], results: list[Result]) -> list[str]:
    """What the run failed to say anything about at all. Empty is the good case."""
    return [f"{c} on {doc}" for c, doc in missing(planned, results)]


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
        # Built once per document and handed to every check: it segments the
        # document, and doing that per finding would re-cut a 90KB manuscript
        # for each hand-typed number in it.
        anchors = _Anchors(flat)
        results.append(check_fragments(d, doc, flat, anchors))
        results.append(check_exhibit_numbers(d, doc, flat, anchors))
        results.append(check_bib_fields(d, doc, flat))
        results.append(check_bib_doi_links(d, doc, flat))
    results.append(check_scripts(d))
    return results


# ------------------------------------------------------------ check: fragments


def check_fragments(d: Path, doc: str, flat: flat_mod.FlatSource,
                    anchors: "_Anchors | None" = None) -> Result:
    """Every include target exists and actually says something.

    The measure is what the target CONTRIBUTED to the flattened buffer, taken
    from the flattening walk itself rather than from a second reading of the
    file. That is the only measure that catches all three shapes of nothing: a
    zero-byte fragment, a fragment holding one comment, and a fragment that
    inputs two other empty fragments.
    """
    anchors = anchors or _Anchors(flat)
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
                                anchors.at(dr.flat_start), why,
                                site=f"\\{dr.kind}{{{dr.target}}}"))
    return _result("fragments", doc, findings,
                   f"{len(flat.directives)} include directives",
                   len(flat.directives))


# ------------------------------------------------------ check: exhibit numbers


def check_exhibit_numbers(d: Path, doc: str, flat: flat_mod.FlatSource,
                          anchors: "_Anchors | None" = None) -> Result:
    """A hand-typed exhibit number in prose is a hardcoded result.

    It goes stale the moment the exhibit order changes, silently and with no
    compile error. The rule this enforces is already written down in the
    repository: within one document always `\\ref` a `\\label`, and across
    documents use a generated label map.
    """
    anchors = anchors or _Anchors(flat)
    findings: list[Finding] = []
    text = flat.text
    doubling_matters = bool(S_COUNTER_RE.search(text))

    for m in EXHIBIT_RE.finditer(text):
        if flat_mod.is_commented(text, m.start()):
            continue
        where, line = _locate(flat, m.start())
        findings.append(Finding(
            "exhibit-numbers", doc, _rel(d, where), line,
            anchors.at(m.start()), f"hand-typed \u201c{m.group(0)}\u201d; use \\ref to a \\label, or a "
            "generated label-map macro across documents", site=m.group(0)))

    if doubling_matters:
        for m in DOUBLED_RE.finditer(text):
            if flat_mod.is_commented(text, m.start()):
                continue
            where, line = _locate(flat, m.start())
            findings.append(Finding(
                "exhibit-numbers", doc, _rel(d, where), line,
                anchors.at(m.start()), f"\u201c{m.group(0)}\u201d doubles the S prefix: this document "
                "redefines the counter to carry it already, so this renders "
                "\u201cTable SS19\u201d", site=m.group(0)))

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
                "", f"analysis script emits a hand-typed \u201c{m.group(0)}\u201d; "
                "have it write a label-map macro instead", site=m.group(0)))
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
    style = _style_target(d, doc, "bib-fields", flat)
    if isinstance(style, Result):
        return style
    bst, via, bibs = style.bst, style.via, style.bibs
    try:
        declared = _bst_fields(bst.read_text(encoding="utf-8", errors="replace"))
    except OSError as exc:
        return Result("bib-fields", doc, "skipped", f"could not read {bst}: {exc}")
    if not declared:
        return Result("bib-fields", doc, "skipped",
                      f"{bst.name} has no readable ENTRY declaration")

    counted = _bib_counts(d, doc, "bib-fields", bibs)
    if isinstance(counted, Result):
        return counted
    entries, present, read = counted

    dropped = [f for f in sorted(present)
               if f not in declared and f not in BIB_BUILTIN]
    where = ", ".join(read)
    # The same anchor `bib-doi-links` uses, and for the same reason: the bytes
    # to fix are in the `.bst`, which is not addressable on the page, so the
    # comment goes where the reader SEES the defect. Several of these findings
    # share the anchor -- one per dropped field -- which is exactly why `key`
    # and not the quote decides whether one has been filed before.
    anchor = _bib_quote(flat)
    findings = [
        Finding("bib-fields", doc, where, 0, anchor,
                f"{bst.name} does not declare \u201c{f}\u201d, so BibTeX drops it "
                f"from {present[f]} of {entries} entries without a warning",
                site=f)
        for f in dropped if f in BIB_LOCATORS]
    rest = [f for f in dropped if f not in BIB_LOCATORS]
    if rest:
        findings.append(Finding(
            "bib-fields", doc, where, 0, anchor,
            f"{bst.name} also drops {len(rest)} field"
            f"{'s' if len(rest) != 1 else ''} no style typesets "
            f"({', '.join(rest)}); harmless unless one of them was carrying "
            "something you meant to print", site="(fields no style typesets)"))
    detail = (f"{bst.name} declares {len(declared)} fields; "
              f"{entries} entries carry {len(present)}")
    if via != doc:
        detail += f"; style named by {via}"
    return _result("bib-fields", doc, findings, detail, entries)


@dataclass(frozen=True)
class _Style:
    """The `.bst` a document's bibliography actually goes through."""

    bst: Path
    via: str            # the file that named the style: the doc, or its class
    name: str
    bibs: tuple[str, ...]


def _style_target(d: Path, doc: str, check: str,
                  flat: flat_mod.FlatSource) -> _Style | Result:
    """One resolver, for every check that needs to know the style.

    `bib-fields` and `bib-doi-links` ask the same question of the same files and
    must never answer it twice: the class path in particular is subtle and was
    written for a real manuscript that names no style in its `.tex` at all, so a
    second copy would sooner or later stop following it.

    Returns the `Result` the caller should hand back when the question cannot be
    reached -- `n/a` when there is positively nothing to check, `skipped` when
    it could not be determined, and never `ok` for either.
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

    if not bibs and not (ADDRESOURCE_RE.search(text) or BIBLATEX_RE.search(text)):
        # Whether a style was found is beside the point: a document that never
        # says `\bibliography` prints none, so there is nothing for a `.bst` to
        # get right or wrong. covet-india's `supplement.tex` cites nothing, and
        # because `wlscirep.cls` names the style anyway both checks reported on
        # its non-existent bibliography -- one of them filing a finding about
        # links that do not exist.
        return Result(check, doc, "n/a", "the document declares no bibliography")
    if not styles and (ADDRESOURCE_RE.search(text) or BIBLATEX_RE.search(text)):
        return Result(check, doc, "n/a",
                      "biblatex: this is the .bbx's business, not a .bst's")
    if not styles:
        return Result(check, doc, "skipped",
                      "a bibliography is declared but no \\bibliographystyle is, "
                      "here or in the class")

    bst = _find_bst(d, styles[0])
    if bst is None:
        return Result(check, doc, "skipped",
                      f"{styles[0]}.bst not beside the manuscript and kpsewhich "
                      "could not find it")
    return _Style(bst, via, styles[0], tuple(bibs))


def _bib_counts(d: Path, doc: str, check: str, bibs: tuple[str, ...] | list[str]):
    """How many entries the declared `.bib` files hold, and what fields they carry."""
    present: dict[str, int] = {}
    entries = 0
    read: list[str] = []
    for name in bibs:
        path = _find_bib(d, name)
        if path is None:
            return Result(check, doc, "skipped",
                          f"bibliography {name!r} is declared but no such .bib exists")
        try:
            n, fields = _bib_fields(path.read_text(encoding="utf-8", errors="replace"))
        except OSError as exc:
            return Result(check, doc, "skipped", f"could not read {path}: {exc}")
        entries += n
        for k, v in fields.items():
            present[k] = present.get(k, 0) + v
        read.append(path.name)
    return entries, present, read


def _local_inputs(d: Path, text: str) -> list[tuple[str, str]]:
    """Every class and package beside the manuscript, as (name, body).

    Only files sitting beside the manuscript are read. A class installed in the
    TeX tree is not opened: guessing at a distribution's copy is how a check
    starts reporting on a file the compile never used.
    """
    names = [n.strip() for m in CLASS_RE.finditer(text) for n in [m.group(1)]]
    names += [n.strip() for m in PACKAGE_RE.finditer(text)
              for n in m.group(1).split(",")]
    out: list[tuple[str, str]] = []
    for name in names:
        for suffix in (".cls", ".sty"):
            p = d / (name + suffix)
            if not p.is_file():
                continue
            try:
                out.append((p.name, _uncommented(
                    p.read_text(encoding="utf-8", errors="replace"))))
            except OSError:
                continue
    return out


def _style_from_class(d: Path, text: str) -> tuple[list[str], str]:
    """The style a document class or a local package sets on the author's behalf."""
    for name, body in _local_inputs(d, text):
        found = [m.group(1).strip() for m in STYLE_RE.finditer(body)
                 if m.group(1).strip()]
        if found:
            return found, name
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


# ------------------------------------------------------- check: bib doi links


def check_bib_doi_links(d: Path, doc: str, flat: flat_mod.FlatSource) -> Result:
    """The DOI reaches a link, and the link carries a resolver.

    `\\url{10.1016/S0140-6736(71)92410-X}` under hyperref is a `/URI` annotation
    with no scheme, and a PDF viewer resolves that relative to the PDF's own
    directory. Fifty-three of them shipped that way, blue and clickable and all
    pointing at files that do not exist. Nothing in a compile says so.

    The two styles that get it right both emit a semantic macro and define it
    WITH the prefix, so the emission alone cannot be judged: `\\doi{}` is
    correct in dsp-bias only because `main.tex:11` defines it as
    `\\url{https://doi.org/#1}`, and the identical `.bst` in a document
    defining it as `\\url{#1}` is the same defect under a nicer name. So the
    macro is followed to its definition -- into the `.bbl` preamble the style
    writes, into the document, into a class beside it -- and when no definition
    can be found this reports `skipped`, because "I could not tell" said as
    "ok" is the failure this module exists to prevent.
    """
    check = "bib-doi-links"
    style = _style_target(d, doc, check, flat)
    if isinstance(style, Result):
        return style
    bst, via = style.bst, style.via
    try:
        bst_text = bst.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return Result(check, doc, "skipped", f"could not read {bst}: {exc}")

    declared = _bst_fields(bst_text)
    if not declared:
        return Result(check, doc, "skipped",
                      f"{bst.name} has no readable ENTRY declaration")
    if "doi" not in declared:
        # estonia-ecm's `aea.bst`. BibTeX never hands the style a doi it did not
        # ask for, so there is positively nothing here that could be linked.
        return Result(check, doc, "n/a",
                      f"{bst.name} does not declare the doi field, so it emits none")

    sites = _doi_sites(bst_text)
    if not sites:
        # Declared but not emitted by any idiom this can read. That is not the
        # same as not emitted, and answering n/a here would hand the clean
        # verdict out for free to exactly the styles this cannot parse.
        return Result(check, doc, "skipped",
                      f"{bst.name} declares doi but emits it by no recognisable "
                      "idiom; check by hand what wraps it")

    counted = _bib_counts(d, doc, check, style.bibs)
    if isinstance(counted, Result):
        return counted
    entries, present, read = counted
    with_doi = present.get("doi", 0)
    if style.bibs and not with_doi:
        return Result(check, doc, "n/a",
                      f"no entry in {', '.join(read) or 'the bibliography'} carries "
                      f"a doi, of {entries}")

    hyper = _loads_hyperref(d, flat)
    verdicts = [(lit, line, _doi_verdict(lit, _defining(d, doc, flat, bst, bst_text)))
                for lit, line in sites]
    unknown = [(lit, v) for lit, _, v in verdicts if v[0] == "unknown"]
    broken = [(lit, line, v) for lit, line, v in verdicts if v[0] == "broken"]

    detail = f"{bst.name} links the doi as {sites[0][0]!r}"
    if via != doc:
        detail += f"; style named by {via}"
    if with_doi:
        detail += f"; {with_doi} of {entries} entries carry one"
    if not hyper:
        detail += "; no hyperref"

    names = ", ".join(sorted({f"\\{v[1]}" for _, v in unknown}))
    if unknown and not broken:
        return Result(check, doc, "skipped",
                      f"{bst.name} wraps the doi in {names}, and no definition of "
                      f"it is beside the manuscript -- in {bst.name}, in {doc}, or "
                      "in a class here. It may carry the resolver prefix or may "
                      "not, and this cannot tell which")
    if not broken:
        ok_why = verdicts[0][2][1]
        return Result(check, doc, "ok", f"{detail}; {ok_why}", entries)

    # A definite defect outranks an undetermined one HERE, which is the reverse
    # of the module's usual order, and only because nothing is hidden by it: a
    # site this could not judge is named in the detail and in the finding, so
    # the reader is told about it either way. Reporting `skipped` instead would
    # produce no `Finding` at all, and a real broken bibliography would go
    # unfiled because some other emission site was unreadable.
    #
    # One finding per document however many sites: `drain.comment` dedupes on
    # (quote, author, doc), so two findings anchored on the same bibliography
    # would silently swallow each other.
    lit, line, (_, why) = broken[0]
    more = (f" ({len(broken)} emission sites)" if len(broken) > 1 else "")
    if unknown:
        more += (f", and {len(unknown)} more wrapping it in {names}, which "
                 "nothing beside the manuscript defines -- check those by hand")
        detail += f"; {len(unknown)} site(s) could not be judged"
    count = f"{with_doi} of {entries} entries" if entries else "every entry"
    if hyper:
        harm = (f"hyperref is loaded, so the doi link on {count} has no scheme, "
                "and a PDF viewer resolves that against the directory the PDF "
                "is sitting in (file:///.../10.1016/...) rather than against "
                "doi.org")
    else:
        harm = (f"no hyperref is loaded here, so nothing is clickable on {count} "
                "and the doi merely prints without a resolver -- but "
                "the style is still "
                "wrong, and loading hyperref, which a journal class routinely "
                "does for you, arms every one of these silently")
    body = (f"{bst.name} emits {lit!r}{more}: {why}, with no http scheme. "
            f"{harm}. Emit a semantic macro and define it with the prefix, as "
            f"aer-doi.bst does: {DOI_FIX}")
    finding = Finding(check, doc, _rel(d, bst), line,
                      _bib_quote(flat), body, site=lit)
    return Result(check, doc, "findings", detail, entries, (finding,))


def _doi_sites(bst_text: str) -> list[tuple[str, int]]:
    """Every distinct literal the style concatenates the doi onto, and its line."""
    out: list[tuple[str, int]] = []
    seen: set[str] = set()
    body = _uncommented(bst_text)
    for m in DOI_EMIT_RE.finditer(body):
        lit = m.group(1)
        if lit in seen:
            continue
        seen.add(lit)
        out.append((lit, body.count("\n", 0, m.start()) + 1))
    return out


def _doi_verdict(literal: str, sources: list[tuple[str, str]]) -> tuple[str, str]:
    """Whether this emission reaches a link that a reader can follow.

    ("ok" | "broken" | "unknown", the reason, or the macro name when unknown).
    """
    if "http" in literal.lower():
        return ("ok", "the resolver prefix is in the style's own literal")
    m = WRAPPER_RE.search(literal)
    if not m:
        return ("ok", "the doi is typeset as text and never linked")
    name = m.group(1)
    if name in LINKING:
        return ("broken", f"\\{name} is applied to the bare doi")
    for where, text in sources:
        body = _macro_body(text, name)
        if body is None:
            continue
        if "http" in body.lower():
            return ("ok", f"\\{name} is defined in {where} with the resolver prefix")
        if re.search(r"\\(?:%s)\b" % "|".join(sorted(LINKING)), body):
            return ("broken",
                    f"\\{name} is defined in {where} as {body!r}, which links the "
                    "bare doi")
        return ("ok", f"\\{name} is defined in {where} and links nothing")
    return ("unknown", name)


def _defining(d: Path, doc: str, flat: flat_mod.FlatSource, bst: Path,
              bst_text: str) -> list[tuple[str, str]]:
    """Where a definition of the style's DOI macro could be, nearest first.

    The `.bst` first, because a style that writes its own `\\providecommand`
    into the `.bbl` preamble carries its definition wherever it is used; then
    the document, then a class or package beside it.
    """
    text = _uncommented(flat.text)
    return ([(bst.name, bst_text), (doc, text)] + _local_inputs(d, text))


def _macro_body(text: str, name: str) -> str | None:
    """The replacement text of `\\name`, from whichever way it was defined."""
    for pat in DEFINES_RE:
        m = re.search(pat % re.escape(name), text)
        if m:
            return _braced(text, m.end() - 1)
    return None


def _braced(text: str, at: int) -> str | None:
    """What sits inside the brace group opening at `at`."""
    if at >= len(text) or text[at] != "{":
        return None
    depth, i = 0, at
    while i < len(text):
        c = text[i]
        if c == "\\":
            i += 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[at + 1:i]
        i += 1
    return None


def _loads_hyperref(d: Path, flat: flat_mod.FlatSource) -> bool:
    """Whether anything readable from here turns `\\url` into a link.

    Best effort and deliberately local: covet-india loads it in `wlscirep.cls`
    line 46 and never mentions it in `main.tex`, so the document alone is not
    the question. A class in the TeX tree is not opened, so a false negative is
    possible -- which is why the no-hyperref case is still reported and not
    waved through.
    """
    text = _uncommented(flat.text)
    for _, body in [("", text)] + _local_inputs(d, text):
        for m in LOADS_RE.finditer(body):
            if any(p.strip() == "hyperref" for p in m.group(1).split(",")):
                return True
    return False


def _bib_quote(flat: flat_mod.FlatSource) -> str:
    """Something from the document for the finding to anchor on.

    The bytes to fix are in the `.bst`, which is not the document and not
    addressable in it, so the comment anchors where the reader SEES the defect:
    the bibliography. Both bibliography checks use this, so several findings
    share one anchor; that is fine and is why `Finding.key` rather than the
    quote decides whether a finding has been filed before. (It did not used to
    be fine: dedupe was on the quote, so `bib-fields` findings, carrying none,
    were re-filed on every run, and two findings sharing this one would have
    swallowed each other.)

    The directive itself, NOT `_quote`'s stripped prose. `match_by_quote`
    compares against block source, and the bibliography is a command rather
    than a sentence, so stripping it leaves the bare `.bib` name -- "sample" for
    covet-india, a word that manuscript uses on nearly every page. A quote that
    matches nothing lands the comment unanchored; a quote that matches the wrong
    paragraph lands it wrong.
    """
    for pattern in (BIBFILES_RE, ADDRESOURCE_RE, STYLE_RE):
        m = pattern.search(flat.text)
        if m:
            return " ".join(m.group(0).split())
    return "bibliography"


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


class _Anchors:
    """Which block of the document an offset falls in, said as a quote.

    THE QUOTE IS BLOCK SOURCE, not the prose a reader sees. `match_by_quote`,
    which places every comment in this program, compares against a block's
    source with the whitespace flattened; a quote stripped of its LaTeX matched
    only when the paragraph happened to open with plain words, and a paragraph
    sitting directly under a `\\section` had the heading's words folded into its
    quote -- the two are one blank-line-delimited chunk of the file and two
    blocks -- so the finding anchored nowhere at all. That was the whole of the
    delivery: the check was right, the comment was right, and it landed in the
    tray because the anchor had been rewritten into something the page does not
    contain.

    So the block boundaries come from `blocks.segment`, the one segmenter, and
    how much of a block identifies it comes from `build.quote_for`, the one
    answer to that question -- grown until no other block contains it, which is
    what stops twelve identically-opening table files from swallowing each
    other's comments. Neither rule is re-derived here.

    An offset in no block at all (the preamble, the space between two floats)
    has no quote, and says so. An unanchored finding waits at the document,
    which is the answer the tray gives an unplaceable reviewer note; a guessed
    paragraph is the one outcome that is worse than that.
    """

    def __init__(self, flat: flat_mod.FlatSource):
        # Imported here rather than at the top of the module: `build` pulls in
        # the render path, and `plan`/`report` have no use for any of it.
        from manuscriptor.server import build as build_mod
        from manuscriptor.source import blocks as blocks_mod

        self._blocks = blocks_mod.segment(flat)
        self._quote_for = build_mod.quote_for
        self._cache: dict[str, str] = {}

    def at(self, offset: int) -> str:
        for blk in self._blocks:
            if blk.flat_start <= offset < blk.flat_end:
                if blk.id not in self._cache:
                    self._cache[blk.id] = self._quote_for(blk, self._blocks)
                return self._cache[blk.id]
        return ""


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


FILED_BY = "preflight"


def deliverable(planned: list[tuple[str, str]],
                results: list[Result]) -> list[Finding]:
    """Everything a run owes the author, findings AND the checks that did not run.

    The second half is not a nicety. This module's whole discipline is that a
    skipped check never renders as a pass, and delivering only findings renders
    it as exactly that: the author reads an empty margin and calls the
    bibliography clean, which is the failure the preflight memo is about.

    A finding from the scripts sweep belongs to no one document -- an R file
    printing "Table~S19" is a defect of the project -- and is filed with no
    document, which is how the log says "whichever one is being read".
    """
    out = [f for r in results for f in r.findings]
    for r in results:
        if r.status != "skipped":
            continue
        out.append(Finding(
            r.check, "" if r.doc == SCRIPTS else r.doc, "", 0, "",
            f"the {r.check} check could not run on {_where(r.doc)}: {r.detail}. "
            "A skipped check is not a pass, so nothing here has been cleared.",
            site="did not run"))
    for check, doc in missing(planned, results):
        out.append(Finding(
            check, "" if doc == SCRIPTS else doc, "", 0, "",
            f"the {check} check reported nothing at all on {_where(doc)}, not "
            "even that it was skipped. Nothing here has been checked.",
            site="no result"))
    return out


def _where(doc: str) -> str:
    return "the analysis scripts" if doc == SCRIPTS else (doc or "this manuscript")


def deliver(manuscript_dir: Path | str, *, main: str | None = None,
            planned: list[tuple[str, str]] | None = None,
            results: list[Result] | None = None,
            author: str = FILED_BY) -> list[dict]:
    """File a run as review comments, and answer with the ones that were new.

    `review` is the state the drain never works, so these are pinned and
    readable at once without an agent ever being handed its own review as
    instructions. Anchoring is by quote, through the same `match_by_quote` every
    other comment goes through; a finding whose quote matches nothing waits at
    the document rather than being guessed onto a paragraph.

    Nothing else here writes anything, which is why this is a separate verb and
    not something `run` does. `manuscriptor preflight` still reports and
    modifies nothing; `--review`, and the toolbar, are the ways to ask for it.
    """
    # Imported here, not at the top: `drain` pulls in the whole build, and a
    # report-only run of this module has no business paying for it.
    from manuscriptor.server import drain

    d = Path(manuscript_dir).resolve()
    if planned is None:
        planned = plan(d, main)
    if results is None:
        results = run(d, main)
    filed: list[dict] = []
    for f in deliverable(planned, results):
        rec = drain.comment(d, author=author, **f.as_comment())
        if rec is not None:
            filed.append(rec)
    return filed


def exit_code(planned: list[tuple[str, str]], results: list[Result]) -> int:
    """0 clean, 1 findings, 2 a check did not run.

    A check that could not run outranks a finding, because it is the failure
    that hides the others.
    """
    if audit(planned, results) or any(r.status == "skipped" for r in results):
        return 2
    return 1 if any(r.findings for r in results) else 0
