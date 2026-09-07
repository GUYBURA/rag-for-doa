# CLAUDE.md

Guidance for Claude Code working in this repository. Read before changing anything in
`ingest/`, `db/`, or `query/`.

## Project

A RAG system over Thai Department of Agriculture pesticide guidance handbooks (editions
2565, 2566, 2568 BE). Portfolio project — **design decisions must be explainable**, not just
working. When you make a non-obvious choice, leave a comment saying why.

Core requirement: never serve superseded regulatory guidance. Edition correctness outranks
answer quality.

## Where things go

Target layout. Not everything exists yet — create files here rather than inventing a
parallel structure.

```
db/
  schema.sql       # the 2 tables, views, promote_current_edition(). Source of truth for schema.
  migrations/      # every schema change, numbered. Never edit schema.sql without one.
ingest/
  extract.py       # PyMuPDF only. Raw text out, no cleaning.
  normalize.py     # the 5-step Thai pipeline. Nothing else normalizes text.
  qa_gate.py       # blocking checks. Writes document.qa. No bypass.
  chunk.py         # prose and table strategies live here, not in extract.py
  embed.py         # embedding + model version pinning
  promote.py       # scope-superset supersession. The only place that archives.
query/
  guards.py        # input (PII, injection) and output (PII, grounding) guards
  retrieve.py      # hybrid dense + sparse
  rerank.py        # top-5 + score threshold
  prompt.py        # prompt assembly. Citations are constructed here.
app/
  main.py          # Cloud Run service. HTTP only — no business logic.
eval/
  questions.yaml   # gold set
  run_eval.py
docs/              # diagrams, ARCHITECTURE.md assets
```

Placement rules that matter more than the tree itself:

- Archiving happens **only** in `promote.py`. If another module sets `status` or nulls
  `embedding`, that is a bug.
- Normalization happens **only** in `normalize.py`. No ad-hoc `.strip()` or regex cleanup in
  `extract.py` or `chunk.py`.
- `app/main.py` is a transport layer. Logic belongs in `query/`.
- Schema truth lives in `db/schema.sql`. Read it rather than trusting any summary, including
  this file.

## Invariants — do not break without asking

**1. Never order or compare editions by PDF metadata.**
`edition_year_be` comes from the Buddhist Era year printed inside the document. The 2565 PDF
was re-exported through iLovePDF in 2024, so its `CreationDate`/`ModDate` make it look newer
than 2566. Any code reaching for `doc.metadata['creationDate']` to decide currency is a bug.

**2. Supersession is decided by `scopes`, never by title, filename, or family.**
A supersedes B when `A.edition_year_be > B.edition_year_be AND A.scopes @> B.scopes`. The
2566 volume is insecticide-only from a *different publication series* than 2565/2568, so any
title- or family-based heuristic fails to retire it. Do not "simplify" this into a title
match.

**3. Archive, never delete.**
Retiring an edition means `status = 'archived'` and `embedding = NULL`. Rows, text, page
numbers and provenance stay. No `DELETE FROM document` or `DELETE FROM chunk` in application
code.

**4. `chunk.embedding` stays nullable and the HNSW index stays partial.**

```sql
CREATE INDEX ... USING hnsw (embedding vector_cosine_ops) WHERE embedding IS NOT NULL;
```

Deliberate: archived chunks are physically absent from the vector index rather than filtered
out of its results, which avoids the post-filtering recall problem in approximate search. Do
not add `NOT NULL`, do not drop the `WHERE` clause, and do not replace it with a
`WHERE status = 'active'` predicate in retrieval queries.

**5. Two tables. Keep it that way.**
The schema went 16 → 6 → 2 deliberately. Before adding a table, check whether a column on
`document`/`chunk` or a view does the job. Views and functions are fine; new entities need
justification.

**6. PyMuPDF only. Do not reintroduce pdfplumber.**
pdfplumber silently corrupts Thai combining marks on the 2568 file — reordered and dropped
vowels and tone marks. PyMuPDF extracts the same file with zero errors. This was measured,
not assumed. If PyMuPDF lacks something you need, raise it rather than swapping parsers.

**7. Never index text that hasn't been through `normalize.py`.** Order matters:
1. PUA tone marks `U+F700`–`U+F71A` → real Thai codepoints (the 2565 cover page has 225)
2. Unicode NFC
3. Strip headers/footers (they contain the edition year and leak into chunk text)
4. Strip page numbers and dot leaders
5. Collapse whitespace

`chunk.content` stores **raw** text; `chunk.content_sha256` hashes **normalized** text. Do
not conflate them — the hash is what catches cross-edition duplicates, and ~48% of 2568's
prose lines are identical to 2565's after normalization.

**8. The parsing QA gate is blocking.**
A document failing page-count, table/layout extraction, OCR confidence, or Thai
combining-mark checks does not get chunked or embedded. Write the failure into `document.qa`
and stop. Do not add a bypass flag.

**9. Answers must carry citations** — source document, edition year, page number. A change
that drops page provenance is not acceptable.

**10. Refusal is a valid output.** Below-threshold reranking → "no relevant document".
Failed grounding check → retry once, then refuse. Do not loosen thresholds or drop the
grounding check to make the system look more responsive.

## Commands

<!-- TODO: replace with real commands; keep this section accurate, the agent relies on it -->

## Conventions

- Schema changes go in `db/migrations/` **and** are reflected in `db/schema.sql`. Never edit
  `schema.sql` alone.
- Anything touching supersession, archiving, or the partial index needs a test proving
  archived chunks are unreachable *through the retrieval path* — not just that a status
  column changed.
- Embedding model version is pinned per document. Changing models means re-embedding the
  whole active set, not an in-place swap. Flag it rather than doing it.
- Thai test fixtures must include combining marks and at least one PUA case. ASCII fixtures
  pass while the real pipeline is broken.
- Prefer stdlib and the existing dependency set. New dependencies need a reason.
- Schema and layout live in `db/schema.sql` and the tree itself — read those rather than
  relying on a copy in this file.

## Note-taking

**KNOWLEDGE.md** — append when a design decision was non-obvious, or when you had to learn
something to make a choice. Not for routine work.

```
### Concept: [Topic]
**Definition:** ...
**Why it matters here:** ...
**Interview answer:** ...
```

**LOG.md** — append when a bug took more than one attempt to fix, or when you tried
something that didn't work and abandoned it. Record the dead ends too; the reasoning is the
point.

```
### Bug: [Description]
**Status:** Fixed / Open
**Date:** YYYY-MM-DD
**Cause:** ...
**Solution:** ...
**Tried and rejected:** ...
```

Append only. Never rewrite or delete earlier entries.

## When to stop and ask

- Any change to the archive mechanism, the partial index, or `promote_current_edition()`.
- Adding a table, or denormalizing across the two existing ones.
- Changing the parser, the normalization order, or the chunking strategy.
- Loosening the rerank threshold, grounding check, or QA gate.
- Anything that would make an answer citable to a page that doesn't contain the claim.