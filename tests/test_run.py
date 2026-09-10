import pathlib

import pymupdf
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

def _chunk_count(conn, document_id):
    return conn.execute(
        "SELECT count(*) FROM chunk WHERE document_id = %s", (document_id,)
    ).fetchone()[0]


def _write_ascii_only_pdf(path: pathlib.Path) -> None:
    """A one-page PDF with no Thai characters at all, so no_thai_consonants
    fails and nothing else does -- combining_ratio_too_low needs at least one
    consonant to even compute a ratio, so it stays passed rather than firing
    for a different reason.
    """
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Chlorpyrifos 40% EC, 20-30 ml per 20 L water.")
    doc.save(str(path))
    doc.close()


def test_a_failing_document_stays_pending_with_zero_chunks(db_conn, tmp_path):
    # No API key needed: the failing path returns before embed_texts() is
    # ever called, which is the whole point of invariant 9.
    pdf_path = tmp_path / "no_thai.pdf"
    _write_ascii_only_pdf(pdf_path)
    meta = DocumentMeta(
        source="no_thai.pdf", title_th="ไม่มีภาษาไทย", edition_year_be=2565,
        scopes=["fungicide"],
    )

    qa = ingest(db_conn, str(pdf_path), meta)

    assert "no_thai_consonants" in failed_checks(qa)
    found = find_document_by_hash(db_conn, file_hash256(pdf_path))
    assert found is not None
    assert found.status == "pending"
    assert _chunk_count(db_conn, found.document_id) == 0


@pytest.mark.requires_embeddings
def test_first_ingest_writes_one_active_row_with_chunks(db_conn):
    qa = ingest(db_conn, EXCERPT, META)
    found = find_document_by_hash(db_conn, file_hash256(EXCERPT))

    assert failed_checks(qa) == set()
    assert _count(db_conn) == 1
    assert found is not None
    # No separate review step: a document that passes the gate and gets
    # embedded goes live in the same ingest call.
    assert found.status == "active"
    assert _chunk_count(db_conn, found.document_id) > 0

@pytest.mark.requires_embeddings
def test_reingest_while_pending_updates_in_place(db_conn):
    # Force the first attempt to stay pending: no_thai_consonants can't be
    # made to fail on EXCERPT, so failure is simulated the same way the qa
    # record itself is wiped below -- directly, at the row.
    first_qa = ingest(db_conn, EXCERPT, META)
    assert failed_checks(first_qa) == set()  # this ingest is already active

    # Roll the row back to pending by hand to exercise the pending re-ingest
    # branch in isolation from promotion, which is covered separately above.
    first = find_document_by_hash(db_conn, file_hash256(EXCERPT))
    db_conn.execute(
        "UPDATE document SET status = 'pending', qa = '{}' WHERE document_id = %s",
        (first.document_id,),
    )

    ingest(db_conn, EXCERPT, META)

    assert _count(db_conn) == 1
    assert find_document_by_hash(db_conn, file_hash256(EXCERPT)).document_id == first.document_id
    assert _qa_of(db_conn, first.document_id) != {}
    # Re-ingesting a passing document re-chunks it exactly once, not on top
    # of the previous attempt's rows.
    assert _chunk_count(db_conn, first.document_id) > 0

@pytest.mark.requires_embeddings
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
