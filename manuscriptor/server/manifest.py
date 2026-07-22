"""What a computed value IS, derived from the code that writes it.

`producers.py` answers "which script writes this file". That is enough to refuse
an edit and not enough to read a sentence: the References tab can say
`correct_p2_wb` was written by `R/10_patna_itt.R` and cannot say that the number
in the prose is a wild-cluster bootstrap p-value on correct case management at
round 2. This module answers the second question, and only where the code
answers it.

NOTHING HERE CALLS A MODEL. A description is static analysis of the author's own
scripts: find the write, recover the filename template, bind its variables to
the literal domains the loops iterate, and read the statistic off the expression
that was written. Where any of that fails, the honest answer is "producer
unknown", and it is returned rather than a plausible sentence. A wrong
description of a coefficient is worse than none: the author reads it, believes
it, and writes prose around it.

Three rules keep it honest.

**A binding must land in a known domain.** `sprintf("%s_b%d.tex", v, r)` would
happily match `mumbai_correct_b2`, which another script writes, so a capture is
only accepted when the loop's own registry contains the captured value. Without
this every Mumbai coefficient would be attributed to the Patna model.

**Two candidate writes claim neither.** Ambiguity is reported, not resolved.

**The filename and the expression have to agree.** A `_p` suffix over a write of
`coef(m)` means one of the two is lying and nothing here can tell which, so the
producer is kept and the statistic claim is dropped.

The manifest is a cache, and it lives in the build directory rather than beside
the fragments. That is a deliberate departure from the obvious placement: the
build directory writes its own `.gitignore`, whereas a file dropped next to the
fragments makes `git status` grow inside the author's repository on a
**read-only** serve, which `build()` cannot currently distinguish. The
hand-editable half is `values.json` in the manuscript directory, which this
module reads and never writes, so a correction survives regeneration by
construction rather than by a merge rule remembering to preserve it. A hand edit
made directly in the cache is preserved too, detected by its text having drifted
from the `derived` text recorded beside it.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path

from manuscriptor.server import producers

MANIFEST_NAME = "values.json"

# How much of a fragment's own text is worth carrying to the page.
VALUE_CLIP = 80
HISTORY_LIMIT = 12


# ---------------------------------------------------------------- the API


def describe(manuscript_dir, targets, *, cache_dir=None, repo=None) -> dict[str, dict]:
    """One record per fragment, keyed the way the viewer keys values (the stem).

    `targets` are the `\\input` targets the manuscript actually reads, so the
    cost is bounded by what the paper uses rather than by what the analysis
    directory holds: qutub-india has 1791 fragments on disk and reads 140.
    """
    manuscript_dir = Path(manuscript_dir).resolve()
    root = Path(repo).resolve() if repo else manuscript_dir.parent
    paths = _unique(Path(t).resolve() for t in targets)
    if not paths:
        return {}

    overlay = _read_manifest(manuscript_dir / MANIFEST_NAME)
    cached = _read_manifest(Path(cache_dir) / MANIFEST_NAME) if cache_dir else {}
    fingerprint = _script_fingerprint(root, manuscript_dir)

    out: dict[str, dict] = {}
    index = None
    for path in paths:
        key = path.stem
        if key in out:
            continue
        prior = (cached.get("values") or {}).get(key) or {}
        entry = _reuse(prior, path, manuscript_dir, cached.get("scripts"), fingerprint)
        if entry is None:
            if index is None:
                index = _index(root, manuscript_dir)
            entry = _derive_entry(index, key, path, manuscript_dir, root)
            entry = _carry_hand_edit(prior, entry)
        _apply_overlay(entry, (overlay.get("values") or {}).get(key))
        out[key] = entry

    _attach_history(out, root, manuscript_dir, cached)
    if cache_dir is not None:
        _write_cache(Path(cache_dir), out, fingerprint, _head(root))
    return out


# ------------------------------------------------------------ the records


def _blank(key: str, path: Path, manuscript_dir: Path) -> dict:
    return {
        "key": key,
        "path": _rel(path, manuscript_dir),
        "value": _value_of(path),
        "producer": None,
        "producer_line": 0,
        "producer_hash": None,
        "description": None,
        "derived": None,
        "source": "unknown",
        "statistic": None,
        "subject": None,
        "family": None,
        "index": {},
        "units": None,
        "model": None,
        "siblings": [],
        "reason": None,
        "history": [],
        "history_note": None,
    }


def _derive_entry(index, key: str, path: Path, manuscript_dir: Path, root: Path) -> dict:
    entry = _blank(key, path, manuscript_dir)
    hit, reason = _match(index, key)
    if hit is None:
        entry["reason"] = reason
        return entry

    write, binds = hit
    entry["producer"] = _rel(write.script, root)
    entry["producer_line"] = write.line
    entry["producer_hash"] = index.hashes.get(write.script)
    entry["model"] = write.model
    entry["index_words"] = write.scope.words

    facts = _facts(index, write, binds, key, path, manuscript_dir)
    entry.update(facts)
    entry["model"] = facts.get("model") or _fill_formula(write.model, binds, write)
    entry["code"], entry["lines"] = _excerpt(write.script, write.line)
    entry["derived"] = _sentence(entry)
    entry["description"] = entry["derived"]
    entry["source"] = "derived" if entry["statistic"] or entry["subject"] else "producer"
    if entry["source"] == "producer":
        # The producer is known and nothing in it names the value. Saying who
        # wrote it is not a guess, and it is the half of the question the
        # References row was missing; the other half stays unclaimed.
        entry["reason"] = "the producing script is known, but nothing in it names this value"
    return entry


def _facts(index, write, binds, key, path, manuscript_dir, depth: int = 0) -> dict:
    """Everything the code says about one write, with the claims it will not make."""
    scope = write.scope
    subject = None
    family = None
    idx: dict[str, str] = {}
    base = None

    for name, value in binds:
        domain = scope.domains.get(name)
        if domain is not None and domain.numeric:
            idx[name] = value
            continue
        if domain is not None and domain.labels:
            subject = domain.labels.get(value) or subject
            family = (domain.extra.get(value) or {}).get("family") or family
        # A capture that is itself a fragment key: `paste0(nm, "_pp.tex")`
        # over a manifest of fragment names. The base entry carries the meaning,
        # so the raw name is never shown as the subject when a base exists.
        if base is None and value != key and _sibling_path(path, value).exists():
            base = value
        elif subject is None and domain is not None:
            subject = value

    # A literal index into a registry, for the fragments named in full:
    # write_frag(fmt_p(p_by_var[["correct"]]), ".../balance_correct_p.tex")
    if subject is None:
        for literal in write.literals:
            for domain in scope.domains.values():
                if domain.labels and literal in domain.labels:
                    subject = domain.labels[literal]
                    family = (domain.extra.get(literal) or {}).get("family") or family
                    break
            if subject:
                break

    from_name = _statistic_in_order(_name_tokens(write, key))
    from_expr = _statistic(write.expr_tokens)
    statistic = _reconcile(from_name, from_expr)

    # A derived fragment inherits what the fragment it was derived FROM means.
    # `correct_b2_pp` is `correct_b2` times a hundred, and reporting it as "for
    # correct_b2" tells the reader the filename they already had.
    words = None
    model = None
    siblings = _siblings(index, write, binds, key, path, manuscript_dir)
    if base is not None and depth < 2:
        hit = _match(index, base)[0]
        if hit is not None:
            bwrite, bbinds = hit
            first = _facts(index, bwrite, bbinds, base,
                           _sibling_path(path, base), manuscript_dir, depth + 1)
            statistic = statistic or first.get("statistic")
            subject = subject or first.get("subject")
            family = family or first.get("family")
            if not idx and first.get("index"):
                idx = first["index"]
                # The index came from the other script, so the word that names
                # it has to come from there too, or a round reads as `r = 2`.
                words = bwrite.scope.words
            # The estimate row belongs to the ORIGINAL estimate. A value in
            # percentage points is the same coefficient, and its standard error
            # and N are the ones sitting beside the fragment it was built from,
            # in the script that ran the regression.
            siblings = _merge(siblings, [_row(base, first.get("statistic"),
                                              _sibling_path(path, base), manuscript_dir)])
            siblings = _merge(siblings, first.get("siblings") or [])
            if not write.model and bwrite.model:
                model = _fill_formula(bwrite.model, bbinds, bwrite)

    facts = {
        "statistic": statistic,
        "subject": subject,
        "family": family,
        "index": idx,
        "units": _units(write, key),
        "base": base,
        "siblings": siblings[:8],
    }
    if words:
        facts["index_words"] = words
    if model:
        facts["model"] = model
    return facts


def _merge(rows: list, more: list) -> list:
    seen = {r["key"] for r in rows}
    return rows + [r for r in more if r and r["key"] not in seen]


def _row(key: str, statistic, path: Path, manuscript_dir: Path) -> dict | None:
    value = _value_of(path)
    if not value or _shape(value) != "number":
        return None
    return {"key": key, "statistic": statistic or "value", "value": value,
            "path": _rel(path, manuscript_dir)}


def _sentence(entry: dict) -> str | None:
    """The one line the References row shows. Facts only, in the author's terms."""
    stat = entry.get("statistic")
    subject = entry.get("subject")
    if not stat and not subject:
        if not entry.get("producer"):
            return None
        # Half an answer, which is still the half the row was missing. What the
        # fragment holds comes from the fragment, so a table body is not
        # reported as a number the code declined to name.
        where = entry["producer"] + (f":{entry['producer_line']}" if entry.get("producer_line") else "")
        shape = _shape(entry.get("value"))
        if shape == "table":
            return f"A table body written by {where}."
        if shape == "text":
            return f"A block of text written by {where}."
        return f"Written by {where}. The code does not say what the number is."

    head = (stat or "computed value").capitalize()
    if subject:
        head += " for " + subject
    bits = [head]
    for name, value in (entry.get("index") or {}).items():
        bits.append(f"{_index_word(entry, name)} {value}")
    if entry.get("units"):
        bits.append("in " + entry["units"])
    # How a model was estimated belongs to the statistics a model produces. A
    # control mean written by a script that also fits a balance regression is
    # not "clustered on fidcode", and saying so on the row would be a claim the
    # code never made. The model card still shows the specification.
    model = entry.get("model") or {}
    if stat and _core(stat) in _MODEL_STATS:
        if model.get("cluster"):
            bits.append("clustered on " + model["cluster"])
        if model.get("fe"):
            bits.append(" and ".join(model["fe"]) + " fixed effects")
    if entry.get("producer"):
        bits.append("from " + entry["producer"])
    return ", ".join(bits) + "."


