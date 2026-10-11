from pydantic import BaseModel, Field

from app.schemas.brand import ResearchSection
from app.schemas.deck import Deck, PlanScale, ResolvedScale


class BrandPlanRequest(BaseModel):
    # No min_length: a blank brand is rejected in the route with a plain-string
    # `detail`, which is what the frontend expects (contract C3).
    brand: str = Field(max_length=200)
    # Optional client brief / objective; drives scale, structure and pitch retrieval.
    brief: str | None = Field(None, max_length=2000)
    scale: PlanScale = "auto"


class PastPitchRef(BaseModel):
    pitch_id: str
    campaign: str
    client: str


class BrandPlanResponse(BaseModel):
    brand: str
    # Markdown derived from `deck` (web view + PDF).
    plan: str
    research: list[ResearchSection]
    # Past pitches given to the LLM as internal inspiration (not shown to the brand).
    references: list[PastPitchRef]
    scale: ResolvedScale = "standard"
    brief: str | None = None
    # Always set by the v2 pipeline; optional so older clients/saved researches still parse.
    deck: Deck | None = None
