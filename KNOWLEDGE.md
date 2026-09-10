# KNOWLEDGE.md

Concepts that were not obvious, and had to be learned before a choice could be made.
Append only.

### Concept: Canonical combining class, and why PUA repair must precede NFC

**Definition:** Every Unicode character carries a *canonical combining class* (ccc), an
integer. NFC uses it to sort runs of combining marks into one canonical order, so two
strings that render identically also compare and hash identically. Characters with ccc = 0
are "starters" — NFC never reorders them.

Thai has no precomposed forms, so NFC composes nothing in Thai text. Its entire contribution
to this pipeline is *reordering* stacked marks. Measured: NFC leaves `น + mai ek + sara i`,
`น + sara i + mai ek` and `น + sara am` all unchanged, while it composes Latin `e + U+0301`
into `é`.

**Why it matters here:** Private Use Area codepoints all carry ccc = 0. While a tone mark is
still `U+F718`, NFC sees a starter and has nothing to sort. Running NFC before the PUA repair
therefore does no work at all, and the marks keep whatever order the PDF happened to emit.

Minimal case — `น` followed by PUA sara u then PUA phinthu:

```
input        U+0E19  U+F718 (ccc=0)    U+F71A (ccc=0)
1 then 2     U+0E19  U+0E3A (ccc=9)    U+0E38 (ccc=103)   <- canonical
2 then 1     U+0E19  U+0E38 (ccc=103)  U+0E3A (ccc=9)     <- PDF order, not canonical
```

Fuzzing 20,000 random strings built from Thai, Latin and the 15 mapped PUA codepoints, the
two orders disagreed on 1,327 of them — 6.6%. Since `content_sha256` hashes normalized text,
each disagreement is a pair of visually identical lines that hash two different ways, so
dedup silently under-reports. Locked in by `test_pua_repair_must_run_before_nfc`.

The docstring originally justified the order as "NFC cannot compose a combining mark it does
not recognise". Close, but the mechanism is reordering, not composition — corrected in
`normalize.py`.

**Interview answer:** Normalization order in the Thai pipeline is load-bearing, and I can
show why with a two-character example. PUA glyphs have combining class zero, so NFC treats
them as starters and skips the reordering pass entirely. Repair the PUA first and NFC sorts
real Thai marks into canonical order; repair it second and you keep the PDF's arbitrary
order. Two lines that look the same then hash differently, which quietly breaks
deduplication. I fuzzed it: the orders differ on about 7% of mixed-script inputs.

### Concept: NFC does not unify nikhahit + sara aa with sara am

**Definition:** `U+0E4D` (nikhahit) followed by `U+0E32` (sara aa) renders identically to
`U+0E33` (sara am). The relationship is a *compatibility* decomposition, not a canonical one,
so NFC leaves the pair alone; only NFKC folds them together.

```
"น" + U+0E4D + U+0E32  vs  "น" + U+0E33
equal raw   False
equal NFC   False
equal NFKC  True
```

**Why it matters here:** `PUA_TO_THAI` maps `U+F700` to nikhahit. Any word the PDF encodes
that way produces the two-codepoint spelling, while the same word typed normally produces
`U+0E33`. Step 2 does not reconcile them, so `content_sha256` differs for text that is
visually identical. CLAUDE.md puts cross-edition prose overlap at ~48% after normalization;
this gap makes the real figure higher than measured.

**Not fixed yet, deliberately.** Switching step 2 to NFKC is too broad — it also folds
ligatures, full-width forms and superscripts, which would alter chemical names and dosage
notation. The narrow fix is to map the `U+0E4D U+0E32` sequence to `U+0E33` inside step 1,
where the PUA table already lives. Deferred until `survey_pua()` has been run over all three
source PDFs, since the decision needs a count of how often it actually occurs. Changing the
normalization pipeline requires sign-off per CLAUDE.md.

**Interview answer:** Two Thai spellings render identically but are different codepoints, and
NFC will not unify them because the decomposition is compatibility-level rather than
canonical. It matters because our PUA repair table produces one spelling and human typists
produce the other, so the dedup hash splits identical content. I did not reach for NFKC —
it is far too aggressive for a document full of chemical names and percentages. The narrow
fix belongs in the PUA mapping step, and I want occurrence counts from the real PDFs before
committing to it.

