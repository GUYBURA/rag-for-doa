import pathlib

import pytest

from ingest.extract import extract_pages, page_count
from ingest.normalize import PUA_RANGE

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SOURCE_2568 = REPO_ROOT / "data" / "raw" / "2568.pdf"
EXCERPT = pathlib.Path(__file__).resolve().parent / "fixtures" / "excerpt.pdf"

@pytest.mark.requires_source_pdfs
@pytest.mark.skipif(not SOURCE_2568.exists(), reason="source PDF not present")
def test_real_2568_has_300_pages():
    assert page_count(str(SOURCE_2568)) == 300
    
def test_page_count_matches_a_known_fixture():
    assert page_count(str(EXCERPT)) == 3

def test_one_page_object_per_pdf_page():
    pages = extract_pages(str(EXCERPT))
    assert len(pages) == 3

def test_page_numbers_are_one_based_and_ordered():
    pages = extract_pages(str(EXCERPT))
    assert [p.page_number for p in pages] == [1,2,3]

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