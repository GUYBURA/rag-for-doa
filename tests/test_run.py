import pathlib
import pytest
from ingest.db import ExistingDocument, find_document_by_hash
from ingest.qa_gate import failed_checks
from ingest.run import AlreadyIngested, DocumentMeta, ingest, file_hash256

EXCERPT = pathlib.Path(__file__).resolve().parent / "fixtures" / "excerpt.pdf"

META = DocumentMeta(
    source="excerpt.pdf", title_th="เอกสารทดสอบ", edition_year_be=2565, scopes=["fungicide"],
)

def _count(conn):
    return conn.execute("SELECT count(*) FROM document").fetchone()[0]

def _qa_of(conn, document_id):
    return conn.execute(
        "SELECT qa FROM document WHERE document_id = %s", (document_id,)
    ).fetchone()[0]

def test_first_ingest_writes_one_pending_row(db_conn):
    qa = ingest(db_conn, EXCERPT, META)
    found =  find_document_by_hash(db_conn, file_hash256(EXCERPT))
    assert failed_checks(qa) == set()
    assert _count(db_conn) == 1
    assert found is not None
    assert found.status == "pending"

def test_reingest_while_pending_updates_in_place(db_conn):
    ingest(db_conn, EXCERPT, META)
    first = find_document_by_hash(db_conn, file_hash256(EXCERPT))
    db_conn.execute(
        "UPDATE document SET qa = '{}' WHERE document_id = %s", (first.document_id,)
    )

    ingest(db_conn, EXCERPT, META)

    assert _count(db_conn) == 1
    assert find_document_by_hash(db_conn, file_hash256(EXCERPT)).document_id == first.document_id
    assert _qa_of(db_conn, first.document_id) != {}

@pytest.mark.parametrize("status", ["active", "archived"])
def test_reingest_while_not_pending_is_refused(db_conn, status):
    ingest(db_conn, EXCERPT, META)
    existing = find_document_by_hash(db_conn, file_hash256(EXCERPT))
    db_conn.execute(
        "UPDATE document SET status = %s WHERE document_id = %s", (status, existing.document_id)
    )

    with pytest.raises(AlreadyIngested):
        ingest(db_conn, EXCERPT, META)

    assert _count(db_conn) == 1
    assert find_document_by_hash(db_conn, file_hash256(EXCERPT)) == ExistingDocument(
        existing.document_id, status
    )
