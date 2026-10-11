"""Deck visuals (WORKPLAN C7). Owner: Agent B.

Fills every ``Visual`` request in a deck:
- want="ref":  candidates from Reddit + Tavily images, best one picked by the
  vision role (``llm_router.invoke_vision_json``), fallback: source rank.
- want="meme": Imgflip ``caption_image`` with ``meme_template_id`` + ``meme_boxes``
  (after a caption safety filter).
- want="mock": AI image (``settings.image_gen_provider``).
Results go through ``media.save_image``. Anything that fails stays a
placeholder; this module never raises for a single bad visual.

Memes (``memes.py``): templates come from a fresh, trend-weighted shortlist; a
template repeated within the deck or used in the last few decks is swapped for
the best unused one with the same box count, and every deck's templates are
recorded in ``{media_dir}/meme_history.json``. ``MemeTemplate`` and
``get_meme_templates`` are re-exported here for existing callers.

Fallbacks when a source is unavailable (verified 2026-10):
- Imgflip captioning needs an account. Without one, templates are captioned
  locally (``memes_layouts``: measured boxes for ~46 popular templates, classic
  outlined top/bottom text, else a caption band). A meme slot without a
  template becomes a generated meme-format scene (when an image model works)
  or a credited real meme image (Ref).
- Pollinations' anonymous endpoint now answers 402 (payment required). When no
  image generator works, a non-hero mock falls back to a web reference image
  for its brief (labelled ``kind="ref"`` with its source), the way the WLDD
  idea slides use real reels as visual references. The hero stays empty and
  the renderer draws a themed background instead.

Process: every slot (hero first, then deck order) is fetched in parallel under a
45 s overall cap; ref slots return a ranked candidate list, and a final pass in
deck order assigns images so the same picture never appears twice in a deck.
"""

import asyncio
import base64
import io
import logging
import random
import re
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urlparse

import httpx
from PIL import Image, ImageChops
from pydantic import BaseModel
from tavily import AsyncTavilyClient

from app.core.config import settings
from app.schemas.deck import Deck, Visual
from app.services import llm_router, trends
from app.services.media import image_digest, resolve_media, save_image
from app.services.memes import (  # re-exported: callers import these from visuals
    MemeTemplate,
    captionable,
    choose_replacement,
    get_meme_templates,
    meme_shortlist,
    recently_used,
    record_meme_use,
    template_image_urls,
)
from app.services.memes_layouts import render_meme
from app.services.visuals_http import FetchedImage, fetch_image, new_client, watermarked_url
from app.services.visuals_safety import is_safe_text

logger = logging.getLogger(__name__)

OVERALL_TIMEOUT = 45.0
SLOT_TIMEOUT = 30.0
MAX_REF_CANDIDATES = 6
MIN_REF_SIDE = 240
MOCK_FALLBACK_TO_REF = True

MOCK_STYLE = "Instagram reel thumbnail, Indian context, photorealistic, no text, no logos, no watermark"
HERO_STYLE = "wide 16:9 background photo, strong negative space, dark tones, no text, no logos, no watermark"


@dataclass(frozen=True)
class VisualBudget:
    """Max visuals filled per kind; requests beyond the budget stay placeholders."""

    ref: int
    meme: int
    mock: int


@dataclass
class _Candidate:
    data: bytes
    mime: str
    source_url: str
    credit: str
    kind: str
    caption: str = ""

    @property
    def digest(self) -> str:
        return image_digest(self.data)


@dataclass
class _Slot:
    visual: Visual
    want: str
    is_hero: bool = False
    candidates: list[_Candidate] = field(default_factory=list)


# --- helpers ----------------------------------------------------------------


def _iter_visuals(node: Any):
    """Every Visual inside a pydantic model tree (slides, cards, lists), in order."""
    if isinstance(node, Visual):
        yield node
        return
    if isinstance(node, BaseModel):
        for name in type(node).model_fields:
            yield from _iter_visuals(getattr(node, name, None))
    elif isinstance(node, list | tuple):
        for item in node:
            yield from _iter_visuals(item)


def _watermarked(v: Visual) -> bool:
    """Anonymous Pollinations images and stock-library previews carry watermarks: never keep them."""
    anon_poll = (v.credit or "") == "AI mock (Pollinations)" and not getattr(settings, "pollinations_api_key", None)
    return anon_poll or watermarked_url(v.source_url, v.credit)


def _already_filled(v: Visual) -> bool:
    return v.kind != "placeholder" and resolve_media(v.url) is not None and not _watermarked(v)


