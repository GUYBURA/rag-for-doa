"""Score query/retrieve.py + query/rerank.py against eval/questions.yaml, and
calibrate SCORE_THRESHOLD.

A script, not a pytest module: it needs a database with the real corpus
ingested (data/raw/*.pdf, gitignored, absent in CI) and a live rerank API
call per question. Run it by hand:

    uv run python -m eval.run_eval            # score at the current threshold
    uv run python -m eval.run_eval --sweep     # search for a better one

Reads DATABASE_URL and OPENROUTER_API_KEY from .env, same as ingest scripts.
Nothing here writes to the database.
"""

import argparse
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import psycopg
import yaml
from dotenv import load_dotenv

from query.rerank import (
    SCORE_THRESHOLD,
    TOP_K,
    ScoredPassage,
    openrouter_rerank_scorer,
    rerank,
)
from query.retrieve import CANDIDATES, make_store, retrieve

QUESTIONS_PATH = Path(__file__).resolve().parent / "questions.yaml"

# Table cells are joined with literal "<br>" markers by extract.py's
# _to_markdown() -- a real newline inside a cell, not a real HTML tag. Two
# words split across a <br> ("Phakopsora<br>pachyrhizi") aren't a contiguous
# substring of chunk.content, so every containment check below normalizes it
# to a space first. Without this, none of the Latin-binomial gold questions
# below would ever register a hit, no matter how correct retrieval was.
_BR = re.compile(r"<br\s*/?>")


def _flatten(text: str) -> str:
    return re.sub(r"\s+", " ", _BR.sub(" ", text))


@dataclass
class QuestionScores:
    """One question's full candidate list, reranked but UNFILTERED by any
    threshold (rerank() was called with threshold below every possible real
    score). This is the one live call per question -- every threshold this
    script reports on, including the whole --sweep table, is computed by
    slicing this same list in Python, with no further API calls.
    """

    id: str
    question: str
    answerable: bool
    expect_contains: str | None
    expect_section: str | None
    scored: list[ScoredPassage]  # sorted desc by score, already


def _load_questions() -> list[dict]:
    return yaml.safe_load(QUESTIONS_PATH.read_text(encoding="utf-8"))


def gather() -> list[QuestionScores]:
    load_dotenv()
    dsn = os.environ["DATABASE_URL"]
    store = make_store(dsn)

    out = []
    with psycopg.connect(dsn) as conn:
        for i, q in enumerate(_load_questions()):
            if i > 0:
                # openrouter_rerank_scorer() already retries a single 429
                # with backoff, but that's sized for a transient burst, not
                # for a full gold set fired back to back -- running all 34
                # questions in this file with no pause hit 429 on OpenRouter's
                # free tier partway through and kept hitting it through every
                # retry (see KNOWLEDGE.md). This script's own bulk nature is
                # the actual cause, so the throttle belongs here, not in
                # rerank.py, which single production questions never trigger.
                time.sleep(2.0)
            passages = retrieve(store, conn, q["question"], k=CANDIDATES)
            scored = rerank(
                q["question"],
                passages,
                scorer=openrouter_rerank_scorer,
                threshold=-1.0,  # keep everything; real scores are always >= 0
                top_k=len(passages),  # keep everything; cut happens below
            )
            out.append(
                QuestionScores(
                    id=q["id"],
                    question=q["question"],
                    answerable=q["answerable"],
                    expect_contains=q.get("expect_contains"),
                    expect_section=q.get("expect_section"),
                    scored=scored,
                )
            )
    return out


def _simulate(qs: QuestionScores, threshold: float, top_k: int) -> list[ScoredPassage]:
    """What rerank() would have returned at this threshold, without calling
    the API again. Valid because qs.scored is already sorted descending by
    score (rerank() sorts before it ever filters or cuts) -- filtering a
    sorted list can't change the relative order of what survives.
    """
    return [sp for sp in qs.scored if sp.score >= threshold][:top_k]


def _is_hit(qs: QuestionScores, kept: list[ScoredPassage]) -> int | None:
    """1-based rank of the first kept passage that satisfies BOTH
    expect_contains and expect_section -- see eval/questions.yaml's own
    comment on why contains-only isn't enough here (shared active
    ingredients and pathogen genera across many crop sections).
    """
    for rank, sp in enumerate(kept, start=1):
        if qs.expect_contains in _flatten(sp.passage.content) and (
            sp.passage.section == qs.expect_section
        ):
            return rank
    return None


