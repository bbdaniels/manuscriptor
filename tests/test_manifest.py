"""Descriptions for computed values, derived from the code that writes them.

The References tab can name the script that writes `correct_p2_wb` and cannot
say what the number IS. Everything here is about closing that gap WITHOUT
guessing, because a wrong description of a coefficient is worse than no
description at all: the author reads it, believes it, and writes a sentence
around it.

So the tests come in two halves. One half asserts that a description is derived
where the code makes it derivable. The other half -- the one that matters more
-- asserts that nothing is claimed where the code does not say it: an unmatched
name, a name two scripts could write, a statistic whose filename and whose
expression disagree. Each of those must come back "producer unknown" rather than
plausible.

No model is called anywhere in here. Deriving a description is static analysis
of the author's own code.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from manuscriptor.server import manifest


# --------------------------------------------------------------- fixtures


def w(root: Path, rel: str, text: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


# The qutub-india shape, reduced: an outcome registry, a loop that binds the
# outcome name into a filename template, and per-round statistic fragments.
# Nothing in this script ever contains the string "correct_p2_wb".
ITT = r"""
### R/10_itt.R -- headline ITT on the trial sample.

outcomes <- list(
  list(var = "correct",  label = "Correct case management", family = "headline"),
  list(var = "test_gx",  label = "GeneXpert ordered",       family = "headline")
)

fit_itt <- function(df, yvar) {
  fml <- as.formula(sprintf("%s ~ i(round, treat, ref = 1) | fidnum + caseround", yvar))
  fixest::feols(fml, data = df, cluster = ~ fidcode)
}

exh_dir <- here::here("manuscript", "exhibits")

