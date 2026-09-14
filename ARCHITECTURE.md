# Architecture

Technical detail and design rationale for the Thai Pesticide Guidance RAG system. For a
plain-language overview, see [README.md](README.md).

---

## Design premise

The corpus is a multi-edition regulatory document set. Retrieving the wrong edition is not a
worse answer, it is a wrong answer — a withdrawn chemical presented as current guidance.
Most of the engineering here is about making superseded content unreachable by construction
rather than by a filter someone has to remember to apply.

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

Supersession is decided on coverage. A document is archived once every scope it covers is
covered by at least one newer document:

```sql
NOT EXISTS (
  SELECT 1 FROM unnest(d.scopes) AS u(scope)
  WHERE NOT EXISTS (
    SELECT 1 FROM document n
    WHERE n.edition_year_be > d.edition_year_be AND u.scope = ANY (n.scopes)
  )
)
```

This is union coverage across newer editions, not single-document superset: three newer
single-scope volumes together retire one older compendium, which is what supersession
actually means. It also crosses the series boundary correctly — 2568 retires 2566 despite
looking nothing like it.

### 3. Query-time filtering is the wrong layer for archival

A `WHERE status = 'active'` predicate still leaves superseded vectors inside the HNSW index.
Approximate search visits them, the filter discards them afterwards, and recall for the
current edition degrades — the standard post-filtering problem in ANN search. With ~48% of
2568's prose duplicated from 2565, a large share of candidate slots would go to archived
near-duplicates before filtering ever ran.

So archived editions have no chunks at all. `promote_current_edition()` flips the document's
status and deletes its rows from `chunk` in one transaction. There is no
filtered-but-present state, which means nothing to get wrong at query time and no predicate
any retrieval path can forget.

**Why deleting is safe here.** The audit trail lives on `document`, which is never deleted:
`title_th`, `edition_year_be`, `scopes`, `page_count`, `chunk_count`, `qa`, `ingested_at`,
`archived_at` and `file_hash` all survive archival. The source PDF stays in Cloud Storage.
Together these make re-ingestion deterministic — `file_hash` proves the same bytes,
`chunk_count` proves the same result — so recovering an archived edition costs a re-parse
and re-embed, not information. Keeping archived chunks in the same table the system searches
would trade that modest cost for a permanent correctness risk.

An earlier design kept archived chunks with `embedding = NULL` behind a partial HNSW index.
It worked, but it made `chunk` a table with business rules embedded in its constraints,
which fought the vector-store library that reads it. Deleting is simpler and fails louder.

---

## Data model

Two tables. Earlier drafts had sixteen, then six; the reductions removed speculative
normalization for queries the system never runs.

### `document` — one row per volume, never deleted

| Column | Type | Purpose |
|---|---|---|
| `document_id` | uuid PK | |
| `source` | text | Original filename |
| `title_th` | text | Display title shown in citations |
| `edition_year_be` | smallint | Buddhist Era year **read from inside the document** |
| `scopes` | `text[]` | Non-empty subset of `{fungicide, insecticide, herbicide}`; drives supersession |
| `status` | text | `pending` / `active` / `archived` |
| `file_hash` | char(64) | Duplicate upload detection; proves re-ingest identity |
| `page_count` | int | QA cross-check against extraction |
| `chunk_count` | int | As of last ingest; survives archival, verifies re-ingest |
| `parser` | text | Which extractor produced the text |
| `embedding_model` | text | Pinned per document |
| `qa` | jsonb | Parsing QA gate results |
| `ingested_at` | timestamptz | |
| `archived_at` | timestamptz | Set by `promote_current_edition()` |

Uniqueness is `(edition_year_be, source)`, not the year alone: the corpus has two
publication series, so one year can carry more than one volume.

### `chunk` — retrievable passages of active editions only

Column names match what LangChain's `PGVectorStore` expects, so it attaches to this table
directly with no subclass. The DDL is ours.

| Column | Type | Purpose |
|---|---|---|
| `langchain_id` | uuid PK | |
| `content` | text | Normalized text — the exact string that is hashed and embedded |
| `embedding` | vector(768) NOT NULL | |
| `document_id` | uuid FK → `document` | |
| `page_number` | int | So citations point at a real page |
| `section` | text | Heading the chunk sits under |
| `content_sha256` | char(64) | Hash of the **normalized** text; unique per document |
| `langchain_metadata` | jsonb | Domain fields lifted from tables (crop, pest, active ingredient, rate) |
| `content_tsv` | tsvector | Sparse half of hybrid retrieval |
| `created_at` | timestamptz | |

