"""Pure tests for rerank()'s selection logic. No network, no container -- every
test here supplies its own fake Scorer, so what's under test is purely
threshold / sort / cut, never the real API.
"""

import time
import uuid

import pytest
import requests

import query.rerank as rerank_module
from query.rerank import (
    _parse_rerank_response,
    openrouter_rerank_scorer,
    rerank,
)
from query.retrieve import Passage, retrieve
from tests.test_retrieve import SOYBEAN


def _passage(rank: int, content: str = "") -> Passage:
    """A minimal Passage for a given retrieval rank. Every other field is a
    placeholder -- these tests never look at content or citation fields,
    only at rank (for tie-breaking) and whatever score the fake scorer hands
    back for this passage's position.

    The `content` default ("") is fine everywhere content is never actually
    read -- the pure rerank() tests above never call a real model on it.
    The live tests below pass real Thai text, because there the model
    genuinely has to judge it.
    """
    return Passage(
        chunk_id=uuid.uuid4(),
        content=content or f"content {rank}",
        page_number=1,
        section=None,
        document_id=uuid.uuid4(),
        edition_year_be=2568,
        title_th="x",
        source="x.pdf",
        rank=rank,
        fusion_score=0.0,
    )


def _stub_scorer(scores: list[float]):
    """A Scorer that ignores the question and returns `scores` positionally,
    aligned with whatever passages it's called with -- exactly the contract
    Scorer promises, with no HTTP call behind it.
    """

    def scorer(question, passages):
        assert len(passages) == len(scores)
        return scores

    return scorer


def _raising_scorer(question, passages):
    """A Scorer that fails loudly if it's ever called -- for the one test
    that asserts the scorer is *not* called at all.
    """
    raise AssertionError("scorer should not have been called")


# ---------------------------------------------------------------------------
# threshold: the refusal mechanism itself (invariant 11)
# ---------------------------------------------------------------------------


def test_all_below_threshold_returns_no_passages():
    passages = [_passage(1),_passage(2),_passage(3)]
    scorer = _stub_scorer([0.0,0.1,0.3])
    result = rerank("Testing Testo Testu", passages, scorer=scorer, threshold=1.0)
    assert result == []


def test_a_passage_at_or_above_threshold_is_kept():
    passages = [_passage(3),_passage(2),_passage(1)]
    scorer = _stub_scorer([0.2,0.3,0.6])
    result = rerank("Testing Testo Testu", passages, scorer=scorer, threshold=0.5)
    assert len(result) == 1

# ---------------------------------------------------------------------------
# boundary: >= or >?
# ---------------------------------------------------------------------------


def test_score_exactly_at_threshold_is_kept():
    threshold = 0.01
    passages = [_passage(1),_passage(2)]
    scorer = _stub_scorer([0.01,0.01])  # exactly equal to threshold

    result = rerank("q", passages, scorer=scorer, threshold=threshold)

    assert len(result) == 2


def test_score_just_below_threshold_is_dropped():
    threshold = 0.01
    passages = [_passage(1)]
    scorer = _stub_scorer([0.00])  # just under threshold

    result = rerank("q", passages, scorer=scorer, threshold=threshold)

    assert result == []


# ---------------------------------------------------------------------------
# ordering: by score, not by retrieval rank
# ---------------------------------------------------------------------------


def test_result_is_ordered_by_score_not_retrieval_rank():
    passages = [_passage(1), _passage(2), _passage(3)]
    scorer = _stub_scorer([0.1,0.3,0.5])
    result = rerank("q", passages, scorer=scorer, threshold=0.0)
    assert [r.passage.rank for r in result] == [3,2,1]


def test_ties_break_by_retrieval_rank_stably():
    passages = [_passage(2), _passage(1)]
    scorer = _stub_scorer([0.01, 0.01])

    result = rerank("q", passages, scorer=scorer, threshold=0.0)

    assert [r.passage.rank for r in result] == [1, 2]


# ---------------------------------------------------------------------------
# top_k: cut AFTER filter+sort, never before (see the worked example)
# ---------------------------------------------------------------------------