### Concept: A combining-mark ratio detects deletion, not reordering

**Definition:** The QA gate's Thai health check is
`count(combining marks) / count(Thai consonants)` over the whole document. Consonants are
the denominator because they are what a broken parser leaves untouched: dropped vowels and
tone marks shrink the numerator while the denominator stays fixed, so the signal is clean.
Using *all* characters as the denominator would make the ratio track how much ASCII a page
holds — chemical names, percentages, dosage tables — rather than how damaged the Thai is.

Measured on `data/raw/2568.pdf` (raw text, pre-normalize):

```
marks 53,483 / consonants 161,382 = 0.3314      per-page median 0.3267
lowest page p288 = 0.1863                        PUA marks 0
pages with zero Thai consonants: 299, 300 (the two blank pages)
```

Floor set at **0.10** — a 3.3x margin at document level. The floor is deliberately a
statement about the Thai language ("real Thai prose always carries marks"), not a fit to
2568, so it survives 2565 and 2566 arriving with different densities.

**Why it matters here:** the check is much weaker than it looks, and invariant 7 is the
reason to be precise about it. pdfplumber both *reordered and dropped* marks on 2568. A
ratio sees only the dropping half — reordering leaves the count identical, so a document
whose marks are all in the wrong order passes at any threshold.

Even for dropping, coverage is partial. Marks by class, and the ratio that survives if a
parser loses a whole class:

```
upper vowels (ั ิ ี ึ ื ํ)  48.6%  ->  0.170   passes a 0.10 floor
tone marks   (่ ้ ๊ ๋)      35.3%  ->  0.215   passes
lower vowels (ุ ู ฺ)        10.2%  ->  0.298   passes
```

Losing every upper vowel in the book — about as bad as Thai damage gets — clears the gate
comfortably. Only near-total loss (upper vowels *and* tone marks, 84% of all marks) drops to
0.053 and fails.

**Rejected: raising the floor to 0.20.** It would catch the upper-vowel case, but leaves
only a 1.65x margin, still passes lower-vowel loss, and 2566 comes from a different
publication series whose natural density is unmeasured. Buying one failure mode at the cost
of false-failing a healthy edition is a bad trade, and a threshold that gets nudged whenever
a document fails is not a gate.

**The right fix, deferred:** assert each mark *class* is present at all. Every genuine Thai
document contains upper vowels, lower vowels and tone marks; a class at zero is impossible
in real text and needs no tuned number. That is a separate check, not a different threshold.
Not added yet — 0.10 ships first, with the gap recorded here.

**Interview answer:** The gate checks combining marks per Thai consonant, denominated on
consonants because they are invariant under the failure being detected. I measured 0.33 on
the live document and set the floor at 0.10, treating it as a fact about written Thai rather
than a curve fit, so it generalizes to editions I have not ingested. What I want to be
honest about is its reach: it catches deletion, never reordering, and I broke the marks down
by class to show that even losing every upper vowel still clears the floor. Tightening the
threshold does not fix that — the real answer is a per-class presence check, which I scoped
as separate work rather than pretending one number covers it.

### Concept: The PUA is not one range, and symbol fonts hide real characters in it

**Definition:** Unicode's Private Use Area is U+E000–U+F8FF. `PUA_TO_THAI` and `PUA_RANGE`
cover only U+F700–U+F71A, the sub-range Thai fonts use for pre-positioned tone marks.
Legacy symbol fonts — SymbolMT, Wingdings — use U+F0xx instead, mapping their glyph at byte
`0xNN` to `U+F0NN`. PyMuPDF reports what the PDF encodes, so those glyphs arrive as PUA
codepoints too, and a scan limited to the Thai sub-range reports zero.

Measured across all three editions:

```
2568  593 PUA total    0 in F700-F71A    F07E x586, F061 x3, F072 x3, F022 x1
2565  233 PUA total  225 in F700-F71A    F061 x3, F072 x3, F022 x1, F097 x1
2566    0 PUA
```

