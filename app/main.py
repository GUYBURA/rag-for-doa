"""HTTP surface. Transport only -- no retrieval, reranking, prompting or
model call happens in this file (CLAUDE.md: logic belongs in query/).

NOT DEPLOYABLE YET. There is no authentication, no rate limit, and no input
guard: query/guards.py, which owns PII and prompt-injection checks on the way
in and grounding checks on the way out, does not exist. Run this locally
against the local corpus until it does.
"""

import os
from collections.abc import Callable, Iterator
from contextlib import asynccontextmanager
from typing import Annotated

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Request
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, StringConstraints

from query.answer import answer as answer_question
from query.prompt import Answer
from query.retrieve import make_store

Answerer = Callable[[str], Answer]


@asynccontextmanager
async def lifespan(app: FastAPI) -> Iterator[None]:
    """Build the store and the pool once.

    make_store() costs a round trip and an engine, and it runs
    assert_model_matches_corpus() -- a corpus embedded with a different model
    than the one configured now should stop the process at startup, not
    surface as quietly bad ranking on someone's first question.
    """
    load_dotenv()
    dsn = os.environ["DATABASE_URL"]
    app.state.store = make_store(dsn)
    with ConnectionPool(dsn) as pool:
        app.state.pool = pool
        yield


app = FastAPI(title="RAG over Thai pesticide handbooks", lifespan=lifespan)


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
def ask(body: Question, answerer: Annotated[Answerer, Depends(get_answerer)]) -> AnswerOut:
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
