import hashlib
import pathlib

from ingest.chunk import _section_for, _split_table_markdown, chunk_document
from ingest.extract import Block, Table, extract_blocks, extract_pages, extract_tables
from ingest.normalize import detect_running_lines, normalize_table

EXCERPT = pathlib.Path(__file__).resolve().parent / "fixtures" / "excerpt.pdf"

FOOTER = "คำแนะนำ การใช้สารป้องกันกำจัดศัตรูพืชจากงานวิจัย"


def _fixture(max_chars=1800):
    pages = extract_pages(str(EXCERPT))
    return chunk_document(
        extract_blocks(str(EXCERPT)),
        extract_tables(str(EXCERPT)),
        detect_running_lines(pages),
        max_chars=max_chars,
    )


def _widest_table():
    return max(extract_tables(str(EXCERPT)), key=lambda t: len(t.markdown))


def _rows(markdown):
    """The data rows, without the header and separator."""
    return markdown.split("\n")[2:]


def _block(page_number, y0, y1, text):
    return Block(page_number=page_number, bbox=(0.0, y0, 500.0, y1), text=text)


def _table(page_number, y0):
    return Table(
        page_number=page_number,
        bbox=(0.0, y0, 500.0, y0 + 100),
        row_count=1,
        col_count=1,
        cells=(("x",),),
        markdown="|x|\n|---|",
    )


def test_a_table_within_budget_is_one_chunk():
    markdown = normalize_table(_widest_table().markdown)
    assert _split_table_markdown(markdown, 1800) == [markdown]


def test_every_part_of_a_split_table_repeats_the_header():
    markdown = normalize_table(_widest_table().markdown)
    header, separator = markdown.split("\n")[:2]

    parts = _split_table_markdown(markdown, 300)

    assert len(parts) > 1
    assert all(part.startswith(header + "\n" + separator) for part in parts)


def test_splitting_never_cuts_a_row():
    # The rows themselves, in order -- not their count. A splitter that dropped
    # one row and duplicated another would keep the count identical.
    markdown = normalize_table(_widest_table().markdown)
    rebuilt = [row for part in _split_table_markdown(markdown, 300) for row in _rows(part)]
    assert rebuilt == _rows(markdown)


def test_a_row_longer_than_the_budget_survives_whole():
    # A truncated dosage row still reads as a complete recommendation, with the
    # wrong rate attached to the wrong chemical, so an oversized chunk is the
    # better failure.
    long_row = "|" + "ก" * 500 + "|"
    markdown = "\n".join(["|h|", "|---|", long_row, "|short|"])

    parts = _split_table_markdown(markdown, 100)

    assert any(long_row in part for part in parts)


def test_each_table_on_a_page_gets_its_own_heading():
    # Fixture page 4 carries three dosage tables under three different crops.
    # A search that walks down the page instead of up gives all three the first
    # crop's name -- a wrong answer that reads as fact, not a crash.
    blocks = [b for b in extract_blocks(str(EXCERPT)) if b.page_number == 4]
    tables = sorted(
        (t for t in extract_tables(str(EXCERPT)) if t.page_number == 4),
        key=lambda t: t.bbox[1],
    )

    sections = [_section_for(table, blocks) for table in tables]

    assert sections == [
        "มันสำปะหลัง (Cassava)",
        "ถั่วเขียว (Mung beans)",
        "ถั่วเหลือง (Soybean)",
    ]


def test_a_table_with_no_heading_above_it_has_no_section():
    # The twin of the test above. A continuation page carries no crop name, and
    # inheriting the previous page's would label the table with the wrong crop.
    blocks = [_block(7, 40, 60, "หน้า 7"), _block(7, 500, 520, "ตารางที่ 3 (ต่อ)")]
    assert _section_for(_table(7, 100), blocks) is None


def test_a_heading_on_another_page_is_not_used():
    heading_on_page_six = [_block(6, 40, 60, "ทุเรียน (Durian)")]
    assert _section_for(_table(7, 100), heading_on_page_six) is None


def test_prose_chunks_leave_out_the_text_inside_tables():
    chunks = _fixture()
    prose = "\n".join(c.content for c in chunks if c.kind == "prose")
    inside_a_table = "โรคแอนแทรคโนส"

    assert inside_a_table in "\n".join(c.content for c in chunks if c.kind == "table")
    assert inside_a_table not in prose


def test_the_running_footer_reaches_no_chunk():
    # It carries the edition year. A chunk containing it would be embedded with
    # "2568" in it and would still be served after 2568 is superseded.
    assert all(FOOTER not in c.content for c in _fixture())


def test_every_chunk_carries_its_page_number():
    assert all(c.page_number >= 1 for c in _fixture())


def test_content_sha256_is_the_hash_of_the_content():
    for chunk in _fixture():
        assert chunk.content_sha256 == hashlib.sha256(
            chunk.content.encode("utf-8")
        ).hexdigest()


def test_identical_content_is_stored_once():
    # chunk_dedup_idx is unique on (document_id, content_sha256), so a repeat
    # would abort the whole insert rather than being ignored.
    chunks = _fixture()
    hashes = [c.content_sha256 for c in chunks]
    assert len(hashes) == len(set(hashes))
