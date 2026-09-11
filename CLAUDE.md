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
  migrations/
    001_init.sql   # full schema, hand-maintained. Source of truth.
  seed.sql         # local dev only
ingest/
  extract.py       # PyMuPDF only. Raw text out, no cleaning. Also owns table
                   # geometry and cell text (extract_tables), because both come
                   # out of PyMuPDF — how a table is split is chunk.py's.
  normalize.py     # the 5-step Thai pipeline. Nothing else normalizes text.
  qa_gate.py       # blocking checks. Writes document.qa. No bypass.
  chunk.py         # prose and table strategies live here, not in extract.py
  embed.py         # embedding + model version pinning
  promote.py       # supersession. The only place that deletes chunks.
  db.py            # psycopg connection + document writes. Every direct SQL
                   # statement in the ingest path lives here, not scattered
                   # across stages.
  run.py           # orchestrates one document: extract -> qa_gate -> write.
                   # Takes metadata as a DocumentMeta it is handed; it does not
                   # read title/edition/scopes from anywhere itself.
query/
  guards.py        # input (PII, injection) and output (PII, grounding) guards
  retrieve.py      # hybrid dense + sparse, via PGVectorStore
  rerank.py        # top-5 + score threshold
  prompt.py        # prompt assembly. Citations are constructed here.
  answer.py        # orchestration: retrieve -> rerank -> prompt -> LLM ->
                   # parse, plus the refusal and retry policy. Spans three
                   # modules, so it belongs to none of them -- and not to
                   # app/main.py either.
app/
  main.py          # FastAPI / Cloud Run service. HTTP only — no business logic.
eval/
  questions.yaml   # gold set
  run_eval.py