for (o in outcomes) {
  v <- o$var
  m <- fit_itt(analysis, v)
  for (r in 2:4) {
    b  <- betas[[as.character(r)]]$b
    s  <- betas[[as.character(r)]]$se
    pb <- boot[[as.character(r)]]$p
    coef_frag <- sprintf("%.3f", b)
    se_frag   <- sprintf("(%.3f)", s)
    p_frag    <- sprintf("%.3f", pb)
    write_frag(coef_frag, file.path(exh_dir, sprintf("%s_b%d.tex", v, r)))
    write_frag(se_frag,   file.path(exh_dir, sprintf("%s_se%d.tex", v, r)))
    write_frag(p_frag,    file.path(exh_dir, sprintf("%s_p%d_wb.tex", v, r)))
  }
  write_frag(sprintf("%d", m$nobs),
             file.path(exh_dir, sprintf("%s_n.tex", v)))
  write_frag(sprintf("%d", length(unique(df_est$fidcode))),
             file.path(exh_dir, sprintf("%s_clusters.tex", v)))
}
"""


def qutub_like(tmp_path: Path) -> tuple[Path, list[Path]]:
    """A manuscript whose numbers are written by a template, not by name."""
    w(tmp_path, "R/10_itt.R", ITT)
    ms = tmp_path / "manuscript"
    frags = {
        "correct_b2": "0.079",
        "correct_se2": "(0.024)",
        "correct_p2_wb": "0.096",
        "correct_n": "4318",
        "correct_clusters": "203",
        "test_gx_b3": "0.150",
    }
    targets = [w(ms, f"exhibits/{k}.tex", v + "%\n") for k, v in frags.items()]
    w(ms, "main.tex", r"\documentclass{article}\begin{document}x\end{document}")
    return ms, targets


def entry(tmp_path, key, **kw):
    ms, targets = qutub_like(tmp_path)
    got = manifest.describe(ms, targets, **kw)
    return got[key]


# ------------------------------------------------- what the code does say


def test_a_templated_name_finds_its_producing_script(tmp_path):
    """No literal in the script contains "correct_p2_wb". The template does."""
    e = entry(tmp_path, "correct_p2_wb")
    assert e["producer"] == "R/10_itt.R", e
    assert e["producer_line"] > 0


def test_the_description_says_which_statistic_and_of_what(tmp_path):
    e = entry(tmp_path, "correct_p2_wb")
    d = e["description"]
    assert d, e
    assert "p-value" in d.lower(), d
    # The outcome's own label, out of the registry the loop reads.
    assert "Correct case management" in d, d
    assert e["statistic"] and "p-value" in e["statistic"]
    assert e["subject"] == "Correct case management"


def test_a_coefficient_and_a_standard_error_are_told_apart(tmp_path):
    b = entry(tmp_path, "correct_b2")
    s = entry(tmp_path, "correct_se2")
    assert "coefficient" in b["statistic"], b
    assert "standard error" in s["statistic"], s
    assert b["description"] != s["description"]


def test_counts_are_named_as_counts(tmp_path):
    n = entry(tmp_path, "correct_n")
    c = entry(tmp_path, "correct_clusters")
    assert "observations" in n["statistic"], n
    assert "clusters" in c["statistic"], c


def test_the_model_is_read_off_the_formula(tmp_path):
    e = entry(tmp_path, "correct_p2_wb")
    model = e["model"] or {}
    assert model.get("cluster") == "fidcode", model
    assert model.get("fe") == ["fidnum", "caseround"], model
    assert "feols" in (model.get("call") or ""), model


def test_the_round_index_comes_from_the_template(tmp_path):
    two = entry(tmp_path, "correct_p2_wb")
    assert two["index"] == {"r": "2"}, two
    assert "2" in two["description"]


def test_the_siblings_of_a_value_carry_the_rest_of_the_estimate(tmp_path):
    """Estimate, SE, p, N and clusters where the code makes them available."""
    e = entry(tmp_path, "correct_p2_wb")
    sib = {s["statistic"]: s["value"] for s in e["siblings"]}
    assert any("coefficient" in k for k in sib), sib
    assert "0.079" in [v for k, v in sib.items() if "coefficient" in k][0]
    assert any("observations" in k for k in sib), sib
    # Its own row is not listed back to it.
    assert all(s["key"] != "correct_p2_wb" for s in e["siblings"])


def test_a_sibling_has_to_share_the_binding(tmp_path):
    """Found in a browser, not in a test: every literal write in the same
    script was coming back as a sibling, so a coefficient was shown beside two
    confidence bounds belonging to a different estimate entirely."""
    ms, targets = qutub_like(tmp_path)
    w(tmp_path, "R/10_itt.R",
      ITT + '\nwrite_frag(other, file.path(exh_dir, "unrelated_total.tex"))\n')
    targets = targets + [w(ms, "exhibits/unrelated_total.tex", "999%\n")]
    got = manifest.describe(ms, targets)
    keys = [s["key"] for s in got["correct_p2_wb"]["siblings"]]
    assert "correct_b2" in keys, keys
    assert "unrelated_total" not in keys, keys


def test_a_literal_named_fragment_is_described_too(tmp_path):
    w(tmp_path, "R/tab_02.R", (
        'balance_vars <- list(list(var = "correct", label = "Correct case mgmt"))\n'
        'write_frag(fmt_p(p_by_var[["correct"]]),\n'
        '           here::here("manuscript", "exhibits", "balance_correct_p.tex"))\n'
    ))
    ms = tmp_path / "manuscript"
    t = w(ms, "exhibits/balance_correct_p.tex", "0.412%\n")
    e = manifest.describe(ms, [t])["balance_correct_p"]
    assert e["producer"] == "R/tab_02.R", e
    assert "p-value" in (e["description"] or "").lower(), e
    # The subject is recoverable from the literal index into the registry.
    assert e["subject"] == "Correct case mgmt", e


def test_a_stata_fragment_names_its_producer(tmp_path):
    w(tmp_path, "do/00-response.do", (
        'reg caseload late i.country, cluster(country)\n'
        'local b_late = string(_b[late], "%4.2f")\n'
        'cap file close dowresult\n'
        'file open dowresult using "${git}/manuscript/dow-numbers.tex", write replace\n'
        'file write dowresult "\\newcommand{\\dowContrast}{`b_late\'}" _n\n'
        'file close dowresult\n'
    ))
    ms = tmp_path / "manuscript"
    t = w(ms, "dow-numbers.tex", "\\newcommand{\\dowContrast}{0.42}\n")
    e = manifest.describe(ms, [t])["dow-numbers"]
    assert e["producer"] == "do/00-response.do", e
    assert e["description"], e


# ------------------------------------------- what the code does NOT say
#
# The half that matters. Every one of these must come back unclaimed.


def test_a_name_outside_the_registry_is_not_claimed(tmp_path):
    """`%s_b%d.tex` would match `mumbai_correct_b2`, and must not.

    The template's own loop reads a registry of outcome names, so a captured
    name that is not in it was written by some other script. Without this the
    Mumbai coefficients would all be attributed to the Patna model.
    """
    ms, _ = qutub_like(tmp_path)
    t = w(ms, "exhibits/mumbai_correct_b2.tex", "0.031%\n")
    e = manifest.describe(ms, [t])["mumbai_correct_b2"]
    assert e["producer"] is None, e
    assert e["description"] is None, e
    assert "unknown" in (e["reason"] or "").lower(), e


def test_two_scripts_that_could_write_one_name_claim_neither(tmp_path):
    w(tmp_path, "R/a.R", 'writeLines(x, here::here("manuscript", "shared.tex"))\n')
    w(tmp_path, "R/b.R", 'writeLines(y, here::here("manuscript", "shared.tex"))\n')
    ms = tmp_path / "manuscript"
    t = w(ms, "shared.tex", "12\n")
    e = manifest.describe(ms, [t])["shared"]
    assert e["producer"] is None, e
    assert e["description"] is None, e
    assert "two" in (e["reason"] or "").lower() or "ambiguous" in (e["reason"] or "").lower()


def test_a_fragment_nothing_writes_says_so(tmp_path):
    """estonia-ecm's case: the .tex is pasted in by hand from a CSV."""
    w(tmp_path, "code/07_table2.R", 'fwrite(dta, file.path(p, "table2_cross.csv"))\n')
    ms = tmp_path / "latex"
    t = w(ms, "tables/table2_cross.tex", "\\toprule\nA & B \\\\\n\\bottomrule\n")
    e = manifest.describe(ms, [t])["table2_cross"]
    assert e["producer"] is None, e
    assert e["description"] is None, e
    assert e["reason"], e


