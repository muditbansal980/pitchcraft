"""Deep research, part 1: find and fetch pages (Owner: Agent D).

- Firecrawl v2 (``/map``, ``/scrape``, ``/search``) with ``settings.firecrawl_api_key`` or
  keyless. Keyless Firecrawl serves only ``/scrape`` + ``/search``, capped per IP per day,
  and refuses "suspicious" IPs (403); ``/map`` needs a key (401). A process-wide circuit
  breaker stops calling an endpoint after an auth/quota refusal (hours) or a 429 (minutes),
  so one refusal never eats the time budget.
- Fallback scraper: httpx (link-preview UA first, desktop UA second, as some brand sites
  block one or the other) + ``trafilatura`` main-content Markdown.
- Fallback discovery: robots.txt / sitemap.xml (indexes and .gz) + homepage links.
- External articles: Firecrawl search, else Tavily search with ``include_raw_content``
  (full article text in one call).
- Disk cache per brand + URL (and per search query) for ``settings.research_cache_hours``
  under ``{chroma_persist_dir}/research_cache/<brand>/`` (not under /media, which is served publicly).
"""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import html as htmllib
import json
import logging
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse, urlunparse

import httpx

from app.core.config import settings
from app.services.visuals_http import BROWSER_UA

logger = logging.getLogger(__name__)

FIRECRAWL_BASE = "https://api.firecrawl.dev/v2"
DESKTOP_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36 "
              "PitchCraft/0.2")
USER_AGENTS = (BROWSER_UA, DESKTOP_UA)

MAX_HTML_BYTES = 3_000_000
MAX_PDF_BYTES = 5 * 1024 * 1024
MAX_PAGE_CHARS = 24_000  # per page kept for chunking (~6K tokens)
MIN_WORDS_SITE = 200  # brand pages (menus, product ranges) are often short but dense
MIN_WORDS_EXTERNAL = 300
PAGE_TIMEOUT = 14.0
FIRECRAWL_SCRAPE_TIMEOUT_MS = 15_000

# Social / app-store pages: login walls or no article text.
_UNSCRAPABLE_HOSTS = (
    "instagram.com", "facebook.com", "x.com", "twitter.com", "tiktok.com", "youtube.com", "youtu.be",
    "linkedin.com", "play.google.com", "apps.apple.com", "pinterest.", "threads.net", "snapchat.com",
)


@dataclass
class Page:
    url: str
    title: str
    markdown: str
    kind: str  # site | blog | news | campaign | social | other
    via: str  # firecrawl | fallback | tavily
    query: str = ""  # search query that found it (external pages)
    published: str = ""

    @property
    def words(self) -> int:
        return len(self.markdown.split())


@dataclass
class FetchStats:
    firecrawl: dict[str, str] = field(default_factory=dict)  # endpoint -> last outcome
    failures: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- disk cache


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60] or "brand"


def _digest(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:20]


def cache_dir(brand: str) -> Path:
    return Path(settings.chroma_persist_dir) / "research_cache" / _slug(brand)


def cache_get(brand: str, key: str) -> Any | None:
    path = cache_dir(brand) / f"{_digest(key)}.json"
    try:
        if time.time() - path.stat().st_mtime > settings.research_cache_hours * 3600:
            return None
        return json.loads(path.read_text(encoding="utf-8"))["value"]
    except (OSError, ValueError, KeyError):
        return None


def cache_put(brand: str, key: str, value: Any) -> None:
    path = cache_dir(brand) / f"{_digest(key)}.json"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"key": key, "value": value}, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        logger.info("research cache write failed: %s", exc)


# --------------------------------------------------------------------------- Firecrawl