def _domain(url: str) -> str:
    host = urlparse(url).netloc.lower().split(":")[0]
    for known in ("instagram.com", "reddit.com", "redd.it", "facebook.com", "fbcdn.net", "twitter.com", "x.com",
                  "youtube.com", "ytimg.com", "pinterest.com", "pinimg.com"):
        if host == known or host.endswith("." + known):
            return {"redd.it": "reddit.com", "fbcdn.net": "facebook.com", "ytimg.com": "youtube.com",
                    "pinimg.com": "pinterest.com"}.get(known, known)
    return host.removeprefix("www.").removeprefix("m.")


def _usable(data: bytes, min_side: int = MIN_REF_SIDE) -> bool:
    try:
        with Image.open(io.BytesIO(data)) as img:
            w, h = img.size
            if min(w, h) < min_side or not 0.4 <= w / h <= 2.6:
                return False
            # Reject almost-blank images (blank placeholders, white boxes).
            small = img.convert("L").resize((32, 32))
            lo, hi = small.getextrema()
            return hi - lo > 40
    except Exception:
        return False


def trim_borders(data: bytes) -> bytes:
    """Crop uniform letterbox/pillarbox bars (video thumbnails, padded screenshots)."""
    try:
        with Image.open(io.BytesIO(data)) as src:
            img = src.convert("RGB")
    except Exception:
        return data
    w, h = img.size
    corner = img.getpixel((0, 0))
    bg = Image.new("RGB", img.size, corner)
    diff = ImageChops.difference(img, bg).convert("L").point(lambda p: 255 if p > 28 else 0)
    bbox = diff.getbbox()
    if not bbox:
        return data
    bw, bh = bbox[2] - bbox[0], bbox[3] - bbox[1]
    trimmed = (w - bw) / w + (h - bh) / h
    if trimmed < 0.04 or bw * bh < 0.45 * w * h:
        return data
    out = io.BytesIO()
    img.crop(bbox).save(out, "JPEG", quality=92)
    return out.getvalue()


def _thumb_for_vision(data: bytes) -> tuple[bytes, str]:
    with Image.open(io.BytesIO(data)) as img:
        img = img.convert("RGB")
        img.thumbnail((512, 512))
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=80)
        return buf.getvalue(), "image/jpeg"


_STOP = {"the", "a", "an", "of", "for", "and", "or", "to", "in", "on", "with", "meme", "memes", "reel",
         "post", "instagram", "viral", "india", "indian", "trend", "trending", "image", "photo"}


def _keywords(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]{3,}", text.lower()) if w not in _STOP}


# --- ref candidates ---------------------------------------------------------


@dataclass
class _Ctx:
    client: httpx.AsyncClient
    tavily: AsyncTavilyClient | None
    pulse: list[trends.TrendPost]
    templates: dict[str, MemeTemplate]
    theme_name: str
    sem_download: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(10))
    sem_search: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(4))
    sem_gen: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(3))
    mock_dead: set[str] = field(default_factory=set)
    logged: set[str] = field(default_factory=set)
    motif: str = ""
    season_words: str = ""
    season_hero_query: str = ""
    season_photo_query: str = ""
    shortlist: list[MemeTemplate] = field(default_factory=list)
    meme_used: set[str] = field(default_factory=set)
    product_images: list[str] = field(default_factory=list)

    def log_once(self, key: str, msg: str, *args: Any) -> None:
        if key not in self.logged:
            self.logged.add(key)
            logger.info(msg, *args)


async def _tavily_images(ctx: _Ctx, query: str) -> list[tuple[str, str, str]]:
    """(image_url, description, source) for a query, in Tavily rank order."""
    if not ctx.tavily:
        ctx.log_once("tavily", "visuals: TAVILY_API_KEY missing; no web reference images")
        return []
    params: dict[str, Any] = {"include_images": True, "include_image_descriptions": True, "max_results": 5}
    if settings.research_country:
        params["country"] = settings.research_country
    async with ctx.sem_search:
        try:
            result = await asyncio.wait_for(ctx.tavily.search(query, **params), timeout=15)
        except Exception as exc:
            try:  # some Tavily versions reject "country" for this topic
                params.pop("country", None)
                result = await asyncio.wait_for(ctx.tavily.search(query, **params), timeout=15)
            except Exception:
                logger.info("visuals: Tavily image search failed for %r: %s", query, exc)
                return []
    out = []
    for item in result.get("images") or []:
        if isinstance(item, str):
            out.append((item, "", _domain(item)))
        elif isinstance(item, dict) and item.get("url"):
            desc = item.get("description") or item.get("title") or ""
            out.append((item["url"], str(desc), _domain(item["url"])))
    return out