def test_more_than_top_k_pass_returns_only_the_highest_scoring():
    passages = [_passage(i) for i in range(1, 9)]  # rank 1..8, in that order
    scores = [0.5, 0.9, 0.1, 0.7, 0.2, 0.8, 0.3, 0.6]
    scorer = _stub_scorer(scores)
    result = rerank("q", passages, scorer=scorer, threshold=0.0, top_k=5)
    assert [r.passage.rank for r in result] == [2, 6, 4, 8, 1]

# ---------------------------------------------------------------------------
# empty input: no passages in, no network call, nothing out
# ---------------------------------------------------------------------------


def test_empty_passages_returns_empty_without_calling_scorer():
    result = rerank("q", [], scorer=_raising_scorer, threshold=0.0)

    assert result == []


# ---------------------------------------------------------------------------
# ScoredPassage carries every citation field through untouched
# ---------------------------------------------------------------------------


def test_scored_passage_preserves_every_citation_field():
    passage = _passage(1)
    scorer = _stub_scorer([0.5])
    result = rerank("q", [passage], scorer=scorer, threshold=0.0)
    assert result[0].passage == passage


# ---------------------------------------------------------------------------
# _parse_rerank_response(): pure re-alignment of a real API response shape.
# No network -- every test below builds its own response dict by hand, shaped
# exactly like what the live probe returned:
#   {"results": [{"index": int, "relevance_score": float, "document": {...}}]}
# ---------------------------------------------------------------------------


def _result(index: int, score: float) -> dict:
    """One entry of a rerank response's "results" list. The "document" field
    is real-shaped but its content is never used by the parser -- only
    "index" and "relevance_score" matter for re-alignment.
    """
    return {"index": index, "relevance_score": score, "document": {"text": "x"}}


def test_reordered_response_is_realigned_to_input_order():
    passages = [_passage(1), _passage(2), _passage(3)]
    response = {"results": [
            _result(2, 0.99),
            _result(0, 0.5),
            _result(1, 0.1),
        ]
    }

    scores = _parse_rerank_response(passages, response)
    assert scores == [0.50, 0.10, 0.99]

def test_a_missing_index_raises():
    # ___: 3 passages, but the response only covers 2 of the 3 indices.
    passages = [_passage(1), _passage(2), _passage(3)]
    response = {"results": [
            _result(0, 0.5),
            _result(2, 0.1)
        ]
    }

    with pytest.raises(ValueError):
        _parse_rerank_response(passages, response)


def test_a_duplicate_index_raises():
    passages = [_passage(1), _passage(2), _passage(3)]
    response = {"results": [
            _result(0,0.5),
            _result(1,0.1),
            _result(1,0.2)
        ]
    }

    with pytest.raises(ValueError):
        _parse_rerank_response(passages, response)


def test_an_unknown_index_raises():
    passages = [_passage(1), _passage(2), _passage(3)]
    response = {"results": [
            _result(1,0.1),
            _result(0,0.2),
            _result(3,0.5)
        ]
    }

    with pytest.raises(ValueError):
        _parse_rerank_response(passages, response)


def test_a_score_above_one_raises():
    passages = [_passage(1)]
    response = {"results": [_result(0, 2.0)]}  # ___: something > 1.0

    with pytest.raises(ValueError):
        _parse_rerank_response(passages, response)


def test_a_score_below_zero_raises():
    passages = [_passage(1)]
    response = {"results": [_result(0, -0.1)]}  # ___: something < 0.0

    with pytest.raises(ValueError):
        _parse_rerank_response(passages, response)


# ---------------------------------------------------------------------------
# 429 retry: no network. requests.post is faked entirely, so this proves the
# retry loop itself, not OpenRouter's actual rate limit -- found for real
# running eval/run_eval.py's 34 questions back to back (a single manual
# probe never triggers it, only a burst of calls does; see KNOWLEDGE.md).
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None):
        self.status_code = status_code
        self._payload = payload

    def raise_for_status(self) -> None:
        if self.status_code != 200:
            raise requests.exceptions.HTTPError(response=self)

    def json(self) -> dict:
        return self._payload


