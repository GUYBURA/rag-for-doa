# Thai Pesticide Guidance RAG

ระบบถาม-ตอบภาษาไทยบนคู่มือคำแนะนำการใช้สารป้องกันกำจัดศัตรูพืช ของกรมวิชาการเกษตร

A Thai-language question answering system over the Thai Department of Agriculture's
pesticide recommendation handbooks (คำแนะนำการใช้สารป้องกันกำจัดศัตรูพืช).

---

## What it does

Ask a question in Thai. Get an answer drawn only from the current edition of the handbook,
with a citation you can go and check.

Real output from the running system, not an illustration:

> **Q:** โรคราสนิมในถั่วเหลืองเกิดจากเชื้อราอะไร ใช้สารอะไรป้องกัน
>
> **A:** ตามข้อมูลสำหรับถั่วเหลือง โรคราสนิมเกิดจากเชื้อรา Phakopsora pachyrhizi [4] สารป้องกันกำจัดที่แนะนำมี
> 2 ชนิด คือ (1) ทีบูโคนาโซล (tebuconazole) 25% EW กลุ่มที่ 3 ความเป็นพิษปานกลาง (1,700) ใช้อัตรา 10 มล.
> ต่อน้ำ 20 ลิตร และ (2) ไซโปรโคนาโซล (cyproconazole) 10% SL กลุ่มที่ 3 ความเป็นพิษปานกลาง (<350) ใช้อัตรา
> 80 มล. ต่อน้ำ 20 ลิตร โดยวิธีการใช้คือพ่นเมื่อพบการระบาดของโรค และพ่นซ้ำทุก 7 วัน [4]
>
> *[4] คำแนะนำการใช้สารป้องกันกำจัดศัตรูพืช ฉบับปี 2568, หน้า 56 (ถั่วเหลือง)*

The `[4]` is not decoration. The model is shown numbered excerpts and may only answer with
their numbers; the document title, edition year and page are attached afterwards from the
retrieved passage itself. So a citation can only ever point at a page the search actually
returned — the model is never in a position to invent one.

If the handbook doesn't cover the question, the system says so instead of guessing. For
regulatory guidance, "I don't know" is a better answer than a confident wrong one.

## Why I built this

> I'm familiar with RAG concepts and have built local RAG applications for personal use.
> What I hadn't done was deploy one in a real production environment. This project is my
> way of learning that end to end — ingestion, retrieval, evaluation, and the operational
> parts that tutorials skip.

I picked this corpus on purpose. Pesticide guidance is revised every year or two, and the
recommendations genuinely change: a chemical recommended in one edition may be withdrawn in
the next. That makes it a corpus where retrieving the *wrong edition* isn't a slightly worse
answer — it's a wrong answer with real consequences for whoever acts on it.

## The corpus

Three editions from the Thai Department of Agriculture, in Buddhist Era years:

| Edition | Coverage | Notes |
|---|---|---|
| 2565 | fungicide, insecticide, herbicide | Full compendium |
| 2566 | insecticide | Insecticide-only, separate series |
| 2568 | fungicide, insecticide, herbicide | Full compendium, current |

I could not locate a 2567 edition. The system doesn't assume editions arrive on a fixed
schedule, so a gap in the sequence is handled as normal rather than treated as a missing
file.

## Why this is harder than it looks

Two things in this corpus break the obvious approach. Both were found by actually opening
the files, not by assuming.

### The newest file is not the newest edition

The natural way to decide which edition is current is to look at the document date. That
fails here: the 2565 PDF was re-exported through iLovePDF in 2024, so its file metadata
makes it look *newer* than the 2566 edition. A pipeline that trusts file dates will serve
three-year-old guidance as if it were current, and will look perfectly correct while doing
it.

The system reads the edition year printed inside the document instead, and never uses file
metadata to decide what's current.

### A new edition can retire a book that looks nothing like it

The 2566 volume covers insecticides only and belongs to a different publication series than
the 2565 and 2568 compendia — different title, different format. But the 2568 compendium
covers insecticides too, so 2566 is fully superseded by it.

Any rule based on title similarity or "document family" would keep 2566 alive forever. The
system compares *what subject areas each edition covers* rather than what it's called, which
retires 2566 correctly even across series boundaries.

## How it works

![System architecture](docs/system_design.png)

Uploading a PDF triggers an automated pipeline: extract the text, run a quality check, split
it into passages, and add them to a searchable index. Once a new edition is indexed, any
edition it fully supersedes is archived — removed from search, but kept on record so the
history stays auditable.

On the query side, a question is matched against the index, the candidates are scored for
relevance and cut to the strongest few, and those are passed to a language model that must
answer from them and say which ones each claim came from. If nothing scores high enough, or
the model finds nothing in them that answers the question, the system refuses instead of
answering.

