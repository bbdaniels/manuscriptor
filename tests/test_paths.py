"""The on-disk layout is decided in one module.

The reason this file exists: `root / "build" / "manuscriptor"` was written out as
a literal in fourteen places, so the layout was unchangeable in practice. A guard
that only checked `paths.py` returned the right answer would not have caught
that, because every one of the fourteen also returned the right answer. What
matters is that nobody else spells it at all.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from manuscriptor.server import paths

SRC = Path(__file__).resolve().parent.parent / "manuscriptor"


def _sources() -> list[Path]:
    return sorted(p for p in SRC.rglob("*.py") if p.name != "paths.py")


def _strip_comments(text: str) -> str:
    """Drop `#` comments and docstrings.

    A guard that trips on the comment explaining why a literal is absent is a
    guard only silence can satisfy. That mistake was made once already, in the
    `representedFilename` check on 2026-07-26.
    """
    text = re.sub(r'"""(?:.|\n)*?"""', "", text)
    text = re.sub(r"'''(?:.|\n)*?'''", "", text)
    return "\n".join(line.split("#", 1)[0] for line in text.splitlines())


def test_only_paths_names_the_home_directory():
    offenders = []
    for path in _sources():
        body = _strip_comments(path.read_text(encoding="utf-8"))
        if '".manuscriptor"' in body or "'.manuscriptor'" in body:
            offenders.append(path.relative_to(SRC.parent))
    assert offenders == [], (
        "the hidden directory is named outside server/paths.py: "
        f"{[str(p) for p in offenders]}"
    )


def _squash(body: str) -> str:
    """Strip everything that merely DECORATES a path literal.

    The first version of this guard was `re.compile(r'"build"\\s*/\\s*"manuscriptor"')`
    -- double quotes only. `app.py` spelled the same path with single quotes, so
    the guard passed for months over the one surviving offender, and the drain's
    live feed was watched at a path nothing had written since 2026-07-27. A guard
    that a change of quote character defeats is a guard that proves nothing.

    So: drop quote characters and their string prefixes, `Path(`/`joinpath(`, the
    closing parens, and all whitespace. Every way Python can spell the two
    segments -- `"build" / "manuscriptor"`, `'build' / 'manuscriptor'`,
    `f"build" / "manuscriptor"`, `Path("build") / "manuscriptor"`,
    `"build/manuscriptor"`, `joinpath("build", "manuscriptor")` -- collapses to
    one of two strings.
    """
    # The string-prefix branch needs the boundary: without it the trailing `r`
    # of `manuscriptor"` reads as an `r"` prefix and the guard eats the very
    # word it is looking for.
    return re.sub(
        r"""(?<![A-Za-z0-9_])[rbufRBUF]{1,2}['"]|['"]|Path\(|joinpath\(|\)|\s+""",
        "", body)


LEGACY_SPELLINGS = ("build/manuscriptor", "build,manuscriptor")


def test_nobody_still_spells_the_old_build_directory():
    """The literal the whole refactor existed to remove, in any quoting."""
    offenders = []
    for path in _sources():
        body = _squash(_strip_comments(path.read_text(encoding="utf-8")))
        if any(s in body for s in LEGACY_SPELLINGS):
            offenders.append(path.relative_to(SRC.parent))
    assert offenders == [], (
        "build/manuscriptor is still spelled outside server/paths.py: "
        f"{[str(p) for p in offenders]}"
    )


@pytest.mark.parametrize("spelling", [
    'root / "build" / "manuscriptor"',
    "root / 'build' / 'manuscriptor'",
    'root / f"build" / "manuscriptor"',
    'Path("build") / "manuscriptor"',
    'root / "build/manuscriptor"',
    'root.joinpath("build", "manuscriptor")',
    'root  /  "build"  /  "manuscriptor"',
])
def test_the_guard_is_not_defeated_by_how_the_path_is_quoted(spelling):
    """Watch the guard catch each form, so it can never again pass on a typo."""
    assert any(s in _squash(spelling) for s in LEGACY_SPELLINGS), spelling


@pytest.mark.parametrize("innocent", [
    'IGNORED_DIRS = {".git", "build", "__pycache__"}',
    '_inside(parent, root / "build") or name.startswith(".")',
    'SKIP = (".git", ".build", "build", "dist", "output")',
    'out = root / "manuscriptor"',
])
def test_the_guard_does_not_trip_on_an_unrelated_build_directory(innocent):
    assert not any(s in _squash(innocent) for s in LEGACY_SPELLINGS), innocent


# ------------------------------------------------------------------- the tiers


def test_the_tiers_are_separate_directories(tmp_path):
    paths.ensure(tmp_path)
    assert paths.comments(tmp_path).parent == paths.home(tmp_path)
    assert paths.drafts(tmp_path).parent == paths.home(tmp_path)
    assert paths.cache(tmp_path).is_dir()
    assert paths.agent_dir(tmp_path).is_dir()
    # The point of the split: nothing durable lives under the removable tier.
    assert paths.cache(tmp_path) not in paths.drafts(tmp_path).parents
    assert paths.cache(tmp_path) not in paths.comments(tmp_path).parents
    assert paths.cache(tmp_path) not in paths.agent_dir(tmp_path).parents


def test_compile_output_is_regenerable(tmp_path):
    """LaTeX artifacts are cache, so `clean` takes them and nothing else does."""
    assert paths.cache(tmp_path) in paths.compile_dir(tmp_path).parents


