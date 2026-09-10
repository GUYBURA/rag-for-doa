"""Embeddings via OpenRouter. document.embedding_model is pinned per document,
so changing this constant means re-embedding the active set, not an in-place
swap -- see CLAUDE.md.

Not LangChain: LangChain owns retrieval only, and PGVectorStore consumes
ready-made vectors, it does not compute them.

OpenRouter rather than calling Vertex directly: Vertex is one of the providers
OpenRouter routes this model to, so the vector space is the same either way and
a later move to calling Vertex directly needs no re-embedding, just a base_url
and credential swap.
"""

import os

from openai import OpenAI

EMBEDDING_MODEL = "google/gemini-embedding-001"
DIMENSIONS = 768


def _client() -> OpenAI:
    return OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=os.environ["OPENROUTER_API_KEY"],
    )


def embed_texts(texts: list[str], *, model: str = EMBEDDING_MODEL) -> list[list[float]]:
    """Embed a batch of already-chunked, already-normalized text.

    Raises if any vector comes back the wrong length. chunk.embedding is
    vector(768) NOT NULL: a silently-wrong dimension would either be rejected
    by Postgres mid-insert, or -- if it happened to match by coincidence --
    pinned into document.embedding_model as though the model had worked, and
    every later query against it would return nonsense.
    """
    if not texts:
        return []

    response = _client().embeddings.create(
        model=model, input=texts, dimensions=DIMENSIONS
    )
    vectors = [item.embedding for item in response.data]

    for vector in vectors:
        if len(vector) != DIMENSIONS:
            raise ValueError(
                f"{model} returned a {len(vector)}-dimensional vector, "
                f"expected {DIMENSIONS}"
            )

    return vectors