async def _reddit_images(ctx: _Ctx, query: str) -> list[tuple[str, str, str]]:
    posts = await trends.search_reddit_images(query, limit=4)
    out = []
    for p in posts:
        if p.image_url and is_safe_text(p.title):
            out.append((p.image_url, p.title, p.permalink or f"reddit.com/r/{p.subreddit}"))
    return out


def _pulse_matches(ctx: _Ctx, query: str) -> list[tuple[str, str, str]]:
    """This week's top posts sharing at least two keywords with the query (cheap relevance gate)."""
    kws = _keywords(query)
    out = []
    for p in ctx.pulse:
        if p.image_url and len(kws & _keywords(p.title)) >= 2 and is_safe_text(p.title):
            out.append((p.image_url, p.title, p.permalink or f"reddit.com/r/{p.subreddit}"))
    return out[:2]


def _interleave(*lists: list) -> list:
    out = []
    for i in range(max((len(x) for x in lists), default=0)):
        for x in lists:
            if i < len(x):
                out.append(x[i])
    return out


def _relevance(brief: str, desc: str) -> int:
    return len(_keywords(brief) & _keywords(desc))


async def _download(ctx: _Ctx, url: str) -> FetchedImage | None:
    async with ctx.sem_download:
        return await fetch_image(ctx.client, url)


async def _ref_candidates(ctx: _Ctx, v: Visual, kind: str = "ref") -> list[_Candidate]:
    query = (v.query or v.brief or "").strip()
    if not query:
        return []
    tav, red = await asyncio.gather(_tavily_images(ctx, query), _reddit_images(ctx, query))
    # Without a vision model the order is the choice: search hits whose description shares the
    # most words with the brief first (stable, so source rank breaks ties), pulse posts last.
    brief = f"{v.brief} {v.query or ''}"
    searched = sorted(_interleave(tav, red), key=lambda c: -_relevance(brief, c[1]))
    pool, seen = [], set()
    for url, desc, src in [*searched, *_pulse_matches(ctx, query)]:
        if watermarked_url(url, src):
            continue  # stock previews are watermarked
        if url not in seen and is_safe_text(desc):
            seen.add(url)
            pool.append((url, desc, src))
    pool = pool[: MAX_REF_CANDIDATES + 2]  # a couple spare for failed downloads
    fetched = await asyncio.gather(*(_download(ctx, url) for url, _, _ in pool))

    cands: list[_Candidate] = []
    digests: set[str] = set()
    for (url, desc, src), img in zip(pool, fetched, strict=True):
        if not img:
            continue
        if watermarked_url(img.url, img.page_url):
            continue
        img = FetchedImage(trim_borders(img.data), img.mime, img.url, img.page_url)
        if not _usable(img.data):
            continue
        page = src if src.startswith("http") else (img.page_url or url)
        c = _Candidate(
            data=img.data, mime=img.mime, kind=kind, caption=desc[:200],
            source_url=page, credit=f"Ref: {_domain(page)}"[:120],
        )
        if c.digest not in digests:
            digests.add(c.digest)
            cands.append(c)
        if len(cands) >= MAX_REF_CANDIDATES:
            break
    return await _rank_with_vision(v, cands)


async def _rank_with_vision(v: Visual, cands: list[_Candidate]) -> list[_Candidate]:
    if len(cands) < 2:
        return cands
    prompt = (
        "You pick reference images for an Indian social-media agency pitch deck slide.\n"
        f"Slot brief: {v.brief or v.query}\n"
        f"Search query: {v.query or v.brief}\n"
        f"There are {len(cands)} images, numbered 0 to {len(cands) - 1} in the order given.\n"
        "Score each for relevance to the brief, legibility at slide size and brand safety "
        "(no nudity, gore, hate, slurs, political or religious provocation, competitor logos as the focus).\n"
        'Reply as JSON: {"ranking": [best index first, only usable ones], "reject": [unsafe or irrelevant indexes]}'
    )
    try:
        images = [_thumb_for_vision(c.data) for c in cands]
        answer = await asyncio.wait_for(llm_router.invoke_vision_json(prompt, images), timeout=20)
    except Exception as exc:  # vision is optional: fall back to source rank
        logger.info("visuals: vision ranking unavailable (%s); using source rank", exc.__class__.__name__)
        return cands
    if not isinstance(answer, dict):
        return cands
    reject = {i for i in answer.get("reject") or [] if isinstance(i, int)}
    ranking = [i for i in answer.get("ranking") or [] if isinstance(i, int) and 0 <= i < len(cands)]
    ordered = [cands[i] for i in dict.fromkeys(ranking) if i not in reject]
    if not ordered:
        ordered = [c for i, c in enumerate(cands) if i not in reject]
    return ordered