def _excerpt(script: Path, line: int, span: int = 6):
    """The lines around the write, so the panel can show the code itself.

    The point of provenance is reading it, and a path plus a line number sends
    the author to a different application to do that.
    """
    try:
        lines = script.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None, None
    lo = max(0, line - 1 - span)
    hi = min(len(lines), line + span)
    return "\n".join(lines[lo:hi]), f"{script.name}:{lo + 1}-{hi}"


def _fill_formula(model, binds, write) -> dict | None:
    """`%s ~ i(round, treat, ref = 1) | fidnum + caseround` for THIS outcome.

    The formula is built by the same `sprintf` that builds the filename, so the
    outcome the manuscript is reading is exactly the one to put in it. Copied
    rather than mutated: one spec is shared by every write in the script.
    """
    if not model or not model.get("formula") or "%s" not in model["formula"]:
        return model
    names = [v for name, v in binds if not (write.scope.domains.get(name) or _Domain([])).numeric]
    if len(names) != 1:
        return model
    filled = dict(model)
    filled["formula"] = model["formula"].replace("%s", names[0], 1)
    return filled


_TABULAR = ("\\begin{tabular}", "\\toprule", "\\midrule", "\\begin{table}", "\\multicolumn")


def _shape(value: str | None) -> str:
    """What a fragment holds, read off the fragment rather than guessed."""
    if not value:
        return "number"
    if any(mark in value for mark in _TABULAR):
        return "table"
    return "text" if len(value.split()) >= 8 else "number"


def _index_word(entry: dict, name: str) -> str:
    """`round 2`, not `r 2`, when the script's own format strings say so."""
    return (entry.get("index_words") or {}).get(name, name + " =")


# ------------------------------------------------------ cache and overlay


def _reuse(prior: dict, path: Path, manuscript_dir: Path, seen: str | None, now: str):
    """A cached record, when the code that produced it has not changed.

    The key is the CONTENT of the analysis code: `seen` and `now` are hashes of
    every script's bytes, so a re-run of the analysis invalidates every
    description it wrote, and so does a new script appearing, which is the case
    a per-file hash cannot see at all (a second script can make a name that had
    one producer ambiguous). Each entry still records the hash of the file it
    came from, which is what makes the cache readable on its own.
    """
    if not prior or seen != now:
        return None
    entry = dict(prior)
    entry["path"] = _rel(path, manuscript_dir)
    entry["value"] = _value_of(path)
    entry["history"] = prior.get("history") or []
    return entry