class _Firecrawl:
    """Firecrawl v2 client with a process-wide circuit breaker per endpoint."""

    def __init__(self) -> None:
        self.blocked_until: dict[str, float] = {}
        self.reason: dict[str, str] = {}

    @property
    def keyed(self) -> bool:
        return bool(settings.firecrawl_api_key)

    def available(self, endpoint: str) -> bool:
        if endpoint == "map" and not self.keyed:
            return False  # not part of the keyless tier
        return time.time() >= self.blocked_until.get(endpoint, 0.0)

    def _block(self, endpoints: tuple[str, ...], seconds: float, reason: str) -> None:
        until = time.time() + seconds
        if all(self.blocked_until.get(ep, 0.0) >= until - 60 for ep in endpoints):
            return  # parallel requests racing into the same refusal
        for ep in endpoints:
            if self.blocked_until.get(ep, 0.0) < until:
                self.blocked_until[ep] = until
                self.reason[ep] = reason
        logger.info("firecrawl %s disabled for %.0f min: %s", "/".join(endpoints), seconds / 60, reason)

    async def post(self, client: httpx.AsyncClient, endpoint: str, body: dict[str, Any], timeout: float,
                   stats: FetchStats | None = None) -> dict[str, Any] | None:
        if not self.available(endpoint):
            return None
        headers = {"Content-Type": "application/json"}
        if self.keyed:
            headers["Authorization"] = f"Bearer {settings.firecrawl_api_key}"
        try:
            resp = await client.post(f"{FIRECRAWL_BASE}/{endpoint}", json=body, headers=headers, timeout=timeout)
        except (httpx.HTTPError, asyncio.TimeoutError) as exc:
            if stats is not None:
                stats.firecrawl[endpoint] = f"error {type(exc).__name__}"
            return None
        code = resp.status_code
        if stats is not None:
            stats.firecrawl[endpoint] = str(code)
        if code == 200:
            try:
                data = resp.json()
            except ValueError:
                return None
            return data if data.get("success", True) else None
        msg = resp.text[:160].replace("\n", " ")
        everything = ("scrape", "search", "map")
        if code == 429:
            retry = resp.headers.get("retry-after")
            wait = float(retry) if retry and retry.isdigit() else 0.0
            # Keyless caps are per day; keyed caps per minute.
            self._block(everything, max(wait, 120.0 if self.keyed else 1800.0), f"429 {msg}")
        elif code in (401, 403) and not self.keyed and "endpoint is not supported" in msg:
            self._block((endpoint,), 6 * 3600, f"{code} {msg}")
        elif code in (401, 402, 403):
            self._block(everything, 6 * 3600, f"{code} {msg}")
        elif code >= 500:
            self._block((endpoint,), 60.0, f"{code} {msg}")
        # 400/404/408 etc. are page-specific: no breaker.
        return None


firecrawl = _Firecrawl()


# --------------------------------------------------------------------------- URL helpers


def normalize_url(url: str) -> str:
    p = urlparse(url.strip())
    if p.scheme not in ("http", "https") or not p.netloc:
        return ""
    path = re.sub(r"/{2,}", "/", p.path or "/")
    if len(path) > 1:
        path = path.rstrip("/")
    return urlunparse((p.scheme, p.netloc.lower(), path, "", p.query if _keep_query(p.query) else "", ""))


def _keep_query(query: str) -> bool:
    return bool(query) and not re.search(r"utm_|ref=|fbclid|gclid|sort=|page=|filter", query)


def host_of(url: str) -> str:
    return urlparse(url).netloc.lower().split(":")[0].removeprefix("www.")


def registrable(host: str) -> str:
    parts = host.lower().split(":")[0].removeprefix("www.").split(".")
    n = 3 if len(parts) >= 3 and parts[-2] in ("co", "com", "net", "org", "gov", "ac") and len(parts[-1]) == 2 else 2
    return ".".join(parts[-n:])


def unscrapable(url: str) -> bool:
    h = host_of(url)
    for s in _UNSCRAPABLE_HOSTS:
        if s.endswith("."):  # "pinterest." = any TLD
            if h.startswith(s) or f".{s}" in h:
                return True
        elif h == s or h.endswith("." + s):
            return True
    return False


