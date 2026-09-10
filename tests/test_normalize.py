import unicodedata

import pytest
from ingest.normalize import (
    PUA_TO_THAI,
    _running_key, 
    collapse_whitespace, 
    strip_page_furniture, 
    detect_running_lines, 
    strip_running_lines, 
    restore_pua_tone_marks, 
    to_nfc
)
from ingest.extract import Page

@pytest.mark.parametrize(
    "given_text, expected_text",
    [
        ("This is a test.", "This is a test."),
        ("ไดโนทีฟูแรน 10% WP 20 กรัม", "ไดโนทีฟูแรน 10% WP 20 กรัม"),
        ("This  is   a    test.", "This is a test."),
        ("สิ่งนี้\tคือ\t\tบททดสอบ", "สิ่งนี้ คือ บททดสอบ"),
        ("นี่\nคือ", "นี่\nคือ"),
        ("นี่\n\nคือ", "นี่\n\nคือ"),
        ("นี่\n\n\n\nคือ", "นี่\n\nคือ"),
        ("นี่ \nคือ", "นี่\nคือ"),
        ("   This is a test.   ", "This is a test."),
        (
            " สวัสดี นี่ คือ บททดสอบ \n\nความถูกต้อง ของ ฟังก์ชัน ",
            "สวัสดี นี่ คือ บททดสอบ\n\nความถูกต้อง ของ ฟังก์ชัน",
        ),
        ("ไดโนทีฟูแรน 10% WP\n อัตรา 20 กรัม", "ไดโนทีฟูแรน 10% WP\n อัตรา 20 กรัม"),
    ],
)
def test_collapse_whitespace(given_text, expected_text):
    assert collapse_whitespace(given_text) == expected_text

@pytest.mark.parametrize(
    "given_text,expected_text",
    [
        ("12\nเนื้อหา\nเนื้อหาสอง", "เนื้อหา\nเนื้อหาสอง"),
        ("เนื้อหา\n๑๒",	"เนื้อหา"),
        ("เนื้อหา\n- 12 -",	"เนื้อหา"),
        ("อัตรา 20 กรัม\nเนื้อหา", "อัตรา 20 กรัม\nเนื้อหา"),
        ("หัวข้อ\n20\nกรัมต่อไร่", "หัวข้อ\n20\nกรัมต่อไร่"),
        ("บทที่ 1 การใช้สาร .......... 15", "บทที่ 1 การใช้สาร"),
        ("อัตรา 2.5 ลิตร", "อัตรา 2.5 ลิตร"),
        ("ดูหมายเหตุ..", "ดูหมายเหตุ.."),
        ("เนื้อหา\nเนื้อหาสอง\n12", "เนื้อหา\nเนื้อหาสอง"),
        ("บทที่ 2 โรคพืช ..........", "บทที่ 2 โรคพืช")
    ]
)
def test_strip_page_furniture(given_text, expected_text):
    assert strip_page_furniture(given_text) == expected_text

def make_pages(*page_line_lists: list[str]) -> list[Page]:
    return [
        Page(page_number=i + 1, raw_text="\n".join(lines))
        for i, lines in enumerate(page_line_lists)
    ]

def test_same_line_ten_times_on_one_page_is_not_a_header():
    pages = make_pages(
        ["ข้อควรระวัง"] * 10,
        ["เนื้อหาหน้าสอง"],
        ["เนื้อหาหน้าสาม"],
    )
    result = detect_running_lines(pages)
    assert result == frozenset()

def test_table_column_headers_in_middle_of_page_is_not_a_header():
    column_header = "ชนิดพืช ศัตรูพืช อัตราการใช้ วิธีใช้"
    page = [
        "คู่มือการใช้สารป้องกันกำจัดศัตรูพืช 2568",   # ขอบบน
        "บทที่ 3 สารกำจัดแมลง",
        "ตารางที่ 3.1",
        column_header,                                # กลางหน้า
        "ข้าว เพลี้ยกระโดด 20 กรัม พ่น",
        "ข้าวโพด หนอนเจาะ 30 กรัม พ่น",
        "ถั่วเหลือง หนอนม้วนใบ 25 กรัม พ่น",
        "กรมวิชาการเกษตร",                            # ขอบล่าง
    ]
    pages = make_pages(page, list(page))

    result = detect_running_lines(pages)
    assert len(result) == 6

