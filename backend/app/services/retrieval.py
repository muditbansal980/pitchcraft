"""Retrieval over the two Chroma collections.

- Services (contract C2): the catalog is tiny (~11 services), so we load all of
  it and give the LLM the full list. That way it can only recommend services
  WLDD actually offers.
- Past pitches (contract C1): similarity search with short queries built from
  the client brief (first, when given) and the brand research, grouped per pitch
  so the prompt gets a few whole pitches (brief + best excerpts) instead of
  scattered chunks.
- Craft excerpts: deck chunks only, spread over several decks, as writing
  examples for the slide writers; ``load_pitch_full_text`` gives a whole deck
  for big-context strategist models.

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


@dataclass(frozen=True)
class CraftExcerpt:
    """A deck chunk used as a writing-craft example (header stripped)."""

    pitch_id: str
    client: str
    campaign: str
    chunk_index: int
    distance: float
    text: str


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


def build_pitch_queries(brand: str, research: list[ResearchSection], brief: str | None = None) -> list[str]:
    """Short similarity queries: the client brief first (when given), then one per research angle."""
    by_title = {s.title.lower(): s for s in research}
    queries = []
    if brief and brief.strip():
        queries.append(clip_text(f"{brand}: {brief}", _MAX_QUERY_CHARS))
    for key in ("brand overview", "marketing & campaigns", "upcoming moments"):
        section = by_title.get(key)
        if section and section.summary and section.summary != "No summary found.":
            queries.append(clip_text(f"{brand}: {section.summary}", _MAX_QUERY_CHARS))
    if not queries:
        queries.append(f"{brand} brand marketing campaign")
    # The brief says what the pitch is about; keep at most one research query per angle after it.
    return queries[:4]


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


def find_craft_excerpts(query: str, max_excerpts: int = 4, k: int = 24, per_pitch: int = 2) -> list[CraftExcerpt]:
    """Deck chunks (never briefs) that show how past decks wrote something like ``query``.

    Spread over pitches (at most ``per_pitch`` chunks from one deck) so writers see
    several voices instead of one deck's slides back to back.
    """
    store = get_pitches_store()
    if store._collection.count() == 0:
        return []
    hits = store.similarity_search_with_score(clip_text(query, _MAX_QUERY_CHARS), k=k, filter={"kind": "deck"})
    picked: list[CraftExcerpt] = []
    counts: dict[str, int] = defaultdict(int)
    for doc, distance in sorted(hits, key=lambda h: h[1]):
        meta = doc.metadata
        pitch_id = str(meta.get("pitch_id", "")).strip()
        if not pitch_id or counts[pitch_id] >= per_pitch:
            continue
        campaign, client = str(meta.get("campaign", "")), str(meta.get("client", ""))
        text = _strip_header(doc.page_content, f"Past pitch {pitch_id}: {campaign} ({client})")
        if len(text) < 80 or _is_repetitive(text):
            continue
        counts[pitch_id] += 1
        picked.append(CraftExcerpt(pitch_id, client, campaign, int(meta.get("chunk_index", 0)), distance, text))
        if len(picked) >= max_excerpts:
            break
    return picked


def _is_repetitive(text: str) -> bool:
    """Deck chunks made of one repeated slide label ("GUESS: ... GUESS: ...") teach nothing."""
    words = text.lower().split()
    return len(words) >= 30 and len(set(words)) / len(words) < 0.45


def load_pitch_full_text(pitch_id: str) -> str:
    """The whole cleaned deck text of one past pitch: deck chunks in chunk_index order, overlap removed."""
    store = get_pitches_store()
    data = store._collection.get(
        where={"$and": [{"pitch_id": pitch_id}, {"kind": "deck"}]},
        include=["documents", "metadatas"],
    )
    parts: list[tuple[int, str]] = []
    for doc, meta in zip(data.get("documents") or [], data.get("metadatas") or []):
        header = f"Past pitch {pitch_id}: {meta.get('campaign', '')} ({meta.get('client', '')})"
        parts.append((int(meta.get("chunk_index", 0)), _strip_header(doc, header)))
    texts: list[str] = []
    for _, text in sorted(parts):
        if texts:
            text = _drop_overlap(texts[-1], text)
        if text:
            texts.append(text)
    return "\n\n".join(texts)


def _drop_overlap(previous: str, text: str, max_overlap: int = 200, min_overlap: int = 20) -> str:
    """Chunks overlap by up to ~120 chars; cut the repeated head of ``text``."""
    for size in range(min(max_overlap, len(previous), len(text)), min_overlap - 1, -1):
        if previous.endswith(text[:size]):
            return text[size:].lstrip()
    return text
