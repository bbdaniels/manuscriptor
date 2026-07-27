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
"""
from __future__ import annotations

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


def home(manuscript_dir: Path | str) -> Path:
    """The hidden directory holding everything Manuscriptor owns."""
    return Path(manuscript_dir).resolve() / HOME


def cache(manuscript_dir: Path | str) -> Path:
    """Regenerable render output. The only tier `clean` may remove."""
    return home(manuscript_dir) / CACHE_NAME


def agent_dir(manuscript_dir: Path | str) -> Path:
    """The drain's own logs and live feed. Durable, private, out of git."""
    return home(manuscript_dir) / AGENT_NAME


def compile_dir(manuscript_dir: Path | str) -> Path:
    """Where LaTeX writes its `.aux`, `.log`, `.bbl` and its PDF.

    Under `cache/` because every byte of it comes back from another compile,
    and inside the hidden directory because these are the files that used to
    litter the manuscript folder beside the `.tex` they came from.
    """
    return cache(manuscript_dir) / COMPILE_NAME


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


def ensure(manuscript_dir: Path | str) -> Path:
    """Create the layout for a manuscript and return its hidden directory.

    Safe to call on every build. Making the directories is what lets the tiers
    be separate rather than a convention nobody enforces.
    """
    h = home(manuscript_dir)
    for d in (h, h / CACHE_NAME, h / AGENT_NAME):
        d.mkdir(parents=True, exist_ok=True)
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
