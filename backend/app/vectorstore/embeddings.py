from chromadb.utils.embedding_functions import DefaultEmbeddingFunction
from langchain_core.embeddings import Embeddings


class ChromaDefaultEmbeddings(Embeddings):
    """LangChain wrapper around Chroma's bundled all-MiniLM-L6-v2 ONNX model.

    Runs locally on CPU with no API key. The model (~80MB) is downloaded to
    ~/.cache/chroma on first use.
    """

    def __init__(self) -> None:
        self._fn = DefaultEmbeddingFunction()

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [vector.tolist() for vector in self._fn(texts)]

    def embed_query(self, text: str) -> list[float]:
        return self.embed_documents([text])[0]
