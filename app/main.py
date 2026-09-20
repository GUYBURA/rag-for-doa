"""HTTP surface. Transport only -- no retrieval, reranking, prompting or
model call happens in this file (CLAUDE.md: logic belongs in query/).

Authentication and rate limiting live here because they are transport
concerns: they decide whether a request gets to ask at all, and know nothing
about what is asked. The guards on the question and the answer themselves
(PII, grounding, injection) are in query/guards.py.
"""

import hmac
import logging
import math
import os
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from dotenv import load_dotenv
from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    Request,
    UploadFile,
)
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, StringConstraints

from app.storage import (
    SIGNED_URL_TTL_SECONDS,
    LocalObjectStore,
    ObjectStore,
    document_name,
    is_upload_name,
    new_upload_name,
)
from ingest.db import DocumentSummary, find_document_summary_by_hash
from ingest.qa_gate import failed_checks
from ingest.run import DocumentMeta, file_hash256
from ingest.run import ingest as ingest_document
from query.answer import answer as answer_question
from query.prompt import Answer
from query.retrieve import make_store

Answerer = Callable[[str], Answer]
Ingester = Callable[[str, DocumentMeta], dict]
DocumentLookup = Callable[[str], DocumentSummary | None]

RATE_LIMIT_PER_WINDOW = 10
RATE_LIMIT_WINDOW_SECONDS = 60

# Cloud Run rejects a request body over 32 MiB before it reaches this process,
# so a larger ceiling here would be a promise the platform does not keep. Some
# volumes in this corpus are bigger; those upload straight to Cloud Storage
# through a signed URL instead (ARCHITECTURE.md), which is why this is a
# ceiling and not the whole answer.
MAX_UPLOAD_BYTES = 32 * 1024 * 1024

# The ceiling for the signed-URL route, which Cloud Run never sees. Higher,
# but not unbounded: it is read from the object's metadata before anything is
# downloaded, so a refusal costs one metadata call rather than the transfer.
MAX_OBJECT_BYTES = 512 * 1024 * 1024

# Where LocalObjectStore keeps its objects. Replaced by a bucket when
# GcsObjectStore lands; the endpoint does not change.
OBJECT_STORE_ROOT = os.environ.get("OBJECT_STORE_ROOT", "data/objects")

# The schema's own CHECK on document.scopes. Repeated here so a bad value is
# a 422 naming the field rather than a constraint violation inside a
# background task nobody is watching.
ALLOWED_SCOPES = frozenset({"fungicide", "insecticide", "herbicide"})

log = logging.getLogger(__name__)


def parse_api_keys(raw: str) -> frozenset[str]:
    """API_KEYS is comma-separated. Blank entries are dropped, not kept: a
    trailing comma would otherwise configure "" as a key, and a request with
    an empty X-API-Key header would authenticate.
    """
    return frozenset(k.strip() for k in raw.split(",") if k.strip())


