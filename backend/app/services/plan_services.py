"""WLDD services woven into the pitch (plan section 6).

A second strategy call, the SERVICE PLANNER, runs after the strategist. It scores the
catalog services against the brief and the brand dossier using WLDD's own playbooks
(``service_playbooks.load_playbooks()``, falling back to the catalog descriptions), and decides:
- which services, each with a ``fit_reason`` citing dossier fact ids;
- the ``combo``: how the chosen services chain together for this brand;
- which services power each content bucket, and the mechanics (volumes, tiers);
- a services x phases matrix;
- for the 1-3 lead services, a 3-step "how it works for <brand>" flow with deliverables and KPIs.

The service slides (offer table, ecosystem, deep-dives, matrix) are built from that plan in
code, so names stay exact and nothing is left to a writer's memory. ``enforce_idea_services``
guarantees every idea is powered by at least one chosen service and every chosen service
powers at least one idea.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import ValidationError

from app.schemas.deck import (
    IdeaSlide,
    Phase,
    StatementSlide,
    TableSlide,
    TimelineSlide,
)
from app.schemas.research import BrandDossier
from app.services import llm_router
from app.services.plan_checks import match_service, similar_titles
from app.services.plan_dossier import extract_fact_ids, facts_by_id, source_label, strip_fact_ids
from app.services.plan_models import Str, StrList, Strategy, _Lenient, _objects
from app.services.retrieval import Service, clip_text

logger = logging.getLogger(__name__)

SERVICE_COUNTS = {"focused": (1, 3), "standard": (3, 5), "flagship": (4, 6)}
LEAD_COUNTS = {"focused": 0, "standard": 1, "flagship": 3}
PLANNER_MAX_TOKENS = {"focused": 1500, "standard": 2400, "flagship": 3000}


class FlowStep(_Lenient):
    name: Str = ""
    detail: Str = ""


class ServiceFit(_Lenient):
    name: Str
    fit_reason: Str = ""
    role: Str = ""  # its job in the combo, one line
    what: Str = ""
    volumes: Str = ""
    kpi: Str = ""
    lead: bool = False
    steps: Annotated[list[FlowStep], _objects("name")] = []
    deliverables: StrList = []


class ComboStep(_Lenient):
    service: Str
    step: Str = ""


class BucketServices(_Lenient):
    bucket: Str
    services: StrList = []
    mechanics: Str = ""


class MatrixRow(_Lenient):
    service: Str
    cells: StrList = []


class ServicePlan(_Lenient):
    services: Annotated[list[ServiceFit], _objects("name")] = []
    combo: Annotated[list[ComboStep], _objects("service")] = []
    combo_summary: Str = ""
    buckets: Annotated[list[BucketServices], _objects("bucket")] = []
    matrix: Annotated[list[MatrixRow], _objects("service")] = []

    @property
    def names(self) -> list[str]:
        return [s.name for s in self.services]

    def for_bucket(self, bucket_name: str) -> BucketServices | None:
        return next((b for b in self.buckets if similar_titles(b.bucket, bucket_name)), None)


PLANNER_SYSTEM = """\
You are WLDD's services lead. WLDD is an Indian creator, meme and influencer agency. The strategy for \
{brand} is set; now decide which WLDD services deliver it and how they work TOGETHER for {brand}. Today is {today}.

Use the PLAYBOOKS: they describe how WLDD really runs each service (mechanics, volumes, tiers, KPIs). Choose \
{n_min}-{n_max} services that this brief and this brand actually need (fit beats coverage). Ground every \
fit_reason in the DOSSIER and cite fact ids like [F3] where a fact supports it. Signals to look for: ORM when \
the news shows a sentiment issue; Offline Activity for college/IRL audiences; Twitter Trend when trending is \
the goal; Memed/Amplification for meme-led reach; SOLO for UGC at scale; Influencers for credibility.

Return ONE JSON object:
{{"services": [{{"name": "exact catalog name", "fit_reason": "<=30 words, why {brand} needs it [F#]",
  "role": "its job in the combo, <=12 words", "what": "<=30 words: what we will do for {brand}",
  "volumes": "e.g. 40 creators x 2 reels, 25 meme pages x 3 waves", "kpi": "the KPI it moves",
  "lead": true|false, "steps": [{{"name": "step", "detail": "<=25 words with volumes/deliverables"}}] (3 steps, lead services only),
  "deliverables": ["..."] (lead services only)}}],
"combo": [{{"service": "exact name", "step": "<=20 words: what it does and what it hands to the next service"}}],
"combo_summary": "one line: how the services chain together for {brand}",
"buckets": [{{"bucket": "bucket name exactly", "services": ["exact names"], "mechanics": "<=35 words, e.g. Memed: 40 creators x 2 clips; Amplification: 25 meme pages, 3 waves"}}],
"matrix": [{{"service": "exact name", "cells": ["what it does in each PHASE, in order (<=8 words each, '-' if idle)"]}}]}}

