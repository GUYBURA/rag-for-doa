"""API key authentication and the per-key rate limit on POST /ask.

Pure: no database, no model, no network. The answerer is a stub that records
every question it receives, so a test can assert not only the status code but
that a rejected request never reached the model -- the part of the contract
that costs money.

Every limiter here gets a fake clock. A window test that sleeps for 60 seconds
is slow enough that people stop running it, and flaky at the boundary on a
loaded CI machine.
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
    parse_api_keys,
)
from query.prompt import REFUSAL_TEXT, Answer, Citation
from query.retrieve import Passage

KEY_A = "key-a-0123456789abcdefghij"
KEY_B = "key-b-0123456789abcdefghij"
QUESTION = "โรคราสนิมในถั่วเหลืองใช้สารอะไร"


class FakeClock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _answered() -> Answer:
    return Answer(
        "ใช้ tebuconazole",
        [
            Citation(
                number=1,
                passage=Passage(
                    chunk_id=uuid.uuid4(),
                    content="เนื้อหา",
                    page_number=56,
                    section="ถั่วเหลือง (Soybean)",
                    document_id=uuid.uuid4(),
                    edition_year_be=2568,
                    title_th="คู่มือ",
                    source="2568.pdf",
                    rank=1,
                    fusion_score=0.0,
                ),
                score=0.7,
            )
        ],
    )


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def calls() -> list[str]:
    return []


@pytest.fixture
def client(clock, calls):
    """A fresh limiter per test. The real one is module state on the app, and
    a count leaking from one test into the next makes results depend on test
    order rather than on the code.
    """
    limiter = FixedWindowLimiter(RATE_LIMIT_PER_WINDOW, RATE_LIMIT_WINDOW_SECONDS, clock=clock)

    def answerer():
        def run(question: str) -> Answer:
            calls.append(question)
            return _answered()

        return run

    app.dependency_overrides[get_api_keys] = lambda: frozenset({KEY_A, KEY_B})
    app.dependency_overrides[get_limiter] = lambda: limiter
    app.dependency_overrides[get_answerer] = answerer
    yield TestClient(app)
    app.dependency_overrides.clear()


def _ask(client: TestClient, key: str | None = KEY_A):
    headers = {} if key is None else {"X-API-Key": key}
    return client.post("/ask", json={"question": QUESTION}, headers=headers)


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


def test_parse_api_keys_strips_and_drops_blanks():
    assert parse_api_keys(" a , b,,  ") == frozenset({"a", "b"})
    assert parse_api_keys("") == frozenset()


def test_a_missing_key_is_401_and_never_reaches_the_answerer(client, calls):
    assert _ask(client, key=None).status_code == 401
    assert calls == []


def test_a_wrong_key_is_indistinguishable_from_a_missing_one(client, calls):
    """Different messages would tell an attacker the header name is right and
    only the value is wrong. Status alone would pass with the leak in place,
    so the bodies are compared whole.
    """
    missing = _ask(client, key=None)
    wrong = _ask(client, key="not-a-real-key")
    assert wrong.status_code == missing.status_code == 401
    assert wrong.json() == missing.json()
    assert calls == []


def test_a_valid_key_is_answered(client, calls):
    """Twin of the 401 tests: without it, rejecting everything passes them."""
    response = _ask(client)
    assert response.status_code == 200
    assert calls == [QUESTION]


def test_every_configured_key_is_accepted(client):
    assert _ask(client, key=KEY_A).status_code == 200
    assert _ask(client, key=KEY_B).status_code == 200


def test_an_empty_key_header_is_rejected_even_with_blank_entries_configured(client, calls):
    """A trailing comma in API_KEYS must not configure the empty string as a
    key, or `X-API-Key:` with no value would authenticate.
    """
    app.dependency_overrides[get_api_keys] = lambda: parse_api_keys(" , a ,")
    assert _ask(client, key="").status_code == 401
    assert calls == []
    assert _ask(client, key="a").status_code == 200


def test_no_configured_keys_rejects_everything(client, calls):
    """Fail closed: a missing API_KEYS must not mean "no auth"."""
    app.dependency_overrides[get_api_keys] = lambda: frozenset()
    assert _ask(client, key=KEY_A).status_code == 401
    assert _ask(client, key="").status_code == 401
    assert calls == []


def test_health_needs_no_key(client):
    assert client.get("/health").status_code == 200


# ---------------------------------------------------------------------------
# Rate limit
# ---------------------------------------------------------------------------


def test_the_tenth_request_in_a_window_is_answered(client, calls):
    """Off-by-one twin of the 429 test: the limit is 10 allowed, not 9."""
    for _ in range(RATE_LIMIT_PER_WINDOW):
        assert _ask(client).status_code == 200
    assert len(calls) == RATE_LIMIT_PER_WINDOW


def test_the_eleventh_request_is_429_with_retry_after_and_never_reaches_the_answerer(
    client, calls
):
    for _ in range(RATE_LIMIT_PER_WINDOW):
        _ask(client)
    response = _ask(client)
    assert response.status_code == 429
    assert "retry-after" in response.headers
    assert len(calls) == RATE_LIMIT_PER_WINDOW


def test_retry_after_is_the_time_left_in_the_window(client, clock):
    for _ in range(RATE_LIMIT_PER_WINDOW):
        _ask(client)
    clock.advance(15)
    response = _ask(client)
    assert response.status_code == 429
    assert response.headers["retry-after"] == str(RATE_LIMIT_WINDOW_SECONDS - 15)


def test_a_new_window_answers_again(client, clock):
    """Twin of the 429 test: a limiter that blocks forever passes that one."""
    for _ in range(RATE_LIMIT_PER_WINDOW):
        _ask(client)
    assert _ask(client).status_code == 429
    clock.advance(RATE_LIMIT_WINDOW_SECONDS)
    assert _ask(client).status_code == 200


def test_one_key_exhausted_does_not_block_the_other(client):
    """The reason there are two keys: testing with curl must not lock out the
    frontend. A single shared counter passes every single-key test.
    """
    for _ in range(RATE_LIMIT_PER_WINDOW):
        _ask(client, key=KEY_A)
    assert _ask(client, key=KEY_A).status_code == 429
    assert _ask(client, key=KEY_B).status_code == 200


def test_a_refusal_counts_toward_the_limit(client, calls):
    """A refusal is a correct answer, not a free one: it can still cost a
    rerank call and a model call.
    """
    app.dependency_overrides[get_answerer] = lambda: (
        lambda question: calls.append(question) or Answer(REFUSAL_TEXT, [])
    )
    for _ in range(RATE_LIMIT_PER_WINDOW):
        response = _ask(client)
        assert response.status_code == 200
        assert response.json()["citations"] == []
    assert _ask(client).status_code == 429


def test_rejected_keys_do_not_consume_a_valid_keys_quota(client):
    """Auth runs before counting. If it did not, the 11th garbage request
    would be a 429 instead of a 401 -- telling the caller it is being
    tracked -- and the limiter would hold an entry for every garbage key
    anyone cares to send, which is unbounded memory. The valid key's quota
    afterwards is the twin: it proves the garbage was not counted anywhere
    that key can see.
    """
    for _ in range(2 * RATE_LIMIT_PER_WINDOW):
        assert _ask(client, key="garbage").status_code == 401
    for _ in range(RATE_LIMIT_PER_WINDOW):
        assert _ask(client).status_code == 200
    assert _ask(client).status_code == 429
