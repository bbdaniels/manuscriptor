"""Where Manuscriptor keeps its files, decided once.

Until this module existed the answer was `root / "build" / "manuscriptor"`,
written out as a literal in fourteen places across five modules. Nothing
answered the question "where does Manuscriptor keep its files", so the layout
could not be changed anywhere without being changed everywhere, and two of those
fourteen had already drifted into meaning slightly different things. One module
owns it now, and a test asserts no second spelling exists.

**Three tiers, because they have different lifetimes and only one of them is
shared.** The old build directory mixed all three, which is how `manuscriptor
clean` came to be a command that deleted the author's unsaved text:

`comments.jsonl` is the review record. It is the durable output of the whole
editor, a coauthor needs to see it, and it is the one file here that belongs in
the manuscript's repository.

`drafts.json` and the agent's logs are durable but private. They are per machine
and per session, they say nothing a coauthor wants, and drafts in particular are
unsaved text that no rebuild can reconstruct. Out of git, never deleted by us.

Everything under `cache/` is regenerable from the `.tex` tree by rebuilding.
That is what `clean` may remove, and it is the only thing it may remove.

**The directory is hidden and it writes its own `.gitignore`.** It sits inside
the manuscript directory, which is nearly always a git working tree the author
cares about, so serving a paper must never make `git status` grow. The ignore
file re-includes `comments.jsonl` alone, which is also what makes the first
`git add` of a new manuscript pick up the record and nothing else.

**A read-only serve gets a different answer, and it is decided here.** `serve
--read-only` promises that nothing reaches the author's filesystem, and until
2026-08-05 that promise stopped at the write handlers: `build()` could not see
the flag, so it created `.manuscriptor/` and rendered into it on every
read-only serve, staged rasters into it, cached the value manifest in it, and a
compile dropped its `.aux` and `.log` there too. Opening somebody's paper to
READ it made a directory inside their working tree.

So `home()` takes the flag and answers with a scratch directory under the
system temp, and everything derived from it -- the cache, the compile directory
-- follows without having to know why. That is the whole redirect, in the one
module that owns the question; the alternative was path arithmetic in `build`,
`compile` and the asset route, which is three places that can disagree.

Only the tiers Manuscriptor WRITES redirect. `comments()`, `drafts()` and
`agent_dir()` take no flag at all, because a read-only serve still has to SHOW
the review record, the unsaved drafts and the drain's feed -- redirecting those
would open a paper on an empty rail over a queue full of comments. The writes to
them are refused at the handlers, which is where a refusal can say so.
"""
from __future__ import annotations

import atexit
import shutil
import tempfile
from pathlib import Path

HOME = ".manuscriptor"

COMMENTS_NAME = "comments.jsonl"
DRAFTS_NAME = "drafts.json"
CACHE_NAME = "cache"
AGENT_NAME = "agent"
COMPILE_NAME = "compile"

# `*` hides the whole directory and the rule hides itself, so serving a paper
# that has drawn no comments leaves `git status` empty. `comments.jsonl` is the
# one exception, and it appears for commit exactly when the author has something
# a coauthor should see. The rule is not tracked because it does not need to be:
# a clone that serves the manuscript writes it again from `ensure`.
GITIGNORE = "*\n!comments.jsonl\n"

# The pre-2026-07-27 layout, kept here only so `migrate` can recognise it.
LEGACY_BUILD = ("build", "manuscriptor")


# Scratch homes for read-only serves, one per manuscript per process. Made
# with `mkdtemp` rather than a name derived from the manuscript path, so two
# servers reading the same paper cannot render over each other, and removed at
# exit because nothing here is worth keeping: every byte of it is regenerable
# and the tiers that are not regenerable never redirect.
SCRATCH_PREFIX = "manuscriptor-read-only-"
_scratch: dict[Path, Path] = {}


def scratch_home(manuscript_dir: Path | str) -> Path:
    """The temp directory a read-only serve renders into, made on first ask.

    Stable for the life of the process: the build writes its rasters here and
    the asset route serves them from here, and a fresh directory per call would
    give those two different answers.
    """
    key = Path(manuscript_dir).resolve()
    known = _scratch.get(key)
    if known is not None:
        return known
    made = Path(tempfile.mkdtemp(prefix=SCRATCH_PREFIX)).resolve()
    _scratch[key] = made
    return made


def drop_scratch() -> None:
    """Remove every scratch home this process made. Registered at exit."""
    for path in list(_scratch.values()):
        shutil.rmtree(path, ignore_errors=True)
    _scratch.clear()


