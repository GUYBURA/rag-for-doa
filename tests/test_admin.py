"""The admin upload surface: POST /admin/documents and its status poll.

Pure HTTP-shape tests. The ingester and the document lookup are stubs, so no
PDF is parsed, no embedding is bought and no database is touched -- what is
under test is admission: who gets in, what is rejected before any work
starts, and whether the work is handed off exactly once.

TestClient runs background tasks synchronously once the response is sent, so
a test can assert on what the background function did without waiting.
"""

import io
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.main as app_main
from app.main import (
    IngestSlot,
    app,
    get_admin_api_keys,
    get_api_keys,
    get_document_lookup,
    get_ingest_slot,
    get_ingester,
    get_object_store,
)
from app.storage import LocalObjectStore, document_name, new_upload_name
from ingest.db import DocumentSummary

ADMIN_KEY = "admin-key-0123456789abcdefghij"
ASK_KEY = "ask-key-0123456789abcdefghij"

TITLE = "คำแนะนำการใช้สารป้องกันกำจัดศัตรูพืช ฉบับปี 2568"
# Not ASCII filler: the form carries Thai through multipart encoding, and a
# title that round-trips in latin-1 proves nothing about the real one.
FORM = {
    "title_th": TITLE,
    "edition_year_be": "2568",
    "scopes": ["fungicide", "insecticide", "herbicide"],
}


def _pdf(size: int = 1024) -> dict:
    """A file part. The bytes are never parsed -- the ingester is a stub --
    so only the size and the filename matter here.
    """
    return {"file": ("2568.pdf", io.BytesIO(b"%PDF-1.7" + b"0" * size), "application/pdf")}


PASSING_QA = {"page_count": {"passed": True, "measured": 423, "threshold": None}}
FAILING_QA = {"page_count": {"passed": False, "measured": 0, "threshold": 423}}


