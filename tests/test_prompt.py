"""Pure tests for prompt assembly and answer parsing. No network, no container:
build_prompt() only formats strings, and parse_answer() only reads a string the
LLM would have returned, so the whole file runs in CI with no key.
"""

import json
import uuid

import pytest

from query.prompt import (
    REFUSAL_TEXT,
    Answer,
    Citation,
    Prompt,
    build_prompt,
    parse_answer,
)
from query.rerank import ScoredPassage
from query.retrieve import Passage


def _scored(rank: int, *, score: float = 0.9, content: str = "", page: int = 1,
            section: str | None = None, edition: int = 2568, title: str = "x",
            source: str = "x.pdf") -> ScoredPassage:
    """One reranked passage. Unlike test_rerank.py's helper, the citation
    fields matter here -- invariant 10 says they end up in the answer, so
    several tests below vary them deliberately.
    """
    return ScoredPassage(
        passage=Passage(
            chunk_id=uuid.uuid4(),
            content=content or f"content {rank}",
            page_number=page,
            section=section,
            document_id=uuid.uuid4(),
            edition_year_be=edition,
            title_th=title,
            source=source,
            rank=rank,
            fusion_score=0.0,
        ),
        score=score,
    )


def _raw(answer: str, citations: list[int]) -> str:
    """What the LLM would have returned, as a string -- parse_answer() takes
    text, not an already-parsed dict, because that is what an HTTP response
    body actually is.
    """
    return json.dumps({"answer": answer, "citations": citations}, ensure_ascii=False)


# -- build_prompt --------------------------------------------------------


def test_no_passages_is_a_caller_error_not_an_empty_prompt():
    """Refusal is decided in Python, before the LLM is ever asked (invariant
    11). Anyone reaching build_prompt() with nothing to ground an answer in
    skipped that check.
    """
    with pytest.raises(ValueError):
        build_prompt("q", [])


def test_every_passage_content_reaches_the_prompt():
    """A passage the model cannot read cannot be cited."""
    passages = [_scored(1, content="ด้วงสาคู"), _scored(2, content="หนอนชิเมโจได")]
    prompt = build_prompt("q", passages)
    assert "ด้วงสาคู" in prompt.text
    assert "หนอนชิเมโจได" in prompt.text


def test_the_question_reaches_the_prompt():
    # Deliberately Thai and specific. "question" appears in INSTRUCTIONS
    # twice, so an assertion on that word is satisfied by the boilerplate
    # alone and stays green for a build_prompt() that never inserts the
    # question at all -- confirmed by mutating the question out of the join.
    prompt = build_prompt("โรคราสนิมถั่วเหลือง", [_scored(1)])
    assert "โรคราสนิมถั่วเหลือง" in prompt.text


def test_the_section_label_reaches_the_prompt():
    """A mango table and a durian table differ mainly in the heading chunk.py
    split off into `section` -- without it in the prompt, the model cannot
    tell it is being handed the wrong crop.
    """
    prompt = build_prompt("q", [_scored(1, section="ทุเรียน")])
    assert "ทุเรียน" in prompt.text


def test_a_passage_with_no_section_is_still_formatted():
    """section is nullable on Passage. An f-string left to render None would
    put the literal text "None" in front of the content.
    """
    prompt = build_prompt("q", [_scored(1, content="เนื้อหา", section=None)])
    assert "None" not in prompt.text


def test_citation_numbers_start_at_one_and_follow_input_order():
    """[n] is 1-based because that is what the prompt tells the model to use.
    A 0-based list would make every citation off by one.
    """
    passages = [_scored(1), _scored(2), _scored(3)]
    prompt = build_prompt("q", passages)
    assert [c.number for c in prompt.citations] == [1,2,3]


def test_citation_numbers_are_positional_not_retrieval_rank():
    """The trap: rerank() reorders passages, so the 1st passage handed here
    is usually NOT retrieval rank 1. Numbering by `passage.rank` would still
    pass a test whose ranks happen to be 1, 2, 3 -- so these deliberately
    are not.
    """
    passages = [_scored(7), _scored(3)]  # ranks that are not 1, 2
    prompt = build_prompt("q", passages)
    assert [c.number for c in prompt.citations] == [1,2]


def test_each_citation_keeps_its_own_passage():
    """Number -> Passage is the mapping the whole citation design rests on.
    Zipping it up wrong points an answer at the wrong page (invariant 10).
    """
    first = _scored(1, page=10, title="โป๋งดั๊ง")
    second = _scored(2, page=5, title="ลำตึ๋ย")
    prompt = build_prompt("q", [first, second])
    # Hint: assert on the Passage objects themselves, not on one field --
    # identity catches a swap that a single-field check could miss.
    assert prompt.citations[0].passage is first.passage
    assert prompt.citations[1].passage is second.passage


# -- parse_answer --------------------------------------------------------


def test_a_cited_answer_survives_parsing():
    passages = [_scored(1), _scored(2)]
    prompt = build_prompt("q", passages)
    answer = parse_answer(_raw("ใช้โพรคลอราซ", [2]), prompt)
    assert answer.text == "ใช้โพรคลอราซ"
    # Hint: the citation the model chose must come back as the matching
    # Passage, not as a bare int.
    assert [c.passage for c in answer.citations] == [passages[1].passage]


def test_an_uncited_answer_is_a_refusal_however_confident_it_sounds():
    """citations == [] is the only signal that nothing in these passages
    answered the question -- and it outranks whatever `answer` says, because
    text with no source cannot be served (invariant 10).
    """
    prompt = build_prompt("q", [_scored(1)])
    answer = parse_answer(_raw("ใช้อัตรา 20 มิลลิลิตรต่อน้ำ 20 ลิตร", []), prompt)
    assert answer.citations == []
    assert answer.text == REFUSAL_TEXT  # Hint: not the model's text. What replaces it?


def test_a_citation_past_the_end_is_rejected():
    """The model inventing [7] out of 2 passages is a grounding failure, and
    it has to raise so the caller can retry once and then refuse.
    """
    prompt = build_prompt("q", [_scored(1), _scored(2)])
    with pytest.raises(ValueError):
        parse_answer(_raw("...", [7]), prompt)


def test_a_zero_citation_is_rejected():
    """[0] is what a model that counted from zero would emit. Silently
    treating it as the first passage would cite a page nobody checked.
    """
    prompt = build_prompt("q", [_scored(1)])
    with pytest.raises(ValueError):
        parse_answer(_raw("...", [0]), prompt)


def test_malformed_json_is_rejected():
    prompt = build_prompt("q", [_scored(1)])
    with pytest.raises(ValueError):
        parse_answer("ชั้นไม่ใช่ json นะ", prompt)


def test_a_duplicate_citation_appears_once():
    """A model citing [1] twice in one answer is normal. The Answer should
    carry one Citation per source, not a repeat.
    """
    prompt = build_prompt("q", [_scored(1), _scored(2)])
    answer = parse_answer(_raw("...", [1,1]), prompt)
    assert len(answer.citations) == 1
