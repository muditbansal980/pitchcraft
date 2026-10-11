"""Brand plan generation (v2): research -> strategist -> parallel writers -> editor -> deck.

Pipeline (WORKPLAN C3/C4, plan sections 1, 3, 7):

1. In parallel: Tavily research, Reddit trend pulse, Imgflip meme templates,
   brand colour probe (+ the services catalog, loaded first so a missing catalog
   fails before Tavily is spent).
2. Past-pitch retrieval from the brief + research.
3. STRATEGIST (role "strategist", JSON): scale, tension, From -> To, 3 routes ->
   big idea, buckets with visual specs, hacks, distribution, services, phases,
   KPIs, theme, extra slides. Context size adapts to the model: rich (full past
   decks) on a big-context model, compact on Groq's 8K tokens/minute.
4. Slide plan built in code from the strategy and the scale profile (required
   slides guaranteed, slide count clamped to the profile's range).
5. WRITERS (role "writer", JSON): contiguous slide groups written in parallel,
   one model per group (``prefer=i``). Each slide is validated on its own;
   invalid slides are dropped and logged, required ones rebuilt from the strategy.
6. EDITOR (role "editor", JSON): one pass over the deck (chunked per group when
   the model's per-request budget is too small) returning corrected slides only.
   Failure keeps the unedited deck.
7. Rules enforced in code (catalog service names, meme box counts), theme
   resolved, visuals attached, Markdown derived from the deck.
"""

from __future__ import annotations

import asyncio
import contextlib
import difflib
import inspect
import json
import logging
import math
import re
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, TypeAdapter, ValidationError

from app.schemas.brand import ResearchSection
from app.schemas.deck import (
    Card,
    CardGridSlide,
    ChipGroup,
    ChipsSlide,
    CoverSlide,
    Deck,
    DividerSlide,
    FinSlide,
    FromToSlide,
    IdeaSlide,
    KpiTableSlide,
    Phase,
    PlanScale,
    RefSlide,
    SamplesSlide,
    Slide,
    StatementSlide,
    TableSlide,
    Theme,
    TimelineSlide,
    Visual,
)
from app.schemas.plan import BrandPlanResponse, PastPitchRef
from app.schemas.research import BrandDossier
from app.services import brand_identity, llm_router, memes, research_deep, service_playbooks, themes, trends, visuals
from app.services.plan_dossier import (
    dossier_corpus,
    dossier_slice,
    extract_fact_ids,
    fact_for_stat,
    facts_by_id,
    is_deep,
    mentions_any,
    render_dossier,
    source_label,
    specific_terms,
    strip_fact_ids,
)
from app.services.plan_services import (
    ServicePlan,
    render_playbooks,
    deep_dive_slides,
    ecosystem_slide,
    enforce_idea_services,
    matrix_slide,
    normalise_plan,
    offer_table,
    plan_services,
)
from app.services.brand_identity import BrandIdentity
from app.services.brand_identity_season import SEASONS, detect_season_detail
from app.services.brand_research import research_brand
from app.services.plan_checks import (
    BANNED_PLATFORM_RE,
    NARRATIVE_LAYOUTS,
    PRICE_RE,
    PROPOSAL_KEYS,
    STAT_RE,
    banned_terms,
    find_issues,
    match_service,
    similar_titles,
    slide_text,
    soften_stats,
    strip_handles,
    unsupported_stats,
)
from app.services.plan_markdown import deck_to_markdown
from app.services.plan_models import (
    BUCKET_LAYOUTS,
    DEFAULT_MODULES,
    EXTRA_LAYOUTS,
    MODULES,
    LAYOUT_WEIGHT,
    PROFILES,
    SECTIONS,
    PlanItem,
    ScaleProfile,
    ServicePick,
    Strategy,
)
from app.services.plan_prompts import (
    CORE_RULES,
    EDITOR_SYSTEM,
    LAYOUT_SPEC,
    SCALE_AUTO,
    SCALE_FIXED,
    STRATEGIST_SYSTEM,
    STYLE_GUIDE,
    WRITER_SYSTEM,
)
from app.services.retrieval import (
    CraftExcerpt,
    PitchMatch,
    Service,
    build_pitch_queries,
    clip_text,
    find_craft_excerpts,
    find_similar_pitches,
    load_pitch_full_text,
    load_services_catalog,
)

logger = logging.getLogger(__name__)

SLIDE_ADAPTER: TypeAdapter = TypeAdapter(Slide)
_HEX_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")

# Strategist completion budgets (auto uses the flagship one: the scale is not known yet).
# Real strategist outputs peaked at ~1.8K tokens (flagship); the rest is headroom for reasoning.
_STRATEGIST_TOKENS = {"focused": 2200, "standard": 2800, "flagship": 3000}
# Above this prompt capacity the strategist gets full past-deck text ("rich" context).
_RICH_CAPACITY = 40_000
# Writer prompt size we plan for when splitting the deck into groups.
_WRITER_PROMPT_BUDGET = 4300
_WRITER_MAX_OUTPUT = 8000
# Per-bucket writer calls queue on the same few Groq models: allow a longer (still bounded) wait.
_WRITER_MAX_WAIT = 180.0
_EDITOR_SYSTEM_TOKENS = 1300
_SERVICE_LIMIT = {"focused": 3, "standard": 5, "flagship": 7}
# Retry failed writer groups only if the first pass finished within this many seconds.
_WRITER_RETRY_WITHIN = 240.0


class ServicesCatalogEmptyError(RuntimeError):
    """The wldd_services collection has not been ingested."""


class PlanGenerationError(RuntimeError):
    """The LLMs returned no usable plan."""


# =========================================================================== inputs


@dataclass
class Inputs:
    brand: str
    brief: str | None
    requested_scale: PlanScale
    today: str
    services: list[Service]
    research: list[ResearchSection] = field(default_factory=list)
    pitches: list[PitchMatch] = field(default_factory=list)
    trend: trends.TrendPulse = field(default_factory=trends.TrendPulse)
    memes: list[visuals.MemeTemplate] = field(default_factory=list)  # shortlist(s): the only allowed templates
    identity: BrandIdentity | None = None
    dossier: BrandDossier | None = None
    playbooks: dict[str, Any] = field(default_factory=dict)
    service_plan: ServicePlan | None = None
    on_stage: Any = None
    colors: list[str] = field(default_factory=list)
    corpus: str = ""  # research + dossier text, the "supported facts" corpus (set by refresh_corpus)
    full_texts: dict[str, str] = field(default_factory=dict)
    shown_texts: list[str] = field(default_factory=list)
    shown_pitches: dict[str, tuple[str, str]] = field(default_factory=dict)
    banned: list[str] = field(default_factory=list)
    timings: dict[str, float] = field(default_factory=dict)

    @property
    def meme_ids(self) -> dict[str, visuals.MemeTemplate]:
        return {m.id: m for m in self.memes}

    @property
    def research_text(self) -> str:
        if self.corpus:
            return self.corpus
        parts = []
        for s in self.research:
            parts.append(s.summary)
            parts += [f"{src.title} {src.snippet}" for src in s.sources]
        return "\n".join(parts) + "\n" + (self.brief or "")

    def refresh_corpus(self) -> None:
        self.corpus = ""
        self.corpus = self.research_text + "\n" + dossier_corpus(self.dossier)

    @property
    def deep(self) -> bool:
        return is_deep(self.dossier)

    async def stage(self, name: str, progress: float) -> None:
        await _emit(self.on_stage, name, progress)

    @property
    def allowed_text(self) -> str:
        parts = [self.brand, self.brief or ""]
        for s in self.research:
            parts.append(s.summary)
            parts += [f"{src.title} {src.snippet}" for src in s.sources]
        parts += [f"{s.name} {s.description}" for s in self.services]
        return "\n".join(parts)


# Overall progress (0..1) at the start of each C8 stage.
STAGE_PROGRESS = {
    "research": 0.02, "identity": 0.12, "deep_research": 0.18, "strategy": 0.45, "writing": 0.55,
    "editing": 0.8, "visuals": 0.88, "done": 1.0,
}


async def _emit(on_stage: Any, name: str, progress: float) -> None:
    """Call ``on_stage(name, progress)`` (sync or async); a failing callback never breaks the plan."""
    if on_stage is None:
        return
    try:
        result = on_stage(name, round(min(1.0, max(0.0, progress)), 3))
        if inspect.isawaitable(result):
            await result
    except Exception as exc:  # noqa: BLE001
        logger.warning("on_stage(%r) failed: %s", name, exc)


async def _safe(coro, default, what: str):
    try:
        return await coro
    except Exception as exc:  # noqa: BLE001 - optional inputs never fail the plan
        logger.warning("%s failed (%s: %s); continuing without it", what, type(exc).__name__, str(exc)[:200])
        return default


async def _gather_inputs(brand: str, brief: str | None, scale: PlanScale, on_stage: Any = None) -> Inputs:
    started = time.monotonic()
    await _emit(on_stage, "research", STAGE_PROGRESS["research"])
    services = await asyncio.to_thread(load_services_catalog)
    if not services:
        raise ServicesCatalogEmptyError("WLDD services collection is empty")
    if scale == "auto" and (stated := _brief_scale(brief)):
        # Clear signals in the brief ("always-on", "6 months" / "burst", "DVC") decide auto-scale
        # up front, so the strategist sizes buckets and slides for it.
        logger.info("Auto scale for %r resolved from the brief: %s", brand, stated)
        scale = stated
    inputs = Inputs(brand=brand, brief=brief, requested_scale=scale, today=date.today().strftime("%d %B %Y"),
                    services=services, on_stage=on_stage)

    # The identity probe mostly reads the brand's website, so it runs alongside research with no
    # research text; motifs and the season are re-derived from the research afterwards.
    research, pulse, shortlist, ident = await asyncio.gather(
        research_brand(brand),
        _safe(trends.get_trend_pulse(), trends.TrendPulse(), "Trend pulse"),
        _safe(memes.meme_shortlist(brand, [brand, brief or ""], n=12), [], "Meme shortlist"),
        _safe(brand_identity.probe_brand_identity(brand, ""), None, "Brand identity"),
    )
    inputs.research = research.sections
    await inputs.stage("identity", STAGE_PROGRESS["identity"])
    inputs.trend = pulse or trends.TrendPulse()
    inputs.memes = [m for m in (shortlist or []) if m.box_count >= 1]
    ident = ident or BrandIdentity(brand=brand)
    research_text = inputs.research_text
    try:
        from_research = brand_identity.extract_motifs(
            brand, ident.site_nav, ident.site_headings + [ident.title], ident.description, research_text, limit=10,
        )
    except Exception as exc:  # noqa: BLE001 - motifs are optional
        logger.info("Motif extraction failed (%s); keeping the site motifs", exc)
        from_research = []
    ident.motifs = _merge_motifs(ident.motifs, from_research)
    # The brief comes first so a moment it names wins ties against the research.
    ident.season, ident.season_source = detect_season_detail(date.today(), f"{brief or ''}\n{research_text}")
    inputs.identity = ident
    inputs.colors = [c for c in ident.colors if isinstance(c, str) and _HEX_RE.match(c)]
    inputs.timings["research"] = time.monotonic() - started

    t = time.monotonic()
    queries = build_pitch_queries(brand, inputs.research, brief)
    inputs.pitches = await asyncio.to_thread(find_similar_pitches, queries, 25, 3)
    if llm_router.prompt_capacity("strategist", _STRATEGIST_TOKENS["flagship"]) >= _RICH_CAPACITY:
        texts = await asyncio.gather(*(asyncio.to_thread(load_pitch_full_text, p.pitch_id) for p in inputs.pitches))
        inputs.full_texts = {p.pitch_id: text for p, text in zip(inputs.pitches, texts) if text}
    inputs.timings["retrieval"] = time.monotonic() - t

    # Deep research: the brand's own pages + full articles, distilled into a sourced dossier.
    await inputs.stage("deep_research", STAGE_PROGRESS["deep_research"])
    t = time.monotonic()
    lo, hi = STAGE_PROGRESS["deep_research"], STAGE_PROGRESS["strategy"]

    async def sub_stage(_name: str, fraction: float) -> None:
        await inputs.stage("deep_research", lo + (hi - lo) * min(1.0, max(0.0, fraction)))

    dossier_scale = scale if scale in PROFILES else "standard"
    inputs.dossier = await _safe(
        research_deep.build_dossier(brand, brief, inputs.research, identity=ident, scale=dossier_scale, on_stage=sub_stage),
        None, "Deep research",
    )
    inputs.playbooks = await _safe(asyncio.to_thread(service_playbooks.load_playbooks), {}, "Service playbooks")
    inputs.refresh_corpus()
    inputs.timings["deep_research"] = time.monotonic() - t
    d = inputs.dossier
    logger.info(
        "Dossier for %r: depth=%s, %d pages, %d facts, %d products, %d campaigns, %d competitors; %d playbooks (%.1fs)",
        brand, d.depth if d else "-", d.pages_read if d else 0, len(d.facts) if d else 0, len(d.products) if d else 0,
        len(d.recent_campaigns) if d else 0, len(d.competitors) if d else 0, len(inputs.playbooks), inputs.timings["deep_research"],
    )
    logger.info(
        "Plan inputs for %r: %d research sections, %d pitches %s, %d trend posts, meme shortlist %s, identity site=%s "
        "colours=%s fonts=%s/%s logo=%s motifs=%s season=%s (%s)",
        brand, len(inputs.research), len(inputs.pitches), [p.pitch_id for p in inputs.pitches],
        len(inputs.trend.posts), [m.id for m in inputs.memes], ident.site_url, inputs.colors, ident.heading_font,
        ident.body_font, bool(ident.logo_url), ident.motifs, ident.season, ident.season_source,
    )
    return inputs


