"""The two checks that stand between a working pipeline and a deployable one.

Nothing here is retrieval, prompting or orchestration, which is why it is its
own module rather than more code in answer.py: these are checks on text, and
they are meaningful without knowing where the text came from. answer.py is the
only caller (CLAUDE.md keeps policy out of app/main.py), but this module knows
nothing about it.

Grounding is the second half of invariant 11. rerank.py's SCORE_THRESHOLD
answers "is this passage about the question", and its own comment records that
it cannot answer "does the answer match the passage" -- the hard negatives in
eval/questions.yaml score 0.62-0.80, straddling real positives, so no single
threshold separates them. The failure that survives every structural check is
an answer citing [1], where [1] exists and points at the right page, and the
dose written in the answer is not the dose written on that page. parse_answer()
cannot see that. A judge reading both can.

The judge is a different model from ANSWER_MODEL, and it sits behind a
`Judge` protocol for the same reason `Scorer` and `Chat` do: every rule below
is testable with no network.

PII is the inbound half, and deliberately not a model at all -- see the
comment above _PATTERNS.
"""

import json
import os
import re
from typing import Protocol

import openai
from openai import OpenAI

from query.prompt import Answer

# A different model from ANSWER_MODEL on purpose. A model grading its own
# output has a measured bias toward passing it (self-preference bias), and
# while that bias is weaker here than usual -- the judge sees only the answer
# text and the excerpts, never the reasoning that produced them -- there is no
# reason to accept any of it when a second vendor costs the same.
#
# Dated snapshot, not the floating `deepseek/deepseek-v4-flash` alias, for the
# same reason document.embedding_model is pinned per document: an alias can be
# repointed upstream with no diff here to review, and a guard that silently
# changes what it lets through is worse than no guard.
JUDGE_MODEL = "deepseek/deepseek-v4-flash-0731"

# Refusals are not interchangeable. REFUSAL_TEXT says the corpus had nothing;
# saying that to someone whose question was blocked for containing an ID card
# number would be a lie, and would hide the one thing they can act on.
PII_REFUSAL_TEXT = (
    "คำถามนี้มีข้อมูลส่วนบุคคล ระบบไม่รับคำถามที่มีข้อมูลส่วนบุคคล "
    "กรุณาถามใหม่โดยไม่ระบุเลขบัตรประชาชน เบอร์โทรศัพท์ หรืออีเมล"
)

# The judge is asked to list the unsupported claims before it answers, and the
# list is deliberately not part of the contract below -- only `grounded` is
# read. It earns its place twice over anyway: naming the offending claim first
# measurably steadies the verdict, and when a refusal looks wrong, the list is
# the only thing that says why.
JUDGE_INSTRUCTIONS = """\
You check whether an answer is supported by the excerpts it cites. You are \
not judging whether the answer is helpful, complete, or well written.

Method:
1. Break the answer into individual factual claims.
2. For each claim, find an excerpt that states it. An excerpt states a claim \
when it means the same thing -- rewording, summarising, translating and \
reordering are all fine. Matching wording is not required.
3. Numbers, rates, concentrations, dates and names are claims like any other, \
and they are supported only when they match exactly. "20 ml per 20 litres of \
water" does not support "40 ml per 20 litres of water".
4. A claim you know to be true from your own knowledge is NOT supported. Only \
the excerpts count. If the excerpts do not say it, it is unsupported even if \
it is correct.
5. The answer is grounded when every claim is supported. One unsupported \
claim makes the whole answer ungrounded.

OUTPUT EXACTLY ONE JSON OBJECT and nothing else -- no prose before or after \
it, no markdown code fence:
   {"unsupported": ["<claim>", ...], "grounded": true or false}
"""


class NotGrounded(ValueError):
    """The judge read the answer and the excerpts and said no.

    A ValueError on purpose. answer.py's retry loop already catches ValueError
    for a malformed or invented-citation reply, and an ungrounded answer wants
    exactly the same treatment -- ask the model once more, then refuse -- so
    inheriting from it puts this failure on the existing path instead of
    adding a second retry budget. Invariant 11's ceiling stays at
    MAX_ATTEMPTS, whatever the mix of failures.
    """


