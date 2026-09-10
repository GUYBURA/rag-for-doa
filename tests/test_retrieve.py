"""Hybrid retrieval against a real container with a real ingested document.

The corpus here is tests/fixtures/excerpt.pdf: 9 chunks, three of them dosage
tables for three different crops. Nine chunks is fewer than CANDIDATES, so
every search returns all of them and membership proves nothing -- these tests
assert on *rank*, which is the only thing retrieval actually decides.
"""

import hashlib
import uuid

import pytest
from langchain_postgres.v2.hybrid_search_config import HybridSearchConfig

from ingest.db import insert_pending_document
from ingest.embed import EMBEDDING_MODEL
from query.embeddings import assert_model_matches_corpus
from query.retrieve import (
    TSV_COLUMN,
    Passage,
    assert_hybrid_enabled,
    async_url,
    fts_query,
    hybrid_config,
    make_store,
    retrieve,
)

CASSAVA = "มันสำปะหลัง (Cassava)"
MUNG_BEAN = "ถั่วเขียว (Mung beans)"
SOYBEAN = "ถั่วเหลือง (Soybean)"

PASSING_QA = {"no_thai_consonants": {"passed": True, "measured": 5, "threshold": None}}


@pytest.fixture(scope="session")
def store(ingested_corpus):
    """One store per session. create_sync() costs a round trip and an engine."""
    return make_store(ingested_corpus)


# --------------------------------------------------------------------------
# Pure, no container, no key. These run in CI.
# --------------------------------------------------------------------------


def test_async_url_names_the_driver_in_the_scheme():
    # A bare postgresql:// URL loads the sync psycopg2 dialect under
    # create_async_engine and fails at connect time, not at parse time.
    assert async_url("postgresql://u:p@h:5432/db") == "postgresql+asyncpg://u:p@h:5432/db"
    assert async_url("postgres://u:p@h:5432/db") == "postgresql+asyncpg://u:p@h:5432/db"
    assert (
        async_url("postgresql+asyncpg://u:p@h:5432/db")
        == "postgresql+asyncpg://u:p@h:5432/db"
    )


def test_async_url_refuses_something_that_is_not_a_postgres_dsn():
    with pytest.raises(ValueError):
        async_url("mysql://u:p@h/db")


def test_fts_query_splits_thai_into_more_than_one_token():
    # The whole point. Thai has no spaces, so an unsegmented question reaches
    # plainto_tsquery as a single token that matches nothing in a tsvector
    # built from newmm output.
    question = "โรคราสนิมถั่วเหลือง"
    tokens = fts_query(question)

    assert " " in tokens
    assert tokens.replace(" ", "") == question


def test_hybrid_config_is_a_new_object_every_call():
    # The store writes the question into fts_query when it is empty, and the
    # value sticks to the object. Sharing one config means every question
    # after the first is searched lexically for the first question's words.
    first = hybrid_config(tokens="ก ข", top_k=5)
    second = hybrid_config(tokens="ค ง", top_k=5)

    assert first is not second
    assert first.fusion_function_parameters is not second.fusion_function_parameters
    assert second.fts_query == "ค ง"


def test_hybrid_config_pins_the_simple_text_search_configuration():
    # content_tsv was built with to_tsvector('simple', ...). The library
    # defaults to english, which would stem and stopword the query but not the
    # indexed text.
    assert hybrid_config(tokens="ก", top_k=5).tsv_lang == "simple"


def test_a_blanked_tsv_column_is_an_error_not_a_silent_downgrade():
    blanked = HybridSearchConfig(tsv_column="", tsv_lang="simple")
    with pytest.raises(RuntimeError):
        assert_hybrid_enabled(blanked)

    assert_hybrid_enabled(HybridSearchConfig(tsv_column=TSV_COLUMN, tsv_lang="simple"))


# --------------------------------------------------------------------------
# Model pinning. Container, but no key: no embedding call happens.
# --------------------------------------------------------------------------


def _document_with_model(conn, model: str) -> uuid.UUID:
    document_id = insert_pending_document(
        conn,
        source=f"{model}.pdf",
        title_th="x",
        edition_year_be=2568,
        scopes=["fungicide"],
        file_hash=hashlib.sha256(model.encode("utf-8")).hexdigest(),
        page_count=1,
        parser="pymupdf",
        qa=PASSING_QA,
    )
    conn.execute(
        "UPDATE document SET status = 'active', embedding_model = %s "
        "WHERE document_id = %s",
        (model, document_id),
    )
    return document_id