# Words research text is full of that are not products or objects of the brand.
_GENERIC_MOTIFS = {
    "marketing", "people", "brand", "brands", "company", "customers", "customer", "users", "user", "india",
    "indian", "campaign", "campaigns", "social media", "market", "business", "platform", "services", "service",
    "news", "year", "years", "time", "growth", "revenue", "share", "app users", "consumers", "audience",
    "content", "online", "digital", "influencer", "influencers", "ceo", "founder", "report", "latest",
    "their", "positioning", "strategy", "also", "more", "other", "new", "top", "best", "first", "data",
    "launch", "position", "leader", "leading", "category", "sector", "industry", "segment", "offering",
}


def _merge_motifs(site: list[str], research: list[str], limit: int = 6) -> list[str]:
    """Site motifs first (the brand's own words), then research ones that look like products/objects."""
    out: list[str] = []
    for m in [*site, *research]:
        m = " ".join(m.lower().split())
        if not m or m in _GENERIC_MOTIFS or set(m.split()) <= _GENERIC_MOTIFS or any(m in o or o in m for o in out):
            continue
        out.append(m)
        if len(out) >= limit:
            break
    return out


def _register_shown(inputs: Inputs, pitch_id: str, client: str, campaign: str, text: str) -> None:
    inputs.shown_pitches[pitch_id] = (client, campaign)
    if text:
        inputs.shown_texts.append(text)


def _refresh_banned(inputs: Inputs) -> None:
    inputs.banned = banned_terms(inputs.brand, inputs.shown_pitches.values(), inputs.shown_texts, inputs.allowed_text)


def _scrub(text: str, inputs: Inputs) -> str:
    """Hide past clients, campaigns and creator names from the models' view of past decks."""
    for term in sorted(inputs.banned, key=len, reverse=True):
        if term.startswith("@"):
            continue
        text = re.sub(r"(?<![A-Za-z0-9])" + re.escape(term) + r"(?![A-Za-z0-9])", "[name]", text, flags=re.IGNORECASE)
    return re.sub(r"@[A-Za-z0-9_.]{2,}", "@creator", text)


# =========================================================================== context formatting

_LEVELS = [
    # name, summary chars, sources/section, snippet chars, trend posts, meme templates, pitch mode, brief chars,
    # excerpt chars, dossier chars
    ("rich", 4000, 5, 1500, 40, 40, "full", 600, 0, 16000),
    ("standard", 1000, 3, 300, 20, 25, "excerpts", 380, 450, 9000),
    ("compact", 800, 2, 220, 10, 10, "excerpts", 300, 320, 6000),
    ("lean", 500, 1, 160, 8, 10, "brief", 220, 0, 4800),
    ("minimal", 400, 1, 120, 6, 8, "brief", 160, 0, 3200),
]


