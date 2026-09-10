"""Score query/retrieve.py against eval/questions.yaml.

A script, not a pytest module: it needs a database with the real corpus
ingested (data/raw/*.pdf, gitignored, absent in CI) and a live embedding call
per question. Run it by hand:

    uv run python -m eval.run_eval

Reads DATABASE_URL and OPENROUTER_API_KEY from .env, same as ingest scripts.
Nothing here writes to the database.
"""

import os
import sys
from dataclasses import dataclass
from pathlib import Path

import psycopg
import yaml
from dotenv import load_dotenv

from query.retrieve import Passage, make_store, retrieve

QUESTIONS_PATH = Path(__file__).resolve().parent / "questions.yaml"
K = 5  # matches the top-k rerank.py will eventually see, not CANDIDATES


@dataclass
class Result:
    id: str
    question: str
    answerable: bool
    hit: bool
    hit_rank: int | None       # 1-based rank of the passage that satisfied the check
    top_looks_confident: bool  # top-1 exists at all -- retrieve() never abstains


def _load_questions() -> list[dict]:
    return yaml.safe_load(QUESTIONS_PATH.read_text(encoding="utf-8"))


def _score_answerable(passages: list[Passage], q: dict) -> Result:
    """A hit needs BOTH expect_contains and expect_section on the same passage.

    Active ingredients repeat heavily across crop tables in this corpus --
    prochloraz and the anthracnose pathogen genus alone show up in six
    different crop sections -- so a passage matching expect_contains without
    also being the passage for the right crop is not evidence retrieval found
    the right chunk, only that hybrid search returns plausible-looking
    neighbours. expect_contains without expect_section would silently score
    "found some fungicide table" as a win.
    """
    needle = q["expect_contains"]
    section = q["expect_section"]
    hit_rank = None
    for passage in passages[:K]:
        if needle in passage.content and passage.section == section:
            hit_rank = passage.rank
            break
    return Result(
        id=q["id"],
        question=q["question"],
        answerable=True,
        hit=hit_rank is not None,
        hit_rank=hit_rank,
        top_looks_confident=bool(passages),
    )


def _score_unanswerable(passages: list[Passage], q: dict) -> Result:
    # retrieve() has no relevance gate (see its docstring: fusion_score is not
    # a threshold), so it always returns its k nearest hits regardless of
    # whether any of them mean anything. "Correct" here just records that a
    # question with no real answer still produced a full candidate list --
    # this is future work for rerank.py, not something retrieve() can fix, so
    # it is reported rather than pass/failed.
    return Result(
        id=q["id"],
        question=q["question"],
        answerable=False,
        hit=False,
        hit_rank=None,
        top_looks_confident=bool(passages),
    )


def run() -> list[Result]:
    load_dotenv()
    dsn = os.environ["DATABASE_URL"]
    store = make_store(dsn)

    results = []
    with psycopg.connect(dsn) as conn:
        for q in _load_questions():
            passages = retrieve(store, conn, q["question"], k=max(K, 10))
            if q["answerable"]:
                results.append(_score_answerable(passages, q))
            else:
                results.append(_score_unanswerable(passages, q))
    return results


def report(results: list[Result]) -> None:
    answerable = [r for r in results if r.answerable]
    hits = [r for r in answerable if r.hit]

    recall_at_k = len(hits) / len(answerable) if answerable else float("nan")
    mrr = (
        sum(1.0 / r.hit_rank for r in answerable if r.hit_rank) / len(answerable)
        if answerable
        else float("nan")
    )
    print(f"{'id':<40} {'hit':<5} {'rank':<5} question")
    for r in results:
        if r.answerable:
            print(f"{r.id:<40} {r.hit!s:<5} {r.hit_rank or '-':<5} {r.question}")
        else:
            flag = "returned candidates anyway" if r.top_looks_confident else "empty"
            print(f"{r.id:<40} {'n/a':<5} {flag:<8} {r.question}")

    print()
    print(f"answerable questions : {len(answerable)}")
    print(f"recall@{K}            : {recall_at_k:.2f}  (needle AND correct section, same passage)")
    print(f"MRR                   : {mrr:.2f}")
    print()
    print(
        "Unanswerable questions above are not scored pass/fail: retrieve() has "
        "no relevance gate by design (see its docstring), so it always returns "
        "candidates. Whether those candidates get refused is rerank.py's "
        "threshold, not built yet -- these rows are for eyeballing what a "
        "future threshold would need to reject."
    )


def main() -> int:
    results = run()
    report(results)
    answerable = [r for r in results if r.answerable]
    missed = [r for r in answerable if not r.hit]
    if missed:
        print()
        print(f"{len(missed)} answerable question(s) had no hit in the top {K}:")
        for r in missed:
            print(f"  - {r.id}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