# --- memes ------------------------------------------------------------------


async def _imgflip_caption(ctx: _Ctx, template_id: str, boxes: list[str]) -> _Candidate | None:
    form: dict[str, str] = {
        "template_id": template_id,
        "username": settings.imgflip_username or "",
        "password": settings.imgflip_password or "",
    }
    for i, text in enumerate(boxes):
        form[f"boxes[{i}][text]"] = text
    try:
        resp = await ctx.client.post("https://api.imgflip.com/caption_image", data=form)
        body = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.info("visuals: Imgflip caption_image failed: %s", exc)
        return None
    if not body.get("success"):
        logger.info("visuals: Imgflip refused caption: %s", body.get("error_message"))
        return None
    data = body.get("data") or {}
    img = await _download(ctx, data.get("url", ""))
    if not img:
        return None
    name = ctx.templates.get(template_id)
    return _Candidate(data=img.data, mime=img.mime, kind="meme",
                      source_url=data.get("page_url") or data.get("url", ""),
                      credit=f"Brand meme, Imgflip template: {name.name if name else template_id}"[:120])


async def _caption(ctx: _Ctx, tid: str, boxes: list[str]) -> _Candidate | None:
    if settings.imgflip_username and settings.imgflip_password:
        made = await _imgflip_caption(ctx, tid, boxes)
        if made:
            return made
    else:
        ctx.log_once("imgflip", "visuals: Imgflip credentials missing; captioning templates locally")
    tpl = ctx.templates.get(tid)
    if not tpl or not tpl.url:
        return None
    img = None
    for url in template_image_urls(tpl):
        img = await _download(ctx, url)
        if img:
            break
    if not img:
        return None
    try:
        data = await asyncio.to_thread(render_meme, img.data, tid, boxes, tpl.box_count)
    except Exception as exc:  # noqa: BLE001 - one bad meme never breaks the deck
        logger.info("visuals: local meme caption failed: %s", exc)
        return None
    return _Candidate(data=data, mime="image/jpeg", kind="meme", source_url=tpl.url,
                      credit=f"Brand meme, template: {tpl.name}"[:120])


async def _meme(ctx: _Ctx, v: Visual) -> list[_Candidate]:
    boxes = [b for b in v.meme_boxes if b and b.strip()]
    tid = (v.meme_template_id or "").strip()
    if boxes and not is_safe_text(*boxes):
        logger.info("visuals: meme caption failed the safety filter; left as placeholder")
        return []
    if not tid or not boxes:
        return await _meme_without_template(ctx, v, boxes)
    tried: set[str] = set()
    for _attempt in range(3):  # the template, then up to two captionable stand-ins
        tried.add(tid)
        made = await _caption(ctx, tid, boxes)
        if made:
            v.meme_template_id = tid
            return [made]
        current = ctx.templates.get(tid)
        repl = choose_replacement(current, current.box_count if current else len(boxes),
                                  ctx.meme_used | tried, set(), ctx.shortlist, v.brief or "")
        if repl is None:
            break
        logger.info("visuals: meme template %s failed; retrying with %s (%s)", tid, repl.id, repl.name)
        ctx.meme_used.add(repl.id)
        tid = repl.id
    return []


async def _meme_without_template(ctx: _Ctx, v: Visual, boxes: list[str]) -> list[_Candidate]:
    """No template: a generated brand scene in meme format (when an image model works), else real
    trending meme images for the brief as credited refs."""
    if v.brief and _generator_ready() and "generator" not in ctx.mock_dead:
        motif = f", featuring {ctx.motif}" if ctx.motif else ""
        scene = Visual(want="mock", brief=f"meme-format reaction photo: {v.brief}{motif}, expressive faces")
        made = await _mock(ctx, scene, hero=False, fallback=False)
        if made:
            cand = made[0]
            if boxes:
                try:
                    cand.data = await asyncio.to_thread(render_meme, cand.data, "", boxes, len(boxes))
                except Exception:  # noqa: BLE001
                    pass
            cand.kind, cand.credit = "meme", "Brand meme (AI scene)"
            return [cand]
    query = (v.query or v.brief or "").strip()
    if not query:
        return []
    ref = Visual(want="ref", brief=v.brief, query=f"{query} meme")
    return await _ref_candidates(ctx, ref, kind="ref")


# --- AI mocks ---------------------------------------------------------------