def _fmt_research(research: list[ResearchSection], summary: int, sources: int, snippet: int) -> str:
    blocks = []
    for section in research:
        lines = [f"### {section.title}", clip_text(section.summary, summary)]
        for src in section.sources[:sources]:
            lines.append(f"- {clip_text(src.title, 100)}: {clip_text(src.snippet, snippet)}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) or "No research available."


def _fmt_trends(pulse: trends.TrendPulse, n: int) -> str:
    posts = sorted(pulse.posts, key=lambda p: p.score, reverse=True)[:n]
    if not posts:
        return "Not available."
    return "\n".join(f"- r/{p.subreddit}: {clip_text(p.title, 120)}" for p in posts)


def _fmt_memes(memes: list[visuals.MemeTemplate], n: int) -> str:
    if not memes:
        return "Not available (don't request memes; use mock or ref visuals)."
    return "\n".join(f"{m.id} | {m.name} | {m.box_count}" for m in memes[:n])


def _season_line(inputs: Inputs) -> str:
    ident = inputs.identity
    season = SEASONS.get(ident.season) if ident and ident.season else None
    if not season:
        return "MOMENT: none detected."
    where = "named in the brief/research" if ident.season_source == "text" else "calendar guess for launch in ~2 weeks"
    return f'MOMENT: {season.label} (key "{season.key}", {where}).'


def _fmt_identity(inputs: Inputs) -> str:
    """Brand identity block: the brand's real products/objects, colours, fonts, and the detected moment."""
    ident = inputs.identity
    if ident is None:
        return f"BRAND IDENTITY: colours {', '.join(inputs.colors) or 'unknown'}.\n{_season_line(inputs)}"
    parts = [f"site {ident.site_url}" if ident.site_url else "site not found"]
    if ident.products:
        parts.append("named products/features: " + ", ".join(ident.products[:8]))
    if ident.motifs:
        parts.append("products/objects: " + ", ".join(ident.motifs[:6]))
    parts.append("colours " + (", ".join(inputs.colors[:3]) or "unknown"))
    parts.append(f"fonts {ident.heading_font}/{ident.body_font}")
    return "BRAND IDENTITY: " + "; ".join(parts) + ".\n" + _season_line(inputs)


def _fmt_services(services: list[Service], chars: int = 0) -> str:
    return "\n".join(f"- {s.name}: {clip_text(s.description, chars) if chars else s.description}" for s in services)


def _fmt_pitches(inputs: Inputs, mode: str, brief_chars: int, excerpt_chars: int) -> str:
    if not inputs.pitches:
        return "None available."
    blocks = []
    for p in inputs.pitches:
        lines = [f"### Past pitch ({p.services})"]
        if p.brief:
            lines.append("Brief: " + clip_text(_scrub(p.brief, inputs), brief_chars))
        if mode == "full" and p.pitch_id in inputs.full_texts:
            lines.append("Deck:\n" + _scrub(inputs.full_texts[p.pitch_id][:14_000], inputs))
        elif mode == "excerpts" and excerpt_chars:
            for ex in p.excerpts[:2 if excerpt_chars >= 400 else 1]:
                lines.append("Deck excerpt: " + clip_text(_scrub(ex, inputs), excerpt_chars))
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def _strategist_messages(inputs: Inputs, level: tuple) -> list:
    name, summary, sources, snippet, n_trends, n_memes, mode, brief_chars, excerpt_chars, dossier_chars = level
    scale = inputs.requested_scale
    scale_rule = SCALE_AUTO if scale == "auto" else SCALE_FIXED.format(scale=scale)
    buckets = (
        "focused 1-2, standard 3-5, flagship 5-8"
        if scale == "auto"
        else f"{PROFILES[scale].min_buckets}-{PROFILES[scale].max_buckets}"
    )
    extras = PROFILES["flagship" if scale == "auto" else scale].max_extra_slides
    system = STRATEGIST_SYSTEM.format(
        brand=inputs.brand, today=inputs.today, scale_rule=scale_rule, buckets=buckets, extras=extras,
        rules=CORE_RULES.format(brand=inputs.brand),
    )
    themes_list = "; ".join(f"{k} ({clip_text(v.split('.')[-2] if v.count('.') > 1 else v, 70)})" for k, v in themes.THEME_DESCRIPTIONS.items())
    human = (
        f"CLIENT BRIEF: {inputs.brief or 'none given (decide from research)'}\n"
        f"REQUESTED SCALE: {scale}\n\n"
        + (
            # A deep dossier replaces the clipped Tavily snippets; only the moments section stays.
            f"DOSSIER ({inputs.brand}; cite facts by id)\n{render_dossier(inputs.dossier, dossier_chars)}\n\n"
            f"RECENT MOMENTS\n{_fmt_research([s for s in inputs.research if s.title.lower() == 'upcoming moments'], 500, 1, 160)}\n\n"
            if inputs.deep else
            f"BRAND RESEARCH ({inputs.brand})\n{_fmt_research(inputs.research, summary, sources, snippet)}\n\n"
            + (f"DOSSIER (cite facts by id)\n{render_dossier(inputs.dossier, dossier_chars // 3)}\n\n"
               if inputs.dossier and inputs.dossier.facts else "")
        )
        + f"TREND PULSE (top Indian Reddit posts this week)\n{_fmt_trends(inputs.trend, n_trends)}\n\n"
        f"MEME TEMPLATES (id | name | text boxes)\n{_fmt_memes(inputs.memes, n_memes)}\n\n"
        f"WLDD SERVICES (a services lead details them next)\n{_fmt_services(inputs.services, 90)}\n\n"
        f"PAST PITCHES (confidential; mechanics only)\n{_fmt_pitches(inputs, mode, brief_chars, excerpt_chars)}\n\n"
        f"{_fmt_identity(inputs)}\n"
        f"THEMES: {themes_list}\n\n"
        f"Write the strategy JSON for {inputs.brand}."
    )
    return [SystemMessage(system), HumanMessage(human)]


async def _run_strategist(inputs: Inputs) -> Strategy:
    scale = inputs.requested_scale
    max_tokens = _STRATEGIST_TOKENS["flagship" if scale == "auto" else scale]
    capacity = llm_router.prompt_capacity("strategist", max_tokens)
    if capacity <= 0:
        raise llm_router.LLMUnavailableError("No LLM is configured for the strategist (set GROQ_API_KEY or GOOGLE_API_KEY)")

    # Register what the strategist may see, so names in it are banned + scrubbed.
    for p in inputs.pitches:
        text = inputs.full_texts.get(p.pitch_id) or "\n".join([p.brief or "", *p.excerpts])
        _register_shown(inputs, p.pitch_id, p.client, p.campaign, text)
    _refresh_banned(inputs)

    messages, chosen = None, None
    for level in _LEVELS:
        if level[6] == "full" and not (inputs.full_texts or inputs.deep):
            continue
        messages = _strategist_messages(inputs, level)
        chosen = level[0]
        if llm_router._messages_tokens(messages) <= capacity:
            break
    est = llm_router._messages_tokens(messages)
    logger.info("Strategist context: level=%s, ~%d prompt tokens (capacity %d), max_tokens %d", chosen, est, capacity, max_tokens)
    t = time.monotonic()
    try:
        strategy = await llm_router.invoke_json("strategist", messages, Strategy, max_tokens=max_tokens, temperature=0.8)
    except llm_router.LLMOutputError as exc:
        raise PlanGenerationError(f"the strategist returned no usable strategy ({exc})") from exc
    inputs.timings["strategist"] = time.monotonic() - t
    return strategy


# =========================================================================== strategy normalisation


_FLAGSHIP_RE = re.compile(
    r"always[- ]on|retainer|community (?:page|ip|platform)|\bip\b|\b(?:[3-9]|1[0-2]|six|three|four|nine|twelve)[- ]months?\b"
    r"|year[- ]long|multi[- ]phase|tentpole|ongoing",
    re.IGNORECASE,
)
_FOCUSED_RE = re.compile(r"\bburst\b|\bdvc\b|\bactivation\b|\bone[- ]off\b|\bamplification\b|\bsingle\b", re.IGNORECASE)


def _brief_scale(brief: str | None) -> str | None:
    """Scale the brief states outright (plan rules), or None when the strategist should judge."""
    text = brief or ""
    flagship, focused = bool(_FLAGSHIP_RE.search(text)), bool(_FOCUSED_RE.search(text))
    if flagship and not focused:
        return "flagship"
    if focused and not flagship:
        return "focused"
    return None


def _heuristic_scale(brief: str | None) -> str:
    return _brief_scale(brief) or "standard"


def _resolve_scale(inputs: Inputs, strategy: Strategy) -> str:
    if inputs.requested_scale != "auto":
        return inputs.requested_scale
    said = (strategy.scale or "").strip().lower()
    return said if said in PROFILES else _heuristic_scale(inputs.brief)


def _normalise_strategy(strategy: Strategy, inputs: Inputs, profile: ScaleProfile) -> None:
    brand = inputs.brand
    if not strategy.title:
        strategy.title = f"{brand} x {strategy.big_idea.name or 'the next big moment'}"
    if re.search(r"\bx\s*wldd\b|×\s*wldd", strategy.title, flags=re.IGNORECASE):
        strategy.title = f"{brand} x {strategy.big_idea.name or strategy.big_idea.hook or 'the internet'}"

    # Services: exact catalog names only, deduped, capped per scale.
    picks: list[ServicePick] = []
    seen: set[str] = set()
    for pick in strategy.services:
        svc = match_service(pick.name, inputs.services)
        if svc is None:
            logger.warning("Strategist picked unknown service %r; dropped", pick.name)
            continue
        if svc.name in seen:
            continue
        seen.add(svc.name)
        picks.append(ServicePick(name=svc.name, why=pick.why, what=pick.what))
    if not picks:
        defaults = [s for s in inputs.services if s.name.lower().startswith(("amplification", "influencers"))][:2]
        picks = [ServicePick(name=s.name, why="", what=clip_text(s.description, 160)) for s in defaults or inputs.services[:2]]
        logger.warning("No valid services from the strategist; using %s", [p.name for p in picks])
    strategy.services = picks[: _SERVICE_LIMIT[profile.name]]

    # Buckets: clamp count, valid layouts, idea first.
    if len(strategy.buckets) > profile.max_buckets:
        strategy.buckets = strategy.buckets[: profile.max_buckets]
    if len(strategy.buckets) < profile.min_buckets:
        logger.warning("Strategist gave %d buckets for %s (min %d)", len(strategy.buckets), profile.name, profile.min_buckets)
    per_bucket = 2 if profile.name == "focused" else 3
    meme_ids = inputs.meme_ids
    strategy.buckets = [b for b in strategy.buckets if b.name.strip() or b.hook.strip()]
    strategy.hacks = [h for h in strategy.hacks if h.name.strip()]
    for bucket in strategy.buckets:
        if not bucket.name.strip():
            bucket.name = clip_text(bucket.hook, 40)
        layouts = [lay for lay in (s.strip().lower() for s in bucket.slides) if lay in BUCKET_LAYOUTS]
        if "idea" in layouts:
            layouts.remove("idea")
        layouts = ["idea"] + [lay for i, lay in enumerate(layouts) if lay not in layouts[:i]]
        if not meme_ids and "samples" in layouts and not bucket.visual.mock_prompt:
            bucket.visual.mock_prompt = bucket.concept
        bucket.slides = layouts[:per_bucket]
        if bucket.visual.meme_template_id and bucket.visual.meme_template_id not in meme_ids:
            bucket.visual.meme_template_id = ""

    strategy.extra_slides = [
        e for e in strategy.extra_slides if e.layout in EXTRA_LAYOUTS and e.title
    ][: profile.max_extra_slides]
    for e in strategy.extra_slides:
        if e.after not in SECTIONS:
            e.after = "context"
    strategy.season = (strategy.season or "").strip().lower()
    if strategy.season not in SEASONS:
        strategy.season = ""
    if strategy.theme.name not in themes.THEME_DESCRIPTIONS:
        strategy.theme.name = "bold_dark"
    if strategy.theme.accent_hint and not _HEX_RE.match(strategy.theme.accent_hint):
        strategy.theme.accent_hint = ""


# =========================================================================== slide plan


def build_slide_plan(
    strategy: Strategy, profile: ScaleProfile, brand: str, has_memes: bool = True, plan: ServicePlan | None = None,
) -> list[PlanItem]:
    scale = profile.name
    big = strategy.big_idea
    items: list[PlanItem] = []
    modules = list(dict.fromkeys(m for m in strategy.modules if m in MODULES)) or list(DEFAULT_MODULES[scale])

    def add(layout: str, title: str, brief: str, section: str, priority: int = 5, **kw: Any) -> PlanItem:
        item = PlanItem(i=0, layout=layout, title=title, brief=brief, section=section, priority=priority, **kw)
        items.append(item)
        return item

    def module(key: str, priority: int) -> None:
        if key not in modules:
            return
        layout, section, _sub, title, brief = MODULES[key]
        add(layout, title, brief.format(brand=brand), section, priority=priority, tags={"module": key})

    def extras(after: str) -> None:
        for e in strategy.extra_slides:
            if e.after != after:
                continue
            # The skeleton already has one timeline, KPI table, objective and distribution slide.
            if e.layout in ("timeline", "kpi_table", "from_to") or (e.layout == "chips" and strategy.distribution):
                logger.info("Extra slide %r (%s) duplicates a skeleton slide; skipped", e.title, e.layout)
                continue
            if any(similar_titles(e.title, it.title) for it in items):
                continue
            if e.layout in ("card_grid", "table") and len(strategy.buckets) >= 2 and re.search(
                r"bucket|pillar|segment|content (mix|plan)|persona|competitor|calendar|creator mix", f"{e.title} {e.brief}",
                re.IGNORECASE,
            ):
                logger.info("Extra slide %r repeats a skeleton/module slide; skipped", e.title)
                continue
            # Execution slides (tables, chips) belong with the plan, not before the strategy.
            section = "plan" if e.layout in _EXECUTION_LAYOUTS else after
            add(e.layout, e.title, e.brief or e.title, section, priority=2, tags={"extra": True})

    add("cover", strategy.title, f"Cover. Subtitle frames the promise to {brand}; tagline = the big idea hook ({big.hook}).",
        "context", required=True)
    if "why_now" in modules:
        add("statement", "Why now",
            f"Why now: {strategy.why_now or strategy.moment}. Headline = the moment as a provocation; body = what it "
            "means for the brand; bullets = 2-4 sourced stats/facts from the DOSSIER (ids in sources).",
            "context", priority=6, tags={"module": "why_now"})
    elif scale != "focused":
        add("statement", f"Where {brand} is today",
            f"Context / why now: {brand}'s position and the moment ({strategy.moment or 'from research'}). DOSSIER facts only; "
            "bullets = sourced stats.", "context", priority=3)
    module("competitor_scan", 4)
    extras("context")
    add("statement", "Tension point",
        f"TENSION POINT: {strategy.tension}. Headline = the tension as a punchy line; strip starts with 'TENSION POINT:'; "
        "body = why it matters now; bullets = the facts behind it.", "tension", priority=6)
    if scale == "flagship" or (scale == "standard" and strategy.insight):
        add("statement", "The insight", f"The human insight: {strategy.insight}. Headline = the truth in internet words.",
            "tension", priority=2)
    module("personas", 4)
    add("from_to", "Our objective", f"Objective. From: {strategy.from_text} -> To: {strategy.to_text}. Strip = the shift in one line.",
        "tension", priority=3 if scale == "focused" else 6)
    extras("tension")
    if scale == "flagship":
        add("divider", "The big idea", f"Divider into the big idea; subtitle teases {big.name}.", "idea", priority=1)
    add("statement", big.name or "The big idea",
        f"THE BIG IDEA: {big.name}. Hook: {big.hook}. {big.idea} Headline = idea name or hook; strip = the idea in one line; "
        "body = how it lives in Indian internet culture; bullets = 3 reasons it works for the brand.", "idea", required=True)
    extras("idea")

    buckets = strategy.buckets
    if scale != "focused" and len(buckets) >= 2:
        if len(buckets) <= 4:
            add("card_grid", "Key content buckets", "One card per bucket: name, its job in one line, tags = format/platform.",
                "buckets", priority=3)
        else:
            add("table", "Content buckets at a glance", "Table, one row per bucket: Bucket | Format | Hook | Goal.",
                "buckets", priority=3)
    for bi, bucket in enumerate(buckets):
        powered = plan.for_bucket(bucket.name) if plan else None
        svc_note = (
            f" Powered by {', '.join(powered.services)}" + (f" ({powered.mechanics})" if powered.mechanics else "") + "."
            if powered and powered.services else ""
        )
        for j, layout in enumerate(bucket.slides):
            if layout == "idea":
                title, brief = bucket.name, (
                    f"Idea slide for bucket '{bucket.name}': concept note naming a real product/campaign/insight, hook, "
                    "format, creator archetype + tier, scalability, 3-5 execution steps, 2-3 sample posts, services + "
                    f"service_mechanics, visual.{svc_note}"
                )
            elif layout == "samples":
                kind = "meme visuals (template id + exactly its box count)" if has_memes else "mock visuals"
                title, brief = f"{bucket.name}: samples", (
                    f"Samples for '{bucket.name}': 2-3 brand memes/sample posts as {kind}; strip = what these show."
                )
            elif layout == "ref":
                title, brief = f"{bucket.name}: ref", (
                    f"Internet references for '{bucket.name}': 1-3 ref visuals with search queries; why = what we borrow."
                )
            else:
                title, brief = bucket.name, f"{layout} slide expanding bucket '{bucket.name}' (mechanics, examples, rules)."
            # The first bucket's samples/ref outrank later buckets' extras when a deck must be trimmed.
            add(layout, title, brief, "buckets", priority=7 if j == 0 else (5 if bi == 0 else 4) - (j - 1), bucket=bi)
    extras("buckets")
    module("sample_scripts", 3)

    if strategy.hacks and scale != "focused":
        if scale == "flagship":
            add("divider", "How do we hack the internet?", "Divider into the internet hacks.", "hacks", priority=1)
        add("card_grid", "How we hack the internet",
            "One card per hack (max 4): name, mechanic + an example comment/poll/post line; tags = where it runs.",
            "hacks", priority=4)
    extras("hacks")

    if strategy.distribution:
        add("chips", "Distribution science",
            "Chip groups: label = handle category, chips = what they post for us (buckets/formats).", "plan",
            priority=5)
    module("creator_mix", 3)
    # WLDD services, woven in: offer table, how they connect, lead-service deep dives, services x phases.
    add("table", "What we can offer", "Services table (built from the service plan).", "plan", required=True, prebuilt=True,
        tags={"service": "offer"})
    if plan and scale != "focused":
        if len(plan.services) >= 2:
            add("timeline", "How it all connects", "", "plan", priority=5, prebuilt=True, tags={"service": "ecosystem"})
        for f in (f for f in plan.services if f.lead):
            add("statement", f"Why {f.name.split('(')[0].strip()} for {brand}", "", "plan", priority=5, prebuilt=True,
                tags={"service": "dive_why", "name": f.name})
            add("timeline", f"How {f.name.split('(')[0].strip()} works for {brand}", "", "plan", priority=4, prebuilt=True,
                tags={"service": "dive_how", "name": f.name})
        if len(plan.services) >= 2 and len(strategy.phases) >= 2:
            add("table", "Who does what, when", "", "plan", priority=4, prebuilt=True, tags={"service": "matrix"})
    add("timeline", "How it rolls out",
        "Timeline of the phases with their timing (" + "; ".join(f"{p.name} {p.when}" for p in strategy.phases) + ").",
        "plan", priority=6)
    module("content_calendar", 3)
    add("kpi_table", "How we'll measure success",
        "KPI table: KPI | Target | How we measure. Targets read as targets.", "plan", required=True)
    module("projections", 3)
    if scale != "focused":
        add("card_grid", "Next steps", f"3 cards: what happens in the first two weeks after {brand} says yes.", "plan",
            priority=3, tags={"next_steps": True})
    extras("plan")
    if scale == "flagship" and strategy.other_routes:
        add("card_grid", "Other routes we explored", "One card per rejected route: name, hook, the idea in one line.",
            "plan", priority=2)
    add("fin", "Fin.", "Closing slide: title 'Fin.', subtitle = a one-line sign-off using the hook.", "plan", required=True)

    items = _fit_plan(items, profile, strategy, brand)
    items.sort(key=_story_rank)
    for i, item in enumerate(items):
        item.i = i
    return items


_EXECUTION_LAYOUTS = ("table", "chips", "timeline", "kpi_table")
_SECTION_RANK = {s: k for k, s in enumerate(SECTIONS)}
_SERVICE_SUB = {"offer": 2.0, "ecosystem": 2.2, "dive_why": 2.4, "dive_how": 2.4, "matrix": 2.6}


def _story_rank(item: PlanItem) -> tuple[int, float]:
    """Story arc: cover, context/why now, competitor scan, tension, insight, personas, From->To, big idea,
    buckets, sample scripts, hacks, distribution, creator mix, services (offer, ecosystem, deep dives,
    matrix), timeline, content calendar, KPIs, projections, next steps, (other routes), fin.
    Stable within a step."""
    if item.layout == "cover":
        return (-1, 0)
    if item.layout == "fin":
        return (99, 0)
    section = _SECTION_RANK.get(item.section, 0)
    key = item.tags.get("module")
    if key in MODULES:
        return (section, MODULES[key][2])
    sub: float = 0
    if item.section == "context":
        sub = 1 if item.tags.get("extra") else 0
    elif item.section == "tension":
        sub = {"from_to": 3}.get(item.layout, 0 if item.title == "Tension point" else (1 if item.title == "The insight" else 1.8))
    elif item.section == "plan":
        kind = item.tags.get("service")
        if kind in _SERVICE_SUB:
            sub = _SERVICE_SUB[kind]
        elif item.layout == "chips":
            sub = 0
        elif item.tags.get("extra"):
            sub = 1
        elif item.layout == "timeline":
            sub = 3
        elif item.layout == "kpi_table":
            sub = 4
        elif item.tags.get("next_steps"):
            sub = 5
        elif item.title.startswith("Other routes"):
            sub = 6
        else:
            sub = 1
    return (section, sub)


def _rendered(items: list[PlanItem]) -> int:
    """Slides as rendered: an idea with sample posts becomes a concept page + a how-we-run-it page."""
    return len(items) + sum(1 for it in items if it.layout == "idea")


def _fit_plan(items: list[PlanItem], profile: ScaleProfile, strategy: Strategy, brand: str) -> list[PlanItem]:
    # Too long: drop the lowest-priority optional slides (later ones first).
    while _rendered(items) > profile.max_slides:
        optional = [(it.priority, -idx) for idx, it in enumerate(items) if not it.required]
        if not optional:
            break
        _, neg = min(optional)
        items.pop(-neg)

    # Too short: add bucket refs/samples, hack ideas, then generic depth slides.
    def insert_after_bucket(bi: int, item: PlanItem) -> None:
        last = max((idx for idx, it in enumerate(items) if it.bucket == bi), default=None)
        if last is None:
            last = next((idx for idx, it in enumerate(items) if it.section == "plan"), len(items) - 1) - 1
        items.insert(last + 1, item)

    fillers: list[tuple[int | None, PlanItem]] = []
    for bi, bucket in enumerate(strategy.buckets):
        have = {it.layout for it in items if it.bucket == bi}
        for layout in ("ref", "samples"):
            if layout not in have:
                fillers.append((bi, PlanItem(0, layout, f"{bucket.name}: {layout}",
                                             f"{'Internet references' if layout == 'ref' else 'Brand samples'} for '{bucket.name}'.",
                                             "buckets", priority=2, bucket=bi)))
    for hack in strategy.hacks[:4]:
        fillers.append((None, PlanItem(0, "idea", hack.name, f"Idea slide for the internet hack '{hack.name}': {hack.mechanic}",
                                       "hacks", priority=2)))
    fillers.append((None, PlanItem(0, "two_column", f"Why this works for {brand}",
                                   "Left: what the audience gets; right: what the brand gets.", "plan", priority=1)))
    for bi, filler in fillers:
        if _rendered(items) >= profile.min_slides:
            break
        if bi is not None:
            insert_after_bucket(bi, filler)
        else:
            pos = next((idx for idx, it in enumerate(items) if it.section == "plan"), len(items) - 1)
            items.insert(pos, filler)
    return items


def _unit(item: PlanItem) -> str:
    """Writer unit: the narrative, one per content bucket, the hacks, the plan."""
    if item.bucket is not None:
        return f"bucket{item.bucket}"
    if item.section in ("context", "tension", "idea") or (item.section == "buckets" and not item.tags.get("module")):
        return "narrative"
    if item.section in ("hacks", "buckets"):
        return "hacks"
    return "plan"


def _assign_groups(items: list[PlanItem], out_cap: int) -> int:
    """One writer call per unit (narrative, each bucket, hacks, plan); a unit whose slides would not fit
    one call's output budget is split into contiguous parts. Returns the number of groups."""
    budget = max(800, int((out_cap - 300) / 1.35))
    group = -1
    current_unit, acc = None, 0
    for it in items:
        if it.prebuilt:
            it.group = -1
            continue
        unit = _unit(it)
        weight = LAYOUT_WEIGHT.get(it.layout, 250)
        if unit != current_unit or acc + weight > budget:
            group += 1
            current_unit, acc = unit, 0
        it.group = group
        it.tags["unit"] = unit
        acc += weight
    return group + 1


# =========================================================================== writers


class WriterOutput(BaseModel):
    # Lenient on purpose: non-object entries are skipped per slide instead of failing the batch.
    slides: list[Any] = []


class EditorOutput(BaseModel):
    edits: list[Any] = []


def _compact(data: Any) -> Any:
    """Drop empty values so JSON shown to models stays small."""
    if isinstance(data, dict):
        out = {k: _compact(v) for k, v in data.items()}
        return {k: v for k, v in out.items() if v not in ("", None, [], {})}
    if isinstance(data, list):
        return [c for c in (_compact(v) for v in data) if c not in ("", None, [], {})]
    return data


def _dumps(data: Any) -> str:
    return json.dumps(_compact(data), ensure_ascii=False, separators=(",", ":"))


def _strategy_for_group(strategy: Strategy, group_items: list[PlanItem]) -> str:
    own = {it.bucket for it in group_items if it.bucket is not None}
    sections = {it.section for it in group_items}
    layouts = {it.layout for it in group_items}
    units = {it.tags.get("unit") for it in group_items}
    big = strategy.big_idea
    data: dict[str, Any] = {
        "title": strategy.title, "moment": strategy.moment, "audience": strategy.audience,
        "tension": strategy.tension, "insight": strategy.insight,
        "big_idea": big.model_dump(),
        "buckets": [
            b.model_dump(exclude={"visual"} if bi not in own else set()) if bi in own or (len(strategy.buckets) <= 3 and "buckets" in sections)
            else {"name": b.name, "hook": b.hook, "format": b.format}
            for bi, b in enumerate(strategy.buckets)
        ],
    }
    if "narrative" in units:
        data.update({"why_now": strategy.why_now, "from": strategy.from_text, "to": strategy.to_text})
    if "hacks" in sections or "plan" in sections:
        data["hacks"] = [h.model_dump() for h in strategy.hacks]
    if "plan" in sections:
        data["distribution"] = [d.model_dump() for d in strategy.distribution]
        data["phases"] = [p.model_dump() for p in strategy.phases]
        data["kpis"] = [k.model_dump() for k in strategy.kpis]
    if "plan" in sections and "card_grid" in layouts:
        data["other_routes"] = [r.model_dump() for r in strategy.other_routes]
    return _dumps(data)


def _services_block(inputs: Inputs, strategy: Strategy, mine: list[PlanItem]) -> str:
    """Bucket calls: the services powering THIS idea, with WLDD's playbook mechanics. Others: the combo."""
    plan = inputs.service_plan
    if plan is None:
        return "WLDD SERVICES IN THIS PITCH: " + ", ".join(p.name for p in strategy.services)
    buckets = sorted({it.bucket for it in mine if it.bucket is not None})
    if buckets and any(it.layout == "idea" for it in mine):
        lines = []
        catalog = {s.name: s for s in inputs.services}
        for bi in buckets:
            bs = plan.for_bucket(strategy.buckets[bi].name)
            if not bs:
                continue
            lines.append(f"Bucket '{strategy.buckets[bi].name}': services {bs.services}; mechanics: {bs.mechanics or 'decide volumes'}")
            for n in bs.services:
                fit = next((f for f in plan.services if f.name == n), None)
                lines.append(render_playbooks([catalog[n]], inputs.playbooks, compact=True) if n in catalog else f"### {n}")
                if fit:
                    lines.append(f"for this brand: {strip_fact_ids(fit.what)}; volumes: {fit.volumes}; KPI: {fit.kpi}")
        return "SERVICES FOR THIS IDEA (fill \"services\" with these exact names and \"service_mechanics\")\n" + "\n".join(lines)
    combo = " -> ".join(f"{c.service}" for c in plan.combo)
    return f"WLDD SERVICES IN THIS PITCH: {', '.join(plan.names)}. How they combine: {plan.combo_summary or combo}"


def _dossier_block(inputs: Inputs, strategy: Strategy, mine: list[PlanItem], rich: bool) -> str:
    if not inputs.dossier or not (inputs.dossier.facts or inputs.dossier.products):
        return ""
    units = {it.tags.get("unit") for it in mine}
    if "narrative" in units:
        return "DOSSIER (cite fact ids in \"sources\")\n" + render_dossier(inputs.dossier, 9000 if rich else 3200)
    about = [it.title for it in mine]
    for it in mine:
        if it.bucket is not None:
            b = strategy.buckets[it.bucket]
            about += [b.name, b.concept, b.hook, b.format]
    if "plan" in units:
        about += [p.name for p in strategy.phases] + [k.metric for k in strategy.kpis] + ["creators", "followers", "views"]
    return "DOSSIER SLICE (cite fact ids in \"sources\")\n" + dossier_slice(
        inputs.dossier, " ".join(about), 5000 if rich else 1900, 10 if rich else 6,
    )


def _writer_messages(
    inputs: Inputs, strategy: Strategy, items: list[PlanItem], group: int, excerpts: list[CraftExcerpt], excerpt_chars: int,
    rich: bool = False,
) -> list:
    mine = [it for it in items if it.group == group]
    others = [it for it in items if it.group != group]
    rules = CORE_RULES.format(brand=inputs.brand)
    system = WRITER_SYSTEM.format(
        brand=inputs.brand, today=inputs.today, style=STYLE_GUIDE, layouts=LAYOUT_SPEC.replace("{brand}", inputs.brand), rules=rules,
    )
    needs_memes = any(it.layout in ("samples", "idea", "card_grid") for it in mine)
    parts = [
        f"BRAND: {inputs.brand}",
        f"CLIENT BRIEF: {inputs.brief or 'none'}",
        f"STRATEGY\n{_strategy_for_group(strategy, mine)}",
        _services_block(inputs, strategy, mine),
    ]
    if dossier := _dossier_block(inputs, strategy, mine, rich):
        parts.append(dossier)
    if inputs.identity and inputs.identity.products:
        parts.append(
            "BRAND PRODUCTS (name at least two of these where natural, spelled exactly): "
            + ", ".join(inputs.identity.products[:8])
        )
    if inputs.identity and inputs.identity.motifs:
        parts.append(
            "BRAND OBJECTS (use these real products/objects in ideas and visual briefs): "
            + ", ".join(inputs.identity.motifs[:6])
        )
    if strategy.season and strategy.season in SEASONS:
        parts.append(f"MOMENT: {SEASONS[strategy.season].label}; tie ideas, captions and visual briefs to it.")
    if needs_memes:
        parts.append(
            "MEME TEMPLATES (id | name | boxes): use ONLY these ids, each at most once, with exactly its number of boxes\n"
            + _fmt_memes(inputs.memes, 15 if rich else 10)
        )
    if excerpts:
        shown = "\n---\n".join(clip_text(_scrub(e.text, inputs), excerpt_chars) for e in excerpts)
        parts.append(
            "CRAFT EXAMPLES from past WLDD decks for other brands (tone and specificity only; never reuse their names, "
            f"shows, creators or ideas)\n---\n{shown}\n---"
        )
    if others:
        parts.append("OTHER WRITERS' SLIDES (don't repeat them): " + "; ".join(f"{it.i} {clip_text(it.title, 38)}" for it in others))
    parts.append(
        "YOUR SLIDES (write each one; keep i and layout; the title is a hint, write a better headline)\n"
        + "\n".join(f"{it.i}. {it.layout} | {it.title} | {it.brief}" for it in mine)
    )
    parts.append(f'Write these {len(mine)} slides now as {{"slides": [...]}}.')
    return [SystemMessage(system), HumanMessage("\n\n".join(parts))]


def _visual_dict(value: Any, default_want: str) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, str):
        return {"want": default_want, "brief": value}
    if not isinstance(value, dict):
        return None
    v = dict(value)
    want = str(v.get("want") or v.get("kind") or default_want).lower()
    v["want"] = want if want in ("ref", "meme", "mock") else default_want
    for key in ("kind", "url", "source_url", "credit"):
        v.pop(key, None)  # result fields belong to the visuals service
    if not v.get("brief"):
        v["brief"] = v.get("query") or v.get("caption") or v.get("description") or ""
    if isinstance(v.get("meme_boxes"), str):
        v["meme_boxes"] = [v["meme_boxes"]]
    return v


