"""LangChain's Embeddings interface, wrapped around ingest/embed.py.

The adapter lives here and not in ingest/embed.py because CLAUDE.md scopes
LangChain to the retrieval path. PGVectorStore needs an Embeddings object to
turn a question into a vector; the ingest path has to keep producing its
vectors without importing LangChain at all.

Nothing is reimplemented. The model id, the dimension and the batch limit all
come from ingest.embed, so a query vector and the vectors it is compared
against cannot be produced by two different pieces of code.
"""

import psycopg
from langchain_core.embeddings import Embeddings

from ingest.embed import EMBEDDING_MODEL, embed_texts


class OpenRouterEmbeddings(Embeddings):
    """Query-side embeddings. Same model, same dimension, same client."""

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return embed_texts(texts)

    def embed_query(self, text: str) -> list[float]:
        return embed_texts([text])[0]


def assert_model_matches_corpus(conn: psycopg.Connection) -> None:
    """Raise unless every active document was embedded by EMBEDDING_MODEL.

    Cosine distance between vectors from two different models is a
    well-defined number with no meaning. Nothing else catches this: the schema
    only constrains the dimension, and two models that both emit 768 floats
    insert and query without a single error while ranking passages at random.

    document.embedding_model is pinned per document precisely so this is
    checkable, so check it -- at startup, once, rather than discovering it from
    answer quality. An empty corpus passes: there is nothing yet to disagree
    with.
    """
    rows = conn.execute(
        "SELECT DISTINCT embedding_model FROM document WHERE status = 'active'"
    ).fetchall()
    models = {row[0] for row in rows}

    if models - {EMBEDDING_MODEL}:
        found = ", ".join(sorted(str(m) for m in models))
        raise ValueError(
            f"active documents were embedded by {{{found}}}, but queries are "
            f"embedded by {EMBEDDING_MODEL}. Re-embed the active set or pin "
            f"the query side back -- do not mix vector spaces."
        )
