"""Turning a BibTeX field into plain text, in one place.

A `.bib` title is not a string, it is LaTeX. It carries protective braces
around anything whose capitalization must survive BibTeX's case folding
(`{COVID-19}`, `{India}`, `{van der Berg}`), accent commands instead of
Unicode (`Sch\\"onbrunn`), and escapes for the characters LaTeX reserves
(`\\&`). Zotero holds the same paper as publisher metadata: plain Unicode,
no braces, different capitalization and punctuation.

Nothing can be compared until both sides are spoken in the same language,
and the only safe way to guarantee that is for both to go through the SAME
function. They did not. The Zotero side went through a normalizer; the
BibTeX side went through `.strip("{}")`, which removes braces only at the
two ends of the string and leaves every interior one in place. On a real
42-citation manuscript that produced zero Zotero matches out of 42, and the
same idiom appeared four times over — title, journal, author, year — so
repairing any one of them would have left three copies free to diverge.

`plain_title` is the reader: LaTeX in, human-readable text out. It is what
gets stored and what gets sent as a query. `normalize_title` is the
comparator: it folds case, punctuation and accents on top, and is the only
thing two titles are ever compared through.

Widening the fuzzy threshold is NOT an alternative to this. The threshold
governs how much two normalized titles may differ; it cannot recover a query
string that was malformed before it was sent.
"""
from __future__ import annotations

import html as _html
import re
import unicodedata

__all__ = ["plain_title", "normalize_title", "titles_close"]

# Zotero's answer to the same problem BibTeX solves with braces. Importing
# `{India in the COVID}` gives back
#     TB control in <span class="nocase">India in the COVID</span> era
# so de-bracing the .bib alone left the two sides speaking different markup
# languages and they still could not be compared. Six of the ten citations
# that were still unmatched after the brace fix were only this.
_TAG_RE = re.compile(r"<[^>]+>")


# LaTeX accent commands, as the combining mark each one applies. One table,
# applied to whatever letter follows, so every accented letter is handled by
# the same rule rather than by an enumeration of accented letters.
_COMBINING = {
    "`": "\u0300",   # grave
    "'": "\u0301",   # acute
    "^": "\u0302",   # circumflex
    "~": "\u0303",   # tilde
    "=": "\u0304",   # macron
    ".": "\u0307",   # dot above
    '"': "\u0308",   # diaeresis
    "u": "\u0306",   # breve
    "r": "\u030A",   # ring above
    "H": "\u030B",   # double acute
    "v": "\u030C",   # caron
    "d": "\u0323",   # dot below
    "c": "\u0327",   # cedilla
    "k": "\u0328",   # ogonek
    "b": "\u0331",   # macron below
    "t": "\u0361",   # tie
}

# Standalone glyph macros. Longest alternatives first so `\aa` is not read as
# `\a` + `a`; the trailing guard stops `\o` from eating `\otimes`.
_GLYPHS = {
    "ss": "ß", "SS": "SS", "ae": "æ", "AE": "Æ", "oe": "œ", "OE": "Œ",
    "aa": "å", "AA": "Å", "dh": "ð", "DH": "Ð", "th": "þ", "TH": "Þ",
    "dj": "đ", "DJ": "Đ", "ng": "ŋ", "NG": "Ŋ",
    "o": "ø", "O": "Ø", "l": "ł", "L": "Ł", "i": "i", "j": "j",
}
_GLYPH_RE = re.compile(
    r"\\(" + "|".join(sorted(_GLYPHS, key=len, reverse=True)) + r")(?![A-Za-z])"
)

# `\'e`, `\'{e}`, `\"{o}` — accent commands named by punctuation.
_ACCENT_PUNCT_RE = re.compile(
    r"\\([`'^\"~=.])\s*(?:\{\s*([A-Za-z])\s*\}|([A-Za-z]))"
)
# `\v{s}`, `\c c`, `\H{o}` — accent commands named by a letter. A letter-named
# command MUST be delimited by a brace or a space, or `\vs` would be a caron.
_ACCENT_ALPHA_RE = re.compile(
    r"\\([uvHcdbkrt])\s*(?:\{\s*([A-Za-z])\s*\}|\s+([A-Za-z]))"
)

