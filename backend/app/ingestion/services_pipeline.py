"""Ingest "Services Offered By WLDD.pdf" into Chroma.

PyMuPDF extraction -> semantic chunking -> local embeddings -> Chroma.
Re-running replaces the previous chunks for the same PDF.

    uv run python -m app.ingestion.services_pipeline [path/to/file.pdf]
"""

import hashlib
import sys
from pathlib import Path

from langchain_core.documents import Document

from app.core.config import settings
from app.ingestion.pdf_loader import load_service_sections
from app.ingestion.semantic_chunker import SemanticChunker
from app.vectorstore import get_services_store


def build_documents(pdf_path: Path, chunker: SemanticChunker) -> list[Document]:
    sections = load_service_sections(pdf_path)
    # Paragraphs (a description or one bullet) are the smallest unit, so a bullet's
    # label never gets separated from its explanation.
    groups = [section.paragraphs for section in sections]
    documents: list[Document] = []
    for section, chunks in zip(sections, chunker.chunk_groups(groups)):
        for index, chunk in enumerate(chunks):
            documents.append(
                Document(
                    # Prefix the service name so each chunk is self-describing at retrieval time.
                    page_content=f"Service: {section.name}\n{chunk}",
                    metadata={
                        "source": pdf_path.name,
                        "page": section.page,
                        "service_number": section.number,
                        "service": section.name,
                        "chunk_index": index,
                    },
                )
            )
    return documents


def _chunk_id(doc: Document) -> str:
    key = f"{doc.metadata['source']}:{doc.metadata['service_number']}:{doc.metadata['chunk_index']}"
    return hashlib.sha1(key.encode()).hexdigest()


def run(pdf_path: Path) -> list[Document]:
    store = get_services_store()
    documents = build_documents(pdf_path, SemanticChunker(store.embeddings))

    existing = store.get(where={"source": pdf_path.name}, include=[])["ids"]
    if existing:
        store.delete(ids=existing)
    store.add_documents(documents, ids=[_chunk_id(doc) for doc in documents])
    return documents


def main() -> None:
    pdf_path = Path(sys.argv[1] if len(sys.argv) > 1 else settings.services_pdf_path)
    if not pdf_path.is_file():
        sys.exit(f"PDF not found: {pdf_path}")

    documents = run(pdf_path)
    services = {doc.metadata["service"] for doc in documents}
    print(
        f"Stored {len(documents)} chunks from {len(services)} services in "
        f"'{settings.chroma_services_collection}' ({settings.chroma_persist_dir})"
    )
    for doc in documents:
        meta = doc.metadata
        print(f"  [{meta['service_number']:>2}.{meta['chunk_index']}] {len(doc.page_content):>4} chars  {meta['service']}")


if __name__ == "__main__":
    main()