_KEYWORDS: dict[str, tuple[float, str]] = {
    # word -> (weight, category)
    "about": (3.0, "about"), "about-us": (3.0, "about"), "our-story": (3.0, "about"), "story": (2.0, "about"),
    "who-we-are": (3.0, "about"), "company": (1.5, "about"), "brand": (1.2, "about"), "heritage": (2.0, "about"),
    "products": (3.0, "products"), "product": (2.0, "products"), "menu": (3.0, "products"),
    "collections": (2.6, "products"), "collection": (2.2, "products"), "shop": (2.0, "products"),
    "range": (2.0, "products"), "flavours": (2.5, "products"), "flavors": (2.5, "products"),
    "features": (2.0, "products"), "services": (1.6, "products"), "categories": (1.5, "products"),
    "blog": (2.6, "blog"), "blogs": (2.6, "blog"), "stories": (2.2, "blog"), "journal": (2.0, "blog"),
    "articles": (2.0, "blog"), "magazine": (1.8, "blog"),
    "news": (2.6, "news"), "press": (2.6, "news"), "newsroom": (3.0, "news"), "media": (1.4, "news"),
    "press-release": (3.0, "news"), "press-releases": (3.0, "news"), "announcements": (2.2, "news"),
    "investor": (1.8, "news"), "investors": (1.8, "news"), "annual-report": (1.6, "news"),
    "campaign": (3.0, "campaign"), "campaigns": (3.0, "campaign"), "offers": (2.0, "campaign"),
    "offer": (1.6, "campaign"), "launch": (2.6, "campaign"), "launches": (2.6, "campaign"),
    "new": (0.8, "campaign"), "sale": (1.4, "campaign"), "festive": (1.6, "campaign"),
    "community": (1.8, "about"), "sustainability": (1.2, "about"), "impact": (1.0, "about"),
}
_JUNK_RE = re.compile(
    r"(login|log-in|signin|sign-in|signup|sign-up|register|account|my-?orders|cart|checkout|basket|wishlist|"
    r"privacy|terms|conditions|legal|cookie|policy|policies|disclaimer|careers?|jobs|vacanc|refund|returns?|"
    r"shipping|track-?order|faq|help|support|contact|sitemap|search|feed|rss|wp-json|wp-admin|xmlrpc|"
    r"upsell|recurpay|subscribe|test|tax|coupon|gift-?card|/all(-\d+)?$|/tag/|/tags/|/author/|/page/\d|/amp/?$|\.(jpe?g|png|gif|webp|svg|css|js|ico|xml|zip|mp4|mp3|json)$)",
    re.IGNORECASE,
)
_LOCALE_RE = re.compile(r"^/([a-z]{2})(?:[-_]([a-z]{2}))?(/|$)", re.IGNORECASE)
_GOOD_LOCALES = {"en", "in", "en-in", "in-en", "hi"}


def score_url(url: str, site: str, brand_token: str = "", lastmod: str = "") -> tuple[float, str]:
    """(score, category) for a same-site URL; score <= 0 means skip."""
    p = urlparse(url)
    path = p.path.lower()
    if _JUNK_RE.search(path) or _JUNK_RE.search(p.query or ""):
        return -1.0, ""
    m = _LOCALE_RE.match(path)
    site_path = urlparse(site).path.lower()
    if m:
        loc = (m.group(1) + ("-" + m.group(2) if m.group(2) else "")).lower()
        if loc not in _GOOD_LOCALES and not site_path.startswith(f"/{m.group(0).strip('/')}"):
            return -1.0, ""  # /fr/, /de-de/ ... duplicates of the Indian/English page
    if path.rstrip("/") == site_path.rstrip("/"):
        return 2.5, "home"
    segs = [s for s in path.split("/") if s]
    score, cat = 0.0, ""
    for seg in segs:
        for word in [seg, *seg.split("-")]:
            w = _KEYWORDS.get(word)
            if w and w[0] > score:
                score, cat = w
    if brand_token and brand_token in path.replace("-", ""):
        score += 0.8
    last = segs[-1] if segs else ""
    if cat in ("blog", "news", "campaign") and last.count("-") >= 3:
        score += 1.2  # an article, not the index page
    if not score:
        return 0.0, ""
    score -= max(0, len(segs) - 3) * 0.4
    if re.search(r"\d{5,}", last):
        score -= 0.8  # product/listing ids
    if lastmod:
        try:
            when = datetime.fromisoformat(lastmod.replace("Z", "+00:00")[:25])
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            age = datetime.now(timezone.utc) - when
            if age < timedelta(days=200):
                score += 0.8
            elif age > timedelta(days=900):
                score -= 0.6
        except (ValueError, TypeError):
            pass
    return score, cat


