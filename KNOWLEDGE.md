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
