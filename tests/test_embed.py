import os

import pytest

from ingest.embed import DIMENSIONS, MAX_BATCH, embed_texts

import ingest.embed


class _FakeItem:
    embedding = [0.0] * DIMENSIONS


class _FakeEmbeddings:
    def __init__(self, calls):
        self._calls = calls

    def create(self, *, model, input, dimensions):
        self._calls.append(len(input))
        return type("Response", (), {"data": [_FakeItem() for _ in input]})()


class _FakeClient:
    def __init__(self, calls):
        self.embeddings = _FakeEmbeddings(calls)

skip_reason = "OPENROUTER_API_KEY not set"
needs_key = pytest.mark.skipif(
    not os.environ.get("OPENROUTER_API_KEY"), reason=skip_reason
)


@pytest.mark.requires_embeddings
@needs_key
def test_embedding_has_the_pinned_dimension():
    vectors = embed_texts(["การใช้สารป้องกันกำจัดศัตรูพืช"])
    assert len(vectors) == 1
    assert len(vectors[0]) == DIMENSIONS


@pytest.mark.requires_embeddings
@needs_key
def test_embedding_a_batch_returns_one_vector_per_text():
    vectors = embed_texts(["ข้อความหนึ่ง", "ข้อความสอง", "ข้อความสาม"])
    assert len(vectors) == 3
    assert all(len(v) == DIMENSIONS for v in vectors)


def test_empty_input_returns_empty_without_calling_the_api():
    # No skipif: this must pass even with no key, because it should never
    # reach the network.
    assert embed_texts([]) == []


def test_a_batch_over_the_limit_is_split_into_multiple_requests(monkeypatch):
    # No skipif and no key needed: the client is faked, so this proves the
    # splitting logic itself, not the API. Discovered for real ingesting
    # 2565.pdf: 423 chunks in one call got "batchSize value of 423 but the
    # supported range is from 1 (inclusive) to 251 (exclusive)".
    calls = []
    monkeypatch.setattr(ingest.embed, "_client", lambda: _FakeClient(calls))

    texts = [f"chunk {i}" for i in range(450)]
    vectors = embed_texts(texts)

    assert len(vectors) == 450
    assert all(len(v) == DIMENSIONS for v in vectors)
    assert calls == [MAX_BATCH, MAX_BATCH, 450 - 2 * MAX_BATCH]
    assert all(c <= MAX_BATCH for c in calls)
