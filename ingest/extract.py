"""PDF text extraction. PyMuPDF only, raw text out, no cleaning.

Tables are geometry from PyMuPDF and text from nowhere else. find_tables() is
used to locate cells; every character still comes out of get_text(), the
extractor invariant 7 already trusts. Its own text methods are not safe for
Thai - see extract_tables().
"""

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


@dataclass(frozen=True)
class Block:
    """One text block with its position on the page.

    Page.raw_text is the same text with the geometry thrown away. chunk.py
    needs the geometry to tell which text sits inside a table and must not be
    chunked twice, and only this module is allowed to touch PyMuPDF.
    """

    page_number: int
    bbox: tuple[float, float, float, float]
    text: str


@dataclass(frozen=True)
class Table:
    """One table PyMuPDF located, with its text read back out by get_text().

    Not attached to Page: locating tables costs 24-37 seconds per volume, and
    qa_gate and normalize never look at them, so a caller that only wants text
    must not pay for it. page_number is what joins the two back together.

    cells holds the text exactly as it came out of the page, newlines and all.
    markdown is derived from it, and is the only field that has been altered:
    the format cannot carry a newline or a bare pipe inside a cell.
    """

    page_number: int  # 1-based, matches Page.page_number
    bbox: tuple[float, float, float, float]
    row_count: int
    col_count: int
    cells: tuple[tuple[str, ...], ...]
    markdown: str


def _cell_text(page: pymupdf.Page, rect) -> str:
    """Read one cell through the page's own text extractor.

    Never table.extract(), to_markdown() or header.names. Those order spans by
    vertical position, and a Thai below-vowel sits lower than the consonant it
    belongs to, so they come back as separate lines: 2568 page 56 yields
    'ศัตรพชื\\nู' where this function yields 'ศัตรูพืช'. Measured on the same
    table, the combining-mark ratio is 0.0432 from to_markdown() and 0.3267
    from get_text(). That is invariant 7's failure mode, which is why
    pdfplumber is banned - the tool is different, the damage is the same.
    """
    if rect is None:
        return ""
    return page.get_text("text", clip=pymupdf.Rect(*rect))


def _to_markdown(cells: tuple[tuple[str, ...], ...]) -> str:
    """Render extracted cells as a GitHub-flavoured markdown table.

    The first row becomes the header because the format requires one, not
    because the table necessarily has one.

    Two mechanical cleanups, both driven by what PyMuPDF actually returns for
    these handbooks. Page 56 of 2568 comes back as 11 columns of which four
    are empty in every row, because a merged cell reports its text once and
    leaves the rest of the span blank, and because the ruling lines include
    narrow spacer columns that hold nothing:

      - a column empty in every row is dropped, and a row empty in every
        column with it. What they contribute is "||||||||" -- tokens the
        model pays for and structure a reader has to see past.
      - a newline inside a cell becomes a space, not "<br>". The breaks are
        where the text wrapped in the printed table, not part of the data,
        and rendering them literally splits terms that belong together:
        "Phakopsora<br>pachyrhizi" is not a substring match for the Latin
        binomial anyone would search or cite.

    Neither cleanup drops a character of cell text or makes a judgement about
    what a row means -- `cells` remains the unedited record, and this is a
    rendering of it. Deciding which rows are header, in a table whose header
    spans several physical rows, would be exactly the judgement call this
    module does not make.
    """
    if not cells:
        return ""

    def clean(text: str) -> str:
        return " ".join(text.replace("|", "\\|").split())

    cleaned = [[clean(value) for value in row] for row in cells]
    width = max(len(row) for row in cleaned)
    keep = [i for i in range(width) if any(i < len(row) and row[i] for row in cleaned)]
    kept = [[row[i] if i < len(row) else "" for i in keep] for row in cleaned]
    kept = [row for row in kept if any(row)]
    if not kept:
        return ""

    def line(values: list[str]) -> str:
        return "|" + "|".join(values) + "|"

    header, *body = kept
    separator = "|" + "|".join("---" for _ in header) + "|"
    return "\n".join([line(header), separator, *(line(r) for r in body)])


def extract_tables(pdf_path: str) -> list[Table]:
    """Every table PyMuPDF finds, in page order.

    Nothing is filtered. 49 of the 276 tables in 2568 are two rows or three
    columns and are almost certainly not tables at all, but deciding that is a
    judgement call, and this module does not make judgement calls - a noise
    table that reaches document.qa is visible, one dropped here is not.
    """
    tables: list[Table] = []
    with pymupdf.open(pdf_path) as doc:
        for page in doc:
            for found in page.find_tables().tables:
                cells = tuple(
                    tuple(
                        _cell_text(page, rect)
                        for rect in list(row.cells)
                        + [None] * (found.col_count - len(row.cells))
                    )
                    for row in found.rows
                )
                tables.append(
                    Table(
                        page_number=page.number + 1,
                        bbox=tuple(found.bbox),
                        row_count=found.row_count,
                        col_count=found.col_count,
                        cells=cells,
                        markdown=_to_markdown(cells),
                    )
                )
    return tables


def extract_blocks(pdf_path: str) -> list[Block]:
    """Every text block with its rectangle, in page order.

    Cheap: no table detection is involved, so this costs about what
    extract_pages() costs.
    """
    with pymupdf.open(pdf_path) as doc:
        return [
            Block(page_number=page.number + 1, bbox=tuple(block[:4]), text=block[4])
            for page in doc
            for block in page.get_text("blocks")
        ]


def page_count(pdf_path: str) -> int:
    """Page count straight from the PDF, for the QA gate's cross-check."""
    with pymupdf.open(pdf_path) as doc:
        return doc.page_count
