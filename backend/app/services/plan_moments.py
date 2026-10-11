"""Verify the real-world moments a strategy relies on (festivals, tournaments, sales, launches).

LLMs don't know today's calendar, so they guess dates to make a moment feel "now" (e.g.
"IPL 2026, matches Oct-Nov" pitched in October). Nothing about a dated event is taken from the
model's memory: the events the strategy builds on are extracted, searched live (Tavily), and
their next occurrence is read **only from the sources**, with the source URL kept. Status
(live / upcoming / past) is computed in code from those dates. The built-in festival calendar
(``brand_identity_season``) is only a fallback when the sources don't say.
"""

import asyncio
import json
import logging
import re
from datetime import date, timedelta

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field
from tavily import AsyncTavilyClient

from app.core.config import settings
from app.schemas.research import BrandDossier, DossierFact
from app.services import llm_router
from app.services.brand_identity_season import SEASONS, fix_dates_for, season_window

logger = logging.getLogger(__name__)

MAX_MOMENTS = 3
# Answer farms and social posts are not a source for event dates.
_UNTRUSTED_DATE_SOURCES = [
    "facebook.com", "instagram.com", "quora.com", "reddit.com", "x.com", "twitter.com", "pinterest.com",
    "youtube.com", "tiktok.com", "medium.com", "threads.com", "threads.net", "linkedin.com", "telegram.org",
]


class MomentClaim(BaseModel):
    event: str = Field(description="Searchable event name, e.g. 'IPL 2027', 'Diwali', 'Amazon Great Indian Festival'")
    keywords: list[str] = Field(default_factory=list, description="Short aliases as written in slides, e.g. ['IPL', 'T20']")


class MomentClaims(BaseModel):
    moments: list[MomentClaim] = Field(default_factory=list)


class MomentDates(BaseModel):
    start: str | None = Field(None, description="YYYY-MM-DD, only if a source states it")
    end: str | None = Field(None, description="YYYY-MM-DD, only if a source states it")
    dates_text: str = Field("", description="Human form as the sources state it, e.g. '14 Mar–30 May 2027 (tentative)'")
    source_url: str | None = None


