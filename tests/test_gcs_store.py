"""GcsObjectStore against fake-gcs-server, plus the one thing it cannot test.

Same reasoning as the Postgres tests: the guarantees here are about how a
real client behaves against a real API, and a mocked google.cloud.storage
would only prove that the mock was called. fake-gcs-server speaks the same
HTTP API, so size(), download_to(), upload_from() and copy() are exercised
for real.

signed_upload_url() is the exception and is tested separately below. The
emulator does not verify signatures, and a signature is not ours to verify
anyway -- what is ours is the parameters: v4, PUT, the configured TTL, the
right object, and signing through the IAM API rather than a local key when
an email is configured. Those are asserted directly.
"""

from datetime import timedelta

import pytest

from app.storage import (
    SIGNED_URL_TTL_SECONDS,
    GcsObjectStore,
    document_name,
    new_upload_name,
)

BUCKET = "test-bucket"
HASH = "a" * 64


@pytest.fixture(scope="session")
def gcs_endpoint():
    """fake-gcs-server on a random port, once for the session."""
    from testcontainers.core.container import DockerContainer
    from testcontainers.core.waiting_utils import wait_for_logs

    container = (
        DockerContainer("fsouza/fake-gcs-server:1.52")
        .with_command("-scheme http -port 4443 -backend memory")
        .with_exposed_ports(4443)
    )
    with container:
        wait_for_logs(container, "server started at")
        host = container.get_container_host_ip()
        port = container.get_exposed_port(4443)
        yield f"http://{host}:{port}"


@pytest.fixture
def store(gcs_endpoint):
    from google.auth.credentials import AnonymousCredentials
    from google.cloud import storage

    client = storage.Client(
        project="test",
        credentials=AnonymousCredentials(),
        client_options={"api_endpoint": gcs_endpoint},
    )
    # Recreated per test so one test's objects cannot be another's premise.
    bucket = client.bucket(BUCKET)
    if bucket.exists():
        for blob in client.list_blobs(BUCKET):
            blob.delete()
    else:
        client.create_bucket(BUCKET)
    return GcsObjectStore(BUCKET, client=client)


def _put(store, object_name: str, content: bytes = b"%PDF-1.7 data") -> None:
    """Stand in for the browser's PUT to the signed URL."""
    store._bucket.blob(object_name).upload_from_string(content)


# ---------------------------------------------------------------------------
# Against the emulator
# ---------------------------------------------------------------------------


def test_size_is_none_when_there_is_no_object(store):
    assert store.size(new_upload_name()) is None


def test_size_is_the_byte_count(store):
    name = new_upload_name()
    _put(store, name, b"0123456789")

    assert store.size(name) == 10


def test_download_to_reproduces_the_bytes(store, tmp_path):
    name = new_upload_name()
    _put(store, name, b"%PDF-1.7 real bytes")
    dest = tmp_path / "downloaded.pdf"

    store.download_to(name, str(dest))

    assert dest.read_bytes() == b"%PDF-1.7 real bytes"


def test_upload_from_puts_a_local_file_in_the_bucket(store, tmp_path):
    src = tmp_path / "body.pdf"
    src.write_bytes(b"%PDF-1.7 from a request body")
    name = document_name(HASH)

    store.upload_from(str(src), name)

    assert store.size(name) == len(b"%PDF-1.7 from a request body")


def test_copy_leaves_the_source_in_place(store):
    """Never a move: the lifecycle rule owns deletion in uploads/, and the
    local stand-in behaves the same way. If these two ever disagree, every
    test written against the local one is lying.
    """
    src = new_upload_name()
    _put(store, src, b"bytes")
    dst = document_name(HASH)

    store.copy(src, dst)

    assert store.size(src) == len(b"bytes")
    assert store.size(dst) == len(b"bytes")


def test_copy_overwrites_the_same_content_addressed_name(store):
    """Re-ingesting the same bytes writes the same object, not a second one
    -- which is why object versioning is off on the bucket.
    """
    first = new_upload_name()
    second = new_upload_name()
    _put(store, first, b"same bytes")
    _put(store, second, b"same bytes")
    dst = document_name(HASH)

    store.copy(first, dst)
    store.copy(second, dst)

    assert store.size(dst) == len(b"same bytes")


# ---------------------------------------------------------------------------
# The signature: parameters, not cryptography
# ---------------------------------------------------------------------------


class _RecordingBlob:
    def __init__(self, recorded: dict) -> None:
        self._recorded = recorded

    def generate_signed_url(self, **kwargs):
        self._recorded.update(kwargs)
        return "https://signed.example/url"


class _RecordingBucket:
    def __init__(self, recorded: dict) -> None:
        self.recorded = recorded
        self.names: list[str] = []

    def blob(self, object_name: str):
        self.names.append(object_name)
        return _RecordingBlob(self.recorded)


@pytest.fixture
def recording_store():
    """A store whose bucket records what generate_signed_url was asked for.

    Not a test of google-cloud-storage: a test of the arguments this module
    chooses, which are the part a change here could silently get wrong.
    """
    store = GcsObjectStore.__new__(GcsObjectStore)
    store._client = None
    store._signer_email = None
    store._bucket = _RecordingBucket({})
    return store


def test_the_url_is_signed_for_a_v4_put_on_that_object(recording_store):
    name = new_upload_name()

    recording_store.signed_upload_url(name)

    assert recording_store._bucket.names == [name]
    assert recording_store._bucket.recorded["version"] == "v4"
    assert recording_store._bucket.recorded["method"] == "PUT"


def test_the_url_expires_at_the_configured_ttl(recording_store):
    recording_store.signed_upload_url(new_upload_name())

    assert recording_store._bucket.recorded["expiration"] == timedelta(
        seconds=SIGNED_URL_TTL_SECONDS
    )


def test_no_header_is_bound_into_the_signature(recording_store):
    """A bound content type or length is one more thing the client has to
    reproduce byte-exactly, and a mismatch fails with nothing useful to read.
    What the bytes are is the QA gate's question.
    """
    recording_store.signed_upload_url(new_upload_name())

    assert "content_type" not in recording_store._bucket.recorded
    assert "headers" not in recording_store._bucket.recorded


def test_signing_goes_through_iam_when_a_signer_email_is_configured():
    """The whole point of the signer email: no private key in the process.

    With one configured, the call carries the account to sign as and a
    bearer token, which is what makes google-cloud-storage use the IAM
    Credentials API instead of looking for a key locally.
    """

    class _Credentials:
        valid = True
        token = "ya29.fake"

    class _Client:
        _credentials = _Credentials()

    store = GcsObjectStore.__new__(GcsObjectStore)
    store._client = _Client()
    store._signer_email = "ingest@rag-for-doa.iam.gserviceaccount.com"
    store._bucket = _RecordingBucket({})

    store.signed_upload_url(new_upload_name())

    recorded = store._bucket.recorded
    assert recorded["service_account_email"] == store._signer_email
    assert recorded["access_token"] == "ya29.fake"


def test_without_a_signer_email_nothing_about_iam_is_sent(recording_store):
    recording_store.signed_upload_url(new_upload_name())

    assert "service_account_email" not in recording_store._bucket.recorded
    assert "access_token" not in recording_store._bucket.recorded