Around that path sits a guard layer, and it is running, not planned. A request needs an API
key and is rate-limited per key. A question carrying a Thai ID number, phone number or email
is refused before anything is searched or sent to a model. Every answer is read by a second,
different model that checks each claim against the excerpts it cites — including anything
added that no excerpt says, which is what stops an instruction smuggled into a question from
being carried out. An answer that fails is regenerated once, then refused.

Full technical detail, schema, and design rationale: **[ARCHITECTURE.md](ARCHITECTURE.md)**

## Running it locally

```bash
uv sync
docker compose up -d          # pgvector/pg17, applies the schema on first boot
uv run pytest -m "not requires_source_pdfs"
```

Ingesting the handbooks needs the PDFs, which are not in this repository. With them in
`data/raw/`, and `OPENROUTER_API_KEY` plus `API_KEYS` (comma-separated, see `.example.env`)
in `.env`, the pipeline runs per document and the service starts with:

```bash
uv run uvicorn app.main:app --reload
curl -s localhost:8000/ask -H 'content-type: application/json' \
  -H 'X-API-Key: <one of API_KEYS>' \
  -d '{"question": "โรคราสนิมในถั่วเหลืองเกิดจากเชื้อราอะไร"}'
```

The service refuses to start with `API_KEYS` empty. On Windows, send Thai request bodies
from a UTF-8 file (`--data-binary @body.json`) rather than inline: Git Bash re-encodes
command-line arguments and the server receives invalid JSON.

## How it's evaluated

Evaluation targets the specific failure modes this system exists to prevent, not general
answer quality. The gold set is 34 hand-checked questions — 29 answerable, 5 that the
handbook does not cover — built from the corpus rather than from memory: each answerable
question keys on a fact that appears in exactly one crop section, because active
ingredients and pathogen genera are shared across many crops and a looser check scores
"found some fungicide table" as a hit.

Measured, against the real corpus in a local container:

| | |
|---|---|
| recall@5 | 28/29 |
| answers whose cited chunk contains the expected fact | 27/27 |
| answers in the same language as the question | 28/28 |
| unanswerable questions refused | 1/5 by retrieval score, 4/5 more by the model |

The one recall miss ranks 7th of 20 candidates, so no threshold recovers it.

Two of those rows are worth reading together. The relevance threshold alone refuses only
the unambiguously off-domain question; the genuinely hard negatives — a fabricated
chemical, an absent crop, a real pest asked about the wrong real crop — score in the same
band as real answers, and no single threshold separates them. What catches those is the
model being told it may only cite the excerpts it was given, and being given the crop
each excerpt belongs to. That is measured too, not assumed: three candidate models were
run over the whole gold set and they differed most on exactly this axis.

The abstention number is deliberately reported as a metric rather than as a failure
count. A system that answers everything confidently is unusable for regulatory guidance.

Still to build: an **edition-correctness** set, whose answer changed between editions, and
a **supersession** set answerable only from the archived 2566 volume. Both need the
chunker's section detection fixed first — see Known gaps.

Run it with `uv run python -m eval.run_eval`, or `--sweep` to re-derive the relevance
threshold from the data.

That script stops at reranking, so it cannot see the answering model or the guards. A
second one, `uv run python -m eval.run_answer_eval`, runs the same questions through the
whole served path. With the grounding judge on, 27 of 29 answerable questions are answered,
all 27 cite the expected passage, and the judge wrongly refuses none of them. `--judge off`
swaps in a judge that approves everything, so the guard's effect is a measured difference
between two runs rather than a claim.

Prompt injection is evaluated the same way, with `--set attacks` over 13 hand-written cases
in `eval/attacks.yaml`: nine attacks (override the rules, leak the instructions, off-domain
tasks dressed in pesticide vocabulary, instructions riding on a real question, a forged
excerpt, a forged reply) and four real questions that merely *sound* like instructions,
which must still be answered. Before any injection defence was written, the existing design
already stopped 7 of 9. The two that got through both added something that was not a
factual claim — a marker token, and code built from numbers that really were on the page —
which a claim-by-claim judge has nothing to check. One added rule in the judge closed both:
9 of 9 blocked, 4 of 4 real questions still answered. Each figure is one run against
nondeterministic models, and the attacks were written by the same person who built the
defences.

## Status

End to end and answering questions, locally, behind authentication, a rate limit and the
guard layer. Every stage landed with the tests that prove it before the next one started —
225 of them, 199 run in CI on every push including the ones that need a real Postgres with
pgvector. The 26 CI skips are the ones that need the source PDFs (gitignored) or a live
API key.

All three editions are ingested, 2568 is active, and the other two are archived with their
passages removed from search and their records kept.

Not deployed yet. Deployment to Google Cloud is the next stage and is planned, not started —
see Next.

**Working**

