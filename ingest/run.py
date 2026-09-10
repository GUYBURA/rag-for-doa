import hashlib
from dataclasses import dataclass
from ingest.db import connect, find_document_by_hash, insert_pending_document, update_qa
from ingest.extract import extract_pages, page_count
from ingest.qa_gate import failed_checks, qa_gate

PARSER = "pymupdf"


class AlreadyIngested(RuntimeError):
    """This file is in the database with a status that forbids re-ingestion.

    Its own type rather than ValueError: the admin UI will need to tell
    "already live" apart from "archived, and putting it back would serve
    superseded guidance" without parsing a message string.
    """

@dataclass(frozen=True)
class DocumentMeta:
    source: str
    title_th: str
    edition_year_be: int
    scopes: list[str]

def file_hash256(path) -> str:
    with open(path, "rb") as f:
        digest = hashlib.file_digest(f, "sha256")
    file_hash = digest.hexdigest()
    return file_hash

def ingest(conn, pdf_path: str, meta: DocumentMeta) -> dict:
    """คืน qa ทั้งก้อน ผู้เรียกดูเองว่าผ่านไหมด้วย failed_checks()"""
    pages = extract_pages(pdf_path)
    pages_in_pdf = page_count(pdf_path)
    qa = qa_gate(pages, pdf_page_count=pages_in_pdf, scopes=meta.scopes)
    digest = file_hash256(pdf_path)
    existing = find_document_by_hash(conn, digest)

    with conn.transaction():
        if existing is None:
            insert_pending_document(
                conn,
                source=meta.source,
                title_th=meta.title_th,
                edition_year_be=meta.edition_year_be,
                scopes=meta.scopes,
                file_hash=digest,
                page_count=pages_in_pdf,
                parser=PARSER,
                qa=qa,
            )
        elif existing.status == "pending":
            update_qa(conn, existing.document_id, qa)
        else:
            raise AlreadyIngested(
                f"{meta.source} is already ingested with status "
                f"{existing.status!r}"
            )
    return qa