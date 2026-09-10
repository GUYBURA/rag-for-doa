import os

import pytest

from ingest.embed import DIMENSIONS, embed_texts

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
