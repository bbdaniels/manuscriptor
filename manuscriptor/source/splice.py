"""M4 — write an edited block back into its source file.

Reconciliation is a splice, never a diff. A human edit replaces block 212's
byte range and a Claude edit replaces block 340's, and the two cannot interact.
The only genuine conflict is both writing the same block at once, which is one
lock on one block.

Two guards are load bearing. The write is atomic, so a crash mid-write cannot
truncate a manuscript. And the target range must still hold the WHOLE of the
block -- its bytes and the boundaries its cut fell on -- because a stale splice
would otherwise overwrite whatever had already replaced them. Hashing the bytes
alone does not do that: an id says what a block's bytes are and not where it
ends, so a paragraph that has since GROWN still contains the old block's text,
still hashes back to the old id, and a splice over it leaves the growth standing
behind the new text.

Line numbers are not trusted to find the range. A block's id is content derived
precisely so that an edit above it does not move it, and splice honours that: it
locates the block by its own source text and uses the recorded line only to pick
between identical candidates.
"""
from __future__ import annotations

import difflib
import fcntl
import hashlib
import os
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path

from manuscriptor.source.blocks import base_id, block_id


class BlockLocked(Exception):
    """Raised when a block is already being written by the other party."""


class StaleBlock(Exception):
    """Raised when the bytes at a block's range are no longer the block's."""


class NotEditable(Exception):
    """Raised on an attempt to hand-edit a machine-generated block."""


_LOCKS: dict[str, str] = {}
_GUARD = threading.Lock()

# One lock per file, held across read-locate-write.
#
# splice reads the whole file, replaces a byte range, and writes the whole file
# back. Two writers on two different paragraphs of one file lose an edit: A
# reads, B reads, A writes, B writes, and A's work is gone because B's copy
# predates it. Measured before this existed: eight concurrent splices, two
# survivors.
#
# Serializing the writers would fix it and throw away the parallelism. The
# critical section is a file read and a file write, microseconds, so agents can
# think concurrently and only their writes take turns. The second re-reads after
# the first wrote and finds its own block by content, which is how _locate
# already works.
_FILE_LOCKS: dict[str, threading.Lock] = {}
_FILE_LOCKS_GUARD = threading.Lock()


def _file_lock(path: Path) -> threading.Lock:
    key = str(path)
    with _FILE_LOCKS_GUARD:
        lock = _FILE_LOCKS.get(key)
        if lock is None:
            lock = _FILE_LOCKS[key] = threading.Lock()
        return lock


def splice(block, new_source: str, *, root, holder: str | None = None,
           following=()) -> None:
    """Replace `block`'s byte range in `block.file`, and what it still carries.

    `root` is the manuscript directory; a block claiming to live outside it is
    refused rather than followed. `holder` names the writer, and only matters
    when the block is locked.

    `following` is the blocks after this one in the same file, in document
    order, and it is what an EDITOR BOX passes: see `_carried` for why a save
    sometimes owns more bytes than its block. A caller that hands one block and
    nothing else -- every agent write does -- replaces exactly that block, which
    is the invariant the whole design rests on and is not touched here.
    """
    if not block.editable:
        raise NotEditable(
            f"{block.file} is not the root manuscript file, so block {block.id} is "
            "treated as machine generated and refuses hand edits. Change the code "
            "that writes it instead; editing it here would hardcode a result."
        )

    root_dir = Path(root).resolve()
    path = Path(block.file)
    if not path.is_absolute():
        path = root_dir / path
    path = path.resolve()
    if not path.is_relative_to(root_dir):
        raise ValueError(f"{path} is outside the manuscript root {root_dir}")

    with _GUARD:
        owner = _LOCKS.get(block.id)
    if owner is not None and owner != holder:
        raise BlockLocked(f"block {block.id} is held by {owner!r}")

    # Two locks, because there are two kinds of contention. The threading lock
    # covers writers inside this process, a parallel drain being the obvious
    # one. The advisory file lock covers writers in OTHER processes: the server
    # splicing the author's edit while a Claude session splices its own is two
    # processes, and a threading lock is blind to that.
    #
    # It cannot cover an editor that does not take it. Claude's ordinary file
    # edits do not, so that pairing is still protected only by the staleness
    # check below and by the block lock the margin shows.
    with _file_lock(path), _across_processes(path):
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise StaleBlock(f"cannot read {path}: {exc}") from exc

        text = raw.decode("utf-8")
        crlf = "\r\n" in text
        if crlf:
            text = text.replace("\r\n", "\n")

        at = _locate(text, block)
        if at is None:
            raise StaleBlock(
                f"block {block.id} no longer matches the bytes in {path}; "
                "something else rewrote it first"
            )

        end = at + len(block.source_text)
        end += _carried(text, end, new_source, following)
        updated = text[:at] + new_source + text[end:]
        if crlf:
            updated = updated.replace("\n", "\r\n")
        _atomic_write(path, updated.encode("utf-8"))