The project's working assumption was "2568 has no PUA". It has 593. The number came from
`survey_pua()`, which is bounded by `PUA_RANGE`.

Identified by rendering each glyph from the PDF at 8x rather than by reading a font table:

```
U+F061  SymbolMT     α   "กลุ่มเคมี α-Chloroacetamides"      -> part of a chemical group name
U+F072  Wingdings3   △   used as Δ in "Δ14-reductase"        -> FRAC mode-of-action text
U+F022  Wingdings3   →   "Δ8→Δ7-isomerase"                    -> same sentence
U+F07E  SymbolMT     ∼   flanks headings, x586                -> decoration
U+F097  Wingdings2   (renders blank), x1
```

**Why it matters here:** α, Δ and → are content. They sit inside chemical group names and
FRAC mode-of-action descriptions — exactly the text a question about a pesticide's mode of
action would have to retrieve. Today they reach `chunk.content` as U+F061 and friends, so
they are unsearchable, they cannot render, and they change `content_sha256` for lines that
are otherwise identical across editions.

The QA gate does not catch this. `unmapped_pua_codepoints` scans `PUA_RANGE`, so a document
with 593 unrepaired PUA characters passes cleanly. The check is narrower than its name
suggests.

**Not fixed yet.** Widening the range touches both `normalize.py` step 1 and the gate, which
CLAUDE.md gates behind sign-off, and the mapping is not a single table: U+F061 means α only
because the span's font is SymbolMT. The same codepoint under a different font is a
different character, so a font-blind table would be wrong by construction. Any fix has to
read the span font, which means `extract.py` would have to carry font information it does
not carry today — a change to the Page contract, not a lookup table.

**Interview answer:** I found 593 private-use codepoints in a document our tooling reported
as having none, because the survey was scoped to the Thai tone-mark sub-range while the file
also embeds SymbolMT and Wingdings. I identified them by rendering the glyphs out of the PDF
rather than trusting a font chart, and three of them are semantic — alpha, delta and an
arrow inside chemical group names and FRAC mode-of-action text. The fix is not a bigger
lookup table: the same codepoint means different things under different fonts, so a correct
repair needs font context from the extraction layer, which changes the Page contract. I
recorded it and left the pipeline alone rather than guessing a mapping into chemical names.

### Concept: A font stores one tone mark at several codepoints, chosen by the glyph underneath

**Definition:** THSarabunPSK does not render a Thai tone mark from one PUA codepoint. It
keeps several *positional variants* of the same mark — one drawn for a plain consonant, one
raised or shifted for a consonant with an ascender (ป ฟ ฝ ล) or an upper vowel already in
the slot — and the PDF producer emits whichever variant it drew. So U+F702, U+F706 and
U+F70B are three codepoints that all mean ้ (mai tho).

**Why it matters here:** the QA gate blocked 2565 on
`unmapped_pua_codepoints = [U+F706, U+F708, U+F70A, U+F70B]` — 174 of the 225 PUA characters
in the file. `PUA_TO_THAI` had the F701–F705 block and F70E, so the whole positional-variant
block was missing and `restore_pua_tone_marks()` would have raised on every real page of the
document. The gate did exactly its job: the failure surfaced before anything was chunked.

Identified by rendering the glyph out of the PDF and reading the word it produces, never
from a font chart:

```
U+F70A x85  ส<pua>งออก  -> ส่งออก     ่  U+0E48
U+F70B x71  ใช<pua>สาร  -> ใช้สาร     ้  U+0E49
U+F706 x15  ป<pua>องกัน -> ป้องกัน     ้  U+0E49   (follows ป, อ — ascenders)
U+F708 x3   ปุ<pua>ย    -> ปุ๋ย        ๋  U+0E4B
```

F708's isolated clip rendered blank, and all three occurrences sit on page 220, the page that
extracts with reversed lines. It was resolved from the reversed context instead: the raw run
`ย<pua>ุป` read backwards is `ปุ<pua>ย`, and the only Thai word there is ปุ๋ย.

**Deliberately not filled in:** F707, F709, F70C, F70D. They complete the block by symmetry,
but they occur in none of the three editions, so there is no evidence for what they are.
Guessing would put an unverified tone mark inside a pesticide name; leaving them out means
the gate blocks loudly the first time one appears. A wrong tone mark is a different, still
valid Thai word — the failure would be invisible downstream.