def test_a_corpus_embedded_by_another_model_is_refused(db_conn):
    # Nothing else catches this. Two models that both emit 768 floats insert
    # and query without an error while ranking passages at random.
    _document_with_model(db_conn, "some/other-embedding-model")

    with pytest.raises(ValueError, match="vector spaces"):
        assert_model_matches_corpus(db_conn)


def test_a_corpus_embedded_by_the_pinned_model_passes(db_conn):
    _document_with_model(db_conn, EMBEDDING_MODEL)
    assert_model_matches_corpus(db_conn)


def test_an_empty_corpus_passes(db_conn):
    # Nothing to disagree with yet. Failing here would make a fresh database
    # unusable before its first ingest.
    assert_model_matches_corpus(db_conn)


# --------------------------------------------------------------------------
# Real retrieval. Needs the key: every question is embedded for real.
# --------------------------------------------------------------------------


@pytest.mark.requires_embeddings
def test_the_matching_crop_table_ranks_first(store, corpus_conn):
    # Page 4 carries three dosage tables for three crops. Retrieval has to
    # pick the right one, not merely return all three.
    passages = retrieve(store, corpus_conn, "โรคราสนิมในถั่วเหลือง", k=5)

    assert passages
    assert passages[0].section == SOYBEAN


@pytest.mark.requires_embeddings
def test_a_different_crop_ranks_a_different_table_first(store, corpus_conn):
    # The twin of the test above. Without it, an implementation that always
    # returns the same table would pass.
    passages = retrieve(store, corpus_conn, "โรคเน่าดำในถั่วเขียว", k=5)

    assert passages
    assert passages[0].section == MUNG_BEAN


@pytest.mark.requires_embeddings
def test_a_latin_scientific_name_reaches_the_table_that_contains_it(store, corpus_conn):
    # Colletotrichum appears in exactly one chunk. Latin-script chemical and
    # pathogen names are what the lexical half exists for -- dense embeddings
    # of a Thai corpus handle them poorly.
    passages = retrieve(store, corpus_conn, "Colletotrichum", k=3)

    assert passages
    assert passages[0].section == CASSAVA


@pytest.mark.requires_embeddings
def test_every_passage_carries_what_a_citation_needs(store, corpus_conn):
    # Invariant 10. edition_year_be, title_th and source live on `document`,
    # which the vector store never reads.
    passages = retrieve(store, corpus_conn, "การเก็บรักษาสารป้องกันกำจัดศัตรูพืช", k=5)

    assert passages
    for passage in passages:
        assert isinstance(passage, Passage)
        assert passage.edition_year_be == 2568
        assert passage.title_th
        assert passage.source == "excerpt.pdf"
        assert passage.page_number is not None
        assert isinstance(passage.chunk_id, uuid.UUID)


@pytest.mark.requires_embeddings
def test_ranks_are_dense_and_start_at_one(store, corpus_conn):
    passages = retrieve(store, corpus_conn, "การปฐมพยาบาล", k=5)

    assert [p.rank for p in passages] == list(range(1, len(passages) + 1))


@pytest.mark.requires_embeddings
def test_the_second_question_is_not_searched_with_the_first_questions_words(
    store, corpus_conn, monkeypatch
):
    # The sticky-config trap, at the boundary where it would actually bite.
    # An implementation that builds one config and reuses it passes every
    # other test in this file: the dense half still tracks the new question,
    # so the results stay plausible while the lexical half answers the old one.
    seen: list[str] = []
    original = store.similarity_search_with_score

    def spy(query, k=None, filter=None, **kwargs):
        seen.append(kwargs["hybrid_search_config"].fts_query)
        return original(query, k, filter, **kwargs)

    monkeypatch.setattr(store, "similarity_search_with_score", spy)

    retrieve(store, corpus_conn, "โรคราสนิมในถั่วเหลือง", k=3)
    retrieve(store, corpus_conn, "Colletotrichum", k=3)

    assert len(seen) == 2
    assert seen[0] == fts_query("โรคราสนิมในถั่วเหลือง")
    assert seen[1] == fts_query("Colletotrichum")


def test_an_empty_question_never_reaches_the_store_or_the_database():
    # Neither a store nor a connection is passed: if this made either call it
    # would raise, rather than quietly embedding whitespace and returning
    # whatever the index felt was closest to it.
    assert retrieve(None, None, "   ") == []
