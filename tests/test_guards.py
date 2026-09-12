"""Pure tests for the two guards: what they catch, and what they must not.

No network. The judge is a stub returning fixed replies, so what is under test
is guards.py's own rules -- which verdict means refuse, which means retry,
which means "could not decide" -- and not any model's behaviour. The live
tests at the bottom are the only ones that make a claim about the real judge.

Every "must catch X" test below has a twin "must not catch Y". A PII pattern
that fires on doses, or a judge that calls every answer ungrounded, would pass
a suite that only tested the positive half while making the system useless.
"""

import json
import uuid

import httpx
import openai
import pytest

from query.guards import (
    ANSWER_PII_KINDS,
    GroundingUndecided,
    NotGrounded,
    PIIDetected,
    build_grounding_prompt,
    check_answer_text,
    check_grounding,
    check_question,
    find_pii,
)
from query.prompt import Answer, Citation
from query.retrieve import Passage

SOYBEAN = "ถั่วเหลือง (Soybean)"
DURIAN = "ทุเรียน (Durian)"

CITED = (
    "โรคราสนิม (Phakopsora pachyrhizi) พ่นสาร prochloraz 45% EC "
    "อัตรา 20 มิลลิลิตร ต่อน้ำ 20 ลิตร"
)
UNCITED = (
    "โรครากเน่าโคนเน่า (Phytophthora palmivora) พ่นสาร metalaxyl 25% WP "
    "อัตรา 30 กรัม ต่อน้ำ 20 ลิตร"
)

# Thirteen digits whose check digit is correct. Generated, not copied from a
# real card -- the point is only that the checksum passes.
VALID_ID = "3101005001239"
VALID_ID_DASHED = "3-1010-05001-23-9"

# A real ISBN-13 shape, and the reason _has_valid_id_checksum exists: without
# it every one of these on a handbook cover reads as an ID card number.
ISBN = "978-616-576-244-1"


def _passage(content: str, section: str) -> Passage:
    return Passage(
        chunk_id=uuid.uuid4(),
        content=content,
        page_number=56,
        section=section,
        document_id=uuid.uuid4(),
        edition_year_be=2568,
        title_th="คู่มือ",
        source="2568.pdf",
        rank=1,
        fusion_score=0.0,
    )


def _answer(text: str, *contents: tuple[str, str]) -> Answer:
    """An Answer citing one Citation per (content, section) pair given."""
    return Answer(
        text=text,
        citations=[
            Citation(number=n, passage=_passage(c, s), score=0.9)
            for n, (c, s) in enumerate(contents, start=1)
        ],
    )


def _judge_returning(*raws: str):
    """A Judge returning each raw reply in turn, recording its prompts.

    Fixed replies, never anything derived from the prompt it was handed: a
    stub that computed its verdict from its input would agree with the
    implementation by construction and stay green through any change to it.
    """
    calls = []

    def judge(prompt: str) -> str:
        calls.append(prompt)
        return raws[len(calls) - 1]

    judge.calls = calls
    return judge


def _verdict(grounded: bool, unsupported: list[str] | None = None) -> str:
    return json.dumps({"unsupported": unsupported or [], "grounded": grounded})


def _raising_judge(prompt: str) -> str:
    raise AssertionError("the judge was called when it should not have been")


def _failing_judge(prompt: str) -> str:
    raise openai.APITimeoutError(request=httpx.Request("POST", "https://x/api"))


# ---------------------------------------------------------------------------
# Grounding
# ---------------------------------------------------------------------------


def test_a_grounded_verdict_passes_silently():
    check_grounding(
        "คำถาม",
        _answer("ใช้ prochloraz", (CITED, SOYBEAN)),
        judge=_judge_returning(_verdict(True)),
    )


def test_an_ungrounded_verdict_raises_not_grounded():
    with pytest.raises(NotGrounded):
        check_grounding(
            "คำถาม",
            _answer("ใช้ prochloraz อัตรา 40 มิลลิลิตร", (CITED, SOYBEAN)),
            judge=_judge_returning(_verdict(False, ["อัตรา 40 มิลลิลิตร"])),
        )


def test_a_judge_reply_that_is_not_json_is_undecided():
    """Not NotGrounded. A judge that cannot be read has decided nothing, and
    routing it to the retry path would spend a second answering call to reach
    the same unknown.
    """
    with pytest.raises(GroundingUndecided):
        check_grounding(
            "คำถาม",
            _answer("ใช้ prochloraz", (CITED, SOYBEAN)),
            judge=_judge_returning("The answer looks fine to me."),
        )


