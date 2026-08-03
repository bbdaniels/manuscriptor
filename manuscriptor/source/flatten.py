"""M1 — resolve \\input and \\include into one buffer, keeping a source map.

Pandoc follows neither `\\input` nor `\\include`; it drops them silently with a
zero exit code, and sometimes emits them as literal text instead. Measured
across five manuscripts, four produced zero tables and three produced zero
figures because of this. qutub-india has 182 include directives and pandoc
turned 68 of them into visible LaTeX. So we flatten ourselves.

Because a flattening pass already knows which byte of its output came from
which line of which file, the source map is free. That map is what every
downstream feature depends on: block anchors, margin comments, and byte-range
write-back all resolve through it.

Known limit: an include inside a `verbatim` or `lstlisting` environment is
still followed, where LaTeX would print it literally. No manuscript in the
corpus does this, and the failure is visible rather than silent.
"""
from __future__ import annotations

import bisect
import re
from dataclasses import dataclass
from pathlib import Path

_INCLUDE_RE = re.compile(r"\\(input|include)\s*\{([^}]*)\}")


def command_re(name: str) -> re.Pattern[str]:
    """`\\name[optional]{`, matched up to but not including the `{`.

    One spelling of this, because two callers ask the same question about the
    same commands from opposite ends: `blocks._preamble_title` cuts `\\title`'s
    BYTES into a block, and `pandoc._command_spans` reads `\\title`'s and
    `\\author`'s TEXT out for the rendered heading and byline. When they
    disagree about what counts as a `\\title` the block and the heading stop
    describing the same thing, and the marker handoff between them silently
    finds nothing to hand over.

    Both halves of the pattern are load-bearing and both were once missing. The
    optional argument is why no byline had ever rendered on any manuscript:
    every `wlscirep` paper writes `\\author[1]{Benjamin Daniels}` and a bare `{`
    lookahead never matched it. The trailing word boundary is why estonia-ecm's
    `\\titlespacing{\\section}{0pt}{6pt}{6pt}`, declared nineteen lines above its
    real `\\title`, is not mistaken for the title of the paper.
    """
    return re.compile(r"\\" + name + r"(?![a-zA-Z@])\s*(?:\[[^\]]*\])?\s*(?=\{)")


@dataclass(frozen=True)
class Segment:
    """One contiguous run of the flattened buffer, traced to its origin file."""

    flat_start: int
    flat_end: int
    file: Path
    line_start: int
    line_end: int


@dataclass(frozen=True)
class Directive:
    """One `\\input` or `\\include`, and what its target actually contributed.

    `contributed` is the number of characters the target added to the flattened
    buffer, counted by the walk itself rather than by a second reading of the
    file. That is the only honest measure of "did this fragment say anything":
    a zero-byte file, a file holding one comment, and a file that inputs two
    other empty files all contribute nothing, and all three drop a number out
    of a sentence without troubling the compiler.
    """

    kind: str                    # "input" or "include"
    target: str                  # the text inside the braces, as written
    file: Path                   # the file the directive appears in
    line: int                    # 1-indexed line within that file
    resolved: Path | None        # what it resolved to, None when nothing did
    cyclic: bool                 # already open further up the include stack
    flat_start: int              # where its contribution begins in `text`
    contributed: int


@dataclass(frozen=True)
class FlatSource:
    text: str
    segments: tuple[Segment, ...]
    root: Path
    directives: tuple[Directive, ...] = ()

    @property
    def missing(self) -> tuple[str, ...]:
        """Include targets that resolved to nothing, in document order."""
        return tuple(d.target for d in self.directives
                     if d.resolved is None and not d.cyclic)

    def locate(self, offset: int) -> tuple[Path, int]:
        """Map a byte offset in `text` back to its (file, 1-indexed line)."""
        if offset < 0 or offset >= len(self.text):
            raise IndexError(f"offset {offset} outside flattened source of length {len(self.text)}")
        i = bisect.bisect_right(self._starts, offset) - 1
        seg = self.segments[i]
        return seg.file, seg.line_start + self.text.count("\n", seg.flat_start, offset)

    @property
    def _starts(self) -> list[int]:
        return [s.flat_start for s in self.segments]


def flatten(main_tex: Path) -> FlatSource:
    """Recursively resolve \\input and \\include starting from `main_tex`.

    An unresolvable include is left verbatim in the buffer rather than dropped,
    so a missing file shows up in the render instead of being silently absent,
    and is also reported in `missing`.
    """
    main_tex = Path(main_tex).resolve()
    state = _State(root=main_tex.parent)
    _walk(main_tex, state, stack=())
    return FlatSource(
        text="".join(state.parts),
        segments=tuple(state.segments),
        root=main_tex.parent,
        directives=tuple(d for d in state.directives if d is not None),
    )


# ----------------------------------------------------------------- internals