- **Text extraction** — PyMuPDF, raw text out with 1-based page numbers preserved for
  citation. pdfplumber was measured against it and rejected: it silently reorders and drops
  Thai vowels and tone marks on the 2568 file.
- **Thai normalization** — the full five-step pipeline, 34 tests. Private Use Area tone
  marks restored to real Thai codepoints, Unicode NFC, running headers and footers removed,
  page numbers and dot leaders stripped, whitespace collapsed.

  Two findings from this stage are written up in [KNOWLEDGE.md](KNOWLEDGE.md). The step
  order is not stylistic: PUA codepoints carry combining class 0, so NFC has nothing to
  reorder until they are repaired, and running the two steps the other way round changes the
  result on 6.6% of mixed-script inputs. Each of those is a pair of lines that look
  identical but hash differently, which quietly breaks deduplication. There is a test that
  fails if anyone reorders the steps.

- **Parsing QA gate** — ten checks over the raw text: page numbering and page-count
  cross-check, blank-page ratio, thin pages, unmapped Thai PUA, symbol-font PUA, Thai
  combining-mark ratio, presence of Thai at all, tables found, and presence of the
  hand-entered subject scopes. Each check records what it measured and the threshold it was judged against, not
  just a pass or fail, because the reason is what has to survive in the database.

  The combining-mark floor is the interesting one. It counts Thai marks per Thai consonant,
  since a parser that drops vowels leaves the consonants untouched. All three volumes land
  within 1% of each other — 0.3314, 0.3345, 0.3324 — across two different publication
  series, which is what makes it a fact about written Thai rather than about one book.

- **Ingestion writes** — one document row per volume, written through psycopg, with the
  full QA record stored as JSON. Re-ingesting a file already in the database updates it in
  place while it is still pending, and is refused outright once it is live or archived.
  Document rows are never deleted; that is the audit trail.

  Tested against a real Postgres with pgvector in a container, not a mock, since the
  guarantees that matter here are schema-level. Each test runs inside a transaction that is
  rolled back, so no test can see another's rows.

- **Chunking** — separate strategies for prose and for dosage tables, because splitting a
  dosage table mid-row produces a passage that states the wrong application rate. A table
  split across passages repeats its header, so each piece still reads as a table.

- **Embedding and supersession** — the embedding model is pinned per document, and a query
  against a corpus embedded with a different model is refused at startup rather than
  quietly returning worse rankings. When a new edition goes live, every edition it fully
  supersedes is archived in the same transaction, its passages deleted from the index.

  Nothing in the query path filters on edition or status, which is the point: everything in
  the index belongs to an active edition by construction, so there is no filter to forget.
  A database-level assertion that no passage belongs to a non-active edition has to return
  zero rows, and is checked after every promotion.

- **Retrieval** — hybrid dense and lexical search in one round trip, fused by reciprocal
  rank. The lexical half needs Thai word segmentation first: Thai does not put spaces
  between words, so an unsegmented question reaches Postgres as one long token and matches
  nothing.

- **Reranking and abstention** — a cross-encoder scores each candidate against the question
  on an absolute scale, which the fusion score cannot do (it is a function of rank, so the
  top hit of a search that found nothing scores what the top hit of a search that found the
  answer scores). Everything below a calibrated threshold is dropped, and an empty result
  is a refusal. The threshold is swept from the gold set, not guessed, and was re-swept
  after the corpus was re-ingested because every score moved with the text.

- **Answering, with citations** — the model is shown numbered excerpts and returns JSON
  naming which numbers it used. Citations are assembled in Python from the retrieved
  passages, so a cited page is always one the search returned. A reply citing an excerpt
  that was never sent is retried once and then refused; a reply citing nothing is a
  refusal and is *not* retried, because "these excerpts don't answer this" is a correct
  outcome rather than a malfunction.

- **HTTP service** — FastAPI, `POST /ask` and `GET /health`. A refusal is a normal `200`
  with an empty citation list, never a `404`: declining to answer is the behaviour this
  system exists to get right, not an error condition for clients to special-case.

- **Grounding guard** — every answer is checked by a judge model from a different vendor
  than the one that wrote it, shown only the excerpts the answer actually cited, so an
  answer that cites one excerpt while quoting another cannot pass. Numbers must match
  exactly, knowledge the judge has that the excerpts don't state does not count, and
  anything added that no excerpt says is unsupported. A failed check is retried once and
  then refused; a judge that is down refuses immediately rather than serving an unchecked
  answer. The one test that matters most — the judge catching a dose changed from 20 to 40
  — runs against the real model.

- **Personal-data guard** — regular expressions, not a model, so the check runs before a
  question leaves the process. Thai ID numbers are validated by their check digit, which
  is what stops every ISBN in the corpus from looking like one. Phone numbers were once
  allowed through in answers on the assumption the handbooks print contact numbers; a scan
  of the whole active corpus found none, so the exemption was removed.

