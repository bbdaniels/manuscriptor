"""The author's display name is decided in one module.

It was written out as a literal in eight places -- three in `app.py`, three in
`chat.py`, one in `drain.py`, one in the CLI's `--author` default -- so changing
what the author is called meant finding all eight and getting all eight right.
The guard below is the point of this file: not that the name is "Ben", but that
nobody except `server/chat.py` spells it at all.

The other half is that changing it must not touch the past. `comments.jsonl` is
append-only and git-tracked, so records already written keep whatever name they
were written with, and both names have to read back and render.
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

from manuscriptor.server import chat, drain, paths

SRC = Path(__file__).resolve().parent.parent / "manuscriptor"

DOC = "\n".join([
    r"\documentclass{article}",
    r"\begin{document}",
    r"\section{Introduction}",
    "A first paragraph that is long enough to be quoted back at itself.",
    "",
    "A second paragraph, also long enough to anchor a comment against.",
    r"\end{document}",
    "",
])


def _sources() -> list[Path]:
    return sorted(p for p in SRC.rglob("*.py") if p.name != "chat.py")


def _strip_comments(text: str) -> str:
    """Drop `#` comments and docstrings, as `test_paths.py` does and for its
    reason: a guard that trips on the prose explaining the rule is a guard only
    silence can satisfy."""
    text = re.sub(r'"""(?:.|\n)*?"""', "", text)
    text = re.sub(r"'''(?:.|\n)*?'''", "", text)
    return "\n".join(line.split("#", 1)[0] for line in text.splitlines())


def test_only_chat_names_the_author():
    """The guard that matters. Eight homes for one string is the bug."""
    offenders = []
    for path in _sources():
        body = _strip_comments(path.read_text(encoding="utf-8"))
        if re.search(r"""(?<![\w.])['"]Ben['"]""", body):
            offenders.append(path.relative_to(SRC.parent))
    assert offenders == [], (
        "the author's display name is spelled outside server/chat.py: "
        f"{[str(p) for p in offenders]}"
    )


def _strip_js_comments(text: str) -> str:
    text = re.sub(r"/\*(?:.|\n)*?\*/", "", text)
    return "\n".join(re.sub(r"//.*$", "", line) for line in text.splitlines())


def test_no_script_on_the_page_spells_either_name():
    """The same rule, in the language the other half of this program is written in.

    The guard above only ever read `*.py`, so `isMine` in `viewer.js` carried a
    hard-coded `'bb'` -- a second home for the legacy name, in the one place a
    Python sweep would never look. Both spellings belong to `server/chat.py`,
    and the page is told what they are: they ride in the payload, and the client
    compares against what it was handed.
    """
    scripts = sorted((SRC / "templates").rglob("*.js"))
    assert scripts, "no page scripts were scanned, so this guard proves nothing"
    offenders = []
    for path in scripts:
        body = _strip_js_comments(path.read_text(encoding="utf-8"))
        for name in (chat.AUTHOR, *chat.LEGACY_AUTHORS):
            if re.search(r"""(?<![\w.])['"]%s['"]""" % re.escape(name), body):
                offenders.append(f"{path.relative_to(SRC.parent)}: {name!r}")
    assert offenders == [], (
        "the author's name is spelled in a page script instead of arriving in "
        f"the payload: {offenders}")


def test_the_page_is_handed_every_name_the_author_answers_to():
    """The payload is how the client learns them, so it carries all of them."""
    assert list(chat.NAMES) == [chat.AUTHOR, *chat.LEGACY_AUTHORS]
    assert "bb" in chat.NAMES, "the legacy name has to keep tinting its own bubbles"


def test_the_name_is_the_one_the_author_asked_for():
    assert chat.AUTHOR == "Ben"


def test_a_comment_typed_on_the_page_is_recorded_under_the_authors_name(tmp_path):
    from manuscriptor.server.app import Session

    (tmp_path / "main.tex").write_text(DOC, encoding="utf-8")
    s = Session(tmp_path)
    bid = [x.id for x in s.build.blocks if x.kind == "paragraph"][0]
    frame = asyncio.run(s.on_chat(bid, "tighten this"))
    rec = chat.read_records(paths.comments(tmp_path))[-1]
    assert rec["author"] == chat.AUTHOR
    # The frame the page is handed must agree with the record on disk, or the
    # bubble says one name and the log holds another.
    assert frame["message"]["who"] == chat.AUTHOR


def test_a_todo_typed_on_the_page_carries_the_same_name(tmp_path):
    from manuscriptor.server.app import Session

    (tmp_path / "main.tex").write_text(DOC, encoding="utf-8")
    s = Session(tmp_path)
    asyncio.run(s.on_todo("chase the referee"))
    rec = chat.read_records(paths.comments(tmp_path))[-1]
    assert rec["kind"] == "todo" and rec["author"] == chat.AUTHOR


def test_a_comment_appended_from_outside_the_page_carries_it_too(tmp_path):
    (tmp_path / "main.tex").write_text(DOC, encoding="utf-8")
    rec = drain.comment(tmp_path, body="a finding")
    assert rec["author"] == chat.AUTHOR


def test_the_cli_default_is_the_authors_name(tmp_path):
    """`manuscriptor comment` with no --author. The default is exercised end to
    end rather than read off the parser, so the record on disk is the assertion."""
    from manuscriptor import cli

    (tmp_path / "main.tex").write_text(DOC, encoding="utf-8")
    assert cli.main(["comment", str(tmp_path), "a finding from a check"]) == 0
    rec = chat.read_records(paths.comments(tmp_path))[-1]
    assert rec["author"] == chat.AUTHOR


def test_a_record_written_under_the_old_name_still_loads(tmp_path):
    """`comments.jsonl` is append-only and git-tracked: 32 records already say
    "bb" and none of them may be touched. They must read back unchanged."""
    log = paths.comments(tmp_path)
    chat.append(log, {"id": "c-0001", "kind": "comment", "block": "b-old",
                      "file": "main.tex", "quote": "a quote", "body": "an old note",
                      "author": "bb", "doc": "main.tex"})
    chat.append(log, {"id": "c-0002", "kind": "comment", "block": "b-new",
                      "file": "main.tex", "quote": "another quote", "body": "a new note",
                      "author": chat.AUTHOR, "doc": "main.tex"})
    chats = {c.id: c for c in chat.read_chats(log)}
    assert chats["c-0001"].author == "bb"
    assert chats["c-0002"].author == chat.AUTHOR


def test_a_mixed_log_renders_both_names(tmp_path):
    """What the page is handed for a log holding both. Neither disappears and
    neither is rewritten; the old bubble simply carries the old label."""
    log = paths.comments(tmp_path)
    chat.append(log, {"id": "c-0001", "kind": "comment", "block": "b-1",
                      "body": "an old note", "author": "bb", "ts": "2026-01-01T00:00:00+00:00"})
    chat.append(log, {"id": "c-0002", "kind": "comment", "block": "b-1",
                      "body": "a new note", "author": chat.AUTHOR,
                      "ts": "2026-08-01T00:00:00+00:00"})
    msgs = chat.by_block(log)["b-1"]
    assert [m["who"] for m in msgs] == ["bb", chat.AUTHOR]


def test_a_record_with_no_author_at_all_reads_as_the_author(tmp_path):
    """The oldest records predate the field. They are the author's own."""
    log = paths.comments(tmp_path)
    chat.append(log, {"id": "c-0001", "kind": "comment", "block": "b-1",
                      "body": "from before the field existed"})
    assert chat.read_chats(log)[0].author == chat.AUTHOR
