r"""Which files are written by analysis code.

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

**A mention is not a claim; a write is.** The first cut of the producer scan
matched the filename and stopped there, which reads a script that opens a file
and a script that writes one as the same fact. dsp-bias has both in four
consecutive lines of `paper/make_word_submission.py`: it READS `main.tex` and
WRITES `main_anonymous.tex`, and the scan claimed the manuscript itself. All 73
blocks of that manuscript had a claimed host, and only the root-file rescue in
`apply` kept them editable -- so the 74% failure was armed, one document switch
or one `\input` away. What is computed here is therefore the verb, not the noun:
a filename counts only when the statement it sits in, or the name it is bound
to, is doing the writing.

**A `.tex` is not the only artifact a script writes.** covet-india's
`sample.bib` is exported from Zotero by `manuscript/make-bib.py`, and an entry
appended to it is destroyed by the next `make bib` while the `\citep{...}` that
needs it survives -- a citation with no bibliography entry, and a broken build
the author did not cause. The question "who writes this file" is the same
question whatever the suffix, so it is answered here, once, by `provenance()`.
Callers ask that function; they do not re-derive an answer from a path or a
directory name.

**A generated file's own header is a second SIGNAL, not a second decision.** A
producer scan can only see scripts it can reach, and a Makefile rule or a
generator living in another repo is invisible to it. A file whose leading
comments say `GENERATED FILE` / `DO NOT EDIT` -- and often name the generator
and the command that regenerates it -- is telling us the same fact its producer
would have. That signal feeds `provenance()` and lives nowhere else.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# Where analysis code tends to live, relative to the repo root.
_CODE_DIRS = ("code", "scripts", "do", "R", "src", "analysis", "stata", "dofiles")
_CODE_SUFFIXES = (".R", ".r", ".do", ".py", ".Rmd", ".qmd")

_SKIP_DIRS = {".git", "renv", "node_modules", "__pycache__", ".venv", "venv", "build", "data"}

# The artifacts a manuscript is built from and that a script may write. `.bib`
# is here because covet-india's bibliography is exported from Zotero, and an
# append to it is silently undone by the next export.
_OUT_SUFFIXES = ("tex", "bib")
_SUFFIX_ALT = "|".join(_OUT_SUFFIXES)

_TEX_LITERAL_RE = re.compile(rf"""['"]([^'"\n]*?\.(?:{_SUFFIX_ALT}))['"]""")
# Stata names its files bare as often as quoted: `esttab using tabA.tex`.
_DO_TEX_RE = re.compile(
    rf"""['"]([^'"\n]*?\.(?:{_SUFFIX_ALT}))['"]|\busing\s+([^\s,"]+\.(?:{_SUFFIX_ALT}))"""
)
# A captured string is only a filename if it could be one. A progress line like
# `cat("wrote outputs/tab_main.tex\n", file = log)` sits in a real write and
# ends in a real basename, and would hand `tab_main.tex` to whichever script
# merely announced it. Whitespace is the tell; braces are NOT, because
# estonia-qbs writes every exhibit through a Stata global --
# `using "${exhibits}/T1.tex"`.
_PATHISH_RE = re.compile(r"^\S+$")
_NOT_NEWLINE_RE = re.compile(r"[^\n]")

# Jump between the characters that start a string or a comment, rather than
# testing every character in the file for being one of them. `scan` reads every
# script in the repo on every save; the difference is most of that save.
_PY_TOKENS = re.compile(r'"""|\'\'\'|"|\'|#')
_R_TOKENS = re.compile(r'"|\'|#')
# Stata's single quote delimits a macro, not a string, and a `*` in the first
# column is a comment while a `*` anywhere else is multiplication.
_DO_TOKENS = re.compile(r'(?m)"|//|/\*|^[ \t]*\*')
_STR_END = {
    '\\"': re.compile(r'(?:\\.|[^"\\\n])*"?'),
    "\\'": re.compile(r"(?:\\.|[^'\\\n])*'?"),
    '"': re.compile(r'[^"\n]*"?'),
}

# ------------------------------------------------------- reading vs writing
#
# Deliberately asymmetric. An unmatched verb yields no claim, so every gap in
# these patterns costs an under-claim, never an over-claim -- and under-claims
# fall through to `looks_generated`, which recognises the fragments whose
# accidental editing actually loses analytical work. An over-claim has no such
# backstop: it refuses a whole file of prose and there is nothing downstream to
# notice.

_PY_R_WRITE = re.compile(r"""
      \bopen\s*\([^()]*,\s*['"][wax]                # open(p, "w")
    | \.open\s*\(\s*['"][wax]                       # Path(p).open("w")
    | \bwrite[A-Za-z_.]*\s*\(                       # writeLines, write.csv, write_csv
    | \.write(_text|_bytes)?\s*\(
    | \bsink\s*\(
    | \b(saveRDS|savefig|ggsave|dump)\s*\(
    | \.to_(latex|csv|string|html|markdown)\s*\(
    | \b(file|cat|capture\.output|print|writeLines)\s*\([^\n]*\bfile\s*=
    | \b(kable|kableExtra|stargazer|xtable|texreg|modelsummary|esttab|etable|gtsave)\b
      [^\n]*\b(file|out|output)\s*=
    | \bfile\s*\([^()]*,\s*['"][wa]                 # R's file(p, "w")
""", re.X)

_PY_R_READ = re.compile(r"""
      \bopen\s*\((?![^()]*,\s*['"][wax])            # open(p) / open(p, "r")
    | \.read(_text|_bytes|lines|line)?\s*\(
    | \bread[A-Za-z_.]*\s*\(                        # readLines, read.csv, readRDS
    | \bscan\s*\(
    | \b(json|yaml|toml)\.load\b
    | \bincludegraphics\b
""", re.X)

# Two arguments, one read and one written, and the written one is last.
_COPY_RE = re.compile(r"""
      \b(copyfile|copytree|copy2|copy|move|rename|replace)\s*\(
    | \bfile\.(copy|rename|append)\s*\(
""", re.X)

_DO_WRITE = re.compile(r"""
      \bfile\s+write\b
    | \bfile\s+open\b[^\n]*\bwrite\b
    | \b(esttab|estout|outreg2?|outtable|tabout|texsave|texdoc|xml_tab|estwrite)\b
      [^\n]*\busing\b
    | \bexport\s+\w+\b[^\n]*\busing\b
    | \b(outsheet|save|saveold)\b[^\n]*\busing\b
    | \b(putexcel|putdocx|putpdf)\b
    | \blog\s+using\b
    | \bgraph\s+export\b
""", re.X)

_DO_READ = re.compile(r"""
      \bfile\s+read\b
    | \bfile\s+open\b[^\n]*\bread\b
    | \b(use|insheet|infile|import|append|merge)\b
""", re.X)

# A script's own one-line wrapper around a write is still a write. qutub-india's
# `20_attrition.R` defines `exp_frag <- function(fname, value)` and names
# thirteen of its exhibits through it and through no other verb.
_R_DEF_RE = re.compile(r"(?m)^\s*([A-Za-z_.][\w.]*)\s*(?:<<-|<-|=)\s*function\s*\(")
_PY_DEF_RE = re.compile(r"(?m)^([ \t]*)def\s+([A-Za-z_]\w*)\s*\(")

# `p = <expr naming a file>` on its own says nothing; what happens to `p` does.
_BINDING_RE = re.compile(r"""
      ^\s*(?:local\s+|global\s+|scalar\s+)?([A-Za-z_.][\w.]*)\s*(?:<<-|<-|=)\s*[^=]
    | ^\s*(?:local|global)\s+([A-Za-z_][\w]*)\s+["`]
""", re.X)

# The markup that only ever appears in machine-written exhibits.
_TABULAR_RE = re.compile(r"\\(?:toprule|midrule|bottomrule|hline|cmidrule|multicolumn|multirow)\b")
# A sentence: some words, then a full stop followed by a space or a line end.
_SENTENCE_RE = re.compile(r"[A-Za-z][a-z]+(?:[ ,;][A-Za-z][a-z]+){4,}[.?!](?:\s|$)")
_PROSE_STRUCTURE_RE = re.compile(r"\\(?:section|subsection|subsubsection|paragraph)\b")


def scan(manuscript_dir: Path) -> dict[Path, Path]:
    """Map each generated artifact to the script that writes it.

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
    for suffix in _OUT_SUFFIXES:
        for tex in manuscript_dir.rglob(f"*.{suffix}"):
            if _skipped(tex, manuscript_dir):
                continue
            by_name.setdefault(tex.name, []).append(tex.resolve())

    produced: dict[Path, Path] = {}
    for script in scripts:
        try:
            text = script.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for name in written_names(text, script.suffix):
            for target in by_name.get(name, ()):
                produced.setdefault(target, script.resolve())
    return produced


def written_names(text: str, suffix: str) -> set[str]:
    """The basenames a script WRITES, as opposed to the ones it merely names.

    Two passes. The first takes the statement a filename sits in at its word: a
    literal inside `writeLines(...)` or `esttab ... using` is an output. The
    second follows one hop of assignment, because the common shape is a name
    bound on one line and written three lines later --

        anon_tex = os.path.join(PAPER, "main_anonymous.tex")
        ...
        open(anon_tex, "w").write(anon)

    -- and the neighbouring line binds `src` to `main.tex` and only ever reads
    it. One hop is enough for every producer in the corpus and stops well short
    of guessing.
    """
    # Scanned once and threaded through. `scan` runs on every save, so the four
    # separate passes this used to make were four passes over every script in
    # the repo between the author pressing save and the paragraph moving.
    comments, strings = _spans(text, suffix)
    masked = _blank(text, comments + strings)
    stmts = _statements(text, masked)
    pattern = _DO_TEX_RE if suffix == ".do" else _TEX_LITERAL_RE
    # Classify against the code with its comments blanked and its line breaks
    # collapsed: a Stata command continued with `///` puts `esttab` and `using`
    # on different lines, and estonia-qbs writes three of its seven exhibits
    # that way.
    code = _blank(text, comments)

    names: list[list[str]] = []
    for start, end, raw in stmts:
        found: list[str] = []
        for m in pattern.finditer(raw):
            token = (m.group(1) or (m.group(2) if pattern is _DO_TEX_RE else "") or "").strip()
            if not token or not _PATHISH_RE.match(token):
                continue
            if any(a <= start + m.start() < b for a, b in comments):
                continue
            found.append(Path(token).name)
        names.append(found)

    # Classified on demand and remembered. Most statements in a script are never
    # asked about -- only the ones naming a file, and the ones mentioning a name
    # bound to one -- and classifying all of them eagerly cost more than every
    # other part of the scan put together.
    verdicts: dict[int, str] = {}

    def verdict(i: int) -> str:
        if i not in verdicts:
            start, end, _raw = stmts[i]
            # Comments blanked and line breaks collapsed: a Stata command
            # continued with `///` puts `esttab` and `using` on different lines,
            # and estonia-qbs writes three of its seven exhibits that way.
            verdicts[i] = _classify(" ".join(code[start:end].split()), suffix)
        return verdicts[i]

    for fn in _local_writers(text, suffix, masked):
        call = re.compile(r"(?<![\w.])" + re.escape(fn) + r"\s*\(")
        for i, (start, end, _raw) in enumerate(stmts):
            if names[i] and verdict(i) == "unknown" and call.search(code[start:end]):
                verdicts[i] = "write"

    written: set[str] = set()
    for i, found in enumerate(names):
        if not found:
            continue
        if verdict(i) == "write":
            written.update(found)
        elif verdict(i) == "both":
            # A read and a write in one statement is `copy(src, dst)` shaped,
            # and the destination is the last name in it.
            written.add(found[-1])

    for i, found in enumerate(names):
        if not found or verdict(i) != "unknown":
            continue
        ident = _bound_name(stmts[i][2])
        if ident and _use_of(ident, stmts, verdict, skip=i) == "write":
            written.update(found)
    return written


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


# ------------------------------------------------------- the header a generator writes
#
# Two signals, one decision. The header is read only from the leading run of
# comment lines, so a `%% DO NOT EDIT` discussed halfway down a prose appendix
# claims nothing.

_GENERATED_RE = re.compile(r"GENERATED FILE|DO NOT EDIT", re.I)
_GENERATOR_RE = re.compile(r"(?im)^[\s%#/*!;-]*generator\s*:\s*(\S+)\s*$")
# `[^\S\n]*` and not `\s*`: `\s` crosses the newline, and the tail of
# `%% Regenerate (Zotero must be running):` is then the NEXT line's comment
# marker -- a remedy reading `%%`, which is a refusal with no way out of it.
_REGEN_RE = re.compile(r"(?im)^[\s%#/*!;-]*regenerate[^:\n]*:[^\S\n]*(.*)$")
_COMMENT_RE = re.compile(r"^\s*(?:%|#|//|;|\*)")
_HEADER_LINES = 60


@dataclass(frozen=True)
class Provenance:
    """Who writes a file, and how to make it write it again.

    `signal` says which evidence decided it -- `producer` (a script naming the
    file in a write), `header` (the file's own banner), `content` (it reads as
    analysis output), or `none`. Kept because the three are not equally strong
    and a caller reporting a refusal should be able to say what it saw.
    """

    generated: bool
    producer: str | None = None
    remedy: str | None = None
    signal: str = "none"


def provenance(path: Path, produced: dict[Path, Path] | None = None) -> Provenance:
    """THE answer to "is this file written by something other than the author".

    Whatever the suffix. A producer match is definitive and outranks everything;
    the file's own header is next; the content guess is last and applies only to
    `.tex`, because it was written to recognise a table fragment and says
    nothing useful about a `.bib`.
    """
    p = Path(path)
    try:
        resolved = p.resolve()
    except OSError:
        resolved = p
    head = _header(p)
    script = (produced or {}).get(resolved)
    if script is not None:
        # The scan says WHO writes it; the header, when there is one, says HOW
        # to make it write again. `make bib` is a better instruction than the
        # path of a script the author may not invoke directly.
        remedy = _regen_command(head or "") or f"re-run {script}"
        return Provenance(True, str(script), remedy, "producer")

    if head is not None and _GENERATED_RE.search(head):
        gen = _GENERATOR_RE.search(head)
        return Provenance(
            True,
            gen.group(1) if gen else None,
            _regen_command(head) or (f"re-run {gen.group(1)}" if gen else None),
            "header",
        )

    if p.suffix == ".tex" and looks_generated(p):
        return Provenance(True, None, None, "content")
    return Provenance(False)


def _header(path: Path) -> str | None:
    """The leading run of comment lines, or None if the file cannot be read."""
    try:
        with Path(path).open(encoding="utf-8", errors="replace") as fh:
            lines = []
            for i, line in enumerate(fh):
                if i >= _HEADER_LINES:
                    break
                if line.strip() and not _COMMENT_RE.match(line):
                    break
                lines.append(line)
    except OSError:
        return None
    return "".join(lines)


def _regen_command(head: str) -> str | None:
    """What the header says to run. The command is often on the NEXT line.

    covet-india writes `%% Regenerate (Zotero must be running):`, a blank
    comment line, then the command indented under it. Taking only the tail of
    the matched line would report an empty remedy, which is a refusal with no
    way out of it.
    """
    m = _REGEN_RE.search(head)
    if not m:
        return None
    tail = m.group(1).strip()
    if tail:
        return tail
    for line in head[m.end():].splitlines():
        body = _COMMENT_RE.sub("", line).strip().lstrip("%#/;*").strip()
        if body:
            return body
    return None


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
        if host not in cache:
            # One decision function, so a block and a bibliography are answered
            # the same way. A producer match outranks being the root: dsp-bias's
            # `main_anonymous.tex` is a served document in its own right AND is
            # rewritten by `make_word_submission.py` on every build; editing it
            # loses the edit at the next `make`, which is the whole reason this
            # module exists.
            p = provenance(host, produced)
            # The root is exempt from the CONTENT guess and from nothing else.
            # That guess reads a preamble followed by a column of `\input` lines
            # as a fragment -- no sentences, barely any prose -- and would
            # refuse the one file the author is certainly writing. This used to
            # exempt the root from `produced` as well, which quietly propped up
            # a scan that claimed every file it saw an analysis script mention.
            cache[host] = p.generated and not (host == root_file and p.signal == "content")
        generated = cache[host]

        kind = "generated" if generated else b.kind
        if b.kind == "generated" and not generated:
            kind = "paragraph"

        out.append(_replace(b, kind=kind, editable=b.editable and not generated))
    return tuple(out)


# ----------------------------------------------------------------- internals


def _classify(raw: str, suffix: str) -> str:
    """What this statement does to the file it names: write, read, or nothing."""
    if suffix == ".do":
        wr, rd = bool(_DO_WRITE.search(raw)), bool(_DO_READ.search(raw))
    else:
        if _COPY_RE.search(raw):
            return "both"
        wr, rd = bool(_PY_R_WRITE.search(raw)), bool(_PY_R_READ.search(raw))
    if wr and rd:
        return "both"
    return "write" if wr else ("read" if rd else "unknown")


def _local_writers(text: str, suffix: str, masked: str) -> set[str]:
    """Functions this file defines whose bodies write.

    One more hop of the same kind as the variable binding, and for the same
    reason: the write verb is real, it is just one name away from the filename.
    Iterated to a fixed point so a wrapper around a wrapper still resolves,
    which is what `exp_frag -> write_frag` is.
    """
    if suffix == ".do":
        return set()
    bodies: list[tuple[str, str]] = []
    if suffix in (".py",):
        for m in _PY_DEF_RE.finditer(masked):
            bodies.append((m.group(2), text[m.start():_py_body_end(masked, m)]))
    else:
        for m in _R_DEF_RE.finditer(masked):
            a, b = _r_body_span(masked, m.end())
            bodies.append((m.group(1), text[a:b]))

    writers: set[str] = set()
    for _ in range(3):
        grew = False
        for name, body in bodies:
            if name in writers:
                continue
            flat = " ".join(body.split())
            if _classify(flat, suffix) in ("write", "both") or any(
                re.search(r"(?<![\w.])" + re.escape(w) + r"\s*\(", flat) for w in writers
            ):
                writers.add(name)
                grew = True
        if not grew:
            break
    return writers


def _r_body_span(masked: str, pos: int) -> tuple[int, int]:
    """From an R `function(` to the end of its body: the braces, or the line."""
    depth, i, n = 1, pos, len(masked)
    while i < n and depth:                                   # close the arg list
        depth += (masked[i] == "(") - (masked[i] == ")")
        i += 1
    j = i
    while j < n and masked[j] in " \t":
        j += 1
    if j >= n or masked[j] != "{":                           # a one-line function
        end = masked.find("\n", i)
        return pos, n if end < 0 else end
    depth, k = 1, j + 1
    while k < n and depth:
        depth += (masked[k] == "{") - (masked[k] == "}")
        k += 1
    return j, k


def _py_body_end(masked: str, m) -> int:
    """The end of a `def`'s suite: the first later line indented no further."""
    indent = len(m.group(1).expandtabs())
    i = masked.find("\n", m.end())
    if i < 0:
        return len(masked)
    for line in masked[i + 1:].splitlines(keepends=True):
        if line.strip() and len(line.expandtabs()) - len(line.expandtabs().lstrip()) <= indent:
            return i + 1
        i += len(line)
    return len(masked)


def _bound_name(raw: str):
    m = _BINDING_RE.match(raw.lstrip("\n"))
    return (m.group(1) or m.group(2)) if m else None


def _use_of(ident: str, stmts, verdict, *, skip: int) -> str:
    """What the rest of the file does to a name. A write anywhere settles it.

    A write wins over a read because read-modify-write is a real shape and the
    file is still an output at the end of it.
    """
    word = re.compile(r"(?<![\w.])" + re.escape(ident) + r"(?![\w])")
    seen = "unknown"
    for i, (_s, _e, raw) in enumerate(stmts):
        if i == skip or not word.search(raw):
            continue
        v = verdict(i)
        if v in ("write", "both"):
            return "write"
        if v == "read":
            seen = "read"
    return seen


def _statements(text: str, masked: str) -> list[tuple[int, int, str]]:
    """Logical statements: physical lines joined while a bracket is still open.

    A call may be spread over four lines, and the mode argument that says this
    is a write is rarely on the line holding the filename. `masked` is the same
    text with strings and comments blanked, so a bracket inside either does not
    run the join away.
    """
    lines = text.splitlines(keepends=True)
    mlines = masked.splitlines(keepends=True)
    out: list[tuple[int, int, str]] = []
    pos = start = depth = 0
    buf: list[str] = []
    for line, mline in zip(lines, mlines):
        if not buf:
            start = pos
        buf.append(line)
        # Parentheses and subscripts only. Braces delimit BLOCKS, not calls, and
        # counting them swallowed a whole R `if/else` into one statement --
        # qutub-india's `09_pp_fragments.R` writes eleven fragments inside one,
        # and only the last of them was still being claimed.
        depth += sum(mline.count(c) for c in "([") - sum(mline.count(c) for c in ")]")
        pos += len(line)
        # Stata continues a command with a trailing `///`.
        if depth <= 0 and not line.rstrip().endswith("///"):
            out.append((start, pos, "".join(buf)))
            buf, depth = [], 0
    if buf:
        out.append((start, pos, "".join(buf)))
    return out


def _spans(text: str, suffix: str) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
    """Where the comments are and where the strings are, as byte ranges.

    Comments so a filename discussed in one is not mistaken for a filename
    written by one -- dsp-bias's R scripts mention `main.tex` in six comments.
    Strings so a bracket inside one does not run the statement splitter away.
    """
    comments: list[tuple[int, int]] = []
    strings: list[tuple[int, int]] = []
    stata = suffix == ".do"
    tok = _DO_TOKENS if stata else (_PY_TOKENS if suffix == ".py" else _R_TOKENS)
    n, i = len(text), 0
    while i < n:
        m = tok.search(text, i)
        if not m:
            break
        a, t = m.start(), m.group()
        if t == "/*":                                        # Stata block comment
            j = text.find("*/", a + 2)
            j = n if j < 0 else j + 2
            comments.append((a, j))
        elif t in ("//", "#") or t.endswith("*"):            # to end of line
            # `t.endswith("*")` is Stata's leading-star comment, whose match
            # includes the indent before it.
            j = text.find("\n", a)
            j = n if j < 0 else j
            comments.append((a, j))
        elif len(t) == 3:                                    # Python triple quote
            j = text.find(t, a + 3)
            j = n if j < 0 else j + 3
            strings.append((a, j))
        else:                                                # an ordinary string
            end = _STR_END[t if stata else ("\\" + t)].match(text, a + 1)
            j = end.end() if end else n
            strings.append((a, j))
            i = j
            continue
        i = max(j, a + 1)
    return comments, strings


def _blank(text: str, spans) -> str:
    """`text` with those ranges replaced by spaces, same length, newlines kept.

    Offsets have to survive, because the literal scan runs on the raw text and
    the classifier slices this one at the same positions. Built by slicing
    rather than a loop over characters: a 700-line R script is mostly strings,
    and blanking them one character at a time was most of a save's latency.
    """
    if not spans:
        return text
    out: list[str] = []
    last = 0
    for a, b in sorted(spans):
        if b <= last:
            continue
        a = max(a, last)
        out.append(text[last:a])
        out.append(_NOT_NEWLINE_RE.sub(" ", text[a:b]))
        last = b
    out.append(text[last:])
    return "".join(out)


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