**Interview answer:** the gate blocked an edition on four unmapped private-use codepoints. I
identified all four by rendering the glyphs and reading the resulting words, not by trusting
a font table, and the answer was that the font keeps positional variants of the same tone
mark at several codepoints. I mapped the four I had evidence for and left the four I did not,
because the cost of a wrong mapping is a silently different Thai word and the cost of a
missing one is a loud failure I already know how to read.


### Concept: Three independent books agreeing tightens a threshold that one book could not

**Definition:** `combining_ratio_min` is a floor on Thai combining marks per Thai consonant.
It was set at 0.10 when 2568 was the only measurement — a 3.3x margin, chosen wide because a
single document cannot tell you how much a *different* document legitimately varies.

**Why it matters here:** all three volumes now measure within 1% of each other.

```
2568  0.3314    2565  0.3345    2566  0.3324
```

2566 is insecticide-only from a different publication series, so this is not one house style
measured three times. The spread is a property of written Thai, not of one book, which is
what a threshold documented as a language fact needs in order to be one. Raised to **0.20**:
still 1.66x margin on every observed document, and now above 0.170 — the score a book gets
after losing every upper vowel, which 0.10 passed. That failure mode is the exact damage
pdfplumber caused on 2568 (invariant 7), so it is the one worth catching.

Unchanged: this catches deletion only. Reordered marks leave the count identical.

**Interview answer:** I set a parse-quality floor at 3.3x margin from a single measurement,
then tightened it to 1.66x once two more documents from a different publication series landed
within 1% of the first. The point of the second number was that the tighter floor is above
the score a document gets when it loses an entire class of vowels — with the loose floor,
the exact corruption the check exists to catch would have passed.

### Concept: A table finder and a text extractor disagree about where a Thai vowel is

**Definition:** PyMuPDF's `find_tables()` returns geometry *and* text, and the two
come from different code paths. `page.get_text("text")` reads the page's own
content stream in reading order. `table.extract()`, `table.to_markdown()` and
`table.header.names` reconstruct cell text by grouping spans on vertical
position, because that is how you tell one table row from the next.

Thai breaks that assumption. A below-vowel like ู renders lower than the
consonant it belongs to, so a position-based grouper reads it as its own line.

**Why it matters here:** measured on 2568 page 56, one table, three methods:

```
page.get_text("text")   combining ratio 0.3267   'ศัตรูพืช'
table.extract()         combining ratio 0.2796   'ศัตรพชื\nู'
table.to_markdown()     combining ratio 0.0432   marks mostly gone
```

The dosage tables are the highest-value text in the corpus — they carry the
application rates — so reading them through the lossy path would corrupt exactly
the passages a user is most likely to act on.

This is the same failure as invariant 7, where pdfplumber was measured against
PyMuPDF and rejected for reordering and dropping Thai marks on the same file. It
is worth naming as a class rather than an incident: **any layout algorithm that
infers reading order from vertical position is unsafe for Thai**, and the tool's
own name on the box is not evidence that it is safe.

**Solution:** use `find_tables()` for geometry only — the table bbox and each
cell's rectangle — and read every cell with
`page.get_text("text", clip=cell_rect)`, the extractor that was already
validated. Verified across the corpus: the concatenated markdown of all tables
scores 0.3338 / 0.3297 / 0.3281 for 2565 / 2566 / 2568, against raw-page ratios
of 0.3345 / 0.3324 / 0.3314.

**Two things a ratio cannot see.** `table.extract()` still scores 0.2796, above
the 0.20 gate floor, because the marks are all still present — just attached to
the wrong characters. Deletion moves a ratio; displacement does not. The
regression test therefore asserts the corrupted spelling `ศัตรพชื` is absent as
well as the correct `ศัตรูพืช` being present. Confirmed by mutation: switching
the implementation to `table.extract()` leaves the ratio test green and only the
spelling test red.