class FakeIngester:
    """Records the call instead of running the pipeline.

    Returns a qa record because that is what ingest() returns and what
    decides whether the original is archived.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.raises: Exception | None = None
        self.qa: dict = PASSING_QA

    def __call__(self, pdf_path: str, meta) -> dict:
        self.calls.append((pdf_path, meta))
        if self.raises is not None:
            raise self.raises
        return self.qa


@pytest.fixture
def ingester():
    return FakeIngester()


@pytest.fixture
def lookup():
    """Hash -> DocumentSummary. Empty means nothing has been ingested."""
    return {}


@pytest.fixture
def slot():
    return IngestSlot()


@pytest.fixture
def store(tmp_path):
    """The real LocalObjectStore, not a mock: it is cheap, and the archive
    step is only meaningful if something actually lands on disk.
    """
    return LocalObjectStore(tmp_path)


@pytest.fixture
def client(ingester, lookup, slot, store):
    app.dependency_overrides[get_admin_api_keys] = lambda: frozenset({ADMIN_KEY})
    app.dependency_overrides[get_api_keys] = lambda: frozenset({ASK_KEY})
    app.dependency_overrides[get_ingester] = lambda: ingester
    app.dependency_overrides[get_document_lookup] = lambda: lookup.get
    app.dependency_overrides[get_ingest_slot] = lambda: slot
    app.dependency_overrides[get_object_store] = lambda: store
    # Not a context manager, for the reason test_app.py gives: that is what
    # runs the lifespan handler, which wants a database this test has not got.
    # Background tasks still run inside the request, so the handoff is
    # observable here.
    yield TestClient(app)
    app.dependency_overrides.clear()


def _post(client, **kwargs):
    headers = kwargs.pop("headers", {"X-Admin-Key": ADMIN_KEY})
    data = kwargs.pop("data", FORM)
    files = kwargs.pop("files", _pdf())
    return client.post("/admin/documents", headers=headers, data=data, files=files)


# ---------------------------------------------------------------------------
# Admission
# ---------------------------------------------------------------------------


def test_a_valid_upload_is_accepted_and_handed_off(client, ingester):
    response = _post(client)

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "accepted"
    assert len(body["file_hash"]) == 64
    assert len(ingester.calls) == 1


def test_the_metadata_reaches_ingest_exactly_as_the_form_gave_it(client, ingester):
    """Invariant 1: nothing infers title, edition or scopes from the PDF. The
    form is the only source, so what the uploader typed must arrive intact.
    """
    _post(client)

    _, meta = ingester.calls[0]
    assert meta.title_th == TITLE
    assert meta.edition_year_be == 2568
    assert meta.scopes == ["fungicide", "insecticide", "herbicide"]
    assert meta.source == "2568.pdf"


def test_an_upload_with_no_admin_key_is_refused(client, ingester):
    response = _post(client, headers={})

    assert response.status_code == 401
    assert ingester.calls == []


def test_an_ask_key_cannot_upload(client, ingester):
    """The two key sets are separate on purpose: a key handed to a public
    frontend must not be able to add a document.
    """
    response = _post(client, headers={"X-Admin-Key": ASK_KEY})

    assert response.status_code == 401
    assert ingester.calls == []


def test_an_admin_key_cannot_ask(client):
    response = client.post(
        "/ask", headers={"X-API-Key": ADMIN_KEY}, json={"question": "อะไร"}
    )

    assert response.status_code == 401


# ---------------------------------------------------------------------------
# Validation, before any work starts
# ---------------------------------------------------------------------------


def test_an_unknown_scope_is_refused(client, ingester):
    response = _post(client, data={**FORM, "scopes": ["fungicide", "rodenticide"]})

    assert response.status_code == 422
    assert "rodenticide" in response.json()["detail"]
    assert ingester.calls == []


def test_empty_scopes_are_refused(client, ingester):
    """The schema's CHECK (cardinality(scopes) > 0) would reject this too, but
    only inside a background task, where the failure leaves no trace the
    uploader can see -- and the row it would explain cannot be written either.
    """
    response = _post(client, data={**FORM, "scopes": []})

    assert response.status_code == 422
    assert ingester.calls == []


def test_a_blank_title_is_refused(client, ingester):
    response = _post(client, data={**FORM, "title_th": "  "})

    assert response.status_code == 422
    assert ingester.calls == []


def test_a_file_over_the_ceiling_is_refused(client, ingester, monkeypatch):
    """The ceiling is lowered rather than a 32MB body built: what is under
    test is that the limit is enforced while streaming, not the number.
    """
    monkeypatch.setattr(app_main, "MAX_UPLOAD_BYTES", 512)

    response = _post(client, files=_pdf(size=1024))

    assert response.status_code == 413
    assert ingester.calls == []


# ---------------------------------------------------------------------------
# Duplicates and concurrency
# ---------------------------------------------------------------------------


def test_an_already_active_file_is_a_conflict(client, ingester, lookup):
    first = _post(client)
    file_hash = first.json()["file_hash"]
    lookup[file_hash] = DocumentSummary(uuid.uuid4(), "active", {}, 423)

    again = _post(client)

    assert again.status_code == 409
    assert len(ingester.calls) == 1


def test_a_pending_file_may_be_re_uploaded(client, ingester, lookup):
    """A document that failed the gate is re-ingested by uploading it again --
    the recovery path ingest() already implements (invariant 3).
    """
    first = _post(client)
    file_hash = first.json()["file_hash"]
    lookup[file_hash] = DocumentSummary(uuid.uuid4(), "pending", {"x": 1}, None)

    again = _post(client)

    assert again.status_code == 202
    assert len(ingester.calls) == 2


def test_a_second_upload_while_one_runs_is_refused(client, ingester, slot):
    slot.try_acquire("a" * 64)

    response = _post(client)

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "60"
    assert ingester.calls == []


def test_the_slot_is_released_after_a_successful_ingest(client, slot):
    _post(client)

    assert slot.try_acquire("b" * 64) is True


def test_the_slot_is_released_when_ingest_raises(client, ingester, slot):
    """A held slot would lock out every later upload for the life of the
    process, so the release has to survive the failure that makes it matter.
    """
    ingester.raises = RuntimeError("embedding provider is down")

    response = _post(client)

    assert response.status_code == 202
    assert slot.try_acquire("b" * 64) is True


def test_the_temp_file_is_removed_after_ingest(client, ingester):
    import os

    _post(client)

    path, _ = ingester.calls[0]
    assert not os.path.exists(path)


# ---------------------------------------------------------------------------
# Status poll
# ---------------------------------------------------------------------------


def test_status_reports_the_qa_record_whole(client, lookup):
    file_hash = "c" * 64
    qa = {"page_count": {"ok": True}, "combining_marks": {"ok": False, "ratio": 0.04}}
    lookup[file_hash] = DocumentSummary(uuid.uuid4(), "pending", qa, None)

    response = client.get(
        f"/admin/documents/{file_hash}", headers={"X-Admin-Key": ADMIN_KEY}
    )

    assert response.status_code == 200
    assert response.json()["qa"] == qa
    assert response.json()["status"] == "pending"


def test_status_says_in_progress_before_a_row_exists(client, slot):
    """Extraction runs before qa_gate can produce a row, so for the first
    minute of a large volume there is nothing to look up.
    """
    file_hash = "d" * 64
    slot.try_acquire(file_hash)

    response = client.get(
        f"/admin/documents/{file_hash}", headers={"X-Admin-Key": ADMIN_KEY}
    )

    assert response.status_code == 200
    assert response.json()["status"] == "in_progress"


def test_status_for_an_unknown_hash_is_404(client):
    response = client.get(
        f"/admin/documents/{'e' * 64}", headers={"X-Admin-Key": ADMIN_KEY}
    )

    assert response.status_code == 404


def test_status_needs_the_admin_key(client, lookup):
    file_hash = "f" * 64
    lookup[file_hash] = DocumentSummary(uuid.uuid4(), "active", {}, 423)

    response = client.get(f"/admin/documents/{file_hash}")

    assert response.status_code == 401


# ---------------------------------------------------------------------------
# The env wiring itself. Every test above overrides get_admin_api_keys, so
# none of them notices if it reads the wrong variable -- a mutation pointing
# it at API_KEYS survived the whole file until this test existed.
# ---------------------------------------------------------------------------


def test_the_admin_keys_come_from_their_own_environment_variable(monkeypatch):
    monkeypatch.setenv("API_KEYS", ASK_KEY)
    monkeypatch.setenv("ADMIN_API_KEYS", ADMIN_KEY)

    assert app_main.get_admin_api_keys() == frozenset({ADMIN_KEY})
    assert app_main.get_api_keys() == frozenset({ASK_KEY})


# ---------------------------------------------------------------------------
# The signed-URL route: POST /admin/uploads, then POST /admin/documents with
# an object name instead of a body.
# ---------------------------------------------------------------------------


def _put_object(store, content: bytes = b"%PDF-1.7 big") -> str:
    """Stand in for the browser's PUT to the signed URL."""
    name = new_upload_name()
    path = store._path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return name


