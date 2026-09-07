# Architecture

Technical detail and design rationale for the Thai Pesticide Guidance RAG system. For a
plain-language overview, see [README.md](README.md).

---

## Design premise

The corpus is a multi-edition regulatory document set. Retrieving the wrong edition is not a
worse answer, it is a wrong answer — a withdrawn chemical presented as current guidance.
Most of the engineering here is about making superseded content structurally unreachable
rather than merely deprioritised.

Three findings from inspecting the source files drove the design.

### 1. PDF metadata cannot order editions

The 2565 PDF was re-exported through iLovePDF in 2024, so its `CreationDate` and `ModDate`
are newer than the 2566 edition's. Any pipeline sorting on file metadata will serve
three-year-old guidance as current, and will do so with no visible error.

`document.edition_year_be` is parsed from the Buddhist Era year printed inside the document.
File metadata is recorded for provenance but never used for ordering.

### 2. Supersession is a scope relation, not a title relation

The 2566 volume is insecticide-only and belongs to a different publication series from the
2565/2568 compendia — different title, different format, different numbering. Title
similarity, filename patterns and "document family" heuristics all fail: 2566 resembles
neither compendium, so a title-driven rule would never retire it, despite 2568 fully
covering its subject matter.

Supersession compares a `scopes text[]` array instead:

```sql
-- A supersedes B
A.edition_year_be > B.edition_year_be
AND A.scopes @> B.scopes
```

`{fungicide, insecticide, herbicide} @> {insecticide}` is true, so 2568 retires 2566 across
the series boundary. The rule is about coverage, which is what supersession actually means,
rather than about naming, which is a proxy that happens to fail on this corpus.

### 3. Query-time filtering is the wrong layer for archival

A `WHERE is_archived = false` predicate still leaves stale vectors inside the HNSW index.
Approximate search visits them, the filter discards them afterwards, and recall for the
current edition degrades — the standard post-filtering problem in ANN search. With ~48% of
2568's prose duplicated from 2565, a large fraction of the index would be archived
near-duplicates competing for the same candidate slots.

Instead, `chunk.embedding` is nullable and the HNSW index is partial:

```sql
CREATE INDEX chunk_embedding_hnsw
  ON chunk USING hnsw (embedding vector_cosine_ops)
  WHERE embedding IS NOT NULL;
```

Archiving a document nulls its embeddings, so those chunks become **physically absent from
the vector index** rather than filtered out of its results. The rows, their text, page
numbers and provenance survive, keeping the archive auditable and allowing an edition to be
re-embedded if it ever needs to return. Correctness is enforced by the schema rather than by
remembering a predicate in every query.

---

## Data model

Two tables. Earlier drafts had sixteen, then six; the reductions removed speculative
normalization for queries the system never runs.

### `document` — one row per source PDF

| Column | Type | Purpose |
|---|---|---|
| `document_id` | PK | |
| `source` | text | Original filename |
| `title_th` | text | Display title shown in citations |
| `edition_year_be` | int | Buddhist Era year **read from inside the document** |
| `scopes` | `text[]` | Subset of `{fungicide, insecticide, herbicide}`; drives supersession |
| `status` | text | `pending` / `active` / `archived` |
| `file_hash` | text | Duplicate upload detection |
| `page_count` | int | QA cross-check against extraction |
| `parser` | text | Which extractor produced the text |
| `qa` | jsonb | Parsing QA gate results |
| `ingested_at` | timestamptz | |

### `chunk` — one row per retrievable unit

| Column | Type | Purpose |
|---|---|---|
| `chunk_id` | PK | |
| `document_id` | FK → `document` | |
| `content` | text | Raw extracted text, un-normalized |
| `content_sha256` | text | Hash of the **normalized** text; cross-edition dedup |
| `page_number` | int | So citations point at a real page |
| `section` | text | Heading the chunk sits under |
| `metadata` | jsonb | Domain fields lifted from tables (crop, pest, active ingredient, rate) |
| `embedding` | vector, **nullable** | `NULL` ⇒ archived and out of the index |
| `created_at` | timestamptz | |

Storing raw `content` while hashing normalized text is deliberate. Roughly **48% of 2568's
prose lines are byte-identical to 2565's** after normalization; without a normalized hash the
index fills with near-duplicates differing only in invisible whitespace and combining-mark
ordering. Keeping `content` raw preserves what the PDF actually said, which matters when a
citation is disputed.

### `promote_current_edition()`

Runs the scope-superset comparison, flips superseded documents to `archived`, and nulls
their embeddings. Must remain transactional — a half-promoted state where two editions are
simultaneously queryable is the worst possible outcome for this system.

---

## Ingestion pipeline

```
PDF upload → Cloud Storage → Eventarc → Workflows → Cloud Run Job
                                                        │
        ┌───────────────────────────────────────────────┘
        ▼
  file hash check → parse → parsing QA gate → chunk → embed + index
        │                        │                          │
   (duplicate → stop)      (fail → stop)          promote_current_edition()
```

**File hash check.** `document.file_hash` rejects re-uploads of identical bytes before any
parsing cost is incurred.

