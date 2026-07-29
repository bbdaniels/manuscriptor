r"""Find in the manuscript.

Cmd+F (and Ctrl+F, because the same page runs in the Mac app's WKWebView and in
an ordinary browser tab) opens a bar that searches the RENDERED prose in the
manuscript column. Not the LaTeX: the author is looking at the paper, and the
thing he wants to jump to is a sentence he can see.

Two halves are tested here, and the second is the one that matters.

THE MATCHER is pure and runs under plain node: what counts as a match, how many
there are, which one is current, and where a match sits once the flat text is
mapped back onto the text nodes it came from.

THE PAGE is the risk. This document is patched live by websocket frames, so a
highlight laid over a paragraph is laid over a paragraph that may be replaced
under it a second later. Those tests load the page the SERVER rendered, open
find inside it, and then hand it frames the SERVER built -- the same rule
`tests/test_live_frames.py` sets out: a test may not write a frame. A highlight
that survives its own text is the defect this file exists to catch.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tests import pagedriver

ROOT = Path(__file__).resolve().parents[1]
JS = ROOT / "tests" / "js"
EXT = ROOT / "manuscriptor" / "templates" / "static" / "ext" / "find.js"
NODE = shutil.which("node")


# --------------------------------------------------------------- the matcher


PURE = r"""
const fs = require('fs');
const mod = { exports: {} };
const MSViewer = { extend: function () {}, ext: {} };
new Function('module', 'exports', 'MSViewer', 'document', 'window',
  fs.readFileSync(%(ext)s, 'utf8'))(mod, mod.exports, MSViewer, undefined, undefined);
const api = mod.exports;
const args = JSON.parse(process.argv[1]);
const out = api[%(fn)s].apply(null, args);
process.stdout.write(JSON.stringify(out === undefined ? null : out));
"""


def pure(fn: str, *args):
    """Call one exported function of find.js under node, with no document."""
    assert NODE, "node is required for these tests"
    script = PURE % {"ext": json.dumps(str(EXT)), "fn": json.dumps(fn)}
    p = subprocess.run([NODE, "-e", script, json.dumps(list(args))],
                       capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout)


@pytest.mark.skipif(not NODE, reason="node not installed")
def test_the_matcher_ignores_case_and_never_overlaps():
    """"aa" in "aaaa" is two matches, not three: a match consumes its text."""
    assert pure("_matches", "The Treatment raised treatment rates", "treatment") == \
        [[4, 13], [21, 30]]
    assert pure("_matches", "aaaa", "aa") == [[0, 2], [2, 4]]


@pytest.mark.skipif(not NODE, reason="node not installed")
def test_a_query_crossing_a_line_break_still_matches():
    """Pandoc wraps prose inside a paragraph, so the rendered text carries the
    newline the author never typed. A two-word query that fails at a line break
    is a find bar that fails on exactly the phrases worth finding."""
    text = "the treatment raised\nscreening rates across all"
    assert pure("_matches", text, "raised screening") == [[14, 30]]


@pytest.mark.skipif(not NODE, reason="node not installed")
def test_a_query_of_nothing_matches_nothing():
    """An empty box is not a search for every character in the paper."""
    assert pure("_matches", "anything at all", "") == []
    assert pure("_matches", "anything at all", "   ") == []
    assert pure("_matches", "anything at all", None) == []


@pytest.mark.skipif(not NODE, reason="node not installed")
def test_a_regex_in_the_box_is_a_string_in_the_box():
    """The author searching for `p(x)` gets the literal, not a capture group,
    and an unbalanced bracket must not throw the page over."""
    assert pure("_matches", "we report p(x) below", "p(x)") == [[10, 14]]
    assert pure("_matches", "a [bracket here", "[bracket") == [[2, 10]]


@pytest.mark.skipif(not NODE, reason="node not installed")
def test_next_and_previous_wrap_around():
    """Past the last match is the first one. There is no end of the paper."""
    assert pure("_step", 6, 7, 1) == 0
    assert pure("_step", 0, 7, -1) == 6
    assert pure("_step", 2, 7, 1) == 3
    assert pure("_step", -1, 7, 1) == 0, "the first Enter goes to the first match"
    assert pure("_step", 0, 0, 1) == -1, "nothing to step through"


@pytest.mark.skipif(not NODE, reason="node not installed")
def test_a_match_spanning_two_text_nodes_locates_both_ends():
    """A citation splits a sentence into three text nodes. A phrase that runs
    across the split is one match, and it has to be told where it starts and
    where it stops in terms the DOM understands."""
    # chunk lengths: "raised by " (10), "Smith 2020" (10), " across all" (11)
    got = pure("_locate", [10, 10, 11], 7, 16)
    assert got == {"a": 0, "ao": 7, "b": 1, "bo": 6}


@pytest.mark.skipif(not NODE, reason="node not installed")
def test_a_match_ending_on_a_node_boundary_stays_in_that_node():
    """An end offset that lands exactly on a seam belongs to the node it
    finished, not to the empty head of the next one -- which would be a
    zero-width range the browser paints nowhere."""
    assert pure("_locate", [10, 10], 0, 10) == {"a": 0, "ao": 0, "b": 0, "bo": 10}
    assert pure("_locate", [10, 10], 10, 14) == {"a": 1, "ao": 0, "b": 1, "bo": 4}


@pytest.mark.skipif(not NODE, reason="node not installed")
def test_the_current_match_follows_its_text_through_a_patch():
    """A block above the current match is rewritten and every offset below it
    moves. The current match is anchored to WHERE IT WAS IN THE TEXT, not to its
    ordinal and not to its block id -- an edit renames its own block, so a block
    id is the one thing guaranteed to have changed."""
    assert pure("_nearest", [12, 400, 806, 1290], 800) == 2
    assert pure("_nearest", [12, 400], 9000) == 1, "past the end is the last match"
    assert pure("_nearest", [], 800) == -1
    # A tie goes to the earlier match, so the choice is not a coin flip.
    assert pure("_nearest", [90, 110], 100) == 0


# ------------------------------------------------------------------- the page


WHY = pagedriver.missing()

RIG = r"""
const fs = require('fs');
const { JSDOM, VirtualConsole } = require('jsdom');