**Interview answer:** PyMuPDF finds tables well and reads their text badly, at
least in Thai, because cell text is reassembled from vertical position and a Thai
below-vowel sits on its own baseline. I measured three extraction paths on the
same table and got combining-mark ratios of 0.33, 0.28 and 0.04, then used the
finder for geometry and the trusted text extractor for content, which brought the
whole corpus back in line with the raw pages. The part I would want a reviewer to
notice is that the ratio check alone would not have caught the middle case: the
marks were all there, on the wrong letters, so the test had to name the corrupted
word.

### Concept: On a page built around a table, the page text and the table are the same text

**Definition:** A PDF page has no notion of "the table" and "the prose around
it". `get_text("text")` returns every character on the page in reading order,
including everything printed inside the table's ruled lines. A table extractor
returns those same characters a second time, arranged as cells. Chunk the page
*and* chunk the table and the corpus now holds each recommendation twice.

**Why it matters here:** measured on 2568, the share of a page's characters that
sit inside a table bounding box:

```
page 56   1,609 chars total   1,457 inside tables   91%
page 60   1,069                 960                 90%
page 100  1,916               1,648                 86%
```

So this is not a small overlap to be tidied up later — on a table page the table
*is* the page, and the leftover 9–14% is the crop heading, a spraying note, the
page number and the running footer.

Two copies would not be caught by anything downstream. `content_sha256` is
computed over the text, and the flat page text and the markdown rendering of the
same table differ in every pipe and line break, so `chunk_dedup_idx` sees two
distinct rows. Both then sit in the same vector neighbourhood and compete for
the same top-5 slots, which halves the effective diversity of a retrieval that
is already limited to five passages.

**Solution:** a page contributes its tables as markdown, plus only the blocks
whose rectangle overlaps no table rectangle. That needs geometry, which is why
`extract.py` grew `Block(page_number, bbox, text)` alongside `Page.raw_text` —
`raw_text` is the same text with the coordinates thrown away, and the
coordinates are exactly what this decision needs.

**A table part without its header row is worse than no chunk at all.** 2568's
tables run to 3,358 characters against a 1,800-character budget, so long ones
are split — at row boundaries, never mid-row, and every part repeats the header
row and its separator. Without that, the second part of a dosage table reads:

```
|โรคใบจุด|แมนโคเซบ 80% WP|40 กรัม|
```

and nothing in it says whether `40 กรัม` is the application rate, the amount of
active ingredient, or an LD50 value. The chunk still looks like a complete,
citable recommendation, and the citation still points at the right page. That is
the failure mode this corpus punishes hardest: not a missing answer, a confident
wrong one that survives review.

Measured after splitting: 276 tables in 2568 become 376 parts, longest 2,148
characters. The parts over budget are single rows longer than the budget, which
are emitted whole on purpose.

**Section comes from geometry too, and only from the same page.** The crop name
("มันสำปะหลัง (Cassava)") is printed above the table, outside it. It is found by
taking the blocks whose bottom edge is above the table's top edge and walking
*upward* — nearest first. Page 56 carries three tables and three crops; walking
downward instead gives all three tables the first crop's name, which is a silent
wrong answer rather than a crash.

Roughly half the dosage tables get no section, because they continue from the
previous page and carry no heading of their own. Carrying the last-seen heading
forward would cover them — and would eventually attach the wrong crop to a
table. A missing section is visibly missing; a wrong one reads as fact.

**Interview answer:** the obvious way to chunk a document with tables is to chunk
the text and chunk the tables. I measured first and found that 86–91% of the text
on a table page is *inside* the tables, so that approach indexes the important
content twice in two different shapes that no deduplication catches. I used the
block geometry to take the tables plus only the text outside them. The rule I
would defend hardest is that every part of a split table repeats the header row:
without it, a chunk of dosage numbers still reads like a valid recommendation
with no way to tell an application rate from a toxicity class — and a confident
wrong answer with a correct-looking citation is the worst output this system can
produce.

### Concept: A schema can enforce an invariant and still let you violate it, if nothing ever calls the enforcement

**Definition:** `assert_no_stale_chunks` and `promote_current_edition()` were both already in
the schema before this session — the SQL that archives a superseded edition and deletes its
chunks was correct and tested at the database level. What was missing was any Python code
that ever set `document.status = 'active'` in the first place. `promote_current_edition()`
only archives documents that are *already* active; nothing promoted a newly-ingested,
newly-passing document to active at all.

