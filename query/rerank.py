"""Absolute relevance scoring on top of retrieve.py's candidates.

retrieve.py deliberately has no relevance gate: RRF fusion scores are only
meaningful relative to one search's own result set (see its docstring). This
module is where invariant 11 ("Refusal is a valid output... Below-threshold
reranking -> 'no relevant document'") actually gets enforced -- everything
here scores one passage against one question on a fixed scale, so a threshold
comparison means something.
"""

import os
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import requests
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from query.retrieve import Passage

RERANK_URL = "https://openrouter.ai/api/v1/rerank"
RERANK_MODEL = "voyageai/rerank-2.5-lite"
# Not nvidia/llama-nemotron-rerank-vl-1b-v2:free (used during initial design
# and probing -- see KNOWLEDGE.md): every ":free"-suffixed model on
# OpenRouter shares one 50-requests-per-day cap across the whole account
# (raised to 1000/day only above $10 lifetime purchased, not just a
# balance), which a single eval/run_eval.py pass across a few dozen
# questions exhausts on its own. This model is billed instead --
# effectively free in practice (~$0.000004/call on real Thai chunks) but
# drawn from ordinary credit, not the capped free-model pool, and its scores
# also land on a far more legible 0-1 scale (observed: 0.75 / 0.61 / 0.31 for
# a clearly-relevant/somewhat-relevant/irrelevant triple) than the earlier
# model's 0.0001-0.06 range.
TOP_K = 5

# Calibrated against eval/questions.yaml via `uv run python -m eval.run_eval
# --sweep`: the value that preserves the best achievable recall@5 while
# refusing as many unanswerable questions as any threshold that costs no
# recall. The one recall miss, a strawberry pest question, ranks 7th of 20
# candidates regardless of threshold, so no threshold recovers it.
#
# Re-verified after extract.py's table rendering changed and the whole corpus
# was re-ingested (chunk text changed, so every score did): on the now 29
# answerable / 5 unanswerable gold set, recall@5 = 28/29 and one refusal,
# unchanged. The sweep's own suggestion moved to 0.3916, which sits on the
# same plateau -- identical recall, identical refusals -- so 0.42 stays
# rather than churning the constant for no measured gain.
#
# At this value it refuses the one unambiguously off-domain gold question
# (top score 0.418 before re-ingest). It does NOT
# reliably refuse the harder unanswerable questions -- a fabricated
# chemical, an absent crop, a real pest asked about the wrong real crop --
# whose scores (0.62-0.80) overlap the range real answers
# score in; no single threshold on this scorer separates those cleanly (see
# KNOWLEDGE.md and eval/run_eval.py's sweep() docstring). That gap is
# invariant 11's other check ("failed grounding check -> retry once, then
# refuse"), not yet built (prompt.py/guards.py) -- this threshold was never
# meant to catch those alone.
SCORE_THRESHOLD = 0.42

# == retrieve.CANDIDATES. Probed against 20 real chunks from the local
# corpus: no error, no truncation, all 20 indices returned -- see
# KNOWLEDGE.md. openrouter_rerank_scorer() still batches at this size in
# case CANDIDATES ever grows past what the model's context can hold.
MAX_DOCUMENTS = 20


@dataclass(frozen=True)
class ScoredPassage:
    """A Passage plus its absolute relevance score. Nothing about the Passage
    itself is touched -- every citation field it carries survives reranking
    unchanged.
    """

    passage: Passage
    score: float


class Scorer(Protocol):
    def __call__(self, question: str, passages: Sequence[Passage]) -> list[float]:
        """Absolute relevance per passage, aligned positionally with the input.

        result[i] is the score for passages[i]. Implementations that call an
        API whose response comes back in a different order (see
        openrouter_rerank_scorer) must re-align before returning -- this
        contract is positional, always.
        """
        ...