def _fix_visual(v: dict[str, Any] | None, memes: dict[str, visuals.MemeTemplate]) -> dict[str, Any] | None:
    """Memes must use a known template with exactly its box count; refs need a query."""
    if not v:
        return v
    if v["want"] == "meme":
        template = memes.get(str(v.get("meme_template_id") or ""))
        boxes = [str(b) for b in (v.get("meme_boxes") or []) if b is not None]
        if not any(b.strip() for b in boxes) or not memes:
            v["want"] = "mock"
            if boxes and not v.get("brief"):
                v["brief"] = " / ".join(boxes)
            v.pop("meme_template_id", None)
            v["meme_boxes"] = []
        elif template is None:
            v["meme_template_id"] = None  # not on the shortlist: _assign_memes picks one with this box count
            v["meme_boxes"] = boxes[:5]
        else:
            v["meme_template_id"] = template.id
            v["meme_boxes"] = (boxes + [""] * template.box_count)[: template.box_count]
    else:
        v.pop("meme_template_id", None)
        v["meme_boxes"] = []
    if v["want"] == "ref" and not v.get("query"):
        v["query"] = clip_text(v.get("brief") or "", 120)
    return v


def _str_items(value: Any) -> list[str]:
    """Lists of strings from what models write: objects -> 'name: detail', nested lists flattened."""
    if value is None:
        return []
    if isinstance(value, (str, dict)):
        value = [value]
    out: list[str] = []
    for v in value if isinstance(value, list) else []:
        if isinstance(v, str):
            out.append(v)
        elif isinstance(v, dict):
            vals = [str(x) for x in v.values() if isinstance(x, (str, int, float)) and str(x).strip()]
            if vals:
                out.append(": ".join(vals[:2]) if len(vals) > 1 else vals[0])
        elif isinstance(v, (int, float)):
            out.append(str(v))
        elif isinstance(v, list):
            out.extend(_str_items(v))
    return out