atexit.register(drop_scratch)


def home(manuscript_dir: Path | str, *, read_only: bool = False) -> Path:
    """The directory holding everything Manuscriptor owns for this manuscript.

    Hidden inside the manuscript directory normally; under the system temp when
    the serve is read-only, because a read-only serve may not create it.
    """
    if read_only:
        return scratch_home(manuscript_dir)
    return Path(manuscript_dir).resolve() / HOME


def cache(manuscript_dir: Path | str, *, read_only: bool = False) -> Path:
    """Regenerable render output. The only tier `clean` may remove."""
    return home(manuscript_dir, read_only=read_only) / CACHE_NAME


def agent_dir(manuscript_dir: Path | str) -> Path:
    """The drain's own logs and live feed. Durable, private, out of git."""
    return home(manuscript_dir) / AGENT_NAME


def compile_dir(manuscript_dir: Path | str, *, read_only: bool = False) -> Path:
    """Where LaTeX writes its `.aux`, `.log`, `.bbl` and its PDF.

    Under `cache/` because every byte of it comes back from another compile,
    and inside the hidden directory because these are the files that used to
    litter the manuscript folder beside the `.tex` they came from.
    """
    return cache(manuscript_dir, read_only=read_only) / COMPILE_NAME


def drain_lock(manuscript_dir: Path | str) -> Path:
    """The claim on this manuscript's comment queue. One drain at a time.

    Beside the drain's other private files, and NOT in a temp directory: a
    tempdir sweep can remove a lock file while it is held, after which the next
    process locks a fresh inode and drains a queue somebody else already has.
    """
    return agent_dir(manuscript_dir) / "drain.lock"


def comments(manuscript_dir: Path | str) -> Path:
    """The append-only review record. Tracked in the manuscript's repository."""
    return home(manuscript_dir) / COMMENTS_NAME


def drafts(manuscript_dir: Path | str) -> Path:
    """Unsaved text. Durable, private, and never deleted by `clean`."""
    return home(manuscript_dir) / DRAFTS_NAME


def legacy_build(manuscript_dir: Path | str) -> Path:
    """`<manuscript>/build/manuscriptor`, the layout before 2026-07-27."""
    return Path(manuscript_dir).resolve().joinpath(*LEGACY_BUILD)


def legacy_comments(manuscript_dir: Path | str) -> Path:
    """`<manuscript>/comments.jsonl`, where the record used to sit."""
    return Path(manuscript_dir).resolve() / COMMENTS_NAME


def keep_out_of_git(where: Path | str | None = None) -> None:
    """Write the ignore rule, unless the author has written their own.

    Called on the hidden directory. It is left alone once it exists, because an
    author who has edited it meant to.
    """
    marker = Path(where) / ".gitignore"
    if marker.exists():
        return
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(GITIGNORE, encoding="utf-8")
    except OSError:
        pass


def ensure(manuscript_dir: Path | str, *, read_only: bool = False) -> Path:
    """Create the layout for a manuscript and return its home directory.

    Safe to call on every build. Making the directories is what lets the tiers
    be separate rather than a convention nobody enforces.

    Under `read_only` this makes THE SAME LAYOUT somewhere else: `home()` has
    already answered with a scratch directory under the system temp, so every
    tier is created there and nothing at all is created in the manuscript. The
    only difference in what gets made is the `.gitignore`, which is skipped
    because there is no repository under the system temp to keep a directory
    out of.

    `agent/` is made too, and this docstring used to claim it was not. What
    keeps a read-only serve out of the author's `.manuscriptor/agent/` is
    `home()` pointing elsewhere, plus `watch.watch_file(create=False)` for the
    feed the panel watches -- not an omission here. An empty `agent/` in a
    scratch directory nothing drains costs nothing and is deleted with it.
    """
    h = home(manuscript_dir, read_only=read_only)
    for d in (h, h / CACHE_NAME, h / AGENT_NAME):
        d.mkdir(parents=True, exist_ok=True)
    if not read_only:
        keep_out_of_git(h)
    return h


def is_cache(path: Path | str) -> bool:
    """Whether a path is a `cache/` directory this module would have made.

    `clean` asks before removing anything. The old command took a path and
    deleted it, which meant handing it the directory above this one destroyed
    the drafts store beside it.
    """
    p = Path(path).resolve()
    return p.name == CACHE_NAME and p.parent.name == HOME
