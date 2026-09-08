import pytest
from ingest.normalize import collapse_whitespace, strip_page_furniture

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