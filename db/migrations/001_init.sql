-- 001_init.sql — full schema.
-- Run: psql "$DATABASE_URL" -f db/migrations/001_init.sql
--
-- Column names on `chunk` (langchain_id, content, embedding,
-- langchain_metadata) match what PGVectorStore expects, so it attaches to
-- this table directly with no subclass. We own the DDL.

\set ON_ERROR_STOP on

CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS vector;

-- =====================================================================
-- 1. DOCUMENT — one row per volume. Permanent audit trail.
--    Rows are NEVER deleted, including for superseded editions.
-- =====================================================================

CREATE TABLE document (
    document_id     uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    source          text        NOT NULL,          -- original filename
    title_th        text        NOT NULL,
    edition_year_be smallint    NOT NULL,
    scopes          text[]      NOT NULL
                    CHECK (scopes <@ ARRAY['fungicide','insecticide','herbicide']
                           AND cardinality(scopes) > 0),
    status          text        NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','active','archived')),
    file_hash       char(64)    NOT NULL UNIQUE,   -- detects re-uploads
    page_count      int,
    chunk_count     int,                           -- as of last ingest; survives archival
    parser          text,                          -- 'pymupdf'
    embedding_model text,                          -- pinned per document
    qa              jsonb       NOT NULL DEFAULT '{}',
    ingested_at     timestamptz NOT NULL DEFAULT now(),
    archived_at     timestamptz
);

-- One edition year can carry more than one volume: the corpus has two
-- publication series (compendium + insecticide-only). Uniqueness is on the
-- volume, not the year.
CREATE UNIQUE INDEX document_edition_source_idx
    ON document (edition_year_be, source);


-- =====================================================================
-- 2. CHUNK — retrievable passages of ACTIVE editions only.
--    Archiving an edition deletes its chunks. The document row, the source
--    PDF in Cloud Storage, and file_hash together make re-ingestion
--    reproducible, so the audit trail lives at the document level and this
--    table stays a pure vector store.
-- =====================================================================

CREATE TABLE chunk (
    langchain_id       uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    content            text        NOT NULL,
    embedding          vector(768) NOT NULL,
    document_id        uuid        NOT NULL
                       REFERENCES document (document_id) ON DELETE CASCADE,
    page_number        int,
    section            text,
    content_sha256     char(64)    NOT NULL,
    langchain_metadata jsonb,      -- jsonb, not json: GIN has no json opclass
    content_tsv        tsvector,   -- sparse half of hybrid retrieval
    created_at         timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX chunk_embedding_idx
    ON chunk USING hnsw (embedding vector_cosine_ops);

CREATE INDEX chunk_document_idx ON chunk (document_id);

-- Unique within a document only: ~48% of 2568's prose is byte-identical to
-- 2565's after normalization. Only one edition is live at a time now, but
-- keep the scope correct in case that ever changes.
CREATE UNIQUE INDEX chunk_dedup_idx ON chunk (document_id, content_sha256);

CREATE INDEX chunk_metadata_idx ON chunk USING gin (langchain_metadata);

-- content_tsv is populated by the ingest pipeline from PyThaiNLP newmm
-- output, not by a generated column: Postgres has no Thai word-boundary
-- parser, so tokenization happens in Python and lands here pre-segmented.
CREATE INDEX chunk_tsv_idx ON chunk USING gin (content_tsv);


-- =====================================================================
-- 3. CITATION JOIN
--    Not the retrieval path — retrieval goes through LangChain against
--    `chunk` directly. This is for eval, debugging, and citation payloads.
-- =====================================================================

CREATE VIEW chunk_with_document AS
SELECT
    c.langchain_id AS chunk_id,
    c.content,
    c.page_number,
    c.section,
    c.langchain_metadata,
    d.document_id,
    d.edition_year_be,
    d.title_th,
    d.source,
    d.status
FROM chunk c
JOIN document d USING (document_id);


-- =====================================================================
-- 4. PROMOTION — run after ingesting a new edition.
--
--    Coverage rule: a document is archived when every scope it covers is
--    covered by at least one newer document. This is union coverage across
--    newer editions, not single-document superset — three newer
--    single-scope volumes together retire one older compendium.
-- =====================================================================

CREATE FUNCTION promote_current_edition() RETURNS void
LANGUAGE plpgsql AS $$
BEGIN
    UPDATE document d
    SET    status      = 'archived',
           archived_at = now()
    WHERE  d.status = 'active'
      AND  NOT EXISTS (
          SELECT 1
          FROM   unnest(d.scopes) AS u(scope)
          WHERE  NOT EXISTS (
              SELECT 1 FROM document n
              WHERE  n.status IN ('active','archived')
                AND  n.edition_year_be > d.edition_year_be
                AND  u.scope = ANY (n.scopes)
          )
      );

    -- Chunks of archived editions are removed outright. There is no
    -- filtered-but-present state, so no post-filtering problem in ANN
    -- search and nothing to get wrong at query time.
    DELETE FROM chunk c
    USING  document d
    WHERE  c.document_id = d.document_id
      AND  d.status <> 'active';
END;
$$;


-- Health check: must always return zero rows.
-- LangChain owns retrieval and cannot see document.status, so the whole
-- guarantee is that non-active editions have no chunks at all. Assert this
-- in the test suite after every promote — it is the enforcement mechanism,
-- not a nicety.
CREATE VIEW assert_no_stale_chunks AS
SELECT d.document_id, d.edition_year_be, d.status, count(*) AS chunk_count
FROM   chunk c
JOIN   document d USING (document_id)
WHERE  d.status <> 'active'
GROUP  BY d.document_id, d.edition_year_be, d.status;