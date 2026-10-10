"""Reddit trend pulse (WORKPLAN C7). Owner: Agent B.

App-only OAuth against Reddit, top posts of the week from
``settings.trend_subreddits`` (+ extra subreddits), NSFW/spoiler/stickied posts
skipped, cached in memory for an hour. Missing credentials => empty pulse.

Without credentials Reddit's ``.json`` endpoints return 403 (verified 2026-10),
but its public Atom/RSS feeds still answer, with a tight anonymous rate limit
(429 after a couple of quick requests). So without keys we read the RSS feeds,
one combined multireddit request per call (``r/a+b+c``), and back off for a few
minutes after a 429. RSS has no score or NSFW flag: posts are ranked by feed
order and filtered by title keywords instead.
"""

import asyncio
import html
import logging
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote_plus

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

CACHE_TTL = 3600.0
RSS_BACKOFF = 300.0
_TIMEOUT = httpx.Timeout(10.0, connect=5.0)
_SUB_RE = re.compile(r"^[A-Za-z0-9_]{2,32}$")
_IMAGE_HOST_RE = re.compile(r"^https://(i\.redd\.it|preview\.redd\.it|i\.imgur\.com)/", re.IGNORECASE)
_UNSAFE_TITLE_RE = re.compile(r"\b(nsfw|nsfl|18\+|spoilers?|porn|nude|gore)\b", re.IGNORECASE)
_ATOM = {"a": "http://www.w3.org/2005/Atom", "media": "http://search.yahoo.com/mrss/"}


@dataclass(frozen=True)
class TrendPost:
    title: str
    subreddit: str
    score: int
    permalink: str
    image_url: str | None = None


@dataclass
class TrendPulse:
    posts: list[TrendPost] = field(default_factory=list)


_cache: dict[str, tuple[float, list[TrendPost]]] = {}
_token: tuple[str, float] | None = None
_token_lock = asyncio.Lock()
_rss_lock = asyncio.Lock()
_rss_blocked_until = 0.0
_logged: set[str] = set()


def _log_once(key: str, msg: str, *args: Any) -> None:
    if key not in _logged:
        _logged.add(key)
        logger.info(msg, *args)


def _cached(key: str) -> list[TrendPost] | None:
    hit = _cache.get(key)
    if hit and hit[0] > time.monotonic():
        return hit[1]
    return None


def _store(key: str, posts: list[TrendPost]) -> list[TrendPost]:
    _cache[key] = (time.monotonic() + CACHE_TTL, posts)
    return posts


def _subreddits(extra: list[str] | None) -> list[str]:
    seen: dict[str, str] = {}
    for sub in [*settings.trend_subreddits, *(extra or [])]:
        sub = (sub or "").strip().removeprefix("r/").removeprefix("/r/")
        if _SUB_RE.fullmatch(sub) and sub.lower() not in seen:
            seen[sub.lower()] = sub
    return list(seen.values())


def _has_keys() -> bool:
    return bool(settings.reddit_client_id and settings.reddit_client_secret)


# --- OAuth (app-only) -------------------------------------------------------


async def _get_token(client: httpx.AsyncClient) -> str | None:
    global _token
    async with _token_lock:
        if _token and _token[1] > time.monotonic() + 60:
            return _token[0]
        try:
            resp = await client.post(
                "https://www.reddit.com/api/v1/access_token",
                data={"grant_type": "client_credentials"},
                auth=(settings.reddit_client_id or "", settings.reddit_client_secret or ""),
                headers={"User-Agent": settings.reddit_user_agent},
            )
            resp.raise_for_status()
            body = resp.json()
            _token = (body["access_token"], time.monotonic() + float(body.get("expires_in", 3600)))
            return _token[0]
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            logger.warning("Reddit OAuth failed: %s", exc)
            return None


def _image_from_json(post: dict[str, Any]) -> str | None:
    if post.get("is_video"):
        return None
    url = html.unescape(post.get("url_overridden_by_dest") or post.get("url") or "")
    if _IMAGE_HOST_RE.match(url) and re.search(r"\.(jpe?g|png|webp|gif)(\?|$)", url, re.IGNORECASE):
        return url
    if post.get("is_gallery") and isinstance(post.get("media_metadata"), dict):
        for item in post["media_metadata"].values():
            src = (item or {}).get("s", {}).get("u")
            if src:
                return html.unescape(src)
    images = (post.get("preview") or {}).get("images") or []
    if images:
        src = (images[0].get("source") or {}).get("url")
        if src:
            return html.unescape(src)
    return None


def _posts_from_listing(body: dict[str, Any]) -> list[TrendPost]:
    posts: list[TrendPost] = []
    for child in (body.get("data") or {}).get("children") or []:
        p = child.get("data") or {}
        if p.get("over_18") or p.get("spoiler") or p.get("stickied"):
            continue
        title = " ".join(str(p.get("title") or "").split())
        if not title or _UNSAFE_TITLE_RE.search(title):
            continue
        posts.append(
            TrendPost(
                title=title[:300],
                subreddit=str(p.get("subreddit") or ""),
                score=int(p.get("score") or 0),
                permalink="https://www.reddit.com" + str(p.get("permalink") or ""),
                image_url=_image_from_json(p),
            )
        )
    return posts