**Why it matters here:** wiring `chunk.py` and `embed.py` into `run.py` made this visible
immediately. The moment a document passed the QA gate and its chunks were written, running
`assert_no_stale_chunks` — the view whose whole job is to return zero rows — returned one.
Not because the archival logic was wrong, but because a `pending` document with chunks
attached is itself a state invariant 4 doesn't allow: "everything in `chunk` belongs to an
active edition by construction." A document that never becomes active has no business having
rows in `chunk` at all, however briefly.

**Solution:** `ingest/promote.py`, one function, `promote_new_document()`: set the new
document active, then call `promote_current_edition()` — in that order. Order is load-bearing
and it is the kind of thing a passing test suite would not have caught without a specific
test for it: `promote_current_edition()`'s coverage check only considers `active` and
`archived` documents when deciding whether an older edition is now fully covered. Call it
before the new document is marked active, and the new document is invisible to its own
coverage check — the older edition it was meant to retire never gets archived, on this call
or any other, because nothing else ever promotes anything.

Verified by mutation: swapping the two calls in `promote_new_document()` turns a
should-archive test red without touching the SQL function at all.

**The broader point:** a correct, tested SQL function is not the same claim as "the system
enforces this invariant." A `CREATE FUNCTION` that nothing calls, or that only some code
paths call, enforces nothing. The place to check an invariant end to end is the same place
that would notice if it silently stopped holding — here, that was `assert_no_stale_chunks`
run right after a real ingest, not a unit test of `promote_current_edition()` in isolation.

**Interview answer:** I found a gap between "the schema has the right constraint" and "the
system upholds it" — the archival function and its assertion view were both already correct,
but nothing in the ingestion code ever called the function that would have made a document
active in the first place, so a passing document's chunks were technically visible to
retrieval before anything decided the document was live. I fixed it with a single
promotion function and, more importantly, added the test that would have caught the ordering
bug I initially wrote into it — reversing which SQL statement runs first breaks the coverage
check silently, with no error, just a supersession that never happens.

### Concept: A hybrid retrieval library can silently downgrade to dense-only
**Definition:** `PGVectorStore.create_sync()` validates the `tsv_column` named
in `HybridSearchConfig` against `information_schema.columns`, and if it is
missing or not a `tsvector`, it does not raise — it blanks
`hybrid_search_config.tsv_column` to `""` on the object it was handed and
returns a store that runs dense search only. Every call after that succeeds
and returns plausible-looking results.
**Why it matters here:** `chunk.content_tsv` exists and is a real `tsvector`,
so this exact failure mode does not fire today — but a typo'd column name, a
migration that renames it, or reusing this code against a table that lacks it
would all produce a working-looking retrieval path with silently no lexical
half. Caught by reading the config back after `create_sync()` returns and
raising if `tsv_column` isn't what was asked for — `make_store()` does this as
`assert_hybrid_enabled()`, and `test_a_blanked_tsv_column_is_an_error_not_a_silent_downgrade`
proves the check actually fires on a blanked config.
**Interview answer:** "The library's failure mode for a bad hybrid config
isn't an exception, it's degradation — the object it hands back looks
identical whether hybrid search is on or off. I read the config back after
construction and raise if the column I asked for isn't the column I got,
because silent degradation to dense-only is worse than crashing at startup."

