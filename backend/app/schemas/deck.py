"""Deck contract (WORKPLAN C4): the single source of truth for a generated pitch.

The plan pipeline produces a ``Deck``; the Markdown plan, the PDF, the PPTX and
the Canva import are all derived from it.

LLMs overshoot length limits, so text fields are clipped (at a word boundary)
instead of rejected, and lists are truncated to their maximum size. That keeps
one long hook from throwing away a whole slide.

Visuals are requested by the writers (``want``/``brief``/``query``/meme fields)
and filled in by ``app.services.visuals.attach_visuals`` (``kind``/``url``/
``source_url``/``credit``). A visual that could not be filled stays
``kind="placeholder"`` and renders as a labelled "Sample visual: <brief>" box.
"""

from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, Field

PlanScale = Literal["auto", "focused", "standard", "flagship"]
ResolvedScale = Literal["focused", "standard", "flagship"]
ThemeName = Literal["bold_dark", "cinematic", "pop_meme", "editorial", "sport_energy"]
VisualWant = Literal["ref", "meme", "mock"]
VisualKind = Literal["ref", "meme", "mock", "placeholder"]


def _clip(limit: int):
    def clip(value: Any) -> Any:
        if not isinstance(value, str):
            return value
        text = " ".join(value.split())
        if len(text) <= limit:
            return text
        return text[:limit].rsplit(" ", 1)[0].rstrip(",;:") + "…"

    return clip


def _take(limit: int):
    def take(value: Any) -> Any:
        return value[:limit] if isinstance(value, list) else value

    return take


def Text(limit: int):  # noqa: N802 - reads like a type
    return Annotated[str, BeforeValidator(_clip(limit))]


HexColor = Annotated[str, Field(pattern=r"^#[0-9A-Fa-f]{6}$")]


class Visual(BaseModel):
    # Requested by the writer.
    want: VisualWant = "mock"
    brief: Text(300) = Field("", description="What the image should show; AI prompt, placeholder text and vision criteria")
    query: Text(200) | None = Field(None, description="Search query for ref images (Tavily / Reddit)")
    meme_template_id: str | None = Field(None, description="Imgflip template id, for want='meme'")
    meme_boxes: Annotated[list[Text(120)], BeforeValidator(_take(5))] = Field(default_factory=list)
    # Filled by the visuals service.
    kind: VisualKind = "placeholder"
    url: str | None = Field(None, description="Media path served by the backend, e.g. /media/<sha1>.jpg")
    source_url: str | None = None
    credit: Text(120) | None = None


class _Slide(BaseModel):
    title: Text(90)
    notes: Text(1200) = Field("", description="Speaker notes: the longer explanation behind the slide")


class CoverSlide(_Slide):
    layout: Literal["cover"] = "cover"
    subtitle: Text(160) = ""
    tagline: Text(80) = Field("", description="Hook or hashtag")


class StatementSlide(_Slide):
    layout: Literal["statement"] = "statement"
    strip: Text(160) = Field("", description="Highlight-strip subtitle under the headline")
    body: Text(400) = ""


class FromToSlide(_Slide):
    layout: Literal["from_to"] = "from_to"
    strip: Text(160) = ""
    from_text: Text(160)
    to_text: Text(160)


class FormulaPart(BaseModel):
    label: Text(40)
    text: Text(200)


class FormulaSlide(_Slide):
    layout: Literal["formula"] = "formula"
    parts: Annotated[list[FormulaPart], BeforeValidator(_take(3))] = Field(min_length=2)
    result: FormulaPart


class Card(BaseModel):
    title: Text(40)
    body: Text(220) = ""
    tags: Annotated[list[Text(30)], BeforeValidator(_take(4))] = Field(default_factory=list)
    visual: Visual | None = None


class CardGridSlide(_Slide):
    layout: Literal["card_grid"] = "card_grid"
    strip: Text(160) = ""
    cards: Annotated[list[Card], BeforeValidator(_take(4))] = Field(min_length=2)


class DividerSlide(_Slide):
    layout: Literal["divider"] = "divider"
    subtitle: Text(160) = ""


