import pathlib

import pytest

from ingest.extract import _to_markdown, extract_pages, extract_tables, page_count
from ingest.normalize import PUA_RANGE
from ingest.qa_gate import THAI_COMBINING, THAI_CONSONANT

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SOURCE_2568 = REPO_ROOT / "data" / "raw" / "2568.pdf"
EXCERPT = pathlib.Path(__file__).resolve().parent / "fixtures" / "excerpt.pdf"

@pytest.mark.requires_source_pdfs
@pytest.mark.skipif(not SOURCE_2568.exists(), reason="source PDF not present")
def test_real_2568_has_300_pages():
    assert page_count(str(SOURCE_2568)) == 300
    
def test_page_count_matches_a_known_fixture():
    assert page_count(str(EXCERPT)) == 4

def test_one_page_object_per_pdf_page():
    pages = extract_pages(str(EXCERPT))
    assert len(pages) == 4

def test_page_numbers_are_one_based_and_ordered():
    pages = extract_pages(str(EXCERPT))
    assert [p.page_number for p in pages] == [1,2,3,4]

def test_raw_text_is_not_trimmed():
    pages = extract_pages(str(EXCERPT))
    divider = pages[1]              
    assert divider.raw_text.startswith(" ")
    assert divider.raw_text.endswith("\n")

def test_pua_codepoints_survive_extraction():
    # Fixture page 3 is 2565 p220, 193 Thai tone marks still in the private use
    # area. If restore_pua_tone_marks() ever migrates into extract.py, this
    # fails - invariant 8 wants the gate to see the damage before anyone
    # repairs it. The count is a constant measured from the fixture; deriving
    # it from the source PDF would make both sides the same number.
    pages = extract_pages(str(EXCERPT))
    assert len(PUA_RANGE.findall(pages[2].raw_text)) == 193

# The corrupted spelling PyMuPDF's own table text methods produce for ศัตรูพืช:
# the below-vowel is moved onto a line of its own and lands after the last
# consonant. Asserting it is absent is the twin of asserting the correct form
# is present -- either one alone would pass on an empty markdown string.
BROKEN = "ศัตรพชื"
INTACT = "ศัตรูพืช"


def _combining_ratio(text):
    consonants = len(THAI_CONSONANT.findall(text))
    return len(THAI_COMBINING.findall(text)) / consonants


def test_tables_are_found_only_on_the_page_that_has_them():
    tables = extract_tables(str(EXCERPT))
    assert {t.page_number for t in tables} == {4}


def test_table_geometry_matches_the_dosage_layout():
    tables = extract_tables(str(EXCERPT))
    widest = max(tables, key=lambda t: t.col_count)
    assert widest.col_count == 12
    assert len(widest.cells) == widest.row_count
    assert all(len(row) == widest.col_count for row in widest.cells)


def test_a_column_empty_in_every_row_is_dropped():
    """Merged cells report their text once and leave the rest of the span
    blank, and the ruling lines add narrow spacer columns that never hold
    anything -- page 56 of 2568 is 11 columns of which four are always empty.
    """
    cells = (("a", "", "b"), ("c", "", "d"))
    assert _to_markdown(cells) == "|a|b|\n|---|---|\n|c|d|"


def test_a_column_carrying_text_in_only_one_row_is_kept():
    """The twin of the test above. Dropping a column because it is mostly
    empty would delete a heading that spans a merged group, or a footnote
    marker on one row.
    """
    cells = (("a", "", "b"), ("c", "note", "d"))
    assert _to_markdown(cells) == "|a||b|\n|---|---|---|\n|c|note|d|"


def test_a_row_empty_in_every_column_is_dropped():
    cells = (("a", "b"), ("", ""), ("c", "d"))
    assert _to_markdown(cells) == "|a|b|\n|---|---|\n|c|d|"


def test_a_newline_inside_a_cell_becomes_a_space_not_a_br():
    """The break is where the printed table wrapped, not part of the data.
    Rendered as "<br>" it splits terms that belong together, and a Latin
    binomial that reads "Phakopsora<br>pachyrhizi" is not a substring match
    for the name anyone would search for or cite.
    """
    cells = (("Phakopsora\npachyrhizi", "x"),)
    assert _to_markdown(cells) == "|Phakopsora pachyrhizi|x|\n|---|---|"
    assert "<br>" not in _to_markdown(cells)


def test_a_pipe_inside_a_cell_is_escaped():
    """Unescaped, it would end the cell early and shift every value after it
    into the wrong column.
    """
    cells = (("a|b", "c"),)
    assert _to_markdown(cells) == "|a\\|b|c|\n|---|---|"


def test_a_table_with_nothing_in_it_renders_as_nothing():
    assert _to_markdown((("", ""), ("", ""))) == ""


def test_markdown_keeps_thai_marks_where_they_belong():
    tables = extract_tables(str(EXCERPT))
    markdown = "\n".join(t.markdown for t in tables)
    assert INTACT in markdown
    assert BROKEN not in markdown


def test_markdown_carries_as_many_marks_as_ordinary_thai():
    # to_markdown() scores 0.0432 on this page, so this catches it. It does not
    # catch table.extract(), which keeps most marks (0.2796) but moves them to
    # the wrong character -- displacement is invisible to a ratio, which is why
    # the test above spells out the corrupted word instead.
    tables = extract_tables(str(EXCERPT))
    markdown = "\n".join(t.markdown for t in tables)
    assert _combining_ratio(markdown) >= 0.20


def test_cells_keep_the_raw_line_breaks_markdown_cannot():
    tables = extract_tables(str(EXCERPT))
    assert any("\n" in cell for t in tables for row in t.cells for cell in row)
    assert all("\n" not in line.strip("|") for t in tables for line in t.markdown.split("\n"))


def test_markdown_has_one_line_per_row_plus_a_separator():
    tables = extract_tables(str(EXCERPT))
    for table in tables:
        assert len(table.markdown.split("\n")) == table.row_count + 1
