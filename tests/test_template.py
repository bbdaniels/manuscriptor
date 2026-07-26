"""M4 — the viewer template, its stylesheet, and its script.

No browser is available here, so these tests work on three surfaces that can be
checked without one:

  * the rendered template string, for the data contract and the markup;
  * the stylesheet parsed as rules, for where a custom property is declared;
  * `viewer.js` loaded under node, for the pure logic that decides whether an
    edit is safe to send.

The last one is the only part of the front end with real branching, so it is
tested as behaviour rather than by looking for strings.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from jinja2 import Template

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "manuscriptor" / "templates"
INDEX = TEMPLATES / "index.html.j2"
STYLES = TEMPLATES / "static" / "styles.css"
VIEWER = TEMPLATES / "static" / "viewer.js"

NODE = shutil.which("node")

# A source line carrying every character class that could break the embedded
# JSON: straight quotes, LaTeX backslashes, a script close tag, an em dash, a
# non-ASCII letter, and the sentinel codepoints the marker contract uses.
HOSTILE_SOURCE = (
    'He wrote "p=\\input{exhibits/correct_p2_wb}" \\& 90\\% of ⟦MX3f2a91c0de⟧ '
    "cases — see Brøndum “et al.” </script><script>alert(1)</script> "
    "\\citep{nishtar2018time}\\\\"
)


def fixture_blob() -> dict:
    return {
        "title": "Relational contracting in primary care",
        "html": (
            '<h2 data-mx="b-aa00bb11cc">Introduction</h2>\n'
            '<p data-mx="b-3f2a91c0de">Effective primary healthcare requires '
            '<span class="citation" data-cites="nishtar2018time">(Nishtar et al. 2018)</span>.</p>\n'
            '<p data-mx="b-91c4ff0011">Ordering rose by '
            '<span class="val" data-key="test_ordering_pp">10.4pp</span>.</p>\n'
            '<div data-mx="b-ee55dd44cc"><table><tr><td>0.071</td></tr></table></div>\n'
        ),
        "blocks": {
            "b-aa00bb11cc": {
                "id": "b-aa00bb11cc",
                "kind": "heading",
                "file": "main.tex",
                "line_start": 210,
                "line_end": 210,
                "source": "\\section{Introduction}",
                "editable": True,
                "parent_heading": None,
                "includes": [],
                "cites": [],
                "values": [],
            },
            "b-3f2a91c0de": {
                "id": "b-3f2a91c0de",
                "kind": "paragraph",
                "file": "main.tex",
                "line_start": 212,
                "line_end": 212,
                "source": "Effective primary healthcare requires \\citep{nishtar2018time}.",
                "editable": True,
                "parent_heading": "Introduction",
                "includes": [],
                "cites": ["nishtar2018time"],
                "values": [],
            },
            "b-91c4ff0011": {
                "id": "b-91c4ff0011",
                "kind": "paragraph",
                "file": "main.tex",
                "line_start": 214,
                "line_end": 214,
                "source": HOSTILE_SOURCE,
                "editable": True,
                "parent_heading": "Introduction",
                "includes": [
                    {
                        "directive": "\\input{exhibits/correct_p2_wb}",
                        "target": "exhibits/correct_p2_wb.tex",
                    }
                ],
                "cites": ["nishtar2018time"],
                "values": [
                    {
                        "key": "test_ordering_pp",
                        "path": "exhibits/test_ordering_pp.tex",
                        "producer": "code/09_did_main.R",
                        "description": "Effect on ordering the new test, in percentage points.",
                    }
                ],
            },
            "b-ee55dd44cc": {
                "id": "b-ee55dd44cc",
                "kind": "table",
                "file": "tables/table2_cross.tex",
                "line_start": 1,
                "line_end": 18,
                "source": "\\begin{tabular}{lrr}\\end{tabular}",
                "editable": False,
                "parent_heading": "Introduction",
                "includes": [],
                "cites": [],
                "values": [],
            },
        },
        "outline": [
            {"level": 1, "text": "Introduction", "id": "b-aa00bb11cc"},
        ],
        "chats": {
            "b-3f2a91c0de": [
                {
                    "id": "c1",
                    "who": "you",
                    "body": 'Tighten this <b>a lot</b> & cut the "restatement".',
                    "ts": "2026-07-22T11:04:00Z",
                    "state": "resolved",
                }
            ]
        },
        "todos": [
            {"text": "Resolve the 3 red citations", "done": True},
            {"text": "Answer R2 on identification", "done": False},
        ],
        "activity": [{"text": "Flattened 24 files, 0 unresolved", "when": "16 min ago"}],
        "stats": {"files": 24, "cites": 84, "values": 31, "exhibits": 9},
    }


def render(**kwargs) -> str:
    return Template(INDEX.read_text(encoding="utf-8")).render(
        styles_css=STYLES.read_text(encoding="utf-8"),
        viewer_js=VIEWER.read_text(encoding="utf-8"),
        **kwargs,
    )


MS_RE = re.compile(
    r'<script id="ms-data">\s*window\.MS\s*=\s*(.*?);\s*</script>', re.DOTALL
)


def embedded_blob(html: str) -> dict:
    m = MS_RE.search(html)
    assert m, "no <script id=\"ms-data\"> block carrying window.MS"
    return json.loads(m.group(1))


def without_blob(html: str) -> str:
    return MS_RE.sub("<script id=\"ms-data\"></script>", html)


# --------------------------------------------------------------------------
# the data contract
# --------------------------------------------------------------------------


def test_blob_is_embedded_and_round_trips_exactly():
    blob = fixture_blob()
    assert embedded_blob(render(ms=blob)) == blob


def test_hostile_source_survives_the_embedding():
    blob = embedded_blob(render(ms=fixture_blob()))
    assert blob["blocks"]["b-91c4ff0011"]["source"] == HOSTILE_SOURCE


def test_embedded_json_cannot_break_out_of_the_script_element():
    page = render(ms=fixture_blob())
    m = MS_RE.search(page)
    assert m
    payload = m.group(1)
    # A raw "<" inside the payload would let a </script> in a block's source
    # close the element early and turn the rest of the manuscript into markup.
    assert "<" not in payload
    assert "</script>" not in payload


def test_data_mx_ids_survive_into_the_real_markup():
    page = without_blob(render(ms=fixture_blob()))
    for bid in ("b-aa00bb11cc", "b-3f2a91c0de", "b-91c4ff0011", "b-ee55dd44cc"):
        assert f'data-mx="{bid}"' in page, f"{bid} was not rendered into the document"


def test_title_reaches_the_page():
    page = render(ms=fixture_blob())
    assert "Relational contracting in primary care" in page


def test_renders_without_a_blob_for_the_static_export():
    # manuscriptor/evidence/render.py renders this same template with the older
    # variable names. It must degrade to a readable page, not raise.
    page = render(title="Some paper", manuscript_html='<p data-mx="b-1234567890">Hi.</p>')
    blob = embedded_blob(page)
    assert blob["blocks"] == {}
    assert 'data-mx="b-1234567890"' in without_blob(page)


# --------------------------------------------------------------------------
# the page is self-contained
# --------------------------------------------------------------------------


def test_css_and_js_are_inlined_not_linked():
    page = render(ms=fixture_blob())
    assert "--computed" in page, "stylesheet was not inlined"
    assert "window.MS" in page
    assert "MS_DRAFT_PREFIX" in page, "viewer.js was not inlined"
    assert "<link" not in page
    assert "<script src" not in page


# --------------------------------------------------------------------------
# the skins and the hue
# --------------------------------------------------------------------------

RULE_RE = re.compile(r"([^{}]+)\{([^{}]*)\}", re.DOTALL)
COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)


def css_rules() -> list[tuple[str, str]]:
    """(selector, body) pairs. Comments are stripped FIRST: left in, they are
    captured as part of the following selector, which silently turns every
    `sel.startswith(...)` check into a check that never fires."""
    css = COMMENT_RE.sub("", STYLES.read_text(encoding="utf-8"))
    return [(s.strip(), b) for s, b in RULE_RE.findall(css)]


def test_the_rule_parser_sees_the_selectors_it_claims_to():
    sels = [s for s, _ in css_rules()]
    assert ".app" in sels, "the .app base rule was not parsed as its own selector"
    assert any(s.startswith('.app[data-skin="glass"]') for s in sels)


def test_both_skins_are_present():
    css = STYLES.read_text(encoding="utf-8")
    assert '[data-skin="glass"]' in css
    page = render(ms=fixture_blob())
    assert 'data-skin="instrument"' in page
    assert 'data-skin="glass"' in page


def test_hue_is_declared_on_the_root_not_on_the_app():
    declaring = [(sel, body) for sel, body in css_rules() if re.search(r"--h\s*:", body)]
    assert declaring, "no rule declares --h at all"
    for sel, _ in declaring:
        assert ":root" in sel, f"--h is declared on {sel!r}, it belongs on :root"
    for sel, body in css_rules():
        if sel.startswith(".app"):
            assert not re.search(r"--h\s*:", body), f"{sel!r} must not own the hue"


def test_a_skin_that_redefines_the_ink_also_applies_it():
    # A custom property redefined on .app cannot reach a `color` already
    # resolved on body. Glass redefines --ink, so .app must be the element that
    # applies it, or Glass in a light OS paints the light theme's near-black ink
    # on its own dark ground (measured at 1.17:1 in Chromium before this rule).
    applies = [
        sel for sel, body in css_rules()
        if re.search(r"(^|[;{\s])color\s*:\s*var\(--ink\)", body)
    ]
    assert any(s == ".app" for s in applies), ".app must apply color: var(--ink)"
    for sel, body in css_rules():
        if re.search(r"--ink\s*:", body) and ":root" not in sel:
            base = sel.split("[")[0].split(":")[0].strip()
            assert any(a.split("[")[0].strip() == base for a in applies), (
                f"{sel!r} redefines --ink but nothing applies it on {base}"
            )


def test_the_hue_control_writes_to_the_document_root():
    js = VIEWER.read_text(encoding="utf-8")
    assert "documentElement.style.setProperty('--h'" in js
    assert "documentElement.style.setProperty('--sat'" in js


def test_status_colours_do_not_rotate_with_the_hue():
    # Green, amber, red and violet mean verbatim, paraphrase, missing and
    # computed. They are the only things on the page carrying meaning rather
    # than mood, so they are the one set excluded from the palette. The `-soft`
    # tints are backgrounds, not the meaning colour, and may follow the hue;
    # the pattern below does not match them because a `-` follows the name.
    seen = 0
    for sel, body in css_rules():
        for prop in ("--verbatim", "--paraphrase", "--missing", "--computed"):
            for value in re.findall(re.escape(prop) + r"\s*:\s*([^;]+);", body):
                seen += 1
                assert "var(--h)" not in value, f"{prop} in {sel!r} follows the hue"
    assert seen >= 8, "the status colours are not declared in both skins"


# --------------------------------------------------------------------------
# the layout at every window width
# --------------------------------------------------------------------------


def css_rules_in_context() -> list[tuple[str, str, int, str | None]]:
    """(selector, body, offset, enclosing media condition) for every rule.

    `css_rules()` above cannot answer the question these tests ask, which is
    about ORDER: a media query adds no specificity, so a plain rule appearing
    later in the file silently beats an override written inside one. Knowing the
    offset and the enclosing query is what makes that checkable.
    """
    css = COMMENT_RE.sub("", STYLES.read_text(encoding="utf-8"))
    out: list[tuple[str, str, int, str | None]] = []
    i, n = 0, len(css)
    media: str | None = None
    media_end = -1
    while i < n:
        brace = css.find("{", i)
        if brace < 0:
            break
        head = css[i:brace].strip()
        if head.startswith("@"):
            depth, k = 1, brace + 1
            while k < n and depth:
                if css[k] == "{":
                    depth += 1
                elif css[k] == "}":
                    depth -= 1
                k += 1
            if head.startswith("@media"):
                media, media_end = head[len("@media"):].strip(), k
                i = brace + 1          # walk into it: its rules are what matter
            else:
                i = k                  # @keyframes and friends carry no rules
            continue
        close = css.find("}", brace)
        if close < 0:
            break
        # The `}` that ends a media block is not consumed by the walk above, so
        # it arrives glued to the front of the next selector. Left in, that
        # selector matches nothing and every same-selector check below silently
        # passes: `.rail` inside the query never met the `.rail` after it.
        head = head.rsplit("}", 1)[-1].strip()
        enclosing = media if brace < media_end else None
        out.append((head, css[brace + 1:close], brace, enclosing))
        i = close + 1
    return out


def declared(body: str) -> dict[str, str]:
    props: dict[str, str] = {}
    for piece in body.split(";"):
        if ":" in piece:
            name, _, value = piece.partition(":")
            props[name.strip()] = value.strip()
    return props


def test_the_context_parser_sees_the_media_queries_it_claims_to():
    rules = css_rules_in_context()
    assert any(m and "width" in m for _, _, _, m in rules), "no width query parsed"
    assert any(m is None for _, _, _, m in rules), "no plain rules parsed"
    assert any(s == ".cols" for s, _, _, _ in rules), ".cols was not parsed"
    # The rule immediately after a media block is the one the walk can lose.
    for sel, _, _, _ in rules:
        assert "}" not in sel, f"{sel!r} carries a brace: the walk lost a block"
    assert sum(1 for s, _, _, _ in rules if s == ".rail") >= 2, (
        "both the plain .rail rule and its responsive override must be seen, or "
        "the source-order check below is vacuous"
    )


def track_count(value: str) -> int:
    """Top-level tracks in a grid-template-columns value, so the commas inside
    minmax() and clamp() are not read as track separators."""
    tracks, buf, depth = [], "", 0
    for ch in value:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if depth == 0 and ch.isspace():
            if buf:
                tracks.append(buf)
            buf = ""
        else:
            buf += ch
    if buf:
        tracks.append(buf)
    return len(tracks)


def test_the_columns_never_collapse_into_a_stack():
    # Three columns with three independent scrolls is the interface. Restacking
    # them turns a 970px window into three full-width bands, each scrolling
    # separately, with the manuscript reduced to a strip: the app is unusable
    # below the threshold rather than degraded. Narrow means one column goes,
    # never that they stop being columns.
    grids = [
        (sel, declared(body)["grid-template-columns"], media)
        for sel, body, _, media in css_rules_in_context()
        if ".cols" in sel and "grid-template-columns" in declared(body)
    ]
    assert grids, "nothing declares the column layout"
    for sel, value, media in grids:
        assert track_count(value) == 3, (
            f"{sel!r}{' in @media' + media if media else ''} declares "
            f"{track_count(value)} tracks: {value!r}"
        )
    # The states are widths of the two outer tracks, not competing track lists,
    # so no state can restack the grid. A width that is not one single track
    # would put the grid back in play.
    widths = [
        (sel, name, value)
        for sel, body, _, _ in css_rules_in_context()
        for name, value in declared(body).items()
        if name in ("--rail-w", "--insp-w")
    ]
    assert len(widths) >= 3, "the column widths are not being read as variables"
    for sel, name, value in widths:
        assert track_count(value) == 1, (
            f"{sel!r} sets {name} to {track_count(value)} tracks: {value!r}"
        )


def test_a_width_query_override_is_not_beaten_by_a_later_rule():
    # The trap that made the narrow layout fail twice over: `.rail{display:none}`
    # inside `@media (max-width:1000px)` sat three lines ABOVE `.rail{display:
    # flex}`. Equal specificity, later wins, so the rail was never hidden and a
    # narrow window got the stack AND a full-width rail. Responsive overrides
    # therefore live at the end of the file, after everything they override.
    rules = css_rules_in_context()
    for sel, body, pos, media in rules:
        if not (media and "width" in media):
            continue
        props = set(declared(body))
        for other_sel, other_body, other_pos, other_media in rules:
            if other_media or other_pos < pos:
                continue
            if other_sel != sel:
                continue
            clash = props & set(declared(other_body))
            assert not clash, (
                f"@media{media} sets {sorted(clash)} on {sel!r}, but a plain "
                f"{sel!r} rule later in the file (offset {other_pos}) sets the "
                "same property and wins on source order"
            )


def test_the_titlebar_inset_is_a_variable_the_shell_can_measure_into():
    # The page's own title row runs up under the real macOS title bar, so it has
    # to start clear of the traffic lights. That geometry belongs to AppKit: a
    # hardcoded guess was already wrong twice (an 18px document proxy icon
    # landed on top of the row on macOS 26, and full screen hides the buttons
    # entirely and still paid the inset).
    insets = [
        (sel, declared(body)["padding-left"])
        for sel, body, _, _ in css_rules_in_context()
        if "ms-native-titlebar" in sel and "padding-left" in declared(body)
    ]
    assert insets, "nothing insets the page's title row past the traffic lights"
    for sel, value in insets:
        assert "var(--ms-titlebar-inset" in value, (
            f"{sel!r} hardcodes the inset as {value!r}; it must read the "
            "variable the shell measures"
        )


def test_the_rail_can_be_toggled_out_of_the_way():
    css = STYLES.read_text(encoding="utf-8")
    js = VIEWER.read_text(encoding="utf-8")
    page = render(ms=fixture_blob())
    assert 'data-act="rail:toggle"' in page, "no control hides or shows the rail"
    assert "data-rail" in css, "the stylesheet does not honour a rail state"
    assert "rail:toggle" in js, "the viewer does not handle the rail toggle"


# --------------------------------------------------------------------------
# viewer.js, loaded under node
# --------------------------------------------------------------------------


def node_call(fn: str, *args):
    assert NODE, "node is required for these tests"
    script = (
        "const v = require(%s);\n"
        "const out = v[%s].apply(null, JSON.parse(process.argv[1]));\n"
        "process.stdout.write(JSON.stringify(out === undefined ? null : out));\n"
    ) % (json.dumps(str(VIEWER)), json.dumps(fn))
    p = subprocess.run(
        [NODE, "-e", script, json.dumps(list(args))],
        capture_output=True,
        text=True,
    )
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout)


@pytest.mark.skipif(not NODE, reason="node not installed")
def test_an_edit_to_a_block_the_build_no_longer_has_is_held_not_dropped():
    """The branch that cost an author his abstract, 2026-07-26.

    Ids are content-derived, so the first save renames the block being typed
    into. The old code answered an unknown block with a bare `return`: no send,
    no held state, nothing on screen. Every keystroke of a continuous burst after
    the first save went into a draft under a dead id and never reached disk.
    """
    d = node_call("saveDecision", "some new text", None, {"ok": True})
    assert d["action"] == "held", "an unknown block must be reported, never skipped"
    assert d["reason"], "a held state with no reason is the same silence"


@pytest.mark.skipif(not NODE, reason="node not installed")
@pytest.mark.parametrize(
    "text,block,validity,want",
    [
        (None, {"source": "x"}, None, "none"),
        ("x", {"source": "x"}, {"ok": True}, "clean"),
        ("y", {"source": "x", "editable": False}, {"ok": True}, "none"),
        ("y", {"source": "x"}, {"ok": False, "reason": "unbalanced"}, "held"),
        ("y", {"source": "x"}, {"ok": True}, "send"),
    ],
)
def test_the_save_decision_covers_every_other_branch(text, block, validity, want):
    assert node_call("saveDecision", text, block, validity)["action"] == want


def test_the_open_editor_reads_its_block_id_from_the_dom():
    """A captured id is what went stale: the panel is deliberately not rebuilt
    while its editor has focus, so nothing re-ran the closure that held it."""
    js = VIEWER.read_text(encoding="utf-8")
    body = js[js.index("function wireInspector"):js.index("function rememberCaret")]
    assert "var cur = function () { return src.getAttribute('data-block'); }" in body, (
        "the editor must resolve its block id from the element, not capture it"
    )
    for handler in ("input", "blur"):
        seg = body[body.index("addEventListener('" + handler + "'"):]
        seg = seg[:seg.index("});")]
        assert "cur()" in seg, f"the {handler} handler must read the live id"

    # And the rename must maintain the attribute the editor reads.
    renames = js[js.index("function applyRenames"):js.index("function applyBlockHtml")]
    assert "data-block" in renames, "a rename must re-key the open editor"
    assert "S.saveFor" in renames, "a rename must re-key the pending save"


CITES = {
    "green": {"status": "verbatim"},
    "amber": {"status": "paraphrase"},
    "red": {"status": "missing"},
}


@pytest.mark.skipif(not NODE, reason="node not installed")
@pytest.mark.parametrize(
    "keys,want",
    [
        (["green"], "verbatim"),
        (["amber"], "para"),
        (["red"], "miss"),
        (["unseen"], ""),
        (["green", "green"], "verbatim"),
        (["green", "unseen"], "verbatim"),
        # The stack that shipped the wrong colour: the first key was supported
        # and a later one was not, and the whole parenthetical read green.
        (["green", "red"], "miss"),
        (["green", "amber"], "para"),
        (["amber", "red"], "miss"),
        (["red", "green"], "miss"),
    ],
)
def test_a_citation_stack_takes_its_weakest_status(keys, want):
    assert node_call("citeStatusClass", keys, CITES) == want


@pytest.mark.skipif(not NODE, reason="node not installed")
@pytest.mark.parametrize(
    "rec,want",
    [
        (None, "none"),
        ({}, "none"),
        ({"status": "verbatim", "quotes": [{"text": "q"}], "fulltext": True}, "supported"),
        # Read, and nothing supported the claim. This is the review case.
        ({"status": "missing", "quotes": [], "fulltext": True}, "unsupported"),
        # Seen, but there was nothing to read. A library gap, not a writing one.
        ({"status": "missing", "quotes": [], "fulltext": False}, "unreadable"),
    ],
)
def test_a_red_citation_says_which_kind_of_red_it_is(rec, want):
    """"Missing" covers two situations and the panel described a third.

    A key with no fulltext could not be checked either way, which is a library
    gap and what the repair button is for. A key that WAS read and supported
    nothing is a claim to revisit. Both rendered as "no evidence loaded ... the
    underline stays neutral" while the underline was red and the pass had
    finished, which is what sent the author asking why finished passes leave
    citations unloaded.
    """
    assert node_call("evidenceState", rec) == want


def test_the_render_is_never_held_back_from_the_block_being_typed_in():
    """Behaviour 3 protects the caret, and the caret is in the inspector.

    Deferring the DOCUMENT patch for a focused block protected nothing (the
    caret was never in the rendered paragraph) and cost the author the thing
    they were watching for: their own sentence appearing in the manuscript. What
    waits for the blur is the panel rebuild, which really does replace the
    textarea.
    """
    js = VIEWER.read_text(encoding="utf-8")
    patch = js[js.index("function onPatch"):js.index("function flushDeferred")]
    assert "S.deferredPatches" not in js, "the document patch must not be deferred"
    assert "deferredPanels" in patch, "the panel rebuild is what waits for the blur"
    # The apply must not sit behind a focus test.
    apply_line = patch.index("applyBlockHtml(id, blocks[raw])")
    focus_line = patch.index("if (id === S.focusedBlock)")
    assert apply_line < focus_line, "the block is applied before focus is considered"


def test_the_manuscript_column_is_bounded_and_the_inspector_takes_the_surplus():
    """Measured at 1280 before this: a 544px measure inside a 717px column, so
    173px of the window was empty gutter while the panel that wants width was
    clamped at 384."""
    widths = {
        name: value
        for sel, body, _, _ in css_rules_in_context()
        for name, value in declared(body).items()
        if name in ("--doc-w", "--insp-w") and sel == ".app"
    }
    assert "rem" in widths["--doc-w"], "the manuscript column must be bounded"
    assert "1fr" not in widths["--doc-w"], (
        "an unbounded manuscript column is what produced the wide gutters"
    )
    assert "1fr" in widths["--insp-w"], "the inspector must take the surplus"


@pytest.mark.skipif(not NODE, reason="node not installed")
@pytest.mark.parametrize(
    "want,vw,expect",
    [
        (0, 1280, None),          # no opinion: the default arrangement stands
        (-40, 1280, None),
        (200, 1280, 320),         # the 20rem editor floor
        (500, 1280, 500),
        (5000, 1280, 794),        # 62% of the window; the manuscript keeps the rest
        (400, 600, 372),          # a narrow window still leaves prose on screen
    ],
)
def test_the_divide_cannot_be_dragged_somewhere_unrecoverable(want, vw, expect):
    """Both ends matter. Too narrow and the source editor stops being somewhere
    you can write LaTeX; too wide and the manuscript is a strip in its own
    editor, with no way back except knowing about the double-click."""
    assert node_call("clampSplit", want, vw) == expect


def test_the_reader_can_move_the_divide():
    page = render(ms=fixture_blob())
    js = VIEWER.read_text(encoding="utf-8")
    css = STYLES.read_text(encoding="utf-8")
    assert 'id="split"' in page and 'role="separator"' in page
    assert 'aria-orientation="vertical"' in page
    assert "col-resize" in css, "the handle must look draggable"
    assert 'data-split="user"' in css, "a set divide changes which column takes the surplus"
    assert "ArrowLeft" in js and "dblclick" in js, (
        "the divide must be movable from the keyboard and resettable"
    )
    # A width the reader chose is theirs, so it survives the window closing.
    assert "MS_PREF_PREFIX + 'insp'" in js


@pytest.mark.skipif(not NODE, reason="node not installed")
def test_viewer_js_is_syntactically_valid():
    p = subprocess.run([NODE, "--check", str(VIEWER)], capture_output=True, text=True)
    assert p.returncode == 0, p.stderr


@pytest.mark.skipif(not NODE, reason="node not installed")
def test_viewer_js_loads_outside_a_browser():
    # It has to survive being required with no document, or it cannot be tested
    # at all and the static file cannot be linted.
    assert node_call("validateLatex", "plain text") == {"ok": True, "reason": None}


@pytest.mark.skipif(not NODE, reason="node not installed")
@pytest.mark.parametrize(
    "text,reason_fragment",
    [
        ("The effect was \\citep{nishtar2018time}.", None),
        ("Costs rose by 90\\% and \\{this\\} is escaped.", None),
        ("A comment % with an unclosed { brace\nis fine.", None),
        ("It ends with a float here.\\clearpage", None),
        ("\\begin{itemize}\\item a\\end{itemize}", None),
        ("The effect was \\citep{nishtar2018time", "brace"),
        ("The effect was \\citep{", "brace"),
        ("Closed too many}", "brace"),
        ("Half a command \\citep", "command"),
        ("Half a command \\ci", "command"),
        ("A trailing slash \\", "command"),
        ("\\begin{itemize}\\item a", "itemize"),
    ],
)
def test_only_plausible_latex_is_allowed_to_reach_the_file(text, reason_fragment):
    got = node_call("validateLatex", text)
    if reason_fragment is None:
        assert got["ok"] is True, f"held {text!r} for {got['reason']!r}"
    else:
        assert got["ok"] is False, f"let {text!r} through"
        assert reason_fragment in got["reason"].lower()


@pytest.mark.skipif(not NODE, reason="node not installed")
def test_block_ids_from_the_markup_are_normalised_to_the_b_prefix():
    # The marker contract carries the id without its "b-" prefix, so a harvest
    # that forgets to put it back must not silently unaddress every block.
    assert node_call("normId", "b-3f2a91c0de") == "b-3f2a91c0de"
    assert node_call("normId", "3f2a91c0de") == "b-3f2a91c0de"


@pytest.mark.skipif(not NODE, reason="node not installed")
def test_a_draft_is_keyed_by_its_block_and_not_by_the_panel():
    a = node_call("draftKey", "paper", "b-3f2a91c0de")
    b = node_call("draftKey", "paper", "b-91c4ff0011")
    assert a != b
    assert a.startswith("ms:draft:")
    assert "b-3f2a91c0de" in a
    # Same block, different inspector tab or selection: the same key.
    assert node_call("draftKey", "paper", "b-3f2a91c0de") == a


@pytest.mark.skipif(not NODE, reason="node not installed")
@pytest.mark.parametrize(
    "before,ins,expected",
    [
        ("abc", "X", "abXc"),
        ("", "X", "X"),
    ],
)
def test_inline_insertion_splices_at_the_cursor(before, ins, expected):
    at = 2 if before else 0
    assert node_call("spliceAt", before, at, ins) == {
        "text": expected,
        "caret": at + len(ins),
    }


# --------------------------------------------------------------------------
# the protocol both ends have to agree on
# --------------------------------------------------------------------------


def test_every_protocol_message_is_named_in_the_client():
    js = VIEWER.read_text(encoding="utf-8")
    for inbound in ("patch", "state", "chat", "saved", "held"):
        assert f"'{inbound}'" in js, f"inbound {inbound} is unhandled"
    assert "'/ws'" in js or '"/ws"' in js
    assert "type: 'edit'" in js
    assert "type: 'chat'" in js


def test_the_client_defers_the_panel_rebuild_for_the_focused_block():
    """This asked for `deferredPatches` until 2026-07-26, when the deferral was
    narrowed from the whole patch to the panel alone: the document paragraph never
    held the caret, so holding it back only kept the author's own sentence out of
    the render. What must still wait for the blur is the panel.
    See test_the_render_is_never_held_back_from_the_block_being_typed_in."""
    js = VIEWER.read_text(encoding="utf-8")
    assert "deferredPanels" in js
    assert "focusedBlock" in js


def test_drafts_are_persisted_outside_the_page():
    js = VIEWER.read_text(encoding="utf-8")
    assert "localStorage" in js
    css = STYLES.read_text(encoding="utf-8")
    assert ".restored" in css and ".dirtybar" in css


def test_all_three_scrolls_are_captured_and_restored():
    js = VIEWER.read_text(encoding="utf-8")
    for col in ("railEl", "docEl", "inspEl"):
        assert col in js
    assert "captureUI" in js and "restoreUI" in js


@pytest.mark.skipif(not NODE, reason="node not installed")
def test_the_accent_cannot_impersonate_the_computed_violet():
    # The status colours are excluded from the hue rotation, but the wheel
    # could still PICK the computed violet as the accent, and then every
    # button and link occupies the channel that means "this number came from
    # code". Computed is the one status rendered as coloured inline content,
    # so its band is reserved; a pick inside it is steered to the band edge.
    assert node_call("clampHue", 257) != 257
    assert abs(node_call("clampHue", 257) - 257) >= 20
    assert node_call("clampHue", 214) == 214
    assert node_call("clampHue", 40) == 40
    assert node_call("clampHue", 0) == 0


def test_void_blocks_are_collapsed_hydration_proof():
    # styles.css collapses `p[data-mx]:empty`, but hydration inserts the ¶ tag
    # into every block, after which :empty never matches again. The class is
    # the same rule made hydration-proof; losing either half brings back the
    # invisible 11px clickable slivers the 2026-07-22 audit found.
    js = VIEWER.read_text(encoding="utf-8")
    assert "is-void" in js
    css = STYLES.read_text(encoding="utf-8")
    assert ".blk.is-void" in css


def test_the_document_switcher_is_present_and_wired():
    # One directory can serve several documents (blob "main"/"docs"); the
    # toolbar carries the switcher and the viewer hydrates it, hides it when
    # there is nothing to choose, and navigates by query on change.
    html = INDEX.read_text(encoding="utf-8")
    assert 'id="doc-switch"' in html
    js = VIEWER.read_text(encoding="utf-8")
    assert "S.ms.docs" in js
    assert "'?main=' + encodeURIComponent" in js


def test_drafts_are_keyed_by_the_document_not_the_page_title():
    # A draft typed on the appendix must not surface on the paper: the two
    # share a directory, a log, and a page, and differ only in `main`.
    js = VIEWER.read_text(encoding="utf-8")
    assert "S.docKey = String(S.ms.main ||" in js


@pytest.mark.skipif(not NODE, reason="node not installed")
def test_the_document_chat_key_is_the_empty_id():
    # '' is the document chat's key. normId used to prefix it into a block
    # named "b-", which would file every document message under a phantom
    # block.
    assert node_call("normId", "") == ""


def test_the_home_panel_is_the_document_chat():
    js = VIEWER.read_text(encoding="utf-8")
    assert "function renderHome" in js
    assert "Ask for a change anywhere" in js
    # A block chat renders through the same message helper, so the two panels
    # cannot drift apart in how they show a conversation.
    assert js.count("function chatMsgs") == 1


def test_the_evidence_button_and_frames_are_wired():
    html = INDEX.read_text(encoding="utf-8")
    assert 'data-act="evidence:run"' in html
    js = VIEWER.read_text(encoding="utf-8")
    assert "'/evidence'" in js
    # The two frames the run produces must be named in the client, or the
    # page ignores what the server streams.
    assert "case 'evidence':" in js
    assert "case 'cites':" in js


def test_the_hue_picker_is_the_pages_own_popover():
    # The native <input type=color> panel anchored wherever the platform
    # pleased and offered a preset grid the design rejected.
    html = INDEX.read_text(encoding="utf-8")
    assert 'type="color"' not in html
    assert 'id="hue-pop"' in html
    assert 'id="hue-disc"' in html


def test_todos_have_an_input_and_a_frame():
    html = INDEX.read_text(encoding="utf-8")
    assert 'id="todo-add"' in html
    js = VIEWER.read_text(encoding="utf-8")
    assert "case 'todos':" in js
    assert "todo_toggle" in js


def test_selection_has_a_way_out():
    js = VIEWER.read_text(encoding="utf-8")
    assert "function deselect" in js
    assert "'Escape'" in js


def test_the_repair_button_is_wired_and_deliberate():
    # The one click that leads to a write of the Zotero library: its own
    # button, hidden until a run logs misses, never folded into the run.
    html = INDEX.read_text(encoding="utf-8")
    assert 'id="repair-run"' in html and "hidden" in html.split('id="repair-run"')[0].rsplit("<button", 1)[1] + html.split('id="repair-run"')[1][:80]
    js = VIEWER.read_text(encoding="utf-8")
    assert "'/repair'" in js
    assert "showRepair" in js


def test_the_toolbar_outranks_the_columns():
    # Glass gives the toolbar a backdrop-filter, which mints a stacking
    # context; without a raised index the hue popover paints beneath the
    # inspector.
    css = STYLES.read_text(encoding="utf-8")
    toolbar_rule = [b for s, b in css_rules() if s.strip() == ".toolbar"]
    assert toolbar_rule and "z-index" in toolbar_rule[0]


def test_the_skill_menus_are_wired():
    html = INDEX.read_text(encoding="utf-8")
    assert 'id="checks-menu"' in html
    assert 'id="produce-menu"' in html
    assert 'consistency-check' in html
    js = VIEWER.read_text(encoding="utf-8")
    assert "wireSkillMenu" in js
    assert "check: skill" in js


def test_review_findings_carry_their_triage():
    js = VIEWER.read_text(encoding="utf-8")
    assert "finding:fix:" in js
    assert "finding:dismiss:" in js
    css = STYLES.read_text(encoding="utf-8")
    assert ".pin.review" in css


@pytest.mark.skipif(not NODE, reason="node not installed")
def test_chats_read_newest_first_by_time():
    # FULL reverse-chron, by author request: the newest message tops the list
    # even when it is a reply to an older comment. The reply that just landed
    # is the thing being waited for; making the reader scroll for it buries
    # the answer under its own question.
    msgs = [
        {"id": "c-0001", "body": "old comment", "ts": "2026-07-23T10:00:00+00:00"},
        {"id": "c-0002", "body": "newer comment", "ts": "2026-07-23T10:05:00+00:00"},
        {"id": "c-0001#r1", "body": "the reply that just landed",
         "ts": "2026-07-23T10:09:00+00:00"},
    ]
    out = node_call("newestFirst", msgs)
    assert [m["id"] for m in out] == ["c-0001#r1", "c-0002", "c-0001"]
