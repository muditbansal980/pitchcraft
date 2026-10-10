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
    "standard": ScaleProfile("standard", 15, 22, 2, 3, 5, VisualBudget(ref=4, meme=4, mock=3), 3400, 5),
    "flagship": ScaleProfile("flagship", 30, 45, 3, 5, 8, VisualBudget(ref=8, meme=8, mock=6), 3600, 10),
}

# Rough output tokens a writer spends per slide of each layout (JSON incl. notes).
LAYOUT_WEIGHT: dict[str, int] = {
    "cover": 90, "statement": 130, "from_to": 120, "formula": 170, "card_grid": 260, "divider": 70,
    "idea": 280, "samples": 190, "ref": 170, "two_column": 230, "table": 300, "kpi_table": 280,
    "chips": 200, "timeline": 260, "fin": 60,
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
