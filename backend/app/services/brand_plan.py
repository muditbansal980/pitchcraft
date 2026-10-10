"""Brand plan generation: research -> retrieval -> prompt -> Groq -> BrandPlanResponse.

Token budget (Groq free tier for openai/gpt-oss-120b: 8,000 tokens/minute,
prompt + completion incl. reasoning): the prompt is kept to ~4,200 tokens by
fixed per-block character caps (see the _MAX_* constants) and the completion is
capped at PLAN_MAX_TOKENS, so one request stays under 8K.
"""

import asyncio
import logging
import re
from datetime import date

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_groq import ChatGroq

from app.core.config import settings
from app.schemas.brand import ResearchSection
from app.schemas.plan import BrandPlanResponse, PastPitchRef
from app.services.brand_research import research_brand
from app.services.retrieval import (
    PitchMatch,
    Service,
    build_pitch_queries,
    clip_text,
    find_similar_pitches,
    load_services_catalog,
)

logger = logging.getLogger(__name__)

PLAN_MAX_TOKENS = 3500
PROMPT_TOKEN_BUDGET = 4200

# Per-block character caps (~4 chars/token).
_MAX_SUMMARY_CHARS = 850
_SOURCES_PER_SECTION = 2
_MAX_SOURCE_TITLE_CHARS = 90
_MAX_SNIPPET_CHARS = 240
_MAX_BRIEF_CHARS = 380
_MAX_EXCERPT_CHARS = 320
_EXCERPTS_PER_PITCH = 1


class ServicesCatalogEmptyError(RuntimeError):
    """The wldd_services collection has not been ingested."""


class PlanGenerationError(RuntimeError):
    """The LLM returned no usable plan."""


SYSTEM_PROMPT = """\
You are the strategy team at WLDD, an Indian creator, meme and influencer marketing agency. \
You are writing a proposal addressed to the marketing team of {brand}. It is the document \
{brand} will receive, so write as WLDD ("we") speaking to {brand} ("you"). It is a forward-looking \
plan for {brand}, not a summary of WLDD's history. Today's date is {today}.

You are given three inputs:
1. BRAND RESEARCH: recent web research on {brand}. It is your only source of facts about {brand}.
2. WLDD SERVICES: the complete list of services WLDD offers. You may only recommend these.
3. PAST PITCHES: internal, confidential pitches WLDD wrote for other brands. Use them only as \
inspiration for tactics, formats and campaign mechanics. They are pitches, not case studies.

Write the plan in Markdown with exactly this structure:

# <campaign title>

## Where {brand} is today
2-4 sentences on the brand's position, audience and recent marketing, grounded only in the research.

## The opportunity
What {brand} should aim for in the next few months and why creator-led and meme-led marketing in \
India is the right way to get there. End with one clear objective.

## The big idea: <hook or hashtag>
One campaign concept with a memorable hook or hashtag that fits Indian internet culture (Hinglish \
is welcome where natural). Explain the idea in 3-5 sentences and give 2-3 example content ideas \
that only make sense for {brand}.

## What we can offer
The core of the plan. Choose the 3-6 WLDD services that genuinely fit {brand}'s goals. For each one:
### <service name exactly as written in WLDD SERVICES>
- **Why it fits {brand}:** one or two sentences tied to the research or the big idea.
- **What we'd do:** 2-4 concrete bullets: formats, platforms (Instagram, YouTube, X, WhatsApp and so \
on), creator tiers (nano, micro, mid, macro), proposed volumes (for example "25 micro creators, 2 reels \
each") and specific content ideas for {brand}.

## How it rolls out
3-4 phases (for example Tease, Launch, Sustain) with durations in weeks and which services lead each phase.

## How we'll measure success
4-6 KPIs matched to the objective (reach, engagement rate, share of voice, sentiment, UGC volume, \
search lift and so on). Do not invent target numbers about {brand}'s current performance.

## Next steps
2-4 short bullets for what happens after {brand} says yes.

Rules:
- If BRAND RESEARCH shows a sale event, festival, launch or other big moment that is happening now \
or within the next ~8 weeks (for example a festive sale), name it in "Where {brand} is today", build \
the big idea around it and time the rollout phases to it. If it has just ended, ride its momentum \
into the next moment. Never write a plan that ignores a moment happening right now.
- The title names the campaign the way agency pitches do. Vary the style, for example \
"{brand} x <idea or moment>", "<idea> – {brand}" or "{brand} <idea> <year>". Never title it \
"{brand} × WLDD" or "{brand} x WLDD".
- Spell {brand} exactly as given in titles and hashtags.
- Use only facts about {brand} that appear in BRAND RESEARCH. Never invent statistics, follower \
counts, revenue, market share, launches or quotes. If the research is thin, keep "Where {brand} is \
today" short and general rather than guessing.
- Recommend only services listed in WLDD SERVICES and use their exact names. Skip services that do \
not fit; for example, Twitter Trend suits film, music and cause campaigns where trending is the goal.
- Proposed creator counts, content volumes and phase lengths are your recommendations and are fine. \
Do not mention prices, fees, budgets or costs.
- Do not mention the past pitches, their brands, their campaigns or any creators or influencers named \
in them, do not quote their text, and do not describe WLDD's track record. Borrow mechanics from them \
silently and adapt them to {brand}.
- Be specific to {brand}: every content idea should be something another brand could not run unchanged.
- Keep it to about 900-1,300 words. Output only the Markdown plan, with no preamble.\
"""