def test_a_statistic_the_filename_and_the_code_disagree_on_is_not_named(tmp_path):
    """The suffix says p-value; the expression written is a coefficient.

    One of the two is wrong and nothing here can tell which, so the entry keeps
    the producer and drops the claim.
    """
    w(tmp_path, "R/x.R", (
        'vars <- c("correct")\n'
        'for (v in vars) {\n'
        '  cf <- coef(m)[["treat"]]\n'
        '  write_frag(sprintf("%.3f", cf), file.path(exh, sprintf("%s_p2_wb.tex", v)))\n'
        '}\n'
    ))
    ms = tmp_path / "manuscript"
    t = w(ms, "exhibits/correct_p2_wb.tex", "0.096%\n")
    e = manifest.describe(ms, [t])["correct_p2_wb"]
    assert e["producer"] == "R/x.R", e
    assert e["statistic"] is None, e
    assert "p-value" not in (e["description"] or "").lower(), e


def test_an_unresolvable_loop_variable_is_not_bound(tmp_path):
    """The loop reads a column of a data frame, so its domain is unknowable."""
    w(tmp_path, "R/y.R", (
        'for (v in unique(df$outcome)) {\n'
        '  write_frag(b, file.path(exh, sprintf("%s_b2.tex", v)))\n'
        '}\n'
    ))
    ms = tmp_path / "manuscript"
    t = w(ms, "exhibits/anything_b2.tex", "0.1%\n")
    e = manifest.describe(ms, [t])["anything_b2"]
    assert e["producer"] is None, e
    assert e["description"] is None, e


# ------------------------------------------------------------- the cache


def test_the_cache_is_written_beside_nothing_the_author_tracks(tmp_path):
    """It goes in the build directory, which already writes its own .gitignore.

    A manifest dropped into the manuscript directory would make `git status`
    grow on a read-only serve, which is the exact failure the build directory's
    .gitignore exists to prevent.
    """
    ms, targets = qutub_like(tmp_path)
    cache = tmp_path / "build" / "manuscriptor"
    before = sorted(p.name for p in ms.iterdir())
    manifest.describe(ms, targets, cache_dir=cache)
    assert (cache / manifest.MANIFEST_NAME).exists()
    assert sorted(p.name for p in ms.iterdir()) == before


