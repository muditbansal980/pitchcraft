from pydantic import BaseModel, Field

from app.schemas.brand import ResearchSection


class BrandPlanRequest(BaseModel):
    # No min_length: a blank brand is rejected in the route with a plain-string
    # `detail`, which is what the frontend expects (contract C3).
    brand: str = Field(max_length=200)


class PastPitchRef(BaseModel):
    pitch_id: str
    campaign: str
    client: str


class BrandPlanResponse(BaseModel):
    brand: str
    plan: str
    research: list[ResearchSection]
    # Past pitches given to the LLM as internal inspiration (not shown to the brand).
    references: list[PastPitchRef]
