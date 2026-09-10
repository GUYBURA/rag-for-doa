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

# Vertex's own limit, hit for real ingesting 2565.pdf (423 chunks in one call):
# "batchSize value of 423 but the supported range is from 1 (inclusive) to 251
# (exclusive)". 200 leaves margin below that ceiling.
MAX_BATCH = 200


def _client() -> OpenAI:
    return OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=os.environ["OPENROUTER_API_KEY"],
    )


def embed_texts(texts: list[str], *, model: str = EMBEDDING_MODEL) -> list[list[float]]:
    """Embed a batch of already-chunked, already-normalized text.

    Split into requests of at most MAX_BATCH: a full volume produces well
    over 250 chunks (2565.pdf alone made 423), and the API rejects a request
    that large outright rather than truncating it. Order is preserved, since
    the caller zips this return value against the same chunk list positionally.

    Raises if any vector comes back the wrong length. chunk.embedding is
    vector(768) NOT NULL: a silently-wrong dimension would either be rejected
    by Postgres mid-insert, or -- if it happened to match by coincidence --
    pinned into document.embedding_model as though the model had worked, and
    every later query against it would return nonsense.
    """
    if not texts:
        return []

    client = _client()
    vectors: list[list[float]] = []

    for start in range(0, len(texts), MAX_BATCH):
        batch = texts[start : start + MAX_BATCH]
        response = client.embeddings.create(
            model=model, input=batch, dimensions=DIMENSIONS
        )
        vectors.extend(item.embedding for item in response.data)

    for vector in vectors:
        if len(vector) != DIMENSIONS:
            raise ValueError(
                f"{model} returned a {len(vector)}-dimensional vector, "
                f"expected {DIMENSIONS}"
            )

    return vectors
