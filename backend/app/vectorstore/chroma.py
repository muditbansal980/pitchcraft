from functools import lru_cache

from langchain_chroma import Chroma

from app.core.config import settings
from app.vectorstore.embeddings import ChromaDefaultEmbeddings


@lru_cache
def _store(collection_name: str) -> Chroma:
    return Chroma(
        collection_name=collection_name,
        embedding_function=ChromaDefaultEmbeddings(),
        persist_directory=settings.chroma_persist_dir,
        collection_metadata={"hnsw:space": "cosine"},
    )


def get_vector_store() -> Chroma:
    return _store(settings.chroma_collection)


def get_services_store() -> Chroma:
    return _store(settings.chroma_services_collection)