def _prepare_slide(raw: dict[str, Any], item: PlanItem | None, memes: dict[str, visuals.MemeTemplate]) -> dict[str, Any]:
    data = dict(raw)
    data.pop("i", None)
    if item is not None and not data.get("layout"):
        data["layout"] = item.layout
    layout = data.get("layout")
    if not data.get("title") and item is not None:
        data["title"] = item.title
    for key in ("execution", "sample_posts", "bullets", "services", "sources"):
        if key in data:
            data[key] = _str_items(data[key])
    if isinstance(data.get("service_mechanics"), (dict, list)):
        sm = data["service_mechanics"]
        data["service_mechanics"] = "; ".join(
            f"{k}: {v}" for k, v in sm.items()) if isinstance(sm, dict) else "; ".join(_str_items(sm))
    if layout == "idea" and not data.get("concept_note"):
        for alias in ("concept", "concept note", "conceptNote", "description", "body", "idea"):
            if isinstance(data.get(alias), str) and data[alias].strip():
                data["concept_note"] = data.pop(alias)
                break
    if layout == "idea" and "visual" in data:
        data["visual"] = _fix_visual(_visual_dict(data["visual"], "ref"), memes)
    if layout in ("samples", "ref"):
        default = "ref" if layout == "ref" else ("meme" if memes else "mock")
        vs = data.get("visuals")
        if isinstance(vs, (dict, str)):
            vs = [vs]
        data["visuals"] = [v for v in (_fix_visual(_visual_dict(x, default), memes) for x in vs or []) if v]
        if not data["visuals"]:
            # A samples/ref slide without visual requests still needs one slot to fill.
            brief = data.get("strip") or data.get("why") or data.get("title") or ""
            data["visuals"] = [_fix_visual({"want": "ref" if layout == "ref" else "mock", "brief": brief}, memes)]
        if layout == "ref":
            for v in data["visuals"]:
                if v["want"] != "ref":
                    v["want"] = "ref"
                    v["query"] = v.get("query") or clip_text(v.get("brief") or "", 120)
    if layout == "card_grid" and isinstance(data.get("cards"), list):
        cards = []
        for card in data["cards"]:
            if isinstance(card, str):
                card = {"title": card}
            if isinstance(card, dict):
                card = dict(card)
                if "visual" in card:
                    card["visual"] = _fix_visual(_visual_dict(card["visual"], "mock"), memes)
                cards.append(card)
        data["cards"] = cards
    if layout in ("table", "kpi_table") and isinstance(data.get("rows"), list):
        width = len(data.get("headers") or [])
        rows = []
        for row in data["rows"]:
            if isinstance(row, dict):
                row = list(row.values())
            if isinstance(row, list):
                row = [str(c) if not isinstance(c, str) else c for c in row]
                if width:
                    row = (row + [""] * width)[:width]
                rows.append(row)
        data["rows"] = rows
    if layout == "chips" and isinstance(data.get("groups"), list):
        data["groups"] = [
            {"label": g, "chips": []} if isinstance(g, str) else g for g in data["groups"]
        ]
    if layout == "timeline" and isinstance(data.get("phases"), list):
        data["phases"] = [
            {**p, "points": [p["points"]] if isinstance(p.get("points"), str) else p.get("points", [])}
            if isinstance(p, dict) else {"name": str(p)} for p in data["phases"]
        ]
    return data


def _parse_slides(
    raws: list[dict[str, Any]], items: list[PlanItem], memes: dict[str, visuals.MemeTemplate], where: str,
) -> dict[int, Any]:
    """Validate writer slides one by one, keyed by plan index. Invalid ones are dropped and logged."""
    by_i = {it.i: it for it in items}
    expected = [it.i for it in items]
    out: dict[int, Any] = {}
    for pos, raw in enumerate(raws):
        if not isinstance(raw, dict):
            continue
        idx = raw.get("i")
        try:
            idx = int(idx)
        except (TypeError, ValueError):
            idx = expected[pos] if pos < len(expected) else None
        if idx not in by_i or idx in out:
            idx = next((e for e in expected if e not in out and by_i[e].layout == raw.get("layout")), None)
        if idx is None:
            logger.warning("%s: slide %d has no matching plan slot; dropped", where, pos)
            continue
        try:
            out[idx] = SLIDE_ADAPTER.validate_python(_prepare_slide(raw, by_i[idx], memes))
        except ValidationError as exc:
            errs = "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()[:4])
            logger.warning("%s: invalid %s slide %d dropped: %s (keys %s)", where, raw.get("layout"), idx, errs, sorted(raw)[:12])
    return out


async def _craft_for_group(inputs: Inputs, strategy: Strategy, mine: list[PlanItem], n: int) -> list[CraftExcerpt]:
    words = [it.title for it in mine[:6]]
    for it in mine:
        if it.bucket is not None:
            b = strategy.buckets[it.bucket]
            words.append(f"{b.concept} {b.format}")
            break
    layouts = sorted({it.layout for it in mine})
    query = f"{' '.join(layouts)} slide: " + " ".join(words)
    return await asyncio.to_thread(find_craft_excerpts, query, n)


async def _write_group(
    inputs: Inputs, strategy: Strategy, items: list[PlanItem], group: int, excerpts: list[CraftExcerpt], excerpt_chars: int,
    out_cap: int,
    shift: int = 0,
    rich: bool = False,
    sem: asyncio.Semaphore | None = None,
) -> dict[int, Any]:
    mine = [it for it in items if it.group == group]
    messages = _writer_messages(inputs, strategy, items, group, excerpts, excerpt_chars, rich)
    weight = sum(LAYOUT_WEIGHT.get(it.layout, 250) for it in mine)
    max_tokens = int(min(max(weight * 1.35 + 300, 1200), out_cap))
    try:
        async with sem or contextlib.nullcontext():
            result = await llm_router.invoke_json(
                "writer", messages, WriterOutput, max_tokens=max_tokens, temperature=0.85, prefer=group + shift,
                max_wait=_WRITER_MAX_WAIT, min_output=min(max_tokens, int(weight * 1.2)),
            )
        raws = result.slides
    except llm_router.LLMOutputError as exc:
        # Keep every complete slide object from the broken answer.
        raws = llm_router.salvage_objects(exc.text, "slides")
        if not raws:
            raise
        logger.warning("Writer group %d: invalid JSON, salvaged %d complete slides", group, len(raws))
    slides = _parse_slides(raws, mine, inputs.meme_ids, f"writer group {group}")
    missing = [it.i for it in mine if it.i not in slides]
    logger.info("Writer group %d (%s): %d/%d slides valid%s", group, mine[0].tags.get("unit") if mine else "-",
                len(slides), len(mine), f", missing {missing}" if missing else "")
    return slides


async def _run_writers(inputs: Inputs, strategy: Strategy, items: list[PlanItem], profile: ScaleProfile) -> dict[int, Any]:
    out_cap = max(
        1200,
        min(llm_router.prompt_capacity("writer", _WRITER_PROMPT_BUDGET), llm_router.output_capacity("writer"), _WRITER_MAX_OUTPUT),
    )
    rich = out_cap >= _WRITER_MAX_OUTPUT
    n_groups = _assign_groups(items, out_cap)
    groups = list(range(n_groups))

    n_excerpts, excerpt_chars = (3, 900) if rich else (2, 380)
    excerpt_lists = await asyncio.gather(
        *(_craft_for_group(inputs, strategy, [it for it in items if it.group == g], n_excerpts) for g in groups)
    )
    for exs in excerpt_lists:
        for e in exs:
            _register_shown(inputs, e.pitch_id, e.client, e.campaign, e.text)
    _refresh_banned(inputs)
    # Calls queue in order; at most this many hold a model at once (they still pick models by budget).
    sem = asyncio.Semaphore(max(2, len(llm_router.role_models("writer"))))
    logger.info(
        "Writers: %d calls (units %s), output cap %d tokens, weights %s", n_groups,
        [next((it.tags.get("unit") for it in items if it.group == g), "-") for g in groups], out_cap,
        [sum(LAYOUT_WEIGHT.get(it.layout, 250) for it in items if it.group == g) for g in groups],
    )

    t = time.monotonic()
    done_count = 0

    async def one(g: int, shift: int = 0, pool: list[PlanItem] = items) -> dict[int, Any]:
        nonlocal done_count
        try:
            return await _write_group(inputs, strategy, pool, g, excerpt_lists[g], excerpt_chars, out_cap, shift, rich, sem)
        finally:
            done_count += 1
            lo, hi = STAGE_PROGRESS["writing"], STAGE_PROGRESS["editing"]
            await inputs.stage("writing", lo + (hi - lo) * min(1.0, done_count / max(1, n_groups)))

    results = await asyncio.gather(*(one(g) for g in groups), return_exceptions=True)
    slides: dict[int, Any] = {}
    errors: list[BaseException] = []
    for g, res in zip(groups, results):
        if isinstance(res, BaseException):
            logger.error("Writer group %d failed: %s: %s", g, type(res).__name__, str(res)[:300])
            errors.append(res)
        else:
            slides.update(res)
    # One more pass for the slides still missing (whole failed groups or the ones a writer skipped),
    # regrouped by their original group and sent to a different model, while there is time.
    missing = [it for it in items if not it.prebuilt and it.i not in slides]
    if missing and time.monotonic() - t < _WRITER_RETRY_WITHIN:
        retry_groups = sorted({it.group for it in missing})
        logger.info("Writers: second pass for %d missing slides in groups %s", len(missing), retry_groups)
        retry_items = [
            PlanItem(**{**it.__dict__, "group": (it.group if it in missing else -2)}) for it in items
        ]
        again = await asyncio.gather(*(one(g, 1, retry_items) for g in retry_groups), return_exceptions=True)
        for g, res in zip(retry_groups, again):
            if isinstance(res, BaseException):
                logger.error("Writer group %d failed again: %s: %s", g, type(res).__name__, str(res)[:200])
                errors.append(res)
            else:
                for i, slide in res.items():
                    slides.setdefault(i, slide)
    inputs.timings["writers"] = time.monotonic() - t
    if not slides and errors:
        if any(isinstance(e, llm_router.LLMUnavailableError) for e in errors):
            raise next(e for e in errors if isinstance(e, llm_router.LLMUnavailableError))
        logger.error("Every slide writer failed (%s); building the deck from the strategy alone", errors[0])
    return slides


