"""The object store seam: names, containment, and the four operations.

No GCP, no network. LocalObjectStore is the implementation under test and
also the one the endpoint tests use, so a bug here would be invisible there
-- which is why the containment guard is tested directly rather than only
through is_upload_name().
"""

from pathlib import Path

import pytest

from app.storage import (
    DOCUMENT_PREFIX,
    UPLOAD_PREFIX,
    LocalObjectStore,
    document_name,
    is_upload_name,
    new_upload_name,
)

HASH = "a" * 64


@pytest.fixture
def store(tmp_path):
    return LocalObjectStore(tmp_path)


def _put(store, object_name: str, content: bytes = b"%PDF-1.7 data") -> Path:
    """Write an object directly, the way an upload through a signed URL
    would: the store has no put() of its own, because nothing in this service
    ever writes to the staging prefix.
    """
    path = store._path(object_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------


def test_a_new_upload_name_lands_in_the_staging_prefix():
    name = new_upload_name()

    assert name.startswith(UPLOAD_PREFIX)
    assert is_upload_name(name)


def test_two_upload_names_differ():
    assert new_upload_name() != new_upload_name()


def test_a_document_name_is_derived_from_the_hash():
    assert document_name(HASH) == f"{DOCUMENT_PREFIX}{HASH}.pdf"


def test_a_document_path_is_not_an_upload_name():
    """The check that stops a caller overwriting the source PDF of a live
    edition by passing its permanent path as the object to ingest.
    """
    assert is_upload_name(document_name(HASH)) is False


@pytest.mark.parametrize(
    "name",
    [
        "uploads/../documents/" + HASH + ".pdf",
        "uploads/",
        "uploads",
        "uploads/nested/" + "b" * 32 + ".pdf",
        "uploads/" + "b" * 32,  # no suffix
        "uploads/" + "B" * 32 + ".pdf",  # uuid4().hex is lower case
        "uploads/not-a-hash.pdf",
        "/uploads/" + "b" * 32 + ".pdf",
        "",
    ],
)
def test_names_this_service_never_issued_are_refused(name):
    assert is_upload_name(name) is False


# ---------------------------------------------------------------------------
# Containment
# ---------------------------------------------------------------------------


def test_a_name_escaping_the_root_is_refused_by_the_store_itself(store):
    """Aimed straight at LocalObjectStore, bypassing is_upload_name().

    The two guards answer different questions, and this one has to hold on
    its own: copy()'s destination never passes through the name check at all.
    """
    with pytest.raises(ValueError):
        store.size("uploads/../../etc/passwd")


def test_the_root_is_resolved_so_a_legitimate_name_is_not_refused(tmp_path):
    """A root reached through a '.' segment still contains its own objects.
    Without resolving the root in __init__, this comparison fails on names
    that are perfectly fine.
    """
    store = LocalObjectStore(tmp_path / "." / "bucket")
    name = new_upload_name()
    _put(store, name)

    assert store.size(name) == len(b"%PDF-1.7 data")


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------


def test_the_signed_url_points_at_the_object(store):
    name = new_upload_name()

    assert name in store.signed_upload_url(name)


def test_the_local_signed_url_cannot_be_mistaken_for_a_working_one(store):
    """RFC 2606 reserves .invalid: anything that tries to use this fails
    loudly instead of quietly working on one machine.
    """
    assert "local.invalid" in store.signed_upload_url(new_upload_name())


def test_size_is_none_when_there_is_no_object(store):
    assert store.size(new_upload_name()) is None


def test_size_is_none_for_a_name_that_lands_on_a_directory(store):
    """GCS has no directories, so this is 'no object', not a size."""
    name = new_upload_name()
    store._path(name).mkdir(parents=True)

    assert store.size(name) is None


def test_size_is_the_byte_count(store):
    name = new_upload_name()
    _put(store, name, b"0123456789")

    assert store.size(name) == 10


def test_download_to_reproduces_the_bytes(store, tmp_path):
    name = new_upload_name()
    _put(store, name, b"%PDF-1.7 real bytes")
    dest = tmp_path / "out" / "downloaded.pdf"
    dest.parent.mkdir(parents=True)

    store.download_to(name, str(dest))

    assert dest.read_bytes() == b"%PDF-1.7 real bytes"


def test_copy_leaves_the_source_in_place(store):
    """Never a move: the lifecycle rule owns deletion in the staging prefix,
    and GCS copy_blob leaves the source too. A store that moved would make
    every test built on it lie.
    """
    src = new_upload_name()
    _put(store, src, b"bytes")
    dst = document_name(HASH)

    store.copy(src, dst)

    assert store._path(src).exists()
    assert store._path(dst).read_bytes() == b"bytes"


def test_copy_creates_the_destination_prefix(store):
    """documents/ does not exist until the first successful ingest."""
    src = new_upload_name()
    _put(store, src)

    store.copy(src, document_name(HASH))

    assert (store._path(DOCUMENT_PREFIX.rstrip("/"))).is_dir()
