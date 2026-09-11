"""Prompt assembly and answer parsing. Citations are constructed here.

Pure: nothing below calls an LLM or touches the network. build_prompt() turns
reranked passages into a string, parse_answer() turns the model's reply back
into an Answer whose citations point at real Passage objects. Whoever owns the
HTTP call sits between the two -- app/main.py, not this module.

Refusal never reaches here. rerank() returning [] already means "no relevant
document" (invariant 11), and that is decided in Python rather than delegated
to the model's obedience, so the caller short-circuits to REFUSAL_TEXT instead
of building a prompt with no context in it.
"""

from collections.abc import Sequence
from dataclasses import dataclass
import json

from query.rerank import ScoredPassage
from query.retrieve import Passage

REFUSAL_TEXT = "ไม่พบเอกสารที่เกี่ยวข้องกับคำถามนี้"


@dataclass(frozen=True)
class Citation:
    """A [n] marker bound to the passage it points at.

    The number and the Passage travel together so that nothing downstream has
    to re-derive one from the other by assuming list order -- an assumption
    that, if it ever drifted, would cite a page that does not contain the
    claim (invariant 10).
    """

    number: int
    passage: Passage


@dataclass(frozen=True)
class Prompt:
    text: str
    citations: list[Citation]


@dataclass(frozen=True)
class Answer:
    text: str
    citations: list[Citation]


# The model never sees an edition year or a page number: those reach the
# answer from the Passage objects in Python, so a citation cannot point
# anywhere the retrieval path did not actually return (invariant 10).
# `section` is the exception and it earns its place -- a pesticide table for
# mango and one for durian look nearly identical in `content`, and the crop
# only appears in the heading that chunk.py split off. Without it the model
# cannot tell it is being asked about the wrong crop.
INSTRUCTIONS = """\
You answer questions about Thai pesticide guidance using ONLY the numbered \
excerpts below. Each excerpt is labelled [n] and may carry the document \
section it came from in parentheses.

Rules:
1. NO OUTSIDE KNOWLEDGE. Use only the excerpts. You know a great deal about \
pesticides that is not in them; none of it may appear in your answer.
2. THE SECTION LABEL IS EVIDENCE. It names the crop or subject the excerpt \
belongs to, and the content alone often does not -- a table for mango and one \
for durian look nearly identical. If the question asks about a crop no \
excerpt covers, you cannot answer it, even when an excerpt discusses the same \
pest or the same chemical.
3. ANSWER IN THE SAME LANGUAGE AS THE QUESTION.
4. CITE EVERY CLAIM by the excerpt number it came from. Numbering starts at 1. \
Never write a number that is not one of the excerpts given to you.
5. OUTPUT EXACTLY ONE JSON OBJECT and nothing else -- no prose before or \
after it, no markdown code fence:
   {"answer": "<your answer>", "citations": [<excerpt numbers you used>]}
6. IF THE EXCERPTS DO NOT ANSWER THE QUESTION, return an empty citations list. \
That is a correct and expected outcome, not a failure.
"""


def _passage_block(number: int, passage: Passage) -> str:
    """One numbered document as the model sees it."""
    label = f"({passage.section}) " if passage.section else ""
    return f"[{number}] {label}{passage.content}"


def build_prompt(question: str, passages: Sequence[ScoredPassage]) -> Prompt:
    if not passages:
        raise ValueError(
            "no passages to ground an answer in -- an empty rerank result is a "
            "refusal (REFUSAL_TEXT), decided by the caller before reaching here"
        )

    # 1-based, and the number comes from position here -- never from
    # passage.rank, which reranking has already reordered away from.
    citations = [
        Citation(number=n, passage=sp.passage)
        for n, sp in enumerate(passages, start=1)
    ]
    # Built from `citations` rather than by enumerating `passages` a second
    # time: the number the model reads and the number bound to the Passage
    # have to come from one source, or a future edit can drift them apart in
    # a way no test would notice.
    blocks = [_passage_block(c.number, c.passage) for c in citations]
    text = "\n\n".join([INSTRUCTIONS, *blocks, f"Question: {question}"])
    return Prompt(text=text, citations=citations)


def parse_answer(raw: str, prompt: Prompt) -> Answer:
    data = json.loads(raw)  # raises JSONDecodeError, which is already a ValueError

    numbers = data["citations"]
    for n in numbers:
        # Validated before the refusal check below, not after: a reply that
        # mixes a real citation with an invented one is a grounding failure
        # and has to raise, so the caller retries once and then refuses
        # (invariant 11).
        if not (1 <= n <= len(prompt.citations)):
            raise ValueError(
                f"citation [{n}] is not one of the {len(prompt.citations)} "
                "excerpts sent"
            )

    # Order-preserving dedup. A model citing [1] twice in one answer is
    # normal; two identical Citation entries in the result are not.
    unique = list(dict.fromkeys(numbers))

    if not unique:
        # No source means nothing here can be served, whatever the model
        # wrote in `answer` (invariant 10). Its text is discarded, not shown.
        return Answer(REFUSAL_TEXT, [])

    return Answer(
        text=data["answer"],
        citations=[prompt.citations[n - 1] for n in unique],
    )