- **Authentication and rate limiting** — an API key per caller, compared in constant time,
  with the same response for a missing key and a wrong one. Each key gets 10 requests a
  minute, counted in memory. That is correct only with one running instance, which is how
  it will be deployed, and it deliberately has no daily cap: a serverless instance that
  scales to zero loses its memory, so the real spending ceiling is a limit set at the model
  provider.

**Next**

- **Deployment to Google Cloud**, in phases that each end by matching a number already
  measured locally:
  1. Set up the project, a budget alert, and the region, chosen by database latency.
  2. Cloud SQL for PostgreSQL with pgvector, ingested from a workstation through the Cloud
     SQL Auth Proxy, and checked row by row against the local database by content hash.
  3. A container image for the query service — **built**, and smoke-tested against a local
     database; still to be run against the cloud one.
  4. Cloud Run with a single instance, a dedicated least-privilege service account, and
     secrets in Secret Manager.
  5. Verification against the live URL, including a rollback drill.

  The four models (embedding, reranker, answer, judge) stay on OpenRouter. Moving them to
  Vertex AI was the original first phase and was dropped on cost: Vertex adds a per-call
  bill on top of one already paying for Cloud SQL and Cloud Run, where OpenRouter is
  pay-per-token behind a hard spending limit. It also means no score is re-measured and the
  corpus is not re-embedded, at the price of egress, a little latency, and an API key in
  place of IAM.
- Later: ingestion as a Cloud Run Job reading PDFs from Cloud Storage, a web frontend that
  holds the API key server-side, and CI/CD.

**Known gaps**

- Section detection in the chunker identifies a heading for only about a quarter of
  passages. This is the most consequential open gap, for a non-obvious reason: the section
  label is what tells the model that a table belongs to mango and not durian, since the
  crop name often appears only in the heading and not in the table itself. It also produced
  a wrong label in the gold set — a question was marked unanswerable because a
  section-scoped search for its fact found nothing, when the fact was present in passages
  whose section was simply unset. Written up in [LOG.md](LOG.md).
- Table rendering drops columns that are empty in every row and no longer splits terms
  across `<br>` markers, but a header spanning several physical rows is still rendered as
  several half-empty rows. Merging them would read better and is left undone deliberately:
  deciding which rows *mean* header is a judgement call, and the extraction layer makes
  none.
- The 2565 volume carries 225 private-use characters where tone marks should be, and the
  gate blocked it on four of them. All four are now identified — by rendering the glyphs out
  of the PDF and reading the resulting words, not by trusting a font chart — and the font
  turns out to store one tone mark at several codepoints, picked by the shape of the
  consonant underneath. Four more codepoints complete that block by symmetry but appear in
  no edition, so they are deliberately left unmapped: a guessed tone mark is a silently
  different Thai word, while a missing one is a loud failure.
- Both compendium editions also carry glyphs from symbol fonts in a different part of the
  private use area, three of which are real content — an alpha, a delta and an arrow inside
  chemical group names. Repairing them needs to know which font each span used, which the
  extraction layer does not currently carry, so for now every occurrence is counted and
  recorded per document rather than silently passed through.
- One page of the 2565 volume extracts with a handful of its lines reversed.
- Subject scopes are entered by hand, which makes supersession dependent on a human getting
  them right. The QA gate checks they are present; it cannot check they are correct.
- A document rejected for having no scopes at all cannot be written to the database, since
  the schema requires at least one. That failure mode therefore leaves no trace in the QA
  record — the one gate result that is invisible afterwards.
- Promotion runs only inline during a successful ingest. There is no administrative
  re-promotion flow, and no upload interface: the caller constructs the document metadata
  directly, which today means a script or a test.
- The gold set has no supersession or edition-correctness questions yet, for the reason in
  the evaluation section above.
- The grounding judge cannot catch an answer about the wrong crop. Asked about a mango pest
  on durian, the model can answer from the mango table and every claim checks out against
  its excerpt. The crop is usually named only in the section heading, and most passages
  have none, so this is the same gap as section detection above.
- Prompt injection is defended against in questions only. Instructions planted inside an
  uploaded PDF are out of scope while documents come solely from an administrator uploading
  official handbooks; that changes the moment anyone less trusted can upload.
- The rate limit's count lives in one process's memory and is only correct with a single
  instance. Scaling out means moving it to a shared store.
- FastAPI parses the request body before the API key dependency runs, so a malformed body
  gets a `400` even without a key. It costs nothing and consumes no quota, but an
  unauthenticated caller can see body-parsing errors.

## Disclaimer

This is a portfolio project. It is not an official Department of Agriculture service, and
its answers should not be relied on for real pesticide application decisions. Always consult
the published handbook.