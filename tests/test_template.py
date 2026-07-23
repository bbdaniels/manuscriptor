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


def test_the_client_defers_a_patch_to_the_focused_block():
    js = VIEWER.read_text(encoding="utf-8")
    assert "deferredPatches" in js
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
