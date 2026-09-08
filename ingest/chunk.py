from langchain_text_splitters import RecursiveCharacterTextSplitter

def chunk_documents(documents: list[dict], chunk_size: int = 1000, chunk_overlap: int = 0) -> list[dict]:
    """
    Splits documents into smaller chunks using RecursiveCharacterTextSplitter.
    Args:
        documents (list[dict]): A list of documents to be chunked.
        chunk_size (int): The maximum size of each chunk.
        chunk_overlap (int): The number of characters to overlap between chunks.
    Returns:
        list[dict]: A list of chunked documents.
    """
    text_splitter = RecursiveCharacterTextSplitter(
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        length_function=len
    )
    
    chunked_documents = []
    for doc in documents:
        chunks = text_splitter.split_text(doc.page_content)
        for i, chunk in enumerate(chunks):
            chunked_documents.append({
                'page_content': chunk
            })
    
    return chunked_documents

