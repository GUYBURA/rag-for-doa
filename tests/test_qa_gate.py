import pytest
from ingest.qa_gate import qa_gate
from ingest.extract import Page
from ingest.normalize import PUA_TO_THAI

THAI_OK = "สวัสดี คุณชื่ออะไร เราชื่อบยัปป์"
THAI_NO_MARKS = "สวสด คณชออะไร เราชอบยปป"
THAI_LONG = THAI_OK * 3
ASCII_ONLY = "Chlorpyrifos 40% EC, 20-30 ml per 20 L water"

def make_pages(texts, start=1):
    """Page numbers"""
    return [Page(page_number=i, raw_text=text) for i, text in enumerate(texts, start=start)]

def failed(result):
    """Check for any result that failed"""
    return {key for key, value in result.items() if not value["passed"]}

@pytest.mark.parametrize(
    "numbers",[
        [1,2,2],
        [1,3],
        [0,1,2],
        [1,2,4]
    ],
)
def test_page_numbers_must_be_1_to_n(numbers):
    pages = [Page(page_number=n, raw_text=THAI_OK) for n in numbers]
    result = qa_gate(pages, pdf_page_count=len(numbers), scopes=["fungicide"])

    assert "page_numbers_not_sequential" in failed(result)

def test_sequential_page_number_pass():
    pages = make_pages([THAI_OK] * 5)
    result = qa_gate(pages, pdf_page_count=len(pages), scopes=["fungicide"]) 
    assert "page_numbers_not_sequential" not in failed(result)

def test_page_count_mismatch_is_caught():
    pages = make_pages([THAI_OK] * 3)
    result = qa_gate(pages, pdf_page_count=4, scopes=["fungicide"])
    assert "page_count_mismatch" in failed(result)
    assert "page_numbers_not_sequential" not in failed(result)

def test_two_blank_pages_in_three_hundred_pass():
    pages = make_pages([THAI_OK] * 298 + ["", ""])
    result = qa_gate(pages, pdf_page_count=300, scopes=["fungicide"])
    assert failed(result) == set()

def test_thin_pages_counted_but_not_blocking():
    pages = make_pages([THAI_LONG] * 10 + [THAI_OK] * 3)
    result = qa_gate(pages, pdf_page_count=13, scopes=["fungicide"])
    assert "thin_pages" not in failed(result)
    assert result["thin_pages"]["measured"] == 3

def test_mapped_pua_passes():
    pua = next(iter(PUA_TO_THAI))
    pages = make_pages([THAI_OK + pua] * 3)
    result = qa_gate(pages, pdf_page_count=3, scopes=["fungicide"])
    assert "unmapped_pua_codepoints" not in failed(result)

def test_unmapped_pua_fails():
    unmapped = next(
        chr(c) for c in range(0xF700, 0xF71B) if chr(c) not in PUA_TO_THAI
    )
    pages = make_pages([THAI_OK + unmapped] * 3)
    result = qa_gate(pages, pdf_page_count=3, scopes=["fungicide"])
    assert "unmapped_pua_codepoints" in failed(result)

def test_thai_stripped_of_marks_fails():
    pages = make_pages([THAI_NO_MARKS] * 3)
    result = qa_gate(pages, pdf_page_count=3, scopes=["fungicide"])
    assert "combining_ratio_too_low" in failed(result)
    assert "no_thai_consonants" not in failed(result)

def test_no_thai_consonants_fails_for_a_different_reason():
    pages = make_pages([ASCII_ONLY] * 3)
    result = qa_gate(pages, pdf_page_count=3, scopes=["fungicide"])
    assert "no_thai_consonants" in failed(result)
    assert "combining_ratio_too_low" not in failed(result)

def test_empty_scopes_fails():
    pages = make_pages([THAI_OK] * 3)
    result = qa_gate(pages, pdf_page_count=3, scopes=[])
    assert "scopes_empty" in failed(result)

def test_non_empty_scopes_pass():
    pages = make_pages([THAI_OK] * 3)
    result = qa_gate(pages, pdf_page_count=3, scopes=["insecticide"])
    assert "scopes_empty" not in failed(result)

def test_all_failures_reported_together():
    pages = [Page(page_number=n, raw_text=THAI_NO_MARKS) for n in [1, 1, 3]]
    result = qa_gate(pages, pdf_page_count=99, scopes=[])
    assert len(failed(result)) >= 4

def test_most_of_the_book_blank_fails():
    pages = make_pages([THAI_LONG] * 4 + [""] * 6)
    result = qa_gate(pages, pdf_page_count=10, scopes=["fungicide"])
    assert "empty_ratio_too_high" in failed(result)

def test_unmapped_pua_is_reported_by_codepoint():
    unmapped = next(
        chr(c) for c in range(0xF700, 0xF71B) if chr(c) not in PUA_TO_THAI
    )
    pages = make_pages([THAI_OK + unmapped] * 3)
    result = qa_gate(pages, pdf_page_count=3, scopes=["fungicide"])
    assert result["unmapped_pua_codepoints"]["measured"] == [f"U+{ord(unmapped):04X}"]
