"""Meme templates: fresh, matched and not repeated (Agent B).

- ``get_meme_templates()``: Imgflip's popular list plus *new* templates from
  imgflip.com/memetemplates?sort=top-new (no key needed), cached for 6 h.
- Trend signals (cached 3 h): Imgflip's top-30-days and top-new ranks, this
  week's Reddit pulse (titles mentioning a template), and Tavily searches for
  trending meme formats this month. Each template gets a ``trend`` score.
- History: ``{media_dir}/meme_history.json`` remembers which templates went into
  which brand's deck; templates used in recent decks are penalised (more for the
  same brand), so repeated runs and different brands get different memes.
- ``meme_shortlist(brand, bucket_hints, n)``: a diverse, trend-weighted shortlist
  (fit to the buckets x trend x novelty, sampled with some randomness so runs
  rotate, mixed box counts) for the strategist instead of the full list.
- ``choose_replacement``: swaps a repeated or recently used template for the best
  unused one with the same box count (captions carry over).
"""

import asyncio
import html
import json
import logging
import math
import random
import re
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import httpx
from tavily import AsyncTavilyClient

from app.core.config import settings
from app.services import trends
from app.services.memes_layouts import LAYOUTS, USES
from app.services.visuals_http import BROWSER_UA

logger = logging.getLogger(__name__)

TEMPLATE_TTL = 6 * 3600.0
TREND_TTL = 3 * 3600.0
HISTORY_DECKS = 12  # how many recent decks count for the novelty penalty
HISTORY_MAX = 400


@dataclass(frozen=True)
class MemeTemplate:
    id: str
    name: str
    box_count: int
    url: str = ""  # blank template image (Imgflip CDN)
    rank: int = 999  # position in Imgflip's all-time popular list
    new: bool = False  # from Imgflip's "top new" list
    trend: float = 0.0  # current-trend score (higher = more talked about now)


@dataclass
class _State:
    templates: tuple[float, list[MemeTemplate]] | None = None
    trend: tuple[float, dict[str, float], dict[str, str]] | None = None


_state = _State()


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0), follow_redirects=True,
                             headers={"User-Agent": BROWSER_UA})


_TOPNEW_RE = re.compile(
    r'<a title="([^"]+?) Meme" href="/meme/(\d+)/[^"]*">.*?<img[^>]+src="(//i\.imgflip\.com/[^"]+)"', re.DOTALL)
_RANKED_RE = re.compile(r'<h3 class="mt-title">\s*<a title="([^"]+?) Meme" href="/meme/([^"]+)"')


def _full_image(src: str) -> str:
    # //i.imgflip.com/4/b0zriv.jpg (thumbnail) -> https://i.imgflip.com/b0zriv.jpg
    return "https:" + re.sub(r"(i\.imgflip\.com)/\d+/", r"\1/", src)


async def _imgflip_page(client: httpx.AsyncClient, sort: str) -> str:
    try:
        resp = await client.get(f"https://imgflip.com/memetemplates?sort={sort}")
        return resp.text if resp.status_code == 200 else ""
    except httpx.HTTPError:
        return ""


async def get_meme_templates() -> list[MemeTemplate]:
    """Imgflip's popular templates + this period's new ones (cached ~6 h). Empty list on failure."""
    if _state.templates and _state.templates[0] > time.monotonic():
        return _state.templates[1]
    out: list[MemeTemplate] = []
    async with _client() as client:
        try:
            resp = await client.get("https://api.imgflip.com/get_memes")
            body = resp.json()
            memes = (body.get("data") or {}).get("memes") or [] if body.get("success") else []
            out = [
                MemeTemplate(id=str(m["id"]), name=str(m["name"]), box_count=int(m.get("box_count") or 2),
                             url=str(m.get("url") or ""), rank=i)
                for i, m in enumerate(memes) if m.get("id") and m.get("name")
            ]
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            logger.warning("Imgflip get_memes failed: %s", exc)
        page = await _imgflip_page(client, "top-new")
    known = {t.id for t in out}
    for name, tid, src in _TOPNEW_RE.findall(page):
        if tid not in known:
            known.add(tid)
            out.append(MemeTemplate(id=tid, name=" ".join(html.unescape(name).split()), box_count=2,
                                    url=_full_image(src),
                                    rank=999, new=True))
    if out:
        _state.templates = (time.monotonic() + TEMPLATE_TTL, out)
    elif _state.templates:
        return _state.templates[1]
    return out


# --- trend signals ----------------------------------------------------------


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]", " ", text.lower())


_NAME_STOP = {"meme", "the", "a", "of", "and", "is", "it", "to", "you", "i", "me", "my", "on", "in", "at", "for",
              "with", "no", "yes", "man", "guy", "girl", "two", "one", "this"}


