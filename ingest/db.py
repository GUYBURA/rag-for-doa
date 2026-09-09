"""psycopg writes for the ingest path. Every direct SQL statement lives here.

LangChain owns retrieval only, so nothing in this module goes through it.

None of these functions commit. The caller owns the transaction boundary
(`with conn.transaction():`), because a document row and the chunks it claims
to have must land together or not at all - a committed document row whose
chunk insert failed halfway is a row that lies about chunk_count.
"""

import os
import uuid
from typing import NamedTuple

import psycopg
from psycopg.types.json import Jsonb


class ExistingDocument(NamedTuple):
    """What a caller needs to decide whether re-ingestion is allowed.

    A NamedTuple rather than the raw row: `existing.status` says what the
    comparison is about, `existing[1]` does not, and the caller's decision here
    is the difference between updating a failed ingest and resurrecting
    superseded guidance.
    """

    document_id: uuid.UUID
    status: str


def connect() -> psycopg.Connection:
    return psycopg.connect(os.environ["DATABASE_URL"])


def find_document_by_hash(
    conn: psycopg.Connection, file_hash: str
) -> ExistingDocument | None:
    """The existing row for this file, or None.

    Status is what the caller decides on: a pending row may be re-ingested,
    an active or archived one may not.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT document_id, status
            FROM   document
            WHERE  file_hash = %s
            """,
            (file_hash,),
        )
        row = cur.fetchone()
    return ExistingDocument(*row) if row else None


def insert_pending_document(
    conn: psycopg.Connection,
    *,
    source: str,
    title_th: str,
    edition_year_be: int,
    scopes: list[str],
    file_hash: str,
    page_count: int,
    parser: str,
    qa: dict,
) -> uuid.UUID:
    """Insert a document row. status defaults to 'pending' in the schema.

    Nothing here sets status. Invariant 9 leaves a failed document pending,
    and promotion to active or archived belongs to promote.py.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO document (
                source, title_th, edition_year_be, scopes,
                file_hash, page_count, parser, qa
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING document_id
            """,
            (
                source,
                title_th,
                edition_year_be,
                list(scopes),
                file_hash,
                page_count,
                parser,
                Jsonb(qa),
            ),
        )
        return cur.fetchone()[0]


def update_qa(conn: psycopg.Connection, document_id: uuid.UUID, qa: dict) -> None:
    """Overwrite qa for a re-ingested pending document.

    Jsonb() is required: psycopg does not adapt a bare dict to jsonb.
    """
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE document SET qa = %s WHERE document_id = %s",
            (Jsonb(qa), document_id),
        )
