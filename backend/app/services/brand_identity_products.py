"""Concrete product / feature / show names for a brand (Agent B).

The deck should name real things ("Zomato Gold", "Blinkit", "District";
"Thums Up Charged"; a platform's original shows), not "food, restaurant
discovery". Sources, all best-effort:
- schema.org JSON-LD on the brand's site (Product, Brand, SoftwareApplication,
  TVSeries, Organization sub-brands / offers ...);
- the site's navigation and headings;
- a Tavily search for the brand's products / sub-brands / app features;
- the research text.
An LLM ("writer" role) distils these into up to 10 concrete names; without one
(or on failure) a heuristic keeps Title-Case names that carry the brand name or
recur across sources.
"""

import asyncio
import html as htmllib
import json
import logging
import re
from collections import Counter
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field
from tavily import AsyncTavilyClient

from app.core.config import settings

logger = logging.getLogger(__name__)

MAX_PRODUCTS = 10
_PRODUCT_TYPES = {
    "product", "productgroup", "productmodel", "softwareapplication", "mobileapplication", "webapplication",
    "service", "brand", "tvseries", "movie", "creativeworkseries", "podcastseries", "menuitem", "offer",
    "foodestablishment", "event",
}
_GENERIC = set("""
home about contact login sign up cart menu search help support blog careers press investors terms privacy policy
shop store stores offers offer deals sale new all view more explore learn download app apps get started subscribe
account profile wishlist orders order track gift cards card india the and for with your our you best top more
products product services service features feature categories category collections collection men women kids
popular trending latest featured official website online free premium plans plan pricing price community news
""".split())


class ProductList(BaseModel):
    products: list[str] = Field(default_factory=list, description="Concrete product/feature/show names")
    kind: str = Field("", description='"products", "features", "shows" or "services"')


def jsonld_names(page: str) -> list[str]:
    """Names of products, brands, apps, shows and offers in the page's JSON-LD blocks."""
    names: list[str] = []

    def walk(node: Any, parent_type: str = "") -> None:
        if isinstance(node, list):
            for item in node:
                walk(item, parent_type)
            return
        if not isinstance(node, dict):
            return
        types = node.get("@type") or ""
        types = [t.lower() for t in (types if isinstance(types, list) else [types]) if isinstance(t, str)]
        name = node.get("name")
        if isinstance(name, str) and (set(types) & _PRODUCT_TYPES or parent_type in ("itemlist", "brand")):
            names.append(name)
        for key in ("brand", "subOrganization", "department", "itemOffered", "hasOfferCatalog",
                    "itemListElement", "item", "makesOffer", "owns", "@graph"):
            if key in node:
                walk(node[key], types[0] if types else "")

    for block in re.findall(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', page,
                            re.IGNORECASE | re.DOTALL):
        try:
            walk(json.loads(htmllib.unescape(block.strip())))
        except ValueError:
            continue
    return _clean(names)


def _clean(items: list[str]) -> list[str]:
    out: list[str] = []
    for raw in items:
        name = " ".join(htmllib.unescape(str(raw)).split()).strip(" -|·•,.")
        if not 2 < len(name) <= 40 or name.lower() in _GENERIC:
            continue
        if not re.search(r"[A-Za-z]", name) or re.fullmatch(r"[\d\W]+", name):
            continue
        if all(w.lower() in _GENERIC for w in name.split()):
            continue
        if name.lower() not in (o.lower() for o in out):
            out.append(name)
    return out


async def _tavily_text(brand: str) -> str:
    if not settings.tavily_api_key:
        return ""
    tav = AsyncTavilyClient(api_key=settings.tavily_api_key)
    queries = [f"{brand} products sub-brands and app features list",
               f"{brand} new launches and flagship products 2026",
               f"{brand} original shows or signature product variants"]
    res = await asyncio.gather(*(asyncio.wait_for(tav.search(q, max_results=5, include_answer=True), timeout=12)
                                 for q in queries), return_exceptions=True)
    parts: list[str] = []
    for r in res:
        if isinstance(r, BaseException):
            continue
        if r.get("answer"):
            parts.append(r["answer"])
        for item in r.get("results") or []:
            parts.append(f"{item.get('title', '')}. {(item.get('content') or '')[:600]}")
    return "\n".join(parts)[:9000]


_TITLE_PHRASE = re.compile(r"\b([A-Z][\w'&+-]*(?:\s+(?:[A-Z][\w'&+-]*|\d+|of|&|X))*)")