# =========================================================================== fallbacks (built from the strategy)


def _services_slide(strategy: Strategy, brand: str) -> TableSlide:
    return TableSlide(
        title="What we can offer",
        strip=f"The WLDD services that make {strategy.big_idea.name or 'this'} happen for {brand}",
        headers=["Service", "Why it fits", "What we'll do"],
        rows=[[p.name, p.why or "-", p.what or "-"] for p in strategy.services],
        notes="Exact scope and volumes get locked with your team after the brief call.",
    )


def _prebuilt_slide(item: PlanItem, inputs: Inputs, strategy: Strategy) -> Any | None:
    """Service slides built in code from the service plan (exact names, no writer needed)."""
    plan, brand = inputs.service_plan, inputs.brand
    kind = item.tags.get("service", "offer")
    if plan is None:
        return _services_slide(strategy, brand) if kind == "offer" else None
    if kind == "offer":
        return offer_table(plan, brand, strategy.big_idea.name, inputs.dossier)
    if kind == "ecosystem":
        return ecosystem_slide(plan, brand)
    if kind == "matrix":
        return matrix_slide(plan, strategy)
    if kind in ("dive_why", "dive_how"):
        lead = next((f for f in plan.services if f.name == item.tags.get("name")), None)
        if lead is None:
            return None
        single = ServicePlan(services=[lead.model_copy(update={"lead": True})])
        why, how = deep_dive_slides(single, brand, inputs.dossier)
        return why if kind == "dive_why" else how
    return None


def _fallback_slide(item: PlanItem, strategy: Strategy, brand: str) -> Any | None:
    big = strategy.big_idea
    try:
        if item.layout == "cover":
            return CoverSlide(title=strategy.title or brand, subtitle=f"A WLDD pitch for {brand}", tagline=big.hook)
        if item.layout == "fin":
            return FinSlide(title="Fin.", subtitle=big.hook or f"Let's build this with {brand}.")
        if item.prebuilt:
            return _services_slide(strategy, brand)
        if item.layout == "kpi_table":
            return KpiTableSlide(
                title="How we'll measure success", headers=["KPI", "Target", "How we measure"],
                rows=[[k.metric, k.target or "-", k.how or "-"] for k in strategy.kpis] or [["Reach", "Target: agreed at kickoff", "Platform analytics"]],
            )
        if item.layout == "timeline" and len(strategy.phases) >= 2:
            return TimelineSlide(title=item.title, phases=[Phase(name=p.name, when=p.when, points=p.points) for p in strategy.phases])
        if item.layout == "from_to" and strategy.from_text and strategy.to_text:
            return FromToSlide(title="Our objective", from_text=strategy.from_text, to_text=strategy.to_text)
        if item.layout == "statement":
            if item.required:
                return StatementSlide(title=big.name or item.title, strip=big.hook, body=big.idea)
            if item.section == "tension" and strategy.tension:
                return StatementSlide(title=item.title, strip="TENSION POINT", body=strategy.tension)
            if item.section == "context" and (strategy.moment or strategy.audience):
                return StatementSlide(title=item.title, strip=strategy.moment, body=strategy.audience)
        if item.layout == "idea" and item.bucket is not None:
            b = strategy.buckets[item.bucket]
            if b.concept:
                return IdeaSlide(
                    title=b.name, concept_note=b.concept, hook=b.hook, format=b.format, creator=b.creator,
                    scalability=b.scalability,
                    visual=Visual(want="ref", brief=b.visual.ref_query or b.concept, query=b.visual.ref_query or None),
                )
        if item.layout == "chips" and strategy.distribution:
            carried = {
                d.category: [b.name for b in strategy.buckets if any(d.category.lower() in x.lower() for x in b.distribution)]
                for d in strategy.distribution
            }
            return ChipsSlide(
                title=item.title,
                strip="Who posts what, and why it travels",
                groups=[ChipGroup(label=d.category, chips=carried[d.category] or ([d.why] if d.why else [])) for d in strategy.distribution],
            )
        if item.layout == "card_grid" and item.section == "buckets" and len(strategy.buckets) >= 2:
            return CardGridSlide(title=item.title, cards=[Card(title=b.name, body=b.hook) for b in strategy.buckets])
        if item.layout == "card_grid" and item.section == "hacks" and len(strategy.hacks) >= 2:
            return CardGridSlide(title=item.title, cards=[Card(title=h.name, body=h.mechanic) for h in strategy.hacks])
        if item.layout == "card_grid" and item.title.startswith("Other routes") and len(strategy.other_routes) >= 2:
            return CardGridSlide(
                title=item.title, cards=[Card(title=r.name, body=f"{r.hook} {r.idea}".strip()) for r in strategy.other_routes],
            )
        if item.layout == "table" and item.section == "buckets" and strategy.buckets:
            return TableSlide(
                title=item.title, headers=["Bucket", "Format", "Hook", "Creator"],
                rows=[[b.name, b.format, b.hook, b.creator] for b in strategy.buckets],
            )
        if item.tags.get("next_steps"):
            return CardGridSlide(title="Next steps", cards=[
                Card(title="Brief call", body=f"Lock objectives, moments and guardrails with the {brand} team."),
                Card(title="Creator shortlist", body="Archetypes, tiers and handle categories mapped to each bucket."),
                Card(title="Launch plan", body=f"First content calendar and the {big.name or 'launch'} kick-off."),
            ])
        if item.layout == "divider":
            return DividerSlide(title=item.title, subtitle=big.hook if item.section == "idea" else "")
        if item.layout in ("samples", "ref") and item.bucket is not None:
            b = strategy.buckets[item.bucket]
            if item.layout == "ref":
                query = b.visual.ref_query or b.visual.reddit_hint or b.name
                return RefSlide(title=item.title, why=b.concept, visuals=[Visual(want="ref", brief=query, query=query)])
            return SamplesSlide(
                title=item.title, strip=b.hook,
                visuals=[Visual(want="mock", brief=b.visual.mock_prompt or f"{b.name}: {b.concept}")],
            )
        if item.layout == "idea" and item.section == "hacks":
            hack = next((h for h in strategy.hacks if h.name == item.title), None)
            if hack and hack.mechanic:
                return IdeaSlide(title=hack.name, concept_note=hack.mechanic)
    except ValidationError:
        return None
    return None


# =========================================================================== editor


def _slide_payload(slide: Any) -> dict[str, Any]:
    data = slide.model_dump(exclude_none=True)

    def strip_results(d: Any) -> Any:
        if isinstance(d, dict):
            if "want" in d:
                d = {k: v for k, v in d.items() if k not in ("kind", "url", "source_url", "credit")}
            return {k: strip_results(v) for k, v in d.items()}
        if isinstance(d, list):
            return [strip_results(v) for v in d]
        return d

    layout = data.pop("layout", None)
    return {"layout": layout, **_compact(strip_results(data))}


def _editor_messages(
    inputs: Inputs, strategy: Strategy, slides: list[Any], idxs: list[int], issues: dict[int, list[str]],
) -> list:
    flagged = sorted({t for i in idxs for msg in issues.get(i, []) for t in re.findall(r"name '([^']+)'", msg)})
    banned_show = flagged + [c for c, _ in inputs.shown_pitches.values() if c.lower() != inputs.brand.lower()]
    banned_text = ", ".join(dict.fromkeys(banned_show)) or "past-pitch brands, campaigns and creators"
    system = EDITOR_SYSTEM.format(
        brand=inputs.brand, today=inputs.today, style=STYLE_GUIDE, rules=CORE_RULES.format(brand=inputs.brand),
        services=", ".join(f'"{s.name}"' for s in inputs.services), banned=banned_text,
    )
    others = [i for i in range(len(slides)) if i not in idxs]
    must = [f"- slide {i}: " + "; ".join(issues[i]) for i in idxs if i in issues]
    names = _specific_names(inputs)
    parts = [
        f"BIG IDEA: {strategy.big_idea.name} ({strategy.big_idea.hook}). Buckets: "
        + ", ".join(b.name for b in strategy.buckets),
    ]
    if names:
        parts.append("SPECIFIC NAMES (real products, campaigns, competitors of the brand's world): " + ", ".join(names[:20]))
    if inputs.service_plan:
        parts.append("SERVICES IN THIS PITCH (exact): " + ", ".join(inputs.service_plan.names))
    if others:
        parts.append("OTHER SLIDES (edited separately; avoid repeating them): " + "; ".join(f"{i}. {slides[i].title}" for i in others))
    if must:
        parts.append("MUST FIX\n" + "\n".join(must))
    parts.append("SLIDES\n" + "\n".join(_dumps({"i": i, **_slide_payload(slides[i])}) for i in idxs))
    parts.append('Return {"edits": [...]} with only the slides you changed.')
    return [SystemMessage(system), HumanMessage("\n\n".join(parts))]


def _restore_visuals(new: Any, old: Any) -> Any:
    """Editors sometimes drop visual requests or rich fields; put the writer's back."""
    if hasattr(old, "visual") and getattr(old, "visual", None) and not getattr(new, "visual", None):
        new.visual = old.visual
    if hasattr(old, "visuals") and old.visuals and len(getattr(new, "visuals", []) or []) < len(old.visuals):
        new.visuals = old.visuals
    for key in ("execution", "sample_posts", "services", "service_mechanics", "bullets", "sources"):
        if getattr(old, key, None) and not getattr(new, key, None):
            setattr(new, key, getattr(old, key))
    return new


def _specific_names(inputs: Inputs) -> list[str]:
    extra = list(inputs.identity.products) if inputs.identity and inputs.identity.products else []
    return specific_terms(inputs.dossier, extra)


def _specificity_issues(slides: list[Any], inputs: Inputs) -> dict[int, list[str]]:
    """Idea slides that name no real product, campaign or competitor from the dossier/identity."""
    names = _specific_names(inputs)
    if not names:
        return {}
    out: dict[int, list[str]] = {}
    for i, s in enumerate(slides):
        if isinstance(s, IdeaSlide) and not mentions_any(slide_text(s) + " " + s.notes, names):
            out[i] = [f"generic: name a real product, campaign or audience insight (e.g. {', '.join(names[:4])})"]
    return out


