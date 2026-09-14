"""HTTP-shape tests. The answerer dependency is replaced with a stub, so no
database, no model and no pipeline run here -- what is under test is status
codes and the response body.

The client is deliberately NOT used as a context manager: that is what runs
the lifespan handler, which would try to build a PGVectorStore and a
connection pool against a database CI does not have.
"""

import uuid

import pytest
from fastapi.testclient import TestClient

from app.main import (
    RATE_LIMIT_PER_WINDOW,
    RATE_LIMIT_WINDOW_SECONDS,
    FixedWindowLimiter,
    app,
    get_answerer,
    get_api_keys,
    get_limiter,
)
from query.prompt import REFUSAL_TEXT, Answer, Citation
from query.retrieve import Passage

TITLE = "คำแนะนำการใช้สารป้องกันกำจัดศัตรูพืช ฉบับปี 2568"
KEY = "test-key-0123456789abcdefghij"


def _citation(number: int) -> Citation:
    return Citation(
        number=number,
        passage=Passage(
            chunk_id=uuid.uuid4(),
            content="เนื้อหา",
            page_number=56,
            section="ถั่วเหลือง (Soybean)",
            document_id=uuid.uuid4(),
            edition_year_be=2568,
            title_th=TITLE,
            source="2568.pdf",
            rank=1,
            fusion_score=0.0,
        ),
        score=0.71,
    )


@pytest.fixture
def client():
    """Cleared after each test: dependency_overrides lives on the app object,
    so a leaked one would quietly answer a later test's requests.

    Auth stays on here and every request carries a real key -- these tests
    are about response shape, and switching auth off to test that would
    leave nothing proving the shape survives with auth in front of it. Auth
    and the limit themselves are tested in test_auth.py.
    """
    app.dependency_overrides[get_api_keys] = lambda: frozenset({KEY})
    limiter = FixedWindowLimiter(RATE_LIMIT_PER_WINDOW, RATE_LIMIT_WINDOW_SECONDS)
    app.dependency_overrides[get_limiter] = lambda: limiter
    yield TestClient(app, headers={"X-API-Key": KEY})
    app.dependency_overrides.clear()


def _answering(result: Answer):
    return lambda: (lambda question: result)


def test_health_is_reachable_without_a_database(client):
    assert client.get("/health").status_code == 200


def test_an_answered_question_carries_every_citation_field(client):
    app.dependency_overrides[get_answerer] = _answering(
        Answer("โรคราสนิมเกิดจาก Phakopsora pachyrhizi", [_citation(1)])
    )
    response = client.post("/ask", json={"question": "โรคราสนิมถั่วเหลือง"})
    assert response.status_code == 200
    body = response.json()
    assert body["answer"] == "โรคราสนิมเกิดจาก Phakopsora pachyrhizi"
    # Invariant 10: a reader has to be able to open the source and check the
    # claim, which takes all four of these, not just the page.
    assert body["citations"] == [
        {
            "number": 1,
            "title_th": TITLE,
            "edition_year_be": 2568,
            "page_number": 56,
            "section": "ถั่วเหลือง (Soybean)",
            "source": "2568.pdf",
            "score": 0.71,
        }
    ]


def test_a_refusal_is_a_200_not_a_404(client):
    """Declining to answer is a correct outcome (invariant 11). Returning 404
    would make every client treat the system's most important behaviour as a
    failure.
    """
    app.dependency_overrides[get_answerer] = _answering(Answer(REFUSAL_TEXT, []))
    response = client.post("/ask", json={"question": "วิธีต้มไข่"})
    assert response.status_code == 200
    assert response.json() == {"answer": REFUSAL_TEXT, "citations": []}


def test_a_blank_question_is_rejected_not_refused(client):
    """Whitespace only is a malformed request, not an unanswerable one -- and
    the distinction matters, because a refusal is a claim about the corpus.
    """
    app.dependency_overrides[get_answerer] = _answering(Answer("should not run", []))
    assert client.post("/ask", json={"question": "   "}).status_code == 422


def test_a_missing_question_field_is_rejected(client):
    assert client.post("/ask", json={}).status_code == 422