### Concept: A shared config object across independent calls is stateful by accident
**Definition:** `langchain-postgres`'s hybrid search writes the caller's
question into `hybrid_search_config.fts_query` when that field is empty, and
the write lands on the same object the caller passed in. A config built once
and reused across searches keeps whatever `fts_query` the *first* search gave
it — every later search's dense half tracks the new question correctly while
its lexical half silently re-runs the first question's keywords.
**Why it matters here:** Nothing about this raises or looks wrong from the
call site; a fixed threshold or fixed test question would never catch it,
because most retrieval quality signal still comes from the dense half. Fixed
by building a fresh `HybridSearchConfig` per call (`hybrid_config()` in
`query/retrieve.py`) and never storing one on the vector store or reusing one
across questions. `test_the_second_question_is_not_searched_with_the_first_questions_words`
spies on the config actually handed to `similarity_search_with_score` across
two consecutive questions and asserts the second call's `fts_query` matches
the second question, not the first — this failed with a clear rank mismatch
under mutation (config built once, reused) while every other test in the file
still passed, which is exactly the "looks fine, isn't" shape this class of bug
takes.
**Interview answer:** "A library handed a mutable config object may treat it
as a place to stash state between calls, not just read from. I build one per
call and never share it, and the regression test that catches this is a spy on
two consecutive calls, not a single-call assertion — a single call can't tell
correct behaviour from lucky behaviour."

### Concept: RRF/weighted-sum fusion scores are not a relevance gate
**Definition:** Both fusion strategies `langchain-postgres` ships normalize
within one result set — weighted-sum min-maxes scores so the best result of
*any* search is 1.0, and reciprocal rank fusion is a function of rank alone,
with no term for whether row 1 is actually relevant to the question. A search
that finds nothing relevant produces the same shape of score distribution as
one that finds the exact answer.
**Why it matters here:** Invariant 11 requires "below-threshold reranking ->
refuse", and a threshold set against a fusion score would never fire, because
a bad search's top score looks identical to a good search's top score. This is
why `Passage.fusion_score` exists but `retrieve()` never compares it to
anything — the threshold has to come from a reranker that judges one passage
against one question in absolute terms (`rerank.py`, not yet built). Confirmed
against the real corpus: `eval/run_eval.py`'s `q12_out_of_domain` (a Thai food
recipe question against a pesticide corpus) and `q13_fabricated_active_ingredient`
(an invented chemical name) both return a full top-5 candidate list with
ordinary-looking fusion scores — there is nothing in retrieval's own output
that flags either as unanswerable.
**Interview answer:** "Fusion scores answer 'which of these results is best',
not 'is any of these results good' — they're relative by construction. The
threshold that lets the system refuse has to come from a reranker that scores
a passage against a question on an absolute scale, so I keep retrieval's score
around for debugging but never gate on it."

### Concept: Read the installed library's own source before designing against it
**Definition:** Before writing `query/retrieve.py`, the plan for it came from
reading `.venv/Lib/site-packages/langchain_postgres/v2/*.py` at the exact
pinned version (`0.0.17`) directly — `async_vectorstore.py`,
`hybrid_search_config.py`, `engine.py` — rather than from its README or from
prior knowledge of what a "hybrid search library" is expected to do. Three of
the four design questions asked before writing any code (the reranker choice
aside) came directly out of that reading: the `plainto_tsquery`/newmm mismatch,
the shared-config `fts_query` mutation, and `create_sync()`'s silent
`tsv_column` blanking are all specific to what this version of this library's
code actually does on this line, not properties of "hybrid search" in general.
**Why it matters here:** All three would have shipped invisibly. Every one of
them produces output that looks like a working hybrid search — correct-shaped
`Document` objects, plausible scores, no exception — and differs only in
*which* rows come back and in what order. A design based on the library's
advertised behavior (`HybridSearchConfig` exists, therefore hybrid search
works) would have written `retrieve.py` in under an hour and shipped all three
bugs with it; none of CLAUDE.md's invariants would have caught them, because
none of them are about retrieval correctness at the library-integration level
— they assume retrieval sees what `chunk` actually holds. The asymmetry is
what makes the reading worth doing: a wrong assumption about a pinned
dependency costs the same to find via source-reading before writing code as it
does via a failing eval after ingesting the whole corpus and running real
questions against it, except the second way costs an embedding budget and a
debugging session that starts from "why is recall bad" with no leads.
**Interview answer:** "For a pinned third-party dependency doing something
load-bearing, I read its actual installed source at the version I'm on before
designing around it, not its docs and not my prior assumptions about what that
category of library does. The three retrieval bugs in this project were all
found this way, before a single test was written, each because a specific
line does something the docstring doesn't mention — a config mutated in
place, a validation that downgrades instead of raising. All three would have
passed a design review based on the public API alone."