def get_llm() -> BaseChatModel:
    return ChatGroq(
        model=settings.groq_model,
        api_key=settings.groq_api_key,
        temperature=0.7,
        max_tokens=PLAN_MAX_TOKENS,
        reasoning_effort=settings.groq_reasoning_effort,
        # The SDK retries 429s with backoff; one retry keeps the request bounded.
        max_retries=1,
        timeout=120,
    )


def estimate_tokens(text: str) -> int:
    return len(text) // 4 + 1


def format_research(research: list[ResearchSection]) -> str:
    blocks = []
    for section in research:
        lines = [f"### {section.title}", clip_text(section.summary, _MAX_SUMMARY_CHARS)]
        for source in section.sources[:_SOURCES_PER_SECTION]:
            title = clip_text(source.title, _MAX_SOURCE_TITLE_CHARS)
            snippet = clip_text(source.snippet, _MAX_SNIPPET_CHARS)
            lines.append(f"- {title}: {snippet}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) or "No research available."


def format_services(services: list[Service]) -> str:
    return "\n".join(f"- **{s.name}**: {s.description}" for s in services)


def format_pitches(pitches: list[PitchMatch]) -> str:
    if not pitches:
        return "None available."
    blocks = []
    for p in pitches:
        lines = [f"### {p.campaign} ({p.client}) | services pitched: {p.services}"]
        if p.brief:
            lines.append(f"Brief: {clip_text(p.brief, _MAX_BRIEF_CHARS)}")
        for excerpt in p.excerpts[:_EXCERPTS_PER_PITCH]:
            lines.append(f"Deck excerpt: {clip_text(excerpt, _MAX_EXCERPT_CHARS)}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def build_messages(
    brand: str,
    research: list[ResearchSection],
    services: list[Service],
    pitches: list[PitchMatch],
) -> list[SystemMessage | HumanMessage]:
    user = (
        f"BRAND RESEARCH ({brand})\n{format_research(research)}\n\n"
        f"WLDD SERVICES\n{format_services(services)}\n\n"
        f"PAST PITCHES (internal inspiration only)\n{format_pitches(pitches)}\n\n"
        f"Write the plan for {brand}."
    )
    return [SystemMessage(SYSTEM_PROMPT.format(brand=brand, today=date.today().strftime("%d %B %Y"))), HumanMessage(user)]


def _clean_plan(content: str) -> str:
    text = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
    fenced = re.fullmatch(r"```(?:markdown|md)?\s*\n(.*?)\n?```", text, flags=re.DOTALL)
    return (fenced.group(1) if fenced else text).strip()


async def generate_brand_plan(brand: str) -> BrandPlanResponse:
    services = await asyncio.to_thread(load_services_catalog)
    if not services:
        raise ServicesCatalogEmptyError("WLDD services collection is empty")

    research = (await research_brand(brand)).sections
    queries = build_pitch_queries(brand, research)
    pitches = await asyncio.to_thread(find_similar_pitches, queries)

    messages = build_messages(brand, research, services, pitches)
    prompt_tokens = sum(estimate_tokens(m.content) for m in messages)
    if prompt_tokens > PROMPT_TOKEN_BUDGET:
        logger.warning("Plan prompt for %r is ~%d tokens (budget %d)", brand, prompt_tokens, PROMPT_TOKEN_BUDGET)

    response = await get_llm().ainvoke(messages)
    plan = _clean_plan(response.content if isinstance(response.content, str) else str(response.content))

    finish_reason = response.response_metadata.get("finish_reason")
    usage = response.usage_metadata if isinstance(response, AIMessage) else None
    logger.info("Plan for %r: est. prompt %d tokens, usage %s, finish %s", brand, prompt_tokens, usage, finish_reason)
    if finish_reason == "length":
        logger.warning("Plan for %r hit max_tokens=%d and may be cut off", brand, PLAN_MAX_TOKENS)
    if not plan:
        raise PlanGenerationError(
            "The model returned an empty plan"
            + (" (it ran out of tokens while reasoning)" if finish_reason == "length" else "")
        )

    return BrandPlanResponse(
        brand=brand,
        plan=plan,
        research=research,
        references=[PastPitchRef(pitch_id=p.pitch_id, campaign=p.campaign, client=p.client) for p in pitches],
    )
