"""PDF text extraction. PyMuPDF only, raw text out, no cleaning."""

from dataclasses import dataclass

import pymupdf


@dataclass(frozen=True)
class Page:
    """One PDF page, exactly as PyMuPDF read it.

    A plain dataclass rather than langchain_core.Document: LangChain owns
    retrieval only, and dragging its types through ingestion makes a version
    bump able to break the pipeline.
    """

    page_number: int  # 1-based, matches what a citation must print
    raw_text: str


def extract_pages(pdf_path: str) -> list[Page]:
    """Read every page of a PDF as raw text.

    No stripping, no whitespace collapsing, no unicode work — that all belongs
    to normalize.py. The QA gate needs to see the damage before anyone repairs
    it, so this stage must not repair anything.
    """
    with pymupdf.open(pdf_path) as doc:
        return [
            Page(page_number=page.number + 1, raw_text=page.get_text("text"))
            for page in doc
        ]


def page_count(pdf_path: str) -> int:
    """Page count straight from the PDF, for the QA gate's cross-check."""
    with pymupdf.open(pdf_path) as doc:
        return doc.page_count