def test_a_429_is_retried_until_it_succeeds(monkeypatch):
    # tenacity's backoff really sleeps between attempts; skip the wait so
    # the test doesn't take several real seconds.
    monkeypatch.setattr(time, "sleep", lambda seconds: None)
    # requests.post is faked below and never actually sends this, but
    # _post_rerank() builds the Authorization header unconditionally before
    # calling it -- os.environ["OPENROUTER_API_KEY"] still has to resolve to
    # something, or this fails with KeyError in any environment (CI
    # included) that has no real key set, before the fake ever runs.
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")

    calls = []

    def fake_post(url, **kwargs):
        calls.append(1)
        if len(calls) < 3:
            return _FakeResponse(429)
        return _FakeResponse(200, {"results": [_result(0, 0.5)]})

    monkeypatch.setattr(rerank_module.requests, "post", fake_post)

    scores = openrouter_rerank_scorer("q", [_passage(1)])

    assert scores == [0.5]
    assert len(calls) == 3


def test_a_non_429_error_is_not_retried(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")  # see the test above

    calls = []

    def fake_post(url, **kwargs):
        calls.append(1)
        return _FakeResponse(400)

    monkeypatch.setattr(rerank_module.requests, "post", fake_post)

    with pytest.raises(requests.exceptions.HTTPError):
        openrouter_rerank_scorer("q", [_passage(1)])

    assert len(calls) == 1


# ---------------------------------------------------------------------------
# openrouter_rerank_scorer(): live, real HTTP, real model. No corpus, no
# retrieve() -- these hand-build a couple of Passage values directly, the
# same way _passage() does above but with real Thai content this time,
# because the model actually has to judge it.
# ---------------------------------------------------------------------------


@pytest.mark.requires_rerank
def test_a_relevant_passage_outscores_an_irrelevant_one():
    question = "โรคราสนิมในถั่วเหลืองเกิดจากเชื้อราอะไร ใช้สารอะไรป้องกัน"
    relevant = _passage(
        1,
        content=(
            "โรคราสนิม เชื้อราสาเหตุ Phakopsora pachyrhizi "
            "ทีบูโคนาโซล (tebuconazole) 25% EW"
        ),
    )
    irrelevant = _passage(
        2, content="มอดเจาะผลกาแฟ (Hypothenemus hampei) ไตรอะโซฟอส (triazophos) 40% EC"
    )
    scores = openrouter_rerank_scorer(question, [relevant, irrelevant])
    assert scores[0] > scores[1]


@pytest.mark.requires_rerank
def test_live_scores_are_within_valid_range():
    passages = [_passage(1), _passage(2), _passage(3)]
    scores = openrouter_rerank_scorer("q", passages)
    assert len(scores) == len(passages)
    assert all(0.0 <= s <= 1.0 for s in scores)


# ---------------------------------------------------------------------------
# End to end: retrieve() -> rerank() against the real ingested corpus
# (tests/fixtures/excerpt.pdf, via the shared `store` / `corpus_conn`
# fixtures from conftest.py -- same corpus test_retrieve.py uses).
# ---------------------------------------------------------------------------


@pytest.mark.requires_rerank
@pytest.mark.requires_embeddings
def test_retrieve_then_rerank_keeps_at_most_five_from_the_expected_section(
    store, corpus_conn
):
    question = "โรคราสนิมในถั่วเหลือง"
    passages = retrieve(store, corpus_conn, question, k=10)

    result = rerank(question, passages, scorer=openrouter_rerank_scorer)

    assert len(result) <= 5
    assert result[0].passage.section == SOYBEAN


@pytest.mark.requires_rerank
@pytest.mark.requires_embeddings
def test_an_out_of_domain_question_is_refused_after_rerank(store, corpus_conn):
    # Same question as eval/questions.yaml's q29_out_of_domain, whose
    # measured top score (0.418) is what calibrated SCORE_THRESHOLD (0.42)
    # sits just above -- see query/rerank.py's comment on that constant.
    # This is invariant 11's actual enforcement point: the first case
    # anywhere in the system where a question gets refused instead of
    # answered.
    question = "วิธีต้มส้มตำให้อร่อยทำอย่างไร"
    passages = retrieve(store, corpus_conn, question, k=10)

    result = rerank(question, passages, scorer=openrouter_rerank_scorer)

    assert result == []
