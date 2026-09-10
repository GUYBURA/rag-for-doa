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
from pgvector.psycopg import register_vector
from psycopg.types.json import Jsonb
from pythainlp.tokenize import word_tokenize

from ingest.chunk import Chunk


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


def delete_chunks_for_document(conn: psycopg.Connection, document_id: uuid.UUID) -> None:
    """Remove every chunk row for one document.

    Not the archival mechanism in promote.py, which is the only place that
    deletes chunks to retire a *live* edition from search - see invariant 3.
    This document was never active, so nothing here is being retired. It
    exists for re-ingesting a still-pending document: called unconditionally
    before the caller checks the new qa, so a run that regresses to failing
    ends at zero chunks (what invariant 9 requires), and a run that still
    passes does not collide with chunk_dedup_idx on the previous attempt's
    rows.
    """
    with conn.cursor() as cur:
        cur.execute("DELETE FROM chunk WHERE document_id = %s", (document_id,))


def insert_chunks(
    conn: psycopg.Connection,
    document_id: uuid.UUID,
    chunks: list[Chunk],
    embeddings: list[list[float]],
) -> int:
    """Insert one chunk row per (chunk, embedding) pair. Returns the count.

    content_tsv is built here, not as a generated column, because Postgres has
    no Thai word-boundary parser: newmm segments the text in Python first, and
    the tokens are joined with spaces so to_tsvector('simple', ...) - which
    only lowercases and splits on whitespace, no English stemming - has word
    boundaries to work with.

    register_vector(conn) lets psycopg send a Python list straight into a
    vector column; without it, a list has no adapter to the pgvector type.
    """
    register_vector(conn)

    rows = []
    for chunk, embedding in zip(chunks, embeddings, strict=True):
        tokens = " ".join(word_tokenize(chunk.content, engine="newmm"))
        rows.append(
            (
                chunk.content,
                embedding,
                document_id,
                chunk.page_number,
                chunk.section,
                chunk.content_sha256,
                Jsonb(chunk.metadata),
                tokens,
            )
        )

    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO chunk (
                content, embedding, document_id, page_number, section,
                content_sha256, langchain_metadata, content_tsv
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, to_tsvector('simple', %s))
            """,
            rows,
        )
    return len(rows)


def update_chunk_stats(
    conn: psycopg.Connection,
    document_id: uuid.UUID,
    chunk_count: int,
    embedding_model: str,
) -> None:
    """Record how many chunks a document produced and which model embedded
    them. embedding_model is pinned here, not chosen at query time - see
    CLAUDE.md on why changing it means re-embedding, not an in-place swap.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE document
            SET    chunk_count     = %s,
                   embedding_model = %s
            WHERE  document_id = %s
            """,
            (chunk_count, embedding_model, document_id),
        )


def activate_document(conn: psycopg.Connection, document_id: uuid.UUID) -> None:
    """Flip one document to active. Only ingest/promote.py calls this."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE document SET status = 'active' WHERE document_id = %s",
            (document_id,),
        )


def run_promotion(conn: psycopg.Connection) -> None:
    """Invoke the schema's own supersession function.

    The archival DELETE FROM chunk lives inside promote_current_edition() in
    the schema, not in any Python module here - this just calls it. See
    CLAUDE.md invariant 3.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT promote_current_edition()")