def pick_site_urls(candidates: dict[str, str], site: str, brand: str, n: int, nav: set[str] | None = None) -> list[str]:
    """Top ``n`` same-site URLs, round-robin across categories so blogs don't crowd out products.

    URLs linked from the homepage (``nav``) are curated by the brand and get a small bonus.
    """
    nav = nav or set()
    token = re.sub(r"[^a-z0-9]", "", brand.lower())
    site_host = registrable(host_of(site))
    site_path = urlparse(site).path.rstrip("/").lower()
    by_cat: dict[str, list[tuple[float, str]]] = {}
    for url, lastmod in candidates.items():
        h = host_of(url)
        if registrable(h) != site_host:
            continue
        path = urlparse(url).path.lower()
        if site_path and not path.startswith(site_path) and token not in path.replace("-", ""):
            continue  # sub-brand page on a parent site: stay inside the brand's section
        sc, cat = score_url(url, site, token, lastmod)
        if sc > 0:
            sc += 0.6 if url in nav else 0.0
            by_cat.setdefault(cat, []).append((sc, url))
    for lst in by_cat.values():
        lst.sort(reverse=True)
    order = sorted(by_cat, key=lambda c: (c != "home", -by_cat[c][0][0]))
    picked: list[str] = []
    while len(picked) < n and any(by_cat.values()):
        for cat in order:
            if by_cat[cat] and len(picked) < n:
                picked.append(by_cat[cat].pop(0)[1])
    return picked


# --------------------------------------------------------------------------- fallback fetch


async def _get(client: httpx.AsyncClient, url: str, cap: int, timeout: float) -> tuple[int, str, bytes, str]:
    """(status, content-type, body, final url); tries each UA until one gets a 2xx."""
    last = (0, "", b"", url)
    for ua in USER_AGENTS:
        try:
            async with client.stream("GET", url, timeout=timeout, headers={"User-Agent": ua}) as resp:
                ctype = resp.headers.get("content-type", "").lower()
                declared = resp.headers.get("content-length")
                if resp.status_code >= 400:
                    last = (resp.status_code, ctype, b"", str(resp.url))
                    continue
                if declared and declared.isdigit() and int(declared) > cap:
                    return resp.status_code, ctype, b"", str(resp.url)
                chunks, total = [], 0
                async for chunk in resp.aiter_bytes():
                    total += len(chunk)
                    if total > cap:
                        break
                    chunks.append(chunk)
                return resp.status_code, ctype, b"".join(chunks), str(resp.url)
        except (httpx.HTTPError, ValueError, asyncio.TimeoutError) as exc:
            last = (0, type(exc).__name__, b"", url)
    return last


_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_DATE_META_RE = re.compile(
    r"""<meta[^>]+(?:property|name|itemprop)=["'](?:article:published_time|datePublished|pubdate|date)["'][^>]*"""
    r"""content=["']([^"']{8,40})["']""",
    re.IGNORECASE,
)


def extract_markdown(raw_html: str, url: str) -> tuple[str, str, str]:
    """(markdown, title, published) of the page's main content via trafilatura."""
    import trafilatura

    text = trafilatura.extract(
        raw_html, url=url, output_format="markdown", include_links=False, include_images=False,
        include_tables=True, include_comments=False, favor_recall=True,
    ) or ""
    m = _TITLE_RE.search(raw_html)
    title = " ".join(htmllib.unescape(m.group(1)).split())[:160] if m else ""
    d = _DATE_META_RE.search(raw_html)
    return text, title, (d.group(1)[:10] if d else "")


async def fallback_scrape(client: httpx.AsyncClient, url: str) -> tuple[str, str, str] | None:
    status, ctype, body, final = await _get(client, url, MAX_HTML_BYTES, PAGE_TIMEOUT)
    if not body or status >= 400:
        return None
    if "html" not in ctype and not body.lstrip()[:200].lower().startswith((b"<!doctype html", b"<html")):
        return None  # PDFs and other binaries: only Firecrawl parses them
    raw = body.decode("utf-8", errors="replace")
    return await asyncio.to_thread(extract_markdown, raw, final)