/* `node -e SCRIPT -- a b` eats the `--` itself, so the plan is argv[1]. */
const plan = JSON.parse(fs.readFileSync(process.argv[1], 'utf8'));
const html = fs.readFileSync(process.argv[2], 'utf8');

const virtualConsole = new VirtualConsole();
virtualConsole.on('jsdomError', (e) => { console.error(String((e && e.stack) || e)); });
['log', 'info', 'warn', 'error', 'debug'].forEach((lvl) => {
  virtualConsole.on(lvl, (...a) => { console.error('[page]', ...a); });
});

/* The Highlight API, stood in for. jsdom ships no `CSS.highlights`, and the
 * point of the stub is that it records RANGES: a range still knows what text it
 * covers, so a stale highlight left over a replaced paragraph is visible to a
 * test as text that is no longer in the document. */
function installHighlights(window) {
  function Fake() {
    this.ranges = [];
    for (var i = 0; i < arguments.length; i++) this.ranges.push(arguments[i]);
  }
  Fake.prototype.add = function (r) { this.ranges.push(r); return this; };
  Fake.prototype.clear = function () { this.ranges = []; };
  window.Highlight = Fake;
  window.CSS = window.CSS || {};
  window.CSS.highlights = new Map();
}

const dom = new JSDOM(html, {
  runScripts: 'dangerously',
  pretendToBeVisual: true,
  url: 'http://127.0.0.1:8000/',
  virtualConsole,
  beforeParse(window) {
    window.WebSocket = function () {
      this.readyState = 1;
      this.send = () => {};
      this.close = () => {};
    };
    window.WebSocket.OPEN = 1;
    window.fetch = () => Promise.resolve({ ok: true, status: 200,
      json: () => Promise.resolve({}), text: () => Promise.resolve('') });
    window.__errors = [];
    if (!plan.noHighlightApi) installHighlights(window);
    window.addEventListener('error', (e) => {
      window.__errors.push(String((e.error && e.error.stack) || e.message));
    });
  },
});

const window = dom.window;
const document = window.document;

function ready() {
  if (document.readyState === 'complete') return Promise.resolve();
  return new Promise((res) => window.addEventListener('load', res, { once: true }));
}