def test_a_verdict_that_is_not_a_boolean_is_undecided():
    with pytest.raises(GroundingUndecided):
        check_grounding(
            "คำถาม",
            _answer("ใช้ prochloraz", (CITED, SOYBEAN)),
            judge=_judge_returning(json.dumps({"grounded": "maybe"})),
        )


def test_a_judge_reply_with_no_verdict_key_is_undecided():
    with pytest.raises(GroundingUndecided):
        check_grounding(
            "คำถาม",
            _answer("ใช้ prochloraz", (CITED, SOYBEAN)),
            judge=_judge_returning(json.dumps({"unsupported": []})),
        )


def test_a_judge_that_cannot_be_reached_is_undecided():
    """Fail closed. app/main.py catches nothing, so an exception escaping here
    would be a 500 rather than a refusal.
    """
    with pytest.raises(GroundingUndecided):
        check_grounding(
            "คำถาม",
            _answer("ใช้ prochloraz", (CITED, SOYBEAN)),
            judge=_failing_judge,
        )


def test_an_answer_with_no_citations_never_reaches_the_judge():
    """A refusal has nothing to ground. Calling the judge anyway would put a
    paid API call on the cheapest path in the system.
    """
    check_grounding("คำถาม", Answer("ไม่พบเอกสาร", []), judge=_raising_judge)


def test_not_grounded_is_retryable_and_undecided_is_not():
    """The mechanism answer.py's loop depends on: its `except ValueError`
    catches one of these and not the other, which is what gives the two
    failures opposite retry behaviour without a second counter.
    """
    assert issubclass(NotGrounded, ValueError)
    assert not issubclass(GroundingUndecided, ValueError)
    assert not issubclass(PIIDetected, ValueError)


def test_the_prompt_carries_the_cited_excerpt_and_not_the_uncited_one():
    """The test that pins decision "the judge sees only what was cited".

    Handing it every retrieved passage would pass every other test in this
    file, and would let an answer that cites [1] while quoting an uncited
    excerpt be judged grounded -- a citation pointing at a page that does not
    contain the claim, which is invariant 10's exact failure.
    """
    answer = _answer("ใช้ prochloraz 45% EC", (CITED, SOYBEAN))
    prompt = build_grounding_prompt("โรคราสนิมใช้สารอะไร", answer)

    assert CITED in prompt
    assert UNCITED not in prompt
    assert SOYBEAN in prompt  # section is evidence of the crop, as in prompt.py
    assert "โรคราสนิมใช้สารอะไร" in prompt
    assert "ใช้ prochloraz 45% EC" in prompt


def test_excerpt_numbers_match_the_ones_the_answering_model_saw():
    """A claim about "[2]" has to mean the same excerpt in both prompts."""
    answer = Answer(
        text="ตามข้อ 2",
        citations=[Citation(number=2, passage=_passage(CITED, SOYBEAN), score=0.9)],
    )
    assert "[2]" in build_grounding_prompt("คำถาม", answer)


# ---------------------------------------------------------------------------
# PII -- must catch
# ---------------------------------------------------------------------------


def test_an_id_card_number_is_caught():
    assert find_pii(f"ผมเลขบัตร {VALID_ID} อยากถามเรื่องยาฆ่าแมลง") == [
        "thai_national_id"
    ]


def test_an_id_card_number_with_dashes_is_caught():
    assert find_pii(f"เลขบัตร {VALID_ID_DASHED}") == ["thai_national_id"]


def test_a_phone_number_is_caught():
    assert find_pii("โทรกลับที่ 081-234-5678 ด้วย") == ["thai_phone"]


def test_an_email_is_caught():
    assert find_pii("ส่งคำตอบมาที่ farmer.somchai@example.co.th") == ["email"]


def test_check_question_raises_on_pii():
    with pytest.raises(PIIDetected):
        check_question(f"เลขบัตร {VALID_ID} ใช้สารอะไรกับถั่วเหลือง")


def test_the_detected_values_are_not_reported_back():
    """find_pii names the kind, never the value. A guard that copies the ID
    card number into an exception message has moved the data into the logs
    rather than kept it out of them.
    """
    kinds = find_pii(f"เลขบัตร {VALID_ID} โทร 081-234-5678")
    assert kinds == ["thai_national_id", "thai_phone"]
    assert VALID_ID not in "".join(kinds)