class GroundingUndecided(Exception):
    """The judge could not be consulted, or said something unreadable.

    NOT a ValueError, and that is the whole point. Retrying the answering
    model cannot fix a judge that is down or returning nonsense: it would
    spend a second call to arrive at the same unknown. Staying off answer.py's
    ValueError path is what makes this refuse immediately (fail closed) rather
    than retry.
    """


class PIIDetected(Exception):
    """Personal data found in text that was about to be sent or returned.

    Also not a ValueError: there is nothing to retry. The same question asked
    again contains the same ID card number.
    """


class Judge(Protocol):
    def __call__(self, prompt: str) -> str:
        """Send one judging prompt, return the judge's raw reply text."""
        ...


# ---------------------------------------------------------------------------
# PII
#
# Regex, not a model. Three reasons, in order of weight: this runs before the
# question reaches any third party, so a check that is itself an API call
# defeats its own purpose; the answer is either yes or no with no judgement
# involved, and a deterministic check cannot drift; and the corpus is pesticide
# guidance, so the false-positive surface is numbers -- doses, concentrations,
# page references -- which patterns can be written to exclude and a model would
# have to be trusted about, question by question, forever.
# ---------------------------------------------------------------------------

# (?<!\d) / (?!\d) everywhere: without them a 13-digit ID is also "a 10-digit
# phone number starting at offset 3", and every long number matches something.
_PATTERNS = {
    # 13 digits, optionally grouped with - or spaces. The digit count alone is
    # not the test -- see _has_valid_id_checksum.
    "thai_national_id": re.compile(r"(?<!\d)(?:\d[- ]?){12}\d(?!\d)"),
    # Thai numbers start with a 0 trunk code: 9 digits for older landlines,
    # 10 for mobiles and most landlines now.
    "thai_phone": re.compile(r"(?<!\d)0(?:[- ]?\d){8,9}(?!\d)"),
    "email": re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
}

# Everything, on the way in.
INPUT_PII_KINDS = frozenset(_PATTERNS)

# Not everything, on the way out, and the difference is not an oversight. The
# answer is assembled from a government handbook, and a handbook that prints
# its own department's switchboard would have that number quoted back in a
# correct answer. An institutional contact line is public information about an
# organisation, not personal data about a person, so refusing that answer
# protects nobody. An ID card number or a personal email in the output has no
# such excuse: it could only have come from the model inventing one.
#
# Assumption, not a measurement: the 4-page test fixture has no phone-shaped
# strings in it, and the three real volumes have not been scanned. If none of
# them carries a contact number either, this carve-out is buying nothing and
# should be narrowed to match INPUT_PII_KINDS.
ANSWER_PII_KINDS = frozenset({"thai_national_id", "email"})


def _has_valid_id_checksum(digits: str) -> bool:
    """The 13th digit of a Thai national ID checks the other twelve.

    This is what keeps the pattern from firing on every 13-digit number in the
    corpus. An ISBN-13 on a handbook cover is also thirteen digits grouped
    with hyphens; it fails here, because it carries its own, different check
    digit. Length alone would flag every one of them.
    """
    weighted = sum(int(d) * (13 - i) for i, d in enumerate(digits[:12]))
    return (11 - weighted % 11) % 10 == int(digits[12])


def find_pii(text: str, *, kinds: frozenset[str] = INPUT_PII_KINDS) -> list[str]:
    """Names of the PII kinds present in `text`, sorted, without the values.

    The values are deliberately not returned. This result is what a caller
    logs or puts in an error message, and a guard that copies the ID card
    number into a log line has moved the problem rather than solved it.
    """
    found = set()
    for kind, pattern in _PATTERNS.items():
        if kind not in kinds:
            continue
        for match in pattern.finditer(text):
            if kind == "thai_national_id":
                digits = re.sub(r"\D", "", match.group())
                if not _has_valid_id_checksum(digits):
                    continue
            found.add(kind)
            break
    return sorted(found)


def check_question(question: str) -> None:
    """Raise PIIDetected if the question must not leave this process."""
    kinds = find_pii(question, kinds=INPUT_PII_KINDS)
    if kinds:
        raise PIIDetected(f"question contains {', '.join(kinds)}")


def check_answer_text(text: str) -> None:
    """Raise PIIDetected if the answer must not be returned. See
    ANSWER_PII_KINDS for why this checks less than check_question does.
    """
    kinds = find_pii(text, kinds=ANSWER_PII_KINDS)
    if kinds:
        raise PIIDetected(f"answer contains {', '.join(kinds)}")


