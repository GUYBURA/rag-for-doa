"""HTTP surface. Transport only -- no retrieval, reranking, prompting or
model call happens in this file (CLAUDE.md: logic belongs in query/).

Authentication and rate limiting live here because they are transport
concerns: they decide whether a request gets to ask at all, and know nothing
about what is asked. The guards on the question and the answer themselves
(PII, grounding, injection) are in query/guards.py.
"""

import hmac
import math
import os
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import asynccontextmanager
from typing import Annotated

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, StringConstraints

from query.answer import answer as answer_question
from query.prompt import Answer
from query.retrieve import make_store

Answerer = Callable[[str], Answer]

RATE_LIMIT_PER_WINDOW = 10
RATE_LIMIT_WINDOW_SECONDS = 60


def parse_api_keys(raw: str) -> frozenset[str]:
    """API_KEYS is comma-separated. Blank entries are dropped, not kept: a
    trailing comma would otherwise configure "" as a key, and a request with
    an empty X-API-Key header would authenticate.
    """
    return frozenset(k.strip() for k in raw.split(",") if k.strip())


class FixedWindowLimiter:
    """Per-key request count over a fixed window, held in this process's memory.

    Correct only while exactly one instance serves traffic: each Cloud Run
    instance has its own memory, so N instances would allow N times the limit.
    Deploy with max-instances=1. Raising that means moving the count to a
    shared store, not raising the number here.

    Deliberately no daily cap. Cloud Run scales to zero when idle and wipes
    this memory, so a daily count would reset every time the service slept.
    The money ceiling is the OpenRouter key's spending limit, which lives
    outside this process and survives restarts. This class only stops bursts.

    The dict grows by one entry per key that passes auth, never per garbage
    key, because enforce_rate_limit() depends on require_api_key().
    """

    def __init__(
        self,
        limit: int,
        window_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._limit = limit
        self._window = window_seconds
        self._clock = clock
        self._windows: dict[str, tuple[float, int]] = {}
        self._lock = threading.Lock()

    def hit(self, key: str) -> float | None:
        """Record one request. None if allowed; otherwise the seconds left
        until this key's window resets, and nothing is recorded.
        """
        now = self._clock()
        # FastAPI runs sync endpoints in a threadpool, so two requests with
        # the same key can read the same count and both pass without a lock.
        with self._lock:
            start, count = self._windows.get(key, (now, 0))
            if now - start >= self._window:
                start, count = now, 0
            if count >= self._limit:
                return self._window - (now - start)
            self._windows[key] = (start, count + 1)
            return None


@asynccontextmanager
async def lifespan(app: FastAPI) -> Iterator[None]:
    """Build the store and the pool once.

    make_store() costs a round trip and an engine, and it runs
    assert_model_matches_corpus() -- a corpus embedded with a different model
    than the one configured now should stop the process at startup, not
    surface as quietly bad ranking on someone's first question.
    """
    load_dotenv()
    # Fail at startup, not on every request: with no keys configured every
    # /ask would be a 401, and a deploy that looks healthy but rejects
    # everyone is harder to notice than one that does not start.
    if not parse_api_keys(os.environ.get("API_KEYS", "")):
        raise RuntimeError("API_KEYS is empty -- set at least one key")
    dsn = os.environ["DATABASE_URL"]
    app.state.store = make_store(dsn)
    with ConnectionPool(dsn) as pool:
        app.state.pool = pool
        yield


app = FastAPI(title="RAG over Thai pesticide handbooks", lifespan=lifespan)
app.state.limiter = FixedWindowLimiter(RATE_LIMIT_PER_WINDOW, RATE_LIMIT_WINDOW_SECONDS)


def get_api_keys() -> frozenset[str]:
    """A dependency, like get_answerer, so tests configure keys without
    touching the environment. Read per request so a rotated secret takes
    effect on the next revision without a code change.
    """
    return parse_api_keys(os.environ.get("API_KEYS", ""))


def get_limiter(request: Request) -> FixedWindowLimiter:
    return request.app.state.limiter


def require_api_key(
    keys: Annotated[frozenset[str], Depends(get_api_keys)],
    x_api_key: Annotated[str | None, Header()] = None,
) -> str:
    """The matching key, or 401.

    Missing and wrong keys get the same response, so the reply never says
    which half of the guess was right.

    hmac.compare_digest, not ==. String == returns as soon as one character
    differs, so response time leaks how much of a guess matched. No test in
    this suite can catch a swap back to == -- the difference is nanoseconds
    -- so this comment and code review are the only guard on it.
    """
    if x_api_key is not None:
        for key in keys:
            if hmac.compare_digest(x_api_key.encode(), key.encode()):
                return key
    raise HTTPException(status_code=401, detail="invalid or missing API key")


def enforce_rate_limit(
    key: Annotated[str, Depends(require_api_key)],
    limiter: Annotated[FixedWindowLimiter, Depends(get_limiter)],
) -> None:
    """429 once a key is over its limit. Depends on require_api_key, so auth
    always resolves first and a rejected key never consumes anyone's quota.
    """
    retry_after = limiter.hit(key)
    if retry_after is not None:
        raise HTTPException(
            status_code=429,
            detail="rate limit exceeded",
            headers={"Retry-After": str(math.ceil(retry_after))},
        )


def get_answerer(request: Request) -> Answerer:
    """The seam the tests replace, so the HTTP layer can be exercised with no
    database and no model behind it.
    """

    def run(question: str) -> Answer:
        with request.app.state.pool.connection() as conn:
            return answer_question(question, request.app.state.store, conn)

    return run


class Question(BaseModel):
    # A blank question is a malformed request, not an unanswerable one: 422,
    # not a refusal. Refusal means the corpus had nothing to say.
    question: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1)
    ]


class CitationOut(BaseModel):
    """Everything needed to open the source and check the claim (invariant
    10). `score` is the rerank score, exposed for debugging and for the
    frontend to show how strong a match each source was.
    """

    number: int
    title_th: str
    edition_year_be: int
    page_number: int | None
    section: str | None
    source: str
    score: float


class AnswerOut(BaseModel):
    answer: str
    citations: list[CitationOut]


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/ask")
def ask(
    body: Question,
    _: Annotated[None, Depends(enforce_rate_limit)],
    answerer: Annotated[Answerer, Depends(get_answerer)],
) -> AnswerOut:
    """Answer one question, or refuse.

    A refusal is a 200 with an empty citations list, not a 404. Declining to
    answer is a correct outcome (invariant 11), and modelling it as an HTTP
    error would push every client into treating the system's most important
    behaviour as a failure.
    """
    result = answerer(body.question)
    return AnswerOut(
        answer=result.text,
        citations=[
            CitationOut(
                number=c.number,
                title_th=c.passage.title_th,
                edition_year_be=c.passage.edition_year_be,
                page_number=c.passage.page_number,
                section=c.passage.section,
                source=c.passage.source,
                score=c.score,
            )
            for c in result.citations
        ],
    )
