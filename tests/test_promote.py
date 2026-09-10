"""promote_new_document() end to end against a real Postgres, with synthetic
documents and chunks -- no source PDF and no embedding API needed. What is
under test is the SQL supersession logic and the order promote.py calls it
in, not the ingest pipeline that normally feeds it.
"""

from ingest.chunk import Chunk
from ingest.db import insert_chunks, insert_pending_document
from ingest.promote import promote_new_document

PASSING_QA = {"no_thai_consonants": {"passed": True, "measured": 5, "threshold": None}}

FAKE_VECTOR = [0.0] * 768


def _chunk(content: str) -> Chunk:
    import hashlib

    return Chunk(
        content=content,
        content_sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
        page_number=1,
        section=None,
        kind="prose",
        metadata={},
    )


def _insert_document(conn, *, source, edition_year_be, scopes, file_hash, status):
    document_id = insert_pending_document(
        conn,
        source=source,
        title_th=source,
        edition_year_be=edition_year_be,
        scopes=scopes,
        file_hash=file_hash,
        page_count=1,
        parser="pymupdf",
        qa=PASSING_QA,
    )
    if status != "pending":
        conn.execute(
            "UPDATE document SET status = %s WHERE document_id = %s",
            (status, document_id),
        )
    return document_id


def _chunk_count(conn, document_id):
    return conn.execute(
        "SELECT count(*) FROM chunk WHERE document_id = %s", (document_id,)
    ).fetchone()[0]


def test_promoting_marks_the_document_active(db_conn):
    document_id = _insert_document(
        db_conn, source="a.pdf", edition_year_be=2568, scopes=["fungicide"],
        file_hash="a" * 64, status="pending",
    )

    promote_new_document(db_conn, document_id)

    status = db_conn.execute(
        "SELECT status FROM document WHERE document_id = %s", (document_id,)
    ).fetchone()[0]
    assert status == "active"


def test_promoting_a_newer_edition_archives_one_it_fully_covers(db_conn):
    old_id = _insert_document(
        db_conn, source="old.pdf", edition_year_be=2565, scopes=["fungicide"],
        file_hash="b" * 64, status="active",
    )
    insert_chunks(db_conn, old_id, [_chunk("old content")], [FAKE_VECTOR])

    new_id = _insert_document(
        db_conn, source="new.pdf", edition_year_be=2568, scopes=["fungicide"],
        file_hash="c" * 64, status="pending",
    )

    promote_new_document(db_conn, new_id)

    old_status = db_conn.execute(
        "SELECT status FROM document WHERE document_id = %s", (old_id,)
    ).fetchone()[0]
    assert old_status == "archived"
    assert _chunk_count(db_conn, old_id) == 0
    assert db_conn.execute("SELECT count(*) FROM assert_no_stale_chunks").fetchone()[0] == 0


def test_promoting_does_not_archive_a_document_only_partly_covered(db_conn):
    # The old compendium covers two scopes; the new document covers only one
    # of them. Invariant 2 is union coverage across newer editions, and this
    # test proves a single new document is not treated as a full replacement
    # on its own.
    old_id = _insert_document(
        db_conn, source="old.pdf", edition_year_be=2565,
        scopes=["fungicide", "insecticide"], file_hash="d" * 64, status="active",
    )
    insert_chunks(db_conn, old_id, [_chunk("old content")], [FAKE_VECTOR])

    new_id = _insert_document(
        db_conn, source="new.pdf", edition_year_be=2568, scopes=["fungicide"],
        file_hash="e" * 64, status="pending",
    )

    promote_new_document(db_conn, new_id)

    old_status = db_conn.execute(
        "SELECT status FROM document WHERE document_id = %s", (old_id,)
    ).fetchone()[0]
    assert old_status == "active"
    assert _chunk_count(db_conn, old_id) == 1


def test_ordering_lets_the_new_document_count_toward_its_own_coverage_check(db_conn):
    # promote_current_edition()'s coverage check only looks at active/archived
    # documents. If activate_document() ran after run_promotion() instead of
    # before, the new document would not exist as a candidate coverer on this
    # call, and nothing else ever promotes it -- the old document would never
    # be archived at all.
    old_id = _insert_document(
        db_conn, source="old.pdf", edition_year_be=2565, scopes=["herbicide"],
        file_hash="f" * 64, status="active",
    )

    new_id = _insert_document(
        db_conn, source="new.pdf", edition_year_be=2568, scopes=["herbicide"],
        file_hash="g" * 64, status="pending",
    )

    promote_new_document(db_conn, new_id)

    statuses = dict(
        db_conn.execute(
            "SELECT source, status FROM document WHERE document_id IN (%s, %s)",
            (old_id, new_id),
        ).fetchall()
    )
    assert statuses == {"old.pdf": "archived", "new.pdf": "active"}
