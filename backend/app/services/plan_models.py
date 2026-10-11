"""Strategy schema (strategist output), scale profiles and the slide plan for brand_plan.

The strategist's JSON is parsed leniently: every field has a default, strings
and lists are coerced where models commonly drift (a bare string instead of an
object, a dict instead of a list), so one sloppy field never discards the
whole strategy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator, ConfigDict

from app.schemas.deck import ResolvedScale
from app.services.visuals import VisualBudget


def _as_str(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return "; ".join(str(v) for v in value if v)
    if isinstance(value, dict):
        return "; ".join(f"{k}: {v}" for k, v in value.items())
    return value


def _as_list(value: Any) -> Any:
    if value is None:
        return []
    if isinstance(value, (str, dict)):
        return [value]
    return value


def _str_list(value: Any) -> Any:
    value = _as_list(value)
    return [_as_str(v) for v in value] if isinstance(value, list) else value


Str = Annotated[str, BeforeValidator(_as_str)]
StrList = Annotated[list[str], BeforeValidator(_str_list)]


def _objects(key: str):
    """List of objects, where a bare string becomes {key: string}."""

    def coerce(value: Any) -> Any:
        value = _as_list(value)
        if isinstance(value, list):
            return [{key: v} if isinstance(v, str) else v for v in value]
        return value

    return BeforeValidator(coerce)


class _Lenient(BaseModel):
    model_config = ConfigDict(extra="ignore")


class Route(_Lenient):
    name: Str = ""
    hook: Str = ""
    idea: Str = ""


class BucketVisual(_Lenient):
    ref_query: Str = ""
    reddit_hint: Str = ""
    meme_template_id: Str = ""
    mock_prompt: Str = ""


class Bucket(_Lenient):
    name: Str
    concept: Str = ""
    hook: Str = ""
    format: Str = ""
    creator: Str = ""
    scalability: Str = ""
    distribution: StrList = []
    visual: BucketVisual = BucketVisual()
    slides: StrList = ["idea"]


class Hack(_Lenient):
    name: Str
    mechanic: Str = ""


class HandleGroup(_Lenient):
    category: Str
    why: Str = ""


class ServicePick(_Lenient):
    name: Str
    why: Str = ""
    what: Str = ""


class PhasePlan(_Lenient):
    name: Str
    when: Str = ""
    points: StrList = []


class Kpi(_Lenient):
    metric: Str
    target: Str = ""
    how: Str = ""


class ThemePick(_Lenient):
    name: Str = "bold_dark"
    accent_hint: Str = ""
    hero_prompt: Str = ""


class ExtraSlide(_Lenient):
    after: Str = "context"
    layout: Str = "statement"
    title: Str = ""
    brief: Str = ""


class Strategy(_Lenient):
    scale: Str = ""
    scale_reason: Str = ""
    title: Str = ""
    moment: Str = ""
    audience: Str = ""
    tension: Str = ""
    insight: Str = ""
    from_text: Str = ""
    to_text: Str = ""
    routes: Annotated[list[Route], _objects("name")] = []
    chosen: int = 0
    buckets: Annotated[list[Bucket], _objects("name")] = []
    hacks: Annotated[list[Hack], _objects("name")] = []
    distribution: Annotated[list[HandleGroup], _objects("category")] = []
    services: Annotated[list[ServicePick], _objects("name")] = []
    phases: Annotated[list[PhasePlan], _objects("name")] = []
    kpis: Annotated[list[Kpi], _objects("metric")] = []
    season: Str = ""
    why_now: Str = ""
    modules: StrList = []
    theme: ThemePick = ThemePick()
    extra_slides: Annotated[list[ExtraSlide], _objects("title")] = []

    @property
    def big_idea(self) -> Route:
        if not self.routes:
            return Route(name=self.title)
        return self.routes[self.chosen if 0 <= self.chosen < len(self.routes) else 0]

    @property
    def other_routes(self) -> list[Route]:
        big = self.big_idea
        return [r for r in self.routes if r is not big]


# --------------------------------------------------------------------------- scale profiles


@dataclass(frozen=True)
class ScaleProfile:
    name: ResolvedScale
    min_slides: int
    max_slides: int
    writer_groups: int
    min_buckets: int
    max_buckets: int
    visuals: VisualBudget
    strategist_max_tokens: int
    max_extra_slides: int


PROFILES: dict[str, ScaleProfile] = {
    "focused": ScaleProfile("focused", 6, 10, 1, 1, 2, VisualBudget(ref=2, meme=2, mock=1), 2600, 2),
    # v3: +5 service slides and up to 5 content modules => standard allows up to 26 slides.
    "standard": ScaleProfile("standard", 15, 26, 2, 3, 5, VisualBudget(ref=4, meme=4, mock=3), 3400, 5),
    "flagship": ScaleProfile("flagship", 30, 45, 3, 5, 8, VisualBudget(ref=8, meme=8, mock=6), 3600, 10),
}

# Rough output tokens a writer spends per slide of each layout (JSON incl. notes).
# v3: richer slides (idea = concept + 3-5 execution steps + 2-3 sample posts + services; statements carry bullets).
LAYOUT_WEIGHT: dict[str, int] = {
    "cover": 110, "statement": 260, "from_to": 170, "formula": 220, "card_grid": 380, "divider": 80,
    "idea": 620, "samples": 230, "ref": 200, "two_column": 330, "table": 450, "kpi_table": 420,
    "chips": 260, "timeline": 340, "fin": 70,
}

# Content modules the strategist can add (v3). key -> (layout, section, plan sub-rank, title, brief)
MODULES: dict[str, tuple[str, str, float, str, str]] = {
    "why_now": ("statement", "context", 0.5, "Why now",
                "Why now: the moment and 2-4 sourced stats as bullets; cite fact ids in sources."),
    "competitor_scan": ("table", "context", 0.8, "The category right now",
                        "Competitor scan table: Brand | What they do on social | The gap {brand} can own (from DOSSIER competitors)."),
    "personas": ("card_grid", "tension", 1.5, "Who we are talking to",
                 "3-4 audience personas as cards: name + one-line life + what they want from {brand} (from DOSSIER audience)."),
    "sample_scripts": ("two_column", "buckets", 9.0, "Sample scripts",
                       "Two short creator scripts (hook, beats, CTA) for the strongest buckets, one per column."),
    "creator_mix": ("table", "plan", 0.5, "The creator mix",
                    "Creator mix table: Tier | Archetype | Count (target) | Role | Formats."),
    "content_calendar": ("table", "plan", 3.5, "A week in the feed",
                         "Content calendar table: Week | Morning | Afternoon | Evening | Late night (what goes live; buckets/formats)."),
    "projections": ("kpi_table", "plan", 4.5, "Month by month",
                    "Projection table: Metric | Month 1 | Month 2 | Month 3 (all labelled targets)."),
}
DEFAULT_MODULES: dict[str, tuple[str, ...]] = {
    "focused": ("why_now",),
    "standard": ("why_now", "competitor_scan", "personas", "creator_mix", "content_calendar"),
    "flagship": ("why_now", "competitor_scan", "personas", "sample_scripts", "creator_mix", "content_calendar", "projections"),
}

# Layouts the strategist may pick for buckets / extra slides.
BUCKET_LAYOUTS = ("idea", "samples", "ref", "card_grid", "two_column", "chips", "table", "statement")
EXTRA_LAYOUTS = (
    "statement", "from_to", "formula", "card_grid", "two_column", "table", "chips", "timeline", "ref", "samples", "idea",
)
SECTIONS = ("context", "tension", "idea", "buckets", "hacks", "plan")


@dataclass
class PlanItem:
    i: int
    layout: str
    title: str
    brief: str
    section: str
    priority: int = 5  # lower = dropped first when trimming
    required: bool = False
    bucket: int | None = None
    prebuilt: bool = False  # built in code (services table), not by a writer
    group: int = 0
    tags: dict[str, Any] = field(default_factory=dict)
