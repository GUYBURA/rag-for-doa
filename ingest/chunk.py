import hashlib
import re
from ingest.extract import Block, Table
from ingest.normalize import normalize, normalize_table
from langchain_text_splitters import RecursiveCharacterTextSplitter
from dataclasses import dataclass

@dataclass(frozen=True)
class Chunk:
    content: str
    content_sha256: str
    page_number: int
    section: str | None
    kind: str
    metadata: dict

HEADING = re.compile(r"^[ก-๛\s]+\([A-Za-z][A-Za-z\s,'\-\.]*\)")

def _make_chunk(content: str, page_number: int, section: str | None, kind: str) -> Chunk:
    return Chunk(
        content=content,
        content_sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
        page_number=page_number,
        section=section,
        kind=kind,
        metadata={"kind": kind, "page_number": page_number, "section": section},
    )

def _split_table_markdown(markdown: str, max_chars: int) -> list[str]:
    """One markdown table -> parts that each stand on their own."""
    lines = markdown.split("\n")
    header, separator, *rows = lines

    if len(markdown) <= max_chars:
        return [markdown]

    parts, current = [], []
    budget = max_chars - len(header) - len(separator) - 2

    for row in rows:
        if current and sum(len(r) + 1 for r in current) + len(row) > budget:
            parts.append("\n".join([header, separator, *current]))
            current = []
        current.append(row)

    if current:
        parts.append("\n".join([header, separator, *current]))
    return parts

def _section_for(table: Table, blocks: list[Block]) -> str | None:
    """The crop heading printed above this table, or None.

    Same page only. A table continuing onto the next page gets None rather than
    the previous page's crop: a wrong section is worse than a missing one, it
    reads as fact.
    """
    above = [b for b in blocks if b.page_number == table.page_number and b.bbox[3] <= table.bbox[1]]

    for block in sorted(above, key=lambda b: -b.bbox[3]):
        first_line = block.text.split("\n")[0].strip()
        if HEADING.match(first_line):
            return first_line
    return None

def _overlaps(a: tuple[float, ...], b: tuple[float, ...]) -> bool:
    """True if two rectangles share any area. bbox is (x0, y0, x1, y1)."""
    return a[0] < b[2] and a[2] > b[0] and a[1] < b[3] and a[3] > b[1]


def _prose_text(page_blocks: list[Block], page_tables: list[Table]) -> str:
    """The text on a page that is not inside any table.

    On a page carrying tables this is 9-14% of the text: the crop heading, a
    method note, the page number and the running footer. The other 86-91% is
    the tables, and chunking it here as well would index the same content twice
    in two shapes -- content_sha256 would differ, so the dedup index would not
    catch it and both copies would compete for the same top-5 slot.

    Blocks are joined in reading order because normalize() identifies running
    headers and footers by their position among the lines.
    """
    outside = [
        block
        for block in page_blocks
        if not any(_overlaps(block.bbox, table.bbox) for table in page_tables)
    ]
    return "\n".join(b.text for b in sorted(outside, key=lambda b: b.bbox[1]))


def chunk_document(
    blocks: list[Block],
    tables: list[Table],
    running_lines: frozenset[str] = frozenset(),
    *,
    max_chars: int = 1800,
    overlap: int = 150,
) -> list[Chunk]:
    """Split one document into the passages that will be embedded.

    Pure: no database, no PyMuPDF, no network. Everything it needs was already
    read by extract.py, which is what makes it testable without a container.

    Tables and prose are split by different rules because they fail
    differently. Prose rewards overlap - a sentence cut in half is recoverable
    from the neighbouring chunk. A dosage table row cut in half is not: the
    remaining half still reads as a complete recommendation, with the wrong
    rate attached to the wrong chemical.
    """
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=max_chars,
        chunk_overlap=overlap,
        length_function=len,
        # No Thai sentence terminator exists, and Thai does not put spaces
        # between words, so paragraph and line breaks are the only real
        # boundaries in the text. The last two entries are the fallbacks
        # LangChain needs to guarantee it can always split something.
        separators=["\n\n", "\n", " ", ""],
    )

    by_page_blocks: dict[int, list[Block]] = {}
    for block in blocks:
        by_page_blocks.setdefault(block.page_number, []).append(block)

    by_page_tables: dict[int, list[Table]] = {}
    for table in tables:
        by_page_tables.setdefault(table.page_number, []).append(table)

    chunks: list[Chunk] = []
    for page_number in sorted(by_page_blocks):
        page_blocks = by_page_blocks[page_number]
        page_tables = sorted(
            by_page_tables.get(page_number, []), key=lambda t: t.bbox[1]
        )

        prose = normalize(_prose_text(page_blocks, page_tables), running_lines)
        for piece in splitter.split_text(prose):
            if piece.strip():
                chunks.append(_make_chunk(piece, page_number, None, "prose"))

        for table in page_tables:
            section = _section_for(table, page_blocks)
            for part in _split_table_markdown(
                normalize_table(table.markdown), max_chars
            ):
                chunks.append(_make_chunk(part, page_number, section, "table"))

    # The chunk_dedup_idx unique index would abort the whole insert on a
    # repeat, and a document does repeat itself: a running line that survived
    # normalization, or an empty table rendered identically twice.
    seen: set[str] = set()
    unique = []
    for chunk in chunks:
        if chunk.content_sha256 in seen:
            continue
        seen.add(chunk.content_sha256)
        unique.append(chunk)
    return unique