docs/              # diagrams, ARCHITECTURE.md assets
```

Placement rules that matter more than the tree itself:

- Archiving happens **only** in `promote.py`. If another module sets `document.status` or
  deletes from `chunk`, that is a bug.
- Normalization happens **only** in `normalize.py`. No ad-hoc `.strip()` or regex cleanup in
  `extract.py` or `chunk.py`.
- `app/main.py` is a transport layer. Logic belongs in `query/`.
- `title_th`, `edition_year_be` and `scopes` are entered by a human and reach the pipeline
  as a `DocumentMeta` argument. Until the admin UI exists, callers construct one directly.
  No stage infers them from the PDF — invariant 1.
- Schema truth lives in `db/migrations/001_init.sql`. Read it rather than trusting any
  summary, including this file.

## Library boundary

LangChain owns **retrieval only** — `PGVectorStore` attached to the `chunk` table, plus
prompt assembly, chaining and tracing. Everything else goes through `psycopg` directly:
migrations, ingestion writes, `promote_current_edition()`, and the `assert_no_stale_chunks`
assertion.

Do not call `init_vectorstore_table()`. The DDL is hand-maintained on purpose — letting the
library create the table put a Python step in the middle of every migration and let a
version bump change the schema with no diff to review. Column names on `chunk`
(`langchain_id`, `content`, `embedding`, `langchain_metadata`) are LangChain's so
`PGVectorStore.create()` attaches with no subclass; do not rename them.

`content_column` must be named explicitly on both sides. `PGVectorStore.create()` defaults it
to `content` while `init_vectorstore_table()` defaults to `page_content`, and a mismatch
fails at query time, not at startup.

## Invariants — do not break without asking

**1. Never order or compare editions by PDF metadata.**
`edition_year_be` comes from the Buddhist Era year printed inside the document. The 2565 PDF
was re-exported through iLovePDF in 2024, so its `CreationDate`/`ModDate` make it look newer
than 2566. Any code reaching for `doc.metadata['creationDate']` to decide currency is a bug.

**2. Supersession is decided by `scopes`, never by title, filename, or family.**
A document is archived when every scope it covers is covered by at least one newer document
— union coverage across newer editions, not single-document superset. The 2566 volume is
insecticide-only from a *different publication series* than 2565/2568, so any title- or
family-based heuristic fails to retire it. Do not "simplify" this into a title match, and do
not narrow it back to `A.scopes @> B.scopes`.

**3. `document` rows are permanent. `chunk` rows are not.**
Archiving an edition means `status = 'archived'`, `archived_at = now()`, and deleting its
chunks. `document` is the audit trail — `title_th`, `scopes`, `page_count`, `chunk_count`,
`qa`, `file_hash` must survive so re-ingestion from the source PDF is verifiable. **No
`DELETE FROM document` anywhere.** `DELETE FROM chunk` for archival belongs only inside
`promote_current_edition()` in the schema, invoked only from `promote.py`'s
`promote_new_document()` — never called directly from `db.py` or `run.py`. There is no
separate review step: `run.py` calls `promote_new_document()` the instant a document has
passed the QA gate and been chunked and embedded, so a document goes live and any edition
it fully supersedes is archived in the same transaction. The one other place a chunk row is
deleted is `db.py`'s `delete_chunks_for_document()`, called only when re-ingesting a
document that is still `pending` — a document that was never `active` has nothing to
retire from search, and clearing its old chunks first is what keeps a failing re-ingest at
zero chunks and a passing one from colliding with `chunk_dedup_idx`.

**4. Never filter on edition or status in the retrieval path.**
Everything in `chunk` belongs to an active edition by construction. A `WHERE status =
'active'` predicate, a metadata filter on edition year, or any equivalent is post-filtering
— which this design exists to avoid, and which silently degrades recall in approximate
search. If you find yourself needing such a filter, the bug is in `promote.py`, not in the
query.

The `chunk_with_document` view exposes `status` for eval and citation building. It is not
the retrieval path. Do not retrieve through it.

**5. `assert_no_stale_chunks` must return zero rows.**
This is the enforcement mechanism for invariant 4, not a diagnostic. Assert it in tests
after every promote. If it ever returns rows, retrieval is serving superseded guidance right
now.

**6. Two tables. Keep it that way.**
The schema went 16 → 6 → 2 deliberately. Before adding a table, check whether a column on
`document`/`chunk` or a view does the job. Views and functions are fine; new entities need
justification.

**7. PyMuPDF only. Do not reintroduce pdfplumber.**
pdfplumber silently corrupts Thai combining marks on the 2568 file — reordered and dropped
vowels and tone marks. PyMuPDF extracts the same file with zero errors. This was measured,
not assumed. If PyMuPDF lacks something you need, raise it rather than swapping parsers.

The same rule applies inside PyMuPDF. `table.extract()`, `table.to_markdown()` and
`table.header.names` group spans by vertical position, which puts a Thai below-vowel on its
own line — measured combining ratios on one table: 0.3267 from `get_text`, 0.2796 from
`extract()` with marks displaced, 0.0432 from `to_markdown()`. Table cells are read with
`page.get_text("text", clip=cell_rect)`. Any layout algorithm that infers reading order from
vertical position is unsafe here, whoever ships it.

**8. Never index text that hasn't been through `normalize.py`.** Order matters:
1. PUA tone marks `U+F700`–`U+F71A` → real Thai codepoints (225 in 2565, spread over pages
   1, 2 and 220 — not only the cover). This range is the *Thai* slice of the private use
   area. 2568 carries 593 private-use characters from SymbolMT and Wingdings at `U+F0xx`
   that nothing here repairs, three of them real content (α, Δ, →). See LOG.md.
2. Unicode NFC
3. Strip headers/footers (they contain the edition year and leak into chunk text)
4. Strip page numbers and dot leaders
5. Collapse whitespace

`chunk.content` stores **normalized** text, and `chunk.content_sha256` hashes that same
text — the two are deliberately the same string, computed once in `chunk.py`. Raw text
never reaches `chunk` at all: `chunk_document()` normalizes each page (and each table,
through `normalize_table()`) before ever building a `Chunk`, because the running footer
that step 3 strips carries the edition year, and a chunk embedded with "2568" baked into it
would still be findable after 2568 is superseded. The hash is what catches duplicates, and
~48% of 2568's prose lines are identical to 2565's after normalization — computing it from
anything other than what gets embedded would make the dedup index compare the wrong thing.

**9. The parsing QA gate is blocking.**
A document failing page-count, table/layout extraction, OCR confidence, or Thai
combining-mark checks does not get chunked or embedded. Write the failure into `document.qa`
and leave it `pending`. Do not add a bypass flag.

**10. Answers must carry citations** — source document, edition year, page number. A change
that drops page provenance is not acceptable.

**11. Refusal is a valid output.** Below-threshold reranking → "no relevant document".
Failed grounding check → retry once, then refuse. Do not loosen thresholds or drop the
grounding check to make the system look more responsive.

## Commands

```bash
uv sync
docker compose up -d                  # pgvector/pg17; applies 001_init.sql on first boot
docker compose down -v                # reset: drops the volume, schema reapplies on next up
pytest
pytest -m "not requires_source_pdfs"  # full local suite: live API calls included
python tests/fixtures/make_fixture.py # rebuild the extract fixture from the source PDFs
uvicorn app.main:app --reload         # local only -- no auth until guards.py exists
```

What CI runs is narrower, and the filter lives in `.github/workflows/test.yml`:

```
pytest -m "not requires_source_pdfs and not requires_embeddings and not requires_rerank and not requires_llm"
```

**Adding a marker to `pyproject.toml` means adding it to that filter in the same commit.**
Splitting the two has already broken CI once: the marker existed, CI did not exclude it, and
four live-API tests ran in an environment with no key.

`ingest.run` has no CLI. `ingest(conn, pdf_path, meta)` takes a `DocumentMeta` the caller
constructs; the admin UI will build one from a form, and until then a script or test does.

`ingest.promote` exists but has one function, `promote_new_document()`, called only from
inside `ingest()` on the passing path — there is no standalone command for it yet, and no
retroactive/admin re-promotion flow.

`eval/run_eval.py` exists and is run by hand (`uv run python -m eval.run_eval`,
or `--sweep` to re-derive the threshold): it needs a database with the real
corpus ingested, which CI does not have, so it is not part of the test suite.
The gold set is 29 answerable / 5 unanswerable. It has no supersession or
edition-correctness questions — the one supersession negative turned out to be
mislabelled, because the check that built it was section-scoped and most chunks
have no section (LOG.md). Do not add one back until section detection is fixed.

`query/rerank.py` exists: `rerank(question, passages, scorer=..., threshold=,
top_k=)` filters/sorts/cuts, with `openrouter_rerank_scorer()` (VoyageAI's
rerank-2.5-lite via OpenRouter) as the real `Scorer`. `SCORE_THRESHOLD` is
calibrated against `eval/questions.yaml` (see the constant's own comment and
`eval/run_eval.py --sweep`), not guessed.

`query/prompt.py` exists and is pure — `build_prompt(question, passages)` returns a
`Prompt` carrying the text and the `[n]` → `Passage` mapping, `parse_answer(raw, prompt)`
turns the model's JSON reply into an `Answer`. The model is never shown a page number or
an edition year: citations are built in Python from the `Passage` objects, so a cited
page is always one retrieval actually returned (invariant 10). An empty `citations` list
from the model means refusal and its answer text is discarded (`REFUSAL_TEXT`); an
out-of-range `[n]` raises, which is the grounding failure invariant 11's "retry once,
then refuse" exists for. Nothing here calls an LLM — that belongs to `query/answer.py`.

`query/answer.py` orchestrates the whole query path — `answer(question, store, conn)`
runs retrieve → rerank → build_prompt → model → parse_answer and owns the refusal and
retry policy: nothing above the rerank threshold refuses without calling the model at
all, an unparseable or ungrounded reply is retried exactly once and then refuses, and a
reply with no citations is a correct refusal that is never retried. The model sits behind
a `Chat` protocol, like `Scorer` in `rerank.py`. `ANSWER_MODEL` was chosen by running the
full gold set through three candidates — see the constant's comment for the table.

`app/main.py` runs with `uv run uvicorn app.main:app --reload`: `POST /ask` and
`GET /health`. Refusal is a `200` with an empty `citations` list, never a `404`. **Do not
deploy it** — there is no auth, no rate limit and no input guard until `query/guards.py`
exists.

Not written yet, so the command does not exist: `db/seed.sql`, `query/guards.py`.

## Conventions

- Every migration starts with `\set ON_ERROR_STOP on`. Without it psql continues past a
  failed statement and leaves a half-applied schema that looks fine.
- Schema changes are new numbered files in `db/migrations/`. Never edit an applied one.
- Anything touching supersession or archiving needs a test asserting
  `assert_no_stale_chunks` is empty — not just that a status column changed.
- `Table.cells` is the record; `Table.markdown` is a rendering of it. `_to_markdown()` may
  prune columns empty in every row and join wrapped cell lines with a space, because
  neither discards cell text. It may not decide which rows *mean* header — `extract.py`
  makes no judgement calls, and a first data row wrongly absorbed into a header is
  invisible. Changing this rendering changes `chunk.content` and therefore
  `content_sha256`, so it means re-ingesting the corpus, not an in-place edit.
- `document.embedding_model` is pinned per document. Changing models means re-embedding the
  whole active set, not an in-place swap. Flag it rather than doing it.
- Thai test fixtures must include combining marks and at least one PUA case. ASCII fixtures
  pass while the real pipeline is broken.
- Tests run against real Postgres + pgvector via testcontainers. The important guarantees
  are schema-level and cannot be mocked.
- Pin `langchain-*` packages to exact versions. `PGVector` was already deprecated in favour
  of `PGVectorStore` once.

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

- Any change to `promote_current_edition()`, the archival mechanism, or
  `assert_no_stale_chunks`.
- Adding a filter to the retrieval path.
- Adding a table, or denormalizing across the two existing ones.
- Changing the parser, the normalization order, or the chunking strategy.
- Loosening the rerank threshold, grounding check, or QA gate.
- Widening what LangChain owns beyond retrieval and prompting.
- Anything that would make an answer citable to a page that doesn't contain the claim.