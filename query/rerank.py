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

from query.retrieve import Passage

RERANK_URL = "https://openrouter.ai/api/v1/rerank"
RERANK_MODEL = "nvidia/llama-nemotron-rerank-vl-1b-v2:free"
TOP_K = 5
SCORE_THRESHOLD = 0.0  # placeholder -- calibrated later against eval/questions.yaml

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
        response = requests.post(
            RERANK_URL,
            headers={
                "Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}",
                "Content-Type": "application/json",
            },
            json={
                "model": RERANK_MODEL,
                "query": question,
                "documents": [p.content for p in batch],
            },
            timeout=90,
        )
        response.raise_for_status()
        scores.extend(_parse_rerank_response(batch, response.json()))
    return scores
