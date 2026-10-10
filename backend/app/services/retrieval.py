"""Retrieval over the two Chroma collections.

- Services (contract C2): the catalog is tiny (~11 services), so we load all of
  it and give the LLM the full list. That way it can only recommend services
  WLDD actually offers.
- Past pitches (contract C1): similarity search with short queries built from
  the brand research, grouped per pitch so the prompt gets a few whole pitches
  (brief + best excerpts) instead of scattered chunks.

Everything here is sync (Chroma + a local CPU embedding model); call it from
async code via ``asyncio.to_thread``.
"""

import logging
from collections import defaultdict
from dataclasses import dataclass, field

from app.schemas.brand import ResearchSection
from app.vectorstore.chroma import get_pitches_store, get_services_store

logger = logging.getLogger(__name__)

# "MISCL (Miscellaneous)" is a budget line for travel/lodging, not a service.
_EXCLUDED_SERVICE_PREFIXES = ("MISCL",)

# all-MiniLM-L6-v2 truncates at 256 tokens (~1,000 chars); stay well under.
_MAX_QUERY_CHARS = 700
# Cosine distance above this is treated as "not related" (MiniLM scale).
_MAX_PITCH_DISTANCE = 0.85


@dataclass(frozen=True)
class Service:
    number: int
    name: str
    description: str


@dataclass
class PitchMatch:
    pitch_id: str
    campaign: str
    client: str
    services: str
    distance: float
    brief: str | None = None
    excerpts: list[str] = field(default_factory=list)


def load_services_catalog() -> list[Service]:
    """Reassemble every service from its chunks, sorted by (service_number, chunk_index)."""
    data = get_services_store()._collection.get(include=["documents", "metadatas"])
    chunks: dict[tuple[int, str], list[tuple[int, str]]] = defaultdict(list)
    for doc, meta in zip(data["documents"] or [], data["metadatas"] or []):
        name = str(meta.get("service", "")).strip()
        if not name or name.startswith(_EXCLUDED_SERVICE_PREFIXES):
            continue
        number = int(meta.get("service_number", 0))
        text = _strip_header(doc, f"Service: {name}")
        chunks[(number, name)].append((int(meta.get("chunk_index", 0)), text))

    return [
        Service(
            number=number,
            name=name,
            description=" ".join(text for _, text in sorted(parts)),
        )
        for (number, name), parts in sorted(chunks.items())
    ]


def build_pitch_queries(brand: str, research: list[ResearchSection]) -> list[str]:
    """One short query per research angle (overview, marketing) prefixed with the brand."""
    by_title = {s.title.lower(): s for s in research}
    queries = []
    for key in ("brand overview", "marketing & campaigns"):
        section = by_title.get(key)
        if section and section.summary and section.summary != "No summary found.":
            queries.append(clip_text(f"{brand}: {section.summary}", _MAX_QUERY_CHARS))
    if not queries:
        queries.append(f"{brand} brand marketing campaign")
    return queries


def find_similar_pitches(queries: list[str], k: int = 25, top_n: int = 3) -> list[PitchMatch]:
    """Top past pitches for the queries, each with its brief and best 1-2 deck excerpts.

    Searches briefs and deck chunks together (deck chunks carry most of the signal;
    some briefs are short or truncated). One deck often fills many of the top-k
    slots, so k is generous and results are grouped per pitch_id, ranked by each
    pitch's best (lowest) cosine distance. Briefs of selected pitches that were not
    among the hits are fetched by pitch_id.
    """
    store = get_pitches_store()
    if store._collection.count() == 0:
        logger.warning("Pitches collection is empty; planning without past pitches")
        return []

    pitches: dict[str, PitchMatch] = {}
    excerpts: dict[str, list[tuple[float, str]]] = defaultdict(list)
    for query in queries:
        for doc, distance in store.similarity_search_with_score(query, k=k):
            meta = doc.metadata
            pitch_id = str(meta.get("pitch_id", "")).strip()
            if not pitch_id or distance > _MAX_PITCH_DISTANCE:
                continue
            match = pitches.get(pitch_id)
            if match is None:
                match = pitches[pitch_id] = PitchMatch(
                    pitch_id=pitch_id,
                    campaign=str(meta.get("campaign", "")),
                    client=str(meta.get("client", "")),
                    services=str(meta.get("services", "")),
                    distance=distance,
                )
            match.distance = min(match.distance, distance)
            header = f"Past pitch {pitch_id}: {match.campaign} ({match.client})"
            body = _strip_header(doc.page_content, header)
            if meta.get("kind") == "brief":
                match.brief = _brief_text(body)
            elif body and body not in (text for _, text in excerpts[pitch_id]):
                excerpts[pitch_id].append((distance, body))

    top = sorted(pitches.values(), key=lambda m: m.distance)[:top_n]
    for match in top:
        match.excerpts = [text for _, text in sorted(excerpts[match.pitch_id])[:2]]
        if match.brief is None:
            match.brief = _fetch_brief(store, match.pitch_id)
    return top


def _fetch_brief(store, pitch_id: str) -> str | None:
    data = store._collection.get(
        where={"$and": [{"pitch_id": pitch_id}, {"kind": "brief"}]},
        include=["documents"],
        limit=1,
    )
    docs = data.get("documents") or []
    return _brief_text(docs[0]) if docs else None


def _brief_text(content: str) -> str:
    """Brief docs read 'Past pitch ...\\nServices: ...\\nBrief: ...'; keep the brief part."""
    _, sep, brief = content.partition("Brief:")
    return (brief if sep else content).strip()


def _strip_header(text: str, header: str) -> str:
    text = text.strip()
    if text.startswith(header):
        text = text[len(header):]
    return text.strip()


def clip_text(text: str, limit: int) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0].rstrip(",;:") + "…"