class IdeaSlide(_Slide):
    layout: Literal["idea"] = "idea"
    concept_note: Text(500)
    hook: Text(160) = ""
    format: Text(120) = Field("", description='Platform + duration, e.g. "IG reel, 45s"')
    creator: Text(160) = Field("", description="Creator archetype + tier")
    scalability: Text(300) = ""
    visual: Visual | None = None


class SamplesSlide(_Slide):
    layout: Literal["samples"] = "samples"
    strip: Text(160) = ""
    visuals: Annotated[list[Visual], BeforeValidator(_take(4))] = Field(min_length=1)


class RefSlide(_Slide):
    layout: Literal["ref"] = "ref"
    why: Text(300) = Field("", description="Why this reference works and what we borrow")
    visuals: Annotated[list[Visual], BeforeValidator(_take(4))] = Field(min_length=1)


class Column(BaseModel):
    heading: Text(60)
    bullets: Annotated[list[Text(160)], BeforeValidator(_take(6))] = Field(default_factory=list)


class TwoColumnSlide(_Slide):
    layout: Literal["two_column"] = "two_column"
    left: Column
    right: Column


class TableSlide(_Slide):
    layout: Literal["table"] = "table"
    strip: Text(160) = ""
    headers: Annotated[list[Text(40)], BeforeValidator(_take(6))] = Field(min_length=2)
    rows: Annotated[list[Annotated[list[Text(160)], BeforeValidator(_take(6))]], BeforeValidator(_take(10))]


class KpiTableSlide(TableSlide):
    layout: Literal["kpi_table"] = "kpi_table"


class ChipGroup(BaseModel):
    label: Text(40)
    chips: Annotated[list[Text(40)], BeforeValidator(_take(8))] = Field(default_factory=list)


class ChipsSlide(_Slide):
    layout: Literal["chips"] = "chips"
    strip: Text(160) = ""
    groups: Annotated[list[ChipGroup], BeforeValidator(_take(6))] = Field(min_length=1)


class Phase(BaseModel):
    name: Text(40)
    when: Text(40) = Field("", description='e.g. "Week 1-2", "Fri 10am", "Month 3"')
    points: Annotated[list[Text(140)], BeforeValidator(_take(4))] = Field(default_factory=list)


class TimelineSlide(_Slide):
    layout: Literal["timeline"] = "timeline"
    strip: Text(160) = ""
    phases: Annotated[list[Phase], BeforeValidator(_take(6))] = Field(min_length=2)


class FinSlide(_Slide):
    layout: Literal["fin"] = "fin"
    subtitle: Text(160) = ""


Slide = Annotated[
    CoverSlide
    | StatementSlide
    | FromToSlide
    | FormulaSlide
    | CardGridSlide
    | DividerSlide
    | IdeaSlide
    | SamplesSlide
    | RefSlide
    | TwoColumnSlide
    | TableSlide
    | KpiTableSlide
    | ChipsSlide
    | TimelineSlide
    | FinSlide,
    Field(discriminator="layout"),
]

SLIDE_LAYOUTS: tuple[str, ...] = (
    "cover", "statement", "from_to", "formula", "card_grid", "divider", "idea",
    "samples", "ref", "two_column", "table", "kpi_table", "chips", "timeline", "fin",
)


class Theme(BaseModel):
    name: ThemeName = "bold_dark"
    bg: HexColor = "#0E0E0E"
    surface: HexColor = "#1A1A1A"
    accent: HexColor = "#F5B700"
    accent_2: HexColor = "#FFFFFF"
    text: HexColor = "#FFFFFF"
    muted: HexColor = "#B3B3B3"
    heading_font: str = "Anton"
    body_font: str = "Montserrat"
    hero_image: Visual | None = Field(None, description="Cover/divider background (AI mock)")


class Deck(BaseModel):
    title: Text(120)
    brand: Text(200)
    scale: ResolvedScale = "standard"
    theme: Theme = Field(default_factory=Theme)
    slides: list[Slide] = Field(default_factory=list)