`content` and `content_sha256` are deliberately the same string: normalized text, computed
once in `chunk.py`. Raw text never reaches `chunk`. Two reasons. The running footer that
normalization strips carries the edition year, and a chunk embedded with "2568" baked into
it would stay findable by edition after 2568 is superseded. And roughly **48% of 2568's
prose lines are byte-identical to 2565's** after normalization, so a hash of anything other
than what is embedded would make the dedup index compare the wrong thing. What the PDF
literally said is preserved by the source file itself, identified by `document.file_hash`.

(An earlier version of this document said `content` stored raw text. It never did in the
implementation; `CLAUDE.md` invariant 8 is the authority.)

`content_tsv` is populated by the ingest pipeline from PyThaiNLP `newmm` output rather than
by a generated column: Postgres has no Thai word-boundary parser, so tokenization happens in
Python and lands here pre-segmented.

### `promote_current_edition()`

Runs the coverage comparison, flips superseded documents to `archived`, and deletes their
chunks. Must remain transactional — a half-promoted state where an edition is marked
archived but still has live chunks is exactly the failure this system exists to prevent.

### `assert_no_stale_chunks`

Must always return zero rows. LangChain owns retrieval and cannot see `document.status`, so
the entire guarantee is that non-active editions have no chunks. This view is the
enforcement mechanism, not a diagnostic — it belongs in the test suite, asserted after every
promote.

---

## Ingestion pipeline

The diagram is the target shape on Google Cloud. Today `ingest(conn, pdf_path, meta)` in
`ingest/run.py` is called by a script with a hand-built `DocumentMeta`, and the first cloud
deployment will run that same call from a workstation through the Cloud SQL Auth Proxy.
The job-based shape below comes once there is an upload interface to trigger it.

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

