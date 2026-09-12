"""Pure tests for the orchestration policy: when the model is called at all,
how often, and when a reply is thrown away.

No network and no container. retrieve() is monkeypatched to hand back fixed
passages and the Scorer is a stub, so what is under test is only answer()'s
own rules -- refuse before calling, retry exactly once, never retry a correct
refusal.
"""

import json
import uuid

import pytest

import query.answer as answer_module
from query.answer import MAX_ATTEMPTS, answer
from query.guards import PII_REFUSAL_TEXT
from query.prompt import REFUSAL_TEXT
from query.rerank import SCORE_THRESHOLD
from query.retrieve import Passage

SOYBEAN = "ถั่วเหลือง (Soybean)"
RELEVANT = SCORE_THRESHOLD + 0.1
IRRELEVANT = SCORE_THRESHOLD - 0.1
# Checksum-valid, generated rather than real. See tests/test_guards.py.
VALID_ID = "3101005001239"


def _passage(rank: int) -> Passage:
    return Passage(
        chunk_id=uuid.uuid4(),
        content=f"content {rank}",
        page_number=rank,
        section=f"section {rank}",
        document_id=uuid.uuid4(),
        edition_year_be=2568,
        title_th="x",
        source="x.pdf",
        rank=rank,
        fusion_score=0.0,
    )


@pytest.fixture
def passages(monkeypatch):
    """Two candidates, returned by a stand-in for the whole retrieval path.

    Returned as well as installed, so a test can assert which Passage a
    citation ended up pointing at.
    """
    found = [_passage(1), _passage(2)]
    monkeypatch.setattr(answer_module, "retrieve", lambda *a, **kw: found)
    return found


def _scorer(*scores: float):
    def scorer(question, ps):
        return list(scores)

    return scorer


def _chat_returning(*replies: str):
    """A Chat that returns each reply in turn and records its calls. Raises if
    called more times than it has replies, so an unexpected extra attempt
    fails loudly instead of reusing the last one.
    """
    calls = []

    def chat(prompt: str) -> str:
        calls.append(prompt)
        return replies[len(calls) - 1]

    chat.calls = calls
    return chat


def _raising_chat(prompt: str) -> str:
    raise AssertionError("the model was called when it should not have been")


def _judge_saying(*verdicts: bool):
    """A Judge that returns each verdict in turn and records its calls.

    Injected into every pure test below, even the ones that are not about
    grounding: answer()'s `judge` default is the real DeepSeek call, so a test
    that omits it would quietly start hitting the network.
    """
    calls = []

    def judge(prompt: str) -> str:
        calls.append(prompt)
        return json.dumps({"unsupported": [], "grounded": verdicts[len(calls) - 1]})

    judge.calls = calls
    return judge


def _raising_judge(prompt: str) -> str:
    raise AssertionError("the judge was called when it should not have been")


def _reply(text: str, citations: list[int]) -> str:
    return json.dumps({"answer": text, "citations": citations}, ensure_ascii=False)


def test_nothing_above_the_threshold_refuses_without_calling_the_model(passages):
    """An empty rerank result is a certainty, not an opinion to consult the
    model about -- and the model knows enough about pesticides to answer from
    memory if asked with no context at all.
    """
    result = answer(
        "q",
        None,
        None,
        chat=_raising_chat,
        scorer=_scorer(IRRELEVANT, IRRELEVANT),
        judge=_raising_judge,
    )
    assert result.text == REFUSAL_TEXT
    assert result.citations == []


def test_a_grounded_reply_is_returned_after_one_call(passages):
    chat = _chat_returning(_reply("คำตอบ", [2]))
    judge = _judge_saying(True)
    result = answer(
        "q", None, None, chat=chat, scorer=_scorer(RELEVANT, RELEVANT), judge=judge
    )
    assert result.text == "คำตอบ"
    assert [c.passage for c in result.citations] == [passages[1]]
    assert len(chat.calls) == 1
    assert len(judge.calls) == 1


