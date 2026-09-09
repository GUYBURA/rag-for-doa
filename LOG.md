# LOG.md

Bugs that took more than one attempt, and things tried and abandoned. Append only.

### Bug: 2565 page 220 extracts with some lines reversed
**Status:** Open
**Date:** 2026-09-09
**Cause:** Not established. PyMuPDF returns 7 of the page's 318 non-empty lines in reverse
character order — `"ดยีอเะลยาร"` for `"รายละเอียด"`. The rest of the page is fine, and no
other page in any edition shows it: a heuristic looking for lines that start on a dependent
vowel or combining mark (impossible in written Thai) flags page 220 of 2565 and nothing
else in 2565, 2566 or 2568. The same page carries 193 PUA tone marks, the most of any page
in the corpus, so a shared cause in how that page was produced is plausible but unproven.

**Tried and rejected:** `get_text("text", sort=True)`. It does not un-reverse the lines; it
returns a different reading order entirely — 23,247 characters against 3,160, with list
numbering interleaved. Wrong tool: `sort` reorders blocks geometrically, and the damage here
is inside a line.

**Why it is not blocked on:** `normalize.py` cannot repair it — reversal is not in the five
steps, and adding a sixth would need sign-off. The QA gate cannot see it either: the
combining-mark ratio counts marks, and reversing a line leaves the count identical. This is
the exact blind spot recorded in KNOWLEDGE.md, now with a real instance.

**Note for whoever picks this up:** `tests/fixtures/excerpt.pdf` page 3 *is* this page. It
was chosen for its PUA density before the reversal was found. It still serves its test —
`extract.py` promises to hand back what PyMuPDF read, and reversed text proves that as well
as clean text does — but the fixture is not an example of healthy extraction.

### Bug: survey_pua() reported zero PUA in 2568, which has 593
**Status:** Fixed in understanding, not in code
**Date:** 2026-09-09
**Cause:** `PUA_RANGE` is `[-]`, the Thai tone-mark sub-range. 2568's private-use
characters come from SymbolMT and Wingdings, which live in U+F0xx, so every scan built on
`PUA_RANGE` — `survey_pua()`, `restore_pua_tone_marks()` and the gate's
`unmapped_pua_codepoints` — reported nothing.

**Solution:** None applied. Identification is done (see KNOWLEDGE.md: α, Δ, → and a
decorative tilde), but repair needs the span's font, which `Page` does not carry. Widening
`PUA_RANGE` alone would make `restore_pua_tone_marks()` raise on 593 characters it has no
mapping for and block ingestion of 2568.

**Tried and rejected:** mapping the codepoints from published Symbol/Wingdings charts. The
byte-to-glyph tables are close enough to be convincing and wrong — U+F072 in Wingdings 3 is
a triangle used *as* Δ, not the Greek letter, and getting that backwards writes the wrong
character into a chemical name. Rendered each glyph out of the PDF at 8x and read it
instead.
