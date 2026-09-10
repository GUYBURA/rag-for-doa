"""The only place text is normalized. Invariant 8.

Normalized text is what chunk.content stores, and it is also the *derivation
key*: the embedding, the content_sha256 dedup hash and the content_tsv tokens
are all computed from that same string. Four uses, one input, so they cannot
drift apart -- and a query has to enter through normalize_query() for the same
reason, or it is compared against text derived differently from itself.

Order is load-bearing and must not be rearranged:
  1. PUA tone marks -> real Thai codepoints
  2. Unicode NFC
  3. strip running headers/footers
  4. strip page numbers and dot leaders
  5. collapse whitespace

Why this order: PUA codepoints carry canonical combining class 0, so NFC sees
starters and has nothing to reorder while a tone mark is still PUA. Repair them
first, or the marks keep the PDF's raw order and text that renders identically
hashes two different ways. Measured: the two orders disagree on 6.6% of mixed
Thai/Latin/PUA inputs (see KNOWLEDGE.md). Header/footer lines are matched whole, so they
have to be stripped before whitespace collapsing destroys the line structure
they are identified by.
"""

import re
import unicodedata
from collections import Counter

from ingest.extract import Page

PUA_TO_THAI: dict[str, str] = {
    "\uf700": "\u0e4d",
    "\uf701": "\u0e48",
    "\uf702": "\u0e49",
    "\uf703": "\u0e4a",
    "\uf704": "\u0e4b",
    "\uf705": "\u0e47",
    # Positional variants: the font stores one mark at more than one codepoint,
    # chosen by the shape of the consonant under it. Identified by rendering the
    # glyph and reading the word it sits in, not from a font chart:
    #   F706 x15  ปอง   -> ป้อง   (follows an ascender: ป, อ)
    #   F708 x3   ปุย    -> ปุ๋ย    (all three on 2565 p220, which extracts reversed)
    #   F70A x85  สงออก -> ส่งออก
    #   F70B x71  ใชสาร -> ใช้สาร
    # F707/F709/F70C/F70D stay out on purpose: they occur in none of the three
    # editions, so there is no evidence for what they are, and the QA gate
    # blocks loudly if one ever appears.
    "\uf706": "\u0e49",
    "\uf708": "\u0e4b",
    "\uf70a": "\u0e48",
    "\uf70b": "\u0e49",
    "\uf70e": "\u0e4c",
    "\uf710": "\u0e34",
    "\uf711": "\u0e35",
    "\uf712": "\u0e36",
    "\uf713": "\u0e37",
    "\uf714": "\u0e31",
    "\uf718": "\u0e38",
    "\uf719": "\u0e39",
    "\uf71a": "\u0e3a",
}

PUA_RANGE = re.compile(r"[\uf700-\uf71a]")
EDGE_LINES = 3
_DIGITS = re.compile(r"\d+")

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

def _running_key(line: str) -> str:
    """Return a normalized key for a line, for matching running headers/footers."""
    masked = _DIGITS.sub("#", line)
    return collapse_whitespace(masked)

def _edge_keys(page: Page) -> set[str]:
    """Return the normalized keys of the first and last EDGE_LINES lines."""
    lines = [line for line in page.raw_text.split("\n") if line.strip()]
    edges = lines[:EDGE_LINES] + lines[-EDGE_LINES:]
    return {_running_key(line) for line in edges}

def detect_running_lines(
    pages: list[Page], min_page_ratio: float = 0.5
) -> frozenset[str]:
    """Find the header/footer lines that repeat across the document.

    Computed once per document and passed into normalize(), because a running
    header is only identifiable from the corpus -- a single page cannot tell a
    header from a heading.

    Returns _running_key() values, not raw lines: a header carries the page
    number, so no two pages spell it the same way. strip_running_lines() must
    key its input the same way or nothing will match.
    """
    if not pages:
        return frozenset()

    counts: Counter[str] = Counter()
    for page in pages:
        counts.update(_edge_keys(page))

    threshold = len(pages) * min_page_ratio
    return frozenset(
        key for key, pages_seen in counts.items() if pages_seen >= threshold
    )
    