def _carry_hand_edit(prior: dict, entry: dict) -> dict:
    """A description the author rewrote is not overwritten by a re-derivation.

    Detected by drift: the cache records what was derived beside what is shown,
    so a `description` that no longer matches its own `derived` was typed by a
    human. The fresh derivation is kept in `derived`, which is what makes a
    stale hand note visible rather than invisible.
    """
    if not prior or not prior.get("description"):
        return entry
    if prior.get("source") == "hand" or prior["description"] != prior.get("derived"):
        entry["description"] = prior["description"]
        entry["source"] = "hand"
    return entry


def _apply_overlay(entry: dict, given) -> None:
    """`values.json` in the manuscript directory, which the server only reads."""
    if given is None:
        return
    if isinstance(given, str):
        given = {"description": given}
    if not isinstance(given, dict):
        return
    for field in ("description", "statistic", "subject", "units"):
        if given.get(field):
            entry[field] = given[field]
    if given.get("description"):
        entry["source"] = "hand"
        entry["reason"] = None


def _read_manifest(path: Path) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_cache(cache_dir: Path, values: dict, fingerprint: str, head: str | None) -> None:
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        _write_if_new(
            cache_dir / MANIFEST_NAME,
            json.dumps(
                {
                    "note": (
                        "Derived from the code that writes each fragment. Correct a wrong "
                        "description in values.json beside your manuscript; this file is "
                        "regenerated and that one is never written to."
                    ),
                    "scripts": fingerprint,
                    "head": head,
                    "values": values,
                },
                indent=1,
                sort_keys=True,
            ),
        )
    except OSError:
        pass


def _write_if_new(path: Path, text: str) -> None:
    """A rebuild fires on every typing pause, and the manifest rarely moves.

    Rewriting a quarter of a megabyte on each of those would be pure churn, and
    the page fetches this file, so a needless mtime bump is a needless refetch.
    """
    try:
        if path.read_text(encoding="utf-8") == text:
            return
    except OSError:
        pass
    path.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------- history


def _attach_history(entries: dict, root: Path, manuscript_dir: Path, cached: dict) -> None:
    """What the value was before, and what changed it.

    A fragment file holds only what the value is now, so the history has to come
    from somewhere else. Git is where it is when the fragments are committed,
    which in this corpus they are: one `git log -p` over the fragment directory
    yields every past value of every fragment in a single call. When the
    fragments are not tracked, the entry says so rather than showing an empty
    panel that reads as "this value has never changed".
    """
    tracked = [e for e in entries.values() if e.get("path")]
    if not tracked:
        return
    head = _head(root)
    if head is None:
        for e in entries.values():
            e["history_note"] = "No git repository here, so there is no record of earlier values."
        return

    reuse = cached.get("head") == head and cached.get("values")
    log: dict[str, list] = {}
    if reuse:
        log = {k: (v.get("history") or []) for k, v in cached["values"].items()}
        log = {k: [h for h in v if h.get("why") != _UNCOMMITTED] for k, v in log.items()}
    else:
        log = _git_history(root, manuscript_dir, entries)

    for key, e in entries.items():
        hist = list(log.get(key) or [])
        now = e.get("value")
        if now and (not hist or _same(hist[0].get("value"), now)) is False:
            hist.insert(0, {"when": "now", "value": now, "why": _UNCOMMITTED})
        e["history"] = hist[:HISTORY_LIMIT]
        if not e["history"]:
            e["history_note"] = (
                "This fragment is not committed, so git holds no earlier value of it."
            )


_UNCOMMITTED = "uncommitted change in the working tree"


def _git_history(root: Path, manuscript_dir: Path, entries: dict) -> dict[str, list]:
    dirs = sorted({str(Path(e["path"]).parent) for e in entries.values()})
    rel = [str((manuscript_dir / d).resolve().relative_to(root)) for d in dirs
           if (manuscript_dir / d).resolve().is_relative_to(root)]
    if not rel:
        return {}
    out = _git(root, ["log", "-p", "--unified=0", "--no-color", "--no-renames",
                      "--format=%x00%cI%x00%s", "--"] + rel)
    if out is None:
        return {}
    return _parse_log(out, entries.keys())


_DIFF_RE = re.compile(r"^diff --git a/(\S+) b/(\S+)$")


def _parse_log(text: str, keys) -> dict[str, list]:
    keys = set(keys)
    hist: dict[str, list] = {}
    when = why = ""
    current = None
    for line in text.splitlines():
        if line.startswith("\x00"):
            _, when, why = line.split("\x00", 2)
            current = None
            continue
        m = _DIFF_RE.match(line)
        if m:
            current = Path(m.group(2)).stem
            current = current if current in keys else None
            continue
        if current and line.startswith("+") and not line.startswith("+++"):
            value = line[1:].strip()
            if value:
                hist.setdefault(current, []).append(
                    {"when": when[:10], "value": value[:VALUE_CLIP], "why": why}
                )
            current = None
    return hist


def _head(root: Path) -> str | None:
    out = _git(root, ["rev-parse", "HEAD"])
    return out.strip() if out else None