def test_line_below_ratio_is_not_a_header():
    pages = make_pages(
        ["สงวนลิขสิทธิ์", "เนื้อหา"],
        ["เนื้อหาสอง"],
        ["เนื้อหาสาม"],
        ["เนื้อหาสี่"],
    )
    assert detect_running_lines(pages, min_page_ratio=0.5) == frozenset()

def test_header_with_changing_page_number_is_still_one_header():
    pages = make_pages(
        ["คู่มือการใช้สารป้องกันกำจัดศัตรูพืช 2568   12", "เนื้อหาหน้าหนึ่ง"],
        ["คู่มือการใช้สารป้องกันกำจัดศัตรูพืช 2568   13", "เนื้อหาหน้าสอง"],
        ["คู่มือการใช้สารป้องกันกำจัดศัตรูพืช 2568   14", "เนื้อหาหน้าสาม"],
    )

    result = detect_running_lines(pages)

    assert len(result) == 1

def test_strips_header_at_page_edge():
    header = "คู่มือการใช้สารป้องกันกำจัดศัตรูพืช 2568   12"
    text = "\n".join([
        header,
        "บทที่ 3 สารกำจัดแมลง",
        "ข้าว เพลี้ยกระโดด 20 กรัม",
    ])

    running = frozenset({_running_key(header)})

    result = strip_running_lines(text, running)

    assert header not in result
    assert "ข้าว เพลี้ยกระโดด 20 กรัม" in result

def test_keeps_identical_line_in_middle_of_page():
    header = "บทที่ 3 สารกำจัดแมลง"
    text = "\n".join([
        "สารบัญ",
        "บทที่ 1 บทนำ",
        "บทที่ 2 โรคพืช",
        header,                          
        "บทที่ 4 วัชพืช",
        "บทที่ 5 ภาคผนวก",
        "กรมวิชาการเกษตร",
    ])
    running = frozenset({_running_key(header)})

    result = strip_running_lines(text, running)

    assert header in result

def test_returns_text_unchanged_when_nothing_matches():
    text = "บรรทัดหนึ่ง\nบรรทัดสอง\nบรรทัดสาม"
    assert strip_running_lines(text, frozenset()) == text

def test_restores_mapped_pua_to_thai():
    given = "น" + "\uf701" + "ำ"
    expected = "น" + "\u0e48" + "ำ"

    assert restore_pua_tone_marks(given) == expected

def test_leaves_normal_thai_untouched():
    given = "ไดโนทีฟูแรน 10% WP อัตรา 20 กรัม"
    assert restore_pua_tone_marks(given) == given

def test_raises_on_unmapped_pua():
    # Picked from the range at run time, not hardcoded: F706 used to be the
    # example here and then became mapped, which would turn this test green
    # for the wrong reason.
    unmapped = next(
        chr(c) for c in range(0xF700, 0xF71B) if chr(c) not in PUA_TO_THAI
    )
    with pytest.raises(ValueError, match=rf"U\+{ord(unmapped):04X}"):
        restore_pua_tone_marks("ก" + unmapped)

def test_nfc_composes_latin_combining_marks():
    given = "Nicotiana tabacum var. Bre" + "\u0301" + "sil"   # e + acute
    expected = "Nicotiana tabacum var. Bré" + "sil"
    assert to_nfc(given) == expected

def test_nfc_leaves_thai_combining_marks_alone():
    given = "น" + "\u0e48" + "\u0e33"
    assert to_nfc(given) == given

def test_pua_repair_must_run_before_nfc():
    given = "น" + "\uf718" + "\uf71a"

    correct_order = to_nfc(restore_pua_tone_marks(given))
    swapped_order = restore_pua_tone_marks(to_nfc(given))

    assert correct_order != swapped_order
    assert correct_order == unicodedata.normalize("NFC", correct_order)