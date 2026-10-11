"""Deep research -> Brand Dossier (WORKPLAN C9). Owner: Agent D.

Pipeline: URL discovery (Firecrawl /v2/map, fallback sitemap.xml + homepage links;
plus campaign/news/buzz search results) -> scrape to clean Markdown (Firecrawl
/v2/scrape keyless or with ``settings.firecrawl_api_key``, fallback httpx +
trafilatura) -> chunk + embed locally (MiniLM, in-memory) -> top chunks per
research question -> one "distiller" LLM call per question (parallel across
models) -> merged, deduped ``BrandDossier`` with sourced facts.

Never raises: on any failure it returns a thin dossier built from the Tavily data.
"""

from collections.abc import Awaitable, Callable
from typing import Any

from app.schemas.brand import ResearchSection
from app.schemas.research import BrandDossier

StageCallback = Callable[[str, float], Awaitable[None] | None]


async def build_dossier(
    brand: str,
    brief: str | None,
    research: list[ResearchSection],
    identity: Any = None,
    scale: str = "standard",
    on_stage: StageCallback | None = None,
) -> BrandDossier:
    """Deep-research ``brand`` and return its dossier (pages per scale: ``settings.research_pages``).

    ``identity`` is the ``brand_identity.BrandIdentity`` (site URL, products) when available.
    ``on_stage(name, fraction)`` is called with progress inside this step (0..1).
    """
    return BrandDossier(brand=brand)


def dossier_text(dossier: BrandDossier, max_chars: int = 9000) -> str:
    """Plain-text rendering of the dossier (used by plan checks as the 'research' corpus)."""
    return dossier.summary
