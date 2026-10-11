"""Deep research -> Brand Dossier (WORKPLAN C9). Owner: Agent D.

Pipeline: URL discovery (Firecrawl /v2/map with a key, else sitemap.xml + homepage links;
plus campaign/news/buzz/competitor search results) -> scrape to clean Markdown (Firecrawl
/v2/scrape keyless or keyed, fallback httpx + trafilatura; search results that already carry
the full article are used as is) -> chunk + embed locally (MiniLM, in-memory Chroma) -> top
chunks per research question -> one "distiller" LLM call per question (parallel across
models) -> verified, merged, deduped ``BrandDossier`` with sourced facts.

Never raises: on any failure it returns a thin dossier built from the Tavily data.
Modules: ``research_deep_fetch`` (discovery, scraping, cache), ``research_deep_distill``
(chunks, selection, distillation, verification, reduce).
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import re
import time
from collections.abc import Awaitable, Callable
from datetime import date
from typing import Any

import httpx

from app.core.config import settings
from app.schemas.brand import ResearchSection, Source
from app.schemas.research import (
    BrandDossier,
    DossierCampaign,
    DossierCompetitor,
    DossierFact,
    DossierProduct,
    DossierSource,
)
from app.services import llm_router
from app.services import research_deep_distill as distill
from app.services import research_deep_fetch as fetch

logger = logging.getLogger(__name__)

StageCallback = Callable[[str, float], Awaitable[None] | None]

SCRAPE_CAP_S = 40.0
SCRAPE_CONCURRENCY = 6
# Whole step, distillation included (the router may wait for Groq's per-minute budgets).
OVERALL_CAP_S = {"focused": 90.0, "standard": 130.0, "flagship": 180.0}
SITE_SHARE = 0.6

# Stats of the last build per brand (pages, via, tokens, timings): logs and verification.
last_run: dict[str, dict[str, Any]] = {}


async def _emit(on_stage: StageCallback | None, fraction: float) -> None:
    if on_stage is None:
        return
    try:
        res = on_stage("deep_research", round(max(0.0, min(1.0, fraction)), 3))
        if inspect.isawaitable(res):
            await res
    except Exception as exc:  # noqa: BLE001 - progress reporting must never break research
        logger.debug("on_stage failed: %s", exc)


def _search_queries(brand: str, brief: str | None, site: str | None) -> list[tuple[str, str, bool, str | None]]:
    year = date.today().year
    qs: list[tuple[str, str, bool, str | None]] = [
        (f"{brand} campaign {year}", "campaign", False, None),
        (f"{brand} marketing strategy India", "other", False, None),
        (f"{brand} viral Instagram reel", "social", False, None),
        (f"{brand} latest news", "news", True, None),
        (f"{brand} competitors India", "other", False, None),
    ]
    if brief and brief.strip():
        words = " ".join(brief.split()[:14])
        qs.append((f"{brand} {words}", "campaign", False, None))
    if site:
        # The brand's own pages a search engine ranks for it (about, launches, blog posts).
        qs.append((f"{brand} about products launch campaign", "site", False, fetch.registrable(fetch.host_of(site))))
    return qs


def _section_kind(title: str) -> str:
    t = title.lower()
    if "news" in t or "moment" in t:
        return "news"
    if "campaign" in t or "marketing" in t:
        return "campaign"
    return "other"


def _season_label(season: str) -> str:
    """Identity season keys ("world_cup", "ipl") as readable labels."""
    words = season.replace("_", " ").split()
    return " ".join(w.upper() if len(w) <= 3 else w.capitalize() for w in words)


_SITE_KIND = {"blog": "blog", "news": "news", "campaign": "campaign"}


async def _resolve_site(brand: str, identity: Any, client: Any) -> str | None:
    site = getattr(identity, "site_url", None) if identity is not None else None
    if site:
        return site
    try:
        from app.services.brand_identity import find_official_site

        return await asyncio.wait_for(find_official_site(brand, client), 14)
    except Exception as exc:  # noqa: BLE001
        logger.info("deep research: no site for %s (%s)", brand, exc)
        return None


async def _gather_pages(brand: str, brief: str | None, research: list[ResearchSection], site: str | None,
                        n_total: int, client: Any, stats: fetch.FetchStats, on_stage: StageCallback | None,
                        run: dict[str, Any]) -> list[fetch.Page]:
    n_site = max(2, round(n_total * SITE_SHARE)) if site else 0
    t0 = time.monotonic()

    async def discover() -> list[str]:
        if not site:
            return []
        try:
            urls, via = await asyncio.wait_for(fetch.discover_site_urls(client, brand, site, n_site + 4, stats), 22)
            run["discovery"] = via
            return urls
        except Exception as exc:  # noqa: BLE001
            logger.info("deep research discovery failed for %s: %s", brand, exc)
            run["discovery"] = f"failed: {type(exc).__name__}"
            return [fetch.normalize_url(site)]

    site_urls, hits = await asyncio.gather(
        discover(), fetch.search_external(client, brand, _search_queries(brand, brief, site), stats)
    )
    run["timings"]["discover_s"] = round(time.monotonic() - t0, 1)
    await _emit(on_stage, 0.2)

    site_reg = fetch.registrable(fetch.host_of(site)) if site else ""
    seen: set[str] = set()
    # External hits: best per query round-robin, max 2 per host, the brand's own domain counted as site.
    by_query: dict[str, list[fetch.SearchHit]] = {}
    own_hits: list[fetch.SearchHit] = []
    for h in sorted(hits, key=lambda h: -h.score):
        if site_reg and fetch.registrable(fetch.host_of(h.url)) == site_reg:
            own_hits.append(h)
        else:
            by_query.setdefault(h.query, []).append(h)
    external: list[fetch.SearchHit] = []
    hosts: dict[str, int] = {}
    while any(by_query.values()):
        for q in list(by_query):
            while by_query[q]:
                h = by_query[q].pop(0)
                if h.url in seen or hosts.get(fetch.host_of(h.url), 0) >= 2:
                    continue
                seen.add(h.url)
                hosts[fetch.host_of(h.url)] = hosts.get(fetch.host_of(h.url), 0) + 1
                external.append(h)
                break
    # Search hits on the brand's own domain join the site pages (ahead of sitemap picks they rank above).
    token = re.sub(r"[^a-z0-9]", "", brand.lower())
    site_path = fetch.urlparse(site).path.rstrip("/").lower() if site else ""
    own: list[str] = []
    for h in own_hits:
        path = fetch.urlparse(h.url).path.lower()
        if h.url in site_urls or h.url in own or fetch.score_url(h.url, site or h.url, token)[0] < 0:
            continue
        if site_path and not path.startswith(site_path) and token not in path.replace("-", ""):
            continue
        own.append(h.url)
    site_urls = site_urls[:1] + own[:3] + site_urls[1:]
    # Tavily research sources (already shown to the user) as last-resort article candidates.
    for sec in research:
        for s in sec.sources:
            u = fetch.normalize_url(s.url)
            if u and u not in seen and not fetch.unscrapable(u) and (not site_reg or fetch.registrable(fetch.host_of(u)) != site_reg):
                seen.add(u)
                external.append(fetch.SearchHit(u, s.title, s.snippet, "", "tavily", sec.title, _section_kind(sec.title), 0.0))

    pages: list[fetch.Page] = []
    done = 0
    want = len(site_urls) + len(external)
    sem = asyncio.Semaphore(SCRAPE_CONCURRENCY)

    own_md = {h.url: h for h in own_hits if h.markdown}

    async def site_page(url: str) -> fetch.Page | None:
        _, cat = fetch.score_url(url, site or url, token)
        kind = _SITE_KIND.get(cat, "site")
        async with sem:
            try:
                p = await asyncio.wait_for(
                    fetch.scrape_page(client, brand, url, kind, stats, fetch.MIN_WORDS_SITE), fetch.PAGE_TIMEOUT * 2)
            except asyncio.TimeoutError:
                p = None
        if p is None and url in own_md:  # site blocks us; the search engine's copy of the page
            h = own_md[url]
            md = fetch.clean_markdown(h.markdown)
            if fetch.real_words(md) >= fetch.MIN_WORDS_SITE:
                p = fetch.Page(url, h.title or url, md, kind, h.via, h.query, h.published)
        return p

    def relevant(title: str, md: str) -> bool:
        # Off-topic hits ("night owls" for Sleepy Owl): the article must be about the brand.
        return fetch.mentions(brand, md) + 2 * fetch.mentions(brand, title) >= 3

    async def ext_page(h: fetch.SearchHit) -> fetch.Page | None:
        if h.markdown:
            md = fetch.clean_markdown(h.markdown)
            if fetch.real_words(md) >= fetch.MIN_WORDS_EXTERNAL:
                if not relevant(h.title, md):
                    return None
                return fetch.Page(h.url, h.title or h.url, md, h.kind, h.via, h.query, h.published)
        async with sem:
            p = await asyncio.wait_for(
                fetch.scrape_page(client, brand, h.url, h.kind, stats, fetch.MIN_WORDS_EXTERNAL), fetch.PAGE_TIMEOUT * 2)
        if p and not relevant(p.title, p.markdown):
            return None
        if p:
            p.query = h.query
            p.published = p.published or h.published
        return p

    # Ask for more than we keep: many pages are thin, blocked or JS-only.
    n_ext_target = n_total - min(n_site, len(site_urls))
    site_tasks = [asyncio.create_task(site_page(u)) for u in site_urls[: n_site + 4]]
    ext_tasks = [asyncio.create_task(ext_page(h)) for h in external[: max(n_ext_target + 6, 8)]]
    tasks = site_tasks + ext_tasks
    order = {t: i for i, t in enumerate(tasks)}
    results: dict[int, fetch.Page] = {}
    deadline = time.monotonic() + SCRAPE_CAP_S
    pending = set(tasks)
    while pending and time.monotonic() < deadline:
        finished, pending = await asyncio.wait(pending, timeout=deadline - time.monotonic(),
                                               return_when=asyncio.FIRST_COMPLETED)
        for t in finished:
            done += 1
            try:
                p = t.result()
            except Exception:  # noqa: BLE001 - timeouts/errors per page
                p = None
            if p:
                results[order[t]] = p
        await _emit(on_stage, 0.2 + 0.4 * done / max(1, want))
        site_ok = sum(1 for i in results if i < len(site_tasks))
        ext_ok = len(results) - site_ok
        if site_ok >= n_site and ext_ok >= n_total - n_site:
            break
    for t in pending:
        t.cancel()
    run["timings"]["scrape_s"] = round(time.monotonic() - t0 - run["timings"]["discover_s"], 1)
    site_pages = [results[i] for i in sorted(results) if i < len(site_tasks)]
    ext_pages = [results[i] for i in sorted(results) if i >= len(site_tasks)]
    keep_site = site_pages[:n_site]
    keep_ext = ext_pages[: n_total - len(keep_site)]
    if len(keep_site) + len(keep_ext) < n_total:  # fill from whichever side has spares
        keep_site = site_pages[: n_total - len(keep_ext)]
    pages, seen_urls = [], set()
    for p in keep_site + keep_ext:
        if p.url not in seen_urls:
            seen_urls.add(p.url)
            pages.append(p)
    run["failed_urls"] = stats.failures[:20]
    return pages


def _thin_dossier(brand: str, research: list[ResearchSection], identity: Any, site: str | None,
                  pages: list[fetch.Page] | None = None) -> BrandDossier:
    """No LLM: Tavily summaries + verbatim number sentences from snippets/pages."""
    pages = pages or []
    overview = next((s for s in research if "overview" in s.title.lower()), research[0] if research else None)
    summary = overview.summary if overview and overview.summary and "No summary" not in overview.summary else ""
    facts: list[DossierFact] = []
    seen: list[str] = []
    brand_l = brand.lower()

    def add_sentences(text: str, url: str, title: str, need_brand: bool) -> None:
        for sent in re.split(r"(?<=[.!?])\s+|\n+", text):
            s = " ".join(sent.split()).strip(" -*#|")
            if not (40 <= len(s) <= 300) or not distill.figures(s) or s.endswith("...") or "|" in s:
                continue
            if need_brand and brand_l not in s.lower():
                continue
            if all(re.fullmatch(r"(19|20)\d\d", n) and not u for n, u, _ in distill.figures(s)):
                continue  # only years
            if any(distill.similar(s, o, 0.7) for o in seen):
                continue
            seen.append(s)
            facts.append(DossierFact(id=f"F{len(facts) + 1}", text=s, number=distill.headline_figure(s),
                                     source_url=url, source_title=title))
            if len(facts) >= 30:
                return

    for p in pages:
        add_sentences(p.markdown, p.url, p.title, need_brand=True)
    for sec in research:
        for src in sec.sources:
            add_sentences(src.snippet, src.url, src.title, need_brand=False)
    sources = [DossierSource(url=p.url, title=p.title, kind=p.kind, via=p.via) for p in pages]
    sources += [DossierSource(url=s.url, title=s.title, kind=_section_kind(sec.title), via="tavily")
                for sec in research for s in sec.sources if s.url not in {x.url for x in sources}]
    products = [DossierProduct(name=p) for p in (getattr(identity, "products", None) or [])]
    season = getattr(identity, "season", None)
    return BrandDossier(
        brand=brand, site=site, summary=summary, products=products, facts=facts[:60], sources=sources[:40],
        moments=[_season_label(season)] if season else [], pages_read=len(pages), depth="thin",
    )


# Indian annual-report PDFs map the rupee glyph to "`" or "H": "H7,950 crores", "`4954 cr".
_RUPEE_GLYPH_RE = re.compile(r"(?<![A-Za-z])[`H](?=\s?\d[\d,.]*\s?(?:crores?|cr\b|lakhs?|mn\b|bn\b|million|billion))")
_UNKNOWN_RE = re.compile(r"^\s*(not (specified|mentioned|stated|given)|unknown|n/?a|none|null|-)\s*\.?$", re.IGNORECASE)


def _rupee(text: str | None) -> str | None:
    return _RUPEE_GLYPH_RE.sub("₹", text) if text else text


def _known(text: str) -> str:
    return "" if _UNKNOWN_RE.match(text or "") else text


def _reduce(brand: str, site: str | None, qs: list[distill.Question], items: list[distill.Item],
            scalars: dict[str, str], pages: list[fetch.Page], research: list[ResearchSection],
            identity: Any) -> BrandDossier:
    facts_items = distill.dedupe_facts(items, [q.key for q in qs], brand)
    facts = [
        DossierFact(id=f"F{i + 1}", text=_rupee(it.data["text"]), number=_rupee(it.data.get("number")),
                    source_url=it.url, source_title=it.title)
        for i, it in enumerate(facts_items[:60])
    ]
    products = [DossierProduct(name=it.data["name"], detail=_known(_rupee(it.data.get("detail", ""))))
                for it in distill.merge_named(items, "product", "name")]
    have = {re.sub(r"[^a-z0-9]", "", p.name.lower()) for p in products}
    for name in getattr(identity, "products", None) or []:  # names read off the brand site by identity
        if re.sub(r"[^a-z0-9]", "", name.lower()) not in have:
            products.append(DossierProduct(name=name))
    campaigns = [DossierCampaign(name=it.data["name"], when=_known(it.data.get("when", "")),
                                 what=_known(_rupee(it.data.get("what", ""))),
                                 source_url=it.url) for it in distill.merge_named(items, "campaign", "name")]
    competitors = [DossierCompetitor(name=it.data["name"], angle=_known(it.data.get("angle", "")))
                   for it in distill.merge_named(items, "competitor", "name")
                   if brand.lower() not in it.data["name"].lower()
                   and "parent" not in (it.data["name"] + it.data.get("angle", "")).lower()]
    notes = {k: [it.data["text"] for it in distill.merge_notes(items, k)]
             for k in ("audience", "cultural_hooks", "social_signals", "moments")}
    if len(notes["audience"]) < 3:  # the audience question often answers as facts: reuse them as segments
        for it in facts_items:
            if it.q == "audience" and len(notes["audience"]) < 5 and not it.data.get("number") \
                    and not any(distill.similar(it.data["text"], a, 0.6) for a in notes["audience"]):
                notes["audience"].append(it.data["text"][:200])
    season = getattr(identity, "season", None)
    if season and not any(season.lower() in m.lower() for m in notes["moments"]):
        notes["moments"].append(_season_label(season))
    summary = _rupee(scalars.get("summary") or "") or ""
    if not summary:
        overview = next((s for s in research if "overview" in s.title.lower()), None)
        summary = overview.summary if overview else ""
    sources = [DossierSource(url=p.url, title=p.title, kind=p.kind, via=p.via) for p in pages]
    known = {s.url for s in sources}
    sources += [DossierSource(url=s.url, title=s.title, kind=_section_kind(sec.title), via="tavily")
                for sec in research for s in sec.sources if s.url not in known]
    deep = len(pages) >= 4 and len(facts) >= 5
    return BrandDossier(
        brand=brand, site=site, summary=summary, audience=notes["audience"],
        tone_of_voice=scalars.get("tone_of_voice", ""), products=products, recent_campaigns=campaigns,
        competitors=competitors, cultural_hooks=notes["cultural_hooks"], social_signals=notes["social_signals"],
        moments=notes["moments"], facts=facts, sources=sources[:40], pages_read=len(pages),
        depth="deep" if deep else "thin",
    )


async def _build(brand: str, brief: str | None, research: list[ResearchSection], identity: Any, scale: str,
                 on_stage: StageCallback | None, run: dict[str, Any], state: dict[str, Any]) -> BrandDossier:
    n_total = settings.research_pages.get(scale) or settings.research_pages.get("standard", 10)
    stats = fetch.FetchStats()
    async with httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(fetch.PAGE_TIMEOUT, connect=6.0),
                                       headers={"Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                                                "Accept-Language": "en-IN,en;q=0.9"}) as client:
        site = await _resolve_site(brand, identity, client)
        state["site"] = site
        await _emit(on_stage, 0.05)
        pages = await _gather_pages(brand, brief, research, site, n_total, client, stats, on_stage, run)
    state["pages"] = pages
    run["firecrawl"] = dict(stats.firecrawl) | {f"blocked:{k}": v for k, v in fetch.firecrawl.reason.items()
                                               if fetch.firecrawl.blocked_until.get(k, 0) > time.time()}
    run["pages"] = [{"url": p.url, "kind": p.kind, "via": p.via, "words": p.words} for p in pages]
    if not pages:
        run["fallback"] = "no pages"
        return _thin_dossier(brand, research, identity, site)

    t1 = time.monotonic()
    chunks = distill.chunk_pages(pages)
    season = getattr(identity, "season", None)
    qs = distill.questions(brand, brief, season, scale)
    ranked = await asyncio.to_thread(distill.rank_chunks, chunks, qs)
    target = distill.PROMPT_TARGET.get(scale, 2400)
    cap = llm_router.prompt_capacity("distiller", distill.DISTILL_MAX_TOKENS)
    if cap <= 0:
        run["fallback"] = "no distiller model"
        return _thin_dossier(brand, research, identity, site, pages)
    budget = min(target, cap) - max(distill.prompt_overhead(brand, q) for q in qs)
    excerpts = distill.assign_excerpts(chunks, qs, ranked, budget)
    run["chunks"] = len(chunks)
    run["timings"]["select_s"] = round(time.monotonic() - t1, 1)
    await _emit(on_stage, 0.65)

    t2 = time.monotonic()
    active = [q for q in qs if excerpts[q.key]]
    tasks = {asyncio.create_task(distill.distil_question(brand, q, excerpts[q.key], prefer=i)): q
             for i, q in enumerate(active)}
    remaining = max(15.0, state["deadline"] - time.monotonic())
    pending = set(tasks)
    done_n = 0
    outputs: dict[str, distill.Distilled] = {}
    end = time.monotonic() + remaining
    while pending and time.monotonic() < end:
        finished, pending = await asyncio.wait(pending, timeout=end - time.monotonic(), return_when=asyncio.FIRST_COMPLETED)
        for t in finished:
            done_n += 1
            try:
                res = t.result()
            except Exception:  # noqa: BLE001
                res = None
            if res is not None:
                outputs[tasks[t].key] = res
        await _emit(on_stage, 0.65 + 0.3 * done_n / max(1, len(tasks)))
    for t in pending:
        t.cancel()
    run["timings"]["distil_s"] = round(time.monotonic() - t2, 1)
    run["questions"] = {q.key: {"excerpts": len(excerpts[q.key]), "ok": q.key in outputs} for q in qs}

    items: list[distill.Item] = []
    scalars: dict[str, str] = {}
    dropped = 0
    for q in qs:
        if q.key not in outputs:
            continue
        its, sc, dr = distill.verify(brand, q, outputs[q.key], excerpts[q.key])
        items += its
        dropped += dr
        for k, v in sc.items():
            scalars.setdefault(k, v)
    run["dropped_items"] = dropped
    if not items:
        run["fallback"] = "distil produced nothing"
        return _thin_dossier(brand, research, identity, site, pages)
    dossier = _reduce(brand, site, qs, items, scalars, pages, research, identity)
    if len(dossier.facts) < 12:  # top up with verbatim figures from the pages / Tavily snippets
        extra = _thin_dossier(brand, research, identity, site, pages).facts
        for f in extra:
            if len(dossier.facts) >= 60:
                break
            if not any(distill.similar(f.text, g.text, 0.6) for g in dossier.facts):
                dossier.facts.append(f.model_copy(update={"id": f"F{len(dossier.facts) + 1}"}))
    return dossier


async def build_dossier(
    brand: str,
    brief: str | None,
    research: list[ResearchSection],
    identity: Any = None,
    scale: str = "standard",
    on_stage: StageCallback | None = None,
) -> BrandDossier:
    """Deep-research ``brand`` and return its dossier (pages per scale: ``settings.research_pages``).

    ``identity`` is the ``brand_identity.BrandIdentity`` (site URL, products) when available.
    ``on_stage(name, fraction)`` is called with progress inside this step (0..1).
    """
    t0 = time.monotonic()
    research = research or []
    run: dict[str, Any] = {"scale": scale, "timings": {}}
    state: dict[str, Any] = {"deadline": t0 + OVERALL_CAP_S.get(scale, 130.0), "site": None, "pages": []}
    outer = llm_router._records.get()  # noqa: SLF001 - don't hide our calls from an outer track_calls()
    start = len(outer) if outer is not None else 0
    records: list[llm_router.CallRecord] = []
    dossier: BrandDossier | None = None
    try:
        if outer is None:
            with llm_router.track_calls() as records:
                dossier = await asyncio.wait_for(
                    _build(brand, brief, research, identity, scale, on_stage, run, state),
                    OVERALL_CAP_S.get(scale, 130.0) + 20)
        else:
            dossier = await asyncio.wait_for(
                _build(brand, brief, research, identity, scale, on_stage, run, state),
                OVERALL_CAP_S.get(scale, 130.0) + 20)
            records = outer[start:]
    except Exception as exc:  # noqa: BLE001 - contract: never raises
        logger.warning("deep research for %s failed, using thin dossier: %s", brand, exc, exc_info=True)
        run["fallback"] = f"error: {type(exc).__name__}: {str(exc)[:160]}"
    if dossier is None:
        try:
            dossier = _thin_dossier(brand, research, identity, state.get("site"), state.get("pages"))
        except Exception:  # noqa: BLE001
            dossier = BrandDossier(brand=brand)
    tokens: dict[str, dict[str, int]] = {}
    for r in records:
        t = tokens.setdefault(r.model, {"calls": 0, "errors": 0, "in": 0, "out": 0})
        t["errors" if r.error else "calls"] += 1
        t["in"] += r.input_tokens or 0
        t["out"] += r.output_tokens or 0
    run["tokens"] = tokens
    run["timings"]["total_s"] = round(time.monotonic() - t0, 1)
    run["facts"] = len(dossier.facts)
    run["depth"] = dossier.depth
    last_run[brand.lower()] = run
    logger.info("deep research %s: %s, %d pages, %d facts, %.1fs, tokens %s, fallback=%s", brand, dossier.depth,
                dossier.pages_read, len(dossier.facts), run["timings"]["total_s"], tokens, run.get("fallback"))
    await _emit(on_stage, 1.0)
    return dossier


# --------------------------------------------------------------------------- renderings


def _with_number(f: DossierFact) -> str:
    if f.number and f.number.replace(" ", "").lower() not in f.text.replace(" ", "").lower():
        return f"{f.text} [{f.number}]"
    return f.text


def dossier_text(dossier: BrandDossier, max_chars: int = 9000) -> str:
    """Plain-text rendering of the dossier (used by plan checks as the 'research' corpus).

    Facts (with their figures) come first so a clipped text still holds every number.
    """
    parts: list[str] = []
    if dossier.summary:
        parts.append(dossier.summary)
    parts += [f"{f.id}: {_with_number(f)}" for f in dossier.facts]
    if dossier.products:
        parts.append("Products: " + "; ".join(f"{p.name}" + (f" - {p.detail}" if p.detail else "") for p in dossier.products))
    for c in dossier.recent_campaigns:
        parts.append(f"Campaign: {c.name}" + (f" ({c.when})" if c.when else "") + (f": {c.what}" if c.what else ""))
    if dossier.audience:
        parts.append("Audience: " + "; ".join(dossier.audience))
    if dossier.tone_of_voice:
        parts.append("Tone of voice: " + dossier.tone_of_voice)
    if dossier.competitors:
        parts.append("Competitors: " + "; ".join(c.name + (f" - {c.angle}" if c.angle else "") for c in dossier.competitors))
    for label, vals in (("Cultural hooks", dossier.cultural_hooks), ("Social signals", dossier.social_signals),
                        ("Moments", dossier.moments)):
        if vals:
            parts.append(f"{label}: " + "; ".join(vals))
    return "\n".join(parts)[:max_chars]


def _short_host(url: str | None) -> str:
    return fetch.host_of(url or "") or ""


def dossier_brief(dossier: BrandDossier, max_tokens: int = 2500) -> str:
    """Compact prompt rendering for the strategist/writers, facts shown as ``[F3] ...`` to cite."""
    n_facts, n_list = len(dossier.facts), 8
    text = ""
    for _ in range(40):
        lines = [f"BRAND DOSSIER: {dossier.brand} ({dossier.depth}; {dossier.pages_read} pages read"
                 + (f"; site {_short_host(dossier.site)}" if dossier.site else "") + ")"]
        if dossier.summary:
            lines.append("Summary: " + dossier.summary)
        if dossier.products:
            lines.append("Products: " + "; ".join(p.name + (f" ({p.detail})" if p.detail else "")
                                                  for p in dossier.products[: n_list + 4]))
        if dossier.recent_campaigns:
            lines.append("Recent campaigns:")
            lines += [f"- {c.name}" + (f" ({c.when})" if c.when else "") + (f": {c.what}" if c.what else "")
                      for c in dossier.recent_campaigns[:n_list]]
        if dossier.audience:
            lines.append("Audience: " + " | ".join(dossier.audience[:n_list]))
        if dossier.tone_of_voice:
            lines.append("Tone of voice: " + dossier.tone_of_voice)
        if dossier.competitors:
            lines.append("Competitors: " + "; ".join(c.name + (f" ({c.angle})" if c.angle else "")
                                                     for c in dossier.competitors[:n_list]))
        for label, vals in (("Cultural hooks", dossier.cultural_hooks), ("Social buzz", dossier.social_signals),
                            ("Moments", dossier.moments)):
            if vals:
                lines.append(f"{label}: " + " | ".join(vals[:n_list]))
        if dossier.facts and n_facts:
            lines.append("Facts (cite as [F#]; figures exactly as written):")
            lines += [f"[{f.id}] {_with_number(f)} ({_short_host(f.source_url)})" for f in dossier.facts[:n_facts]]
        text = "\n".join(lines)
        if llm_router.estimate_tokens(text) <= max_tokens:
            return text
        if n_facts > 12:
            n_facts = max(12, n_facts - 4)
        elif n_list > 3:
            n_list -= 1
        elif n_facts > 4:
            n_facts -= 2
        else:
            break
    return text[: max_tokens * 3]


async def research_sources_sections(dossier: BrandDossier) -> list[ResearchSection]:
    """Research-panel sections listing the pages the dossier read and its key facts."""
    via: dict[str, int] = {}
    read = [s for s in dossier.sources if s.via != "tavily"]
    for s in read:
        via[s.via] = via.get(s.via, 0) + 1
    via_txt = ", ".join(f"{n} via {k}" for k, n in via.items()) or "none"
    sections = [ResearchSection(
        title="Deep research: pages read",
        summary=f"{dossier.pages_read} pages read in full ({via_txt}); {len(dossier.facts)} sourced facts; "
                f"depth: {dossier.depth}.",
        sources=[Source(title=s.title or s.url, url=s.url, snippet=f"{s.kind} page, via {s.via}") for s in read],
    )]
    if dossier.facts:
        sections.append(ResearchSection(
            title="Deep research: key facts",
            summary=dossier.summary or f"Facts about {dossier.brand} with their sources.",
            sources=[Source(title=f"[{f.id}] {f.source_title or _short_host(f.source_url)}", url=f.source_url,
                            snippet=f.text) for f in dossier.facts[:25]],
        ))
    return sections
