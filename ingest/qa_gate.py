"""Blocking parse-quality gate. Invariant 9.

A document that fails here is not chunked and not embedded; the result is
written to document.qa and the row stays 'pending'. There is no bypass flag.

The return value is a dict of check name -> {passed, measured, threshold}, not
a boolean: invariant 9 requires the *reason* to reach document.qa, and a
boolean cannot say which check fired or how far off it was. Every check runs on
every call — none of them are expensive once the text is in memory, and
short-circuiting would leave the qa record half empty.

Thresholds are keyword arguments with defaults chosen as statements about Thai
documents in general, not fits to any one edition, and the threshold actually
used is recorded alongside each measurement so an old qa row stays readable
after a default changes.

combining_ratio_min = 0.20: marks per Thai consonant. Consonants are the
denominator because a parser that drops vowels leaves them untouched. Measured
on all three source volumes: 2568 0.3314, 2565 0.3345, 2566 0.3324 — a spread
under 1% across two different publication series, which is what justifies a
floor this close to the observed value. It leaves a 1.66x margin and, unlike
0.10, is above the 0.170 a book scores after losing every upper vowel. Known
gap: this catches deletion only. Reordered marks (invariant 7's other failure
mode) leave the count identical. See KNOWLEDGE.md.

unmapped_pua_codepoints scans PUA_RANGE, the Thai tone-mark sub-range, so
symbol_pua_codepoints reports the rest of the private use area separately -
the U+F0xx characters SymbolMT and Wingdings produce, 593 of them in 2568,
three of which are real content (alpha, delta, an arrow inside chemical group
names). It measures and never blocks, on purpose: repairing those needs the
span font, which Page does not carry, so blocking would refuse a document
nothing in this repo can fix. The number reaching document.qa is what makes the
gap auditable instead of invisible. See LOG.md and KNOWLEDGE.md.

Text arrives raw, before normalize.py — the gate has to see the damage before
anyone repairs it. A consequence is that mapped PUA tone marks are not counted
as combining marks here, which biases combining_ratio slightly low on documents
that use them (2568 has none; 2565 has 225).
"""

import re

from ingest.extract import Page
from ingest.normalize import PUA_RANGE, PUA_TO_THAI

# The whole Basic Multilingual Plane private use area. PUA_RANGE is the Thai
# tone-mark slice of it; everything else in here is some other font's glyph.
ANY_PUA = re.compile("[\ue000-\uf8ff]")

THAI_COMBINING = re.compile(r"[ัิ-ฺ็-๎]")
THAI_CONSONANT = re.compile(r"[ก-ฮ]")


def failed_checks(qa: dict[str, dict]) -> set[str]:
    """Names of the checks that failed. Empty set means the document passed.

    Lives here rather than in the caller so run.py, the tests and anything
    later reading document.qa back out of Postgres all ask the same question
    the same way.
    """
    return {name for name, check in qa.items() if not check["passed"]}


def _check(passed: bool, measured, threshold=None) -> dict:
    return {"passed": passed, "measured": measured, "threshold": threshold}


def qa_gate(
    pages: list[Page],
    *,
    pdf_page_count: int,
    scopes: list[str],
    empty_ratio_max: float = 0.5,
    combining_ratio_min: float = 0.20,
    thin_page_chars: int = 50,
) -> dict[str, dict]:
    """Run every parse-quality check. Returns the record for document.qa."""
    numbers = [page.page_number for page in pages]
    expected = list(range(1, len(pages) + 1))

    text = "".join(page.raw_text for page in pages)
    marks = len(THAI_COMBINING.findall(text))
    consonants = len(THAI_CONSONANT.findall(text))

    empty = [p.page_number for p in pages if not p.raw_text.strip()]
    thin = [p.page_number for p in pages if len(p.raw_text.strip()) < thin_page_chars]
    empty_ratio = len(empty) / len(pages) if pages else 1.0

    unmapped = sorted(
        {f"U+{ord(c):04X}" for c in PUA_RANGE.findall(text) if c not in PUA_TO_THAI}
    )
    symbol_pua = sorted(
        {f"U+{ord(c):04X}" for c in ANY_PUA.findall(text) if not PUA_RANGE.match(c)}
    )

    return {
        "page_numbers_not_sequential": _check(numbers == expected, numbers),
        "page_count_mismatch": _check(
            len(pages) == pdf_page_count, len(pages), pdf_page_count
        ),
        "empty_ratio_too_high": _check(
            empty_ratio <= empty_ratio_max, round(empty_ratio, 4), empty_ratio_max
        ),
        "thin_pages": _check(True, len(thin), thin_page_chars),
        "unmapped_pua_codepoints": _check(not unmapped, unmapped),
        # Measured, never blocking: nothing here can repair a symbol-font glyph
        # without the span font, so failing the document would only stop it
        # being ingested at all. Recorded so the gap is auditable per document.
        "symbol_pua_codepoints": _check(True, symbol_pua),
        "combining_ratio_too_low": _check(
            consonants == 0 or marks / consonants >= combining_ratio_min,
            round(marks / consonants, 4) if consonants else None,
            combining_ratio_min,
        ),
        "no_thai_consonants": _check(consonants > 0, consonants),
        "scopes_empty": _check(bool(scopes), list(scopes)),
    }