function key(target, k, opts) {
  const ev = new window.KeyboardEvent('keydown', Object.assign({
    key: k, bubbles: true, cancelable: true,
  }, opts || {}));
  (target || document).dispatchEvent(ev);
  return ev.defaultPrevented;
}

const events = [];

function bar() { return document.querySelector('.msfind'); }
function box() { return document.querySelector('.msfind input'); }

function snap() {
  const hl = {};
  if (window.CSS && window.CSS.highlights) {
    window.CSS.highlights.forEach((h, name) => {
      hl[name] = (h.ranges || []).map((r) => r.toString());
    });
  }
  const b = bar();
  const inner = document.getElementById('doc-inner');
  return {
    open: !!b,
    query: box() ? box().value : null,
    count: b ? (b.querySelector('.msfind-n') || {}).textContent : null,
    focused: document.activeElement ? (document.activeElement.className ||
      document.activeElement.tagName) : null,
    highlights: hl,
    // Everything the highlight covers must still be IN the document. A range
    // over a detached node is the orphan this whole file is about.
    detached: Object.keys(hl).reduce((n, name) => {
      const h = window.CSS.highlights.get(name);
      return n + (h.ranges || []).filter((r) => !document.contains(
        r.startContainer.nodeType === 3 ? r.startContainer.parentNode : r.startContainer)).length;
    }, 0),
    mx: Array.from(document.querySelectorAll('#doc-inner [data-mx]'))
      .map((e) => e.getAttribute('data-mx')),
    selected: Array.from(document.querySelectorAll('#doc-inner .sel'))
      .map((e) => e.getAttribute('data-mx')),
    docHtml: inner ? inner.innerHTML : '',
    text: inner ? inner.textContent : '',
    errors: window.__errors,
  };
}

async function main() {
  await ready();
  const V = window.MSViewer;
  if (!V) { console.error('viewer.js did not install MSViewer'); process.exit(2); }

  for (const step of (plan.actions || [])) {
    if (step.do === 'key') {
      const t = step.inBox ? box() : (step.on ? document.querySelector(step.on) : document);
      events.push({ key: step.key, prevented: key(t, step.key, step.mods || {}) });
    } else if (step.do === 'type') {
      const el = box();
      if (!el) { events.push({ type: step.q, ok: false }); continue; }
      el.value = step.q;
      el.dispatchEvent(new window.Event('input', { bubbles: true }));
      events.push({ type: step.q, ok: true });
    } else if (step.do === 'click') {
      const el = document.querySelector(step.sel);
      if (el) el.dispatchEvent(new window.MouseEvent('click', { bubbles: true, cancelable: true }));
      events.push({ click: step.sel, ok: !!el });
    } else if (step.do === 'frame') {
      V.handle(step.msg);
    } else if (step.do === 'settle') {
      await new Promise((r) => setTimeout(r, 60));
    } else if (step.do === 'snap') {
      events.push({ snap: snap() });
    }
  }
  process.stdout.write(JSON.stringify({ events: events, final: snap() }));
  /* `boot` arms a 15-second interval so the ticker keeps ageing, which is right
   * in a browser and is a process that never exits here. */
  dom.window.close();
  process.exit(0);
}

main().catch((e) => { console.error(String((e && e.stack) || e)); process.exit(3); });
"""


def rig(page_html: str, actions: list, tmp_path: Path, **plan) -> dict:
    """Open the server's page in jsdom and drive the find bar through it."""
    assert not WHY, WHY
    html_file = tmp_path / "page.html"
    html_file.write_text(page_html, encoding="utf-8")
    plan_file = tmp_path / "plan.json"
    plan.update({"actions": actions})
    plan_file.write_text(json.dumps(plan), encoding="utf-8")
    p = subprocess.run(
        [NODE, "-e", RIG, "--", str(plan_file), str(html_file)],
        capture_output=True, text=True, cwd=str(JS),
    )
    assert p.returncode == 0, p.stderr
    out = json.loads(p.stdout)
    assert not out["final"]["errors"], f"the page threw: {out['final']['errors']}"
    return out


PAPER = r"""\documentclass{article}
\begin{document}
\section{Results}
The treatment raised screening rates across all three cohorts that were followed.

We read the treatment effect as evidence about the contract and not the payment.

A third paragraph, mentioning the treatment once more, so that wrapping has work.
\end{document}
"""


