# Thai Pesticide Guidance RAG

A Retrival-Augmented Generation system answering over The Thai Department of Agriculture's pesticide recommendation handbooks (คำแนะนำการใช้สารป้องกันกำจัดศัตรูพืช)

## Personal Information

> From my experience, I am familiar with RAG concepts and have built local RAG applications for personal use. However, I realized I did not fully know how to deploy RAG in a real production environment. Therefore, this project is a learning opportunity for me to build production-ready RAG systems and deepen my understanding of core concepts, including data ingestion, retrieval, and evaluation.

## The corpus

Three editions from the Thai Department of Agriculture, in Buddhist Era years:
 
| Edition | Scope | Notes |
|---|---|---|
| 2565 | fungicide, insecticide, herbicide | Full compendium |
| 2566 | insecticide | Separate insecticide-only series |
| 2568 | fungicide, insecticide, herbicide | Full compendium, current |

I couldn't find the 2567 edition. So there is no 2567 edition. The gap is real, not a missing file.

## The real problem in this project:

### 1. The PDF metadata trap

The obvious way to order editions is by document date. That is wrong for this corpus.
The 2565 PDF was re-exported through iLovePDF in 2024, so its file metadata makes it look
*newer* than the 2566 edition. Any pipeline that sorts by `CreationDate` or `ModDate` will confidently serve three-year-old guidance as current.

### 2. Supersession is about scope, not title

The 2566 volume is an insecticide-only book from a different publication series than the 2565/2568 compendia. Title similarity, filename patterns, and "document family" all fail here: 2566 does not look like the other two, so a title-driven rule would never retire it, even though the 2568 compendium fully covers its subject matter.

---

## Architecture

![alt text](docs/system_design.png)

---
- **File hash check** — `file_hash` on `document` rejects re-uploads of identical bytes.
- **Parsing** — PyMuPDF, with layout and table extraction and an OCR fallback.
- **Parsing QA gate** — a document is not allowed into the index until it passes: page
  count matches, tables and layout extracted, OCR confidence above threshold, Thai
  combining marks intact. The result is stored in `document.qa` so a failure is
  inspectable rather than silent.
- **Chunking** — size and overlap tuned for Thai prose and for tabular recommendation
  entries, which behave very differently.
- **Indexing** — dense and sparse representations, with the embedding model version pinned
  per document so a model upgrade never silently mixes vector spaces.
- **Promotion** — `promote_current_edition()` runs the scope-superset comparison, flips
  the superseded document to `archived`, and nulls its embeddings in one transaction.

---

Answers cite source document, edition year and page number, so a user can open the original
PDF and check. Refusing is a first-class outcome: below-threshold retrieval and failed
grounding checks both return "I don't have this in the current guidance" rather than a
plausible fabrication.

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
## Evaluation
 
The gold set is built around the failure modes this system is designed to prevent, not
just general answer quality:
 
- **Edition correctness** — questions whose answer changed between 2565 and 2568. The
  system must return the 2568 value.
- **Supersession** — questions answerable only from 2566, which must now be served from
  2568 and must never cite the archived volume.
- **Abstention** — questions with no support in the current edition. Refusing is the
  correct answer.
- **Citation validity** — every cited page number must exist and must actually contain the
  claim.
- **Retrieval quality** — recall@k and MRR against labelled chunks.

## Disclaimer
 
This is a portfolio project. It is not an official Department of Agriculture service and
its answers should not be relied on for real pesticide application decisions. Always
consult the published handbook.