def test_a_malformed_reply_is_retried_once_and_then_succeeds(passages):
    chat = _chat_returning("not json at all", _reply("คำตอบ", [1]))
    result = answer(
        "q",
        None,
        None,
        chat=chat,
        scorer=_scorer(RELEVANT, RELEVANT),
        judge=_judge_saying(True),
    )
    assert result.text == "คำตอบ"
    assert len(chat.calls) == 2


def test_an_invented_citation_is_retried_once_and_then_succeeds(passages):
    """[7] out of 2 excerpts is a grounding failure, not a parse error, and it
    has to trigger the same retry -- a cited page that was never retrieved is
    exactly what invariant 10 forbids.
    """
    chat = _chat_returning(_reply("คำตอบ", [7]), _reply("คำตอบ", [1]))
    result = answer(
        "q",
        None,
        None,
        chat=chat,
        scorer=_scorer(RELEVANT, RELEVANT),
        judge=_judge_saying(True),
    )
    assert result.text == "คำตอบ"
    assert len(chat.calls) == 2


def test_two_bad_replies_refuse_and_the_model_is_not_asked_a_third_time(passages):
    chat = _chat_returning("garbage", "garbage again")
    result = answer(
        "q",
        None,
        None,
        chat=chat,
        scorer=_scorer(RELEVANT, RELEVANT),
        judge=_raising_judge,
    )
    assert result.text == REFUSAL_TEXT
    assert result.citations == []
    assert len(chat.calls) == MAX_ATTEMPTS == 2


def test_a_reply_with_no_citations_is_not_retried(passages):
    """The trap. "These excerpts do not answer the question" is the correct
    outcome, so a retry written with too broad a catch would spend a second
    call on every question the system is supposed to decline.
    """
    chat = _chat_returning(_reply("ฟังดูมั่นใจมาก", []))
    result = answer(
        "q",
        None,
        None,
        chat=chat,
        scorer=_scorer(RELEVANT, RELEVANT),
        # A refusal has nothing to ground, so the judge is not consulted
        # either. Paying for a second API call on the questions the system is
        # meant to decline is the trap this whole test guards.
        judge=_raising_judge,
    )
    assert result.text == REFUSAL_TEXT
    assert result.citations == []
    assert len(chat.calls) == 1


# ---------------------------------------------------------------------------
# Grounding and PII: guards.py decides, answer() decides what to do about it.
# ---------------------------------------------------------------------------


def _judge_returning(*raws: str):
    calls = []

    def judge(prompt: str) -> str:
        calls.append(prompt)
        return raws[len(calls) - 1]

    judge.calls = calls
    return judge


def test_an_ungrounded_reply_is_retried_once_and_then_succeeds(passages):
    chat = _chat_returning(_reply("มั่วมา", [1]), _reply("คำตอบ", [1]))
    judge = _judge_saying(False, True)
    result = answer(
        "q", None, None, chat=chat, scorer=_scorer(RELEVANT, RELEVANT), judge=judge
    )
    assert result.text == "คำตอบ"
    assert len(chat.calls) == 2
    assert len(judge.calls) == 2


def test_two_ungrounded_replies_refuse_and_the_model_is_not_asked_a_third_time(
    passages,
):
    chat = _chat_returning(_reply("มั่ว", [1]), _reply("มั่วอีก", [1]))
    judge = _judge_saying(False, False)
    result = answer(
        "q", None, None, chat=chat, scorer=_scorer(RELEVANT, RELEVANT), judge=judge
    )
    assert result.text == REFUSAL_TEXT
    assert result.citations == []
    assert len(chat.calls) == MAX_ATTEMPTS == 2


def test_a_parse_failure_and_a_grounding_failure_share_one_budget(passages):
    """The test that pins "one ceiling, whatever the mix of failures".

    A first attempt that will not parse and a second that parses but is
    ungrounded is two failures of different kinds. If grounding ever gets its
    own retry counter, this asks the model a third time and goes red -- which
    is the only way that change would be noticed.
    """
    chat = _chat_returning("not json at all", _reply("มั่ว", [1]))
    judge = _judge_saying(False)
    result = answer(
        "q", None, None, chat=chat, scorer=_scorer(RELEVANT, RELEVANT), judge=judge
    )
    assert result.text == REFUSAL_TEXT
    assert len(chat.calls) == MAX_ATTEMPTS == 2
    assert len(judge.calls) == 1  # the unparseable reply never reached it