def _generator_ready() -> bool:
    if settings.image_gen_provider == "cloudflare":
        return bool(settings.cloudflare_account_id and settings.cloudflare_api_token)
    if settings.image_gen_provider == "pollinations":
        return bool(getattr(settings, "pollinations_api_key", None))
    return False


def _mock_prompt(ctx: _Ctx, v: Visual, hero: bool) -> str:
    base = " ".join((v.brief or v.query or "").split())[:240]
    extra = []
    if ctx.motif and not hero and ctx.motif.split(",")[0].strip().lower() not in base.lower():
        extra.append(f"brand world: {ctx.motif}")
    if ctx.season_words and not hero:
        extra.append(ctx.season_words)
    tail = (", " + ", ".join(extra)) if extra else ""
    return f"{base}{tail}. {HERO_STYLE if hero else MOCK_STYLE}"


async def _pollinations(ctx: _Ctx, prompt: str, hero: bool) -> bytes | None:
    key = getattr(settings, "pollinations_api_key", None)
    seed = random.randint(1, 2**31 - 1)
    params = f"width=1280&height=720&nologo=true&seed={seed}" if hero else f"width=1024&height=1024&nologo=true&seed={seed}"
    if key:  # new authenticated endpoint (untested here: no key)
        url = f"https://gen.pollinations.ai/image/{quote(prompt, safe='')}?{params}&model=flux"
        headers = {"Authorization": f"Bearer {key}"}
    else:
        url = f"https://image.pollinations.ai/prompt/{quote(prompt, safe='')}?{params}"
        headers = {}
    try:
        resp = await ctx.client.get(url, headers=headers, timeout=httpx.Timeout(60.0, connect=8.0))
    except httpx.HTTPError as exc:
        logger.info("visuals: Pollinations request failed: %s", exc)
        return None
    if resp.status_code in (401, 402, 403):
        ctx.mock_dead.add("pollinations")
        ctx.log_once("pollinations", "visuals: Pollinations answered %s (key/payment required); no AI mocks",
                     resp.status_code)
        return None
    if resp.status_code != 200 or not resp.headers.get("content-type", "").startswith("image/"):
        logger.info("visuals: Pollinations returned %s", resp.status_code)
        return None
    return resp.content


async def _cloudflare(ctx: _Ctx, prompt: str) -> bytes | None:
    acct, token = settings.cloudflare_account_id, settings.cloudflare_api_token
    if not acct or not token:
        ctx.mock_dead.add("cloudflare")
        ctx.log_once("cloudflare", "visuals: Cloudflare Workers AI keys missing; no AI mocks")
        return None
    url = f"https://api.cloudflare.com/client/v4/accounts/{acct}/ai/run/@cf/black-forest-labs/flux-1-schnell"
    try:
        resp = await ctx.client.post(url, json={"prompt": prompt, "steps": 4},
                                     headers={"Authorization": f"Bearer {token}"},
                                     timeout=httpx.Timeout(60.0, connect=8.0))
        body = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.info("visuals: Cloudflare image generation failed: %s", exc)
        return None
    if resp.status_code in (401, 403):
        ctx.mock_dead.add("cloudflare")
        ctx.log_once("cloudflare-auth", "visuals: Cloudflare rejected the token (%s)", resp.status_code)
        return None
    image = (body.get("result") or {}).get("image") if isinstance(body, dict) else None
    if not image:
        logger.info("visuals: Cloudflare returned no image: %s", str(body)[:200])
        return None
    try:
        return base64.b64decode(image)
    except ValueError:
        return None


async def _mock(ctx: _Ctx, v: Visual, hero: bool, fallback: bool = True) -> list[_Candidate]:
    provider = settings.image_gen_provider
    prompt = _mock_prompt(ctx, v, hero)
    data = None
    if not _generator_ready():
        # Anonymous Pollinations watermarks its images (and mostly answers 402): never used for client decks.
        ctx.log_once("nogen", "visuals: no image generator key (POLLINATIONS_API_KEY / Cloudflare); "
                              "AI mocks fall back to brand and web images")
    elif provider not in ctx.mock_dead and is_safe_text(prompt):
        async with ctx.sem_gen:
            if provider not in ctx.mock_dead:
                data = await (_pollinations(ctx, prompt, hero) if provider == "pollinations"
                              else _cloudflare(ctx, prompt))
    if data and _usable(data, min_side=256):
        label = "FLUX schnell" if provider == "cloudflare" else "Pollinations"
        return [_Candidate(data=data, mime="image/jpeg", kind="mock", source_url="",
                           credit=f"AI mock ({label})", caption=v.brief[:200])]
    if hero:
        return await _hero_fallback(ctx, v)
    if not fallback or not MOCK_FALLBACK_TO_REF:
        return []
    return await _ref_candidates(ctx, v, kind="ref")