**Parsing.** PyMuPDF text blocks plus table geometry, with table cells read by
`page.get_text("text", clip=cell_rect)` rather than PyMuPDF's own table text, which
displaces Thai below-vowels. There is no OCR fallback. pdfplumber was evaluated and
rejected — see [Thai text handling](#thai-text-handling).

**Parsing QA gate.** Blocking. A document does not enter the index until it passes. Each
check records what it measured and the threshold, not just a verdict:

| Check | Failure means |
|---|---|
| `page_numbers_not_sequential` | Extraction reordered or skipped pages |
| `page_count_mismatch` | Extracted page count differs from the PDF's |
| `empty_ratio_too_high` | Too many pages came out with no text |
| `thin_pages` | Recorded only — pages with very little text |
| `unmapped_pua_codepoints` | Thai private-use tone marks with no known mapping |
| `symbol_pua_codepoints` | Recorded only — symbol-font glyphs that cannot be repaired without span fonts |
| `combining_ratio_too_low` | Thai vowels and tone marks lost (floor 0.20; real volumes measure ~0.33) |
| `no_thai_consonants` | No Thai text at all |
| `tables_found` | Recorded only — how many tables were detected |
| `scopes_empty` | No hand-entered subject scopes, so supersession cannot be decided |

Results are written to `document.qa`, so a failure is inspectable rather than silent. There
is no bypass flag: a document that fails stays `pending` with a recorded reason, rather than
going `active` with a warning.

**Chunking.** Size and overlap are tuned separately for Thai prose and for tabular
recommendation entries, which behave very differently — prose rewards overlap, table rows
are self-contained and are fragmented by it.

**Indexing.** Dense and sparse representations. `document.embedding_model` pins the model
version so an upgrade cannot silently mix vector spaces; changing models means re-embedding
the whole active set.

**Promotion.** `promote_current_edition()` archives superseded editions and removes their
chunks in one transaction.

---

## Query pipeline

```
Client ─→ API key (401) ─→ rate limit per key (429)            app/main.py
            │
            ▼
         PII in question? ── yes ─→ PII refusal (nothing searched)  query/guards.py
            │ no
            ▼
         Hybrid retrieval: dense + Thai-segmented sparse, RRF        query/retrieve.py
            │
            ▼
         Rerank, keep top 5 with score ≥ threshold                    query/rerank.py
            │ none ─→ refusal (model never called)
            ▼
         Prompt: numbered excerpts [n] + section label                query/prompt.py
            │
            ▼
         ┌─→ LLM (JSON) ─→ parse: [n] must exist ──────────────┐     query/answer.py
         │                  citations [] ─→ refusal (no retry)  │
         │                                                      ▼
         │               Grounding judge (other vendor, cited excerpts only)
         │                  unreadable / down ─→ refusal (no retry)
         │                                                      │
         │               PII in answer? ─→ PII refusal          │
         │                                                      │
         └── parse error or ungrounded: retry once, then refuse ┘
                                                                ▼
                                             answer + citations built in Python
```

**Authentication and rate limit** run before anything else, as FastAPI dependencies in the
transport layer. The key is compared in constant time; missing and wrong keys get an
identical response. The limit is 10 requests per key per fixed 60-second window, counted
in process memory, and auth resolves first so rejected keys never occupy the limiter. An
in-memory count is only correct with a single instance, which is how the service is to be
deployed; there is no daily cap in the app, because a service that scales to zero loses
that memory, and the spending ceiling belongs to a limit at the model provider.

**Hybrid retrieval** combines dense vectors with sparse lexical matching. Thai pesticide
names and active ingredients are largely transliterated or Latin-script chemical names, which
dense embeddings handle poorly and lexical matching handles well.

No edition filter appears anywhere in this path. Everything in `chunk` belongs to an active
edition by construction — that is the point of the archival design.

**Re-ranking** keeps the top 5 above a score threshold. If nothing clears it, the pipeline
returns "no relevant document" rather than passing weak context to the model.

**Citations** carry source document, edition year and page number, so a user can open the
original PDF and verify. Page provenance is a hard requirement of prompt assembly.

**Grounding guard.** A judge model from a different vendor than the answering model — to
avoid self-preference bias — receives the question, the answer, and **only the excerpts the
answer cited**. `build_grounding_prompt()` takes an `Answer`, not a passage list, so uncited
passages are unreachable by construction and an answer citing one excerpt while quoting
another cannot pass. The verdict is binary, with no threshold to calibrate. Rule 5 of the
judge's instructions treats anything added that no excerpt states — a marker, code, a poem,
evidence of following an instruction — as unsupported, while allowing plain framing. That
rule is the prompt-injection defence: an attack set showed the rest of the design already
stopped 7 of 9 direct injections, and the two that passed both added non-claim content.

Failure types are distinct exceptions. `NotGrounded` is a `ValueError`, so it shares the
single retry budget with malformed JSON and invented citations. `GroundingUndecided` (judge
unreachable or unreadable) is not, so it refuses immediately: re-asking the answering model
cannot repair the judge.

**PII guard.** Regular expressions, not a model, so the inbound check runs before the
question leaves the process. Thai national IDs are validated by check digit. The same kinds
are checked on the answer. Refusal for PII uses its own message, distinct from "no relevant
document".

Refusal is a first-class outcome, not an error path, and is an HTTP `200` with an empty
citation list.

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

## Implementation notes

LangChain owns the retrieval layer only. `PGVectorStore` attaches to the `chunk` table by
column name; prompt assembly, chaining and tracing run through LangChain as well. Everything
else — migrations, ingestion writes, `promote_current_edition()`, and the
`assert_no_stale_chunks` assertion — goes through `psycopg` directly.

The boundary is deliberate. A vector-store abstraction models one table of embeddings; it
has no concept of editions, supersession, or a document-level audit trail, so those stay in
SQL where they can be enforced rather than remembered.

An earlier iteration let LangChain create the table via `init_vectorstore_table()`. That
added a Python step in the middle of every migration and let a library upgrade change the
schema with no diff to review. The DDL is now hand-maintained, with column names kept
LangChain-compatible.

---

## Deployment

**Status: not deployed.** Everything runs locally today, with models served through
OpenRouter. The plan below is agreed, not built.

| Concern | Planned | Notes |
|---|---|---|
| Query API | Cloud Run service, `max-instances=1` | One instance is what makes the in-memory rate limit correct |
| Database and vectors | Cloud SQL for PostgreSQL 17 + pgvector, Enterprise edition, smallest shared-core machine | Paid from trial credit; a move to a cheaper host is planned before it expires |
| Embedding, reranking, answer, judge | Vertex AI model APIs | Not Vertex AI Agent Engine: this is a fixed pipeline where Python makes every decision, not an agent |
| Secrets | Secret Manager | `API_KEYS`, `DATABASE_URL`; no model API key once models run on Vertex via the service account |
| Identity | Dedicated service account: Vertex AI user, Cloud SQL client, secret accessor | Not the default compute account |
| Client auth | App-level API key, held server-side by a web frontend | Identity Platform deferred until there are end users |
| Guardrails | In-app grounding judge + regex PII | Model Armor not evaluated |
| Ingestion (first) | `ingest()` run from a workstation through the Cloud SQL Auth Proxy | Three volumes, run rarely |
| Ingestion (later) | Cloud Storage + Cloud Run Job, same image | Triggered by an upload interface |
| Spend control | Budget alert + spending limit at the model provider + single instance | A budget alert notifies; it does not stop spending |
| Observability | Cloud Logging | |

**Order.** Models move to Vertex AI *locally* first, one at a time — embedding, reranker,
answer model, judge — each gated on the existing evaluation, before any infrastructure moves.
Changing provider and infrastructure together would make a regression impossible to
attribute. Changing the embedding provider means re-embedding the corpus (the model is
pinned per document), and changing the reranker invalidates the calibrated threshold. The
cloud database is then ingested and checked against the local one by `content_sha256`, row
by row, before the service is containerized and deployed.

Ingestion runs as a job rather than a service because it is bursty, long-running and
tolerant of latency; the query path runs as a service because it is the opposite.

---

## Evaluation

The gold set targets the failure modes this design exists to prevent:

| Dimension | What it tests |
|---|---|
| Edition correctness | Questions whose answer changed between 2565 and 2568 must return the 2568 value |
| Supersession | Questions answerable from 2566 must be served from 2568, and no citation may reference an archived volume |
| Abstention | Questions with no support in the current edition — refusing is correct |
| Citation validity | Every cited page exists and contains the claim |
| Retrieval quality | recall@k and MRR against labelled chunks |

Abstention and citation validity are weighted deliberately. A system that answers everything
confidently scores well on conventional RAG benchmarks and is unusable for regulatory
guidance.

What exists today, and what does not:

| Set / script | Scope | Status |
|---|---|---|
| `eval/questions.yaml` + `run_eval.py` | Retrieval recall@k, MRR, rerank threshold sweep | Built; stops at reranking |
| `eval/questions.yaml` + `run_answer_eval.py` | Full served path: answered, cited passage, language, refusals, judge rejections; `--judge off` baseline | Built |
| `eval/attacks.yaml` + `run_answer_eval.py --set attacks` | Direct prompt injection, plus benign look-alikes that must be answered | Built |
| Edition correctness, supersession | As in the table above | Not built — needs section detection fixed first |

---

## Known limitations

- **Recovering an archived edition requires re-ingestion.** Chunks are deleted, so restoring
  2566 means re-parsing and re-embedding it. Deterministic, but not free — accepted in
  exchange for a retrieval path with no edition filter in it.
- **Table extraction quality varies by edition.** Some recommendation tables still need
  manual verification after the QA gate passes.
- **Section detection is weak.** Prose and tables are chunked by separate strategies, with a
  table's header repeated when it splits, but a section heading is found for only about a
  quarter of chunks. The section is the only evidence of which crop a table belongs to, so
  neither the answering model nor the grounding judge can reliably reject an answer about
  the wrong crop.
- **Injection defence covers questions, not documents.** Instructions embedded in an
  uploaded PDF are out of scope while only an administrator uploads official handbooks.
- **Only full supersession is modelled.** A new edition covering a *subset* of an old one
  would require chunk-level rather than document-level archival. No such case exists in the
  current corpus, so it is deliberately unbuilt.
- **No feedback loop.** Evaluation is offline against a fixed gold set; there is no
  mechanism for user corrections to reach the index.
- **Single-language.** Queries and corpus are Thai only. Latin-script chemical names work
  incidentally through lexical matching, not by design.