def test_an_unreadable_judge_refuses_without_asking_the_model_again(passages):
    """Fail closed, and do not pay twice for it. Retrying the answering model
    cannot repair a judge that is down.
    """
    chat = _chat_returning(_reply("คำตอบ", [1]))
    judge = _judge_returning("I think it is fine")
    result = answer(
        "q", None, None, chat=chat, scorer=_scorer(RELEVANT, RELEVANT), judge=judge
    )
    assert result.text == REFUSAL_TEXT
    assert result.citations == []
    assert len(chat.calls) == 1
    assert len(judge.calls) == 1


def test_a_question_containing_personal_data_never_reaches_retrieval(monkeypatch):
    """Refused before retrieve(), before rerank, before any API call -- which
    is the only arrangement where the data provably does not leave.
    """

    def _raising_retrieve(*a, **kw):
        raise AssertionError("retrieval ran on a question that should be refused")

    monkeypatch.setattr(answer_module, "retrieve", _raising_retrieve)
    result = answer(
        f"เลขบัตร {VALID_ID} ใช้สารอะไรกับถั่วเหลือง",
        None,
        None,
        chat=_raising_chat,
        scorer=_scorer(RELEVANT),
        judge=_raising_judge,
    )
    assert result.text == PII_REFUSAL_TEXT
    assert result.citations == []


def test_personal_data_in_a_grounded_answer_is_refused_and_not_retried(passages):
    """Asking again would get the same excerpts and the same leak, so the
    second call buys nothing. The refusal names the reason rather than
    claiming the corpus had nothing to say.
    """
    chat = _chat_returning(_reply(f"ติดต่อ เลขบัตร {VALID_ID}", [1]))
    result = answer(
        "q",
        None,
        None,
        chat=chat,
        scorer=_scorer(RELEVANT, RELEVANT),
        judge=_judge_saying(True),
    )
    assert result.text == PII_REFUSAL_TEXT
    assert result.citations == []
    assert len(chat.calls) == 1


# ---------------------------------------------------------------------------
# Live: the whole pipeline against the real ingested corpus
# (tests/fixtures/excerpt.pdf, via conftest.py's shared `store` /
# `corpus_conn`), with the real reranker and the real model.
#
# These are the only tests that exercise prompt.py's INSTRUCTIONS against a
# model at all. Everything the prompt asserts -- bare JSON, Thai out of
# English instructions, citations only from the excerpts given -- is a claim
# about a model's behaviour and cannot be checked by a stub.
# ---------------------------------------------------------------------------


@pytest.mark.requires_llm
@pytest.mark.requires_rerank
@pytest.mark.requires_embeddings
def test_a_real_question_is_answered_in_thai_with_a_citation(store, corpus_conn):
    result = answer("โรคราสนิมในถั่วเหลืองเกิดจากเชื้อราอะไร", store, corpus_conn)

    assert result.text != REFUSAL_TEXT
    assert result.citations
    assert all(c.passage.section == SOYBEAN for c in result.citations)
    # The instructions are in English and the question is in Thai. A model
    # that answered in English would be technically grounded and useless.
    assert any("฀" <= char <= "๿" for char in result.text)


@pytest.mark.requires_llm
@pytest.mark.requires_rerank
@pytest.mark.requires_embeddings
def test_an_out_of_domain_question_is_refused_end_to_end(store, corpus_conn):
    # Same question as eval/questions.yaml's q29. It is refused by the rerank
    # threshold before the model is reached, which is the point: the cheapest
    # refusal in the system costs no tokens at all.
    result = answer("วิธีต้มส้มตำให้อร่อยทำอย่างไร", store, corpus_conn)

    assert result.text == REFUSAL_TEXT
    assert result.citations == []
