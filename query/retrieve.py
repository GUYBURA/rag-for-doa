"""Hybrid retrieval: dense vectors and Thai lexical matching, fused.

LangChain owns this layer (CLAUDE.md, library boundary). PGVectorStore attaches
to the hand-written `chunk` table by column name and runs both halves of the
search in one round trip.

No edition or status predicate appears anywhere below, and none may be added -
invariant 4. Everything in `chunk` belongs to an active edition because
promote.py deleted the rest, which is the whole reason this path has no filter
to forget.
"""

import uuid
from dataclasses import dataclass

import psycopg
from langchain_postgres import PGEngine, PGVectorStore
from langchain_postgres.v2.hybrid_search_config import (
    HybridSearchConfig,
    reciprocal_rank_fusion,
)
from pythainlp.tokenize import word_tokenize

from ingest.normalize import normalize_query
from query.embeddings import OpenRouterEmbeddings, assert_model_matches_corpus

TABLE = "chunk"
TSV_COLUMN = "content_tsv"

# 'simple' because that is what insert_chunks() built content_tsv with. The
# library defaults tsv_lang to pg_catalog.english, whose stemmer and stopword
# list would then be applied to the query but not to the indexed text -- a
# mismatch that silently costs recall rather than raising.
TSV_LANG = "simple"

# Candidates handed to reranking, not the final answer set. Deliberately wider
# than the top 5 that reach the prompt: the reranker can only choose from what
# this returns.
CANDIDATES = 20


@dataclass(frozen=True)
class Passage:
    """One retrieved chunk, already carrying everything a citation needs.

    edition_year_be, title_th and source live on `document`, not on `chunk`,
    and langchain_metadata does not copy them. Invariant 10 makes them part of
    the answer, so they are joined in here rather than left for prompt.py to
    fetch - a passage that cannot be cited should not exist as a value.
    """

    chunk_id: uuid.UUID
    content: str
    page_number: int | None
    section: str | None
    document_id: uuid.UUID
    edition_year_be: int
    title_th: str
    source: str
    rank: int
    fusion_score: float


def async_url(dsn: str) -> str:
    """Rewrite a psycopg DSN as the SQLAlchemy async URL PGEngine needs.

    PGEngine calls create_async_engine(), so the driver has to be named in the
    scheme; a bare postgresql:// URL loads the sync psycopg2 dialect and fails
    at connect time. asyncpg is already in the tree as a transitive dependency
    of langchain-postgres.

    Query parameters are left alone. libpq spellings asyncpg does not
    understand (sslmode=, channel_binding=) would need translating, and the
    right time to do that is when a deployment actually uses one.
    """
    for prefix in ("postgresql+asyncpg://", "postgresql://", "postgres://"):
        if dsn.startswith(prefix):
            return "postgresql+asyncpg://" + dsn[len(prefix) :]
    raise ValueError(f"not a PostgreSQL DSN: {dsn!r}")


def fts_query(question: str) -> str:
    """Segment a Thai question the same way content_tsv was segmented.

    This is the load-bearing line of the sparse half. The library builds its
    query as plainto_tsquery(lang, fts_query) over whatever string it is given,
    and Thai is written without spaces, so a raw question arrives as one
    enormous token that matches nothing. content_tsv was built from PyThaiNLP
    newmm tokens joined by spaces; the query has to be built the same way, or
    the lexical half returns zero rows for every question and hybrid retrieval
    quietly degrades to dense-only.
    """
    return " ".join(word_tokenize(question, engine="newmm"))


def hybrid_config(tokens: str, top_k: int) -> HybridSearchConfig:
    """A fresh config for one search. Never reuse one.

    The store mutates the config it is handed: if fts_query is empty it writes
    the raw question into it and the value sticks to the object. Share one
    config across calls and every question after the first is searched
    lexically for the first question's words, with correct dense results
    alongside to make the output look plausible.

    Reciprocal rank fusion rather than the library's default weighted sum:
    cosine distance and ts_rank_cd have no common scale, and the weighted-sum
    path min-max normalizes each side within its own result set, which makes
    the top hit score 1.0 whether or not it is any good. RRF reads only the
    ranks, so nothing about it depends on those two scores being comparable.
    """
    return HybridSearchConfig(
        tsv_column=TSV_COLUMN,
        tsv_lang=TSV_LANG,
        fts_query=tokens,
        fusion_function=reciprocal_rank_fusion,
        primary_top_k=top_k,
        secondary_top_k=top_k,
    )