def _name_keys(name: str) -> list[str]:
    """Distinctive tokens/bigrams of a template name, for matching it in free text."""
    words = [w for w in _norm(name).split() if w not in _NAME_STOP and len(w) > 2]
    keys = [" ".join(words[i:i + 2]) for i in range(len(words) - 1)]
    if len(words) == 1 and len(words[0]) >= 5:
        keys.append(words[0])
    return keys or ([_norm(name).strip()] if len(_norm(name).strip()) >= 6 else [])


async def _trend_text() -> str:
    """Text about what's trending now: Reddit pulse titles + Tavily trending-meme articles."""
    parts: list[str] = []
    try:
        pulse = await asyncio.wait_for(trends.get_trend_pulse(), timeout=12)
        parts += [p.title for p in pulse.posts]
    except Exception:  # noqa: BLE001 - signals are optional
        pass
    if settings.tavily_api_key:
        month = date.today().strftime("%B %Y")
        tav = AsyncTavilyClient(api_key=settings.tavily_api_key)
        queries = ["trending meme template India this week", f"viral meme format {month}"]
        res = await asyncio.gather(*(asyncio.wait_for(tav.search(q, max_results=6), timeout=12) for q in queries),
                                   return_exceptions=True)
        for r in res:
            if isinstance(r, BaseException):
                continue
            for item in r.get("results") or []:
                parts.append(f"{item.get('title', '')} {item.get('content', '')}")
    return "\n".join(parts)


async def trend_scores(templates: list[MemeTemplate]) -> dict[str, float]:
    """template id -> trend score (recent popularity + mentions in this week's/month's chatter)."""
    if _state.trend and _state.trend[0] > time.monotonic():
        return _state.trend[1]
    async with _client() as client:
        page30, text = await asyncio.gather(_imgflip_page(client, "top-30-days"), _trend_text())
    scores: dict[str, float] = {}
    by_slug: dict[str, MemeTemplate] = {}
    for t in templates:
        by_slug[re.sub(r"[^a-z0-9]", "", t.name.lower())] = t
    # Imgflip's last-30-days ranking.
    for pos, (name, _slug) in enumerate(_RANKED_RE.findall(page30)):
        t = by_slug.get(re.sub(r"[^a-z0-9]", "", name.lower()))
        if t:
            scores[t.id] = scores.get(t.id, 0) + max(0.0, 2.0 - pos * 0.05)
    # New templates are trending by definition.
    for t in templates:
        if t.new:
            scores[t.id] = scores.get(t.id, 0) + 0.9
    # Mentions in Reddit/Tavily chatter.
    low = _norm(text)
    for t in templates:
        hits = sum(low.count(k) for k in _name_keys(t.name))
        if hits:
            scores[t.id] = scores.get(t.id, 0) + min(3.0, 1.2 * math.log1p(hits) + 0.6)
    _state.trend = (time.monotonic() + TREND_TTL, scores, {})
    logger.info("memes: trend signals for %d templates (%d mentioned)", len(templates), len(scores))
    return scores


# --- history ----------------------------------------------------------------


def _history_path() -> Path:
    return Path(settings.media_dir) / "meme_history.json"


def load_history() -> list[dict[str, Any]]:
    try:
        data = json.loads(_history_path().read_text())
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def record_meme_use(brand: str, template_ids: list[str]) -> None:
    """Remember the templates one deck used (newest last). Never raises."""
    ids = [str(t) for t in template_ids if t]
    if not ids:
        return
    try:
        hist = load_history()
        hist.append({"brand": (brand or "").strip().lower(), "ts": int(time.time()), "templates": ids})
        hist = hist[-HISTORY_MAX:]
        path = _history_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(hist))
        tmp.replace(path)
    except OSError as exc:
        logger.info("memes: could not write history: %s", exc)


def recent_penalties(brand: str, decks: int = HISTORY_DECKS) -> dict[str, float]:
    """template id -> penalty from recent decks (newer and same-brand uses weigh more)."""
    brand = (brand or "").strip().lower()
    pen: dict[str, float] = {}
    recent = load_history()[-decks:]
    for age, entry in enumerate(reversed(recent)):
        w = 2.2 * (0.82 ** age) * (1.6 if entry.get("brand") == brand else 1.0)
        for tid in entry.get("templates") or []:
            pen[str(tid)] = pen.get(str(tid), 0) + w
    return pen


def recently_used(brand: str, decks: int = 3) -> set[str]:
    """Templates in the last few decks (any brand): these get swapped out of a new deck."""
    out: set[str] = set()
    for entry in load_history()[-decks:]:
        out.update(str(t) for t in entry.get("templates") or [])
    return out