class VerifiedMoment(BaseModel):
    event: str
    keywords: list[str] = Field(default_factory=list)
    status: str = "unknown"  # live | upcoming | past | unknown (computed in code)
    start: date | None = None
    end: date | None = None
    dates_text: str = ""
    source_url: str | None = None
    basis: str = "sources"  # sources | calendar estimate
    end_known: bool = True  # False when the sources state only a start date

    def line(self, today: date) -> str:
        if self.status == "unknown" or not self.start:
            return f"{self.event}: dates NOT verified; do not state any dates for it."
        src = f"source: {_domain(self.source_url)}" if self.source_url else self.basis
        if self.status == "live":
            when = "live now"
        elif self.status == "upcoming":
            weeks = max(1, (self.start - today).days // 7)
            when = f"NOT live now, starts in ~{weeks} week{'s' if weeks > 1 else ''}"
        else:
            when = "already over"
        return f"{self.event}: {self.dates_text or _span(self.start, self.end)} ({when}; {src})."


_EXTRACT_SYSTEM = """\
List the real-world, dated events this marketing strategy relies on: festivals, sports seasons or \
tournaments, sale events, product launches, film/music releases, award shows. Not the campaign's own \
phases. At most 3, most important first. For each give a searchable name (add the year you think it is) \
and 1-4 short aliases exactly as they would appear in slide text (e.g. "IPL", "T20", "Diwali"). \
Return JSON {"moments": [{"event": "...", "keywords": ["..."]}]}; {"moments": []} if none."""

_DATES_SYSTEM = """\
You read search results and report when an event's NEXT occurrence happens: the one that ends on or \
after TODAY. Use ONLY dates stated in the SOURCES; never use your own memory. If the sources give only \
a month range, or say the dates are reported/expected/likely, use them and put "(tentative)" in dates_text. If the \
sources don't state dates for the next occurrence, return nulls and an empty dates_text. \
source_url must be one of the given source URLs. Return JSON \
{"start": "YYYY-MM-DD"|null, "end": "YYYY-MM-DD"|null, "dates_text": "...", "source_url": "..."|null}."""


def _domain(url: str | None) -> str:
    m = re.match(r"https?://(?:www\.)?([^/]+)", url or "")
    return m.group(1) if m else (url or "")


def _span(start: date, end: date | None) -> str:
    end = end or start
    if start.year == end.year:
        return f"{start.day} {start:%b}–{end.day} {end:%b} {end.year}"
    return f"{start.day} {start:%b} {start.year}–{end.day} {end:%b} {end.year}"


def _parse(value: str | None) -> date | None:
    try:
        return date.fromisoformat((value or "")[:10])
    except ValueError:
        return None


def _strategy_view(strategy) -> str:
    view = {
        "title": strategy.title, "moment": strategy.moment, "season": strategy.season, "why_now": strategy.why_now,
        "big_idea": getattr(strategy.big_idea, "name", ""),
        "phases": [{"name": p.name, "when": getattr(p, "when", "")} for p in strategy.phases][:6],
        "buckets": [b.name for b in strategy.buckets][:6],
    }
    return json.dumps(view, ensure_ascii=False)


def _calendar_fallback(claim: MomentClaim, today: date) -> VerifiedMoment | None:
    text = " ".join([claim.event, *claim.keywords]).lower()
    for season in SEASONS.values():
        if any(re.search(r"(?<![a-z])" + re.escape(kw) + r"(?![a-z])", text) for kw in season.keywords):
            window = season_window(season.key, today)
            if window:
                return VerifiedMoment(
                    event=re.sub(r"\b20\d\d\b", str(window[0].year), claim.event), keywords=claim.keywords,
                    start=window[0], end=window[1], dates_text=f"~{_span(*window)}", basis="calendar estimate",
                )
    return None


async def _verify_one(client: AsyncTavilyClient | None, claim: MomentClaim, today: date, i: int) -> VerifiedMoment:
    found = VerifiedMoment(event=claim.event, keywords=claim.keywords)
    if client is not None:
        try:
            # Search for the event itself, not the model's guessed year: this year's and next year's edition.
            base = re.sub(r"\b20\d\d\b", "", claim.event).strip()
            results = await asyncio.gather(*(
                client.search(
                    f"{base} {year} dates schedule", search_depth="advanced", max_results=4,
                    include_answer="advanced", exclude_domains=_UNTRUSTED_DATE_SOURCES,
                )
                for year in (today.year, today.year + 1)
            ))
            sources = [r for res in results for r in res.get("results", [])]
            urls = {r["url"] for r in sources}
            answers = " | ".join((res.get("answer") or "")[:500] for res in results)
            listing = "\n".join(f"- {r['url']}: {r.get('content', '')[:450]}" for r in sources)
            messages = [
                SystemMessage(_DATES_SYSTEM),
                HumanMessage(
                    f"TODAY: {today.isoformat()}\nEVENT: {claim.event}\n"
                    f"SEARCH ANSWERS (unsourced hints): {answers}\nSOURCES:\n{listing}"
                ),
            ]
            dates = await llm_router.invoke_json("distiller", messages, MomentDates, max_tokens=300, temperature=0.0, prefer=i)
            start, end = _parse(dates.start), _parse(dates.end) or _parse(dates.start)
            if start and end and end >= start and dates.source_url in urls:
                found.start, found.end, found.source_url = start, end, dates.source_url
                found.end_known = _parse(dates.end) is not None and end != start
                tentative = "tentative" in (dates.dates_text or "").lower()
                when = _span(start, end) if found.end_known else f"from {start.day} {start:%b %Y}"
                found.dates_text = when + (" (tentative)" if tentative else "")
                # The model's guessed year ("IPL 2026") gives way to the verified occurrence's year.
                if re.search(r"\b20\d\d\b", found.event):
                    found.event = re.sub(r"\b20\d\d\b", str(start.year), found.event)
        except Exception as exc:  # noqa: BLE001 - verification is best effort; never fail the plan
            logger.info("Moment check for %r failed (%s)", claim.event, exc)
    if not found.start:
        found = _calendar_fallback(claim, today) or found
    if found.start:
        if found.start > today:
            found.status = "upcoming"
        elif found.end_known and found.end < today:
            found.status = "past"
        else:
            found.status = "live"
    return found


async def verify_moments(strategy, today: date) -> list[VerifiedMoment]:
    """Events the strategy builds on, with their next real dates taken from live sources."""
    try:
        claims = await llm_router.invoke_json(
            "distiller", [SystemMessage(_EXTRACT_SYSTEM), HumanMessage(_strategy_view(strategy))],
            MomentClaims, max_tokens=300, temperature=0.0,
        )
    except Exception as exc:  # noqa: BLE001
        logger.info("Moment extraction failed (%s); no moments verified", exc)
        return []
    picked = [c for c in claims.moments if c.event.strip()][:MAX_MOMENTS]
    if not picked:
        return []
    client = AsyncTavilyClient(api_key=settings.tavily_api_key) if settings.tavily_api_key else None
    moments = await asyncio.gather(*(_verify_one(client, c, today, i) for i, c in enumerate(picked)))
    for m in moments:
        logger.info("Moment %r: %s %s (%s)", m.event, m.status, m.dates_text or "-", m.source_url or m.basis)
    return list(moments)


def moments_prompt(moments: list[VerifiedMoment], today: date) -> str:
    if not moments:
        return ""
    lines = "\n".join(f"- {m.line(today)}" for m in moments)
    return (
        f"EVENT DATES (today {today:%d %b %Y}; from live sources or a stated calendar estimate, never memory):\n{lines}\n"
        "Use only these dates for these events and never move an event to fit 'now'. If an event is weeks "
        "or months away, pitch a build-up timed to its real window; if it is over, don't build on it."
    )


def add_moment_facts(dossier: BrandDossier | None, moments: list[VerifiedMoment]) -> None:
    """Verified dates become sourced dossier facts (footnotes + the 'supported' corpus)."""
    if dossier is None:
        return
    for i, m in enumerate(moments, 1):
        if m.start and m.source_url:
            dossier.facts.append(DossierFact(
                id=f"M{i}", text=f"{m.event}: {m.dates_text or _span(m.start, m.end)}", source_url=m.source_url,
                source_title=_domain(m.source_url),
            ))


def fix_moment_dates(text: str, moments: list[VerifiedMoment], today: date) -> str:
    for m in moments:
        if m.start and m.end:
            # Start-only sources: tolerate months in the following quarter, but never print an invented end.
            end = m.end if m.end_known else m.start + timedelta(days=90)
            label = None if m.end_known else m.dates_text
            for kw in {m.event, *m.keywords}:
                if kw.strip():
                    text = fix_dates_for(text, kw.strip(), m.start, end, today, label=label)
    return text


def covered_keywords(moments: list[VerifiedMoment]) -> set[str]:
    return {kw.lower() for m in moments if m.start for kw in (m.event, *m.keywords)}