async def _edit_chunk(inputs: Inputs, strategy: Strategy, slides: list[Any], idxs: list[int], issues, prefer: int,
                      max_tokens: int, max_wait: float | None = None):
    messages = _editor_messages(inputs, strategy, slides, idxs, issues)
    result = await llm_router.invoke_json("editor", messages, EditorOutput, max_tokens=max_tokens, temperature=0.4,
                                          prefer=prefer, max_wait=max_wait)
    edits: dict[int, Any] = {}
    allowed = set(idxs)
    for edit in result.edits:
        if not isinstance(edit, dict):
            continue
        raw = edit.get("slide") if isinstance(edit.get("slide"), dict) else {k: v for k, v in edit.items() if k != "i"}
        try:
            i = int(edit.get("i", raw.get("i")))
        except (TypeError, ValueError):
            continue
        if i not in allowed:
            continue
        old = slides[i]
        raw = dict(raw)
        raw.setdefault("layout", old.layout)
        if raw.get("layout") != old.layout:
            logger.info("Editor tried to change slide %d layout %s -> %s; ignored", i, old.layout, raw.get("layout"))
            continue
        try:
            new = SLIDE_ADAPTER.validate_python(_prepare_slide(raw, None, inputs.meme_ids))
        except ValidationError as exc:
            logger.warning("Editor slide %d invalid, kept original: %s", i, str(exc.errors()[:2])[:300])
            continue
        edits[i] = _restore_visuals(new, old)
    return edits


async def _run_editor(
    inputs: Inputs, strategy: Strategy, slides: list[Any], items_group: list[int], skip: set[int] | None = None,
) -> list[Any]:
    issues = find_issues(slides, inputs.banned, inputs.research_text)
    for i, msgs in _specificity_issues(slides, inputs).items():
        issues.setdefault(i, []).extend(msgs)
    skip = skip or set()
    if issues:
        logger.info("Pre-edit rule issues: %s", {i: v for i, v in issues.items()})
    sizes = [llm_router.estimate_tokens(_dumps(_slide_payload(s))) + 10 for s in slides]
    total = sum(sizes)
    out_tokens = int(min(max(total * 0.6, 1500), 8000))
    capacity = llm_router.prompt_capacity("editor", min(out_tokens, 3200))
    if capacity <= 0:
        logger.warning("No editor model available; keeping the unedited deck")
        return slides

    editable = [i for i in range(len(slides)) if i not in skip]  # prebuilt service slides are exact already
    total = sum(sizes[i] for i in editable)
    if total + _EDITOR_SYSTEM_TOKENS > 2.5 * capacity:
        # Small per-request budget (Groq free tier): edit where it matters most, i.e. flagged slides,
        # ideas (specificity) and the narrative statements (voice), not every table and chip.
        focus = [i for i in editable if i in issues or slides[i].layout in ("idea", "statement", "from_to", "cover")]
        logger.info("Editor: deck ~%d tokens vs capacity %d; editing %d focus slides of %d", total, capacity, len(focus), len(editable))
        editable = focus
        total = sum(sizes[i] for i in editable)
    if total + _EDITOR_SYSTEM_TOKENS <= capacity and total <= 9000:
        chunks = [editable]
    else:
        # Pack consecutive slides (deck order keeps related slides together) into chunks that fit one call.
        budget = max(1200, min(capacity - _EDITOR_SYSTEM_TOKENS - 400, 9000))
        chunks, chunk, size = [], [], 0
        for i in editable:
            if chunk and size + sizes[i] > budget:
                chunks.append(chunk)
                chunk, size = [], 0
            chunk.append(i)
            size += sizes[i]
        if chunk:
            chunks.append(chunk)
    logger.info("Editor: %d chunk(s) %s, deck ~%d tokens, capacity %d", len(chunks), [len(c) for c in chunks], total, capacity)

    t = time.monotonic()
    results = await asyncio.gather(
        *(
            _edit_chunk(
                inputs, strategy, slides, chunk, issues, prefer=k,
                max_tokens=int(min(max(sum(sizes[i] for i in chunk) * 0.9, 1200), 8000)),
                max_wait=_WRITER_MAX_WAIT,
            )
            for k, chunk in enumerate(chunks)
        ),
        return_exceptions=True,
    )
    inputs.timings["editor"] = time.monotonic() - t
    edited = list(slides)
    n_edits = 0
    for chunk, res in zip(chunks, results):
        if isinstance(res, BaseException):
            logger.warning("Editor chunk %s failed (%s: %s); keeping those slides unedited", chunk[:3], type(res).__name__, str(res)[:200])
            continue
        for i, slide in res.items():
            edited[i] = slide
            n_edits += 1
    logger.info("Editor changed %d of %d slides", n_edits, len(slides))
    return edited


# =========================================================================== rules enforced in code


def _enforce_services(slides: list[Any], strategy: Strategy, inputs: Inputs, services_idx: int | None) -> None:
    if services_idx is None:
        return
    slide = slides[services_idx]
    rebuild = (
        (lambda: offer_table(inputs.service_plan, inputs.brand, strategy.big_idea.name, inputs.dossier))
        if inputs.service_plan else (lambda: _services_slide(strategy, inputs.brand))
    )
    if not isinstance(slide, TableSlide) or slide.layout != "table":
        slides[services_idx] = rebuild()
        return
    rows, seen = [], set()
    for row in slide.rows:
        svc = match_service(row[0] if row else "", inputs.services)
        if svc is None or svc.name in seen:
            continue
        seen.add(svc.name)
        rows.append([svc.name, *row[1:]])
    if not rows:
        slides[services_idx] = rebuild()
    else:
        slide.rows = rows


_SINGLETON_LAYOUTS = ("timeline", "kpi_table", "chips", "from_to")
_NO_TEXT_KEYS = {"layout", "want", "kind", "url", "source_url", "meme_template_id"}


def _duplicate_of(slide: Any, slides: list[Any]) -> int | None:
    """Index of an earlier slide this one repeats (same layout + similar title/content, or a 2nd timeline etc.)."""
    def body(s: Any) -> str:
        data = _slide_payload(s)
        data.pop("title", None)
        data.pop("notes", None)
        return _dumps(data)

    mine = body(slide)
    for j, other in enumerate(slides):
        if other.layout != slide.layout:
            continue
        if slide.layout in _SINGLETON_LAYOUTS:
            return j
        theirs = body(other)
        if theirs == mine:
            return j
        if similar_titles(slide.title, other.title) and difflib.SequenceMatcher(None, mine, theirs).ratio() >= 0.6:
            return j
    return None


_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}
_DATE_RANGE_RE = re.compile(
    r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+(\d{1,2})"
    r"(?:\s*[-\u2010-\u2014]\s*(?:(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+)?(\d{1,2}))?\b",
    re.IGNORECASE,
)


def _parse_day(month: str, day: str, today: date) -> date | None:
    try:
        d = date(today.year, _MONTHS[month[:3].lower()], int(day))
    except (KeyError, ValueError):
        return None
    # A month far behind today means next year's calendar (e.g. "Jan 5" written in October).
    return d.replace(year=today.year + 1) if (today - d).days > 180 else d


def _shift_past_timeline(data: dict, today: date) -> None:
    """LLMs often start the rollout on dates that have already passed; move the whole timeline forward."""
    phases = data.get("phases") or []
    starts = []
    for phase in phases:
        m = _DATE_RANGE_RE.search(phase.get("when") or "")
        if m and (d := _parse_day(m.group(1), m.group(2), today)):
            starts.append(d)
    if not starts or min(starts) >= today:
        return
    offset = (today + timedelta(days=3)) - min(starts)

    def shift(m: re.Match) -> str:
        start = _parse_day(m.group(1), m.group(2), today)
        if start is None:
            return m.group(0)
        new_start = start + offset
        out = f"{new_start:%b} {new_start.day}"
        if m.group(4):
            end = _parse_day(m.group(3) or m.group(1), m.group(4), today)
            if end is not None:
                new_end = end + offset
                out += f"\u2013{new_end.day}" if new_end.month == new_start.month else f"\u2013{new_end:%b} {new_end.day}"
        return out

    for phase in phases:
        if phase.get("when"):
            phase["when"] = _DATE_RANGE_RE.sub(shift, phase["when"])
    logger.info("Timeline %r started in the past; shifted forward by %d days", data.get("title"), offset.days)


# Banned filler the editor sometimes leaves in: plain replacements.
_FILLER_FIX = {
    "leverage": "use", "leverages": "uses", "leveraging": "using", "leveraged": "used", "seamless": "smooth",
    "seamlessly": "smoothly", "elevate": "lift", "elevates": "lifts", "elevating": "lifting", "synergy": "fit",
    "holistic": "full", "game-changer": "big shift", "game changer": "big shift", "cutting-edge": "new",
    "revolutionize": "change", "revolutionise": "change", "delve": "dig",
}
_FILLER_RE = re.compile(r"\b(" + "|".join(re.escape(k) for k in sorted(_FILLER_FIX, key=len, reverse=True)) + r")\b", re.IGNORECASE)


def _match_case(src: str, repl: str) -> str:
    return repl.capitalize() if src[:1].isupper() else repl


def _clean_slide(slide: Any, inputs: Inputs) -> Any:
    """Last-line rules in code: no @handles, no unsupported stats (targets excepted), cards with bodies."""
    research = inputs.research_text
    kpi = slide.layout == "kpi_table"
    allow_targets = slide.layout not in NARRATIVE_LAYOUTS
    facts = facts_by_id(inputs.dossier)
    cited: list[str] = []  # fact ids found inline or in "sources"
    stat_facts: list[str] = []  # ids of dossier facts whose figures the slide uses

    def fix(value: Any, key: str | None = None) -> Any:
        if isinstance(value, dict):
            return {k: (v if k in _NO_TEXT_KEYS else fix(v, k)) for k, v in value.items()}
        if isinstance(value, list):
            return [fix(v, key) for v in value]
        if isinstance(value, str) and value:
            if "[F" in value or "(F" in value:
                cited.extend(extract_fact_ids(value))
                value = strip_fact_ids(value)
            out = strip_handles(value) if "@" in value else value
            out = BANNED_PLATFORM_RE.sub("YouTube Shorts", out)
            out = _FILLER_RE.sub(lambda m: _match_case(m.group(0), _FILLER_FIX[m.group(0).lower()]), out)
            out = re.sub(r"YouTube Shorts(\s*(?:/|,|\+|and|&)\s*(?:YouTube |YT )?Shorts)+", "YouTube Shorts", out, flags=re.IGNORECASE)
            out = re.sub(r"(?:YouTube |YT )?Shorts\s*(?:/|,|\+|and|&)\s*YouTube Shorts", "YouTube Shorts", out, flags=re.IGNORECASE)
            stats = [m.group(0) for m in PRICE_RE.finditer(out)] if key not in ("query", "brief") else []
            if not kpi and key not in PROPOSAL_KEYS:
                stats += unsupported_stats(out, research, allow_targets=allow_targets)
            if stats:
                logger.info("Softened unsupported stat(s)/prices %s on slide %r", stats, slide.title)
                out = soften_stats(out, stats)
            if inputs.dossier and key not in PROPOSAL_KEYS:
                for m in STAT_RE.finditer(out):
                    if (f := fact_for_stat(m.group(0), inputs.dossier)) is not None:
                        stat_facts.append(f.id)
            return out
        return value

    raw = slide.model_dump()
    given = raw.pop("sources", []) or []
    data = fix(raw)
    # Footnotes: cited fact ids + facts behind the figures used -> short labels; labels a model wrote
    # itself are kept only if they name a dossier source.
    known_titles = {f.source_title.lower(): source_label(f) for f in facts.values() if f.source_title}
    known_titles.update({source_label(f).lower(): source_label(f) for f in facts.values()})
    labels: list[str] = []
    for ref in [*cited, *(x for g in given for x in (extract_fact_ids(g) or [g])), *stat_facts]:
        lab = source_label(facts[ref]) if ref in facts else known_titles.get(str(ref).lower())
        if lab and lab not in labels:
            labels.append(lab)
    data["sources"] = labels[:4]
    if isinstance(data.get("hook"), str):
        data["hook"] = data["hook"].strip().strip("\"'\u201c\u201d\u2018\u2019").strip()  # the renderer adds quotes
    if isinstance(data.get("sample_posts"), list):
        data["sample_posts"] = [p.strip().strip("\"'\u201c\u201d").strip() for p in data["sample_posts"] if p and p.strip()]
    if slide.layout == "timeline":
        _shift_past_timeline(data, date.today())
    if slide.layout == "card_grid":
        cards = data.get("cards") or []
        for card in cards:
            if not (card.get("body") or "").strip():
                card["body"] = ", ".join(card.get("tags") or [])
        with_body = [c for c in cards if c["body"].strip()]
        data["cards"] = with_body if len(with_body) >= 2 else [
            {**c, "body": c["body"] or c["title"]} for c in cards
        ]
    try:
        return SLIDE_ADAPTER.validate_python(data)
    except ValidationError as exc:
        logger.warning("Cleaning slide %r broke it (%s); kept the original", slide.title, str(exc)[:160])
        return slide