async def firecrawl_scrape(client: httpx.AsyncClient, url: str, stats: FetchStats) -> tuple[str, str, str] | None:
    body = {
        "url": url,
        "formats": ["markdown"],
        "onlyMainContent": True,
        "timeout": FIRECRAWL_SCRAPE_TIMEOUT_MS,
        "maxAge": settings.research_cache_hours * 3600 * 1000,
        "blockAds": True,
        "removeBase64Images": True,
        "location": {"country": "IN", "languages": ["en-IN"]},
    }
    data = await firecrawl.post(client, "scrape", body, FIRECRAWL_SCRAPE_TIMEOUT_MS / 1000 + 5, stats)
    if not data:
        return None
    d = data.get("data") or {}
    md = d.get("markdown") or ""
    meta = d.get("metadata") or {}
    if (meta.get("statusCode") or 200) >= 400 or not md:
        return None
    pub = str(meta.get("publishedTime") or meta.get("article:published_time") or "")[:10]
    return md, str(meta.get("title") or meta.get("ogTitle") or "")[:160], pub


async def _pdf_too_big(client: httpx.AsyncClient, url: str) -> bool:
    try:
        r = await client.head(url, timeout=6, headers={"User-Agent": BROWSER_UA})
        size = r.headers.get("content-length")
        return bool(size and size.isdigit() and int(size) > MAX_PDF_BYTES)
    except httpx.HTTPError:
        return True


_MD_NOISE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)|\[([^\]]*)\]\([^)]*\)")


def clean_markdown(md: str) -> str:
    md = _MD_NOISE_RE.sub(lambda m: m.group(1) or "", md)  # drop images, keep link text
    lines, seen = [], set()
    for line in md.splitlines():
        s = line.strip()
        key = s.lower()
        if len(s) > 40 and key in seen:
            continue  # repeated nav/footer blocks
        seen.add(key)
        lines.append(line.rstrip())
    out = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    out = re.sub(r"`(?=\s?\d)", "₹", out)  # Indian annual-report PDFs render the rupee sign as a backtick
    return out[:MAX_PAGE_CHARS]


def mentions(brand: str, text: str) -> int:
    """How often the brand name occurs ("Sleepy Owl", "SleepyOwl", "sleepy-owl")."""
    words = [re.escape(w) for w in brand.split() if w]
    if not words:
        return 0
    return len(re.findall(r"(?<![a-z0-9])" + r"[\s\-]?".join(words) + r"(?![a-z0-9])", text, re.IGNORECASE))


def real_words(md: str) -> int:
    return sum(1 for w in re.findall(r"[A-Za-zऀ-ॿ][\w'’-]*", md) if len(w) > 1)


async def scrape_page(client: httpx.AsyncClient, brand: str, url: str, kind: str, stats: FetchStats,
                      min_words: int) -> Page | None:
    """One page: cache -> Firecrawl -> fallback. None when unusable."""
    cached = cache_get(brand, "page:" + url)
    if cached:
        return Page(**cached) if cached.get("markdown") else None
    if url.lower().endswith(".pdf") and await _pdf_too_big(client, url):
        return None
    got, via = None, "firecrawl"
    if firecrawl.available("scrape"):
        got = await firecrawl_scrape(client, url, stats)
    if not got:
        via = "fallback"
        got = await fallback_scrape(client, url)
    if not got:
        stats.failures.append(url)
        return None
    md, title, pub = got
    md = clean_markdown(md)
    if real_words(md) < min_words:
        cache_put(brand, "page:" + url, {"url": url, "markdown": ""})  # remember thin pages too
        return None
    page = Page(url=url, title=title or url, markdown=md, kind=kind, via=via, published=pub)
    cache_put(brand, "page:" + url, asdict(page))
    return page


# --------------------------------------------------------------------------- discovery


_LOC_RE = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>(?:\s*<lastmod>\s*([^<\s]+)\s*</lastmod>)?", re.IGNORECASE)
_HREF_RE = re.compile(r"""<a\b[^>]*\bhref\s*=\s*["']([^"'#]+)""", re.IGNORECASE)
_SITEMAP_GOOD = re.compile(r"page|post|blog|news|press|story|stories|campaign|collection|categor|about|main|static|"
                           r"article|en-in|default", re.IGNORECASE)
_SITEMAP_BAD = re.compile(r"restaurant|product-?\d|sku|image|video|author|tag|user|city|location|store|dish|"
                          r"recipe|brand-?\d|listing", re.IGNORECASE)