def _git(root: Path, args: list[str]) -> str | None:
    try:
        p = subprocess.run(
            ["git", "-C", str(root)] + args,
            capture_output=True, text=True, timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return p.stdout if p.returncode == 0 else None


def _same(a, b) -> bool:
    return _clean(a) == _clean(b)


def _clean(v) -> str:
    return re.sub(r"[\s%]+", "", str(v or ""))


# ------------------------------------------------------- the script index


class _Domain:
    """The literal values a loop variable takes, and their labels if it has any."""

    def __init__(self, values, labels=None, extra=None, numeric=False):
        self.values = set(values)
        self.labels = labels or {}
        self.extra = extra or {}
        self.numeric = numeric

    def holds(self, value: str) -> bool:
        return value in self.values


class _Scope:
    def __init__(self):
        self.domains: dict[str, _Domain] = {}
        self.model = None
        self.models: dict[str, dict] = {}
        self.words: dict[str, str] = {}


class _Write:
    __slots__ = ("script", "line", "pattern", "vars", "expr_tokens", "reach",
                 "literals", "scope", "weight", "model")

    def __init__(self, script, line, pattern, vars_, expr_tokens, reach, literals, scope):
        self.script = script
        self.line = line
        self.pattern = pattern
        self.vars = vars_
        self.expr_tokens = expr_tokens      # two hops: what the value IS
        self.reach = reach                  # five hops: what fitted it
        self.literals = literals
        self.scope = scope
        self.weight = len(re.sub(r"\\(.)", r"\1", re.sub(r"\([^)]*\)", "", pattern.pattern)))
        # The model this write's own expression reaches, or the script's only
        # one. A script with two specifications and no chain says nothing.
        reached = [scope.models[name] for name in reach if name in scope.models]
        self.model = reached[0] if len(reached) == 1 else scope.model


class _Index:
    def __init__(self):
        self.writes: list[_Write] = []
        self.hashes: dict[Path, str] = {}


_INDEX_MEMO: dict[tuple, _Index] = {}


def _index(root: Path, manuscript_dir: Path) -> _Index:
    """Every `.tex`-writing call in the analysis code, as a filename template.

    Memoised on the scripts' own fingerprint: a rebuild fires on every typing
    pause, and re-parsing 51 R scripts each time would be paid on every
    keystroke pause rather than once.
    """
    scripts = _scripts(root, manuscript_dir)
    fp = _fingerprint(scripts)
    memo = _INDEX_MEMO.get((str(root), fp))
    if memo is not None:
        return memo

    index = _Index()
    for script in scripts:
        try:
            text = script.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        index.hashes[script] = _digest(text)
        stata = script.suffix.lower() == ".do"
        text = _strip_comments(text, stata)
        scope = _scope(text, stata)
        finder = _stata_writes if stata else _r_writes
        for write in finder(text, script, scope):
            index.writes.append(write)
    _INDEX_MEMO.clear()
    _INDEX_MEMO[(str(root), fp)] = index
    return index


def _scripts(root: Path, manuscript_dir: Path) -> list[Path]:
    # Where analysis code lives is producers.py's knowledge, not a second copy
    # of it here: one wrong answer about that is enough for the whole project.
    found = producers._find_scripts(root) + producers._find_scripts(manuscript_dir)
    return sorted({p.resolve() for p in found})


def _fingerprint(scripts: list[Path]) -> str:
    """The analysis code's CONTENT, not its timestamps.

    A stat-based fingerprint was the first version and it was wrong in a way a
    test caught: the in-process index is memoised on this, so a script rewritten
    without moving its size or its mtime kept serving descriptions derived from
    the old text, and the per-entry content hash could not save it because the
    stale index is what produced the entry. Reading the scripts costs a few
    milliseconds against the 0.8 seconds a rebuild already takes.
    """
    h = hashlib.sha256()
    for p in scripts:
        try:
            h.update(str(p).encode() + b"|" + p.read_bytes() + b"\n")
        except OSError:
            continue
    return h.hexdigest()[:16]


def _script_fingerprint(root: Path, manuscript_dir: Path) -> str:
    return _fingerprint(_scripts(root, manuscript_dir))


# ------------------------------------------------------------- R parsing


_R_WRITERS = (
    "write_frag", "writeLines", "fwrite", "write.csv", "write.table",
    "cat", "save_kable", "capture.output", "etable", "print_tex", "write_tex",
)

_ASSIGN_RE = r"^[ \t]*{name}(?:\[\[[^\]]*\]\]|\[[^\]]*\]|\$\w+)*[ \t]*(<-|=)[ \t]*(.+?)[ \t]*$"


def _assigned(text: str, name: str, at: int) -> str | None:
    """The last value assigned to `name` before offset `at`.

    `<-` beats `=`, and that is not a style preference. A named argument on its
    own line inside a multi-line call (`results[[v]] <- list(boot = boot, ...)`)
    is indistinguishable from an assignment by shape, and taking the last match
    made `boot` resolve to itself and cut the chain that reaches the model.

    The value is read to its BALANCED end rather than to the end of the line.
    `betas[[as.character(r)]] <- list(` is four lines of R, and reading one of
    them yields the token `list` and stops the walk one hop short of the
    regression every coefficient in the paper came out of.
    """
    arrow = plain = None
    for m in re.finditer(_ASSIGN_RE.format(name=re.escape(name)), text[:at], re.M):
        rhs = _balanced(text, m.start(2))
        if m.group(1) == "<-":
            arrow = rhs
        else:
            plain = rhs
    return arrow or plain


def _balanced(text: str, start: int, cap: int = 2000) -> str:
    depth, i, end = 0, start, min(len(text), start + cap)
    while i < end:
        ch = text[i]
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
            if depth <= 0:
                return text[start : i + 1].strip()
        elif ch == "\n" and depth <= 0:
            return text[start:i].strip()
        i += 1
    return text[start:end].strip()


def _r_writes(text: str, script: Path, scope: _Scope) -> list[_Write]:
    out = []
    for fn in _R_WRITERS:
        for at, args in _calls(text, fn):
            if not args:
                continue
            path_expr = _named(args, ("file", "con", "path", "output")) or args[-1]
            value_expr = args[0] if len(args) > 1 else ""
            expr = _resolve_var(text, at, path_expr)
            base = _basename_expr(text, at, expr)
            built = _template(base)
            if built is None or ".tex" not in built[0].replace("\\", ""):
                continue
            pattern, vars_ = built
            near, far, literals = _expr_tokens(text, at, value_expr)
            out.append(_Write(script, text.count("\n", 0, at) + 1,
                              re.compile(pattern), vars_, near, far, literals, scope))
    return out


def _basename_expr(text: str, at: int, expr: str) -> str:
    expr = expr.strip()
    for wrapper in ("file.path", "here::here", "here", "path", "fs::path"):
        m = re.match(re.escape(wrapper) + r"\s*\(", expr)
        if m:
            parts, _ = _args(expr, m.end() - 1)
            return _resolve_var(text, at, parts[-1]) if parts else expr
    return expr


def _resolve_var(text: str, at: int, expr: str) -> str:
    """One hop back through a local assignment: `p <- file.path(...); write(x, p)`."""
    expr = expr.strip()
    if not re.fullmatch(r"[A-Za-z._][\w.]*", expr):
        return expr
    return _assigned(text, expr, at) or expr


_FMT_RE = re.compile(r"%[-+ #0]*[\d.]*([sdfgeix])")


def _template(expr: str):
    """A filename expression as (regex over the basename, variable names)."""
    expr = expr.strip()
    lit = _literal(expr)
    if lit is not None:
        return re.escape(lit), []

    m = re.match(r"^sprintf\s*\(", expr)
    if m:
        parts, _ = _args(expr, m.end() - 1)
        fmt = _literal(parts[0]) if parts else None
        if fmt is None:
            return None
        pattern, last = "", 0
        for hit in _FMT_RE.finditer(fmt):
            pattern += re.escape(fmt[last:hit.start()]) + _group(hit.group(1))
            last = hit.end()
        pattern += re.escape(fmt[last:])
        return pattern, [p.strip() for p in parts[1:]]

    m = re.match(r"^paste0?\s*\(", expr)
    if m:
        parts, _ = _args(expr, m.end() - 1)
        pattern, vars_ = "", []
        for part in parts:
            part = part.strip()
            if re.match(r"^(sep|collapse)\s*=", part):
                continue
            text = _literal(part)
            if text is not None:
                pattern += re.escape(text)
            else:
                pattern += r"(.+?)"
                vars_.append(part)
        return pattern, vars_
    return None


def _group(kind: str) -> str:
    if kind in "dix":
        return r"(\d+)"
    if kind in "feg":
        return r"([-\d.]+)"
    return r"(.+?)"


def _literal(expr) -> str | None:
    if expr is None:
        return None
    m = re.fullmatch(r"""\s*["'](.*)["']\s*""", expr, re.S)
    return m.group(1) if m else None


def _named(args, names):
    for arg in args:
        m = re.match(r"^\s*([A-Za-z._][\w.]*)\s*=(?!=)\s*(.+)$", arg, re.S)
        if m and m.group(1) in names:
            return m.group(2)
    return None


def _calls(text: str, fn: str):
    """Every call to `fn`, as (offset, argument list)."""
    for m in re.finditer(r"(?<![\w.$])" + re.escape(fn) + r"\s*\(", text):
        args, end = _args(text, m.end() - 1)
        if end > 0:
            yield m.start(), args


def _args(text: str, open_idx: int):
    """Split one parenthesised argument list, respecting nesting and strings."""
    depth, i, parts, cur = 0, open_idx, [], []
    while i < len(text):
        ch = text[i]
        if ch in "([{":
            depth += 1
            if depth == 1:
                i += 1
                continue
        elif ch in ")]}":
            depth -= 1
            if depth == 0:
                parts.append("".join(cur).strip())
                return [p for p in parts if p != ""] or [], i
        if ch in "\"'":
            quote = ch
            cur.append(ch)
            i += 1
            while i < len(text) and text[i] != quote:
                if text[i] == "\\":
                    cur.append(text[i])
                    i += 1
                if i < len(text):
                    cur.append(text[i])
                    i += 1
            cur.append(quote)
            i += 1
            continue
        if ch == "," and depth == 1:
            parts.append("".join(cur).strip())
            cur = []
        elif depth >= 1:
            cur.append(ch)
        i += 1
    return [], -1


# --------------------------------------------------------- Stata parsing


_STATA_USING_RE = re.compile(
    r"^[ \t]*(?:(?:qui(?:etly)?|cap(?:ture)?|noi(?:sily)?)[ \t]+)*"
    r"(file open [A-Za-z_]\w*|esttab|estout|outreg2|outsheet|export delimited|"
    r"estpost|texsave|listtex|graph export)\b(.*)$",
    re.M,
)
_USING_PATH_RE = re.compile(r'using[ \t]+"([^"]+)"|using[ \t]+(\S+)')


def _stata_writes(text: str, script: Path, scope: _Scope) -> list[_Write]:
    out = []
    for m in _STATA_USING_RE.finditer(text):
        head, rest = m.group(1), m.group(2)
        hit = _USING_PATH_RE.search(rest)
        raw = None
        if hit:
            raw = hit.group(1) or hit.group(2)
        elif head.startswith("graph export"):
            lit = re.search(r'"([^"]+)"', rest)
            raw = lit.group(1) if lit else None
        if not raw or ".tex" not in raw:
            continue
        name = raw.replace("\\", "/").rsplit("/", 1)[-1]
        pattern = _stata_pattern(name)
        if ".tex" not in pattern.replace("\\", ""):
            continue
        line = text.count("\n", 0, m.start()) + 1
        handle = head.split()[-1] if head.startswith("file open") else None
        tokens, literals = _stata_content(text, m.end(), handle)
        out.append(_Write(script, line, re.compile(pattern), [], tokens, tokens, literals, scope))
    return out


def _stata_pattern(name: str) -> str:
    """`${g}` and a local both stand for text nobody here can resolve."""
    parts = re.split(r"(\$\{[^}]+\}|\$\w+|`[^']*')", name)
    return "".join(r"(.+?)" if i % 2 else re.escape(p) for i, p in enumerate(parts))


def _stata_content(text: str, at: int, handle: str | None):
    """What a `file write` block puts in the file, as tokens and macro names."""
    window = text[at : at + 4000]
    literals: list[str] = []
    tokens: set[str] = set()
    pattern = rf"file write\s+{re.escape(handle)}\s+(.*)" if handle else r"file write\s+\S+\s+(.*)"
    for body in re.findall(pattern, window)[:40]:
        for macro in re.findall(r"\\newcommand\{\\(\w+)\}", body):
            literals.append("\\" + macro)
            tokens |= set(_words(macro))
        for local in re.findall(r"`(\w+)'", body):
            # A Stata local is named the way a filename is, `b_late`, so it is
            # read the same way rather than as one opaque identifier.
            tokens |= set(_words(local))
            rhs = _assigned(text, "local[ \t]+" + local, len(text))
            if rhs:
                tokens |= _tokens(re.sub(r'"[^"]*"', " ", rhs))
    return tokens, literals


# ---------------------------------------------------- scopes and domains


def _scope(text: str, stata: bool) -> _Scope:
    scope = _Scope()
    if stata:
        scope.model = _stata_model(text)
        return scope

    vectors = _vectors(text)
    registries = _registries(text)

    for var, expr in re.findall(r"for\s*\(\s*([A-Za-z._][\w.]*)\s+in\s+(.+?)\)\s*\{", text):
        expr = expr.strip()
        rng = re.fullmatch(r"(\d+)\s*:\s*(\d+)", expr)
        if rng:
            lo, hi = int(rng.group(1)), int(rng.group(2))
            scope.domains[var] = _Domain([str(i) for i in range(lo, hi + 1)], numeric=True)
            continue
        if expr in vectors:
            scope.domains[var] = _Domain(vectors[expr])
            continue
        if expr in registries:
            rows = registries[expr]
            # `for (o in outcomes) { v <- o$var }`: the loop binds the row, and
            # the name the filename uses is a field of it.
            for field in ("var", "name", "outcome", "key"):
                values = [r[field] for r in rows if r.get(field)]
                if not values:
                    continue
                labels = {r[field]: r.get("label", r[field]) for r in rows if r.get(field)}
                extra = {r[field]: r for r in rows if r.get(field)}
                domain = _Domain(values, labels, extra)
                for alias in _aliases(text, var, field):
                    scope.domains[alias] = domain
            continue
        literal = _literal(expr)
        if literal is not None:
            scope.domains[var] = _Domain([literal])

    for var, values in vectors.items():
        scope.domains.setdefault(var, _Domain(values))

    # A registry is also a domain under its own name, which is how a fragment
    # named in full gets a subject: `p_by_var[["correct"]]` written to
    # `balance_correct_p.tex` says which row of `balance_vars` it reports.
    for name, rows in registries.items():
        for field in ("var", "name", "outcome", "key"):
            values = [r[field] for r in rows if r.get(field)]
            if values:
                scope.domains.setdefault(
                    name,
                    _Domain(values,
                            {r[field]: r.get("label", r[field]) for r in rows if r.get(field)},
                            {r[field]: r for r in rows if r.get(field)}),
                )
                break
    scope.model, scope.models = _r_model(text)
    scope.words = _index_words(text)
    return scope


def _aliases(text: str, row_var: str, field: str) -> list[str]:
    """`v <- o$var` makes `v` another name for the row's `var` field."""
    found = re.findall(
        r"^[ \t]*([A-Za-z._][\w.]*)[ \t]*(?:<-|=)[ \t]*"
        + re.escape(row_var) + r"\$" + re.escape(field) + r"\b",
        text, re.M)
    return found + ([row_var + "$" + field] if not found else [row_var + "$" + field])


def _vectors(text: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for name, body in re.findall(
        r"^[ \t]*([A-Za-z._][\w.]*)[ \t]*(?:<-|=)[ \t]*c\((.*?)\)[ \t]*$", text, re.M | re.S
    ):
        values = re.findall(r"""["']([^"']+)["']""", body)
        if values:
            out[name] = values
    for name, lo, hi in re.findall(
        r"^[ \t]*([A-Za-z._][\w.]*)[ \t]*(?:<-|=)[ \t]*(\d+)\s*:\s*(\d+)", text, re.M
    ):
        out[name] = [str(i) for i in range(int(lo), int(hi) + 1)]
    return out


def _registries(text: str) -> dict[str, list[dict]]:
    """`x <- list(list(var = "correct", label = "Correct case management"), ...)`."""
    out: dict[str, list[dict]] = {}
    for m in re.finditer(r"^[ \t]*([A-Za-z._][\w.]*)[ \t]*(?:<-|=)[ \t]*list\s*\(", text, re.M):
        args, end = _args(text, m.end() - 1)
        if end < 0:
            continue
        rows = []
        for arg in args:
            inner = re.match(r"^list\s*\(", arg.strip())
            if not inner:
                continue
            fields, _ = _args(arg.strip(), inner.end() - 1)
            row = {}
            for field in fields:
                fm = re.match(r"""^\s*([A-Za-z._][\w.]*)\s*=\s*["'](.*?)["']\s*$""", field, re.S)
                if fm:
                    row[fm.group(1)] = fm.group(2)
            if row:
                rows.append(row)
        if rows:
            out[m.group(1)] = rows
    return out


def _index_words(text: str) -> dict[str, str]:
    """What a numeric loop variable indexes, when the code spells it out.

    `sprintf("round::%d:treat", r)` is the script saying that `r` is a round.
    Reading the word off the format string is the difference between "round 2"
    and "r = 2".
    """
    words: dict[str, str] = {}
    for fmt, args in re.findall(r"""sprintf\s*\(\s*["'](.*?)["']\s*,([^)]*)\)""", text):
        m = re.search(r"([A-Za-z]{3,})[^A-Za-z%]{0,3}%[-+ #0]*[\d.]*[dix]", fmt)
        if not m:
            continue
        for var in re.findall(r"[A-Za-z._][\w.]*", args):
            words.setdefault(var, m.group(1).lower())
    return words


_R_FITS = ("feols", "fepois", "lm", "glm", "felm", "lm_robust", "ivreg", "iv_robust")


def _r_model(text: str):
    """The estimating equations in a script, and which function fits each.

    Returns (the script's single spec or None, {function name: its spec}).

    A script with two specifications, an ITT and a two-stage TOT beside it, can
    say nothing about a write on its own: attributing a coefficient to the wrong
    regression is exactly the failure this module exists to avoid. What ties
    them is the CALL CHAIN. `write_frag(p_frag, ...)` follows back to `boot`, to
    `m`, to `m <- fit_itt(df, v)`, and `fit_itt`'s body fits one model. That is
    evidence rather than proximity, and proximity is what would be wrong here:
    the fit sits at the top of the file and the second one sits between the
    loop and the write.
    """
    specs = {}
    at_offset = {}
    for fn in _R_FITS:
        for at, args in _calls(text, fn):
            if not args:
                continue
            formula = _formula(text, at, args[0])
            spec = {"call": fn, "formula": formula, "cluster": _cluster(args),
                    "fe": _fixed_effects(formula)}
            key = json.dumps(spec, sort_keys=True)
            specs[key] = spec
            at_offset.setdefault(at, key)

    by_function = {}
    for name, start, end in _functions(text):
        inside = {k for at, k in at_offset.items() if start <= at < end}
        if len(inside) == 1:
            by_function[name] = specs[next(iter(inside))]

    single = next(iter(specs.values())) if len(specs) == 1 else None
    return single, by_function


_FUNCTION_RE = re.compile(r"^[ \t]*([A-Za-z._][\w.]*)[ \t]*(?:<-|=)[ \t]*function\s*\(", re.M)


def _functions(text: str):
    """Every `name <- function(...) { ... }`, as (name, start, end) offsets."""
    for m in _FUNCTION_RE.finditer(text):
        brace = text.find("{", m.end())
        if brace < 0:
            continue
        depth, i = 0, brace
        while i < len(text):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        yield m.group(1), m.start(), i


def _formula(text: str, at: int, expr: str) -> str | None:
    expr = _resolve_var(text, at, expr).strip()
    m = re.match(r"^as\.formula\s*\(", expr)
    if m:
        args, _ = _args(expr, m.end() - 1)
        inner = args[0] if args else ""
        lit = _literal(inner)
        if lit is None:
            sm = re.match(r"^sprintf\s*\(", inner.strip())
            if sm:
                parts, _ = _args(inner.strip(), sm.end() - 1)
                lit = _literal(parts[0]) if parts else None
        expr = lit or expr
    return re.sub(r"\s+", " ", expr).strip() or None


def _cluster(args) -> str | None:
    raw = _named(args, ("cluster", "clustid", "vcov", "se"))
    if not raw:
        return None
    raw = raw.strip().lstrip("~").strip()
    lit = _literal(raw)
    if lit:
        raw = lit
    m = re.fullmatch(r"[A-Za-z._][\w.]*", raw)
    return m.group(0) if m else None


def _fixed_effects(formula: str | None) -> list[str]:
    if not formula or "|" not in formula:
        return []
    tail = formula.split("|", 1)[1]
    return [p.strip() for p in tail.split("+") if re.fullmatch(r"[A-Za-z._][\w.]*", p.strip())]


_STATA_FITS = re.compile(
    r"^[ \t]*(?:(?:qui(?:etly)?|cap(?:ture)?|noi(?:sily)?)[ \t]+)*"
    r"(reg|regress|areg|xtreg|reghdfe|ivreghdfe|ivregress|logit|probit|poisson)\b[ \t]+(.*)$",
    re.M)


def _stata_model(text: str) -> dict | None:
    specs = {}
    for m in _STATA_FITS.finditer(text):
        cmd, rest = m.group(1), m.group(2)
        body, _, opts = rest.partition(",")
        terms = body.split()
        cluster = re.search(r"(?:vce\(\s*cluster|cluster)\(\s*([A-Za-z_]\w*)", opts)
        absorb = re.search(r"absorb\(\s*([^)]*)\)", opts)
        spec = {
            "call": cmd,
            "formula": " ".join(terms[:1] + ["~"] + terms[1:]) if terms else None,
            "cluster": cluster.group(1) if cluster else None,
            "fe": absorb.group(1).split() if absorb else [],
        }
        specs[json.dumps(spec, sort_keys=True)] = spec
    if len(specs) != 1:
        return None
    return next(iter(specs.values()))


# ------------------------------------------------------ statistics, honestly


_STATISTICS = [
    ({"se", "stderr", "stderror", "sigma"}, "standard error"),
    ({"p", "pval", "pvalue", "pvals", "pb", "pv"}, "p-value"),
    ({"q", "qval", "qvalue"}, "multiplicity-adjusted q-value"),
    ({"clusters", "nclust", "clust", "nclusters"}, "number of clusters"),
    ({"n", "nobs", "obs", "sample"}, "number of observations"),
    ({"mean", "avg"}, "mean"),
    ({"ci", "lo", "hi", "lower", "upper"}, "confidence bound"),
    ({"b", "beta", "betas", "coef", "coefs", "cf", "est", "estimate", "tau",
      "d", "did", "delta", "g", "gamma", "diff"}, "coefficient"),
]

# The statistics a regression produces, and therefore the only ones a cluster
# or a fixed effect says anything about.
_MODEL_STATS = {"coefficient", "standard error", "p-value", "confidence bound",
                "multiplicity-adjusted q-value"}

_MODIFIERS = [
    ({"wb", "boot", "boottest", "fwildclusterboot", "wild"}, "wild-cluster bootstrap"),
    ({"rw", "romano", "wyoung"}, "Romano-Wolf adjusted"),
    ({"one"}, "one-sided"),
    ({"tot"}, "treatment-on-treated"),
]


def _statistic(tokens) -> str | None:
    """From an expression: identifiers only, and a disagreement claims nothing.

    Deliberately strict, because an expression is where a genuine contradiction
    with the filename shows up. `df_est` is one identifier and not an estimate,
    which is why nothing here splits identifiers on their underscores.
    """
    if not tokens:
        return None
    found = None
    for names, label in _STATISTICS:
        if tokens & names:
            if found and found != label:
                return None  # two readings of the same evidence: claim neither
            found = found or label
    if not found:
        return None
    return _modified(found, tokens)


def _statistic_in_order(tokens: list[str]) -> str | None:
    """From a filename: `<subject>_<statistic>`, so the LAST word wins.

    `correct_b3_ci_lo` names a coefficient and a confidence bound in one string
    and means the bound. Reading it as a set would find two statistics and
    refuse to name either, which is the wrong kind of caution: the naming
    convention is the author's and it puts the statistic at the end.
    """
    found = None
    for token in tokens:
        for names, label in _STATISTICS:
            if token in names:
                found = label
    if not found:
        return None
    return _modified(found, set(tokens))


def _modified(label: str, tokens: set[str]) -> str:
    for names, prefix in _MODIFIERS:
        if tokens & names:
            label = prefix + " " + label
    return label


def _reconcile(from_name, from_expr):
    """The filename and the written expression have to agree, or nothing is said."""
    if from_name and from_expr:
        return from_name if _core(from_name) == _core(from_expr) else None
    return from_name or from_expr


def _core(label: str) -> str:
    for names, base in _STATISTICS:
        if label.endswith(base):
            return base
    return label


def _name_tokens(write: _Write, key: str) -> list[str]:
    """The words in the filename the template wrote, not the ones it read.

    Blanking the captures first is what keeps an outcome called `dr_1a` or
    `med_anti_any_2` from being read as a statistic: those words came out of the
    registry, not out of the author's naming convention.
    """
    m = write.pattern.fullmatch(key + ".tex")
    literal = key
    if m:
        for group in m.groups():
            literal = literal.replace(group, " ", 1)
    return _words(literal)


def _words(text: str) -> list[str]:
    """A filename split the way a filename is read: letters and digits apart."""
    return [w.lower() for w in re.findall(r"[A-Za-z]+|\d+", text or "")]


def _expr_tokens(text: str, at: int, expr: str):
    """The written expression, followed back through its local assignments.

    Returns (near, far, literals). TWO WINDOWS, because the two questions want
    different amounts of evidence and mixing them was wrong in both directions.

    The STATISTIC is named by the expression itself, so it reads only two hops:
    `p1_frag` to `sprintf("%.3f", p1)`. Five hops away lies the body of a helper
    that computes a coefficient on the way to a p-value, and letting that count
    made the filename and the expression "disagree" about values that were never
    in question. Nine of them, checked against the corpus.

    The MODEL is only ever reached by the long chain: p_frag to pb to
    `boot[[r]]$p` to `boottest_round(m, ...)` to `m` to `fit_itt`. Cutting that
    short loses the regression on every fragment in the paper.
    """
    literals: list[str] = []
    near: set[str] = set()
    far: set[str] = set()
    seen: set[str] = set()
    frontier = [expr]
    for hop in range(5):
        nxt = []
        for item in frontier:
            if not item:
                continue
            literals.extend(re.findall(r"""["']([^"']+)["']""", item))
            bare = re.sub(r"""["'][^"']*["']""", " ", item)
            names = re.findall(r"[A-Za-z._][\w.]*", bare)
            far |= _tokens(bare)
            if hop < 2:
                near |= _tokens(bare)
            for name in names:
                if name in seen or len(seen) > 24:
                    continue
                seen.add(name)
                rhs = _assigned(text, name, at)
                if rhs:
                    nxt.append(rhs)
        frontier = nxt
    return near, far, literals


def _tokens(text: str) -> set[str]:
    """Whole identifiers, lowercased. `df_est` stays `df_est` and is nothing."""
    out = set()
    for word in re.split(r"[^A-Za-z0-9_$]+", text or ""):
        word = word.strip("_$").lower()
        if word:
            out.add(word)
    return out


def _units(write: _Write, key: str) -> str | None:
    tokens = _name_tokens(write, key)
    if "pp" in tokens:
        return "percentage points"
    if "pct" in tokens or "percent" in tokens:
        return "percent"
    return None


# ------------------------------------------------------------- matching


def _match(index: _Index, key: str):
    """The write that produced a name, or why nothing may be claimed for it."""
    name = key + ".tex"
    accepted = []
    for write in index.writes:
        m = write.pattern.fullmatch(name)
        if not m:
            continue
        binds = list(zip(write.vars, m.groups()))
        if not _bindable(write, binds):
            continue
        accepted.append((write, binds))

    if not accepted:
        return None, "producer unknown: no analysis script writes a file with this name"

    # Two SCRIPTS that both write this name is a genuine conflict: whichever ran
    # last is what is on disk, and nothing here knows which that was. Being more
    # specific does not settle it, so specificity only ranks candidates inside
    # one script, where a literal write beside a templated one is one author's
    # single intention.
    scripts = {w.script for w, _ in accepted}
    if len(scripts) > 1:
        names = ", ".join(sorted(p.name for p in scripts))
        return None, f"producer unknown: two scripts could write this name ({names})"
    return max(accepted, key=lambda pair: pair[0].weight), None


def _bindable(write: _Write, binds) -> bool:
    """Every captured variable must land inside a domain the code declares."""
    for var, value in binds:
        domain = write.scope.domains.get(var)
        if domain is None:
            return False
        if not domain.holds(value):
            return False
    return True


def _siblings(index, write, binds, key, path, manuscript_dir) -> list[dict]:
    """The rest of the estimate: the same template with its statistic slot moved.

    A p-value is read beside its coefficient or it is read wrong, and the code
    that writes one writes the others: `%s_b%d`, `%s_se%d`, `%s_p%d_wb`, `%s_n`,
    `%s_clusters` are five calls in one loop. Their values are on disk already.

    A sibling has to SHARE the bindings. Found in the browser: without that
    rule, every literally-named write in the same script came back as a sibling,
    so a coefficient in percentage points was shown beside two confidence bounds
    from a different estimate entirely.
    """
    if not binds:
        return []
    bound = dict(binds)
    out = []
    for other in index.writes:
        if other.script != write.script or other is write:
            continue
        if not other.vars or any(v not in bound for v in other.vars):
            continue
        try:
            name = _fill(other, bound)
        except KeyError:
            continue
        if name is None or name == key:
            continue
        sib = _sibling_path(path, name)
        if not sib.exists() or _shape(_value_of(sib)) != "number":
            continue
        stat = _statistic_in_order(_name_tokens(other, name)) or _statistic(other.expr_tokens)
        out.append({
            "key": name,
            "statistic": stat or "value",
            "value": _value_of(sib),
            "path": _rel(sib, manuscript_dir),
        })
    return sorted(out, key=lambda s: s["key"])[:8]


def _fill(write: _Write, binds: dict) -> str | None:
    """Rebuild a sibling's filename from the same bindings, or give up."""
    pattern = write.pattern.pattern
    parts = re.split(r"\((?:\.\+\?|\\d\+|\[\-\\d\.\]\+)\)", pattern)
    if len(parts) - 1 != len(write.vars):
        return None
    name = ""
    for i, part in enumerate(parts):
        name += re.sub(r"\\(.)", r"\1", part)
        if i < len(write.vars):
            var = write.vars[i]
            if var not in binds:
                return None
            name += binds[var]
    return name[:-4] if name.endswith(".tex") else None


# ------------------------------------------------------------- utilities


def _sibling_path(path: Path, name: str) -> Path:
    return path.parent / (name + path.suffix)


def _value_of(path: Path) -> str | None:
    """The fragment as it reads, minus the trailing `%`.

    That `%` is not a percent sign and showing it as one would misreport every
    scalar in the paper. It is a LaTeX comment character, written deliberately
    so `\\input` does not emit the file's newline as a space mid-sentence
    ("standard error 2.4 ,"). An actual percent sign in a fragment is escaped.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    flat = " ".join(text.split())
    if flat.endswith("%") and not flat.endswith("\\%"):
        flat = flat[:-1].rstrip()
    return flat[:VALUE_CLIP] or None


def _rel(path: Path, root: Path) -> str:
    try:
        return str(Path(path).resolve().relative_to(Path(root).resolve()))
    except ValueError:
        return str(path)


def _unique(paths):
    seen, out = set(), []
    for p in paths:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]


def _strip_comments(text: str, stata: bool) -> str:
    """Commented-out code does not write files.

    qutub-india's `runfile.do` is entirely commented out; reading it as live
    code would attribute every exhibit to a line nobody runs.
    """
    out = []
    if stata:
        text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    for line in text.splitlines():
        out.append(_strip_line(line, "//" if stata else "#"))
        if stata and re.match(r"^\s*\*", line):
            out[-1] = ""
    return "\n".join(out)


def _strip_line(line: str, marker: str) -> str:
    quote = None
    i = 0
    while i < len(line):
        ch = line[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif line.startswith(marker, i):
            return line[:i]
        i += 1
    return line
