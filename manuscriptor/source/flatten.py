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


@dataclass(frozen=True)
class Segment:
    """One contiguous run of the flattened buffer, traced to its origin file."""

    flat_start: int
    flat_end: int
    file: Path
    line_start: int
    line_end: int


@dataclass(frozen=True)
class FlatSource:
    text: str
    segments: tuple[Segment, ...]
    root: Path
    missing: tuple[str, ...]

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
        missing=tuple(state.missing),
    )


# ----------------------------------------------------------------- internals


class _State:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.parts: list[str] = []
        self.segments: list[Segment] = []
        self.missing: list[str] = []
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


def _walk(path: Path, state: _State, stack: tuple[Path, ...]) -> None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return

    # A `%` consumes its own line terminator, so an included file whose last
    # line ends in a comment contributes no newline to the stream that included
    # it: TeX resumes at the character after the directive. Concatenating the
    # file's bytes verbatim inserts a newline the compiler never sees, and where
    # the including line also ends there, the two meet as a blank line and split
    # a paragraph mid-sentence.
    #
    # This is what makes the `\input{fragment}%` idiom work. A generated value
    # lives in its own file ending in `%` so the include contributes no trailing
    # space. Observed 2026-07-27 in dsp-bias, where every statistic arrives that
    # way and twelve sentences broke, always just after a number.
    #
    # Only for an included file. The root's trailing newline has nothing after
    # it, and a blank line following a comment *inside* a file is a real
    # paragraph break that TeX honors, so neither is touched here.
    if stack and text.endswith("\n") and _is_commented(text, len(text) - 1):
        text = text[:-1]

    pos = 0
    line = 1
    for m in _INCLUDE_RE.finditer(text):
        if _is_commented(text, m.start()):
            continue

        chunk = text[pos:m.start()]
        state.emit(chunk, path, line)
        line += chunk.count("\n")

        target = _resolve(m.group(2).strip(), including=path, root=state.root)
        if target is None or target in stack:
            # Unresolvable or cyclic: keep the directive visible rather than
            # dropping content the author expected to see.
            if target is None:
                state.missing.append(m.group(2).strip())
            state.emit(m.group(0), path, line)
        else:
            _walk(target, state, stack + (path,))

        line += m.group(0).count("\n")
        pos = m.end()

    state.emit(text[pos:], path, line)


def _is_commented(text: str, at: int) -> bool:
    """True when an unescaped `%` precedes `at` on the same line."""
    bol = text.rfind("\n", 0, at) + 1
    i = bol
    while i < at:
        c = text[i]
        if c == "\\":
            i += 2
            continue
        if c == "%":
            return True
        i += 1
    return False


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