def served(tmp_path: Path, body: str = PAPER):
    from manuscriptor.server import app

    (tmp_path / "main.tex").write_text(body, encoding="utf-8")
    session = app.Session(tmp_path)
    return session, pagedriver.page(session)


def pushed(session) -> list:
    async def go():
        with pagedriver.record(session) as sent:
            await session.on_change()
        return sent
    return asyncio.run(go())


def snaps(out: dict) -> list:
    return [e["snap"] for e in out["events"] if "snap" in e]


pagemark = pytest.mark.skipif(bool(WHY), reason=str(WHY))


@pagemark
def test_cmd_f_and_ctrl_f_both_open_the_bar(tmp_path):
    """One page, two hosts. In the Mac app WKWebView offers no find bar at all;
    in a browser tab the browser's own bar would open over the top of ours
    unless the key is taken, which is what `preventDefault` is for here."""
    _, page = served(tmp_path)
    for mods in ({"metaKey": True}, {"ctrlKey": True}):
        out = rig(page, [{"do": "key", "key": "f", "mods": mods},
                         {"do": "snap"}], tmp_path)
        assert out["events"][0]["prevented"], \
            "the browser's own find bar will open on top of this one"
        s = snaps(out)[0]
        assert s["open"], f"{mods} did not open the bar"
        assert s["focused"] and "msfind" in s["focused"], \
            "the bar opened and the author has to click into it"


@pagemark
def test_typing_counts_the_matches_and_stepping_wraps(tmp_path):
    """Three paragraphs say "treatment" three times between them."""
    _, page = served(tmp_path)
    out = rig(page, [
        {"do": "key", "key": "f", "mods": {"metaKey": True}},
        {"do": "type", "q": "treatment"},
        {"do": "snap"},
        {"do": "key", "key": "Enter", "inBox": True},
        {"do": "snap"},
        {"do": "key", "key": "Enter", "inBox": True},
        {"do": "key", "key": "Enter", "inBox": True},
        {"do": "snap"},
    ], tmp_path)
    first, second, wrapped = snaps(out)
    assert first["count"] == "1/3", first["count"]
    assert second["count"] == "2/3"
    assert wrapped["count"] == "1/3", "past the last match is the first one"


@pagemark
def test_every_match_is_painted_and_the_current_one_apart(tmp_path):
    """A count with no colour is a search that tells the author how much he has
    not been shown. The current match must be its own highlight, or next and
    previous move a number and nothing on the page."""
    _, page = served(tmp_path)
    out = rig(page, [
        {"do": "key", "key": "f", "mods": {"metaKey": True}},
        {"do": "type", "q": "treatment"},
        {"do": "snap"},
    ], tmp_path)
    hl = snaps(out)[0]["highlights"]
    assert set(hl) == {"ms-find", "ms-find-current"}, sorted(hl)
    assert hl["ms-find-current"] == ["treatment"]
    assert hl["ms-find"] == ["treatment", "treatment"], \
        "the other two matches are not painted"


@pagemark
def test_finding_writes_nothing_into_the_manuscript(tmp_path):
    """The whole reason this is a Highlight and not a wrapper span. The document
    is patched live and spliced back to disk; markup this feature invented would
    be markup the next diff has to survive."""
    _, page = served(tmp_path)
    out = rig(page, [
        {"do": "snap"},
        {"do": "key", "key": "f", "mods": {"metaKey": True}},
        {"do": "type", "q": "treatment"},
        {"do": "key", "key": "Enter", "inBox": True},
        {"do": "snap"},
    ], tmp_path)
    before, during = snaps(out)
    assert during["docHtml"] == before["docHtml"], \
        "find changed the manuscript's markup"
    assert during["mx"] == before["mx"], "a data-mx anchor moved"