def test_the_ignore_rule_hides_everything_but_the_record(tmp_path):
    paths.ensure(tmp_path)
    rule = (paths.home(tmp_path) / ".gitignore").read_text(encoding="utf-8")
    assert rule.splitlines() == ["*", "!comments.jsonl"]


def test_serving_a_paper_that_has_drawn_no_comments_leaves_git_alone(tmp_path):
    """The invariant, checked against real git rather than by reading the rule.

    An earlier version of this rule re-included `.gitignore`, so the directory
    always held one non-ignored file and merely opening a manuscript put
    `?? .manuscriptor/` in the author's `git status`.
    """
    import subprocess

    def git(*args) -> str:
        return subprocess.run(["git", *args], cwd=tmp_path, capture_output=True,
                              text=True).stdout

    git("init", "-q")
    (tmp_path / "main.tex").write_text("x", encoding="utf-8")
    git("add", "main.tex")
    git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "in")

    paths.ensure(tmp_path)
    (paths.cache(tmp_path) / "manuscript.html").write_text("<p/>", encoding="utf-8")
    paths.drafts(tmp_path).write_text("{}", encoding="utf-8")
    assert git("status", "--porcelain") == ""

    # ...and the record is the one thing that does surface, for committing.
    paths.comments(tmp_path).write_text("{}\n", encoding="utf-8")
    # `-uall`, because git collapses an untracked directory to its name and
    # would hide which of the files inside it is the one being offered.
    assert git("status", "--porcelain", "-uall").split() == [
        "??", ".manuscriptor/comments.jsonl"]


def test_an_authors_own_ignore_rule_is_left_alone(tmp_path):
    paths.ensure(tmp_path)
    marker = paths.home(tmp_path) / ".gitignore"
    marker.write_text("*\n!mine.txt\n", encoding="utf-8")
    paths.ensure(tmp_path)
    assert marker.read_text(encoding="utf-8") == "*\n!mine.txt\n"


def test_ensure_is_safe_to_repeat(tmp_path):
    first = paths.ensure(tmp_path)
    (paths.cache(tmp_path) / "rendered.html").write_text("x", encoding="utf-8")
    second = paths.ensure(tmp_path)
    assert first == second
    assert (paths.cache(tmp_path) / "rendered.html").exists()


# ------------------------------------------------------- what `clean` may take


@pytest.mark.parametrize("factory, expected", [
    (paths.cache, True),
    (paths.home, False),
    (paths.agent_dir, False),
    (paths.compile_dir, False),
])
def test_only_the_cache_directory_answers_to_clean(tmp_path, factory, expected):
    """Handing `clean` the directory above `cache/` destroyed drafts.json."""
    assert paths.is_cache(factory(tmp_path)) is expected


def test_a_lookalike_elsewhere_is_not_a_cache_directory(tmp_path):
    decoy = tmp_path / "build" / "cache"
    decoy.mkdir(parents=True)
    assert paths.is_cache(decoy) is False


# --------------------------------------------------- `clean`, the live hazard
#
# Before 2026-07-27 this command was `shutil.rmtree` on whatever path it was
# handed, and the path it was handed held the author's unsaved text.


def _stocked(tmp_path):
    """A manuscript with something in every tier."""
    paths.ensure(tmp_path)
    (paths.cache(tmp_path) / "manuscript.html").write_text("<p/>", encoding="utf-8")
    paths.compile_dir(tmp_path).mkdir(parents=True, exist_ok=True)
    (paths.compile_dir(tmp_path) / "main.aux").write_text("aux", encoding="utf-8")
    paths.drafts(tmp_path).write_text('{"drafts": [{"block": "b-1", "text": "unsaved"}]}',
                                      encoding="utf-8")
    paths.comments(tmp_path).write_text('{"id": "c-0001"}\n', encoding="utf-8")
    (paths.agent_dir(tmp_path) / "agent.log").write_text("ran", encoding="utf-8")


def test_clean_takes_the_render_and_leaves_the_unsaved_text(tmp_path):
    from manuscriptor import cli

    _stocked(tmp_path)
    assert cli.main(["clean", str(tmp_path)]) == 0

    assert not paths.cache(tmp_path).exists(), "the regenerable tier should be gone"
    assert paths.drafts(tmp_path).read_text(encoding="utf-8").count("unsaved") == 1
    assert paths.comments(tmp_path).exists()
    assert (paths.agent_dir(tmp_path) / "agent.log").exists()


def test_clean_refuses_a_directory_that_is_not_a_cache(tmp_path):
    """Handed the hidden directory itself, the old command took everything."""
    from manuscriptor import cli

    _stocked(tmp_path)
    stray = tmp_path / "somewhere"
    stray.mkdir()
    (stray / "keep.txt").write_text("mine", encoding="utf-8")

    with pytest.raises(SystemExit):
        cli.main(["clean", str(paths.home(tmp_path))])
    assert paths.drafts(tmp_path).exists()
    assert (stray / "keep.txt").exists()


def test_clean_accepts_the_cache_directory_by_its_own_path(tmp_path):
    from manuscriptor import cli

    _stocked(tmp_path)
    assert cli.main(["clean", str(paths.cache(tmp_path))]) == 0
    assert not paths.cache(tmp_path).exists()
    assert paths.drafts(tmp_path).exists()
