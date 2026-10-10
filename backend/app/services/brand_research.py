import asyncio
from dataclasses import dataclass
from datetime import date
from typing import Any

from tavily import AsyncTavilyClient

from app.core.config import settings
from app.schemas.brand import BrandInfoResponse, ResearchSection, Source


@dataclass(frozen=True)
class ResearchQuery:
    title: str
    query: str
    topic: str = "general"
    time_range: str | None = None


def _queries(brand: str) -> list[ResearchQuery]:
    month = date.today().strftime("%B %Y")
    return [
        ResearchQuery(
            "Brand overview",
            f"{brand} brand overview: company, products, target audience, positioning",
        ),
        ResearchQuery(
            "Marketing & campaigns",
            f"{brand} recent marketing campaigns, advertising, influencer and social media activity",
        ),
        ResearchQuery("Recent news", f"{brand} latest news", topic="news", time_range="month"),
        # Tentpole moments (big sales, festive campaigns, launches) the plan should be built around.
        ResearchQuery(
            "Upcoming moments",
            f"{brand} ongoing or upcoming sale event, festive campaign, product launch or big event {month}",
            topic="news",
            time_range="month",
        ),
    ]


async def _search(client: AsyncTavilyClient, q: ResearchQuery) -> ResearchSection:
    params: dict[str, Any] = {
        "search_depth": "advanced",
        "topic": q.topic,
        "max_results": 5,
        "include_answer": "advanced",
    }
    if q.time_range:
        params["time_range"] = q.time_range
    if q.topic == "general" and settings.research_country:
        params["country"] = settings.research_country

    result = await client.search(q.query, **params)
    return ResearchSection(
        title=q.title,
        summary=result.get("answer") or "No summary found.",
        sources=[
            Source(title=r.get("title", ""), url=r["url"], snippet=r.get("content", ""))
            for r in result.get("results", [])
        ],
    )


async def research_brand(brand: str) -> BrandInfoResponse:
    client = AsyncTavilyClient(api_key=settings.tavily_api_key)
    sections = await asyncio.gather(*(_search(client, q) for q in _queries(brand)))
    message = "\n\n".join(f"{s.title}\n{s.summary}" for s in sections)
    return BrandInfoResponse(brand=brand, message=message, sections=list(sections))
