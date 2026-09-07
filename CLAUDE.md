## CLAUDE.md — Thai Pesticide Guidance RAG

---

## Project Overview

A RAG system over Thai Department of Agriculture pesticide guidance handbooks (editions
2565, 2566, 2568 BE). It is a portfolio piece, so **design decisions need to be
explainable**, not just working. When you make a non-obvious choice, leave a comment saying
why, in a form that would survive being asked about in an interview.

## Repository Layout

```
db/
  schema.sql              # 2 tables, views, promote_current_edition()
  migrations/
ingest/
  extract.py              # PyMuPDF extraction
  normalize.py            # Thai normalization pipeline
  qa_gate.py              # parsing QA checks
  chunk.py
  embed.py
  promote.py              # scope-superset supersession
query/
  guards.py               # input/output guardrails
  retrieve.py             # hybrid dense + sparse
  rerank.py
  prompt.py               # citation-carrying prompt assembly
app/
  main.py                 # Cloud Run service
eval/
  questions.yaml          # gold set
  run_eval.py
docs/
```

## Data Model

**`document`** — one row per source PDF.
 
| Column | Purpose |
|---|---|
| `document_id` | PK |
| `source` | Original filename |
| `title_th` | Display title shown in citations |
| `edition_year_be` | Buddhist Era year **read from inside the document** |
| `scopes` | `text[]` — subset of `{fungicide, insecticide, herbicide}`; drives supersession |
| `status` | `pending` / `active` / `archived` |
| `file_hash` | Duplicate upload detection |
| `page_count` | QA cross-check |
| `parser` | Which extractor produced the text |
| `qa` | Parsing QA gate results |
| `ingested_at` | Ingestion timestamp |
 
**`chunk`** — one row per retrievable unit.
 
| Column | Purpose |
|---|---|
| `chunk_id` | PK |
| `document_id` | FK → `document` |
| `content` | Raw extracted text, un-normalized |
| `content_sha256` | Hash of the *normalized* text; cross-edition dedup |
| `page_number` | So citations point at a real page |
| `section` | Heading the chunk sits under |
| `metadata` | Domain fields lifted out of tables (crop, pest, active ingredient, rate) |
| `embedding` | Nullable; `NULL` ⇒ archived and out of the index |
| `created_at` | |