COMMONS_UA = "PitchCraft/0.2 (pitch deck generator; season photos)"


async def _commons_images(ctx: "_Ctx", query: str, limit: int = 6) -> list[_Candidate]:
    """Freely licensed photos from Wikimedia Commons (no key), credited with author + licence."""
    try:
        resp = await ctx.client.get("https://commons.wikimedia.org/w/api.php", params={
            "action": "query", "generator": "search", "gsrsearch": f"{query} filetype:bitmap", "gsrnamespace": 6,
            "gsrlimit": 10, "prop": "imageinfo", "iiprop": "url|size|extmetadata", "iiurlwidth": 1600,
            "format": "json"}, headers={"User-Agent": COMMONS_UA}, timeout=10)
        pages = (resp.json().get("query") or {}).get("pages") or {}
    except (httpx.HTTPError, ValueError) as exc:
        logger.info("visuals: Wikimedia Commons search failed: %s", exc)
        return []
    items = []
    for page in sorted(pages.values(), key=lambda p: p.get("index", 0)):
        info = (page.get("imageinfo") or [{}])[0]
        if info.get("width", 0) < 1000 or info.get("width", 0) < 1.15 * info.get("height", 1):
            continue
        meta = info.get("extmetadata") or {}
        artist = re.sub(r"<[^>]+>", "", (meta.get("Artist") or {}).get("value") or "").strip()[:40]
        lic = ((meta.get("LicenseShortName") or {}).get("value") or "").strip()
        items.append((info.get("thumburl") or info.get("url"), info.get("descriptionurl") or "",
                      f"Background: {artist or 'Wikimedia Commons'}, {lic or 'CC'} (Wikimedia Commons)"))
        if len(items) >= limit:
            break
    fetched = await asyncio.gather(*(_download(ctx, u) for u, _, _ in items))
    out = []
    for (u, page_url, credit), img in zip(items, fetched, strict=True):
        if img and _usable(img.data, 400):
            out.append(_Candidate(data=img.data, mime=img.mime, kind="ref", source_url=page_url or u,
                                  credit=credit[:120], caption=query))
    return out


def _landscape(data: bytes, min_w: int = 800) -> bool:
    try:
        with Image.open(io.BytesIO(data)) as img:
            return img.width >= min_w and img.width / img.height >= 1.15
    except Exception:  # noqa: BLE001
        return False


async def _hero_fallback(ctx: _Ctx, v: Visual) -> list[_Candidate]:
    """Hero without an image model: the brand's product/site image, else a credited season/mood image."""
    out: list[_Candidate] = []
    for url in ctx.product_images:
        path = resolve_media(url)
        if path:
            data = path.read_bytes()
            if _landscape(data, 700):
                out.append(_Candidate(data=data, mime="image/jpeg", kind="ref", source_url="",
                                      credit="Brand website image"))
    # Real season photos from Wikimedia Commons (freely licensed, large, no watermark) ...
    if ctx.season_photo_query:
        out += await _commons_images(ctx, ctx.season_photo_query)
    # ... then web images for the season mood, landscape only.
    if len(out) < 2:
        queries = [q for q in (ctx.season_hero_query, v.query) if q]
        found = await asyncio.gather(*(_ref_candidates(ctx, Visual(want="ref", brief=q, query=q), kind="ref")
                                       for q in queries[:2]))
        for c in (c for batch in found for c in batch):
            if _landscape(c.data, 800):
                c.credit = (c.credit or "Ref").replace("Ref:", "Background:")
                out.append(c)
    return out


# --- orchestration ----------------------------------------------------------


async def _fill(ctx: _Ctx, slot: _Slot) -> None:
    try:
        if slot.want == "ref":
            slot.candidates = await _ref_candidates(ctx, slot.visual)
        elif slot.want == "meme":
            slot.candidates = await _meme(ctx, slot.visual)
        else:
            slot.candidates = await _mock(ctx, slot.visual, slot.is_hero)
    except Exception:  # one bad visual must never break the deck
        logger.exception("visuals: slot failed (%s: %s)", slot.want, slot.visual.brief[:60])
        slot.candidates = []


def _season_words(season: str | None) -> str:
    from app.services.brand_identity_season import get_season

    sea = get_season(season)
    return ", ".join(sea.prompt.split(", ")[:3]) if sea else ""


