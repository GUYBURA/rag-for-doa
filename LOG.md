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

### Bug: the PUA check was narrower than its name and hid symbol-font glyphs
**Status:** Partly fixed — detected and recorded, not repaired
**Date:** 2026-09-10
**Cause:** `unmapped_pua_codepoints` scans `PUA_RANGE`, which is only the Thai
tone-mark slice U+F700-U+F71A. Every other private-use character passed the gate
silently, so a document could reach `chunk.content` carrying hundreds of glyphs
that cannot be searched or rendered while `document.qa` recorded no problem.
**Solution:** added `symbol_pua_codepoints`, which scans the whole BMP private
use area (U+E000-U+F8FF) minus the Thai slice. It measures and never blocks.
Measured across the corpus, which turned up something the earlier survey missed
— 2565 carries symbol glyphs too, not just 2568:

```
2565  U+F022 U+F061 U+F072 U+F097
2566  (none)
2568  U+F022 U+F061 U+F072 U+F07E
```

**Tried and rejected:** widening `PUA_RANGE` itself so the existing check blocks
on these. That would fail 2568 and 2565 outright on 593 and 225 characters
respectively, with no code path able to repair them: the same codepoint means
different things under different fonts (U+F061 is alpha only because the span
is SymbolMT), so a font-blind mapping table would be wrong by construction, and
a correct one needs span fonts that `Page` does not carry. Blocking a document
nothing can fix converts a silent data-quality gap into a hard stop without
improving the data. Recording the codepoints per document keeps the gap
auditable and leaves the repair for when `extract.py` carries font information.

### Bug: A gold "supersession" negative was mislabelled, because the check that built it was section-scoped

**Status:** Fixed
**Date:** 2026-09-11
**Cause:** `eval/questions.yaml`'s `q34_superseded_soybean_stinkbug` claimed
that the 2565 edition's stink bug entry (`Nezara viridula`) had been
superseded out of the active 2568 edition, and was labelled `answerable:
false` on that basis. It had not been. The content is in five active 2568
chunks (pages 96, 102, 110, 112, 120).

The verification that produced the wrong label searched the active corpus's
*soybean section*. 434 of 583 chunks have `section = NULL` -- `chunk.py`'s
heading detection identifies a section for only 26% of them -- and all five
chunks containing this fact are among them. A section-scoped search for
content in an unsectioned chunk returns nothing whether or not the content is
there, so the query could not have found it even in principle.

Surfaced by running all 34 questions through `query/answer.py`: the model
"failed" to refuse q34 and cited three pages. Reading the answer showed it was
correct and grounded, which made the gold label the suspect rather than the
model. Confirmed with a direct `content ilike '%Nezara%'` against `chunk`,
with no section predicate.

**Solution:** Relabelled q34 as answerable with `expect_contains: Nezara
viridula` and `expect_section: null`, and recorded why in the question's own
comment. The gold set now has no supersession negative at all, which is the
honest state: building a real one requires trusting a section-scoped query,
and that is exactly the blind spot that produced this mistake.

**Tried and rejected:** deleting the question outright. The comment explaining
how the label went wrong is worth more than the slot, and a future
supersession negative belongs in the same place.

**Left open deliberately:** `chunk.py`'s section detection. 74% null is not a
crash and nothing depends on section being present, but two things degrade
quietly because of it: the section label that `prompt.py` feeds the model as
crop evidence is absent for most chunks (it is the only signal that a mango
table is not a durian table), and `eval/run_eval.py`'s `_is_hit()` requires a
section match. One observed section value is also plainly wrong
(`"รูปร่างของแมลง (metamorphosis) ส่งผลให้แมลงมีการลอก"`, 6 chunks), so
detection is not merely incomplete. Changing the chunking strategy is on
CLAUDE.md's stop-and-ask list, so it stays recorded rather than fixed here.

### Bug: Table markdown was mostly empty cells, and split terms across "<br>"

**Status:** Fixed
**Date:** 2026-09-11
**Cause:** `extract.py`'s `_to_markdown()` rendered PyMuPDF's cell grid
verbatim. Two artefacts of that grid reached `chunk.content` and therefore the
prompt the model reads:

A merged cell reports its text once and leaves the rest of its span blank, and
the ruling lines include narrow spacer columns that never hold anything. Page
56 of 2568 comes back as 11 columns of which four are empty in every row, so
rows rendered as `|ศัตรูพืช||สารป้องกันกำจัดศัตรูพืช||||||||วิธีการใช้|`.

Newlines inside a cell became a literal `<br>`. Those breaks are where the
printed table wrapped, not data, and rendering them split terms:
`Phakopsora<br>pachyrhizi` is not a substring of the binomial anyone searches
for or cites. `eval/run_eval.py` had already grown a `_BR` regex to undo this
before every containment check -- a workaround downstream of the real problem.

**Solution:** Drop any column empty in every row, and any row empty in every
column, and join wrapped cell lines with a space. Both are mechanical: no cell
text is discarded and `Table.cells` stays the unedited record, so this is a
rendering change, not an extraction change. `_BR` removed from run_eval.py as
dead code.

Chunk text changed, so the whole corpus was re-ingested (`docker compose down
-v`, then all three editions in edition order). Chunk counts fell -- 2565
423 -> 383, 2566 531 -> 486, 2568 583 -> 519 -- because shorter table markdown
splits fewer times. `assert_no_stale_chunks` returns 0 rows and no chunk
carries `<br>`.

Every score changed with the text, so `SCORE_THRESHOLD` was re-swept:
recall@5 = 28/29 and one refusal, identical to before. The sweep's suggestion
moved to 0.3916, on the same plateau as 0.42 (same recall, same refusals), so
the constant stayed.

**Tried and rejected:** merging the multi-row header into one row, which is
what would actually make these tables read well -- the real header for page 56
spans two physical rows, so the rendered header is still half empty. A
complementary-columns rule (merge a leading row when its non-empty columns
don't overlap the header's) is mechanical enough to implement, but it is a
judgement about which rows *mean* header, and `extract.py` states in its own
docstring that it makes no judgement calls: a first data row that happened to
be complementary would be silently absorbed into the header. Left undone
deliberately, and the discarded option recorded here because the layout
complaint that prompted this fix is only partly addressed by it.

### Bug: 13 testcontainers tests error with "Port mapping ... is not available"

**Status:** Fixed (worked around)
**Date:** 2026-09-12
**Cause:** Not the test suite and not a schema change. Every test that takes a
container fixture errored at setup with
`ConnectionError: Port mapping for container <id> and port 8080 is not
available` -- port 8080 being Ryuk's, testcontainers' reaper sidecar, not
Postgres's. `docker ps` showed the Ryuk container running and healthy, which
is what made this misleading: Docker was up, the image was present, and the
failure still looked like the DB layer had broken. It is testcontainers being
unable to read back the host port mapping for the reaper in this environment.

**Solution:** `TESTCONTAINERS_RYUK_DISABLED=true` before pytest. The reaper
only cleans up abandoned containers after a crashed run; without it the
containers must be removed by hand if a run dies, which is an acceptable trade
locally. With it set, the same command goes from `171 passed, 13 errors` to
`184 passed`.

**Tried and rejected:** re-running (the Ryuk container id changes each time
and so does nothing); assuming the guards change had broken something, which
it had not -- the errors are all at *fixture setup*, before any test body
runs, and that is the tell. A stack trace that ends inside
`testcontainers/core/docker_client.py` is never about the code under test.
Worth remembering before debugging the wrong file for an hour.