def _assign_memes(slides: list[Any], inputs: Inputs) -> None:
    """Every meme uses a shortlist template, none twice in the deck, none from the brand's last decks;
    a template that breaks that is swapped for one with the same box count (captions carry over)."""
    shortlist = list(inputs.memes)
    by_id = {m.id: m for m in shortlist}
    try:
        recent = memes.recently_used(inputs.brand)
    except Exception:  # noqa: BLE001 - history is optional
        recent = set()
    used: set[str] = set()
    for v in (v for s in slides for v in _slide_visuals(s) if v.want == "meme"):
        boxes = [b for b in v.meme_boxes if b and b.strip()]
        current = by_id.get(v.meme_template_id or "")
        if current is None or current.id in used or current.id in recent:
            n_boxes = current.box_count if current else len(boxes)
            repl = memes.choose_replacement(current, n_boxes, used, recent, shortlist, v.brief or "")
            if repl is None and current is not None and current.id not in used:
                repl = current  # only a recent-use clash and nothing else fits: keep it
            if repl is None:
                v.want, v.meme_template_id, v.meme_boxes = "mock", None, []
                v.brief = v.brief or " / ".join(boxes)
                continue
            if current is None or repl.id != current.id:
                logger.info("Meme %s -> %s (%s)", v.meme_template_id or "-", repl.id, repl.name)
            current = repl
        used.add(current.id)
        v.meme_template_id = current.id
        v.meme_boxes = (list(v.meme_boxes) + [""] * current.box_count)[: current.box_count]


def _slide_visuals(slide: Any) -> list[Visual]:
    out: list[Visual] = []
    if getattr(slide, "visual", None):
        out.append(slide.visual)
    out += list(getattr(slide, "visuals", []) or [])
    out += [c.visual for c in getattr(slide, "cards", []) or [] if c.visual]
    return out


def _fix_all_visuals(slides: list[Any], memes: dict[str, visuals.MemeTemplate]) -> None:
    for slide in slides:
        targets: list[Visual] = []
        if getattr(slide, "visual", None):
            targets.append(slide.visual)
        targets += list(getattr(slide, "visuals", []) or [])
        for card in getattr(slide, "cards", []) or []:
            if card.visual:
                targets.append(card.visual)
        for v in targets:
            fixed = _fix_visual(
                {"want": v.want, "brief": v.brief, "query": v.query, "meme_template_id": v.meme_template_id,
                 "meme_boxes": list(v.meme_boxes)}, memes,
            )
            v.want, v.query = fixed["want"], fixed.get("query") or v.query
            v.meme_template_id, v.meme_boxes = fixed.get("meme_template_id"), fixed.get("meme_boxes", [])


async def _refresh_shortlist(inputs: Inputs, strategy: Strategy) -> None:
    """Writers get a shortlist fitted to the buckets; the strategist's picks stay valid."""
    hints = [f"{b.name} {b.hook} {b.concept}" for b in strategy.buckets] or [inputs.brand]
    fresh = await _safe(memes.meme_shortlist(inputs.brand, hints, n=15), [], "Meme shortlist (buckets)")
    chosen = {b.visual.meme_template_id for b in strategy.buckets if b.visual.meme_template_id}
    keep = [m for m in inputs.memes if m.id in chosen]
    merged = {m.id: m for m in [*keep, *(m for m in fresh if m.box_count >= 1)]}
    if merged:
        inputs.memes = list(merged.values())
    logger.info("Meme shortlist for writers: %s", [f"{m.id}:{m.name}:{m.box_count}" for m in inputs.memes])


def _confirmed_season(inputs: Inputs, strategy: Strategy) -> str | None:
    """Season passed to the theme only when the strategist builds the plan around it.

    A calendar-only guess the strategist doesn't confirm just flavours image prompts; a moment
    the strategist declines is downgraded to that too.
    """
    ident = inputs.identity
    said = (strategy.season or "").strip().lower()
    if said in SEASONS:
        return said
    # A moment the client's brief names is the brief: no strategist confirmation needed.
    in_brief, source = detect_season_detail(date.today(), inputs.brief or "")
    if in_brief and source == "text":
        return in_brief
    if ident and ident.season and ident.season_source == "text":
        logger.info("Strategist did not build around %s; season only flavours prompts", ident.season)
        ident.season_source = "calendar"
    return None


# =========================================================================== entry point


def _log_summary(brand: str, scale: str, inputs: Inputs, deck: Deck, records: list[llm_router.CallRecord], started: float) -> None:
    per_model: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0])
    for r in records:
        logger.info(
            "  LLM call: %-10s %-28s in=%s out=%s reasoning=%s finish=%s %.1fs%s",
            r.role, r.model, r.input_tokens, r.output_tokens, r.reasoning_tokens, r.finish_reason, r.seconds,
            f" ERROR {r.error}" if r.error else "",
        )
        stats = per_model[r.model]
        stats[0] += 1
        stats[1] += r.input_tokens or 0
        stats[2] += r.output_tokens or 0
    layouts = defaultdict(int)
    for s in deck.slides:
        layouts[s.layout] += 1
    logger.info(
        "Deck for %r: scale=%s (requested %s), %d slides %s, theme=%s, timings %s, total %.1fs, models %s",
        brand, scale, inputs.requested_scale, len(deck.slides), dict(layouts), deck.theme.name,
        {k: round(v, 1) for k, v in inputs.timings.items()}, time.monotonic() - started,
        {m: {"calls": c, "in": i, "out": o} for m, (c, i, o) in per_model.items()},
    )


async def generate_brand_plan(
    brand: str, brief: str | None = None, scale: PlanScale = "auto", on_stage: Any = None,
) -> BrandPlanResponse:
    """Research -> deep research (dossier) -> strategist -> service planner -> per-unit writers -> editor -> deck.

    ``on_stage(stage, progress)`` (sync or async) is called at the C8 stages: research, identity,
    deep_research, strategy, writing, editing, visuals, done.
    """
    started = time.monotonic()
    brief = (brief or "").strip() or None
    with llm_router.track_calls() as records:
        inputs = await _gather_inputs(brand, brief, scale, on_stage)

        await inputs.stage("strategy", STAGE_PROGRESS["strategy"])
        strategy = await _run_strategist(inputs)
        resolved = _resolve_scale(inputs, strategy)
        profile = PROFILES[resolved]
        _normalise_strategy(strategy, inputs, profile)
        logger.info(
            "Strategy for %r: scale=%s (strategist said %r: %s), big idea %r, %d buckets, %d hacks, first services %s, "
            "modules %s, theme %s",
            brand, resolved, strategy.scale, strategy.scale_reason, strategy.big_idea.name, len(strategy.buckets),
            len(strategy.hacks), [p.name for p in strategy.services], strategy.modules, strategy.theme.name,
        )

        # Services woven in: a second strategy call decides fit, combo, per-bucket powering and the matrix.
        t = time.monotonic()
        dossier_view = render_dossier(inputs.dossier, 5000) if inputs.dossier else ""
        planned, _ = await asyncio.gather(
            plan_services(brand, brief, inputs.today, strategy, resolved, inputs.services, inputs.playbooks, dossier_view),
            _refresh_shortlist(inputs, strategy),
        )
        inputs.service_plan = normalise_plan(planned, strategy, inputs.services, resolved)
        inputs.timings["services"] = time.monotonic() - t
        sp = inputs.service_plan
        logger.info(
            "Service plan for %r: %s; lead %s; combo %s; buckets %s",
            brand, [(f.name, extract_fact_ids(f.fit_reason)) for f in sp.services], [f.name for f in sp.services if f.lead],
            " -> ".join(c.service for c in sp.combo), {b.bucket: b.services for b in sp.buckets},
        )

        items = build_slide_plan(strategy, profile, brand, has_memes=bool(inputs.memes), plan=sp)
        await inputs.stage("writing", STAGE_PROGRESS["writing"])
        written = await _run_writers(inputs, strategy, items, profile)

        slides: list[Any] = []
        groups: list[int] = []
        bucket_of: dict[int, int | None] = {}
        prebuilt_idx: set[int] = set()
        special_idx: set[int] = set()
        services_idx: int | None = None
        last_group = 0
        for item in items:
            slide = _prebuilt_slide(item, inputs, strategy) if item.prebuilt else written.get(item.i)
            if slide is None and not item.prebuilt:
                slide = _fallback_slide(item, strategy, brand)
                if slide is not None:
                    logger.info("Slide %d (%s) rebuilt from the strategy", item.i, item.layout)
            if slide is None:
                logger.warning("Slide %d (%s %r) dropped: no valid output", item.i, item.layout, item.title)
                continue
            special = item.prebuilt or item.tags.get("module")
            # Only writer slides are compared with each other (a prebuilt ecosystem timeline is not the rollout).
            plain = [x for k, x in enumerate(slides) if k not in special_idx]
            if not item.required and not special and _duplicate_of(slide, plain) is not None:
                logger.info("Slide %d (%s %r) duplicates an earlier slide; dropped", item.i, slide.layout, slide.title)
                continue
            if special:
                special_idx.add(len(slides))
            if item.prebuilt:
                prebuilt_idx.add(len(slides))
                if item.tags.get("service") == "offer":
                    services_idx = len(slides)
            bucket_of[len(slides)] = item.bucket
            slides.append(slide)
            # Prebuilt slides (group -1) are edited with their neighbours.
            last_group = item.group if item.group >= 0 else last_group
            groups.append(last_group)

        if len(slides) < max(4, profile.min_slides // 2):
            raise PlanGenerationError(f"only {len(slides)} usable slides were written")
        enforce_idea_services(slides, bucket_of, sp, strategy, inputs.services)
        await inputs.stage("editing", STAGE_PROGRESS["editing"])
        try:
            slides = await _run_editor(inputs, strategy, slides, groups, skip=prebuilt_idx)
        except Exception as exc:  # noqa: BLE001 - the editor is a polish pass
            logger.warning("Editor pass failed (%s: %s); keeping the unedited deck", type(exc).__name__, str(exc)[:200])

        _enforce_services(slides, strategy, inputs, services_idx)
        enforce_idea_services(slides, bucket_of, sp, strategy, inputs.services)
        _fix_all_visuals(slides, inputs.meme_ids)
        slides = [_clean_slide(s, inputs) for s in slides]
        _assign_memes(slides, inputs)
        residual = find_issues(slides, inputs.banned, inputs.research_text)
        if residual:
            logger.warning("Rule issues left after editing: %s", residual)

        choice = themes.ThemeChoice(
            name=strategy.theme.name, accent_hint=strategy.theme.accent_hint or None, hero_prompt=strategy.theme.hero_prompt,
        )
        season = _confirmed_season(inputs, strategy)
        try:
            theme = themes.resolve_theme(choice, inputs.identity or inputs.colors, season=season)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Theme resolution failed (%s); using the default theme", exc)
            theme = Theme(name=choice.name)

        deck = Deck(title=strategy.title, brand=brand, scale=resolved, theme=theme, slides=slides)
        await inputs.stage("visuals", STAGE_PROGRESS["visuals"])
        t = time.monotonic()
        try:
            deck = await visuals.attach_visuals(deck, profile.visuals)
        except Exception as exc:  # noqa: BLE001 - visuals never fail the plan
            logger.warning("Attaching visuals failed (%s: %s); deck keeps placeholders", type(exc).__name__, str(exc)[:200])
        inputs.timings["visuals"] = time.monotonic() - t

        plan = deck_to_markdown(deck)
        _log_summary(brand, resolved, inputs, deck, records, started)
    await inputs.stage("done", STAGE_PROGRESS["done"])

    return BrandPlanResponse(
        brand=brand,
        plan=plan,
        research=inputs.research,
        references=[PastPitchRef(pitch_id=p.pitch_id, campaign=p.campaign, client=p.client) for p in inputs.pitches],
        scale=resolved,
        brief=brief,
        deck=deck,
        dossier=inputs.dossier,
    )
