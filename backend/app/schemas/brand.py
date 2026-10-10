from pydantic import BaseModel, Field


class BrandInfoRequest(BaseModel):
    brand: str = Field(min_length=1, max_length=200)


class Source(BaseModel):
    title: str
    url: str
    snippet: str


class ResearchSection(BaseModel):
    title: str
    summary: str
    sources: list[Source]


class BrandInfoResponse(BaseModel):
    brand: str
    message: str
    sections: list[ResearchSection]