def _post_object(client, object_name, **overrides):
    data = {**FORM, "object_name": object_name, "source": "2568.pdf", **overrides}
    return client.post(
        "/admin/documents", headers={"X-Admin-Key": ADMIN_KEY}, data=data
    )


def test_an_upload_url_is_issued_for_the_staging_prefix(client):
    response = client.post("/admin/uploads", headers={"X-Admin-Key": ADMIN_KEY})

    assert response.status_code == 201
    body = response.json()
    assert body["object_name"].startswith("uploads/")
    assert body["object_name"] in body["url"]
    assert body["expires_in"] > 0


def test_an_upload_url_needs_the_admin_key(client):
    assert client.post("/admin/uploads").status_code == 401


def test_two_upload_urls_name_different_objects(client):
    headers = {"X-Admin-Key": ADMIN_KEY}
    first = client.post("/admin/uploads", headers=headers).json()
    second = client.post("/admin/uploads", headers=headers).json()

    assert first["object_name"] != second["object_name"]


def test_an_uploaded_object_is_ingested(client, ingester, store):
    name = _put_object(store)

    response = _post_object(client, name)

    assert response.status_code == 202
    assert len(ingester.calls) == 1
    _, meta = ingester.calls[0]
    assert meta.source == "2568.pdf"


def test_the_object_bytes_reach_the_pipeline(client, ingester, store):
    """The temp file handed to ingest() must be the object's content, not an
    empty placeholder -- the download is the only thing that puts it there.
    """
    recorded = {}

    def capture(pdf_path, meta):
        recorded["bytes"] = Path(pdf_path).read_bytes()
        return PASSING_QA

    name = _put_object(store, b"%PDF-1.7 distinctive")
    app.dependency_overrides[get_ingester] = lambda: capture

    _post_object(client, name)

    assert recorded["bytes"] == b"%PDF-1.7 distinctive"