def lock(block_id: str, holder: str) -> None:
    with _GUARD:
        owner = _LOCKS.get(block_id)
        if owner is not None and owner != holder:
            raise BlockLocked(f"block {block_id} is held by {owner!r}")
        _LOCKS[block_id] = holder


def unlock(block_id: str, holder: str) -> None:
    with _GUARD:
        owner = _LOCKS.get(block_id)
        if owner is not None and owner != holder:
            raise BlockLocked(f"block {block_id} is held by {owner!r}, not {holder!r}")
        _LOCKS.pop(block_id, None)


def holder_of(block_id: str) -> str | None:
    with _GUARD:
        return _LOCKS.get(block_id)


# ----------------------------------------------------------------- internals


_LOCK_DIR = Path(tempfile.gettempdir()) / "manuscriptor-locks"


@contextmanager
def _across_processes(path: Path):
    """An advisory lock, held on a sidecar outside the manuscript.

    Not beside the file it guards. Lock files cannot be safely deleted on
    release, so one kept next to the manuscript would accumulate there and show
    up in the author's working tree. Serving a paper must never leave litter in
    it.
    """
    try:
        _LOCK_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        yield
        return
    key = hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:16]
    guard = _LOCK_DIR / f"{key}.lock"
    fh = None
    try:
        fh = open(guard, "a+")
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        yield
    except OSError:
        yield          # a read-only directory must not stop an edit landing
    finally:
        if fh is not None:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            finally:
                fh.close()


_CARRY_RATIO = 0.9
_CARRY_MIN = 60


def _carried(text: str, end: int, new_source: str, following) -> int:
    """How many bytes past the block this save also replaces. Usually none.

    A BLOCK IS THE UNIT OF A WRITE. AN EDITOR BOX IS NOT, and the difference is
    what quadruplicated a passage of qutub-ayush on 2026-08-13. The author typed
    four paragraphs into one paragraph's box and then formatted them, a blank
    line and a save at a time. Every save split his block in two. The page
    renames the box onto the first half -- correctly, and it may not take his
    text away from him while his cursor is in it -- so from the second save
    onward the box held a passage the FILE was keeping as several blocks. Each
    save wrote all of it over the first of them and left the others standing
    below, one duplicate per save, each with one blank line more than the last.
    Four copies, in the manuscript, with the server answering `saved` every
    time.

    Refusing the save was the alternative and it is not one: the author would
    have been locked out of his own paragraph with no way to see why, and his
    formatting is not a mistake. So the save keeps what it always kept -- his
    text, written once -- and the range grows to cover the blocks that text is
    still carrying.

    Carrying is claimed only for what the save can be SEEN to still hold, and
    only forward, block by block, stopping at the first one it does not:

      * the blocks must be next in the file, with nothing but the separator
        between them, or this is not the passage's own tail at all;
      * `new_source` must END with them, because that is what "the box still
        holds this paragraph" means. Exactly, when he only added blank lines;
      * or nearly, when he was also typing in the tail, which is the same
        paragraph edited rather than a new one. A high ratio and a length floor,
        because a short block is mostly separator and a `\\clearpage` he typed
        himself must not eat the `\\clearpage` already there.

    A paragraph he genuinely ADDED matches nothing below it, carries nothing,
    and is inserted -- which is the other half of the rule and the reason this
    compares rather than counts.
    """
    grew = 0
    cursor = end
    for blk in following or ():
        src = getattr(blk, "source_text", "")
        if not src:
            break
        at = text.find(src, cursor)
        if at < 0 or text[cursor:at].strip():
            break                          # not the next thing in the file
        cursor = at + len(src)
        # Nothing longer than the save itself can be inside the save. This is
        # also what ends the walk on a file whose every block is contiguous with
        # the last, which is most files.
        if cursor - end > len(new_source):
            break
        # The run accumulates, so the question is asked of the whole of it: a
        # save carrying two paragraphs ends on the SECOND, and asking whether it
        # ends on the first answers no. A run that does not match is not the end
        # of the walk for that reason -- only a longer one can match.
        #
        # Asked in two parts, and the split is what keeps the fuzzy half honest.
        # Everything before the last block must be there EXACTLY, which is what
        # says the save reaches this far and pins where its last paragraph
        # begins; the last block alone may have been typed in since. Compared as
        # one string instead, a run of five identical paragraphs and one wrong
        # one still scores above any ratio worth having, and the save eats the
        # section heading below it.
        lead, last = text[end:at], src
        cut = new_source.rfind(lead)
        if cut < 0:
            continue
        cand = new_source[cut + len(lead):]
        if cand == last or (
                len(last) >= _CARRY_MIN
                and difflib.SequenceMatcher(None, cand, last, autojunk=False)
                .ratio() >= _CARRY_RATIO):
            grew = cursor - end
    return grew