# ---------------------------------------------------------------------------
# Grounding
# ---------------------------------------------------------------------------


def build_grounding_prompt(question: str, answer: Answer) -> str:
    """The judge's whole view: the question, the answer, the cited excerpts.

    Taking an `Answer` rather than a list of passages is the mechanism, not a
    convenience. Answer.citations holds only the excerpts the model actually
    cited; the ones retrieval returned and the model ignored are not reachable
    from here at all. So "the judge sees only what was cited" cannot be broken
    by a careless edit -- there is nothing else to pass. Handing it every
    retrieved passage would make an answer that cites [1] while quoting [3]
    look supported, which is precisely the mis-citation invariant 10 exists to
    prevent.

    Excerpts keep the numbers the answering model saw, so a claim about "[2]"
    lines up with the same text in both prompts. `section` travels with them
    for the reason prompt.py gives: a mango table and a durian table are
    near-identical in content and the crop is only in the heading.
    """
    excerpts = [
        f"[{c.number}] "
        + (f"({c.passage.section}) " if c.passage.section else "")
        + c.passage.content
        for c in answer.citations
    ]
    return "\n\n".join(
        [
            JUDGE_INSTRUCTIONS,
            *excerpts,
            f"Question: {question}",
            f"Answer: {answer.text}",
        ]
    )


def _parse_verdict(raw: str) -> bool:
    """The judge's reply as a boolean, or GroundingUndecided.

    Three outcomes, not two, and collapsing the third into False would be a
    real bug: "the judge is broken" would then be indistinguishable from "the
    judge says this answer is wrong", and the broken judge would send every
    answer round the retry loop before refusing -- double the cost to reach
    the same refusal.
    """
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise GroundingUndecided(f"judge reply is not JSON: {raw[:200]!r}") from exc

    if not isinstance(data, dict) or "grounded" not in data:
        raise GroundingUndecided(f"judge reply has no verdict: {raw[:200]!r}")

    verdict = data["grounded"]
    # `is True` / `is False`, not truthiness: "maybe", "" and 1 are all replies
    # from a judge that did not follow the contract, and guessing what they
    # meant is how a guard starts passing things it never decided on.
    if verdict is True or verdict is False:
        return verdict
    raise GroundingUndecided(f"judge verdict is not a boolean: {verdict!r}")


def deepseek_judge(prompt: str) -> str:
    """The real Judge. Same client shape as answer.py's openrouter_chat.

    response_format is load-bearing here for the reason KNOWLEDGE.md records
    against the answering model: every model tried wraps its JSON in a ```json
    fence when merely asked not to, and JSON mode is the only thing that
    actually stops it.

    No tenacity wrapper, unlike rerank.py: that module posts with raw
    `requests` and has to implement its own 429 backoff, while the OpenAI
    client already retries rate limits and connection errors internally.
    """
    client = OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=os.environ["OPENROUTER_API_KEY"],
    )
    reply = client.chat.completions.create(
        model=JUDGE_MODEL,
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
    )
    return reply.choices[0].message.content


def check_grounding(
    question: str,
    answer: Answer,
    *,
    judge: Judge = deepseek_judge,
) -> None:
    """Raise NotGrounded if the answer is not supported by what it cites.

    Silence means it passed. Returning a bool instead would put the decision
    in the caller's hands and make forgetting to check it a silent bypass;
    raising cannot be ignored by accident.

    An answer with no citations is already a refusal (prompt.py discards the
    model's text for one), so there is nothing to ground and the judge is not
    called. That early return is what keeps the cheapest outcome in the system
    -- declining to answer -- from being the one that costs an extra API call.
    """
    if not answer.citations:
        return

    try:
        raw = judge(build_grounding_prompt(question, answer))
    except openai.OpenAIError as exc:
        # Fail closed. app/main.py catches nothing, so letting this through
        # would turn a judge outage into a 500; and a 500 is a worse answer
        # than a refusal, because a refusal is a documented outcome and an
        # unhandled error invites a client to retry it.
        raise GroundingUndecided(f"judge call failed: {exc}") from exc

    if not _parse_verdict(raw):
        raise NotGrounded("the answer is not supported by the excerpts it cites")
