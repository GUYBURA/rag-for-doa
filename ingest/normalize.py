"""The only place text is normalized. Invariant 8.

Normalized text is never stored. It is a *derivation key*: the embedding, the
content_sha256 dedup hash and the content_tsv tokens are all computed from it,
while chunk.content keeps the raw text for citation. Three derivations, one
input, so they cannot drift apart.

Order is load-bearing and must not be rearranged:
  1. PUA tone marks -> real Thai codepoints
  2. Unicode NFC
  3. strip running headers/footers
  4. strip page numbers and dot leaders
  5. collapse whitespace

Why this order: NFC cannot compose a combining mark it does not recognise, so
PUA repair has to run first. Header/footer lines are matched whole, so they
have to be stripped before whitespace collapsing destroys the line structure
they are identified by.
"""

import re
import unicodedata
from collections import Counter

from ingest.extract import Page

# Thai PDFs encode tone marks and vowels as font-specific Private Use Area
# glyphs (alternate forms positioned for tall consonants). PyMuPDF returns the
# PUA codepoint verbatim -- correct behaviour, it is what the file says -- so
# the mapping back to real Thai has to happen here. The 2565 cover page alone
# carries 225 of these.
#
# Written as escapes rather than literal glyphs: these codepoints render as
# nothing in an editor, so a wrong entry would be invisible on review.
#
# TREAT THIS TABLE AS A HYPOTHESIS, NOT A FACT. Run survey_pua() over every
# source PDF and confirm each entry against the rendered page before trusting
# it. A wrong entry here silently changes the meaning of a pesticide name.
PUA_TO_THAI: dict[str, str] = {
    "\uf700": "\u0e4d",  # nikhahit ํ
    "\uf701": "\u0e48",  # mai ek ่
    "\uf702": "\u0e49",  # mai tho ้
    "\uf703": "\u0e4a",  # mai tri ๊
    "\uf704": "\u0e4b",  # mai chattawa ๋
    "\uf705": "\u0e47",  # mai taikhu ็
    "\uf70e": "\u0e4c",  # thanthakhat ์
    "\uf710": "\u0e34",  # sara i ิ
    "\uf711": "\u0e35",  # sara ii ี
    "\uf712": "\u0e36",  # sara ue ึ
    "\uf713": "\u0e37",  # sara uee ื
    "\uf714": "\u0e31",  # mai han akat ั
    "\uf718": "\u0e38",  # sara u ุ
    "\uf719": "\u0e39",  # sara uu ู
    "\uf71a": "\u0e3a",  # phinthu ฺ
}

PUA_RANGE = re.compile(r"[\uf700-\uf71a]")


def survey_pua(pages: list[Page]) -> Counter[str]:
    """Count every PUA codepoint in the document, for building PUA_TO_THAI.

    Evidence-gathering, not part of the pipeline. Run it on a new source PDF
    before ingesting it; any codepoint it reports that is missing from
    PUA_TO_THAI will make restore_pua_tone_marks() raise.
    """
    counts: Counter[str] = Counter()
    for page in pages:
        counts.update(PUA_RANGE.findall(page.raw_text))
    return counts


def restore_pua_tone_marks(text: str) -> str:
    """Step 1. Map PUA glyphs back to real Thai codepoints.

    Raises on an unmapped PUA character rather than dropping it. A dropped tone
    mark changes a word into a different, still-valid Thai word -- the failure
    is invisible downstream, so it has to be loud here.
    """
    unmapped = {c for c in PUA_RANGE.findall(text) if c not in PUA_TO_THAI}
    if unmapped:
        codes = ", ".join(f"U+{ord(c):04X}" for c in sorted(unmapped))
        raise ValueError(f"unmapped PUA codepoints: {codes}. Run survey_pua().")
    return PUA_RANGE.sub(lambda m: PUA_TO_THAI[m.group()], text)


def to_nfc(text: str) -> str:
    """Step 2. Canonical composition, so identical text hashes identically."""
    return unicodedata.normalize("NFC", text)


def detect_running_lines(
    pages: list[Page], min_page_ratio: float = 0.5
) -> frozenset[str]:
    """Find the header/footer lines that repeat across the document.

    Computed once per document and passed into normalize(), because a running
    header is only identifiable from the corpus -- a single page cannot tell a
    header from a heading.

    YOUR TURN. Suggested approach:
      - for each page, take the first N and last N non-blank stripped lines
      - count how many *distinct pages* each such line appears on
      - keep lines appearing on >= min_page_ratio of pages
    Count pages, not occurrences: one page repeating a line ten times must not
    promote it to a header.
    """
    raise NotImplementedError


def strip_running_lines(text: str, running_lines: frozenset[str]) -> str:
    """Step 3. Drop the lines detect_running_lines() identified.

    These carry the edition year. Leaving them in leaks '2565' into the text of
    a 2568 chunk, which is how a retriever ends up citing the wrong edition.

    YOUR TURN. Match on the stripped line, keep everything else, and keep the
    remaining lines in order.
    """
    raise NotImplementedError


def strip_page_furniture(text: str) -> str:
    number_only = r"\s*?[-_\[]?\s*\d+\s*[-_\]]?\s*"
    dot_leader = r"\s*\.{3,}\s*\d*\s*$"

    lines = [re.sub(dot_leader, "", line) for line in text.split("\n")]

    filled = [i for i, line in enumerate(lines) if line.strip()]
    if not filled:
        return text

    drop = {i for i in (filled[0], filled[-1]) if re.fullmatch(number_only, lines[i])}

    return "\n".join(line for i, line in enumerate(lines) if i not in drop)

def collapse_whitespace(text: str) -> str:
    stripped_text = text.strip()
    cleaned_space_tab = re.sub(r"[ \t]+", " ", stripped_text)
    cleaned_newlines = re.sub(r"\n{3,}", "\n\n", cleaned_space_tab)
    lines = cleaned_newlines.split("\n")
    cleaned_text = "\n".join(line.rstrip() for line in lines)
    return cleaned_text

def normalize(text: str, running_lines: frozenset[str] = frozenset()) -> str:
    """Run the five steps in order. The only entry point other modules call."""
    text = restore_pua_tone_marks(text)
    text = to_nfc(text)
    text = strip_running_lines(text, running_lines)
    text = strip_page_furniture(text)
    return collapse_whitespace(text)