def test_a_cached_entry_is_reused_while_its_producer_is_unchanged(tmp_path):
    ms, targets = qutub_like(tmp_path)
    cache = tmp_path / "build" / "manuscriptor"
    manifest.describe(ms, targets, cache_dir=cache)

    path = cache / manifest.MANIFEST_NAME
    data = json.loads(path.read_text())
    # Same text in both fields: this is a re-derivation, not a hand edit.
    data["values"]["correct_p2_wb"]["description"] = "CACHED"
    data["values"]["correct_p2_wb"]["derived"] = "CACHED"
    path.write_text(json.dumps(data))

    again = manifest.describe(ms, targets, cache_dir=cache)
    assert again["correct_p2_wb"]["description"] == "CACHED"


def test_editing_the_producer_re_derives_the_description(tmp_path):
    ms, targets = qutub_like(tmp_path)
    cache = tmp_path / "build" / "manuscriptor"
    first = manifest.describe(ms, targets, cache_dir=cache)
    assert "Correct case management" in first["correct_p2_wb"]["description"]

    w(tmp_path, "R/10_itt.R", ITT.replace("Correct case management", "Correct case mgmt"))
    second = manifest.describe(ms, targets, cache_dir=cache)
    assert "Correct case mgmt" in second["correct_p2_wb"]["description"], second["correct_p2_wb"]


def test_a_producer_rewritten_without_a_trace_still_invalidates(tmp_path):
    """Same size, same timestamp, different contents: only the hash can tell.

    The repo-level check is stat-based and would wave this through. The entry
    carries its producing file's CONTENT hash, which is what the rule asks for,
    and a file restored by a tool that preserves timestamps is not exotic.
    """
    ms, targets = qutub_like(tmp_path)
    cache = tmp_path / "build" / "manuscriptor"
    first = manifest.describe(ms, targets, cache_dir=cache)
    assert "Correct case management" in first["correct_p2_wb"]["description"]

    script = tmp_path / "R" / "10_itt.R"
    was = script.stat()
    script.write_text(ITT.replace("Correct case management", "CORRECT CASE MANAGEMENT"))
    os.utime(script, ns=(was.st_atime_ns, was.st_mtime_ns))
    now = script.stat()
    assert (now.st_size, now.st_mtime_ns) == (was.st_size, was.st_mtime_ns), "the stat moved"

    again = manifest.describe(ms, targets, cache_dir=cache)
    assert "CORRECT CASE MANAGEMENT" in again["correct_p2_wb"]["description"]


def test_a_hand_edited_description_survives_regeneration(tmp_path):
    """The one that decides whether he ever trusts the feature."""
    ms, targets = qutub_like(tmp_path)
    cache = tmp_path / "build" / "manuscriptor"
    manifest.describe(ms, targets, cache_dir=cache)

    path = cache / manifest.MANIFEST_NAME
    data = json.loads(path.read_text())
    data["values"]["correct_p2_wb"]["description"] = "The p-value I actually mean."
    path.write_text(json.dumps(data))

    # Regenerate, with the producer changed underneath so the entry is stale.
    w(tmp_path, "R/10_itt.R", ITT.replace("Correct case management", "Correct case mgmt"))
    again = manifest.describe(ms, targets, cache_dir=cache)
    e = again["correct_p2_wb"]
    assert e["description"] == "The p-value I actually mean."
    assert e["source"] == "hand"
    # And the re-derivation is kept beside it, so a stale hand note is visible.
    assert "Correct case mgmt" in (e["derived"] or "")


def test_a_hand_written_overlay_beside_the_fragments_wins(tmp_path):
    """`values.json` in the manuscript directory is the author's file.

    Nothing in the server ever writes it, which is what makes a hand edit
    survive by construction rather than by a merge rule remembering to.
    """
    ms, targets = qutub_like(tmp_path)
    overlay = ms / manifest.MANIFEST_NAME
    overlay.write_text(json.dumps({"values": {"correct_p2_wb": "Mine, not yours."}}))
    stamp = overlay.stat().st_mtime_ns

    got = manifest.describe(ms, targets, cache_dir=tmp_path / "build" / "manuscriptor")
    assert got["correct_p2_wb"]["description"] == "Mine, not yours."
    assert got["correct_p2_wb"]["source"] == "hand"
    assert overlay.stat().st_mtime_ns == stamp, "the overlay was written to"


