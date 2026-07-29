"""Bib titles carry LaTeX, and the query that goes to Zotero must not.

An evidence pass on a real 42-citation manuscript resolved ZERO citations from
Zotero. It appeared to get six, but all six were a prior run's disk cache; the
Zotero path contributed nothing at all.

The cause is one idiom, written four times in `resolve.py`:

    (entry.get("title") or "").strip().strip("{}")

`str.strip("{}")` removes only LEADING and TRAILING braces. The protective
braces a real `.bib` uses are INTERIOR — `{COVID-19}`, `{India}`, `{van der
Berg}` — so the string handed to Zotero's quicksearch still contains them and
matches nothing, while a title whose braced group happens to sit at the very
end loses its closing brace and looks truncated.

Measured on that manuscript: 32 of 36 misses had braces in the title, 0 of 6
hits did.

The rule these guards encode is that ONE function turns a BibTeX field into
plain text, and both sides of every comparison go through the same
normalization, so the two cannot drift apart.
"""
from __future__ import annotations

import re

import pytest

from manuscriptor.evidence import titles


# Titles copied verbatim out of the COVET India `sample.bib`. Each is a shape
# that the strip("{}") idiom gets wrong in a different way.
REAL_BIB_TITLES = {
    # interior group, mid-title: strip() is a no-op and the braces survive
    "world2021engaging": (
        "Engaging private health care providers in {TB} care and prevention: A landscape analysis",
        "Engaging private health care providers in TB care and prevention: A landscape analysis",
    ),
    # group at the very start: the opening brace goes, its closer is orphaned
    "aris2022world": (
        "{COVID-19}: Endemic doesn't mean harmless",
        "COVID-19: Endemic doesn't mean harmless",
    ),
    # group at the very end: the closing brace goes and the title reads truncated
    "arentz2022impact": (
        "The impact of the {COVID-19} pandemic and associated suppression measures "
        "on the burden of tuberculosis in {India}",
        "The impact of the COVID-19 pandemic and associated suppression measures "
        "on the burden of tuberculosis in India",
    ),
    # two groups, one at each end
    "malani2021seroprevalence": (
        "Seroprevalence of {SARS-CoV-2 in slums versus non-slums in Mumbai, India}",
        "Seroprevalence of SARS-CoV-2 in slums versus non-slums in Mumbai, India",
    ),
    "gelman2014beyond": (
        "Beyond power calculations: Assessing type {S} (sign) and type {M} (magnitude) errors",
        "Beyond power calculations: Assessing type S (sign) and type M (magnitude) errors",
    ),
}


@pytest.mark.parametrize("key", sorted(REAL_BIB_TITLES))
def test_a_bib_title_becomes_plain_text_with_no_braces_left(key):
    raw, want = REAL_BIB_TITLES[key]
    assert titles.plain_title(raw) == want


def test_the_old_idiom_is_what_broke_and_the_new_one_does_not():
    """The specific failure, named. strip("{}") only ever touches the ends."""
    raw = REAL_BIB_TITLES["arentz2022impact"][0]
    old = raw.strip().strip("{}")
    assert "{COVID-19}" in old, "the interior braces survived, which is the bug"
    assert old.endswith("{India"), "and the tail lost its closing brace"
    assert "{" not in titles.plain_title(raw) and "}" not in titles.plain_title(raw)


def test_latex_accents_fold_to_the_same_string_as_the_unicode_zotero_holds():
    """Zotero stores publisher Unicode; a .bib stores LaTeX accent commands.

    Unless both sides normalize identically the comparison can never be true,
    and widening the fuzzy threshold to compensate would only buy false matches.
    """
    pairs = [
        (r"Les d\'{e}terminants de la sant\'e", "Les déterminants de la santé"),
        (r"Sch\"onbrunn und die {\ss}tadt", "Schönbrunn und die ßtadt"),
        (r"Mart\'{\i}nez and Nu\~nez", "Martínez and Nuñez"),
        (r"{\O}stergaard's cohort", "Østergaard's cohort"),
        (r"Health \& welfare in {S}\~ao Paulo", "Health & welfare in São Paulo"),
        (r"Erd\H{o}s, Kraskov and \v{S}iler", "Erdős, Kraskov and Šiler"),
    ]
    for latex, unicode_form in pairs:
        assert titles.normalize_title(latex) == titles.normalize_title(unicode_form), latex


def test_normalization_folds_case_and_punctuation_but_not_words():
    """Deliberate: publisher metadata and .bib disagree on case and punctuation
    constantly (title case vs sentence case, ':' vs '—', 'COVID-19' vs
    'Covid-19'). Folding those is what makes the two equal. Nothing that
    carries meaning is dropped, so the word-overlap test still has to work."""
    a = titles.normalize_title("COVID-19: Endemic Doesn't Mean Harmless")
    b = titles.normalize_title(r"{COVID-19} --- endemic doesn't mean harmless.")
    assert a == b
    assert a == "covid 19 endemic doesn t mean harmless"
    assert titles.normalize_title("Tuberculosis in India") != titles.normalize_title(
        "Tuberculosis in Nigeria"
    )


def test_there_is_exactly_one_brace_stripping_implementation():
    """The duplication IS the bug. strip("{}") was written four times in
    resolve.py — title, journal, author, year — and fixing only the title
    would leave three copies to diverge from."""
    import io
    import tokenize
    from pathlib import Path

    def code_only(text: str) -> str:
        """Source with comments and prose removed, so the docstrings in
        titles.py may name the bad idiom without tripping its own guard."""
        out = []
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type == tokenize.COMMENT:
                continue
            if tok.type == tokenize.STRING and tok.string.strip("rbfu") not in ('"{}"', "'{}'"):
                out.append('"<str>"')
                continue
            out.append(tok.string)
        return " ".join(out)

    src = Path(__file__).resolve().parents[1] / "manuscriptor" / "evidence"
    offenders = [
        path.name for path in sorted(src.glob("*.py"))
        if re.search(r"""strip\s*\(\s*["']\{\}["']\s*\)""",
                     code_only(path.read_text(encoding="utf-8")))
    ]
    assert not offenders, "hand-rolled brace stripping outside titles.py: " + ", ".join(offenders)
