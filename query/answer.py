"""One question in, one grounded Answer out: retrieve, rerank, ask, parse.

This is the orchestration layer. It spans retrieve.py, rerank.py and
prompt.py, so it belongs to none of them, and it is deliberately not in
app/main.py either -- CLAUDE.md makes that file transport only, and a policy
as load-bearing as "retry once, then refuse" should be testable without an
HTTP client in the way.

The LLM sits behind the `Chat` protocol for the same reason the reranker sits
behind `Scorer`: every refusal and retry rule below is then testable with no
network at all, and swapping OpenRouter for Vertex later is one function, not
a rewrite. That seam has already paid for itself once, when the reranker moved
from NVIDIA to VoyageAI mid-build without a single test changing.
"""

import os
from typing import Protocol

import psycopg
from langchain_postgres import PGVectorStore
from openai import OpenAI

from query.prompt import REFUSAL_TEXT, Answer, build_prompt, parse_answer
from query.rerank import Scorer, openrouter_rerank_scorer, rerank
from query.retrieve import CANDIDATES, retrieve

# Cheapest of the Gemini family, and measurably the weakest at obeying long
# instructions -- probed against a real build_prompt() prompt, it wrapped its
# reply in a ```json fence despite the prompt forbidding exactly that. It is
# usable only because response_format below removes that freedom. Swapping up
# to google/gemini-2.5-flash is a one-line change if the eval run justifies it.
ANSWER_MODEL = "google/gemini-2.5-flash-lite"

# One attempt, then exactly one retry (invariant 11: "failed grounding check ->
# retry once, then refuse"). Not a tenacity policy: this is not a transient
# network fault to back off from, it is a model that returned something
# ungrounded, and the answer to that is to ask once more and then stop.
MAX_ATTEMPTS = 2


class Chat(Protocol):
    def __call__(self, prompt: str) -> str:
        """Send one prompt, return the model's raw reply text."""
        ...


def openrouter_chat(prompt: str) -> str:
    """The real Chat. Same client shape as ingest/embed.py, so a later move to
    Vertex directly is a base_url and a key, not a rewrite.
    """
    client = OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=os.environ["OPENROUTER_API_KEY"],
    )
    reply = client.chat.completions.create(
        model=ANSWER_MODEL,
        messages=[{"role": "user", "content": prompt}],
        # Measured, not precautionary: without this the model returns its JSON
        # inside a ```json fence and every reply fails to parse.
        response_format={"type": "json_object"},
    )
    return reply.choices[0].message.content


def answer(
    question: str,
    store: PGVectorStore,
    conn: psycopg.Connection,
    *,
    chat: Chat = openrouter_chat,
    scorer: Scorer = openrouter_rerank_scorer,
    k: int = CANDIDATES,
) -> Answer:
    """Answer one question from the active corpus, or refuse.

    Two refusals, and neither is delegated to the model:

    Nothing survives reranking -- the corpus has no passage relevant enough to
    ground an answer, which Python knows for certain from an empty list. The
    model is never called. Handing it an empty context and trusting it to
    decline would be trusting it not to answer from what it already knows
    about pesticides, which is a great deal.

    The model returns something ungrounded -- malformed JSON, or a citation
    number that was never sent to it. Ask once more, and if it happens again,
    refuse. A model citing an excerpt that does not exist is the failure mode
    invariant 10 exists to prevent, so it can never be passed through.

    A reply with no citations is NOT retried. "These excerpts do not answer
    this question" is a correct outcome, not a malfunction -- retrying it would
    double the cost and latency of precisely the questions the system is meant
    to decline.
    """
    passages = retrieve(store, conn, question, k=k)
    scored = rerank(question, passages, scorer=scorer)
    if not scored:
        return Answer(REFUSAL_TEXT, [])

    prompt = build_prompt(question, scored)
    for _ in range(MAX_ATTEMPTS):
        try:
            return parse_answer(chat(prompt.text), prompt)
        except ValueError:  # json.JSONDecodeError is one of these
            continue
    return Answer(REFUSAL_TEXT, [])
