import hashlib
from dataclasses import dataclass

from ingest.chunk import chunk_document
from ingest.db import (
    connect,
    delete_chunks_for_document,
    find_document_by_hash,
    insert_chunks,
    insert_pending_document,
    update_chunk_stats,
    update_qa,
)
from ingest.embed import EMBEDDING_MODEL, embed_texts
from ingest.extract import extract_blocks, extract_pages, extract_tables, page_count
from ingest.normalize import detect_running_lines
from ingest.promote import promote_new_document
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
    """Extract, gate, and -- only if the gate passes -- chunk and embed.

    Returns the qa record either way. The caller checks failed_checks(qa) to
    know whether anything beyond a document row was written.
    """
    pages = extract_pages(pdf_path)
    pages_in_pdf = page_count(pdf_path)
    # Locating tables costs 24-40 seconds on a full volume. It happens here
    # rather than in extract_pages() so that anything only wanting text does
    # not pay for it, and chunk_document() wants this same list next.
    tables = extract_tables(pdf_path)
    qa = qa_gate(
        pages, pdf_page_count=pages_in_pdf, scopes=meta.scopes, tables=tables
    )
    digest = file_hash256(pdf_path)
    existing = find_document_by_hash(conn, digest)

    with conn.transaction():
        if existing is None:
            document_id = insert_pending_document(
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
            document_id = existing.document_id
            update_qa(conn, document_id, qa)
            # A prior passing attempt may have left chunks here. Clear them
            # unconditionally, before the failed_checks() gate below: if this
            # run regresses to failing, invariant 9 requires zero chunks, and
            # if it still passes, re-inserting without this collides with
            # chunk_dedup_idx on the previous attempt's rows. Not archival --
            # see delete_chunks_for_document()'s docstring.
            delete_chunks_for_document(conn, document_id)
        else:
            raise AlreadyIngested(
                f"{meta.source} is already ingested with status "
                f"{existing.status!r}"
            )

        # Invariant 9: a document that fails a check is not chunked or
        # embedded. The row above stays 'pending' with the full qa record.
        # There is no bypass flag -- this is the only way past this line.
        if failed_checks(qa):
            return qa

        running_lines = detect_running_lines(pages)
        blocks = extract_blocks(pdf_path)
        chunks = chunk_document(blocks, tables, running_lines)
        embeddings = embed_texts([chunk.content for chunk in chunks])
        chunk_count = insert_chunks(conn, document_id, chunks, embeddings)
        update_chunk_stats(conn, document_id, chunk_count, EMBEDDING_MODEL)
        # There is no separate review step: a document that reaches here has
        # passed the QA gate and been embedded, so it goes live immediately
        # and any older edition it fully supersedes is archived in the same
        # transaction. See promote_new_document()'s docstring for ordering.
        promote_new_document(conn, document_id)

    return qa