def _locate(text: str, block) -> int | None:
    """Find the block's current byte offset, or None if it is gone.

    Candidates are exact occurrences of the block's source text, each confirmed
    to be a WHOLE block and not part of one. When a manuscript genuinely repeats
    a paragraph, the candidate nearest the recorded line wins.

    Re-deriving the id from the bytes found is kept, and it does catch a block
    record whose id and text disagree. It is NOT a staleness check and never
    was: those bytes are the block's own source text and its id is the hash of
    that text, so for anything `segment` produced the comparison is `x == x`. It
    read as the module's central safeguard and could not fail.

    An id says what a block's bytes ARE. It cannot say where the block ENDS, and
    that is the fact staleness destroys. A save that appends to a paragraph
    leaves the previous version of it in the file as a PREFIX of the new one, so
    the stale text is still found, still hashes back, and the splice writes over
    the prefix and leaves the rest of the paragraph standing behind the new
    text. That is how ordinary typing put `... ALPHA. BETA BETA.` into a
    manuscript on 2026-07-28.

    So the block carries the boundaries of its own cut (`src_before`,
    `src_after`) and they are what is checked here. Neither is content of any
    block: they are the separator that ended this one, so rewriting the
    paragraph above or below leaves them alone and a block still splices after
    its neighbours move -- which the content-derived id exists to guarantee.
    """
    src = block.source_text
    if not src:
        return None
    want = base_id(block.id)
    # A block from somewhere other than `segment` records no boundaries, and an
    # unknown boundary is not a failed one.
    before = getattr(block, "src_before", None)
    after = getattr(block, "src_after", None)

    hits: list[int] = []
    at = text.find(src)
    while at >= 0:
        end = at + len(src)
        pre = text[at - 1] if at > 0 else ""
        post = text[end] if end < len(text) else ""
        if ((before is None or pre == before)
                and (after is None or post == after)
                and block_id(text[at:end]) == want):
            hits.append(at)
        at = text.find(src, at + 1)
    if not hits:
        return None

    starts = [0]
    for i, c in enumerate(text):
        if c == "\n":
            starts.append(i + 1)
    idx = min(max(block.line_start - 1, 0), len(starts) - 1)
    expected = starts[idx]
    return min(hits, key=lambda h: (abs(h - expected), h))


def _atomic_write(path: Path, payload: bytes) -> None:
    """Write a sibling temp file, flush it to disk, then rename over the target.

    Same directory, so the rename is atomic on one filesystem; a crash leaves
    either the old file or the new one, never half a manuscript.
    """
    # The pid alone is not unique: two threads in one process collide on the
    # same temp name and one deletes the other's file mid-write.
    tmp = path.with_name(f".{path.name}.mxtmp{os.getpid()}.{threading.get_ident()}")
    try:
        with open(tmp, "wb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