# `\emph{...}`, `\textit{...}` — keep the argument, drop the wrapper.
_WRAPPER_RE = re.compile(r"\\[A-Za-z]+\s*\{([^{}]*)\}")
_ESCAPED_RE = re.compile(r"\\([&%$#_])")
_LEFTOVER_CMD_RE = re.compile(r"\\[A-Za-z]+\s*|\\(?=[^A-Za-z])")


def _accent(mark: str, letter: str) -> str:
    return unicodedata.normalize("NFC", letter + mark)


def plain_title(raw: str | None) -> str:
    """A citation field as human-readable text: no braces, no markup, real letters.

    Runs over BOTH sources — a BibTeX field and a Zotero record — because they
    protect capitalization in different notations (`{...}` and `<span
    class="nocase">`) and neither is readable. One function for both is what
    makes the two comparable at all; two functions would drift, which is the
    shape of the original bug.

    This is what gets stored in `citations.json` and what gets sent to Zotero
    as a search query. It is deliberately NOT case- or punctuation-folded,
    because it is also what a person reads in the viewer.
    """
    t = (raw or "").strip()
    if not t:
        return ""

    t = _TAG_RE.sub("", t)
    t = _html.unescape(t)
    t = _GLYPH_RE.sub(lambda m: _GLYPHS[m.group(1)], t)
    t = _ACCENT_PUNCT_RE.sub(
        lambda m: _accent(_COMBINING[m.group(1)], m.group(2) or m.group(3)), t)
    t = _ACCENT_ALPHA_RE.sub(
        lambda m: _accent(_COMBINING[m.group(1)], m.group(2) or m.group(3)), t)
    t = _ESCAPED_RE.sub(r"\1", t)

    # Unwrap formatting commands from the inside out, so `\emph{\textbf{x}}`
    # collapses fully rather than leaving a stranded `\textbf`.
    for _ in range(4):
        new = _WRAPPER_RE.sub(r"\1", t)
        if new == t:
            break
        t = new

    t = _LEFTOVER_CMD_RE.sub(" ", t)
    t = t.replace("---", "\u2014").replace("--", "\u2013")
    t = t.replace("$", "")
    # THE fix: every brace goes, wherever it sits. `.strip("{}")` reached only
    # the two ends, which is why `{COVID-19}` survived into the query and why a
    # title ending in `{India}` came out reading `{India`.
    t = t.replace("{", "").replace("}", "")
    return re.sub(r"\s+", " ", t).strip()


# Characters Unicode decomposition will NOT take apart, so that a `.bib`
# spelling and a Zotero spelling of the same name still land on one string.
_FOLD = str.maketrans({
    "ß": "ss", "æ": "ae", "Æ": "AE", "œ": "oe", "Œ": "OE",
    "ø": "o", "Ø": "O", "đ": "d", "Đ": "D", "ð": "d", "Ð": "D",
    "þ": "th", "Þ": "Th", "ł": "l", "Ł": "L", "ı": "i", "ȷ": "j",
    "ŋ": "ng", "Ŋ": "Ng",
})


def normalize_title(raw: str | None) -> str:
    """The comparison form. BOTH sides of every title match go through this.

    Case and punctuation are folded deliberately. A `.bib` and a publisher
    record disagree about them constantly and for no reason that carries
    meaning: title case against sentence case, a colon against an em dash,
    "COVID-19" against "Covid-19". Folding them is what makes two records of
    one paper equal. Nothing that distinguishes two different papers is
    dropped, so the word-overlap test below still has real work to do.
    """
    t = plain_title(raw).translate(_FOLD)
    t = unicodedata.normalize("NFKD", t)
    t = "".join(ch for ch in t if not unicodedata.combining(ch))
    t = t.lower()
    t = re.sub(r"[^a-z0-9 ]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def titles_close(a: str, b: str) -> bool:
    """Word-overlap fallback for two ALREADY-normalized titles.

    Subtitles get dropped and restored between records, so exact equality is
    too strict; the length guard keeps it from matching a paper against a
    book chapter that merely shares vocabulary.
    """
    if not a or not b:
        return False
    a_words = a.split()
    b_words = b.split()
    if abs(len(a_words) - len(b_words)) > 3:
        return False
    common = set(a_words) & set(b_words)
    denom = min(len(a_words), len(b_words))
    return denom > 0 and len(common) / denom >= 0.8