def heuristic_products(brand: str, sources: list[str], jsonld: list[str], nav: list[str]) -> list[str]:
    brand_l = brand.lower()
    counts: Counter = Counter()
    for text in sources:
        for m in _TITLE_PHRASE.finditer(text or ""):
            phrase = m.group(1).strip()
            if 1 <= len(phrase.split()) <= 4:
                counts[phrase] += 1
    scored: Counter = Counter()
    for name in jsonld:
        scored[name] += 5
    for name in nav:
        if re.match(r"^[A-Z]", name) and len(name.split()) <= 3:
            scored[name] += 1.5
    for phrase, n in counts.items():
        low = phrase.lower()
        if low == brand_l or low in _GENERIC:
            continue
        if low.startswith(brand_l + " ") or n >= 3:
            scored[phrase] += n + (3 if low.startswith(brand_l + " ") else 0)
    return _clean([p for p, _ in scored.most_common(30)])[:MAX_PRODUCTS]


async def find_products(brand: str, *, jsonld: list[str], nav: list[str], headings: list[str], title: str,
                        description: str, research_text: str = "") -> list[str]:
    """Up to 10 concrete product / feature / show names for ``brand``. Never raises."""
    try:
        web = await _tavily_text(brand)
    except Exception:  # noqa: BLE001 - optional source
        web = ""
    sources = [web, research_text[:4000], " ".join(headings), description]
    try:
        from app.services import llm_router

        prompt = (
            f"Brand: {brand}\nWebsite title: {title}\nWebsite description: {description}\n"
            f"Website menu: {', '.join(nav[:40])}\nWebsite headings: {' | '.join(headings[:20])}\n"
            f"Structured data names: {', '.join(jsonld[:30])}\n\nWeb search notes:\n{web[:4000]}\n\n"
            f"Research notes:\n{research_text[:2500]}"
        )
        msgs = [
            SystemMessage(content=(
                "You list the named things a brand is known for today, for an Indian agency pitch deck that "
                "must reference them. Return proper names only:\n"
                "- FMCG / food & drink: product lines and flavour or format variants (e.g. 'Thums Up Charged', "
                "'Thums Up XForce'), never pack sizes or bottle volumes;\n"
                "- apps / platforms: sub-brands, spin-off apps, subscription programmes and signature features "
                "(e.g. for Zomato: 'Zomato Gold', 'Blinkit', 'District', 'Zomato Live', 'Intercity Legends'), "
                "never generic functions like search, payments, tracking, or device apps;\n"
                "- OTT / media: original shows, films and franchises currently popular in India, never plans;\n"
                "- beauty / fashion / retail: house brands and flagship ranges.\n"
                "Prefer names in the notes; you may add names you are certain the brand offers in India now. "
                "No competitors, no parent company, no plan tiers or prices. Max 10, most important first. "
                'JSON: {"products": [...], "kind": "products|features|shows|services"}'
            )),
            HumanMessage(content=prompt),
        ]
        res = await asyncio.wait_for(
            llm_router.invoke_json("writer", msgs, ProductList, max_tokens=500, temperature=0.2), timeout=25)
        names = [n for n in _clean([p for p in res.products if isinstance(p, str)]) if n.lower() != brand.lower()]
        # Names found in the evidence come first; the model's own knowledge may add at most three.
        raw_evidence = " ".join([web, research_text, " ".join(nav), " ".join(headings), " ".join(jsonld), title,
                                 description])
        evidence = raw_evidence.lower()

        def supported(name: str) -> bool:
            low = name.lower()
            if low in evidence:
                return True
            tail = name[len(brand):].strip() if low.startswith(brand.lower()) else ""
            if len(tail) < 3:
                return False
            # "Charged" counts if written as a proper name mid-sentence, not just the word "energy".
            return bool(re.search(r"(?<![.!?:]\s)(?<!^)\b" + re.escape(tail) + r"\b", raw_evidence))

        backed = [n for n in names if supported(n)]
        extra = [n for n in names if not supported(n)][:3]
        names = backed + extra
        if len(names) >= 2:
            logger.info("identity: %s products: %d backed by sources, %d from model knowledge", brand,
                        len(backed), len(extra))
            return names[:MAX_PRODUCTS]
    except Exception as exc:  # noqa: BLE001 - fall back to the heuristic
        logger.info("identity: product extraction via LLM unavailable (%s); using heuristic", exc.__class__.__name__)
    return heuristic_products(brand, sources, jsonld, nav)