def strip_running_lines(text: str, running_lines: frozenset[str]) -> str:
    """Remove any line that matches a running header/footer in Edge_LINES of the page."""
    lines = text.split("\n")
    filled = [i for i, line in enumerate(lines) if line.strip()]

    edge = set(filled[:EDGE_LINES] + filled[-EDGE_LINES:])
    drop = {i for i in edge if _running_key(lines[i]) in running_lines}

    return "\n".join(line for i, line in enumerate(lines) if i not in drop)


def strip_page_furniture(text: str) -> str:
    """Remove page numbers and dot leaders from the first and last lines of a page."""
    number_only = r"\s*?[-_\[]?\s*\d+\s*[-_\]]?\s*"
    dot_leader = r"\s*\.{3,}\s*\d*\s*$"

    lines = [re.sub(dot_leader, "", line) for line in text.split("\n")]

    filled = [i for i, line in enumerate(lines) if line.strip()]
    if not filled:
        return text

    drop = {i for i in (filled[0], filled[-1]) if re.fullmatch(number_only, lines[i])}

    return "\n".join(line for i, line in enumerate(lines) if i not in drop)

def collapse_whitespace(text: str) -> str:
    """Remove leading/trailing whitespace, collapse spaces/tabs, and collapse newlines."""
    stripped_text = text.strip()
    cleaned_space_tab = re.sub(r"[ \t]+", " ", stripped_text)
    cleaned_newlines = re.sub(r"\n{3,}", "\n\n", cleaned_space_tab)
    lines = cleaned_newlines.split("\n")
    cleaned_text = "\n".join(line.rstrip() for line in lines)
    return cleaned_text

def normalize_table(markdown: str) -> str:
    """Steps 1, 2 and 5 for a rendered table. Steps 3 and 4 are page-shaped.

    A table has no running header and no page number of its own - those belong
    to the page it sits on and are stripped there. Running them here would be
    worse than useless: strip_page_furniture()'s dot-leader rule would trim a
    cell that legitimately ends in an ellipsis, and strip_running_lines() looks
    at the first and last lines of its input, which for a table are the header
    row and the last row of data.

    Steps 1 and 2 are not optional. content_sha256 has to match across editions
    for the dedup index to do anything, and an unrepaired PUA tone mark or an
    uncomposed sequence hashes differently while rendering identically.
    """
    return collapse_whitespace(to_nfc(restore_pua_tone_marks(markdown)))


def normalize(text: str, running_lines: frozenset[str] = frozenset()) -> str:
    """Run the five steps in order. The only entry point other modules call."""
    text = restore_pua_tone_marks(text)
    text = to_nfc(text)
    text = strip_running_lines(text, running_lines)
    text = strip_page_furniture(text)
    return collapse_whitespace(text)


def normalize_query(question: str) -> str:
    """Steps 1, 2 and 5 for a user's question. Same three steps as a table,
    for a different reason.

    A query is matched against text that already went through NFC, so it has to
    go through NFC too. A question typed with its vowel and tone mark in the
    other keyboard order composes to a different string: the dense side sees a
    different token sequence and the sparse side's newmm segmentation splits it
    somewhere else, so both halves of hybrid retrieval degrade at once and
    neither reports anything wrong.

    Steps 3 and 4 are page-shaped - a question has no running header and no
    page number to strip.

    PUA repair stays in, and stays loud. A question can be pasted out of one of
    these PDFs and carry U+F700-U+F71A with it, and an unmapped codepoint is
    the same missing evidence here as anywhere else. Searching for a character
    that matches nothing by construction is worse than refusing to search.
    """
    return collapse_whitespace(to_nfc(restore_pua_tone_marks(question)))