def test_the_cache_survives_a_corrupt_file(tmp_path):
    ms, targets = qutub_like(tmp_path)
    cache = tmp_path / "build" / "manuscriptor"
    cache.mkdir(parents=True)
    (cache / manifest.MANIFEST_NAME).write_text("{not json")
    got = manifest.describe(ms, targets, cache_dir=cache)
    assert got["correct_p2_wb"]["description"]


# ----------------------------------------------------------- the history


GIT = shutil.which("git")


def git_repo(root: Path, ms: Path) -> None:
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@e",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@e")
    subprocess.run([GIT, "init", "-q"], cwd=root, check=True, env=env)
    subprocess.run([GIT, "add", "-A"], cwd=root, check=True, env=env)
    subprocess.run([GIT, "commit", "-qm", "first pass"], cwd=root, check=True, env=env)
    (ms / "exhibits" / "correct_p2_wb.tex").write_text("0.412%\n")
    subprocess.run([GIT, "add", "-A"], cwd=root, check=True, env=env)
    subprocess.run([GIT, "commit", "-qm", "estimator correction"], cwd=root, check=True, env=env)


@pytest.mark.skipif(not GIT, reason="git not installed")
def test_history_is_read_out_of_git_when_the_fragment_is_tracked(tmp_path):
    ms, targets = qutub_like(tmp_path)
    git_repo(tmp_path, ms)
    e = manifest.describe(ms, targets)["correct_p2_wb"]
    hist = e["history"]
    assert len(hist) >= 2, hist
    assert hist[0]["value"].startswith("0.412")
    assert hist[1]["value"].startswith("0.096")
    assert "estimator correction" in hist[0]["why"]
    assert hist[0]["when"] >= hist[1]["when"]


@pytest.mark.skipif(not GIT, reason="git not installed")
def test_an_uncommitted_change_is_the_newest_history_entry(tmp_path):
    ms, targets = qutub_like(tmp_path)
    git_repo(tmp_path, ms)
    (ms / "exhibits" / "correct_p2_wb.tex").write_text("0.777%\n")
    e = manifest.describe(ms, targets)["correct_p2_wb"]
    assert e["history"][0]["value"].startswith("0.777")
    assert "uncommitted" in e["history"][0]["why"].lower()


def test_no_history_says_why_rather_than_showing_an_empty_list(tmp_path):
    ms, targets = qutub_like(tmp_path)
    e = manifest.describe(ms, targets)["correct_p2_wb"]
    assert e["history"] == []
    assert "git" in (e["history_note"] or "").lower(), e


# ------------------------------------------------- what reaches the page


def test_a_new_script_appearing_re_derives_everything(tmp_path):
    """The cache key is the producer's hash, and a NEW file has no entry to key.

    A second script that could write the same name makes an entry that was
    correct yesterday ambiguous today, so the whole manifest is re-derived when
    the set of scripts changes rather than only when a known one does.
    """
    ms, targets = qutub_like(tmp_path)
    cache = tmp_path / "build" / "manuscriptor"
    first = manifest.describe(ms, targets, cache_dir=cache)
    assert first["correct_p2_wb"]["producer"] == "R/10_itt.R"

    w(tmp_path, "R/99_other.R", (
        'others <- c("correct")\n'
        'for (v in others) {\n'
        '  write_frag(p, file.path(exh, sprintf("%s_p2_wb.tex", v)))\n'
        '}\n'
    ))
    again = manifest.describe(ms, targets, cache_dir=cache)
    assert again["correct_p2_wb"]["producer"] is None
    assert "two scripts" in again["correct_p2_wb"]["reason"]


def test_a_trailing_percent_is_a_comment_and_not_a_unit(tmp_path):
    """`write_frag` terminates every fragment with `%` to eat the newline.

    Showing it would report a coefficient of 0.079 as 0.079%, which is a
    different number and a wrong one.
    """
    ms, targets = qutub_like(tmp_path)
    got = manifest.describe(ms, targets)
    assert got["correct_b2"]["value"] == "0.079"
    (ms / "exhibits" / "correct_b2.tex").write_text("12.5\\%\n")
    assert manifest.describe(ms, targets)["correct_b2"]["value"] == "12.5\\%"