async def _plan_memes(deck: Deck, meme_slots: list["_Slot"], templates: dict[str, MemeTemplate]
                      ) -> tuple[list[MemeTemplate], set[str]]:
    """No template twice in a deck, none from the last few decks: swap for the best unused
    template with the same box count from a fresh shortlist (captions carry over)."""
    hints = [s.visual.brief or s.visual.query or "" for s in meme_slots]
    shortlist = await meme_shortlist(deck.brand, hints, n=30)
    for t in shortlist:
        templates.setdefault(t.id, t)
    recent = recently_used(deck.brand)
    used: set[str] = set()
    local = not (settings.imgflip_username and settings.imgflip_password)
    for slot in meme_slots:
        v = slot.visual
        boxes = [b for b in v.meme_boxes if b and b.strip()]
        tid = (v.meme_template_id or "").strip()
        if not boxes:
            continue
        current = templates.get(tid)
        bad = (not current) or tid in used or tid in recent or current.box_count < len(boxes) \
            or (local and not captionable(current))
        if bad:
            repl = choose_replacement(current, current.box_count if current else len(boxes), used, recent,
                                      shortlist, v.brief or "")
            if repl is None and current is None:
                continue
            if repl is not None:
                logger.info("visuals: meme template %s -> %s (%s)", tid or "-", repl.id, repl.name)
                v.meme_template_id = repl.id
                tid = repl.id
        used.add(tid)
    return shortlist, used


EXTRA_MEMES = 6  # memes past the budget are still captioned locally (cheap, no API quota)
EXTRA_REF_SEARCHES = 3  # leftover slots may run a few more image searches while time allows


def _select_slots(deck: Deck, budget: VisualBudget) -> tuple[list[_Slot], list[_Slot]]:
    """(budgeted slots, extra slots past the budget). The hero is attempted outside the budget."""
    left = {"ref": max(0, budget.ref), "meme": max(0, budget.meme), "mock": max(0, budget.mock)}
    slots: list[_Slot] = []
    extra: list[_Slot] = []
    hero = deck.theme.hero_image
    if hero is not None and _watermarked(hero):
        hero.kind, hero.url, hero.credit, hero.source_url = "placeholder", None, None, None  # type: ignore[assignment]
    if hero is not None and not _already_filled(hero) and (hero.brief or hero.query):
        slots.append(_Slot(visual=hero, want="mock", is_hero=True))
    seen: set[int] = set()
    for slide in deck.slides:
        for v in _iter_visuals(slide):
            if id(v) in seen or _already_filled(v):
                continue
            seen.add(id(v))
            if _watermarked(v):  # refill instead of keeping a watermarked image
                v.kind, v.url, v.credit, v.source_url = "placeholder", None, None, None  # type: ignore[assignment]
            want = v.want if v.want in left else "mock"
            if not (v.brief or v.query or v.meme_boxes):
                continue
            if left[want] > 0:
                left[want] -= 1
                slots.append(_Slot(visual=v, want=want))
            else:
                extra.append(_Slot(visual=v, want=want))
    return slots, extra


def _spare_for(slot: _Slot, spares: list[_Candidate], used: set[str]) -> _Candidate | None:
    """The unused candidate (gathered for another slot) that best matches this slot's brief."""
    brief = f"{slot.visual.brief} {slot.visual.query or ''}"
    best, best_score = None, -1
    for c in spares:
        if c.digest in used or c.kind == "meme":
            continue
        score = _relevance(brief, c.caption)
        if score > best_score:
            best, best_score = c, score
    return best


def _assign(slot: _Slot, cand: _Candidate, used: set[str], filled: dict[str, int]) -> bool:
    if cand.digest in used:
        return False
    try:
        url = save_image(cand.data)
    except ValueError:
        return False
    used.add(cand.digest)
    v = slot.visual
    v.kind = cand.kind  # type: ignore[assignment]
    v.url = url
    v.source_url = cand.source_url or None
    v.credit = cand.credit or None
    filled[cand.kind if cand.kind in filled else "mock"] += 1
    return True


async def _run_slots(ctx: _Ctx, slots: list[_Slot], deadline: float) -> None:
    if not slots:
        return
    tasks = [asyncio.create_task(asyncio.wait_for(_fill(ctx, s), SLOT_TIMEOUT)) for s in slots]
    done, pending = await asyncio.wait(tasks, timeout=max(3.0, deadline - time.monotonic()))
    for t in pending:
        t.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    for t in done:
        if not t.cancelled() and t.exception():
            logger.info("visuals: slot timed out or failed: %s", t.exception().__class__.__name__)