class FixedWindowLimiter:
    """Per-key request count over a fixed window, held in this process's memory.

    Correct only while exactly one instance serves traffic: each Cloud Run
    instance has its own memory, so N instances would allow N times the limit.
    Deploy with max-instances=1. Raising that means moving the count to a
    shared store, not raising the number here.

    Deliberately no daily cap. Cloud Run scales to zero when idle and wipes
    this memory, so a daily count would reset every time the service slept.
    The money ceiling is the OpenRouter key's spending limit, which lives
    outside this process and survives restarts. This class only stops bursts.

    The dict grows by one entry per key that passes auth, never per garbage
    key, because enforce_rate_limit() depends on require_api_key().
    """

    def __init__(
        self,
        limit: int,
        window_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._limit = limit
        self._window = window_seconds
        self._clock = clock
        self._windows: dict[str, tuple[float, int]] = {}
        self._lock = threading.Lock()

    def hit(self, key: str) -> float | None:
        """Record one request. None if allowed; otherwise the seconds left
        until this key's window resets, and nothing is recorded.
        """
        now = self._clock()
        # FastAPI runs sync endpoints in a threadpool, so two requests with
        # the same key can read the same count and both pass without a lock.
        with self._lock:
            start, count = self._windows.get(key, (now, 0))
            if now - start >= self._window:
                start, count = now, 0
            if count >= self._limit:
                return self._window - (now - start)
            self._windows[key] = (start, count + 1)
            return None


class IngestSlot:
    """One ingestion at a time, tracked in this process's memory.

    Held in memory for the same reason FixedWindowLimiter is, and one more:
    asking the database instead ("is any document still pending?") would be
    wrong forever after a single crashed ingest, because that document stays
    pending until someone re-uploads it. This flag dies with the process that
    owns the work it is protecting, which is exactly the lifetime wanted.

    Correct only with one instance and one worker -- see the Dockerfile's
    --workers 1.

    The file hash of the in-flight upload is kept so a poll for it can say
    "in progress" rather than 404 during the minute before qa_gate produces
    a row to look up.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._file_hash: str | None = None

    def try_acquire(self, file_hash: str) -> bool:
        with self._lock:
            if self._file_hash is not None:
                return False
            self._file_hash = file_hash
            return True

    def release(self) -> None:
        with self._lock:
            self._file_hash = None

    def in_progress(self, file_hash: str) -> bool:
        with self._lock:
            return self._file_hash == file_hash


@asynccontextmanager
async def lifespan(app: FastAPI) -> Iterator[None]:
    """Build the store and the pool once.

    make_store() costs a round trip and an engine, and it runs
    assert_model_matches_corpus() -- a corpus embedded with a different model
    than the one configured now should stop the process at startup, not
    surface as quietly bad ranking on someone's first question.
    """
    load_dotenv()
    # Fail at startup, not on every request: with no keys configured every
    # /ask would be a 401, and a deploy that looks healthy but rejects
    # everyone is harder to notice than one that does not start.
    if not parse_api_keys(os.environ.get("API_KEYS", "")):
        raise RuntimeError("API_KEYS is empty -- set at least one key")
    # Same rule for the admin keys, and for the stronger reason: a deployment
    # whose /admin routes exist but authenticate nobody is not a reduced
    # system, it is a system missing the only way new editions get in.
    if not parse_api_keys(os.environ.get("ADMIN_API_KEYS", "")):
        raise RuntimeError("ADMIN_API_KEYS is empty -- set at least one key")
    dsn = os.environ["DATABASE_URL"]
    app.state.store = make_store(dsn)
    # Sized explicitly rather than left at the library default: one ingestion
    # holds a connection for minutes, and Cloud SQL's smallest tier has few to
    # give. IngestSlot caps the long-lived holders at one, so the rest of this
    # pool stays available to /ask.
    # Local today; a GcsObjectStore swaps in here and nothing above the seam
    # changes. Created in lifespan rather than at import so a test's store can
    # be injected without the module having made a directory first.
    app.state.object_store = LocalObjectStore(Path(OBJECT_STORE_ROOT))
    with ConnectionPool(dsn, min_size=2, max_size=4) as pool:
        app.state.pool = pool
        yield


app = FastAPI(title="RAG over Thai pesticide handbooks", lifespan=lifespan)
app.state.limiter = FixedWindowLimiter(RATE_LIMIT_PER_WINDOW, RATE_LIMIT_WINDOW_SECONDS)
app.state.ingest_slot = IngestSlot()


def get_api_keys() -> frozenset[str]:
    """A dependency, like get_answerer, so tests configure keys without
    touching the environment. Read per request so a rotated secret takes
    effect on the next revision without a code change.
    """
    return parse_api_keys(os.environ.get("API_KEYS", ""))


def get_admin_api_keys() -> frozenset[str]:
    """Deliberately a different set from get_api_keys().

    An /ask key leaking from a public frontend must not also be able to add a
    document, and an admin key must not become a question-asking key if it
    leaks the other way. Two sets, no overlap enforced in code: nothing here
    reads both.
    """
    return parse_api_keys(os.environ.get("ADMIN_API_KEYS", ""))


def get_limiter(request: Request) -> FixedWindowLimiter:
    return request.app.state.limiter


def get_ingest_slot(request: Request) -> IngestSlot:
    return request.app.state.ingest_slot


def get_object_store(request: Request) -> ObjectStore:
    return request.app.state.object_store


def require_api_key(
    keys: Annotated[frozenset[str], Depends(get_api_keys)],
    x_api_key: Annotated[str | None, Header()] = None,
) -> str:
    """The matching key, or 401.

    Missing and wrong keys get the same response, so the reply never says
    which half of the guess was right.

    hmac.compare_digest, not ==. String == returns as soon as one character
    differs, so response time leaks how much of a guess matched. No test in
    this suite can catch a swap back to == -- the difference is nanoseconds
    -- so this comment and code review are the only guard on it.
    """
    if x_api_key is not None:
        for key in keys:
            if hmac.compare_digest(x_api_key.encode(), key.encode()):
                return key
    raise HTTPException(status_code=401, detail="invalid or missing API key")


def require_admin_key(
    keys: Annotated[frozenset[str], Depends(get_admin_api_keys)],
    x_admin_key: Annotated[str | None, Header()] = None,
) -> str:
    """The matching admin key, or 401. Same shape as require_api_key, and a
    separate header: a client that holds both should not be able to send one
    where the other is meant by accident.

    No rate limit on top. The limit on /ask exists to cap model spend per
    caller; an upload's cost is bounded by IngestSlot instead, which allows
    exactly one at a time whatever the caller does.
    """
    if x_admin_key is not None:
        for key in keys:
            if hmac.compare_digest(x_admin_key.encode(), key.encode()):
                return key
    raise HTTPException(status_code=401, detail="invalid or missing admin key")


def enforce_rate_limit(
    key: Annotated[str, Depends(require_api_key)],
    limiter: Annotated[FixedWindowLimiter, Depends(get_limiter)],
) -> None:
    """429 once a key is over its limit. Depends on require_api_key, so auth
    always resolves first and a rejected key never consumes anyone's quota.
    """
    retry_after = limiter.hit(key)
    if retry_after is not None:
        raise HTTPException(
            status_code=429,
            detail="rate limit exceeded",
            headers={"Retry-After": str(math.ceil(retry_after))},
        )


def get_answerer(request: Request) -> Answerer:
    """The seam the tests replace, so the HTTP layer can be exercised with no
    database and no model behind it.
    """

    def run(question: str) -> Answer:
        with request.app.state.pool.connection() as conn:
            return answer_question(question, request.app.state.store, conn)

    return run


def get_ingester(request: Request) -> Ingester:
    """The seam the tests replace, like get_answerer.

    The connection is borrowed inside the returned function, not captured
    here: this runs during the request, while the function it returns runs
    afterwards on a background thread, by which time a connection taken now
    would have been handed back to the pool.
    """

    def run(pdf_path: str, meta: DocumentMeta) -> None:
        with request.app.state.pool.connection() as conn:
            ingest_document(conn, pdf_path, meta)

    return run


def get_document_lookup(request: Request) -> DocumentLookup:
    def look_up(file_hash: str) -> DocumentSummary | None:
        with request.app.state.pool.connection() as conn:
            return find_document_summary_by_hash(conn, file_hash)

    return look_up


class Question(BaseModel):
    # A blank question is a malformed request, not an unanswerable one: 422,
    # not a refusal. Refusal means the corpus had nothing to say.
    question: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1)
    ]


class CitationOut(BaseModel):
    """Everything needed to open the source and check the claim (invariant
    10). `score` is the rerank score, exposed for debugging and for the
    frontend to show how strong a match each source was.
    """

    number: int
    title_th: str
    edition_year_be: int
    page_number: int | None
    section: str | None
    source: str
    score: float


class AnswerOut(BaseModel):
    answer: str
    citations: list[CitationOut]


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/ask")
def ask(
    body: Question,
    _: Annotated[None, Depends(enforce_rate_limit)],
    answerer: Annotated[Answerer, Depends(get_answerer)],
) -> AnswerOut:
    """Answer one question, or refuse.

    A refusal is a 200 with an empty citations list, not a 404. Declining to
    answer is a correct outcome (invariant 11), and modelling it as an HTTP
    error would push every client into treating the system's most important
    behaviour as a failure.
    """
    result = answerer(body.question)
    return AnswerOut(
        answer=result.text,
        citations=[
            CitationOut(
                number=c.number,
                title_th=c.passage.title_th,
                edition_year_be=c.passage.edition_year_be,
                page_number=c.passage.page_number,
                section=c.passage.section,
                source=c.passage.source,
                score=c.score,
            )
            for c in result.citations
        ],
    )


class UploadAccepted(BaseModel):
    """file_hash, not document_id, is the handle.

    A document row cannot exist yet when this response is written: the row
    carries `qa`, and qa_gate only has a verdict after extraction, which is
    the slow part this endpoint refuses to wait for. The hash identifies the
    bytes the caller just sent, is what find_document_summary_by_hash keys
    on, and is stable across the re-upload that re-ingesting a failed
    document requires.
    """

    file_hash: str
    status: str


class UploadUrlOut(BaseModel):
    """Where to PUT the bytes, and the name to quote back afterwards.

    The name is issued here rather than accepted from the client for the
    reason KNOWLEDGE.md gives: whoever holds the URL writes to the path it
    was signed for, so the service has to be the one that picks it.
    """

    object_name: str
    url: str
    expires_in: int


class DocumentStatusOut(BaseModel):
    file_hash: str
    status: str
    qa: dict | None
    chunk_count: int | None


def _save_upload(upload: UploadFile) -> str:
    """Stream to a temp file, refusing at the ceiling. Returns the path.

    Streamed in chunks rather than read() into memory: the point of a size
    limit is not to hold the thing you are refusing.
    """
    written = 0
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        path = tmp.name
        while chunk := upload.file.read(1024 * 1024):
            written += len(chunk)
            if written > MAX_UPLOAD_BYTES:
                tmp.close()
                os.unlink(path)
                raise HTTPException(
                    status_code=413,
                    detail=f"file exceeds {MAX_UPLOAD_BYTES} bytes",
                )
            tmp.write(chunk)
    return path


def _fetch_object(
    store: ObjectStore, object_name: str, source: str | None
) -> tuple[str, str]:
    """Bring an already-uploaded object down to a temp path.

    The name comes from the client, so it is checked before it reaches
    storage: it must be one this service issued into the staging prefix.
    Without that, a caller could name a path in documents/ and have the
    service ingest -- and later overwrite -- the source PDF of a live
    edition.

    Size is read from the object's metadata first. Refusing after the
    download would mean paying for the transfer of the thing being refused.
    """
    if not is_upload_name(object_name):
        raise HTTPException(status_code=422, detail="not an upload object name")

    size = store.size(object_name)
    if size is None:
        raise HTTPException(status_code=404, detail="no such object")
    if size > MAX_OBJECT_BYTES:
        raise HTTPException(
            status_code=413, detail=f"object exceeds {MAX_OBJECT_BYTES} bytes"
        )

    # The object name is a uuid, so the human-meaningful filename has to come
    # from the form. document.source is display and uniqueness -- (edition,
    # source) -- and a uuid in that column would make two editions of the same
    # volume look unrelated.
    if not source:
        raise HTTPException(
            status_code=422, detail="source is required with object_name"
        )

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        path = tmp.name
    store.download_to(object_name, path)
    return path, source


def _ingest_in_background(
    ingester: Ingester,
    slot: IngestSlot,
    store: ObjectStore,
    pdf_path: str,
    meta: DocumentMeta,
    file_hash: str,
    object_name: str | None,
) -> None:
    """Runs on Starlette's threadpool after the 202 has been sent.

    Every failure is swallowed into the log on purpose: there is no client
    left to raise at, and an unhandled exception here would leave the slot
    held and the temp file on disk for the life of the process. What the
    uploader sees instead is the document staying 'pending' with its qa
    record, which is the same signal a gate failure gives (invariant 9).

    A passing ingest archives the original at documents/<file_hash>.pdf --
    invariant 3 wants re-ingestion from the source to be verifiable, and a
    file_hash on a permanent row proves nothing if the bytes it names are
    gone. A failing one archives nothing: those bytes are not a source of
    anything, and the staging prefix's lifecycle rule removes them.

    Which call does the archiving depends on where the bytes came from. The
    signed-URL route already has the object in the bucket, so it copies
    server-side; the multipart route only ever had a request body, so it
    uploads the temp file.
    """
    try:
        qa = ingester(pdf_path, meta)
        if not failed_checks(qa):
            destination = document_name(file_hash)
            if object_name is None:
                store.upload_from(pdf_path, destination)
            else:
                store.copy(object_name, destination)
    except Exception:
        log.exception("ingestion failed for %s", meta.source)
    finally:
        slot.release()
        try:
            os.unlink(pdf_path)
        except OSError:
            log.warning("could not remove temp upload %s", pdf_path)


@app.post("/admin/uploads", status_code=201)
def create_upload(
    _: Annotated[str, Depends(require_admin_key)],
    store: Annotated[ObjectStore, Depends(get_object_store)],
) -> UploadUrlOut:
    """Issue a one-shot URL for a file too large to post through here.

    No metadata is taken: title, edition and scopes reach the pipeline on the
    POST that claims the object, because a document row cannot be written
    until qa_gate has run anyway. This endpoint therefore stores nothing --
    an unclaimed object is removed by the bucket's lifecycle rule rather than
    by a record kept here.
    """
    object_name = new_upload_name()
    return UploadUrlOut(
        object_name=object_name,
        url=store.signed_upload_url(object_name),
        expires_in=SIGNED_URL_TTL_SECONDS,
    )


@app.post("/admin/documents", status_code=202)
def upload_document(
    background: BackgroundTasks,
    _: Annotated[str, Depends(require_admin_key)],
    ingester: Annotated[Ingester, Depends(get_ingester)],
    look_up: Annotated[DocumentLookup, Depends(get_document_lookup)],
    slot: Annotated[IngestSlot, Depends(get_ingest_slot)],
    title_th: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1), Form()
    ],
    edition_year_be: Annotated[int, Form()],
    scopes: Annotated[list[str], Form(min_length=1)],
    store: Annotated[ObjectStore, Depends(get_object_store)],
    file: Annotated[UploadFile | None, File()] = None,
    object_name: Annotated[str | None, Form()] = None,
    source: Annotated[str | None, Form()] = None,
) -> UploadAccepted:
    """Accept one handbook and ingest it in the background.

    Two routes in, one handler: `file` for a body Cloud Run will carry, or
    `object_name` for one already uploaded through a signed URL. One endpoint
    rather than two because everything after the bytes reach a local path --
    auth, the slot, the duplicate check, the handoff -- is identical, and two
    handlers would be two chances for those to drift apart.

    Everything here is transport: validating the form, turning it into the
    DocumentMeta that ingest() has always taken (invariant 1 -- no stage
    infers this from the PDF), deciding whether to admit the request, and
    handing off. The pipeline itself is untouched.
    """
    unknown = sorted(set(scopes) - ALLOWED_SCOPES)
    if unknown:
        raise HTTPException(status_code=422, detail=f"unknown scopes: {unknown}")

    if (file is None) == (object_name is None):
        raise HTTPException(
            status_code=422, detail="send exactly one of file or object_name"
        )

    if file is not None:
        path = _save_upload(file)
        filename = file.filename or "upload.pdf"
    else:
        path, filename = _fetch_object(store, object_name, source)

    file_hash = file_hash256(path)

    # ingest() checks this itself and is the authority; this is the same check
    # early, so a duplicate is a 409 the uploader sees rather than a failure
    # buried in a background task after a 202. The check is safe from the
    # usual read-then-write race because the slot below admits one ingestion
    # at a time, so nothing else can insert between here and the write.
    existing = look_up(file_hash)
    if existing is not None and existing.status != "pending":
        os.unlink(path)
        raise HTTPException(
            status_code=409,
            detail=f"already ingested with status {existing.status!r}",
        )

    if not slot.try_acquire(file_hash):
        os.unlink(path)
        raise HTTPException(
            status_code=503,
            detail="another ingestion is in progress",
            headers={"Retry-After": "60"},
        )

    meta = DocumentMeta(
        source=filename,
        title_th=title_th,
        edition_year_be=edition_year_be,
        scopes=list(scopes),
    )
    background.add_task(
        _ingest_in_background,
        ingester,
        slot,
        store,
        path,
        meta,
        file_hash,
        object_name,
    )
    return UploadAccepted(file_hash=file_hash, status="accepted")


@app.get("/admin/documents/{file_hash}")
def document_status(
    file_hash: str,
    _: Annotated[str, Depends(require_admin_key)],
    look_up: Annotated[DocumentLookup, Depends(get_document_lookup)],
    slot: Annotated[IngestSlot, Depends(get_ingest_slot)],
) -> DocumentStatusOut:
    """Progress for one upload.

    qa is returned whole. A document that stops at 'pending' did so because a
    gate check failed, and the uploader cannot act on that without seeing
    which one.
    """
    summary = look_up(file_hash)
    if summary is not None:
        return DocumentStatusOut(
            file_hash=file_hash,
            status=summary.status,
            qa=summary.qa,
            chunk_count=summary.chunk_count,
        )
    # No row yet. Extraction runs before qa_gate can produce one, so for the
    # first minute of a large volume this is the only truthful answer -- and
    # it is memory, so if the process died mid-ingest this correctly becomes
    # a 404 instead of a promise nobody is keeping.
    if slot.in_progress(file_hash):
        return DocumentStatusOut(
            file_hash=file_hash, status="in_progress", qa=None, chunk_count=None
        )
    raise HTTPException(status_code=404, detail="no document with that file hash")