async def _oauth_get(client: httpx.AsyncClient, path: str, params: dict[str, Any]) -> list[TrendPost]:
    token = await _get_token(client)
    if not token:
        return []
    try:
        resp = await client.get(
            f"https://oauth.reddit.com{path}",
            params={**params, "raw_json": 1},
            headers={"Authorization": f"Bearer {token}", "User-Agent": settings.reddit_user_agent},
        )
        resp.raise_for_status()
        return _posts_from_listing(resp.json())
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("Reddit %s failed: %s", path, exc)
        return []


# --- Public RSS fallback (no keys) ------------------------------------------


def _posts_from_atom(xml_text: str) -> list[TrendPost]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    posts: list[TrendPost] = []
    entries = root.findall("a:entry", _ATOM)
    for rank, entry in enumerate(entries):
        title = " ".join((entry.findtext("a:title", "", _ATOM) or "").split())
        if not title or _UNSAFE_TITLE_RE.search(title):
            continue
        cat = entry.find("a:category", _ATOM)
        link = entry.find("a:link", _ATOM)
        thumb = entry.find("media:thumbnail", _ATOM)
        content = html.unescape(entry.findtext("a:content", "", _ATOM) or "")
        image = None
        full = re.search(r'href="(https://i\.redd\.it/[^"]+\.(?:jpe?g|png|webp|gif))"', content, re.IGNORECASE)
        if full:
            image = full.group(1)
        elif thumb is not None and thumb.get("url"):
            image = html.unescape(thumb.get("url", ""))
        posts.append(
            TrendPost(
                title=title[:300],
                subreddit=cat.get("term", "") if cat is not None else "",
                score=len(entries) - rank,  # RSS has no score: keep feed order
                permalink=link.get("href", "") if link is not None else "",
                image_url=image,
            )
        )
    return posts


async def _rss_get(client: httpx.AsyncClient, path: str, params: dict[str, Any]) -> list[TrendPost]:
    global _rss_blocked_until
    async with _rss_lock:  # anonymous Reddit allows very few requests; never burst
        if time.monotonic() < _rss_blocked_until:
            return []
        query = "&".join(f"{k}={quote_plus(str(v))}" for k, v in params.items())
        url = f"https://www.reddit.com{path}.rss" + (f"?{query}" if query else "")
        try:
            resp = await client.get(url, headers={"User-Agent": settings.reddit_user_agent})
        except httpx.HTTPError as exc:
            logger.info("Reddit RSS unreachable: %s", exc)
            return []
        if resp.status_code in (403, 429):
            _rss_blocked_until = time.monotonic() + RSS_BACKOFF
            logger.info("Reddit RSS returned %s (anonymous rate limit); skipping Reddit for %ds",
                        resp.status_code, int(RSS_BACKOFF))
            return []
        if resp.status_code != 200:
            return []
        return _posts_from_atom(resp.text)


# --- Public API -------------------------------------------------------------


async def get_trend_pulse(extra_subreddits: list[str] | None = None) -> TrendPulse:
    """This week's top posts across the configured subreddits (+ ``extra_subreddits``)."""
    subs = _subreddits(extra_subreddits)
    if not subs:
        return TrendPulse()
    key = "pulse:" + "+".join(sorted(s.lower() for s in subs))
    if (hit := _cached(key)) is not None:
        return TrendPulse(posts=hit)

    async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
        if _has_keys():
            results = await asyncio.gather(
                *(_oauth_get(client, f"/r/{sub}/top", {"t": "week", "limit": 25}) for sub in subs)
            )
            posts = [p for batch in results for p in batch]
        else:
            _log_once("rss", "Reddit keys missing; using public RSS feeds (no scores or NSFW flags)")
            posts = await _rss_get(client, f"/r/{'+'.join(subs)}/top/", {"t": "week", "limit": 50})

    posts.sort(key=lambda p: p.score, reverse=True)
    # An empty result is not cached for the full hour: it is usually a rate limit.
    if posts:
        _store(key, posts)
    return TrendPulse(posts=posts)


async def search_reddit_images(query: str, limit: int = 6) -> list[TrendPost]:
    """Recent image posts matching ``query`` in the configured subreddits."""
    query = " ".join((query or "").split())[:120]
    subs = _subreddits(None)
    if not query or not subs or limit <= 0:
        return []
    key = f"search:{query.lower()}:{limit}"
    if (hit := _cached(key)) is not None:
        return hit

    path = f"/r/{'+'.join(subs)}/search"
    params = {"q": query, "restrict_sr": 1, "t": "month", "sort": "top", "limit": 25}
    async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
        if _has_keys():
            posts = await _oauth_get(client, path, {**params, "type": "link"})
        else:
            posts = await _rss_get(client, path + "/", params)

    images = [p for p in posts if p.image_url][:limit]
    if images:
        _store(key, images)
    return images
