# Thai Pesticide Guidance RAG

ระบบถาม-ตอบภาษาไทยบนคู่มือคำแนะนำการใช้สารป้องกันกำจัดศัตรูพืช ของกรมวิชาการเกษตร

A Thai-language question answering system over the Thai Department of Agriculture's
pesticide recommendation handbooks (คำแนะนำการใช้สารป้องกันกำจัดศัตรูพืช).

---

## What it does

Ask a question in Thai. Get an answer drawn only from the current edition of the handbook,
with a citation you can go and check.

> **Q:** ข้าวโพดเป็นโรคใบไหม้แผลใหญ่ ใช้สารอะไรได้บ้าง
>
> **A:** …
> *ที่มา: คำแนะนำการใช้สารป้องกันกำจัดศัตรูพืช ฉบับปี 2568, หน้า 142*

<!-- TODO: replace with a real question and answer from the system, plus a screenshot or GIF -->

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

On the query side, a question is screened, matched against the index, and the strongest
passages are passed to a language model that must answer from them and cite where each claim
came from. A second check verifies the answer is actually supported by those passages before
it reaches the user.

Full technical detail, schema, and design rationale: **[ARCHITECTURE.md](ARCHITECTURE.md)**

## How it's evaluated

<!-- TODO: mark as "planned" until the gold set actually exists -->

Evaluation targets the specific failure modes this system exists to prevent, not general
answer quality:

- **Edition correctness** — questions whose answer changed between editions must return the
  current value.
- **Supersession** — questions answerable from the archived 2566 volume must now be served
  from 2568, and must never cite the archived one.
- **Abstention** — for questions the handbook doesn't cover, refusing is the correct answer.
- **Citation validity** — every cited page must exist and must actually contain the claim.
- **Retrieval quality** — recall@k and MRR against labelled passages.

## Status

In progress. Ingestion is being built stage by stage, and each stage lands with the tests
that prove it before the next one starts.

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

**Next**

- **Parsing QA gate** — page-count cross-check, blank-page ratio, image-without-text
  detection, unmapped PUA, and validation that the hand-entered subject scopes are present.
  A document that fails is recorded and left unprocessed; there is no bypass.
- **Chunking** — prose and dosage tables need different strategies. Splitting a dosage table
  mid-row produces a chunk that states the wrong application rate, which is the most
  consequential failure this corpus allows.
- **Storage, embedding, and supersession** — these need a real Postgres with pgvector under
  test, since the guarantees that matter here are schema-level and cannot be mocked.

**Known gaps**

- Only the 2568 edition is in hand. Supersession cannot be tested end to end until 2565 and
  2566 are available, since there is nothing yet to archive.
- The PUA repair table is a hypothesis. The 2568 file contains no PUA characters at all — it
  is the 2565 cover page that carries them — so the mapping is unverified against a real
  document. The code raises on any unmapped PUA rather than dropping it silently, so the
  first 2565 ingest will say so loudly rather than corrupting a chemical name.
- Subject scopes are entered by hand, which makes supersession dependent on a human getting
  them right. The QA gate checks they are present; it cannot check they are correct.
- The retrieval side, evaluation set, and service have not been started.

## Disclaimer

This is a portfolio project. It is not an official Department of Agriculture service, and
its answers should not be relied on for real pesticide application decisions. Always consult
the published handbook.