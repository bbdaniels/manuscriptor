"""Insertion — the coordinated multi-file writes, planned then applied.

A text editor can only ever write the manuscript line. That is the argument for
this tool existing, and it is what this module does:

  A CITATION touches three places: the `\\citep` at the cursor, an entry in the
  `.bib`, and a Zotero record if the paper is new.
  A NUMBER touches three: a new fragment file, a line in the script that
  computes it, and the `\\input` at the cursor.
  AN EXHIBIT touches four, and the fourth matters most: the runfile line,
  without which the exhibit goes stale on the next rebuild and nobody notices
  until a referee does.

Four things here are load bearing.

**There is no field that accepts a literal number.** A value enters the
manuscript only as an `\\input` of a file some script wrote, so `plan_value`
takes an EXPRESSION and refuses one that is a number wearing a costume: `0.32`,
`32%`, `$0.32$`, `\\num{0.32}`. Inserting a number is a change to analysis code.
The fragment the plan creates is a placeholder carrying no digit at all, because
a plausible-looking placeholder is a hardcoded result by another name.

**Planning and applying are two calls, and the plan does not travel.** The page
is shown a plan and confirms it by token; the token is redeemed against the plan
this process is holding. Were the plan posted back, the form would become the
literal-number field this module exists to not have.

**A failed step rolls back the ones before it.** Every file the plan touches is
snapshotted, the block splice is performed last because it is the step most
likely to refuse, and a Zotero record created moments earlier is removed again.

**The block write goes through `splice`, always.** It holds the per-file lock
and the staleness check, so nothing here serializes anything of its own.

Nothing in this module calls a model. Crossref, OpenAlex and Zotero are
catalogues.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from manuscriptor.server import producers
from manuscriptor.source import splice as splice_mod

HOLDER = "author"


class LibraryError(Exception):
    """Zotero would not take the record."""


# --------------------------------------------------------------- the vocabulary


NA = "n/a"


@dataclass(frozen=True)
class Check:
    """One thing that had to be true. `blocking` decides whether it stops the write.

    `status` exists because a row that cannot apply must not render as a tick.
    The gate asks for a canonical DOI on which Crossref and OpenAlex agree, and
    a book has none of those things -- Crossref does not resolve a work with no
    DOI, so "Crossref agrees" is not a pass, it is a question that was never
    asked. `n/a` is a POSITIVE determination and nothing else may use it: the
    library record was read and it records no DOI. Not knowing is still a
    failure, and is still reported as one. Same discipline as preflight, where a
    skipped check never renders as a pass.
    """

    name: str
    ok: bool
    detail: str
    blocking: bool = True
    status: str = ""

    @property
    def state(self) -> str:
        return self.status or ("pass" if self.ok else "fail")


@dataclass
class Write:
    """One file change, previewed exactly as it will land.

    `preview` is the text that will be ADDED, verbatim, and is what the author
    confirms. `context` is the wider view for the panel and is never written.
    """

    path: Path
    kind: str                       # create | append | runfile | splice
    label: str
    preview: str
    context: str = ""
    block: object = None            # splice only
    new_source: str | None = None   # splice only

    def rel(self, root: Path) -> str:
        try:
            return str(self.path.resolve().relative_to(Path(root).resolve().parent))
        except ValueError:
            return str(self.path)


@dataclass
class Plan:
    kind: str
    checks: tuple[Check, ...] = ()
    writes: tuple[Write, ...] = ()
    blocked: str | None = None
    summary: str = ""
    cite_key: str = ""
    library_add: str | None = None      # a DOI to import, or None
    rerun: str = ""                     # what the author must run afterwards

    @property
    def ok(self) -> bool:
        return self.blocked is None and all(c.ok for c in self.checks if c.blocking)

    @property
    def failed_check(self) -> str | None:
        for c in self.checks:
            if c.blocking and not c.ok:
                return c.name
        return None

    def to_json(self, root: Path) -> dict:
        return {
            "kind": self.kind,
            "ok": self.ok,
            "blocked": self.blocked,
            "failed_check": self.failed_check,
            "summary": self.summary,
            "cite_key": self.cite_key,
            "rerun": self.rerun,
            "library_add": self.library_add,
            "checks": [{"name": c.name, "ok": c.ok, "detail": c.detail,
                        "blocking": c.blocking, "state": c.state} for c in self.checks],
            "writes": [{"path": w.rel(root), "kind": w.kind, "label": w.label,
                        "preview": w.preview, "context": w.context} for w in self.writes],
        }


@dataclass
class Result:
    ok: bool
    wrote: list[str] = field(default_factory=list)
    error: str = ""
    rerun: str = ""


# ------------------------------------------------------- the plan, held here

_PENDING: dict[str, Plan] = {}


def stash(plan: Plan) -> str:
    """Hold a plan and return the token that redeems it.

    The token is the confirmation, and it is single use: a double-click on
    "write it" must not write twice.
    """
    token = secrets.token_urlsafe(12)
    _PENDING[token] = plan
    if len(_PENDING) > 64:                       # a long session, not a leak
        for stale in list(_PENDING)[:32]:
            _PENDING.pop(stale, None)
        _PENDING[token] = plan
    return token


def take(token: str) -> Plan | None:
    return _PENDING.pop(str(token or ""), None)


# =============================================================== the literal rule

_NUM_WRAPPERS = re.compile(r"\\(?:num|SI|si|numprint|nombre)\s*\{([^{}]*)\}(?:\s*\{[^{}]*\})?")
_UNIT_WORDS = re.compile(
    r"\b(?:percent|percentage|points?|pp|sd|sds|std|dev|deviations?|log|logs)\b", re.I
)


def is_literal(expression: str) -> str | None:
    """Why this expression is a typed number, or None if it computes something.

    Deliberately generous about what counts as a literal, because the failure it
    guards is a result being typed into a manuscript and never checked again.
    """
    raw = (expression or "").strip()
    if not raw:
        return "nothing to compute: an expression is required, not a value"

    if not re.search(r"[A-Za-z]", raw):
        return f"{raw!r} is a literal, not an expression. A value may only enter the manuscript as an \\input of a file some script wrote."

    bare = raw
    for _ in range(3):
        bare = _NUM_WRAPPERS.sub(r"\1", bare)
    bare = bare.replace("$", "").replace("\\%", "").replace("%", "")
    bare = bare.replace("\\,", "").replace("\\ ", " ")
    bare = _UNIT_WORDS.sub("", bare)
    bare = bare.replace(",", "").strip().strip("()[]{}").strip()
    if re.fullmatch(r"[-+]?(?:\d+\.?\d*|\.\d+)", bare):
        return f"{raw!r} is a literal, not an expression. A value may only enter the manuscript as an \\input of a file some script wrote."
    return None


# ================================================================ 1. a citation


def plan_citation(
    *,
    query: str,
    block,
    caret: int,
    root: Path,
    bib_path: Path | None,
    net=None,
    library=None,
    base: str | None = None,
    key: str | None = None,
    beside: str | None = None,
    produced: dict | None = None,
) -> Plan:
    """Search the library first, then Crossref and OpenAlex, then gate.

    The author's standing rule is that every citation carries a canonical DOI on
    which Crossref and OpenAlex agree, and has a Zotero record. A key failing any
    of those is not inserted and the page is told which one failed.
    """
    root = Path(root).resolve()
    net = net or HttpCatalogue()
    library = library if library is not None else ZoteroLibrary()

    stale = _stale(block, base)
    if stale:
        return Plan(kind="citation", blocked=stale)

    # THE BIBLIOGRAPHY FIRST, AND WITHOUT THE GATE.
    #
    # The identity gate exists to vet a source the manuscript has never used: a
    # canonical DOI, Crossref and OpenAlex agreeing, a Zotero record. An entry
    # already in `references.bib` passed that when it was added, so making the
    # author search three web services to cite a paper their own paper already
    # cites is a round trip that can only fail. Reported 2026-07-26, on a key
    # (`zhang2026ai`) that was in the file the whole time.
    held_entries = read_bib_entries(bib_path)
    matches, how = match_bib(held_entries, query)
    if len(matches) > 1:
        return Plan(kind="citation", blocked=(
            f"{len(matches)} entries in your bibliography match that: "
            + ", ".join(sorted(matches)[:6])
            + ". Give the cite key of the one you mean."
        ))
    if len(matches) == 1:
        cite_key = matches[0]
        meta = dict(held_entries[cite_key], key=cite_key)
        plan = Plan(kind="citation", checks=(Check(
            "bibliography", True,
            f"already in {Path(bib_path).name if bib_path else 'your bibliography'} "
            f"as {cite_key}, found by {how}. Nothing to look up.",
        ),))
        plan.cite_key = cite_key
        write = _citation_write(block, caret, beside, cite_key, root)
        if isinstance(write, str):
            plan.blocked = write
            plan.summary = write
            return plan
        plan.writes = (write,)
        plan.summary = f"{meta.get('title') or cite_key} — {cite_key}, already in your bibliography."
        return plan

    doi, seed, held_lib = _candidate_doi(query, net, library)
    from_library = held_lib is not None

    checks: list[Check] = []

    # NOTHING MAY BE APPENDED TO A GENERATED BIBLIOGRAPHY, and the author is
    # told so before any of the identity work, because no answer to it can end
    # in a write to that file. covet-india's `sample.bib` is exported from
    # Zotero; an entry appended here dies at the next `make bib` while the
    # `\citep{...}` in main.tex survives, which is a broken build the author did
    # not cause and cannot see coming.
    refusal = _generated_bib(bib_path, root, produced)
    if refusal:
        checks.append(Check("bibliography", False, refusal))

    # A BOOK HAS NO DOI, AND THAT IS AN ANSWER RATHER THAN A GAP. The gate used
    # to find the author's own copy in Zotero and then throw it away for having
    # no DOI, falling through to a Crossref search that could only offer near
    # misses -- three attempts at Rogers' *Diffusion of Innovations* returned
    # three different wrong works, one of them Rogers' own 1993 chapter. When
    # the library holds the work, that IS identity; `match` has said so all
    # along.
    no_doi_on_record = from_library and not doi
    if no_doi_on_record:
        checks.append(Check("doi", True, _no_doi_detail(seed, held_lib), status=NA))
    else:
        checks.append(Check(
            "doi", bool(doi),
            f"{doi}" if doi
            else f"nothing with a DOI matched {query!r} in your library, Crossref or OpenAlex",
        ))

    if no_doi_on_record:
        # Not run, and said so. A tick here would claim a corroboration that
        # never happened.
        cross = alex = None
        agree = False
        for name in ("crossref", "openalex"):
            checks.append(Check(
                name, True,
                f"{name.capitalize() if name == 'crossref' else 'OpenAlex'} resolves DOIs, "
                "and this work has none; it was not asked",
                status=NA))
        checks.append(Check(
            "agreement", True,
            "neither catalogue was asked, so there is nothing for them to agree on; "
            "your library record is the identity instead",
            status=NA))
    else:
        cross = net.crossref_by_doi(doi) if doi else None
        checks.append(Check(
            "crossref", bool(cross),
            (cross or {}).get("title", "") if cross else "Crossref does not resolve this DOI",
        ))
        alex = net.openalex_by_doi(doi) if doi else None
        checks.append(Check(
            "openalex", bool(alex),
            (alex or {}).get("title", "") if alex else "OpenAlex does not resolve this DOI",
        ))
        agree = bool(cross and alex and _titles_agree(cross.get("title"), alex.get("title")))
        checks.append(Check(
            "agreement", agree,
            "Crossref and OpenAlex name the same work" if agree else
            (f"Crossref calls it {cross.get('title')!r}; OpenAlex calls it {alex.get('title')!r}"
             if cross and alex else "both catalogues have to answer before they can agree"),
        ))

    meta = dict(cross or seed or {})
    if doi:
        meta["doi"] = doi

    # IDENTITY IS NOT RELEVANCE, and finding that out cost nothing only because
    # the real catalogues were run rather than a fake. Crossref answers every
    # query with something: a deliberately nonsense search returned a real,
    # resolvable DOI on which Crossref and OpenAlex agreed, so all four checks
    # above passed on a book chapter about colonial legacies. A plausible wrong
    # citation is worse than no citation, because nobody re-reads the ones that
    # look fine.
    typed_doi = bool(_DOI_RE.search(query or ""))
    score = _relevance(query, meta)
    matched = typed_doi or from_library or score >= 0.5
    checks.append(Check(
        "match", matched,
        "you gave the DOI, so there is nothing to match against" if typed_doi else
        ("it is the record already in your library" if from_library else
         (f"{meta.get('title') or 'the candidate'} answers what you asked for" if matched else
          f"the closest thing either catalogue offers is {meta.get('title')!r}, which is not what "
          f"you asked for. Give a DOI, or more of the title."))))

    held = held_lib or (library.find(doi=doi) if doi else None)
    library_add = None
    if held:
        checks.append(Check("zotero", True, f"already in your library as {held.get('key')}"))
        checks.append(Check(
            "fulltext", bool(held.get("has_fulltext")),
            "indexed fulltext is present" if held.get("has_fulltext")
            else "no indexed fulltext yet, so the evidence pass cannot quote it",
            blocking=False,
        ))
    elif agree:
        library_add = doi
        checks.append(Check("zotero", True, f"not in your library; it will be imported by DOI {doi}"))
        checks.append(Check(
            "fulltext", False,
            "a record imported a moment ago has no PDF indexed yet; run the evidence pass once Zotero has fetched one",
            blocking=False,
        ))
    else:
        checks.append(Check("zotero", False, "not looked up, because the DOI did not survive the checks above"))

    plan = Plan(kind="citation", checks=tuple(checks), library_add=library_add)
    if not plan.ok:
        plan.blocked = _first_failure(checks)
        return plan

    # The bib, and the key. A DOI already in the file keeps the key it has,
    # because two keys for one paper is how a bibliography starts to rot.
    entries = read_bib(bib_path) if bib_path else {}
    existing = _bib_key_for_doi(entries, doi)
    # Zotero's own citation key wins over a minted one. covet-india's generator
    # resolves every cited key back to exactly one library record and fails if
    # it cannot, so a key invented here is one `make bib` can never satisfy.
    cite_key = key or existing or meta.get("citation_key") or bib_key(meta, taken=entries)
    plan.cite_key = cite_key

    writes: list[Write] = []
    if not existing and bib_path is not None:
        entry = bib_entry(cite_key, meta)
        writes.append(Write(
            path=Path(bib_path), kind="append",
            label=f"an entry appended to {Path(bib_path).name}",
            preview=entry,
        ))
    write = _citation_write(block, caret, beside, cite_key, root)
    if isinstance(write, str):
        plan.blocked = write
        plan.summary = write
        return plan
    writes.append(write)

    plan.writes = tuple(writes)
    plan.summary = (
        f"{meta.get('title') or query} — {cite_key}. "
        + ("The entry is already in your bibliography. " if existing else "")
        + ("Importing it into Zotero first." if library_add else "Already in your library.")
    )
    return plan


# ================================================================== 2. a number


def plan_value(
    *,
    key: str,
    description: str,
    expression: str,
    script: Path,
    block,
    caret: int,
    root: Path,
    produced: dict | None = None,
    base: str | None = None,
    fragment_dir: str | None = None,
) -> Plan:
    """A quantity the analysis code computes, wired into the manuscript.

    Three writes: the fragment file, the line in the producing script that fills
    it, and the `\\input` at the cursor. The code is written, never run: running
    an author's analysis can cost hours and touch data this process has no
    business reading, so the plan says what to re-run and stops.
    """
    root = Path(root).resolve()
    return _plan_computed(
        kind="value", key=key, description=description, expression=expression,
        script=script, block=block, caret=caret, root=root, produced=produced or {},
        base=base, out_dir=fragment_dir or _fragment_dir(root),
        directive_only=True,
    )


# ================================================================= 3. an exhibit


def plan_exhibit(
    *,
    key: str,
    caption: str,
    expression: str,
    script: Path,
    block,
    root: Path,
    produced: dict | None = None,
    kind: str = "table",
    label: str | None = None,
    runfile: Path | None = None,
    base: str | None = None,
    exhibit_dir: str | None = None,
) -> Plan:
    """A float minted after this paragraph, plus the code and the runfile line.

    The runfile line is the one that matters. An exhibit whose script nothing
    calls is correct on the day it is written and stale on every day after.
    """
    root = Path(root).resolve()
    return _plan_computed(
        kind="exhibit", key=key, description=caption, expression=expression,
        script=script, block=block, caret=None, root=root, produced=produced or {},
        base=base, out_dir=exhibit_dir or _exhibit_dir(root, kind),
        directive_only=False, caption=caption, label=label or f"tab:{key}",
        float_kind=kind, runfile=runfile,
    )


# ------------------------------------------------------------------ the shared half


def _plan_computed(
    *, kind, key, description, expression, script, block, caret, root, produced,
    base, out_dir, directive_only, caption="", label="", float_kind="table",
    runfile=None,
) -> Plan:
    stale = _stale(block, base)
    if stale:
        return Plan(kind=kind, blocked=stale)

    key = (key or "").strip()
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_\-]*", key):
        return Plan(kind=kind, blocked=(
            "A fragment needs a name made of letters, digits, underscores and dashes: "
            f"{key!r} is not one."))

    why = is_literal(expression)
    if why:
        return Plan(kind=kind, blocked=why)

    script = Path(script).resolve()
    if script not in known_scripts(root) and script not in set(produced.values()):
        return Plan(kind=kind, blocked=(
            f"{script} is not one of this project's analysis scripts, so nothing here will "
            "write to it. Pick a script that already produces part of this manuscript."))

    recipe = _RECIPES.get(script.suffix)
    if recipe is None:
        return Plan(kind=kind, blocked=(
            f"No recipe for writing a fragment from a {script.suffix} script, and guessing "
            "one would put invented syntax into your analysis code."))

    frag = (root / out_dir / f"{key}.tex")
    if frag.exists():
        return Plan(kind=kind, blocked=(
            f"{out_dir}/{key}.tex already exists. Pick another name rather than overwrite "
            "an exhibit some script is already filling in."))

    checks = [
        Check("expression", True, f"{expression} — computed by {script.name}, not typed here"),
        Check("producer", True, f"{_rel(script, root.parent)} writes it"),
    ]

    writes: list[Write] = []
    placeholder = _PLACEHOLDER.format(script=script.name)
    writes.append(Write(
        path=frag, kind="create",
        label=f"a new fragment at {out_dir}/{key}.tex",
        preview=placeholder,
        context=("It holds no value until you run the script. A placeholder that looked "
                 "like a result would be a hardcoded number by another name."),
    ))

    line = recipe(expression=expression, target=frag, script=script, root=root,
                  key=key, description=description or caption)
    writes.append(Write(
        path=script, kind="append",
        label=f"a line appended to {script.name}",
        preview=line,
    ))

    if kind == "exhibit":
        rf = Path(runfile) if runfile else find_runfile(root)
        if rf is None:
            checks.append(Check(
                "runfile", False,
                "no runfile found in this project, so nothing will re-run this script for you",
                blocking=False))
        elif _runfile_calls(rf, script):
            checks.append(Check("runfile", True, f"{rf.name} already runs {script.name}"))
        else:
            call = _RUNFILE_CALLS.get(rf.suffix, _RUNFILE_CALLS[".R"])(script)
            checks.append(Check("runfile", True, f"{rf.name} will gain a call to {script.name}"))
            writes.append(Write(
                path=rf, kind="runfile",
                label=f"a line in {rf.name}, so the exhibit is rebuilt with everything else",
                preview=call,
                context=_runfile_context(rf, call),
            ))

    inp = "\\input{" + f"{out_dir}/{key}" + "}"
    if directive_only:
        writes.append(_splice_write(block, caret, inp, root,
                                    label="the \\input at your cursor"))
    else:
        env = _float(float_kind, caption, label, inp)
        writes.append(_splice_write(
            block, None, "\n\n" + env, root,
            label=f"a new {float_kind} after this paragraph",
            preview=env,
        ))

    plan = Plan(kind=kind, checks=tuple(checks), writes=tuple(writes))
    plan.rerun = f"Rscript {script.name}" if script.suffix in (".R", ".r") else str(script.name)
    plan.summary = (
        f"{key}: {description or caption}. The value is computed by {script.name}; "
        f"nothing is run here, so re-run it to fill {out_dir}/{key}.tex in."
    )
    return plan


# ================================================================ applying it


def apply(plan: Plan, *, root: Path, library=None) -> Result:
    """Do every write, or leave the tree exactly as it was.

    The order is deliberate. The library record goes first, because it is the
    step that reaches outside this machine and a failure there must leave the
    manuscript untouched. The block splice goes last, because it is the step
    most likely to refuse: another writer may have rewritten the paragraph
    between the preview and the confirmation, and that refusal has to unwind the
    bib entry rather than sit beside it.
    """
    root = Path(root).resolve()
    if not plan.ok:
        return Result(False, error=plan.blocked or f"the {plan.failed_check} check failed")

    created_key = None
    snaps: list[tuple[Path, bytes | None]] = []
    made_dirs: list[Path] = []
    wrote: list[str] = []
    try:
        if plan.library_add:
            if library is None:
                library = ZoteroLibrary()
            item = library.add_by_doi(plan.library_add)
            created_key = (item or {}).get("key")
            wrote.append(f"Zotero record {created_key or ''}".strip())

        ordered = [w for w in plan.writes if w.kind != "splice"] + \
                  [w for w in plan.writes if w.kind == "splice"]

        for w in ordered:
            # Snapshot immediately before the write it guards, never all of them
            # up front. A file that was never written must not be restored: the
            # tree is watched, so rewriting identical bytes still fires a rebuild
            # and redraws the page on a transaction that is being undone.
            snaps.append((w.path, w.path.read_bytes() if w.path.exists() else None))
            if w.kind == "create":
                parent = w.path.parent
                if not parent.exists():
                    made_dirs.append(parent)
                    parent.mkdir(parents=True, exist_ok=True)
                w.path.write_text(w.preview, encoding="utf-8")
            elif w.kind == "append":
                _append(w.path, w.preview)
            elif w.kind == "runfile":
                _insert_runfile_line(w.path, w.preview)
            elif w.kind == "splice":
                splice_mod.splice(w.block, w.new_source, root=root, holder=HOLDER)
            else:
                raise ValueError(f"unknown write kind {w.kind!r}")
            wrote.append(w.label)
    except BaseException as exc:
        _restore(snaps, made_dirs)
        if created_key and library is not None:
            try:
                library.remove(created_key)
            except Exception:
                pass
        return Result(False, error=f"{type(exc).__name__}: {exc}".replace("Error: ", ": ").strip(": ")
                      if not str(exc) else str(exc))
    return Result(True, wrote=wrote, rerun=plan.rerun)


def _restore(snaps, made_dirs) -> None:
    for path, payload in reversed(snaps):
        try:
            if payload is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(payload)
        except OSError:
            pass
    for d in reversed(made_dirs):
        try:
            d.rmdir()
        except OSError:
            pass


def _append(path: Path, text: str) -> None:
    old = path.read_text(encoding="utf-8") if path.exists() else ""
    sep = "" if (not old or old.endswith("\n")) else "\n"
    path.write_text(old + sep + text, encoding="utf-8")


def _insert_runfile_line(path: Path, call: str) -> None:
    """Beside the other calls, not after the closing banner.

    A runfile usually ends with a summary that must stay last, so the line goes
    after the final existing call rather than at the end of the file.
    """
    text = path.read_text(encoding="utf-8")
    lines = text.split("\n")
    at = None
    for i, line in enumerate(lines):
        if _CALL_RE.search(line):
            at = i
    if at is None:
        _append(path, call)
        return
    lines.insert(at + 1, call.rstrip("\n"))
    path.write_text("\n".join(lines), encoding="utf-8")


# ============================================================== the one route


def context(root: Path, build, block_id: str = "") -> dict:
    """What the forms need to offer real choices rather than free text.

    The scripts are ranked by how close they already are to this paragraph: one
    that writes something the block already `\\input`s first, then one writing
    into the same file, then the rest. Which script owns an output is
    `producers`' answer, never a guess from a path.
    """
    root = Path(root).resolve()
    produced = producers.scan(root)
    block = (build.by_id.get(block_id) if build is not None else None) if block_id else None

    near_targets = {Path(inc.target).resolve() for inc in getattr(block, "includes", ())} if block else set()
    same_file = set()
    if block is not None and build is not None:
        host = Path(block.file).resolve()
        for b in build.blocks:
            if Path(b.file).resolve() == host:
                same_file |= {Path(i.target).resolve() for i in b.includes}

    by_script: dict[Path, list[str]] = {}
    for target, script in produced.items():
        by_script.setdefault(Path(script).resolve(), []).append(Path(target).name)

    scripts = []
    for script in sorted(known_scripts(root)):
        outputs = sorted(by_script.get(script, []))
        owned = {t for t, s in produced.items() if Path(s).resolve() == script}
        rank = 0 if owned & near_targets else (1 if owned & same_file else (2 if outputs else 3))
        scripts.append({
            "path": str(script), "name": script.name, "outputs": outputs,
            "rank": rank, "recipe": script.suffix in _RECIPES,
        })
    scripts.sort(key=lambda s: (s["rank"], not s["recipe"], s["name"]))

    rf = find_runfile(root)
    return {
        "ok": True,
        "scripts": scripts,
        "fragment_dir": _fragment_dir(root),
        "table_dir": _exhibit_dir(root, "table"),
        "figure_dir": _exhibit_dir(root, "figure"),
        "runfile": rf.name if rf else None,
        "block": {"id": block_id, "source": getattr(block, "source_text", "") or "",
                  "file": str(getattr(block, "file", "") or "")} if block else None,
    }


def handle(data: dict, *, root: Path, build, read_only: bool,
           bib_path: Path | None = None, net=None, library=None) -> dict:
    """Plan, then apply against a token. The plan itself never travels.

    A plan posted back from the page would be a field that accepts any text at
    all, including a literal number in a fragment, which is the one door this
    module exists to not have. So the page confirms a token and the writes are
    the ones this process already computed.
    """
    root = Path(root).resolve()
    stage = str(data.get("stage") or "")

    if stage == "context":
        return context(root, build, str(data.get("block") or ""))

    if read_only:
        return {"ok": False, "status": 403,
                "error": "This manuscript is open read-only, so nothing here can write to it."}

    if stage == "apply":
        plan = take(str(data.get("token") or ""))
        if plan is None:
            return {"ok": False, "status": 409,
                    "error": "That preview has expired or was already written. Check again."}
        result = apply(plan, root=root, library=library)
        return {"ok": result.ok, "wrote": result.wrote, "error": result.error,
                "rerun": result.rerun, "summary": plan.summary}

    if stage != "plan":
        return {"ok": False, "status": 400, "error": f"unknown insert stage {stage!r}"}

    block = build.by_id.get(str(data.get("block") or "")) if build is not None else None
    if block is None:
        return {"ok": False, "status": 404, "error": "That block is not in the current build."}

    kind = str(data.get("kind") or "")
    produced = producers.scan(root)
    common = {"block": block, "root": root, "base": data.get("base")}
    try:
        if kind == "citation":
            plan = plan_citation(
                query=str(data.get("query") or ""), caret=int(data.get("caret") or 0),
                # `beside` names a citation the author clicked, which is a target
                # that needs no caret and cannot be measured wrong.
                beside=str(data.get("beside") or "") or None,
                bib_path=bib_path, net=net, library=library, produced=produced, **common)
        elif kind == "value":
            plan = plan_value(
                key=str(data.get("key") or ""), description=str(data.get("description") or ""),
                expression=str(data.get("expression") or ""),
                script=Path(str(data.get("script") or "")), caret=int(data.get("caret") or 0),
                produced=produced, **common)
        elif kind == "exhibit":
            plan = plan_exhibit(
                key=str(data.get("key") or ""), caption=str(data.get("caption") or ""),
                expression=str(data.get("expression") or ""),
                script=Path(str(data.get("script") or "")), produced=produced,
                kind=str(data.get("float") or "table"), label=data.get("label") or None,
                **common)
        else:
            return {"ok": False, "status": 400, "error": f"unknown insertion kind {kind!r}"}
    except Exception as exc:                     # a catalogue that answered badly
        return {"ok": False, "status": 502, "error": f"{type(exc).__name__}: {exc}"}

    payload = plan.to_json(root)
    return {"ok": plan.ok, "plan": payload, "token": stash(plan) if plan.ok else None}


def route(session):
    """An aiohttp handler bound to one session."""
    import asyncio

    from aiohttp import web

    from manuscriptor.server import build as build_mod

    async def handler(request):
        try:
            data = await request.json()
        except Exception:
            data = {}
        payload = await asyncio.to_thread(
            handle, data,
            root=session.root, build=session.build, read_only=session.read_only,
            bib_path=build_mod.find_bib(session.root, getattr(session, "bib", None)),
        )
        status = int(payload.pop("status", 200))
        return web.json_response(payload, status=status if not payload.get("ok") else 200)

    return handler


# ================================================================== the details


def _stale(block, base) -> str | None:
    if base is None:
        return None
    if base != getattr(block, "source_text", None):
        return ("This paragraph has changed since it was last written to disk, so the caret "
                "would land at an offset in text the file does not hold. Let the edit save "
                "first -- it saves on a pause -- and insert again.")
    return None


def _first_failure(checks) -> str:
    for c in checks:
        if c.blocking and not c.ok:
            return f"{c.name}: {c.detail}"
    return "a check failed"


# Commands whose argument is a citation key list. A citation inserted with the
# caret inside one of these belongs IN it.
_CITE_COMMANDS = (
    "cite", "citep", "citet", "citealp", "citealt", "citeauthor", "citeyear",
    "citenum", "autocite", "parencite", "textcite", "footcite", "fullcite",
)
# Commands whose argument is a name or a path, never prose. A citation spliced
# into one of these breaks it, and breaks it quietly: a mangled `\ref` surfaces
# as `??` in a render nobody is looking at yet.
_CLOSED_COMMANDS = (
    "ref", "autoref", "eqref", "pageref", "cref", "Cref", "nameref",
    "label", "input", "include", "includegraphics", "bibliography", "url",
)

_ARG_OPEN_RE = re.compile(r"\\([A-Za-z]+)\*?\s*(?:\[[^\]]*\])?\{")


def _enclosing_command(source: str, caret: int) -> tuple[str, int, int] | None:
    """The command whose braces contain `caret`, as (name, open_brace, close_brace).

    Scans backwards for the nearest unclosed `\\name{` before the caret. Good
    enough for one level, which is what a citation key list ever is.
    """
    depth = 0
    i = caret - 1
    while i >= 0:
        ch = source[i]
        if ch == "}":
            depth += 1
        elif ch == "{":
            if depth:
                depth -= 1
            else:
                m = None
                for cand in _ARG_OPEN_RE.finditer(source, max(0, i - 40), i + 1):
                    if cand.end() == i + 1:
                        m = cand
                if m is None:
                    return None
                close = source.find("}", i)
                if close < 0 or close < caret:
                    return None
                return m.group(1), i, close
        i -= 1
    return None


def _citation_write(block, caret, beside, cite_key, root):
    """The one write a citation makes to the manuscript, or a reason it cannot.

    Where it goes depends on what the author pointed at. `beside` names a citation
    they clicked on the page, which needs no caret at all; otherwise the caret
    decides, and inside an existing citation that means joining its key list
    rather than nesting a second `\\citep` inside the first.
    """
    source = getattr(block, "source_text", "") or ""
    if beside:
        new_source, note = place_beside_key(source, str(beside), cite_key)
    else:
        at = len(source.rstrip()) if caret is None else caret
        new_source, note = place_citation(source, at, cite_key)
    if new_source == source:
        return note or "nothing to write"
    where = new_source.find(cite_key)
    return Write(
        path=Path(getattr(block, "file", "")),
        kind="splice",
        label=note or "the \\citep at your cursor",
        preview=new_source[max(0, where - 70):where + len(cite_key) + 25],
        context=new_source,
        block=block,
        new_source=new_source,
    )


def place_beside_key(source: str, existing_key: str, new_key: str) -> tuple[str, str]:
    """Add a key to the citation that already cites `existing_key`.

    The point of naming a citation rather than an offset: the author clicked
    something on the page, so the target is that citation and not a caret they
    have to place first and trust was recorded. A caret is only trustworthy when
    the textarea it was measured in had focus, which is the whole "click into the
    editor, hope, then find the form on another tab" dance this replaces.
    """
    if not existing_key:
        return source, "no citation was named"
    for m in _ARG_OPEN_RE.finditer(source):
        if m.group(1) not in _CITE_COMMANDS:
            continue
        close = source.find("}", m.end())
        if close < 0:
            continue
        keys = [k.strip() for k in source[m.end():close].split(",")]
        if existing_key not in keys:
            continue
        if new_key in keys:
            return source, f"{new_key} is already cited there."
        return (source[:close] + ", " + new_key + source[close:],
                f"added beside {existing_key}")
    return source, f"{existing_key} is not cited in this paragraph."


def place_citation(source: str, caret: int, cite_key: str) -> tuple[str, str]:
    """Where a citation actually goes, given where the cursor is.

    Returns the new source and a note (empty when it was an ordinary insertion).
    The note is the plan's explanation and, on a refusal, the reason nothing was
    written.

    Three cases, and the first one is why this function exists. The caret inside
    an existing citation means the author is adding to that citation: on
    2026-07-26 a literal splice at the caret produced
    `\\citep{cook2010\\citep{zhang2026ai}}` in a real manuscript, which is not
    LaTeX and does not render. The caret inside a command whose argument is a
    label or a path is refused. Everything else gets its own `\\citep`.
    """
    at = max(0, min(int(caret if caret is not None else len(source)), len(source)))
    found = _enclosing_command(source, at)

    if found is not None:
        name, open_at, close_at = found
        if name in _CITE_COMMANDS:
            keys = [k.strip() for k in source[open_at + 1:close_at].split(",")]
            if cite_key in keys:
                return source, f"{cite_key} is already cited in this \\{name}."
            # Snap out of the middle of a key: half a key is a citation to
            # nothing, and it would compile.
            edge = at
            while edge < close_at and source[edge] not in ",}":
                edge += 1
            joined = source[:edge] + ", " + cite_key + source[edge:]
            return joined, f"added to the \\{name} at your cursor"
        if name in _CLOSED_COMMANDS:
            return source, (
                f"the cursor is inside \\{name}{{...}}, whose argument is a name "
                "rather than prose, so nothing was written. Put the cursor in the "
                "sentence and try again."
            )

    snippet = "\\citep{" + cite_key + "}"
    return source[:at] + snippet + source[at:], ""


def _splice_write(block, caret, snippet, root, *, label, preview=None) -> Write:
    source = getattr(block, "source_text", "") or ""
    if caret is None:
        at = len(source.rstrip())
        new_source = source[:at] + snippet + source[at:]
    else:
        at = max(0, min(int(caret), len(source)))
        new_source = source[:at] + snippet + source[at:]
    return Write(
        path=Path(getattr(block, "file", "")),
        kind="splice",
        label=label,
        preview=preview if preview is not None else snippet,
        context=new_source,
        block=block,
        new_source=new_source,
    )


def _float(kind: str, caption: str, label: str, body: str) -> str:
    kind = kind if kind in ("table", "figure") else "table"
    return (
        f"\\begin{{{kind}}}[htbp]\n"
        "\\centering\n"
        f"\\caption{{{caption}}}\n"
        f"\\label{{{label}}}\n"
        f"{body}\n"
        f"\\end{{{kind}}}"
    )


_PLACEHOLDER = (
    "% Written by manuscriptor as a placeholder. Re-run {script} to fill it in.\n"
    "% It carries no value on purpose: a plausible number here would be a hardcoded result.\n"
    "\\textbf{{??}}%\n"
)


# ------------------------------------------------------------------- recipes


def _r_line(*, expression, target, script, root, key, description) -> str:
    path = _script_path_expr(target, script, root, lang="R")
    return (
        f"\n# {key}: {description}\n"
        f"# written by manuscriptor; the manuscript reads it with \\input\n"
        f"writeLines(as.character({expression}), {path})\n"
    )


def _stata_line(*, expression, target, script, root, key, description) -> str:
    path = _script_path_expr(target, script, root, lang="stata")
    return (
        f"\n* {key}: {description}\n"
        f"* written by manuscriptor; the manuscript reads it with \\input\n"
        f"file open _mx using {path}, write replace\n"
        f"file write _mx ({expression}) _n\n"
        f"file close _mx\n"
    )


def _python_line(*, expression, target, script, root, key, description) -> str:
    path = _script_path_expr(target, script, root, lang="python")
    return (
        f"\n# {key}: {description}\n"
        f"# written by manuscriptor; the manuscript reads it with \\input\n"
        f"Path({path}).write_text(str({expression}), encoding='utf-8')\n"
    )


_RECIPES = {
    ".R": _r_line, ".r": _r_line, ".Rmd": _r_line, ".qmd": _r_line,
    ".do": _stata_line,
    ".py": _python_line,
}

_RUNFILE_CALLS = {
    ".R": lambda s: f'source("{s.name}")',
    ".r": lambda s: f'source("{s.name}")',
    ".do": lambda s: f'do "{s.name}"',
    ".py": lambda s: f"exec(open('{s.name}').read())",
}

_CALL_RE = re.compile(r"^\s*(?:source\s*\(|do\s+[\"']|exec\s*\(\s*open)")


def _script_path_expr(target: Path, script: Path, root: Path, *, lang: str) -> str:
    """Write the path the way the script already writes paths.

    A script that carries a `project_path` gets one built from it; anything else
    gets a path relative to the script's own directory. An absolute path would
    work on this machine and nowhere else, which is not what belongs in an
    author's analysis code.
    """
    repo = root.parent
    try:
        parts = list(target.resolve().relative_to(repo).parts)
    except ValueError:
        parts = None

    text = ""
    try:
        text = script.read_text(encoding="utf-8", errors="replace")
    except OSError:
        pass

    if parts and "project_path" in text:
        if lang == "R":
            inner = ", ".join(f"'{p}'" for p in parts)
            return f"file.path(project_path, {inner})"
        if lang == "stata":
            return '"`project_path\'/' + "/".join(parts) + '"'
        return "project_path / " + " / ".join(f"'{p}'" for p in parts)

    rel = os.path.relpath(target.resolve(), script.parent.resolve())
    if lang == "stata":
        return f'"{rel}"'
    return f"'{rel}'"


# ------------------------------------------------------------------ locations


def known_scripts(root: Path) -> set[Path]:
    """Every analysis script of this project.

    Where code lives is decided in `producers`, and asking it rather than
    re-deriving it here keeps one answer to that question in the codebase.
    """
    root = Path(root).resolve()
    found = producers._find_scripts(root.parent) + producers._find_scripts(root)
    return {p.resolve() for p in found}


_FRAGMENT_DIRS = ("exhibits", "fragments", "results", "values", "numbers")


def _fragment_dir(root: Path) -> str:
    for name in _FRAGMENT_DIRS:
        if (root / name).is_dir():
            return name
    return "exhibits"


def _exhibit_dir(root: Path, kind: str) -> str:
    preferred = ("tables", "Tables") if kind == "table" else ("figures", "Figures")
    for name in preferred:
        if (root / name).is_dir():
            return name
    return _fragment_dir(root)


_RUNFILE_NAMES = ("runfile", "run", "master", "_master", "main", "makefile")


def find_runfile(root: Path) -> Path | None:
    """The script that runs the others, if this project has one."""
    root = Path(root).resolve()
    for base in (root.parent, root):
        for d in (base,) + tuple(base / n for n in producers._CODE_DIRS):
            if not d.is_dir():
                continue
            for p in sorted(d.glob("*")):
                if p.is_file() and p.suffix in producers._CODE_SUFFIXES \
                        and p.stem.lower() in _RUNFILE_NAMES:
                    return p.resolve()
    return None


def _runfile_calls(runfile: Path, script: Path) -> bool:
    try:
        text = runfile.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return script.name in text or script.stem in text


def _runfile_context(runfile: Path, call: str) -> str:
    try:
        lines = runfile.read_text(encoding="utf-8", errors="replace").split("\n")
    except OSError:
        return call
    at = None
    for i, line in enumerate(lines):
        if _CALL_RE.search(line):
            at = i
    if at is None:
        return call
    head = lines[max(0, at - 1): at + 1]
    tail = lines[at + 1: at + 3]
    return "\n".join(head + ["> " + call] + tail)


def _rel(path, base) -> str:
    try:
        return str(Path(path).resolve().relative_to(Path(base).resolve()))
    except (ValueError, OSError):
        return str(path)


# ------------------------------------------------------------------ the .bib


_ENTRY_RE = re.compile(r"@(\w+)\s*\{\s*([^,\s]+)\s*,", re.M)
_DOI_FIELD_RE = re.compile(r"\bdoi\s*=\s*[{\"]\s*([^}\"\n]+)", re.I)


def read_bib_entries(bib_path: Path | None) -> dict[str, dict]:
    """Cite key to the few fields a search needs: doi, title, author, year.

    The same deliberately small scan as `read_bib`, which answers only "which
    keys are taken". This one exists so a citation the paper ALREADY carries can
    be found in the bibliography instead of on the internet.
    """
    if not bib_path:
        return {}
    path = Path(bib_path)
    if not path.exists():
        return {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    out: dict[str, dict] = {}
    hits = list(_ENTRY_RE.finditer(text))
    for i, m in enumerate(hits):
        end = hits[i + 1].start() if i + 1 < len(hits) else len(text)
        body = text[m.end():end]

        def field(name: str) -> str:
            f = re.search(name + r"\s*=\s*[{\"](.*?)[}\"]\s*,?\s*\n", body, re.S | re.I)
            if not f:
                return ""
            # Braces protect capitalisation in BibTeX; they are not part of the text.
            return re.sub(r"\s+", " ", f.group(1).replace("{", "").replace("}", "")).strip()

        doi = _DOI_FIELD_RE.search(body)
        out[m.group(2)] = {
            "doi": (doi.group(1).strip().rstrip(",").strip() if doi else ""),
            "title": field("title"),
            "author": field("author"),
            "year": field("year"),
        }
    return out


def match_bib(entries: dict[str, dict], query: str) -> tuple[list[str], str]:
    """Which bibliography entries the query names, and how it named them.

    Tried in order of how sure each rule is, and every rule requires a UNIQUE
    answer: two entries matching is a question for the author, not a coin toss.
    The forms are the ones an author actually types. A cite key is first because
    it is what someone who knows their own bibliography reaches for, and it was
    not an accepted form at all until 2026-07-26: typing `zhang2026ai` searched
    Zotero, then Crossref, then failed a relevance check, while the entry sat in
    `references.bib` the whole time.
    """
    q = re.sub(r"\s+", " ", (query or "").strip())
    if not q or not entries:
        return [], ""
    low = q.lower()

    exact = [k for k in entries if k.lower() == low]
    if len(exact) == 1:
        return exact, "its cite key"

    m = _DOI_RE.search(q)
    if m:
        want = _norm_doi(m.group(0))
        by_doi = [k for k, e in entries.items() if e.get("doi") and _norm_doi(e["doi"]) == want]
        if len(by_doi) == 1:
            return by_doi, "its DOI"
        if len(by_doi) > 1:
            return by_doi, "its DOI"
        return [], ""          # a DOI naming nothing here is a new source

    prefix = [k for k in entries if k.lower().startswith(low)]
    if len(prefix) == 1:
        return prefix, "the start of its cite key"

    year = re.search(r"\b(19|20)\d{2}\b", q)
    if year:
        words = [w for w in re.findall(r"[A-Za-z]{3,}", low)]
        by_author = [
            k for k, e in entries.items()
            if e.get("year") == year.group(0)
            and any(w in (e.get("author") or "").lower() for w in words)
        ]
        if len(by_author) == 1:
            return by_author, "an author and the year"
        if len(by_author) > 1:
            return by_author, "an author and the year"

    by_title = [k for k, e in entries.items() if low in (e.get("title") or "").lower()]
    if by_title:
        return by_title, "its title"
    return [], ""


def read_bib(bib_path: Path | None) -> dict[str, str]:
    """Cite key to DOI, for every entry in the file.

    A small scan rather than a parser: the question is only which keys are
    taken and which DOI each one carries, and a bibliography that a parser
    chokes on must not stop a citation being inserted.
    """
    if not bib_path:
        return {}
    path = Path(bib_path)
    if not path.exists():
        return {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    out: dict[str, str] = {}
    hits = list(_ENTRY_RE.finditer(text))
    for i, m in enumerate(hits):
        end = hits[i + 1].start() if i + 1 < len(hits) else len(text)
        body = text[m.end():end]
        doi = _DOI_FIELD_RE.search(body)
        out[m.group(2)] = (doi.group(1).strip().rstrip(",").strip() if doi else "")
    return out


def _bib_key_for_doi(entries: dict[str, str], doi: str | None) -> str | None:
    if not doi:
        return None
    want = _norm_doi(doi)
    for key, entry_doi in entries.items():
        if entry_doi and _norm_doi(entry_doi) == want:
            return key
    return None


_STOP = {"the", "a", "an", "of", "on", "in", "for", "and", "to", "from", "with", "is", "are"}


def bib_key(meta: dict, *, taken: dict) -> str:
    """`nishtar2019persistence`, in the style the corpus already uses."""
    authors = meta.get("authors") or []
    last = re.sub(r"[^a-z]", "", str(authors[0]).split(",")[0].split()[-1].lower()) if authors else "anon"
    year = str(meta.get("year") or "")
    year = year if year.isdigit() else "nd"
    words = re.findall(r"[A-Za-z]+", str(meta.get("title") or ""))
    word = next((w.lower() for w in words if w.lower() not in _STOP and len(w) > 3), "work")
    stem = f"{last}{year}{word}"
    if stem not in taken:
        return stem
    for suffix in "abcdefghijklmnop":
        if stem + suffix not in taken:
            return stem + suffix
    return stem + secrets.token_hex(2)


# Two vocabularies, one map: Crossref's `book-chapter` and Zotero's
# `bookSection` are the same entry type, and a caller should not have to know
# which catalogue an answer came from.
_BIB_TYPES = {
    "journal-article": "article", "book": "book", "book-chapter": "incollection",
    "posted-content": "misc", "report": "techreport", "proceedings-article": "inproceedings",
    "journalArticle": "article", "bookSection": "incollection",
    "conferencePaper": "inproceedings", "thesis": "phdthesis", "preprint": "misc",
    "manuscript": "unpublished", "webpage": "misc",
}

# WHICH FIELDS EACH ENTRY TYPE TAKES. Every type used to be given the same nine
# fields, so an `@incollection` was emitted with `journal = {...}` -- a field
# BibTeX does not read on that type, which silently drops the container title
# out of the rendered reference. The container has a different name per type,
# and that is the whole of the mapping.
_BIB_FIELDS = {
    "article": ("title", "author", "journal", "year", "volume", "number", "pages", "doi"),
    "incollection": ("title", "author", "booktitle", "editor", "year", "publisher",
                     "address", "edition", "pages", "isbn", "doi"),
    "inproceedings": ("title", "author", "booktitle", "year", "publisher", "address",
                      "pages", "doi"),
    "book": ("title", "author", "year", "publisher", "address", "edition", "volume",
             "isbn", "doi"),
    "techreport": ("title", "author", "institution", "year", "number", "doi", "url"),
    "phdthesis": ("title", "author", "school", "year", "doi", "url"),
    "unpublished": ("title", "author", "year", "note", "doi", "url"),
    "misc": ("title", "author", "year", "publisher", "howpublished", "doi", "url"),
}


def bib_entry(key: str, meta: dict) -> str:
    kind = _BIB_TYPES.get(str(meta.get("type") or ""), "article")
    authors = meta.get("authors") or []
    # A container title arrives under one name and is written under another:
    # Crossref's `container-title` is a journal for an article and a book title
    # for a chapter, and a report's publisher is its institution.
    have = {
        "title": meta.get("title"),
        "author": " and ".join(str(a) for a in authors) if authors else None,
        "editor": meta.get("editor"),
        "journal": meta.get("journal"),
        "booktitle": meta.get("booktitle") or meta.get("journal"),
        "institution": meta.get("institution") or meta.get("publisher"),
        "school": meta.get("school") or meta.get("publisher"),
        "publisher": meta.get("publisher"),
        "address": meta.get("address"),
        "edition": meta.get("edition"),
        "year": meta.get("year"),
        "volume": meta.get("volume"),
        "number": meta.get("number") or meta.get("issue"),
        "pages": meta.get("pages"),
        "isbn": meta.get("isbn"),
        "doi": meta.get("doi"),
        "url": meta.get("url"),
        "note": meta.get("note"),
        "howpublished": meta.get("howpublished"),
    }
    body = "".join(
        f"  {name} = {{{_clean(have[name])}}},\n"
        for name in _BIB_FIELDS.get(kind, _BIB_FIELDS["article"])
        if have.get(name) not in (None, "")
    )
    return f"\n@{kind}{{{key},\n{body}}}\n"


def _clean(v) -> str:
    return re.sub(r"\s+", " ", str(v)).strip()


def _norm_doi(doi: str) -> str:
    d = str(doi or "").strip().lower()
    d = re.sub(r"^https?://(dx\.)?doi\.org/", "", d)
    return d.rstrip(".")


_DOI_RE = re.compile(r"10\.\d{4,9}/\S+")


def _titles_agree(a, b) -> bool:
    na, nb = _norm_title(a), _norm_title(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    wa, wb = set(na.split()), set(nb.split())
    if not wa or not wb:
        return False
    return len(wa & wb) / min(len(wa), len(wb)) >= 0.8


def _relevance(query, meta) -> float:
    """How much of what he asked for the candidate actually accounts for.

    Matched against the title, the authors AND the year together, because
    "Finkelstein 2012 Oregon" is how people search and two thirds of it is not
    in any title.
    """
    words = [w for w in _norm_title(query).split() if w not in _STOP and len(w) > 2]
    if not words:
        return 0.0
    hay = set(_norm_title(
        f"{meta.get('title') or ''} {' '.join(str(a) for a in (meta.get('authors') or []))} "
        f"{meta.get('year') or ''} {meta.get('journal') or ''}").split())
    return sum(1 for w in words if w in hay) / len(words)


def _norm_title(t) -> str:
    t = re.sub(r"[^a-z0-9 ]+", " ", str(t or "").lower())
    return re.sub(r"\s+", " ", t).strip()


def _candidate_doi(query: str, net, library):
    """The DOI to gate, the metadata that came with it, and the library record.

    The library is asked first, deliberately: a paper the author already holds
    should not be re-derived from a search engine, and its own DOI is the one
    the rest of his bibliography is keyed on. The record itself travels back,
    because a record he already holds needs no relevance check and asking the
    library a second time to find that out is a second round trip for something
    already known.

    **A library hit with no DOI is still the answer.** It used to be discarded
    -- the record was found, `held.get("doi")` was empty, and the search fell
    through to Crossref, which does not hold books and therefore offered the
    nearest article-shaped thing it had. That is how three tries at one book
    produced three different wrong records. A book identified by ISBN, or by
    nothing but the author's own record of it, is identified.
    """
    q = (query or "").strip()
    m = _DOI_RE.search(q)
    if m:
        return _norm_doi(m.group(0)), None, None

    held = library.find(title=q) if hasattr(library, "find") else None
    if held:
        doi = _norm_doi(held["doi"]) if held.get("doi") else None
        return doi, _from_library(held), held

    hits = list(net.crossref_search(q) or [])
    for cand in hits:
        if cand.get("doi"):
            return _norm_doi(cand["doi"]), cand, None
    return None, (hits or [None])[0], None


def _from_library(held: dict) -> dict:
    """A Zotero record as the metadata the rest of this module speaks.

    Only the fields the record actually carries. A missing `publisher` must stay
    missing rather than become an empty field in a bib entry.
    """
    meta = {k: held.get(k) for k in (
        "title", "doi", "isbn", "type", "authors", "year", "journal", "booktitle",
        "publisher", "address", "edition", "volume", "issue", "pages", "citation_key",
    ) if held.get(k) not in (None, "")}
    return meta


def _no_doi_detail(seed: dict | None, held: dict | None) -> str:
    """Why the `doi` row does not apply, said as a determination rather than a shrug."""
    seed = seed or {}
    key = (held or {}).get("key")
    where = f"your Zotero record {key}" if key else "your library"
    isbn = seed.get("isbn")
    if isbn:
        return f"no DOI; {where} identifies it by ISBN {isbn}"
    return f"no DOI; {where} records none, and is itself the identity"


def _generated_bib(bib_path, root, produced=None) -> str | None:
    """Why nothing may be appended to this bibliography, or None if it may.

    Provenance is `server/producers.py`'s decision and this asks it rather than
    guessing from a name or a directory. It is deliberately a CHECK with a named
    remedy and never a silent skip: a hand-maintained bibliography that were
    wrongly claimed here would refuse every legitimate insertion, so the author
    has to be able to see which file, which producer, and what to run instead.
    """
    if bib_path is None:
        return None
    path = Path(bib_path)
    if produced is None:
        produced = producers.scan(Path(root))
    p = producers.provenance(path, produced)
    if not p.generated:
        return None
    who = Path(p.producer).name if p.producer else None
    return (
        f"{path.name} is a generated file"
        + (f", written by {who}" if who else " -- its own header says so")
        + ". An entry appended here is destroyed the next time it is regenerated, while the "
        "\\citep{} in the manuscript survives, leaving a citation with no entry. Add the work "
        "to the source that generator reads"
        + (f", then regenerate: {p.remedy}" if p.remedy else ", then regenerate it")
        + "."
    )


# ============================================================ the outside world


class HttpCatalogue:
    """Crossref and OpenAlex. Journal sites block robots; these do not."""

    def __init__(self, timeout: float = 12.0):
        self.timeout = timeout

    def _get(self, url, params=None):
        import requests
        try:
            r = requests.get(url, params=params, timeout=self.timeout,
                             headers={"User-Agent": "manuscriptor (mailto:bdaniels@g.harvard.edu)"})
        except Exception:
            return None
        if r.status_code != 200:
            return None
        try:
            return r.json()
        except ValueError:
            return None

    def crossref_search(self, query):
        data = self._get("https://api.crossref.org/works",
                         {"query.bibliographic": query, "rows": 5, "select":
                          "DOI,title,author,issued,container-title,volume,issue,page,publisher,type"})
        items = ((data or {}).get("message") or {}).get("items") or []
        return [_from_crossref(i) for i in items]

    def crossref_by_doi(self, doi):
        data = self._get(f"https://api.crossref.org/works/{doi}")
        msg = (data or {}).get("message")
        return _from_crossref(msg) if msg else None

    def openalex_by_doi(self, doi):
        data = self._get(f"https://api.openalex.org/works/doi:{doi}")
        if not data or not data.get("id"):
            return None
        return {
            "doi": _norm_doi(data.get("doi") or doi),
            "title": data.get("title") or data.get("display_name") or "",
            "year": data.get("publication_year"),
            "authors": [(a.get("author") or {}).get("display_name", "")
                        for a in (data.get("authorships") or [])],
        }


def _from_crossref(item) -> dict:
    if not item:
        return {}
    title = (item.get("title") or [""])
    issued = ((item.get("issued") or {}).get("date-parts") or [[None]])[0]
    authors = []
    for a in item.get("author") or []:
        name = (a.get("family") or a.get("name") or "").strip()
        given = (a.get("given") or "").strip()
        authors.append(f"{name}, {given}".strip(", ") if given else name)
    return {
        "doi": _norm_doi(item.get("DOI") or ""),
        "title": title[0] if isinstance(title, list) and title else str(title),
        "year": issued[0] if issued else None,
        "authors": [a for a in authors if a],
        "journal": (item.get("container-title") or [""])[0]
        if isinstance(item.get("container-title"), list) else item.get("container-title"),
        "volume": item.get("volume"),
        "issue": item.get("issue"),
        "pages": (item.get("page") or "").replace("-", "--") or None,
        "publisher": item.get("publisher"),
        "type": item.get("type"),
    }


class ZoteroLibrary:
    """Reads through the local API, writes through `zotero-cli`.

    The local HTTP API is read-only, so the one write path is the same CLI the
    evidence repair stage already uses. Keeping the write on a separate tool is
    also what makes the read path safe to call on every keystroke.
    """

    def __init__(self, client=None):
        self._client = client
        self._checked = False

    @property
    def client(self):
        if not self._checked:
            self._checked = True
            try:
                from manuscriptor.evidence.zotero import ZoteroClient
                c = ZoteroClient()
                self._client = c if c.is_available() else None
            except Exception:
                self._client = None
        return self._client

    def find(self, *, doi=None, title=None):
        c = self.client
        if c is None:
            return None
        key = None
        if doi:
            key = c.search_by_doi(doi)
        if not key and title:
            key = c.search_by_title(title)
        if not key:
            return None
        item = c.get_item(key)
        if item is None:
            return None
        # Everything the entry will need, because a work with no DOI is
        # identified by what else the record holds -- its ISBN, its publisher,
        # and the key Better BibTeX already gave it.
        return {
            "key": item.key, "doi": item.doi, "title": item.title,
            "has_fulltext": bool(c.get_fulltext(item.key)),
            "isbn": item.isbn, "type": item.item_type, "booktitle": item.book_title, "authors": list(item.authors or []),
            "year": item.year, "journal": item.journal, "publisher": item.publisher,
            "address": item.place, "edition": item.edition, "volume": item.volume,
            "issue": item.issue, "pages": item.pages, "citation_key": item.citation_key,
        }

    def add_by_doi(self, doi):
        out = self._cli(["import", "doi", doi])
        key = _first_item_key(out)
        if not key:
            # The import may well have worked and only the answer been
            # unreadable, and that difference matters: a record created but
            # never named is one no rollback can remove. A live run left exactly
            # such an orphan. The library is the authority on what it holds, so
            # it is asked before this is called a failure.
            found = self.find(doi=doi)
            key = (found or {}).get("key")
        if not key:
            raise LibraryError(
                f"Zotero did not report a new item for {doi}. Nothing was written to the manuscript.")
        return {"key": key, "doi": doi}

    def remove(self, key):
        self._cli(["item", "delete", key, "--confirm"])

    def _cli(self, args):
        try:
            r = subprocess.run(["zotero-cli", "--json", *args],
                               capture_output=True, timeout=180,
                               encoding="utf-8", errors="replace")
        except FileNotFoundError:
            raise LibraryError("zotero-cli is not on PATH, so nothing can be added to Zotero.")
        except subprocess.TimeoutExpired:
            raise LibraryError("Zotero did not answer in time.")
        if r.returncode != 0:
            raise LibraryError(f"Zotero refused: {(r.stderr or r.stdout).strip()[:200]}")
        return r.stdout


_KEY_RE = re.compile(r"key:\s*([A-Z0-9]{8})\b")


def _first_item_key(payload: str) -> str | None:
    """The item key an import reported, whatever shape it arrived in.

    `--json` does not mean an object. `zotero-cli import doi` answers with a
    JSON-encoded SENTENCE, so walking the parsed value for a dict found nothing
    and a successful import was reported as a failure: the plan rolled back, the
    page said nothing had been written, and the record was sitting in the
    library all the same. The text is therefore searched too, always, rather
    than only when parsing raises.
    """
    try:
        data = json.loads(payload or "")
    except ValueError:
        data = None
    for candidate in _walk_json(data):
        if isinstance(candidate, dict):
            for name in ("key", "itemKey", "item_key"):
                if isinstance(candidate.get(name), str) and re.fullmatch(r"[A-Z0-9]{8}", candidate[name]):
                    return candidate[name]
    m = _KEY_RE.search(str(payload or ""))
    return m.group(1) if m else None


def _walk_json(node):
    yield node
    if isinstance(node, dict):
        for v in node.values():
            yield from _walk_json(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk_json(v)
