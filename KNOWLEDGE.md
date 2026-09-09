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