**Parsing.** PyMuPDF, with layout and table extraction and an OCR fallback for scanned
pages. pdfplumber was evaluated and rejected — see [Thai text handling](#thai-text-handling).

**Parsing QA gate.** Blocking. A document does not enter the index until it passes:

| Check | Failure means |
|---|---|
| Page count matches PDF | Extraction dropped pages |
| Tables extracted | Recommendation tables lost — the highest-value content |
| Layout extraction succeeded | Column/section structure lost |
| OCR confidence above threshold | Scanned page transcribed unreliably |
| Thai combining marks intact | Silent text corruption |

Results are written to `document.qa`, so a failure is inspectable rather than silent. There
is no bypass flag: a document that fails is `pending` with a recorded reason, not `active`
with a warning.

**Chunking.** Size and overlap are tuned separately for Thai prose and for tabular
recommendation entries, which behave very differently — prose rewards overlap, table rows
are self-contained and are fragmented by it.

**Indexing.** Dense and sparse representations. The embedding model version is pinned per
document so a model upgrade cannot silently mix vector spaces; changing models means
re-embedding the whole active set.

**Promotion.** `promote_current_edition()` archives superseded editions in one transaction.

---

## Query pipeline

```
User → Input Guard → Embed → Hybrid Retrieval → Re-rank → Prompt Build → LLM
       (PII,          (dense +   (pgvector)     (top 5,   (cite source,
        injection)     sparse)                   score ≥    edition, page)
                                                 threshold)
                                                     │
                                                     ├─ nothing above threshold
                                                     │  → "no relevant document"
                                                     ▼
                                          Output Guard (PII, grounding)
                                                     │
                                          retry once, else refuse
```

**Hybrid retrieval** combines dense vectors with sparse lexical matching. Thai pesticide
names and active ingredients are largely transliterated or Latin-script chemical names, which
dense embeddings handle poorly and lexical matching handles well.

**Re-ranking** keeps the top 5 above a score threshold. If nothing clears it, the pipeline
returns "no relevant document" rather than passing weak context to the model.

**Citations** carry source document, edition year and page number, so a user can open the
original PDF and verify. Page provenance is a hard requirement of prompt assembly.

**Output guard** checks PII and grounding — whether each claim is supported by the retrieved
passages. On failure the generation is retried once, then refused. Refusal is a first-class
outcome, not an error path.

---

## Thai text handling

Thai PDFs fail in ways Latin-script PDFs do not. Every issue below was found by inspecting
the actual files.

### Parser selection

pdfplumber corrupts Thai combining marks on the 2568 file — vowels and tone marks reordered
or dropped, producing text that looks plausible at a glance and is wrong. PyMuPDF extracts
the same file with zero combining-mark errors. This was measured on the real corpus, not
inferred from documentation.

### Normalization pipeline

Order matters:

1. **Private Use Area tone marks → real Thai.** Some embedded fonts map tone marks into
   `U+F700`–`U+F71A`. The 2565 cover page alone contains 225. Left alone they match nothing
   and are invisible in a diff.
2. **Unicode NFC.** Canonicalizes vowel and tone-mark ordering so visually identical strings
   compare and hash equal.
3. **Strip headers and footers.** These contain the edition year, which otherwise leaks into
   chunk text and pollutes retrieval — a query about 2568 matching a 2565 chunk on its
   running header.
4. **Strip page numbers and dot leaders.**
5. **Collapse whitespace.**

Steps 1 and 2 must precede any hashing or comparison; steps 3–5 must precede chunking.

---

## Deployment

| Concern | Service |
|---|---|
| Object store | Cloud Storage |
| Ingestion orchestration | Eventarc → Workflows → Cloud Run Jobs |
| Query API | Cloud Run Service |
| Database and vectors | Cloud SQL for PostgreSQL + pgvector |
| Embeddings, reranking, generation | Vertex AI |
| Input/output guardrails | Model Armor |
| Auth | Identity Platform + IAM |
| Observability | Cloud Logging, Cloud Trace |

Ingestion runs as a job rather than a service because it is bursty, long-running and
tolerant of latency; the query path runs as a service because it is the opposite.

---

## Evaluation

The gold set targets the failure modes this design exists to prevent:

| Dimension | What it tests |
|---|---|
| Edition correctness | Questions whose answer changed between 2565 and 2568 must return the 2568 value |
| Supersession | Questions answerable from 2566 must be served from 2568 and must never cite the archived volume |
| Abstention | Questions with no support in the current edition — refusing is correct |
| Citation validity | Every cited page exists and contains the claim |
| Retrieval quality | recall@k and MRR against labelled chunks |

Abstention and citation validity are weighted deliberately. A system that answers everything
confidently scores well on conventional RAG benchmarks and is unusable for regulatory
guidance.

---

## Known limitations

- **Table extraction quality varies by edition.** Some recommendation tables still need
  manual verification after the QA gate passes.
- **Uniform chunking.** Recommendation tables would likely benefit from a row-per-chunk
  strategy distinct from prose, but this is not yet implemented.
- **Only full supersession is modelled.** A new edition covering a *subset* of an old one
  would require chunk-level rather than document-level archival. No such case exists in the
  current corpus, so it is deliberately unbuilt.
- **No feedback loop.** Evaluation is offline against a fixed gold set; there is no
  mechanism for user corrections to reach the index.
- **Single-language.** Queries and corpus are Thai only. Latin-script chemical names work
  incidentally through lexical matching, not by design.