# --- shortlist --------------------------------------------------------------


def _fit(t: MemeTemplate, hints: list[str]) -> float:
    if not hints:
        return 0.0
    words = set(_norm(" ".join(hints)).split()) - _NAME_STOP
    desc = set(_norm(f"{t.name} {USES.get(t.id, '')}").split())
    return min(3.0, 0.9 * len(words & desc))


def score_templates(templates: list[MemeTemplate], trend: dict[str, float], penalties: dict[str, float],
                    hints: list[str]) -> dict[str, float]:
    out = {}
    for t in templates:
        base = 1.4 - min(t.rank, 120) / 100 if not t.new else 0.6  # all-time popularity (weak)
        captionable = 0.6 if t.id in LAYOUTS else 0.0  # we can caption it well without Imgflip
        out[t.id] = base + trend.get(t.id, 0.0) + _fit(t, hints) + captionable - penalties.get(t.id, 0.0)
    return out


def captionable(t: MemeTemplate) -> bool:
    """Can the local captioner draw this template well (no Imgflip account)?"""
    return t.id in LAYOUTS or (not t.new and t.box_count <= 2)


def template_image_urls(t: MemeTemplate) -> list[str]:
    """Candidate full-size image URLs (new Imgflip templates are often .png, not .jpg)."""
    urls = [t.url] if t.url else []
    if t.url.endswith(".jpg"):
        urls.append(t.url[:-4] + ".png")
    return urls


def _sample(scored: list[tuple[float, MemeTemplate]], n: int, rnd: random.Random,
            temperature: float = 0.9) -> list[MemeTemplate]:
    """Weighted sampling without replacement (softmax over scores), keeping a mix of box counts."""
    pool = list(scored)
    picked: list[MemeTemplate] = []
    counts: dict[int, int] = {}
    while pool and len(picked) < n:
        weights = []
        for s, t in pool:
            bc = min(t.box_count, 4)
            crowd = counts.get(bc, 0)
            weights.append(math.exp((s - 0.35 * crowd) / temperature))
        total = sum(weights)
        r = rnd.random() * total
        acc = 0.0
        for i, w in enumerate(weights):
            acc += w
            if acc >= r:
                _, t = pool.pop(i)
                keys = set(_name_keys(t.name))
                if any(keys & set(_name_keys(p.name)) for p in picked):
                    break  # a near-duplicate of a picked template ("Elizabeth Holmes" variants)
                picked.append(t)
                counts[min(t.box_count, 4)] = counts.get(min(t.box_count, 4), 0) + 1
                break
    return picked


async def meme_shortlist(brand: str, bucket_hints: list[str], n: int = 20) -> list[MemeTemplate]:
    """A fresh, diverse, trend-weighted shortlist of templates for this brand's deck."""
    templates = await get_meme_templates()
    if not templates:
        return []
    try:
        trend = await trend_scores(templates)
    except Exception:  # noqa: BLE001 - trends are optional
        trend = {}
    if not (settings.imgflip_username and settings.imgflip_password):
        # Without Imgflip's API we caption locally: only templates with measured boxes, or known
        # classics whose 1-2 captions read well as top/bottom text. New templates often carry their
        # own text or odd panels, so they wait for credentials.
        templates = [t for t in templates if captionable(t)]
    scores = score_templates(templates, trend, recent_penalties(brand), bucket_hints)
    rnd = random.Random(time.time_ns())
    ranked = sorted(((scores[t.id], t) for t in templates), key=lambda x: -x[0])[:max(60, n * 3)]
    picked = _sample(ranked, n, rnd)
    return [
        MemeTemplate(id=t.id, name=t.name, box_count=t.box_count, url=t.url, rank=t.rank, new=t.new,
                     trend=round(trend.get(t.id, 0.0), 2))
        for t in picked
    ]


def choose_replacement(original: MemeTemplate | None, box_count: int, used: set[str], avoid: set[str],
                       candidates: list[MemeTemplate], hint: str = "") -> MemeTemplate | None:
    """Best unused template with the same box count (captions carry over); then any with more boxes."""
    usable = [c for c in candidates if c.id not in used and c.id not in avoid]
    if not usable:
        usable = [c for c in candidates if c.id not in used]
    exact = [c for c in usable if c.box_count == box_count]
    pool = exact or [c for c in usable if c.box_count >= box_count and (c.id in LAYOUTS or c.box_count <= 2)]
    if not pool:
        return None
    return max(pool, key=lambda c: (c.trend + _fit(c, [hint]) + (0.6 if c.id in LAYOUTS else 0), -c.rank))
