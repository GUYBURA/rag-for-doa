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

from query.guards import (
    PII_REFUSAL_TEXT,
    GroundingUndecided,
    Judge,
    PIIDetected,
    check_answer_text,
    check_grounding,
    check_question,
    deepseek_judge,
)
from query.prompt import REFUSAL_TEXT, Answer, build_prompt, parse_answer
from query.rerank import Scorer, openrouter_rerank_scorer, rerank
from query.retrieve import CANDIDATES, retrieve

# Chosen by measurement, not price. All 34 questions in eval/questions.yaml
# were run end to end through three candidates, same prompt, same passages:
#
#                       answered  grounded  answered in Thai  refused
#   gemini-2.5-flash-lite   27/28    27/27         23/28        5/6
#   gemini-2.5-flash        28/28    27/28         27/28        3/6
#   glm-5.3-flash           27/28    27/27         28/28        5/6
#
# flash-lite answered four Thai questions in English, ignoring rule 3 of
# INSTRUCTIONS outright. flash fixed the language but became noticeably more
# willing to answer, including questions it should have declined -- and
# ARCHITECTURE.md weights abstention deliberately, so that is a regression,
# not a wash. glm-5.3-flash gave up nothing on any axis. Its answers are
# longer (231 chars mean against 117), which is the only cost found.
#
# Every model tried wraps its JSON in a ```json fence despite rule 5
# forbidding it, which is why response_format below is load-bearing rather
# than belt and braces.
ANSWER_MODEL = "z-ai/glm-5.3-flash"

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
    judge: Judge = deepseek_judge,
    k: int = CANDIDATES,
) -> Answer:
    """Answer one question from the active corpus, or refuse.

    Four refusals, and none of them is delegated to the answering model:

    The question carries personal data -- an ID card number, a phone number,
    an email. Nothing is retrieved and no API is called, so the data never
    leaves this process, which is the only reason the check is worth having
    before retrieval rather than after.

    Nothing survives reranking -- the corpus has no passage relevant enough to
    ground an answer, which Python knows for certain from an empty list. The
    model is never called. Handing it an empty context and trusting it to
    decline would be trusting it not to answer from what it already knows
    about pesticides, which is a great deal.

    The model returns something ungrounded -- malformed JSON, a citation
    number that was never sent to it, or an answer the judge finds unsupported
    by the excerpt it cites. Ask once more, and if it happens again, refuse. A
    model citing an excerpt that does not exist is the failure mode invariant
    10 exists to prevent, so it can never be passed through. All three share
    one budget: NotGrounded is a ValueError precisely so the loop below needs
    no second counter, and MAX_ATTEMPTS stays the ceiling on model calls
    however the failures are mixed.

    The judge itself is unreachable or unreadable. Refuse at once, without
    retrying -- asking the answering model again cannot repair a broken judge,
    it only spends a second call to reach the same unknown. Fail closed:
    unverified is not served.

    A reply with no citations is NOT retried. "These excerpts do not answer
    this question" is a correct outcome, not a malfunction -- retrying it would
    double the cost and latency of precisely the questions the system is meant
    to decline.
    """
    try:
        check_question(question)
    except PIIDetected:
        return Answer(PII_REFUSAL_TEXT, [])

    passages = retrieve(store, conn, question, k=k)
    scored = rerank(question, passages, scorer=scorer)
    if not scored:
        return Answer(REFUSAL_TEXT, [])

    prompt = build_prompt(question, scored)
    for _ in range(MAX_ATTEMPTS):
        try:
            # Order matters: grounding is checked after the reply parses and
            # before it is returned. Checking it afterwards would mean the
            # unsupported answer had already been served.
            parsed = parse_answer(chat(prompt.text), prompt)
            check_grounding(question, parsed, judge=judge)
            check_answer_text(parsed.text)
            return parsed
        except ValueError:  # json.JSONDecodeError and NotGrounded are these
            continue
        except GroundingUndecided:
            return Answer(REFUSAL_TEXT, [])
        except PIIDetected:
            # Not retried: the excerpts that produced this are the same
            # excerpts the next attempt would get.
            return Answer(PII_REFUSAL_TEXT, [])
    return Answer(REFUSAL_TEXT, [])
