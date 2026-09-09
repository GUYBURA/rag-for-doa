"""Rebuild tests/fixtures/excerpt.pdf from the source handbooks.

The fixture is three real pages, not a synthetic PDF: the point of testing
extract.py is that PyMuPDF handles the Department of Agriculture's actual
files, and synthetic pages made with PyMuPDF's own writer would only prove it
can read itself. Slicing preserves per-page text byte for byte (verified: the
extracted text of every page equals the corresponding page of the source).

Pages, and the trap each one carries:
  1  2568 p10   Thai prose, ~2,900 chars
  2  2568 p55   section divider, 37 chars, starts with a space
                -> catches an extract.py that strips or trims
  3  2565 p220  193 PUA tone marks, the most of any page in any edition
                -> catches an extract.py that repairs text before qa_gate sees it

Deliberately excluded: 2568 p299 (no text, 2,579 vector drawings) and p300
(image-only back cover). Either one takes the fixture from 116 KB to ~900 KB,
and neither exercises anything in extract.py — a page with no text yields an
empty string with no code path of its own. Blank-page logic is qa_gate's, and
its tests build Page objects in memory.

Source PDFs are gitignored, so this script only runs where they are present.
Run it from the repo root: python tests/fixtures/make_fixture.py
"""

import pathlib

import pymupdf

# (source, 0-based page index) — the page numbers in the docstring are 1-based.
PAGES = [
    ("data/raw/2568.pdf", 9),
    ("data/raw/2568.pdf", 54),
    ("data/raw/2565.pdf", 219),
]
OUT = pathlib.Path(__file__).parent / "excerpt.pdf"


def main() -> None:
    doc = pymupdf.open()
    for path, index in PAGES:
        with pymupdf.open(path) as src:
            doc.insert_pdf(src, from_page=index, to_page=index)
    doc.subset_fonts()  # 96 KB -> 61 KB on the prose page alone
    doc.save(OUT, garbage=4, deflate=True)
    doc.close()
    print(f"wrote {OUT} ({OUT.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
