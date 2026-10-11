"""Deep-research contract (WORKPLAN C9): the Brand Dossier.

Built by ``app.services.research_deep.build_dossier`` from the brand's own
pages (Firecrawl, or the httpx + trafilatura fallback), full news/campaign
articles and the Tavily research, then distilled by the "distiller" LLM role.
Every fact carries the URL it came from; numbers are kept exactly as written in
the source, so the plan checks can accept them as supported.
"""

from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, Field

from app.schemas.deck import Text, _take


class DossierFact(BaseModel):
    id: str = Field(description='Stable id, e.g. "F7"')
    text: Text(300)
    number: Text(40) | None = Field(None, description='The figure as written in the source, e.g. "20 crore", "35%"')
    source_url: str
    source_title: Text(160) = ""


class DossierProduct(BaseModel):
    name: Text(80)
    detail: Text(240) = ""


class DossierCampaign(BaseModel):
    name: Text(100)
    when: Text(40) = ""
    what: Text(300) = ""
    source_url: str | None = None


class DossierCompetitor(BaseModel):
    name: Text(60)
    angle: Text(200) = Field("", description="How they compete / what they do on social")


class DossierSource(BaseModel):
    url: str
    title: Text(160) = ""
    kind: Literal["site", "blog", "news", "campaign", "social", "other"] = "other"
    via: Literal["firecrawl", "fallback", "tavily"] = "fallback"


class BrandDossier(BaseModel):
    brand: Text(200)
    site: str | None = None
    summary: Text(1200) = ""
    audience: Annotated[list[Text(200)], BeforeValidator(_take(8))] = Field(default_factory=list)
    tone_of_voice: Text(300) = ""
    products: Annotated[list[DossierProduct], BeforeValidator(_take(15))] = Field(default_factory=list)
    recent_campaigns: Annotated[list[DossierCampaign], BeforeValidator(_take(10))] = Field(default_factory=list)
    competitors: Annotated[list[DossierCompetitor], BeforeValidator(_take(8))] = Field(default_factory=list)
    cultural_hooks: Annotated[list[Text(200)], BeforeValidator(_take(10))] = Field(default_factory=list)
    social_signals: Annotated[list[Text(200)], BeforeValidator(_take(10))] = Field(default_factory=list)
    moments: Annotated[list[Text(160)], BeforeValidator(_take(8))] = Field(default_factory=list)
    facts: Annotated[list[DossierFact], BeforeValidator(_take(60))] = Field(default_factory=list)
    sources: Annotated[list[DossierSource], BeforeValidator(_take(40))] = Field(default_factory=list)
    pages_read: int = 0
    depth: Literal["deep", "thin"] = "thin"