def report_current(all_qs: list[QuestionScores]) -> bool:
    """Score everything at the SCORE_THRESHOLD/TOP_K actually configured in
    query/rerank.py right now. Returns True iff every answerable question
    hit and every unanswerable one was refused.
    """
    print(f"current SCORE_THRESHOLD = {SCORE_THRESHOLD}, TOP_K = {TOP_K}")
    print(f"{'id':<45} {'result':<10} {'rank':<5} question")

    all_ok = True
    hits, ranks = 0, []
    answerable = [q for q in all_qs if q.answerable]
    unanswerable = [q for q in all_qs if not q.answerable]

    for qs in all_qs:
        kept = _simulate(qs, SCORE_THRESHOLD, TOP_K)
        if qs.answerable:
            hit_rank = _is_hit(qs, kept)
            ok = hit_rank is not None
            hits += ok
            if ok:
                ranks.append(hit_rank)
            all_ok &= ok
            print(f"{qs.id:<45} {'hit' if ok else 'MISS':<10} {hit_rank or '-':<5} {qs.question}")
        else:
            refused = len(kept) == 0
            all_ok &= refused
            flag = "refused" if refused else f"NOT REFUSED ({len(kept)} kept)"
            print(f"{qs.id:<45} {flag:<10} {'-':<5} {qs.question}")

    recall = hits / len(answerable) if answerable else float("nan")
    mrr = sum(1.0 / r for r in ranks) / len(answerable) if answerable else float("nan")
    refusal_rate = (
        sum(1 for qs in unanswerable if len(_simulate(qs, SCORE_THRESHOLD, TOP_K)) == 0)
        / len(unanswerable)
        if unanswerable
        else float("nan")
    )

    print()
    print(f"answerable   : {len(answerable)}  recall@{TOP_K} = {recall:.2f}  MRR = {mrr:.2f}")
    print(f"unanswerable : {len(unanswerable)}  refusal rate = {refusal_rate:.2f}")
    return all_ok


def _candidate_thresholds(all_qs: list[QuestionScores]) -> list[float]:
    """Midpoints between every distinct score actually observed. The
    threshold that best separates two classes of real-valued scores always
    sits between two observed values, never exactly on one -- trying only
    observed scores themselves risks landing exactly on a boundary a real
    future score could fall on either side of.
    """
    scores = sorted({sp.score for qs in all_qs for sp in qs.scored})
    if not scores:
        return [0.0]
    mids = [(a + b) / 2 for a, b in zip(scores, scores[1:])]
    return [0.0, *mids, scores[-1] + 1e-6]


def sweep(all_qs: list[QuestionScores]) -> float:
    """Try every candidate threshold at the real TOP_K and report the
    trade-off.

    The naive policy -- maximize negatives refused first, breaking ties by
    positives kept -- was tried first and rejected after actually looking at
    its output on this gold set: it suggested 0.80, which refuses all 6
    unanswerable questions but keeps only 1 of 28 correct answers. The
    reason is visible in the per-question scores: the hardest unanswerable
    questions (a real pest asked about the wrong real crop; a fact
    superseded out of the active edition) score in the same 0.6-0.8 band as
    plenty of genuine answers, so no threshold cleanly separates "refuse
    everything" from "keep everything" -- the naive policy just walks the
    threshold up until the last, hardest negative finally drops below it,
    however much correct recall that costs along the way.

    ARCHITECTURE.md weights abstention deliberately, but that does not mean
    trading 27 correct answers for 1 refusal is a good trade -- it means
    that when a threshold change trades one hit for one refusal, take the
    refusal. Operationalized here as: never give up achievable recall,
    then among the thresholds that preserve it, prefer the one that refuses
    the most negatives. The harder unanswerable questions this leaves
    un-refused (q31-q34: an absent crop, two mismatched real pest/crop
    pairs, one superseded fact) are exactly the cases invariant 11's other
    check -- "failed grounding check -> retry once, then refuse" -- exists
    for; that check lives downstream in prompt.py/guards.py, not yet built,
    and was never meant to be this one threshold's job alone.
    """
    answerable = [q for q in all_qs if q.answerable]
    unanswerable = [q for q in all_qs if not q.answerable]

    rows = []
    for t in _candidate_thresholds(all_qs):
        pos_ok = sum(
            1 for qs in answerable if _is_hit(qs, _simulate(qs, t, TOP_K)) is not None
        )
        neg_ok = sum(1 for qs in unanswerable if not _simulate(qs, t, TOP_K))
        rows.append((t, pos_ok, neg_ok))

    print(f"{'threshold':<12} {'positives kept':<16} {'negatives refused'}")
    for t, pos_ok, neg_ok in rows:
        print(f"{t:<12.6f} {pos_ok}/{len(answerable):<14} {neg_ok}/{len(unanswerable)}")

    best_achievable_recall = max(pos_ok for _, pos_ok, _ in rows)
    at_full_recall = [r for r in rows if r[1] == best_achievable_recall]
    best_t, _, _ = max(at_full_recall, key=lambda row: row[2])
    print()
    print(
        f"best achievable recall at TOP_K={TOP_K}: {best_achievable_recall}/{len(answerable)}"
    )
    print(f"suggested SCORE_THRESHOLD = {best_t!r} (recall-preserving)")
    return best_t


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sweep", action="store_true")
    args = parser.parse_args()

    all_qs = gather()

    if args.sweep:
        sweep(all_qs)
        return 0

    return 0 if report_current(all_qs) else 1


if __name__ == "__main__":
    sys.exit(main())
