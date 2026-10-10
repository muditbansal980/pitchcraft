"""Ingest the past-pitches archive (past_pitches.csv + decks/*.pdf) into Chroma.

Per CSV row: one ``brief`` document plus ``deck`` chunks from the row's PDF.
PyMuPDF extraction -> slide-text cleanup -> recursive character chunking
(<= 900 chars incl. prefix) -> local embeddings -> Chroma.
Re-running replaces every previous brief/deck document in the collection.

    uv run python -m app.ingestion.pitches_pipeline [path/to/archive_dir]
"""

import csv
import hashlib
import re
import sys
import unicodedata
from pathlib import Path

import pymupdf
from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter

from app.core.config import settings
from app.vectorstore import get_pitches_store

CSV_NAME = "past_pitches.csv"
KINDS = ["brief", "deck"]

# all-MiniLM-L6-v2 truncates at 256 tokens, so the whole page_content (prefix + chunk) stays <= 900 chars.
MAX_CHUNK_CHARS = 900
CHUNK_OVERLAP = 120
# Shorter chunks are slide leftovers ("SAMPLE CAROUSEL REFERENCES") that only add retrieval noise.
MIN_CHUNK_CHARS = 80
MIN_CHUNK_WORDS = 10

BULLETS = "●•▪◦○■-–*"
# Lines that carry no content: slide numbers, "SLIDE - 2", stray "see,", "x"/"X" lockups, symbols.
NOISE_RE = re.compile(r"^(?:\d{1,3}|slide\s*-?\s*\d+|see,?|x|[^\w]+)$", re.IGNORECASE)
LETTER_SPACED_RE = re.compile(r"(?<!\S)(?:\w ){3,}\w(?!\S)")
ENUM_RE = re.compile(r"^\d{1,2}[.)]$")


def _clean_line(line: str) -> str:
    # NFKC folds ligatures ("\ufb02" -> "fl"); Google Slides exports add zero-width and soft-hyphen characters.
    line = unicodedata.normalize("NFKC", line)
    line = re.sub(r"[\u200b\u200c\u200d\ufeff\u00ad]", "", line)
    # Emoji and UI glyphs ("\u2665 Like \u27a4 Share") carry no meaning for retrieval.
    line = "".join(c for c in line if unicodedata.category(c) != "So")
    # Letter-spaced display text ("S A M P L E N a r r a t i v e") embeds as noise.
    line = LETTER_SPACED_RE.sub(" ", line)
    return re.sub(r"\s+", " ", line).strip()


def _is_heading(line: str) -> bool:
    letters = [c for c in line if c.isalpha()]
    return len(line) <= 60 and len(letters) >= 2 and sum(c.isupper() for c in letters) / len(letters) >= 0.8


def clean_page(text: str) -> str:
    """Turn one slide's raw text into paragraphs, one per line.

    Slides wrap sentences across short lines, so consecutive body lines are
    joined into a paragraph. ALL-CAPS headings, bullets and numbered items
    start a new paragraph; noise lines and repeated lines are dropped.
    """
    paragraphs: list[str] = []
    current: list[str] = []
    current_is_heading = False
    pending_prefix = ""
    previous = ""

    def flush() -> None:
        if current:
            paragraphs.append(" ".join(current))
        current.clear()

    for raw in text.splitlines():
        line = _clean_line(raw)
        if not line or line == previous:
            continue
        previous = line
        if line in BULLETS:
            flush()
            continue
        if ENUM_RE.match(line):
            flush()
            pending_prefix = line + " "
            continue
        if NOISE_RE.match(line):
            continue

        starts_item = line[0] in BULLETS and len(line) > 1 and line[1] == " "
        if starts_item:
            line = line[2:].strip()
        heading = _is_heading(line)
        if pending_prefix:
            line, pending_prefix, starts_item = pending_prefix + line, "", True

        if starts_item or not current or heading != current_is_heading:
            flush()
            current.append(line)
            current_is_heading = heading and not starts_item
        elif current[-1].endswith("-") and not current[-1].endswith(" -") and line[0].islower():
            # Word broken across lines: "engage-" + "ment".
            current[-1] = current[-1][:-1] + line
        else:
            current.append(line)
    flush()

    # A heading followed by its body reads better as "HEADING: body".
    merged: list[str] = []
    for paragraph in paragraphs:
        if merged and _is_heading(merged[-1]) and not _is_heading(paragraph) and merged[-1][-1] not in ".!?…":
            separator = " " if merged[-1].endswith(":") else ": "
            merged[-1] = f"{merged[-1]}{separator}{paragraph}"
        else:
            merged.append(paragraph)
    return "\n".join(merged)