@pagemark
def test_a_live_patch_leaves_no_stale_match(tmp_path):
    """THE ONE THAT MATTERS. The author is looking at three matches when a patch
    frame replaces the paragraph holding one of them. The highlight over the old
    text must not survive its text, and the count must not still say three.

    The frame comes from the server's own rebuild, never from this file.
    """
    session, page = served(tmp_path)
    src = (tmp_path / "main.tex").read_text(encoding="utf-8")
    (tmp_path / "main.tex").write_text(
        src.replace("A third paragraph, mentioning the treatment once more,",
                    "A third paragraph, mentioning nothing of the kind,"),
        encoding="utf-8")
    frames = pushed(session)
    assert frames, "the rebuild was discarded in silence"

    out = rig(page, [
        {"do": "key", "key": "f", "mods": {"metaKey": True}},
        {"do": "type", "q": "treatment"},
        {"do": "snap"},
    ] + [{"do": "frame", "msg": f} for f in frames] + [
        {"do": "settle"},
        {"do": "snap"},
    ], tmp_path)
    before, after = snaps(out)
    assert before["count"] == "1/3"
    assert after["detached"] == 0, \
        "a highlight is still pointing at a paragraph that was replaced"
    assert after["count"] == "1/2", \
        f"the count did not follow the patch: {after['count']}"
    painted = sorted(after["highlights"].get("ms-find", []) +
                     after["highlights"].get("ms-find-current", []))
    assert painted == ["treatment", "treatment"]


@pagemark
def test_a_patch_below_the_current_match_leaves_it_where_it_was(tmp_path):
    """Re-scanning is not re-starting. The author found his phrase, something
    else on the page changed, and the current match stays the one he was on."""
    session, page = served(tmp_path)
    src = (tmp_path / "main.tex").read_text(encoding="utf-8")
    (tmp_path / "main.tex").write_text(
        src.replace("so that wrapping has work.", "so that the wrapping has some work."),
        encoding="utf-8")
    frames = pushed(session)
    assert frames

    out = rig(page, [
        {"do": "key", "key": "f", "mods": {"metaKey": True}},
        {"do": "type", "q": "treatment"},
        {"do": "key", "key": "Enter", "inBox": True},
        {"do": "snap"},
    ] + [{"do": "frame", "msg": f} for f in frames] + [
        {"do": "settle"},
        {"do": "snap"},
    ], tmp_path)
    before, after = snaps(out)
    assert before["count"] == "2/3"
    assert after["count"] == "2/3", "the author was thrown back to the first match"
    assert after["detached"] == 0


@pagemark
def test_closing_takes_every_highlight_with_it(tmp_path):
    """Nothing this feature painted may outlive the bar."""
    _, page = served(tmp_path)
    out = rig(page, [
        {"do": "key", "key": "f", "mods": {"metaKey": True}},
        {"do": "type", "q": "treatment"},
        {"do": "key", "key": "Escape", "inBox": True},
        {"do": "snap"},
    ], tmp_path)
    s = snaps(out)[0]
    assert not s["open"], "Escape left the bar up"
    assert s["highlights"] == {}, f"paint outlived the bar: {s['highlights']}"


@pagemark
def test_escape_closes_the_bar_before_it_deselects(tmp_path):
    """Escape already means "drop the selection". With find open it means "close
    find", and the selection underneath has to survive -- otherwise looking
    something up costs the author the paragraph he had open.

    The Escape is dispatched OUTSIDE the box on purpose. Inside it, the viewer's
    own handler stops at "Escape in a field leaves the field" and never reaches
    `deselect`, so an Escape typed there proves nothing about the ordering. The
    author who clicked a paragraph after opening find is the case that does.
    """
    session, page = served(tmp_path)
    first = next(iter(session.blob["blocks"]))
    sel = '#doc-inner [data-mx="%s"]' % first
    out = rig(page, [
        {"do": "click", "sel": sel},
        {"do": "snap"},
        {"do": "key", "key": "f", "mods": {"metaKey": True}},
        {"do": "type", "q": "treatment"},
        {"do": "key", "key": "Escape", "on": sel},
        {"do": "snap"},
        {"do": "key", "key": "Escape", "on": sel},
        {"do": "snap"},
    ], tmp_path)
    picked, closed, again = snaps(out)
    assert picked["selected"] == [first], "the fixture never selected anything"
    assert not closed["open"], "Escape left the bar up"
    assert closed["highlights"] == {}
    assert closed["selected"] == [first], \
        "Escape closed find AND dropped the paragraph the author had open"
    assert again["selected"] == [], \
        "Escape with the bar shut no longer deselects"