def rerank(
    question: str,
    passages: Sequence[Passage],
    *,
    scorer: Scorer,
    threshold: float = SCORE_THRESHOLD,
    top_k: int = TOP_K,
) -> list[ScoredPassage]:
    if not passages:
        return []
    scores = scorer(question,passages)
    kept = [ScoredPassage(p,s) for p,s in zip(passages, scores) if s >= threshold]
    kept.sort(key=lambda sp: (-sp.score, sp.passage.rank))
    return kept[:top_k]


def _parse_rerank_response(
    passages: Sequence[Passage], response: dict[str, Any]
) -> list[float]:
    """Pure: turn one rerank API response into scores aligned with `passages`.

    The API returns results sorted by score, each carrying the `index` of the
    document it scored -- NOT in request order (see the design discussion:
    zipping response order onto input order pairs the wrong passage with the
    wrong score, which after the top-k cut means citing a page that doesn't
    contain the claim). This function is the one place that re-alignment
    happens, kept separate from the HTTP call so it's testable with a
    hand-built dict and no network -- same split as chunk.py's
    _split_table_markdown() living apart from chunk_document().

    Raises on: a missing index, a duplicate index, an index outside
    range(len(passages)), a result count that doesn't match len(passages), or
    any relevance_score outside [0, 1]. Every one of these is a contract
    violation from the API side that would otherwise corrupt a citation
    silently.
    """
    results = response["results"]

    if len(results) != len(passages):
        raise ValueError

    score_and_index = {r["index"]: r["relevance_score"] for r in results}

    if len(set(score_and_index)) < len(results):
        raise ValueError

    for i in score_and_index:
        if not (0 <= i < len(passages)):
            raise ValueError

    if any(v > 1.0 or v < 0.0 for v in score_and_index.values()):
        raise ValueError

    final_scores = [None] * len(passages)
    for r in results:
        final_scores[r["index"]] = r["relevance_score"]

    return final_scores


def _is_rate_limited(exc: BaseException) -> bool:
    """True only for a 429 from OpenRouter. Any other HTTP error (a bad
    request, an auth failure) should fail immediately -- retrying those
    just delays a diagnosis that retrying can't fix.
    """
    return (
        isinstance(exc, requests.exceptions.HTTPError)
        and exc.response is not None
        and exc.response.status_code == 429
    )


@retry(
    retry=retry_if_exception(_is_rate_limited),
    wait=wait_exponential(multiplier=1, min=2, max=60),
    stop=stop_after_attempt(6),
    reraise=True,
)
def _post_rerank(payload: dict[str, Any]) -> dict[str, Any]:
    """One HTTP call, retried with exponential backoff on 429 only.

    Found for real: a 34-question eval run (eval/run_eval.py, one rerank
    call per question) hit 429 on OpenRouter's free tier partway through --
    a single manual probe never triggers this, only a burst of calls does.
    OpenRouter did not expose a Retry-After header on the 429 response, so
    this backs off blind rather than honouring one.
    """
    response = requests.post(
        RERANK_URL,
        headers={
            "Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=90,
    )
    response.raise_for_status()
    return response.json()


def openrouter_rerank_scorer(question: str, passages: Sequence[Passage]) -> list[float]:
    """The real Scorer. One HTTP call per MAX_DOCUMENTS-sized batch -- never
    more than one batch in practice at CANDIDATES=20 (probed with 20 real
    chunks, no truncation, no error -- see KNOWLEDGE.md), but the loop still
    holds if that constant ever grows.

    Splitting into batches is safe here specifically because a cross-encoder
    scores each (query, document) pair independently -- unlike an
    LLM-as-reranker, where every passage in one prompt can influence how the
    model judges the others, splitting a batch here never changes a score.

    top_n is deliberately never sent (see MAX_DOCUMENTS above): every
    passage must come back with a score so rerank() can filter and sort them
    all itself, uniformly across whatever Scorer backend is behind it.
    """
    scores: list[float] = []
    for start in range(0, len(passages), MAX_DOCUMENTS):
        batch = passages[start : start + MAX_DOCUMENTS]
        payload = {
            "model": RERANK_MODEL,
            "query": question,
            "documents": [p.content for p in batch],
        }
        scores.extend(_parse_rerank_response(batch, _post_rerank(payload)))
    return scores