def test_a_table_fragment_is_not_reported_as_an_unnamed_number(tmp_path):
    w(tmp_path, "R/tab.R", 'writeLines(rows, here::here("manuscript", "exhibits", "tab_main.tex"))\n')
    ms = tmp_path / "manuscript"
    t = w(ms, "exhibits/tab_main.tex", "\\begin{tabular}{lcc}\n\\toprule\nA & B & C \\\\\n")
    e = manifest.describe(ms, [t])["tab_main"]
    assert "table body" in e["description"].lower(), e
    assert e["statistic"] is None


def test_the_producing_code_is_carried_so_the_panel_can_show_it(tmp_path):
    e = entry(tmp_path, "correct_p2_wb")
    assert "write_frag(p_frag" in e["code"], e["code"]
    assert e["lines"].startswith("10_itt.R:")


def test_the_extension_is_picked_up_by_the_loader():
    from manuscriptor.templates.ext import load

    assert "values" in load()


# ------------------------------------------------- the panel, under node
#
# No browser here, and `viewer.js` returns early with no `document`, so its
# extension surface does not exist under node. The harness therefore supplies
# the contract itself -- `extend`, `_extensions`, `ext` -- which is what the
# extension is written against, and calls the builders with a `ctx` that
# records what they were handed. Registration through the real viewer is
# verified in a browser instead; this is the logic underneath it.

NODE = shutil.which("node")
ROOT = Path(__file__).resolve().parents[1]
EXT = ROOT / "manuscriptor" / "templates" / "static" / "ext" / "values.js"

HARNESS = r"""
const fs = require('fs');

const sent = [];
var MS = { values: {} };
const ctx = {
  send: function (p) { sent.push(p); return true; },
  escape: function (s) {
    return String(s === undefined || s === null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  },
  card: function (t, r, html) { return '<section data-card="' + t + '">' + html + '</section>'; },
  block: function () { return null; },
  selection: function () { return { kind: 'block', key: 'b-1', blockId: 'b-1' }; },
  ms: function () { return MS; },
  notify: function () {},
  refresh: function () {}
};

const EXT = [];
const MSViewer = { extend: function (e) { EXT.push(e); }, ext: ctx, _extensions: EXT };
global.MSViewer = MSViewer;

const mod = { exports: {} };
new Function('module', 'exports', 'MSViewer', 'document', 'fetch',
  fs.readFileSync(%(ext)s, 'utf8'))(mod, mod.exports, MSViewer, undefined, undefined);

const api = mod.exports;
const input = JSON.parse(process.argv[1]);
api._set(input.manifest, input.state);
const out = %(call)s;
process.stdout.write(JSON.stringify(out === undefined ? null : out));
"""


def node(call: str, **payload):
    assert NODE, "node is required"
    script = HARNESS % {"ext": json.dumps(str(EXT)), "call": call}
    p = subprocess.run([NODE, "-e", script, json.dumps(payload)],
                       capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout)


RECORD = {
    "key": "correct_p2_wb",
    "path": "exhibits/correct_p2_wb.tex",
    "value": "0.096",
    "producer": "R/10_patna_itt.R",
    "producer_line": 190,
    "description": "Wild-cluster bootstrap p-value for Correct case management, round 2.",
    "statistic": "wild-cluster bootstrap p-value",
    "subject": "Correct case management",
    "source": "derived",
    "model": {"call": "feols", "formula": "correct ~ i(round, treat) | fidnum",
              "cluster": "fidcode", "fe": ["fidnum", "caseround"]},
    "siblings": [{"key": "correct_b2", "statistic": "coefficient", "value": "0.079"},
                 {"key": "correct_n", "statistic": "number of observations", "value": "4318"}],
    "history": [{"when": "2026-07-19", "value": "0.096", "why": "estimator correction"},
                {"when": "2026-06-02", "value": "0.004", "why": "first pass"}],
    "code": "write_frag(p_frag, file.path(exh_dir, sprintf('%s_p%d_wb.tex', v, r)))",
    "lines": "10_patna_itt.R:184-196",
}
BLOCK = {"values": [{"key": "correct_p2_wb", "producer": None,
                     "description": RECORD["description"]}]}