class _State:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.parts: list[str] = []
        self.segments: list[Segment] = []
        # Slots are reserved on the way down and filled on the way back up, so
        # the tuple stays in document order while `contributed` can be measured
        # across the whole recursion.
        self.directives: list[Directive | None] = []
        self.length = 0

    def emit(self, chunk: str, file: Path, line: int) -> None:
        """Append `chunk`, recording that it began at `line` of `file`."""
        if not chunk:
            return
        self.segments.append(
            Segment(
                flat_start=self.length,
                flat_end=self.length + len(chunk),
                file=file,
                line_start=line,
                line_end=line + chunk.count("\n"),
            )
        )
        self.parts.append(chunk)
        self.length += len(chunk)


def _walk(
    path: Path,
    state: _State,
    stack: tuple[Path, ...],
    *,
    directive_ends_line: bool = False,
) -> None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return

    if stack:
        text = _contribution(text, directive_ends_line=directive_ends_line)

    pos = 0
    line = 1
    for m in _INCLUDE_RE.finditer(text):
        if is_commented(text, m.start()):
            continue

        chunk = text[pos:m.start()]
        state.emit(chunk, path, line)
        line += chunk.count("\n")

        target = _resolve(m.group(2).strip(), including=path, root=state.root)
        slot = len(state.directives)
        state.directives.append(None)
        before = state.length
        if target is None or target in stack:
            # Unresolvable or cyclic: keep the directive visible rather than
            # dropping content the author expected to see.
            state.emit(m.group(0), path, line)
            # The echoed directive is not a contribution; it is the hole.
            before = state.length
        else:
            _walk(
                target,
                state,
                stack + (path,),
                # `\include` clears the page on both sides, so its file is a
                # division of the document and the blank line the join leaves is
                # right. `\input` is a splice into the stream and must not
                # invent a paragraph break; see `_contribution`.
                directive_ends_line=(
                    m.group(1) == "input" and _rest_of_line_is_blank(text, m.end())
                ),
            )

        state.directives[slot] = Directive(
            kind=m.group(1), target=m.group(2).strip(), file=path, line=line,
            resolved=target,
            cyclic=target is not None and target in stack,
            flat_start=before, contributed=state.length - before,
        )
        line += m.group(0).count("\n")
        pos = m.end()

    state.emit(text[pos:], path, line)


def _contribution(text: str, *, directive_ends_line: bool) -> str:
    """What an included file actually hands back to the stream that read it.

    Not its bytes. TeX terminates every *line* it reads, but a file boundary is
    not a line boundary: at the end of an included file TeX simply resumes at
    the character after the directive. Concatenating the bytes verbatim
    therefore invents terminators the compiler never sees, and two of them meet
    as a blank line that splits a paragraph mid-sentence.

    Two corrections, and they compose:

    A trailing comment consumes its own terminator and hands back nothing, so it
    is dropped along with the `%` that opened it. Leaving the `%` in the buffer
    is worse than the newline it was there to eat: in the flattened stream it
    lands mid-line and comments out the rest of the *parent's* sentence.
    Observed 2026-07-27 in dsp-bias, where `\\input{frag}%` is how every
    statistic arrives and every clause following a number vanished from the
    render.

    Then, if the file still ends its own line and the including line ends at the
    directive too, one of the two terminators is dropped. Verified against
    pdflatex: `some text \\input{sub}` followed by `more text`, with `sub.tex`
    ending in a newline, typesets as one paragraph. Only the pair is collapsed,
    because a file ending mid-line (`\\input{sub}tail`) really does contribute
    the space that separates it from what follows.
    """
    while True:
        at = _trailing_comment(text)
        if at is None:
            break
        text = text[:at]

    if directive_ends_line and text.endswith("\n"):
        text = text[:-1]
    return text


def _trailing_comment(text: str) -> int | None:
    """Index of the `%` opening a comment that runs to the end of `text`."""
    body = text[:-1] if text.endswith("\n") else text
    return comment_at(body, body.rfind("\n") + 1, len(body))


def _rest_of_line_is_blank(text: str, at: int) -> bool:
    """True when only spaces separate `at` from its line's end (or the file's).

    Trailing spaces before the newline are not a contribution of their own: TeX
    is skipping blanks by then, so the terminator that follows them is the same
    single terminator.
    """
    eol = text.find("\n", at)
    return text[at : len(text) if eol < 0 else eol].strip(" \t") == ""


def is_commented(text: str, at: int) -> bool:
    """True when an unescaped `%` precedes `at` on the same line."""
    return comment_at(text, text.rfind("\n", 0, at) + 1, at) is not None


def comment_at(text: str, start: int, stop: int) -> int | None:
    """Index of the first unescaped `%` in `text[start:stop]`, or None.

    One scanner. `\\%` is a literal percent sign and opens nothing, and reading
    that rule two ways is how a manuscript quietly loses the half of a sentence
    that follows a number.
    """
    i = start
    while i < stop:
        c = text[i]
        if c == "\\":
            i += 2
            continue
        if c == "%":
            return i
        i += 1
    return None


def _resolve(target: str, *, including: Path, root: Path) -> Path | None:
    """LaTeX resolves against the compilation directory; fall back to the
    including file's own directory, which some projects rely on."""
    if not target:
        return None
    for base in (root, including.parent):
        for name in (target, target + ".tex"):
            candidate = (base / name).resolve()
            if candidate.is_file():
                return candidate
    return None
