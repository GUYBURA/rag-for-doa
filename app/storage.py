"""Object storage for uploads too large to pass through Cloud Run.

Cloud Run refuses a request body over 32 MiB before it reaches the process,
and some volumes in this corpus are bigger. Those upload straight to the
bucket with a signed URL, and this module is the seam that makes the rest of
the code not care which route a file took: the endpoint ends up with a local
path either way, and ingest() never learns that a bucket exists.

Two prefixes, and the reason is in KNOWLEDGE.md: the URL has to be signed
before anyone has seen the file, so the client picks the path it writes to.
`uploads/` guarantees nothing and is lifecycle-deleted after a day;
`documents/` is content-addressed, written only by this service after a
successful ingest, and kept forever because invariant 3 needs the original
bytes to stay verifiable.

LocalObjectStore below is not a mock. It does the same work against the
filesystem, so the endpoint can be finished and tested before a GCP project
exists -- and when GcsObjectStore arrives, a failure is attributable to GCS
rather than to logic that was never exercised.
"""

import re
import shutil
import uuid
from pathlib import Path
from typing import Protocol

UPLOAD_PREFIX = "uploads/"
DOCUMENT_PREFIX = "documents/"

# 15 minutes. Derived, not picked: the ceiling for this route is a few
# hundred MB, and an upload at 2 Mbit/s -- a bad domestic connection -- needs
# about 13 minutes for 200 MB. Shorter and a real upload dies mid-flight;
# longer and a leaked URL stays usable for no good reason. Recompute this if
# the size ceiling moves.
SIGNED_URL_TTL_SECONDS = 900

# What new_upload_name() produces, and the only shape is_upload_name()
# accepts. An allowlist rather than a search for "..": a denylist has to
# anticipate every way out of the prefix, and this one has to be right the
# first time -- see is_upload_name().
_UPLOAD_NAME = re.compile(r"^uploads/[0-9a-f]{32}\.pdf$")


def new_upload_name() -> str:
    """An unguessable name in the staging prefix.

    uuid4 rather than anything sequential: the name is the only thing
    separating one uploader's object from another's, and a countable name
    would also leak how many uploads the system has taken.
    """
    return f"{UPLOAD_PREFIX}{uuid.uuid4().hex}.pdf"


def document_name(file_hash: str) -> str:
    """Where the verified original lives, derived from the hash.

    Content-addressed on purpose: document.file_hash already identifies the
    file, so nothing has to store this path (invariant 6 -- check whether an
    existing column does the job before adding one). Re-ingesting the same
    bytes overwrites the same object rather than accumulating copies.
    """
    return f"{DOCUMENT_PREFIX}{file_hash}.pdf"


def is_upload_name(object_name: str) -> bool:
    """True only for a name this service issued into the staging prefix.

    The client sends this string back on POST /admin/documents, so it is
    untrusted input pointing at storage. Two things it must not be allowed to
    be: a path in `documents/` -- which would let the caller overwrite the
    source PDF of a live edition, the artifact invariant 3 rests on -- or
    anything that climbs out of the prefix.

    Matching the exact shape settles both, and a nested path, an empty
    remainder and a bare directory with it.
    """
    return _UPLOAD_NAME.fullmatch(object_name) is not None


class ObjectStore(Protocol):
    """The four operations the upload route needs, and no more.

    Deliberately not a bucket abstraction: no listing, no deleting. Deleting
    in `uploads/` belongs to the bucket's lifecycle rule, and nothing may
    delete in `documents/` at all.
    """

    def signed_upload_url(self, object_name: str) -> str: ...

    def size(self, object_name: str) -> int | None: ...

    def download_to(self, object_name: str, dest_path: str) -> None: ...

    def upload_from(self, src_path: str, object_name: str) -> None: ...

    def copy(self, src: str, dst: str) -> None: ...


class LocalObjectStore:
    """The filesystem standing in for a bucket, for tests and local runs."""

    def __init__(self, root: Path) -> None:
        # Resolved once here so _path() compares a resolved path against a
        # resolved root. Left unresolved, a root behind a symlink or carrying
        # a "." segment fails the containment check on names that are fine.
        self._root = Path(root).resolve()

    def _path(self, object_name: str) -> Path:
        """Object name to a path on disk, guaranteed to stay under the root.

        The second of two guards, and not redundant with is_upload_name():
        that one answers a domain question at the edge ("is this a name we
        issued?") and is what returns a 422. This one answers a filesystem
        question inside the store ("can this escape?"), it protects copy()'s
        destination which never passes the first check at all, and it still
        holds if the first is ever loosened by mistake.

        Joining strings is not enough to check containment -- `uploads/../..`
        looks contained until the OS walks it -- so resolve first, compare
        after.
        """
        resolved = (self._root / object_name).resolve()
        if not resolved.is_relative_to(self._root):
            raise ValueError(f"object name escapes the store root: {object_name!r}")
        return resolved

    def signed_upload_url(self, object_name: str) -> str:
        """A URL-shaped string that deliberately cannot work.

        There is nothing to sign here, and returning a file:// path would
        hand back something that looks usable and silently is -- on one
        machine, with no signature and no expiry. `.invalid` is reserved by
        RFC 2606 and never resolves, so anything that mistakes this for a
        real upload URL fails loudly.
        """
        return f"https://local.invalid/{object_name}"

    def size(self, object_name: str) -> int | None:
        """Bytes, or None when there is no such object.

        None rather than an exception because the caller distinguishes two
        answers with different status codes: nothing there at all, and there
        but too big. A raise here would make both a 500.
        """
        path = self._path(object_name)
        if not path.is_file():
            # is_file(), not exists(): a directory has a stat() and a size,
            # and neither means anything. GCS has no directories, so a name
            # that lands on one is a name that has no object behind it.
            return None
        return path.stat().st_size

    def download_to(self, object_name: str, dest_path: str) -> None:
        """Copy the object out to a path the caller owns.

        Only the source goes through _path(): the destination is a temp file
        this service chose, not a name from a request.
        """
        shutil.copyfile(self._path(object_name), dest_path)

    def upload_from(self, src_path: str, object_name: str) -> None:
        """Put a local file into the store.

        Used by the multipart route, whose bytes only ever existed as a
        request body: without this, small volumes would have no original in
        the bucket and only large ones would be verifiable, which is not a
        distinction invariant 3 makes. The storage route uses copy() instead
        -- the object is already there, and a server-side copy beats sending
        several hundred MB back up.
        """
        destination = self._path(object_name)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src_path, destination)

    def copy(self, src: str, dst: str) -> None:
        """Copy within the store. Never a move.

        The staging object has to survive: its lifecycle rule is what removes
        it, and GCS copy_blob leaves the source in place too. A move here
        would make this store behave differently from the real one, which is
        the one thing a stand-in must not do.
        """
        source = self._path(src)
        destination = self._path(dst)
        # GCS has no directories -- "uploads/abc.pdf" is one flat name with a
        # slash in it -- but a filesystem needs the parent to exist first.
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