Rules: exact catalog names only; mark {n_lead} lead service(s); every bucket gets at least one service and \
every chosen service powers at least one bucket; volumes are proposals (targets); no prices, fees or budgets; \
never name real creators or past clients."""


def render_playbooks(services: list[Service], playbooks: dict[str, Any], compact: bool) -> str:
    blocks = []
    per = 2 if compact else 4
    for s in services:
        pb = playbooks.get(s.name)
        lines = [f"### {s.name}: {clip_text(s.description, 170 if compact else 320)}"]
        if pb is not None:
            for label, values in (
                ("mechanics", pb.mechanics), ("volumes", pb.typical_volumes), ("tiers", pb.creator_tiers),
                ("handles", pb.handle_categories), ("phase", pb.phase_fit), ("deliverables", pb.deliverables),
                ("kpis", pb.kpis), ("best for", pb.best_for), ("pairs with", pb.pairs_with),
            ):
                if values:
                    lines.append(f"{label}: " + "; ".join(clip_text(v, 90) for v in values[:per]))
        blocks.append("\n".join(lines))
    return "\n".join(blocks)


def planner_messages(
    brand: str, brief: str | None, today: str, strategy: Strategy, scale: str, services: list[Service],
    playbooks: dict[str, Any], dossier_view: str, compact: bool,
) -> list:
    n_min, n_max = SERVICE_COUNTS[scale]
    system = PLANNER_SYSTEM.format(brand=brand, today=today, n_min=n_min, n_max=n_max, n_lead=LEAD_COUNTS[scale] or "no")
    big = strategy.big_idea
    buckets = "\n".join(f"- {b.name}: {clip_text(b.concept, 140)} ({b.format})" for b in strategy.buckets)
    phases = " | ".join(f"{p.name} ({p.when})" for p in strategy.phases) or "Tease | Launch | Sustain"
    first_picks = ", ".join(p.name for p in strategy.services) or "none"
    human = (
        f"BRAND: {brand}\nCLIENT BRIEF: {brief or 'none'}\n"
        f"BIG IDEA: {big.name}: {big.hook}. {clip_text(big.idea, 200)}\n"
        f"BUCKETS:\n{buckets}\nPHASES (matrix columns, in order): {phases}\n"
        f"STRATEGIST'S FIRST PICKS: {first_picks}\n\n"
        f"DOSSIER\n{dossier_view or 'thin: use the brief'}\n\n"
        f"PLAYBOOKS\n{render_playbooks(services, playbooks, compact)}\n\n"
        f"Write the service plan JSON for {brand}."
    )
    return [SystemMessage(system), HumanMessage(human)]


async def plan_services(
    brand: str, brief: str | None, today: str, strategy: Strategy, scale: str, services: list[Service],
    playbooks: dict[str, Any], dossier_view: str,
) -> ServicePlan | None:
    """Run the service planner; None if it fails (callers fall back to the strategist's picks)."""
    max_tokens = PLANNER_MAX_TOKENS[scale]
    compact = llm_router.prompt_capacity("strategist", max_tokens) < 20_000
    messages = planner_messages(brand, brief, today, strategy, scale, services, playbooks, dossier_view, compact)
    capacity = llm_router.prompt_capacity("strategist", max_tokens)
    est = llm_router._messages_tokens(messages)
    if est > capacity and dossier_view:
        # Groq-sized model: shrink the dossier part to fit.
        cut = max(800, len(dossier_view) - (est - capacity) * 4 - 200)
        messages = planner_messages(brand, brief, today, strategy, scale, services, playbooks, dossier_view[:cut], compact)
        est = llm_router._messages_tokens(messages)
    logger.info("Service planner: ~%d prompt tokens (capacity %d), max_tokens %d", est, capacity, max_tokens)
    try:
        # prefer=1: a different model from the strategist's, so the two calls don't queue on one TPM budget.
        return await llm_router.invoke_json("strategist", messages, ServicePlan, max_tokens=max_tokens, temperature=0.5, prefer=1)
    except (llm_router.LLMOutputError, llm_router.LLMUnavailableError, ValidationError) as exc:
        logger.warning("Service planner failed (%s: %s); using the strategist's picks", type(exc).__name__, str(exc)[:200])
        return None


def normalise_plan(plan: ServicePlan | None, strategy: Strategy, catalog: list[Service], scale: str) -> ServicePlan:
    """Exact names, sane counts, every bucket powered, every service used, matrix rows for every service."""
    n_min, n_max = SERVICE_COUNTS[scale]
    plan = plan or ServicePlan()
    fits: dict[str, ServiceFit] = {}
    for fit in plan.services:
        svc = match_service(fit.name, catalog)
        if svc is None:
            logger.warning("Service planner picked unknown service %r; dropped", fit.name)
            continue
        if svc.name not in fits:
            fit.name = svc.name
            fits[svc.name] = fit
    # Fall back to / top up from the strategist's picks.
    for pick in strategy.services:
        if len(fits) >= n_min:
            break
        svc = match_service(pick.name, catalog)
        if svc and svc.name not in fits:
            fits[svc.name] = ServiceFit(name=svc.name, fit_reason=pick.why, what=pick.what or clip_text(svc.description, 160))
    if not fits:
        for svc in catalog:
            if svc.name.lower().startswith(("amplification", "influencers")):
                fits[svc.name] = ServiceFit(name=svc.name, what=clip_text(svc.description, 160))
    chosen = list(fits.values())[:n_max]
    if not any(f.lead for f in chosen) and LEAD_COUNTS[scale]:
        chosen[0].lead = True
    lead_left = LEAD_COUNTS[scale]
    for f in chosen:
        if f.lead and lead_left > 0:
            lead_left -= 1
        else:
            f.lead = False
    plan.services = chosen
    names = [f.name for f in chosen]

    # Combo: chosen services only, in the planner's order, then any missing ones.
    combo, seen = [], set()
    for step in plan.combo:
        svc = match_service(step.service, catalog)
        if svc and svc.name in names and svc.name not in seen:
            step.service = svc.name
            combo.append(step)
            seen.add(svc.name)
    for f in chosen:
        if f.name not in seen:
            combo.append(ComboStep(service=f.name, step=f.role or f.what))
    plan.combo = combo

    # Buckets: exact bucket names, exact service names, at least one service each.
    buckets: list[BucketServices] = []
    for bi, b in enumerate(strategy.buckets):
        got = next((x for x in plan.buckets if similar_titles(x.bucket, b.name)), None)
        if got is None and len(plan.buckets) == len(strategy.buckets):
            got = plan.buckets[bi]  # same count, names drifted: trust the order
        svcs = []
        for n in (got.services if got else []):
            svc = match_service(n, catalog)
            if svc and svc.name in names and svc.name not in svcs:
                svcs.append(svc.name)
        buckets.append(BucketServices(bucket=b.name, services=svcs, mechanics=got.mechanics if got else ""))
    # Every bucket powered; every service used somewhere.
    for bs in buckets:
        if not bs.services and names:
            bs.services = [names[0]]
    for n in names:
        if buckets and not any(n in bs.services for bs in buckets):
            min(buckets, key=lambda x: len(x.services)).services.append(n)
    plan.buckets = buckets

    # Matrix: one row per chosen service, one cell per phase.
    n_phases = max(1, len(strategy.phases))
    rows = {}
    for row in plan.matrix:
        svc = match_service(row.service, catalog)
        if svc and svc.name in names:
            rows[svc.name] = row.cells
    plan.matrix = [
        MatrixRow(service=n, cells=(list(rows.get(n, [])) + ["-"] * n_phases)[:n_phases]) for n in names
    ]
    return plan


# --------------------------------------------------------------------------- slides built from the plan


def _sources(texts: list[str], dossier: BrandDossier | None) -> list[str]:
    facts = facts_by_id(dossier)
    labels: list[str] = []
    for t in texts:
        for fid in extract_fact_ids(t):
            if fid in facts and (lab := source_label(facts[fid])) not in labels:
                labels.append(lab)
    return labels[:4]


def offer_table(plan: ServicePlan, brand: str, big_idea: str, dossier: BrandDossier | None) -> TableSlide:
    rows = [
        [f.name, strip_fact_ids(f.fit_reason) or "-", strip_fact_ids(f.what) or "-", f.volumes or "-", f.kpi or "-"]
        for f in plan.services
    ]
    return TableSlide(
        title="What we can offer",
        strip=f"The WLDD services that make {big_idea or 'this'} happen for {brand}",
        headers=["Service", f"Why it fits {brand}", "What we'll do", "Volumes", "KPI"],
        rows=rows,
        sources=_sources([f.fit_reason for f in plan.services], dossier),
        notes=plan.combo_summary or "Exact scope and volumes get locked with your team after the brief call.",
    )


def ecosystem_slide(plan: ServicePlan, brand: str) -> TimelineSlide | None:
    steps = plan.combo[:6]
    if len(steps) < 2:
        return None
    return TimelineSlide(
        title="How it all connects",
        strip=plan.combo_summary or f"One engine for {brand}: each service feeds the next",
        phases=[
            Phase(name=clip_text(s.service, 40), when=f"Step {k}", points=[strip_fact_ids(s.step)] if s.step else [])
            for k, s in enumerate(steps, start=1)
        ],
        notes=plan.combo_summary,
    )


def matrix_slide(plan: ServicePlan, strategy: Strategy) -> TableSlide | None:
    if not plan.matrix or not strategy.phases:
        return None
    phases = [p.name for p in strategy.phases][:5]
    return TableSlide(
        title="Who does what, when",
        strip="Every service, every phase",
        headers=["Service", *phases],
        rows=[[row.service, *row.cells[: len(phases)]] for row in plan.matrix],
    )


def deep_dive_slides(plan: ServicePlan, brand: str, dossier: BrandDossier | None) -> list[Any]:
    """'Why <service> for <brand>' + 'How it works for <brand>' for each lead service."""
    out: list[Any] = []
    for f in (f for f in plan.services if f.lead):
        short = f.name.split("(")[0].strip()
        why = strip_fact_ids(f.fit_reason)
        body = " ".join(x for x in (why, f"What we'll do: {strip_fact_ids(f.what)}" if f.what else "") if x)
        bullets: list[str] = []
        for b in (f.volumes and f"Volumes: {f.volumes}", f.kpi and f"Moves: {f.kpi}",
                  f.deliverables and "Deliverables: " + ", ".join(f.deliverables[:3]),
                  f.role and f"Role in the mix: {f.role}"):
            if b and b not in bullets and b.lower() not in body.lower():
                bullets.append(b)
        out.append(StatementSlide(
            title=clip_text(f"Why {short} for {brand}", 90),
            strip=clip_text(strip_fact_ids(f.role or f.what), 160),
            body=clip_text(body, 400),
            bullets=bullets[:4],
            sources=_sources([f.fit_reason], dossier),
        ))
        steps = [s for s in f.steps if s.name or s.detail][:4]
        if len(steps) < 2:
            steps = [FlowStep(name="Brief", detail=f.what), FlowStep(name="Run", detail=f.volumes),
                     FlowStep(name="Measure", detail=f.kpi)]
        out.append(TimelineSlide(
            title=clip_text(f"How {short} works for {brand}", 90),
            strip=clip_text(" · ".join(x for x in (f.volumes, f"KPI: {f.kpi}" if f.kpi else "") if x), 160),
            phases=[
                Phase(name=clip_text(s.name or f"Step {k}", 40), when=f"Step {k}",
                      points=[p for p in (strip_fact_ids(s.detail),) if p])
                for k, s in enumerate(steps, start=1)
            ],
            notes=("Deliverables: " + "; ".join(f.deliverables[:4])) if f.deliverables else "",
        ))
    return out


def enforce_idea_services(slides: list[Any], bucket_of: dict[int, int | None], plan: ServicePlan,
                          strategy: Strategy, catalog: list[Service]) -> None:
    """Every idea powered by >= 1 chosen service (exact names); every chosen service powers >= 1 idea."""
    names = plan.names
    ideas = [(i, s) for i, s in enumerate(slides) if isinstance(s, IdeaSlide)]
    for i, s in ideas:
        exact = []
        for n in s.services:
            svc = match_service(n, catalog)
            if svc and svc.name in names and svc.name not in exact:
                exact.append(svc.name)
        bi = bucket_of.get(i)
        bs = plan.for_bucket(strategy.buckets[bi].name) if bi is not None and bi < len(strategy.buckets) else None
        if not exact and bs:
            exact = list(bs.services)
        if not exact and names:
            exact = [names[0]]
        s.services = exact[:4]
        if not s.service_mechanics and bs and bs.mechanics:
            s.service_mechanics = clip_text(bs.mechanics, 300)
    for n in names:
        if ideas and not any(n in s.services for _, s in ideas):
            _, target = min(ideas, key=lambda x: len(x[1].services))
            target.services = [*target.services, n][:4]
            logger.info("Service %r powered no idea; added to %r", n, target.title)
