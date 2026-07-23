"""Compiling the manuscript to PDF and to Word.

Two toolbar buttons promised this and did nothing, which is the worst kind of
gap: a control that lies, sitting beside controls that work.

A compile is a SUBPROCESS, not a model call. Nothing here invokes an LLM, and
nothing here needs to: `latexmk`, `pdflatex` and `pandoc` are on PATH and the
author already has a documented recipe for each. The invariant that the server
has zero knowledge of Claude survives untouched.

Four things here are load-bearing, and each of them was found by running it.

**Everything is written inside `build/manuscriptor/compile`.** LaTeX lays down
`.aux`, `.log`, `.out`, `.bbl` and the PDF, and the reference manuscript has
several of those COMMITTED, so a compile beside the source rewrites tracked
files and `git status` grows. The build directory already writes its own
`.gitignore`; the compile lives under it and inherits that.

**A `\\include` in a subdirectory needs that subdirectory mirrored under the
output directory.** `\\include{tables/t}` makes TeX open `tables/t.aux` for
writing, relative to the output directory, and TeX does not create the folder:
it stops with `I can't write on file 'tables/table1_balance_patient_pre.aux'`.
Every table in the reference manuscript arrives that way.

**Exit status is not the answer to "did it compile".** pdflatex exits 1 on a
recoverable error while writing a perfectly good PDF, and the reference
manuscript does exactly that: twelve `Missing } inserted` and 119 pages of
output. `-halt-on-error` would therefore fail a paper that builds fine for its
author. Success is a PDF this run wrote, judged by the file.

**The Word conversion delegates to `~/.claude/skills/pandoc-docx`.** That skill
carries work that is not worth repeating and would be got wrong: it launders the
conversion through an intermediate format because pandoc's direct LaTeX-to-docx
OOXML is rejected by Word, and its `format_docx.py` is a publication pass over
the OOXML itself. What this module owns is the running order. See the note at
`compile_docx` for the one place the skill's recipe cannot be followed literally.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
import weakref
from dataclasses import dataclass, field
from pathlib import Path

from manuscriptor.server import build as build_mod

# ------------------------------------------------------------------- the skill

SKILL_DIR = Path("~/.claude/skills/pandoc-docx").expanduser()
SKILL_SCRIPTS = {
    "format": SKILL_DIR / "scripts" / "format_docx.py",
    "merge_bib": SKILL_DIR / "scripts" / "merge_csl_bib.py",
    "resolve_refs": SKILL_DIR / "scripts" / "resolve_refs.py",
    "plain": SKILL_DIR / "filters" / "plain.lua",
}

# nonstopmode so nothing waits for a keystroke, file-line-error so a failure
# names a file and a line. NOT halt-on-error: see the module docstring.
ENGINE_FLAGS = ("-interaction=nonstopmode", "-file-line-error")

STEP_TIMEOUT = 600.0


@dataclass(frozen=True)
class Step:
    """One command, reported the moment it finishes."""

    name: str
    ok: bool
    seconds: float
    detail: str = ""


@dataclass
class Result:
    kind: str
    ok: bool
    output: Path | None
    seconds: float
    steps: list
    error: str | None
    log: Path | None
    notes: list = field(default_factory=list)

    def as_frame(self, *, root: Path | None = None) -> dict:
        """The websocket frame the page reads. Plain JSON, no Paths."""
        url = None
        if self.output is not None and root is not None:
            try:
                url = "/" + str(Path(self.output).resolve().relative_to(
                    Path(root).resolve() / "build" / "manuscriptor"))
            except ValueError:
                url = None
        return {
            "type": "compile",
            "phase": "done",
            "kind": self.kind,
            "ok": self.ok,
            "output": str(self.output) if self.output else None,
            "url": url,
            "seconds": round(self.seconds, 1),
            "error": self.error,
            "log": str(self.log) if self.log else None,
            "notes": list(self.notes),
            "steps": [
                {"name": s.name, "ok": s.ok, "seconds": round(s.seconds, 1), "detail": s.detail}
                for s in self.steps
            ],
        }


# ------------------------------------------------------------- where it writes


def out_dir(manuscript_dir) -> Path:
    """`<manuscript>/build/manuscriptor/compile`, created and out of git.

    The same directory the render already writes into, so one `.gitignore`
    covers both and serving or compiling a paper leaves `git status` alone.
    """
    root = Path(manuscript_dir).resolve()
    build = root / "build" / "manuscriptor"
    out = build / "compile"
    out.mkdir(parents=True, exist_ok=True)
    build_mod.keep_out_of_git(build)
    return out


def mirror_tex_dirs(manuscript_dir, out: Path) -> list[Path]:
    """Recreate, under `out`, every subdirectory of the manuscript holding a
    `.tex` file.

    `\\include{tables/t}` opens `tables/t.aux` for writing relative to the
    output directory, and TeX will not create the folder. Without this the
    compile dies on the first included table with `I can't write on file`, which
    is exactly how this was found.
    """
    root = Path(manuscript_dir).resolve()
    out = Path(out).resolve()
    made: list[Path] = []
    for path in sorted(root.rglob("*.tex")):
        parent = path.parent.resolve()
        if parent == root:
            continue
        if _inside(parent, root / "build") or any(p.startswith(".") for p in parent.parts):
            continue
        try:
            rel = parent.relative_to(root)
        except ValueError:
            continue
        target = out / rel
        if not target.exists():
            target.mkdir(parents=True, exist_ok=True)
            made.append(target)
    return made


def _inside(path: Path, parent: Path) -> bool:
    try:
        Path(path).resolve().relative_to(Path(parent).resolve())
        return True
    except ValueError:
        return False


# -------------------------------------------------------------------- engine

_MAGIC_RE = re.compile(r"%\s*!\s*TEX\s+program\s*=\s*([A-Za-z]+)", re.I)
_XETEX_PACKAGES = ("fontspec", "unicode-math", "polyglossia", "xltxtra", "mathspec")


def engine_for(source: str) -> str:
    """Which TeX to run, read off the manuscript rather than assumed.

    `% !TEX program = …` is the author saying it outright, so it wins. Failing
    that, the packages that only a Unicode engine can load are real evidence.
    Everything else is pdflatex, which is what the reference manuscript's own
    log says it was built with.
    """
    magic = _MAGIC_RE.search(source or "")
    if magic:
        said = magic.group(1).lower()
        if said in ("xelatex", "lualatex", "pdflatex"):
            return said
    head = (source or "").split("\\begin{document}")[0]
    for pkg in _XETEX_PACKAGES:
        if re.search(r"\\usepackage(\[[^\]]*\])?\{[^}]*\b" + re.escape(pkg) + r"\b", head):
            return "xelatex"
    return "pdflatex"


# ---------------------------------------------------------------- the error

_FILE_LINE_RE = re.compile(r"^(?:\./)?([^\s:]+\.[A-Za-z]{1,4}):(\d+):\s*(.+?)\s*$")
_BANG_RE = re.compile(r"^!\s*(.+?)\s*$")
_BIBTEX_RE = re.compile(r"^(I couldn't open .+|I found no .+|Repeated entry.*)$")
_CONTEXT_RE = re.compile(r"^l\.\d+\s.+?\s*$")
_EPITAPH = ("emergency stop", "==> fatal error")


def _error_on(line: str) -> str | None:
    """The three shapes TeX and BibTeX report an error in, or nothing."""
    m = _FILE_LINE_RE.match(line)
    if m:
        return f"{m.group(1)}:{m.group(2)}: {m.group(3)}"
    bang = _BANG_RE.match(line)
    if bang:
        return bang.group(1)
    if _BIBTEX_RE.match(line.strip()):
        return line.strip()
    return None


def first_error(log: str) -> str | None:
    """The first thing that went wrong, as one line.

    DOCUMENT ORDER DECIDES, not the shape of the line, and that is the whole
    subtlety. A missing file is announced as `! LaTeX Error: File 'x.tex' not
    found.` with no file prefix, and only afterwards does TeX stop with
    `./main.tex:3: Emergency stop.`, which carries a file and a line and no
    information at all. Preferring the richer-looking form -- the obvious
    improvement -- reports the epitaph and throws away the diagnosis. Checked
    against both the terminal output and the `.log`, which agree on the order.
    """
    for line in (log or "").splitlines():
        found = _error_on(line)
        if found:
            return found
    return None


def stopping_error(log: str) -> str | None:
    """The error that ENDED the compile, which is a different question.

    Found by breaking the reference manuscript in a browser and reading what the
    page said. That paper carries twelve RECOVERABLE errors -- `Missing }
    inserted` inside esttab tables -- and compiles to 119 pages with all of
    them. Add a missing `\\input` and it dies, and the first error is still one
    of the twelve, so the panel named a table that had nothing to do with it
    while the file that actually stopped the compile went unmentioned.

    So: when TeX left an epitaph, the error immediately above it is the one that
    killed it. With no epitaph nothing was fatal and the first error is the
    right answer, which is the `No pages of output` case.
    """
    lines = (log or "").splitlines()
    stop = next(
        (i for i, line in enumerate(lines)
         if any(e in line.lower() for e in _EPITAPH)), None)
    if stop is None:
        return first_error(log)
    for j in range(stop - 1, -1, -1):
        found = _error_on(lines[j])
        if found and not any(e in found.lower() for e in _EPITAPH):
            return found
    return first_error(log)


def error_context(log: str, near: str | None = None) -> str:
    """The source line TeX was looking at, which is what names the offender.

    Anchored to the error being reported rather than to the top of the log: a
    manuscript with a dozen recoverable errors has a dozen of these, and the
    first belongs to the first error, not to the one that stopped the run.
    """
    lines = (log or "").splitlines()
    start = 0
    if near:
        start = next((i for i, line in enumerate(lines) if _error_on(line) == near), 0)
    for line in lines[start:]:
        if _CONTEXT_RE.match(line):
            return line.strip()
    return ""


def _diagnosis(log: str) -> str | None:
    head = stopping_error(log)
    if not head:
        return None
    tail = error_context(log, near=head)
    return f"{head}\n{tail}" if tail else head


# --------------------------------------------------------------- running things


def _default_runner(cmd: list[str], *, cwd: Path, env: dict | None = None):
    """One command, its output captured. Returns `(returncode, output)`.

    Both streams together, because TeX writes its errors to stdout and its
    complaints about the terminal to stderr and the author wants the whole
    story in one place.
    """
    try:
        done = subprocess.run(
            cmd, cwd=str(cwd), env=env, capture_output=True, text=True,
            timeout=STEP_TIMEOUT, stdin=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        return 127, f"! {cmd[0]} is not installed"
    except subprocess.TimeoutExpired:
        return 124, f"! {cmd[0]} did not finish within {int(STEP_TIMEOUT)} seconds"
    return done.returncode, (done.stdout or "") + (done.stderr or "")


def _tex_env(manuscript_dir: Path, out: Path) -> dict:
    """TeX and BibTeX search paths, and a terminal wide enough to name a file.

    bibtex runs in the output directory, where the `.aux` is, so it cannot see
    the `.bib` or the `.bst` next to the manuscript unless it is told. A trailing
    colon keeps the distribution's own paths.

    `max_print_line` is not decoration. TeX wraps its output at 79 columns by
    default, INCLUDING the path in an error, so the reference manuscript
    reported `_pre.tex:114: Missing } inserted.` -- a file that does not exist,
    the tail of `tables/table1_balance_patient_pre.tex`. The author would go
    looking for it. Widening the terminal is the fix; stitching the halves back
    together afterwards would be guessing where the break was.
    """
    env = dict(os.environ)
    # THE ENGINE IS NOT TOLD THE MANUSCRIPT DIRECTORY, and that is deliberate.
    # It already runs there, so `\include{tables/t}` resolves through the
    # working directory and TeX reports the path RELATIVE, which is the path the
    # author would type. Put the manuscript on TEXINPUTS as well and the same
    # file is found through the search path instead, so every error line comes
    # back as `/Users/…/latex/tables/table1_balance_patient_pre.tex:114` --
    # correct, unusable, and eighty characters of noise in a status panel.
    # bibtex is the one that genuinely cannot see the manuscript, because it
    # runs in the output directory where the `.aux` is.
    for var in ("BIBINPUTS", "BSTINPUTS"):
        env[var] = f"{manuscript_dir}:{out}:" + env.get(var, "")
    env["TEXINPUTS"] = f"{out}:" + env.get("TEXINPUTS", "")
    env["max_print_line"] = "1000"
    env["error_line"] = "254"
    env["half_error_line"] = "238"
    return env


# ------------------------------------------------------------------- PDF


def compile_pdf(manuscript_dir, *, main: str | None = None, bib: str | None = None,
                on_step=None, runner=None) -> Result:
    """Three passes around a bibtex, which is the author's documented recipe.

    Three because the first writes the `.aux` with the citation keys, bibtex
    turns those into a `.bbl`, the second pulls the bibliography in and moves
    every page number, and the third settles the cross-references against the
    pages they finally landed on.

    Each step is announced through `on_step` the moment it finishes rather than
    collected and handed over at the end, because the whole thing takes tens of
    seconds and a control that goes quiet for that long reads as broken.
    """
    root = Path(manuscript_dir).resolve()
    run = runner or _default_runner
    main_tex = build_mod.find_main_tex(root, main)
    out = out_dir(root)
    mirror_tex_dirs(root, out)

    try:
        engine = engine_for(main_tex.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        engine = "pdflatex"

    pdf = out / (main_tex.stem + ".pdf")
    before = pdf.stat().st_mtime_ns if pdf.exists() else None
    env = _tex_env(root, out)
    tex_cmd = [engine, *ENGINE_FLAGS, f"-output-directory={out}", main_tex.name]

    plan = [
        ("pass 1 of 3", tex_cmd, root),
        ("bibtex", ["bibtex", main_tex.stem], out),
        ("pass 2 of 3", tex_cmd, root),
        ("pass 3 of 3", tex_cmd, root),
    ]

    started = time.monotonic()
    steps: list[Step] = []
    notes: list[str] = []
    transcript: list[str] = []
    for name, cmd, cwd in plan:
        t0 = time.monotonic()
        code, output = run(cmd, cwd=cwd, env=env)
        took = time.monotonic() - t0
        transcript.append(output or "")
        # bibtex reports a paper with no citations as a failure, and that paper
        # still compiles. A step's own status is recorded; only the PDF decides.
        detail = "" if code == 0 else (stopping_error(output) or f"exited {code}")
        step = Step(name, code == 0, took, detail)
        steps.append(step)
        if on_step:
            on_step(step)
        if name == "bibtex" and code != 0 and detail:
            notes.append(f"bibtex: {detail}")
        if _fatal(output):
            break

    log = out / (main_tex.stem + ".log")
    whole = "\n".join(transcript)
    ok = pdf.exists() and (before is None or pdf.stat().st_mtime_ns != before)
    error = None
    if not ok:
        error = _diagnosis(whole) or _diagnosis(_read(log)) or \
            "the compile produced no PDF and said nothing about why"
    return Result(
        kind="pdf", ok=ok, output=pdf if ok else None,
        seconds=time.monotonic() - started, steps=steps, error=error,
        log=log if log.exists() else None, notes=notes,
    )


def _fatal(output: str) -> bool:
    """TeX gave up. Running the remaining passes would waste half a minute."""
    low = (output or "").lower()
    return "==> fatal error occurred" in low or "emergency stop" in low


def _read(path: Path) -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


# ------------------------------------------------------------------- Word


def compile_docx(manuscript_dir, *, main: str | None = None, bib: str | None = None,
                 on_step=None, runner=None) -> Result:
    """LaTeX to a Word file that opens, by way of the `pandoc-docx` skill.

    THE SKILL HAS NO SINGLE ENTRY POINT. It is a recipe plus four bundled
    assets, so the only thing to shell out to is each script in turn:
    `merge_csl_bib.py` for the split numeric bibliography and `format_docx.py`
    for the publication pass over the OOXML. Those are shelled out to here, not
    rewritten.

    ONE STEP OF THE RECIPE CANNOT BE FOLLOWED LITERALLY. The skill feeds
    `main.tex` to pandoc and lets pandoc expand the includes, and on this pandoc
    (3.1.1) it does expand them -- the project's older note that it "follows
    neither `\\input` nor `\\include`" no longer holds and was measured again
    here. But following them is exactly what kills it. Pandoc reads the included
    file RAW, so it meets the `\\newcolumntype{m}[1]{…#1…}` that opens every
    esttab table in the corpus and aborts the WHOLE document with
    `unexpected #1`: on the reference manuscript, exit 65 and no output at all.

    `normalize_for_pandoc` is what neutralizes that, and it can only neutralize
    what it can see, so the includes have to be resolved BEFORE it runs rather
    than by pandoc afterwards. That is `flatten()`, and it is the same order the
    render already uses. The skill's own appendix says the same thing in its own
    words: inline the includes yourself if the cleaning has to reach inside the
    exhibit files.

    Cross-references are resolved from the `.aux` first, which is the job
    `resolve_refs.py` does in the skill, done on the flattened text so it reaches
    the appendices and so `\\pageref` has somewhere to come from.
    """
    from manuscriptor.render import pandoc as pandoc_mod
    from manuscriptor.render import refs as refs_mod
    from manuscriptor.source.flatten import flatten

    root = Path(manuscript_dir).resolve()
    run = runner or _default_runner
    started = time.monotonic()
    steps: list[Step] = []
    notes: list[str] = []

    missing = [str(p) for p in (SKILL_DIR, SKILL_SCRIPTS["format"], SKILL_SCRIPTS["merge_bib"])
               if not p.exists()]
    if missing:
        return Result(
            kind="docx", ok=False, output=None, seconds=0.0, steps=[],
            error=("the pandoc-docx skill is not installed, and the Word conversion is "
                   "its work rather than this module's. Missing: " + ", ".join(missing)),
            log=None,
        )

    main_tex = build_mod.find_main_tex(root, main)
    bib_path = build_mod.find_bib(root, bib)
    out = out_dir(root)
    docx = out / (main_tex.stem + ".docx")

    def record(name, ok, t0, detail=""):
        step = Step(name, ok, time.monotonic() - t0, detail)
        steps.append(step)
        if on_step:
            on_step(step)
        return step

    # The .aux is where every cross-reference number lives, and HTML has no
    # pages, so `\pageref` can come from nowhere else. Compiling once is what
    # the skill says to do when it is missing.
    aux = out / (main_tex.stem + ".aux")
    if not aux.exists():
        beside = main_tex.with_suffix(".aux")
        if beside.exists():
            aux = beside
        elif shutil.which("pdflatex") or shutil.which("xelatex"):
            t0 = time.monotonic()
            pre = compile_pdf(root, main=main, on_step=on_step, runner=runner)
            steps.extend(pre.steps)
            if not aux.exists():
                notes.append("cross-references could not be resolved: no .aux was produced")
        else:
            notes.append("no TeX engine, so cross-references are unresolved")

    t0 = time.monotonic()
    flat = flatten(main_tex)
    labels = refs_mod.load_labels(aux) if aux.exists() else {}
    resolved, unresolved = refs_mod.resolve_source(flat.text, labels)
    source = pandoc_mod.normalize_for_pandoc(resolved)
    source, rasterized = _rasterize_pdf_figures(source, root, out, run)
    inter_tex = out / "inter.tex"
    inter_tex.write_text(source, encoding="utf-8")
    if unresolved:
        notes.append(f"{len(unresolved)} unresolved cross-reference(s): {', '.join(sorted(set(unresolved))[:4])}")
    if rasterized:
        notes.append(f"{rasterized} PDF figure(s) rasterized; Word cannot open a docx holding one")
    record("flatten and resolve", True, t0, f"{len(flat.text)} characters")

    # The HTML route, because HTML round-trips tables far better than Markdown
    # and this is a manuscript. Citeproc runs here rather than at the docx step,
    # since the HTML reader will not re-detect the keys afterwards.
    t0 = time.monotonic()
    inter_html = out / "inter.html"
    cmd = ["pandoc", str(inter_tex), "-f", "latex", "-t", "html",
           f"--resource-path={root}", "-o", str(inter_html)]
    if bib_path is not None:
        cmd += ["--citeproc", f"--bibliography={bib_path}"]
        csl = _find_csl(root)
        if csl:
            cmd.append(f"--csl={csl}")
    code, output = run(cmd, cwd=root, env=None)
    record("pandoc to html", code == 0, t0, "" if code == 0 else output.strip()[:300])
    if code != 0 or not inter_html.exists():
        return Result("docx", False, None, time.monotonic() - started, steps,
                      output.strip()[:600] or "pandoc could not read the manuscript", None, notes)

    # A numeric CSL emits each reference as two divs and pandoc turns each into
    # its own paragraph, so every reference number lands on a line by itself.
    t0 = time.monotonic()
    code, output = run(["python3", str(SKILL_SCRIPTS["merge_bib"]), str(inter_html)], cwd=out)
    record("merge the bibliography", code == 0, t0, output.strip()[:200])

    t0 = time.monotonic()
    code, output = run(["pandoc", str(inter_html), "-f", "html", "-t", "docx", "-o", str(docx)],
                       cwd=root, env=None)
    record("pandoc to docx", code == 0, t0, "" if code == 0 else output.strip()[:300])
    if code != 0 or not docx.exists():
        return Result("docx", False, None, time.monotonic() - started, steps,
                      output.strip()[:600] or "pandoc could not write the docx", None, notes)

    # Sandbox-written files carry com.apple.quarantine and Word blocks on it.
    run(["xattr", "-c", str(docx)], cwd=out)

    t0 = time.monotonic()
    code, output = run(["python3", str(SKILL_SCRIPTS["format"]), str(docx), str(docx)], cwd=out)
    record("publication formatting", code == 0, t0, output.strip()[-200:])
    if code != 0:
        notes.append("the skill's formatting pass failed; the file opens but is not formatted")

    # textutil is Apple's own OOXML reader and is as strict as Word, which
    # python-docx is not. It is the only check here that means anything.
    t0 = time.monotonic()
    opens = True
    if shutil.which("textutil"):
        code, output = run(["textutil", "-info", str(docx)], cwd=out)
        opens = code == 0 and bool(output.strip())
        record("check it opens", opens, t0, "textutil read it" if opens else "textutil could not read it")
    if not opens:
        return Result("docx", False, None, time.monotonic() - started, steps,
                      "the file was written but Word will not open it (textutil could not read it)",
                      None, notes)

    return Result("docx", True, docx, time.monotonic() - started, steps, None, None, notes)


_GRAPHICS_RE = re.compile(r"(\\includegraphics\s*(?:\[[^\]]*\])?\s*\{)([^}]+)(\})")


def _rasterize_pdf_figures(source: str, root: Path, out: Path, run) -> tuple[str, int]:
    """PDF figures become PNGs, or the docx will not open at all.

    Pandoc copies a PDF figure verbatim into `word/media/`, Word cannot parse a
    PDF-format image inside a docx, and it fails to open the ENTIRE package. The
    rasterizer and the resolution are the skill's.
    """
    if not shutil.which("pdftoppm"):
        return source, 0
    made = {}

    def one(m):
        target = m.group(2).strip()
        candidate = (root / target)
        if candidate.suffix.lower() != ".pdf":
            candidate = candidate.with_suffix(".pdf")
        if not candidate.exists():
            return m.group(0)
        if target not in made:
            stem = out / ("fig-" + re.sub(r"\W+", "-", str(Path(target).with_suffix("")).strip("./")))
            run(["pdftoppm", "-png", "-r", "200", "-singlefile", str(candidate), str(stem)], cwd=root)
            png = Path(str(stem) + ".png")
            made[target] = str(png) if png.exists() else None
        return m.group(1) + made[target] + m.group(3) if made[target] else m.group(0)

    swapped = _GRAPHICS_RE.sub(one, source)
    return swapped, sum(1 for v in made.values() if v)


def _find_csl(directory: Path) -> Path | None:
    found = sorted(Path(directory).glob("*.csl"))
    if found:
        return found[0]
    home = Path.home() / ".csl" / "econ.csl"
    return home if home.exists() else None


# ------------------------------------------------------------------ revealing


def reveal(path, *, root, runner=None) -> Path:
    """Show the compiled file in the Finder.

    The page asks for this, and the page is not handed an arbitrary path just
    because it is served on localhost: only what a compile actually produced,
    inside the build directory, can be revealed.
    """
    target = Path(path).resolve()
    allowed = out_dir(root)
    if not _inside(target, allowed):
        raise ValueError(f"{target} is not something a compile produced")
    if not target.exists():
        raise ValueError(f"{target} is not there any more")
    (runner or _reveal_runner)(["open", "-R", str(target)])
    return target


def _reveal_runner(cmd: list[str]) -> None:
    subprocess.run(cmd, capture_output=True, check=False)


# --------------------------------------------------------------- the route
#
# ONE route, `POST /compile`, carrying the action. The progress goes back over
# the websocket that already exists rather than down a second channel, which is
# how everything else in this server drives the page.

_BUSY: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()
# asyncio keeps only a weak reference to a running task, so a compile with
# nothing else holding it can be collected mid-run and simply stop, silently.
_RUNNING: set = set()

LABELS = {"pdf": "Compile PDF", "docx": "Compile Word"}


def route(session):
    """An aiohttp handler bound to one session."""
    import asyncio

    from aiohttp import web

    async def handler(request):
        try:
            data = await request.json()
        except Exception:
            data = {}
        action = str(data.get("action") or "")

        if action == "reveal":
            try:
                shown = reveal(data.get("path", ""), root=session.root)
            except ValueError as exc:
                return web.json_response({"error": str(exc)}, status=400)
            return web.json_response({"revealed": str(shown)})

        if action not in ("pdf", "docx"):
            return web.json_response(
                {"error": f"unknown compile action: {action!r}"}, status=400)

        if _BUSY.get(session):
            return web.json_response(
                {"error": f"a {_BUSY[session]} compile is already running"}, status=409)
        _BUSY[session] = action

        loop = asyncio.get_running_loop()

        def announce(msg):
            asyncio.run_coroutine_threadsafe(session.broadcast(msg), loop)

        async def work():
            try:
                await session.broadcast({
                    "type": "compile", "phase": "start", "kind": action,
                    "label": LABELS[action],
                })

                def on_step(step):
                    announce({
                        "type": "compile", "phase": "step", "kind": action,
                        "step": step.name, "ok": step.ok,
                        "seconds": round(step.seconds, 1), "detail": step.detail,
                    })

                fn = compile_pdf if action == "pdf" else compile_docx
                result = await asyncio.to_thread(
                    fn, session.root, main=session.doc, bib=session.bib, on_step=on_step
                )
                await session.broadcast(result.as_frame(root=session.root))
            except Exception as exc:  # a failed compile must not kill the server
                await session.broadcast({
                    "type": "compile", "phase": "done", "kind": action, "ok": False,
                    "error": f"{type(exc).__name__}: {exc}", "steps": [], "notes": [],
                    "output": None, "url": None, "seconds": 0, "log": None,
                })
            finally:
                _BUSY.pop(session, None)

        task = loop.create_task(work())
        _RUNNING.add(task)
        task.add_done_callback(_RUNNING.discard)
        return web.json_response({"started": action}, status=202)

    return handler