async def attach_visuals(deck: Deck, budget: VisualBudget, identity: Any = None) -> Deck:
    """Fill the deck's visual requests (incl. ``deck.theme.hero_image``) within ``budget``.

    Every slot falls back along: requested source -> web/Reddit ref for its brief -> an unused
    image found for another slot -> the brand's product/site images. Memes past the budget are
    still captioned locally. ``identity`` (a ``BrandIdentity``) is optional: without it the
    identity cached for ``deck.brand`` is used, if any. Never raises for a single visual.
    """
    deck = deck.model_copy(deep=True)
    slots, extra = _select_slots(deck, budget)
    if not slots and not extra:
        return deck
    started = time.monotonic()
    deadline = started + OVERALL_TIMEOUT
    if identity is None:
        from app.services.brand_identity import cached_identity

        identity = cached_identity(deck.brand)
    from app.services.brand_identity_season import get_season

    sea = get_season(deck.theme.season)

    async with new_client(timeout=10) as client:
        templates: dict[str, MemeTemplate] = {}
        pulse: list[trends.TrendPost] = []
        extra_memes = [s for s in extra if s.want == "meme"][:EXTRA_MEMES]
        meme_slots = [s for s in slots if s.want == "meme"] + extra_memes
        shortlist: list[MemeTemplate] = []
        meme_used: set[str] = set()
        if meme_slots:
            templates = {t.id: t for t in await get_meme_templates()}
            try:
                shortlist, meme_used = await asyncio.wait_for(_plan_memes(deck, meme_slots, templates), timeout=15)
            except Exception as exc:  # noqa: BLE001 - keep the writer's templates
                logger.info("visuals: meme planning skipped (%s)", exc.__class__.__name__)
        try:
            pulse = (await asyncio.wait_for(trends.get_trend_pulse(), timeout=8)).posts
        except Exception:  # noqa: BLE001
            pulse = []
        ctx = _Ctx(
            client=client,
            tavily=AsyncTavilyClient(api_key=settings.tavily_api_key) if settings.tavily_api_key else None,
            pulse=pulse, templates=templates, theme_name=deck.theme.name,
            motif=deck.theme.motif or "", season_words=_season_words(deck.theme.season),
            season_hero_query=(f"{sea.label} {', '.join(sea.prompt.split(', ')[1:3])} photo" if sea else ""),
            season_photo_query=(sea.photo if sea else ""),
            shortlist=shortlist, meme_used=meme_used,
            product_images=list(getattr(identity, "product_images", []) or []),
        )
        first = slots + extra_memes
        await _run_slots(ctx, first, deadline)

        used: set[str] = set()
        filled = {"ref": 0, "meme": 0, "mock": 0}
        for slot in first:  # assign in deck order so the same image is never used twice
            for cand in slot.candidates:
                if _assign(slot, cand, used, filled):
                    break

        # Leftovers (failed or past the budget): spare candidates, a few more searches, brand images.
        leftovers = [s for s in first + [x for x in extra if x not in extra_memes]
                     if s.visual.kind == "placeholder" and not s.is_hero]
        spares = [c for s in first for c in s.candidates if c.digest not in used]
        still: list[_Slot] = []
        for slot in leftovers:
            cand = _spare_for(slot, spares, used)
            if not (cand and _assign(slot, cand, used, filled)):
                still.append(slot)
        searchable = [s for s in still if s.visual.brief or s.visual.query][:EXTRA_REF_SEARCHES]
        if searchable and time.monotonic() < deadline - 5:
            for s in searchable:
                s.want = "ref"
            await _run_slots(ctx, searchable, min(deadline, time.monotonic() + 15))
            for slot in searchable:
                for cand in slot.candidates:
                    if _assign(slot, cand, used, filled):
                        break
        product_pool = [resolve_media(u) for u in ctx.product_images]
        for slot in [s for s in still if s.visual.kind == "placeholder"]:
            for path in product_pool:
                if path is None:
                    continue
                data = path.read_bytes()
                cand = _Candidate(data=data, mime="image/jpeg", kind="ref", source_url=getattr(identity, "site_url", "") or "",
                                  credit="Brand website image")
                if _assign(slot, cand, used, filled):
                    break

    meme_ids = [s.visual.meme_template_id for s in first
                if s.want == "meme" and s.visual.kind == "meme" and s.visual.meme_template_id]
    if meme_ids:
        record_meme_use(deck.brand, meme_ids)
    every = slots + extra
    logger.info(
        "visuals: %d slots (%d past budget), filled ref=%d meme=%d mock=%d, %d left for brand tiles, %.1fs",
        len(every), len(extra), filled["ref"], filled["meme"], filled["mock"],
        sum(1 for s in every if s.visual.kind == "placeholder"), time.monotonic() - started,
    )
    return deck