async def _sitemap_urls(client: httpx.AsyncClient, site: str, budget: float) -> dict[str, str]:
    """URL -> lastmod from robots.txt sitemaps / sitemap.xml (indexes followed, max ~6 files)."""
    root = f"{urlparse(site).scheme}://{urlparse(site).netloc}"
    deadline = time.monotonic() + budget
    queue: list[str] = []
    status, _, body, _ = await _get(client, root + "/robots.txt", 200_000, 6)
    if body and status < 400:
        queue += re.findall(r"(?im)^\s*sitemap:\s*(\S+)", body.decode("utf-8", "replace"))
    queue += [root + "/sitemap.xml", root + "/sitemap_index.xml"]
    seen: set[str] = set()
    out: dict[str, str] = {}
    files = 0
    while queue and files < 6 and time.monotonic() < deadline and len(out) < 4000:
        sm = queue.pop(0)
        if sm in seen:
            continue
        seen.add(sm)
        status, _, body, _ = await _get(client, sm, 8_000_000, min(8.0, max(1.0, deadline - time.monotonic())))
        if not body or status >= 400:
            continue
        files += 1
        if body[:2] == b"\x1f\x8b":
            try:
                body = gzip.decompress(body)
            except OSError:
                continue
        text = body.decode("utf-8", "replace")
        locs = _LOC_RE.findall(text)
        if "<sitemapindex" in text[:2000].lower():
            children = [u for u, _ in locs]
            good = [u for u in children if _SITEMAP_GOOD.search(u.rsplit("/", 1)[-1]) and not _SITEMAP_BAD.search(u)]
            rest = [u for u in children if u not in good and not _SITEMAP_BAD.search(u.rsplit("/", 1)[-1])]
            queue = good[:4] + rest[:2] + queue
            continue
        for u, lastmod in locs:
            nu = normalize_url(htmllib.unescape(u))
            if nu:
                out.setdefault(nu, lastmod)
    return out


async def _homepage_links(client: httpx.AsyncClient, site: str) -> dict[str, str]:
    status, ctype, body, final = await _get(client, site, MAX_HTML_BYTES, 10)
    if not body or "html" not in ctype:
        return {}
    page = body.decode("utf-8", "replace")
    out: dict[str, str] = {}
    for href in _HREF_RE.findall(page):
        nu = normalize_url(urljoin(final, htmllib.unescape(href)))
        if nu:
            out.setdefault(nu, "")
    return out


async def _firecrawl_map(client: httpx.AsyncClient, site: str, stats: FetchStats) -> dict[str, str]:
    body = {"url": site, "limit": 500, "sitemap": "include", "includeSubdomains": True, "ignoreQueryParameters": True,
            "location": {"country": "IN", "languages": ["en-IN"]}, "timeout": 15000}
    data = await firecrawl.post(client, "map", body, 20, stats)
    out: dict[str, str] = {}
    for link in (data or {}).get("links") or []:
        url = link.get("url") if isinstance(link, dict) else link
        nu = normalize_url(url or "")
        if nu:
            out.setdefault(nu, "")
    return out


async def discover_site_urls(client: httpx.AsyncClient, brand: str, site: str, n: int,
                             stats: FetchStats) -> tuple[list[str], str]:
    """Top ``n`` brand-site URLs and how they were found ("firecrawl-map" / "sitemap+links")."""
    cached = cache_get(brand, f"discover:{site}:{n}")
    if cached:
        return cached["urls"], cached["via"]
    candidates: dict[str, str] = {}
    nav: set[str] = set()
    via = "sitemap+links"
    if firecrawl.available("map"):
        candidates = await _firecrawl_map(client, site, stats)
        if candidates:
            via = "firecrawl-map"
    if len(candidates) < 10:
        sitemap, links = await asyncio.gather(_sitemap_urls(client, site, 14.0), _homepage_links(client, site))
        nav = set(links)
        for src in (links, sitemap):
            for u, lm in src.items():
                candidates.setdefault(u, lm)
    home = normalize_url(site)
    if home:
        candidates.setdefault(home, "")
    urls = pick_site_urls(candidates, site, brand, n, nav)
    logger.info("deep research %s: %d site candidates via %s -> %d picked", brand, len(candidates), via, len(urls))
    if urls:
        cache_put(brand, f"discover:{site}:{n}", {"urls": urls, "via": via})
    return urls, via