@pagemark
def test_find_reads_the_manuscript_and_not_the_furniture(tmp_path):
    """The outline repeats every heading and the gutter labels every paragraph
    with a line number. Neither is prose the author is reading, and a match in
    either is a match he cannot be shown."""
    _, page = served(tmp_path)
    out = rig(page, [
        {"do": "key", "key": "f", "mods": {"metaKey": True}},
        {"do": "type", "q": "Results"},
        {"do": "snap"},
    ], tmp_path)
    s = snaps(out)[0]
    assert s["count"] == "1/1", \
        "the rail's copy of the heading was counted as well as the paper's"

    out2 = rig(page, [
        {"do": "key", "key": "f", "mods": {"metaKey": True}},
        {"do": "type", "q": "¶1"},
        {"do": "snap"},
    ], tmp_path)
    assert snaps(out2)[0]["count"] == "0/0", \
        "the gutter's paragraph label is being searched as if it were prose"


@pagemark
def test_reopening_keeps_the_last_query_ready_to_replace(tmp_path):
    """Cmd+F on an open bar refocuses and selects, the way every find bar
    behaves; it does not clear what is there and lose the highlight."""
    _, page = served(tmp_path)
    out = rig(page, [
        {"do": "key", "key": "f", "mods": {"metaKey": True}},
        {"do": "type", "q": "treatment"},
        {"do": "key", "key": "f", "mods": {"metaKey": True}},
        {"do": "snap"},
    ], tmp_path)
    s = snaps(out)[0]
    assert s["open"] and s["query"] == "treatment"
    assert s["count"] == "1/3"


@pagemark
def test_the_arrows_step_the_same_way_the_keys_do(tmp_path):
    """One implementation behind two gestures, or they drift."""
    _, page = served(tmp_path)
    out = rig(page, [
        {"do": "key", "key": "f", "mods": {"metaKey": True}},
        {"do": "type", "q": "treatment"},
        {"do": "click", "sel": '[data-msfind="next"]'},
        {"do": "snap"},
        {"do": "click", "sel": '[data-msfind="prev"]'},
        {"do": "click", "sel": '[data-msfind="prev"]'},
        {"do": "snap"},
        {"do": "click", "sel": '[data-msfind="close"]'},
        {"do": "snap"},
    ], tmp_path)
    fwd, back, shut = snaps(out)
    assert fwd["count"] == "2/3"
    assert back["count"] == "3/3", "the arrows do not wrap the way Enter does"
    assert not shut["open"] and shut["highlights"] == {}


@pagemark
def test_shift_enter_walks_backwards(tmp_path):
    _, page = served(tmp_path)
    out = rig(page, [
        {"do": "key", "key": "f", "mods": {"metaKey": True}},
        {"do": "type", "q": "treatment"},
        {"do": "key", "key": "Enter", "inBox": True, "mods": {"shiftKey": True}},
        {"do": "snap"},
    ], tmp_path)
    assert snaps(out)[0]["count"] == "3/3"


@pagemark
def test_a_browser_without_the_highlight_api_still_finds(tmp_path):
    """Safari before 17.2 has no `CSS.highlights`. The count, the stepping and
    the scroll are the same code; only the way the current match is made visible
    changes, and it must not start writing markup into the manuscript to do it."""
    _, page = served(tmp_path)
    out = rig(page, [
        {"do": "snap"},
        {"do": "key", "key": "f", "mods": {"metaKey": True}},
        {"do": "type", "q": "treatment"},
        {"do": "key", "key": "Enter", "inBox": True},
        {"do": "snap"},
    ], tmp_path, noHighlightApi=True)
    before, during = snaps(out)
    assert during["open"] and during["count"] == "2/3"
    assert during["docHtml"] == before["docHtml"], \
        "the fallback wrote markup into the manuscript"


# --------------------------------------------------------------- it is shipped


def test_the_find_extension_is_served_with_the_page():
    from manuscriptor.templates.ext import load

    got = load()
    assert "find" in got, "find.js is not loaded into the page"
    assert "MSViewer.extend" in got["find"]


def test_find_holds_no_opinion_about_the_source():
    """Decided scope: the rendered prose the author can see. A find that also
    searched the LaTeX would report matches in text that is not on the screen,
    and would need a second way to address them."""
    src = EXT.read_text(encoding="utf-8")
    for banned in ("blocks[", ".source", "/blocks", "fetch("):
        assert banned not in src, f"find.js reached for {banned}"