def test_sending_both_a_file_and_an_object_name_is_refused(client, ingester, store):
    name = _put_object(store)

    response = client.post(
        "/admin/documents",
        headers={"X-Admin-Key": ADMIN_KEY},
        data={**FORM, "object_name": name, "source": "2568.pdf"},
        files=_pdf(),
    )

    assert response.status_code == 422
    assert ingester.calls == []


def test_sending_neither_is_refused(client, ingester):
    response = client.post(
        "/admin/documents", headers={"X-Admin-Key": ADMIN_KEY}, data=FORM
    )

    assert response.status_code == 422
    assert ingester.calls == []


def test_a_document_path_cannot_be_claimed_as_an_upload(client, ingester, store):
    """The attack the staging prefix exists to stop: naming the permanent
    object of a live edition and having the service ingest it -- and then
    overwrite it as its own source.
    """
    permanent = document_name("a" * 64)
    path = store._path(permanent)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"%PDF-1.7 the real 2568")

    response = _post_object(client, permanent)

    assert response.status_code == 422
    assert ingester.calls == []


def test_an_object_that_does_not_exist_is_404(client, ingester):
    response = _post_object(client, new_upload_name())

    assert response.status_code == 404
    assert ingester.calls == []


def test_an_object_over_the_ceiling_is_refused_without_downloading(
    client, ingester, store, monkeypatch
):
    monkeypatch.setattr(app_main, "MAX_OBJECT_BYTES", 8)
    name = _put_object(store, b"%PDF-1.7 more than eight bytes")

    response = _post_object(client, name)

    assert response.status_code == 413
    assert ingester.calls == []


def test_an_object_name_without_a_source_is_refused(client, ingester, store):
    """document.source is display and half of the uniqueness key; a uuid
    there would make two editions of one volume look unrelated.
    """
    name = _put_object(store)

    response = _post_object(client, name, source="")

    assert response.status_code == 422
    assert ingester.calls == []


# ---------------------------------------------------------------------------
# Archiving the original
# ---------------------------------------------------------------------------


def test_a_passing_ingest_archives_the_object(client, store):
    name = _put_object(store, b"%PDF-1.7 keep me")

    file_hash = _post_object(client, name).json()["file_hash"]

    archived = store._path(document_name(file_hash))
    assert archived.read_bytes() == b"%PDF-1.7 keep me"
    # Never a move: the lifecycle rule owns the staging prefix.
    assert store._path(name).exists()


def test_a_passing_multipart_ingest_archives_the_body(client, store):
    """Small volumes get an original in the bucket too. invariant 3 draws no
    line between the two routes.
    """
    file_hash = _post(client).json()["file_hash"]

    assert store._path(document_name(file_hash)).is_file()


def test_a_failing_ingest_archives_nothing(client, ingester, store):
    ingester.qa = FAILING_QA

    file_hash = _post(client).json()["file_hash"]

    assert not store._path(document_name(file_hash)).exists()


def test_a_raising_ingest_archives_nothing(client, ingester, store):
    ingester.raises = RuntimeError("embedding provider is down")

    file_hash = _post(client).json()["file_hash"]

    assert not store._path(document_name(file_hash)).exists()