# --------------------------------------------------------------------------- external search


@dataclass
class SearchHit:
    url: str
    title: str
    snippet: str
    markdown: str  # full article text when the search engine returned it
    via: str  # firecrawl | tavily
    query: str
    kind: str
    score: float
    published: str = ""


def _kind_for(url: str, query_kind: str) -> str:
    h = host_of(url)
    if "reddit.com" in h or "twitter" in h or h == "x.com":
        return "social"
    return query_kind


async def _firecrawl_search(client: httpx.AsyncClient, query: str, news: bool, limit: int,
                            stats: FetchStats, domain: str | None = None) -> list[dict[str, Any]] | None:
    body: dict[str, Any] = {
        "query": f"{query} site:{domain}" if domain else query, "limit": limit, "sources": ["news" if news else "web"], "country": "IN", "location": "India",
        "timeout": 25000, "scrapeOptions": {"formats": ["markdown"], "onlyMainContent": True},
    }
    if news:
        body["tbs"] = "qdr:m"
    data = await firecrawl.post(client, "search", body, 30, stats)
    if data is None:
        return None
    d = data.get("data") or {}
    items = (d.get("news") if news else d.get("web")) or []
    return [
        {"url": it.get("url", ""), "title": it.get("title", ""), "content": it.get("description") or it.get("snippet", ""),
         "raw_content": it.get("markdown") or "", "score": 1.0 - i * 0.1, "published_date": it.get("date", "")}
        for i, it in enumerate(items) if isinstance(it, dict)
    ]


async def _tavily_search(query: str, news: bool, limit: int, domain: str | None = None) -> list[dict[str, Any]]:
    if not settings.tavily_api_key:
        return []
    from tavily import AsyncTavilyClient

    params: dict[str, Any] = {"max_results": limit, "search_depth": "basic", "include_raw_content": "markdown",
                              "topic": "news" if news else "general"}
    if domain:
        params["include_domains"] = [domain]
    if news:
        params["time_range"] = "month"
    elif settings.research_country:
        params["country"] = settings.research_country
    res = await asyncio.wait_for(AsyncTavilyClient(api_key=settings.tavily_api_key).search(query, **params), 25)
    return list(res.get("results") or [])


async def search_external(client: httpx.AsyncClient, brand: str, queries: list[tuple[str, str, bool, str | None]],
                          stats: FetchStats, per_query: int = 4) -> list[SearchHit]:
    """Run (query, kind, news, only-this-domain) searches; Firecrawl search first (keyless/keyed), Tavily otherwise."""

    async def one(query: str, kind: str, news: bool, domain: str | None) -> list[SearchHit]:
        key = f"search:{query}:{news}:{domain or ''}"
        cached = cache_get(brand, key)
        via = "tavily"
        if cached is None:
            results = None
            if firecrawl.available("search"):
                results = await _firecrawl_search(client, query, news, per_query, stats, domain)
                via = "firecrawl"
            if not results:
                via = "tavily"
                try:
                    results = await _tavily_search(query, news, per_query, domain)
                except Exception as exc:  # noqa: BLE001 - search is best-effort
                    logger.info("deep research search failed (%s): %s", query, exc)
                    results = []
            cached = {"via": via, "results": results}
            if results:
                cache_put(brand, key, cached)
        out = []
        for r in cached.get("results") or []:
            url = normalize_url(r.get("url") or "")
            if not url or unscrapable(url):
                continue
            out.append(SearchHit(
                url=url, title=(r.get("title") or "")[:160], snippet=(r.get("content") or "")[:600],
                markdown=r.get("raw_content") or "", via=cached.get("via", via), query=query,
                kind=_kind_for(url, kind), score=float(r.get("score") or 0.0),
                published=str(r.get("published_date") or "")[:10],
            ))
        return out

    groups = await asyncio.gather(*(one(q, k, n, d) for q, k, n, d in queries), return_exceptions=True)
    hits: list[SearchHit] = []
    for g in groups:
        if isinstance(g, list):
            hits.extend(g)
    return hits
