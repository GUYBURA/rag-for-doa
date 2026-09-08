from langchain_ollama import OllamaEmbeddings
from langchain_postgres import PGVector

def embed_documents(chunk_documents: list[dict]) -> list[dict]:
    """
    Generates embeddings for a list of documents using OllamaEmbeddings.
    Args:
        chunk_documents (list[dict]): A list of chunked documents to be embedded.
    """
    embeddings_model = OllamaEmbeddings(
        model="nomic-embed-text-v2-moe:latest",
        dimensions=768
    )

    embedded_documents = []
    for chunk in chunk_documents:
        embedding = embeddings_model.embed_query(chunk["page_content"])
        embedded_documents.append({
            'page_content': chunk["page_content"],
            'embedding': embedding
        })
    return embedded_documents

def store_embeddings(embedded_documents: list[dict], db_connection) -> None:
    """
    Stores the embedded documents in a database.
    Args:
        embedded_documents (list[dict]): A list of embedded documents to be stored.
        db_connection: A database connection object.
    """
    vector_store = PGVector(
        embeddings=embedded_documents,
        connection=db_connection,
        collection_name="chunk"
    )