@pytest.mark.skipif(not NODE, reason="node not installed")
def test_the_extension_registers_a_tab_only_where_there_are_values():
    got = node("[EXT.map(e => e.name), EXT[0].tab('b-1', %s, ctx),"
               " EXT[0].tab('b-2', {values: []}, ctx)]" % json.dumps(BLOCK),
               manifest={"correct_p2_wb": RECORD}, state="ready")
    names, with_values, without = got
    assert "values" in names
    assert with_values["name"] == "Values" and with_values["n"] == 1
    assert without is None


@pytest.mark.skipif(not NODE, reason="node not installed")
def test_the_panel_shows_the_estimate_row_the_model_and_the_history():
    html = node("api._body(ctx, %s)" % json.dumps(BLOCK),
                manifest={"correct_p2_wb": RECORD}, state="ready")
    assert "Wild-cluster bootstrap p-value" in html
    # the estimate this belongs to, read off the siblings
    assert "0.079" in html and "coefficient" in html
    assert "4318" in html
    # the model
    assert "fidcode" in html and "correct ~ i(round, treat) | fidnum" in html
    # the history, newest first
    assert html.index("estimator correction") < html.index("first pass")
    assert "0.004" in html
    # the producing code
    assert "write_frag(p_frag" in html


@pytest.mark.skipif(not NODE, reason="node not installed")
def test_the_panel_still_works_when_the_manifest_never_loads():
    """A static export opened from disk cannot fetch. It must still describe."""
    html = node("api._body(ctx, %s)" % json.dumps(BLOCK), manifest=None, state="absent")
    assert RECORD["description"] in html
    assert "manifest has not loaded" in html


@pytest.mark.skipif(not NODE, reason="node not installed")
def test_a_value_with_no_description_says_so_rather_than_inventing_one():
    block = {"values": [{"key": "table2_cross", "producer": None, "description": None}]}
    html = node("api._body(ctx, %s)" % json.dumps(block),
                manifest={"table2_cross": {
                    "key": "table2_cross", "description": None, "producer": None,
                    "reason": "producer unknown: no analysis script writes a file with this name",
                    "siblings": [], "history": [],
                    "history_note": "This fragment is not committed."}},
                state="ready")
    assert "No description could be derived" in html
    assert "producer unknown" in html


@pytest.mark.skipif(not NODE, reason="node not installed")
def test_the_manifest_fills_the_field_the_value_panel_already_reads():
    """`ms().values` is the viewer's own hook for this and was never filled."""
    got = node("(api._adopt(ctx, {correct_p2_wb: %s}), MS.values.correct_p2_wb.code)"
               % json.dumps(RECORD), manifest=None, state="ready")
    assert "write_frag" in got


@pytest.mark.skipif(not NODE, reason="node not installed")
def test_a_correction_is_a_comment_and_never_a_write():
    got = node("(api._ask(ctx, 'correct_p2_wb'), sent)",
               manifest={"correct_p2_wb": RECORD}, state="ready")
    assert len(got) == 1
    assert got[0]["type"] == "chat"
    assert got[0]["block"] == "b-1"
    assert "correct_p2_wb" in got[0]["body"]
    assert "values.json" in got[0]["body"]


def test_the_value_rows_of_a_block_carry_the_description(tmp_path):
    """The one field build.py is allowed to fill, filled."""
    from manuscriptor.server import build as build_mod

    ms, _ = qutub_like(tmp_path)
    (ms / "main.tex").write_text(
        "\\documentclass{article}\n\\begin{document}\n"
        "The bootstrap p-value is $p=\\input{exhibits/correct_p2_wb.tex}$ overall.\n"
        "\\end{document}\n"
    )
    b = build_mod.build(ms, output_dir=tmp_path / "out")
    rows = [v for rec in b.blob["blocks"].values() for v in rec["values"]]
    described = [v for v in rows if v["key"] == "correct_p2_wb"]
    assert described, rows
    assert described[0]["description"], described[0]
    assert "p-value" in described[0]["description"].lower()