# ---------------------------------------------------------------------------
# PII -- must not catch. The corpus is full of numbers.
# ---------------------------------------------------------------------------


def test_a_dosage_is_not_pii():
    assert find_pii("prochloraz 45% EC อัตรา 20 มิลลิลิตร ต่อน้ำ 20 ลิตร") == []


def test_an_isbn_is_not_an_id_card_number():
    """Thirteen digits, hyphen-grouped, on the cover of every volume in the
    corpus. Only the checksum separates it from an ID card number.
    """
    assert find_pii(f"ISBN {ISBN}") == []


def test_thirteen_digits_with_a_wrong_check_digit_are_not_an_id_card_number():
    wrong = VALID_ID[:12] + str((int(VALID_ID[12]) + 1) % 10)
    assert find_pii(wrong) == []


def test_a_year_and_a_page_number_are_not_a_phone_number():
    assert find_pii("ฉบับปี 2568 หน้า 56 ตารางที่ 12") == []


# ---------------------------------------------------------------------------
# PII on the way out -- checks less than on the way in, on purpose
# ---------------------------------------------------------------------------


def test_an_id_card_number_in_an_answer_is_caught():
    with pytest.raises(PIIDetected):
        check_answer_text(f"ติดต่อเจ้าของแปลง เลขบัตร {VALID_ID}")


def test_a_department_phone_number_in_an_answer_is_allowed():
    """The asymmetry, and the test that documents why it is not an oversight.

    The handbooks print the department's own switchboard. That is public
    information about an organisation, and refusing a correct answer because
    it quoted the source's contact line would protect nobody.
    """
    check_answer_text("สอบถามเพิ่มเติม กรมวิชาการเกษตร โทร 02-579-0151")
    assert "thai_phone" not in ANSWER_PII_KINDS


# ---------------------------------------------------------------------------
# Live: the real judge against real Thai corpus text.
#
# Everything above stubs the verdict, so nothing above tests the claim that
# actually matters -- that a model reading Thai pesticide guidance can tell a
# supported dose from an altered one. Only these can.
# ---------------------------------------------------------------------------


@pytest.mark.requires_llm
def test_a_faithful_answer_is_judged_grounded():
    # Reworded, not copied: the judge must accept paraphrase, or every correct
    # answer the model writes in its own words gets refused.
    check_grounding(
        "โรคราสนิมในถั่วเหลืองใช้สารอะไร",
        _answer(
            "โรคราสนิมในถั่วเหลืองป้องกันกำจัดได้ด้วยการพ่น prochloraz 45% EC "
            "ในอัตรา 20 มิลลิลิตรต่อน้ำ 20 ลิตร",
            (CITED, SOYBEAN),
        ),
    )


@pytest.mark.requires_llm
def test_an_altered_dose_is_judged_ungrounded():
    """The failure every structural check passes: [1] exists, [1] is the right
    page, and the number on it is not the number in the answer.
    """
    with pytest.raises(NotGrounded):
        check_grounding(
            "โรคราสนิมในถั่วเหลืองใช้สารอะไร อัตราเท่าไร",
            _answer(
                "พ่น prochloraz 45% EC อัตรา 40 มิลลิลิตร ต่อน้ำ 20 ลิตร",
                (CITED, SOYBEAN),
            ),
        )


@pytest.mark.requires_llm
def test_a_fact_absent_from_the_excerpt_is_judged_ungrounded():
    with pytest.raises(NotGrounded):
        check_grounding(
            "โรคราสนิมในถั่วเหลืองใช้สารอะไร",
            _answer(
                "พ่น prochloraz 45% EC อัตรา 20 มิลลิลิตร ต่อน้ำ 20 ลิตร "
                "และควรพ่นซ้ำทุก 7 วัน ติดต่อกัน 3 ครั้ง",
                (CITED, SOYBEAN),
            ),
        )


@pytest.mark.requires_llm
def test_an_answer_about_the_wrong_crop_is_judged_ungrounded():
    """The negative rerank cannot separate: a real pest, a real chemical, and
    the wrong crop. Scores 0.62-0.80 on the gold set, straddling positives.
    The excerpt is about soybean; the answer claims it is about durian.
    """
    with pytest.raises(NotGrounded):
        check_grounding(
            "โรคในทุเรียนใช้สารอะไร",
            _answer(
                "ทุเรียนใช้ prochloraz 45% EC อัตรา 20 มิลลิลิตร ต่อน้ำ 20 ลิตร",
                (CITED, SOYBEAN),
            ),
        )