def assert_hybrid_enabled(config: HybridSearchConfig) -> None:
    """Raise if the store turned lexical search off behind our back.

    create_sync() does not raise when the tsv column is missing or is not a
    tsvector: it blanks tsv_column on the config object it was handed and
    carries on. Hybrid retrieval then runs dense-only, returning plausible
    results for every question, and nothing anywhere reports a problem. Reading
    the config back afterwards is the only signal there is.
    """
    if config.tsv_column != TSV_COLUMN:
        raise RuntimeError(
            f"{TABLE}.{TSV_COLUMN} is missing or is not a tsvector, so the "
            f"store disabled lexical search. Hybrid retrieval would run "
            f"dense-only without reporting anything."
        )


def make_store(dsn: str) -> PGVectorStore:
    """Attach a vector store to the existing `chunk` table.

    Never init_vectorstore_table(): the DDL is hand-maintained (CLAUDE.md).
    content_column is named explicitly on purpose - create() defaults it to
    `content` and init_vectorstore_table() to `page_content`, and the mismatch
    surfaces at query time rather than at startup.

    The model check runs here, at startup, on a throwaway psycopg connection.
    It is the one moment where failing is cheap; after this the mismatch only
    shows up as bad answers.

    The config passed to create_sync() is a probe, not the one searches use.
    """
    with psycopg.connect(dsn) as conn:
        assert_model_matches_corpus(conn)

    engine = PGEngine.from_connection_string(async_url(dsn))
    probe = hybrid_config(tokens="", top_k=CANDIDATES)

    store = PGVectorStore.create_sync(
        engine=engine,
        embedding_service=OpenRouterEmbeddings(),
        table_name=TABLE,
        content_column="content",
        embedding_column="embedding",
        id_column="langchain_id",
        metadata_json_column="langchain_metadata",
        metadata_columns=[],
        hybrid_search_config=probe,
    )

    assert_hybrid_enabled(probe)
    return store


def _hydrate(conn: psycopg.Connection, chunk_ids: list[uuid.UUID]) -> dict:
    """Fetch the citation fields for retrieved chunks, in one query.

    chunk_with_document, not the retrieval path - CLAUDE.md is explicit that
    the view is for citation building and eval. Retrieval already happened,
    against `chunk`; this only decorates its result. Note there is still no
    status predicate: the view exposes the column, and filtering on it would
    reintroduce exactly the post-filtering this design removes.
    """
    rows = conn.execute(
        """
        SELECT chunk_id, page_number, section, document_id,
               edition_year_be, title_th, source
        FROM   chunk_with_document
        WHERE  chunk_id = ANY(%s)
        """,
        (chunk_ids,),
    ).fetchall()
    return {row[0]: row for row in rows}


def retrieve(
    store: PGVectorStore,
    conn: psycopg.Connection,
    question: str,
    *,
    k: int = CANDIDATES,
) -> list[Passage]:
    """Hybrid search for one question, ranked, with citations attached.

    fusion_score is deliberately not a relevance gate. RRF scores are a
    function of rank alone: the top hit of a search that found nothing relevant
    scores exactly what the top hit of a search that found the answer scores.
    Invariant 11's threshold has to come from reranking, which judges this
    passage against this question in absolute terms.
    """
    normalized = normalize_query(question)
    if not normalized:
        return []

    config = hybrid_config(tokens=fts_query(normalized), top_k=k)
    hits = store.similarity_search_with_score(
        normalized, k=k, hybrid_search_config=config
    )

    chunk_ids = [uuid.UUID(document.id) for document, _ in hits]
    by_id = _hydrate(conn, chunk_ids)

    passages = []
    for rank, (document, score) in enumerate(hits, start=1):
        chunk_id = uuid.UUID(document.id)
        row = by_id.get(chunk_id)
        if row is None:
            # The chunk was archived between the search and this join.
            # Dropping it is the only safe answer: promote.py deleted it
            # because a newer edition supersedes it, and citing it would serve
            # retired guidance.
            continue
        _, page_number, section, document_id, edition_year_be, title_th, source = row
        passages.append(
            Passage(
                chunk_id=chunk_id,
                content=document.page_content,
                page_number=page_number,
                section=section,
                document_id=document_id,
                edition_year_be=edition_year_be,
                title_th=title_th,
                source=source,
                rank=rank,
                fusion_score=score,
            )
        )
    return passages
