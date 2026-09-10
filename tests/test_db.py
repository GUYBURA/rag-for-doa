from ingest.db import (
    ExistingDocument,
    find_document_by_hash,
    insert_pending_document,
    update_qa
)

FAILING_QA = {
    "unmapped_pua_codepoints": {"passed": False, "measured": ["U+F707"], "threshold": None},
    "combining_ratio_too_low": {"passed": True, "measured": 0.3314, "threshold": 0.2},
}

PASSING_QA = {
    "unmapped_pua_codepoints": {"passed": True, "measured": [], "threshold": None},
}

def _insert(conn, **overrides):
    """One valid document; each test overrides only what it is about."""
    kwargs = dict(
        source="2565.pdf", title_th="เอกสารทดสอบ", edition_year_be=2565,
        scopes=["fungicide"], file_hash="a" * 64, page_count=300,
        parser="pymupdf", qa=FAILING_QA,
    )
    return insert_pending_document(conn, **{**kwargs, **overrides})

def test_a_failed_document_lands_pending_with_its_reason(db_conn):
    document_id = _insert(db_conn)

    status, qa = db_conn.execute(
        "SELECT status, qa FROM document WHERE document_id = %s", (document_id,)
    ).fetchone()

    assert status == "pending"
    # The whole record, not one key: invariant 9 wants the reasons readable
    # later, and comparing a single field would not notice a nested dict that
    # arrived flattened or a check that went missing on the way through Jsonb().
    assert qa == FAILING_QA

def test_find_by_hash_returns_none_when_absent(db_conn):
    assert find_document_by_hash(db_conn, "b" * 64) is None

def test_find_by_hash_returns_id_and_status(db_conn):
    document_id = _insert(db_conn)
    found = find_document_by_hash(db_conn, "a" * 64)
    assert found == ExistingDocument(document_id, "pending")

def test_update_qa_replaces_the_record_without_touching_the_row(db_conn):
    document_id = _insert(db_conn)
    before = db_conn.execute(
        "SELECT ingested_at, status FROM document WHERE document_id = %s",
        (document_id,),
    ).fetchone()

    update_qa(db_conn, document_id, PASSING_QA)

    qa, *after = db_conn.execute(
        "SELECT qa, ingested_at, status FROM document WHERE document_id = %s",
        (document_id,),
    ).fetchone()

    # Replaced, not merged: the old failure must not survive as a stale key.
    assert qa == PASSING_QA
    # ingested_at defaults to now(), so a delete-and-reinsert or a rewrite of
    # the whole row would move it. It not moving is the evidence that update_qa
    # touched one column.
    assert tuple(after) == before

def test_nothing_is_committed_by_db_py(db_conn):
    """A caller that rolls back must leave no row behind.

    This is the test that locks the module docstring's promise. Add a
    conn.commit() anywhere in db.py and it goes red.
    """
    _insert(db_conn)
    db_conn.rollback()
    assert db_conn.execute("SELECT count(*) FROM document").fetchone()[0] == 0