def load_deck_text(pdf_path: Path) -> str:
    """Cleaned deck text; slides separated by blank lines, empty slides skipped."""
    with pymupdf.open(pdf_path) as doc:
        pages = [clean_page(page.get_text()) for page in doc]
    return "\n\n".join(page for page in pages if page)


def _is_useful(chunk: str) -> bool:
    words = re.findall(r"[A-Za-z]{2,}", chunk)
    return len(chunk) >= MIN_CHUNK_CHARS and len(words) >= MIN_CHUNK_WORDS


def chunk_deck(text: str, prefix: str) -> list[str]:
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=MAX_CHUNK_CHARS - len(prefix),
        chunk_overlap=CHUNK_OVERLAP,
        separators=["\n\n", "\n", ". ", "? ", "! ", "; ", ", ", " ", ""],
        keep_separator="end",
    )
    chunks = (re.sub(r"\n{2,}", "\n", chunk).strip() for chunk in splitter.split_text(text))
    return [chunk for chunk in chunks if _is_useful(chunk)]


def _services(raw: str) -> str:
    return "; ".join(part.strip() for part in raw.split(";") if part.strip())


def build_documents(archive_dir: Path) -> list[Document]:
    documents: list[Document] = []
    with (archive_dir / CSV_NAME).open(encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))

    for row in rows:
        pitch_id, campaign, client = row["pitch_id"].strip(), row["campaign"].strip(), row["client"].strip()
        services = _services(row["services_pitched"])
        header = f"Past pitch {pitch_id}: {campaign} ({client})"
        base = {"pitch_id": pitch_id, "campaign": campaign, "client": client, "services": services}

        documents.append(
            Document(
                page_content=f"{header}\nServices: {services}\nBrief: {row['brief'].strip()}",
                metadata={**base, "source": CSV_NAME, "kind": "brief", "chunk_index": 0},
            )
        )

        deck_path = archive_dir / row["document"].strip()
        if not deck_path.is_file():
            print(f"  warning: {pitch_id} deck not found: {deck_path}", file=sys.stderr)
            continue
        for index, chunk in enumerate(chunk_deck(load_deck_text(deck_path), header + "\n")):
            documents.append(
                Document(
                    # Prefix the pitch so each chunk is self-describing at retrieval time.
                    page_content=f"{header}\n{chunk}",
                    metadata={**base, "source": deck_path.name, "kind": "deck", "chunk_index": index},
                )
            )
    return documents


def _doc_id(doc: Document) -> str:
    meta = doc.metadata
    key = f"{meta['source']}:{meta['pitch_id']}:{meta['kind']}:{meta['chunk_index']}"
    return hashlib.sha1(key.encode()).hexdigest()


def run(archive_dir: Path) -> list[Document]:
    store = get_pitches_store()
    documents = build_documents(archive_dir)

    # The collection holds only this archive, so drop every brief/deck doc
    # (also removes pitches that were deleted from the CSV since the last run).
    existing = store.get(where={"kind": {"$in": KINDS}}, include=[])["ids"]
    if existing:
        store.delete(ids=existing)
    store.add_documents(documents, ids=[_doc_id(doc) for doc in documents])
    return documents


def main() -> None:
    archive_dir = Path(sys.argv[1] if len(sys.argv) > 1 else settings.archive_dir)
    if not (archive_dir / CSV_NAME).is_file():
        sys.exit(f"{CSV_NAME} not found in: {archive_dir}")

    documents = run(archive_dir)
    briefs = [doc for doc in documents if doc.metadata["kind"] == "brief"]
    print(
        f"Stored {len(documents)} docs ({len(briefs)} briefs, {len(documents) - len(briefs)} deck chunks) "
        f"in '{settings.chroma_collection}' ({settings.chroma_persist_dir})"
    )
    for brief in briefs:
        meta = brief.metadata
        chunks = [doc for doc in documents if doc.metadata["kind"] == "deck" and doc.metadata["pitch_id"] == meta["pitch_id"]]
        longest = max((len(doc.page_content) for doc in chunks), default=0)
        print(f"  {meta['pitch_id']}  {len(chunks):>2} deck chunks (max {longest:>3} chars)  {meta['campaign']}")


if __name__ == "__main__":
    main()
