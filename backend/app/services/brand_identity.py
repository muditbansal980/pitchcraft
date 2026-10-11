"""Brand identity probe (Agent B): the brand's own website -> colours, fonts, logo, products.

``probe_brand_identity(brand, research_text)``:
1. Finds the official website (Tavily "<brand> official website"; the result whose
   domain contains the brand name wins, .in/.com preferred; marketplaces, socials
   and wikis are skipped), with a guessed ``<brand>.com``/``.in`` fallback.
2. Reads the home page and up to 4 linked stylesheets (1.5 MB / ~10 s total):
   ``theme-color``, brand CSS custom properties (``--primary``, ``--brand-*``) and
   the non-neutral colours used most, weighted toward buttons/links/headers; the
   page's background mode; ``font-family`` / Google Fonts / ``@font-face`` names;
   ``og:image`` and large hero images; the logo (``img``/``svg`` marked "logo",
   apple-touch-icon, icon; SVGs rasterised with cairosvg).
3. In parallel, a Tavily image search for "<brand> logo" whose dominant colours
   confirm (or replace) the site colours.
4. Maps the fonts to Canva/Google fonts (``brand_identity_fonts``); if no font is
   found, the vision role classifies the logo lettering; then category defaults.
5. Collects product/object words (navigation menu, headings, meta description,
   research text) as motifs, and stores product images as hero candidates.
6. Detects the campaign season from the research text and today's date.

Everything is best-effort: any step can fail and the identity just has fewer
fields. Never raises.
"""

import asyncio
import colorsys
import html as htmllib
import io
import logging
import re
import time
from collections import Counter
from datetime import date
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
from PIL import Image
from pydantic import BaseModel, Field
from tavily import AsyncTavilyClient

from app.core.config import settings
from app.services import llm_router
from app.services.brand_identity_fonts import CATEGORY_PAIRS, clean_family, is_generic, map_fonts
from app.services.brand_identity_products import find_products, jsonld_names
from app.services.brand_identity_season import detect_season_detail
from app.services.media import image_digest, save_image, save_png
from app.services.visuals_http import BROWSER_UA, fetch_image

logger = logging.getLogger(__name__)

PROBE_TIMEOUT = 18.0
SITE_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36 "
           "PitchCraft/0.2")
SITE_BUDGET_S = 10.0
MAX_CSS_FILES = 4
MAX_TOTAL_BYTES = 1_500_000

_SKIP_DOMAINS = (
    "wikipedia.org", "linkedin.com", "instagram.com", "facebook.com", "twitter.com", "x.com", "youtube.com",
    "amazon.", "flipkart.com", "crunchbase.com", "zaubacorp.com", "glassdoor", "ambitionbox.com", "quora.com",
    "reddit.com", "play.google.com", "apps.apple.com", "tracxn.com", "economictimes", "livemint.com",
    "moneycontrol.com", "business-standard.com", "thehindu", "ndtv.com", "indiatimes.com", "forbes",
    "bloomberg.com", "reuters.com", "pitchbook.com", "owler.com", "bigbasket.com", "blinkit.com", "swiggy.com/instamart",
    "jiomart.com", "myntra.com", "ajio.com", "tiktok.com", "pinterest.", "medium.com", "yourstory.com", "inc42.com",
    "trustpilot", "mouthshut", "justdial", "indiamart", "tofler", "dnb.com", "zoominfo", "rocketreach", "similarweb",
)


class BrandIdentity(BaseModel):
    brand: str
    site_url: str | None = None
    colors: list[str] = Field(default_factory=list, description="Brand colours, most important first")
    color_sources: dict[str, str] = Field(default_factory=dict)
    bg_mode: str | None = Field(None, description='"dark" or "light" site background')
    site_bg: str | None = None
    raw_fonts: list[str] = Field(default_factory=list, description="Font families found on the site, most used first")
    heading_font: str = "Montserrat"
    body_font: str = "Montserrat"
    font_source: str = "default"
    logo_url: str | None = Field(None, description="/media/<sha1>.png")
    logo_source: str | None = None
    motifs: list[str] = Field(default_factory=list)
    products: list[str] = Field(default_factory=list,
                                description="Concrete products / sub-brands / app features / shows to name in the deck")
    product_images: list[str] = Field(default_factory=list, description="/media URLs of product/hero shots")
    title: str = ""
    description: str = ""
    season: str | None = None
    season_source: str | None = Field(None, description='"text" (brief/research) or "calendar"')
    timings: dict[str, float] = Field(default_factory=dict)
    # Raw site text kept so motifs can be recomputed with new research text from the cache.
    site_nav: list[str] = Field(default_factory=list, exclude=True)
    site_headings: list[str] = Field(default_factory=list, exclude=True)
    site_jsonld: list[str] = Field(default_factory=list, exclude=True)


IDENTITY_TTL = 3600.0
_cache: dict[str, tuple[float, BrandIdentity]] = {}


# --- colour helpers ---------------------------------------------------------

_HEX_RE = re.compile(r"#([0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{3,4})\b")
_RGB_RE = re.compile(r"rgba?\(\s*(\d{1,3})[\s,]+(\d{1,3})[\s,]+(\d{1,3})(?:[\s,/]+([\d.]+%?))?\s*\)")


def _norm_hex(v: str) -> str | None:
    v = v.strip().lstrip("#")
    if len(v) in (3, 4):
        if len(v) == 4 and v[3] in "0":
            return None  # fully transparent
        v = "".join(c * 2 for c in v[:3])
    elif len(v) == 8:
        if int(v[6:8], 16) < 128:
            return None  # mostly transparent
        v = v[:6]
    if len(v) != 6:
        return None
    try:
        int(v, 16)
    except ValueError:
        return None
    return "#" + v.upper()


def _colors_in(value: str) -> list[str]:
    out = []
    for m in _HEX_RE.finditer(value):
        h = _norm_hex(m.group(0))
        if h:
            out.append(h)
    for m in _RGB_RE.finditer(value):
        alpha = m.group(4)
        if alpha:
            a = float(alpha.rstrip("%")) / (100 if alpha.endswith("%") else 1)
            if a < 0.5:
                continue
        r, g, b = (min(255, int(x)) for x in m.groups()[:3])
        out.append(f"#{r:02X}{g:02X}{b:02X}")
    return out


def _hls(h: str) -> tuple[float, float, float]:
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (1, 3, 5))
    return colorsys.rgb_to_hls(r, g, b)


def _neutral(h: str) -> bool:
    _, lum, sat = _hls(h)
    return sat < 0.22 or lum > 0.94 or lum < 0.07


def _lum(h: str) -> float:
    def chan(c: float) -> float:
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (1, 3, 5))
    return 0.2126 * chan(r) + 0.7152 * chan(g) + 0.0722 * chan(b)


def _dist(a: str, b: str) -> float:
    ra, ga, ba = (int(a[i:i + 2], 16) for i in (1, 3, 5))
    rb, gb, bb = (int(b[i:i + 2], 16) for i in (1, 3, 5))
    return ((ra - rb) ** 2 * 0.3 + (ga - gb) ** 2 * 0.59 + (ba - bb) ** 2 * 0.11) ** 0.5


def _hue_gap(a: str, b: str) -> float:
    d = abs(_hls(a)[0] - _hls(b)[0])
    return min(d, 1 - d)


def _cluster(scores: dict[str, float], radius: float = 26.0) -> list[tuple[str, float]]:
    """Merge near-identical colours; the heaviest member names the cluster."""
    clusters: list[list[Any]] = []
    for color, w in sorted(scores.items(), key=lambda kv: -kv[1]):
        for cl in clusters:
            if _dist(color, cl[0]) < radius:
                cl[1] += w
                break
        else:
            clusters.append([color, w])
    return sorted(((c, w) for c, w in clusters), key=lambda cw: -cw[1])


def dominant_image_colors(data: bytes, count: int = 3) -> list[tuple[str, float]]:
    try:
        img = Image.open(io.BytesIO(data))
        img.thumbnail((96, 96))
        rgba = img.convert("RGBA")
    except Exception:  # noqa: BLE001 - any undecodable image is just skipped
        return []
    px = [p[:3] for p in rgba.getdata() if p[3] > 200]
    if len(px) < 40:
        return []
    flat = Image.new("RGB", (len(px), 1))
    flat.putdata(px)
    quant = flat.quantize(colors=8, method=Image.Quantize.MEDIANCUT)
    pal = quant.getpalette() or []
    out = []
    for n, idx in sorted(quant.getcolors() or [], reverse=True):
        h = "#" + "".join(f"{v:02X}" for v in pal[idx * 3: idx * 3 + 3])
        if not _neutral(h) and n / len(px) >= 0.03:
            out.append((h, n / len(px)))
    return out[:count]


# --- site discovery ---------------------------------------------------------


def _slug(brand: str) -> str:
    return re.sub(r"[^a-z0-9]", "", brand.lower())


def _brand_tokens(brand: str) -> list[str]:
    words = [re.sub(r"[^a-z0-9]", "", w) for w in brand.lower().replace("&", " ").split()]
    words = [w for w in words if len(w) >= 3 and w not in ("the", "and", "india", "official", "pvt", "ltd")]
    toks = [_slug(brand)] + words
    return [t for t in dict.fromkeys(toks) if t]


def _site_score(url: str, brand: str, rank: int) -> float:
    host = urlparse(url).netloc.lower().split(":")[0].removeprefix("www.")
    if not host or any(s in host or s in url for s in _SKIP_DOMAINS):
        return -1
    base = host.split(".")[0] if host.count(".") <= 1 else ".".join(host.split(".")[:-2]) or host
    score = 0.0
    toks = _brand_tokens(brand)
    flat = host.replace("-", "").replace(".", "")
    path_flat = re.sub(r"[^a-z0-9]", "", urlparse(url).path.lower())
    sub_brand = False
    if toks and toks[0] in flat:
        score += 6
    elif any(t in flat for t in toks[1:]):
        score += 3.5
    elif toks and toks[0] in path_flat and re.search(r"/brands?/", url.lower()):
        score += 2.5  # a sub-brand page on the parent company's site (e.g. coca-cola.com/in/en/brands/thums-up)
        sub_brand = True
    else:
        return -1  # an official site carries the brand name in its domain (or brand page path)
    if re.search(r"/(in|en-in|in-en)(/|$)", url.lower()):
        score += 0.6
    if host.endswith((".in", ".co.in", ".com", ".co")):
        score += 1.0
    if host.endswith(".in") or host.endswith(".co.in"):
        score += 0.5  # Indian campaigns: the .in site has the local look
    if base.replace("-", "") == toks[0]:
        score += 1.5
    path = urlparse(url).path.strip("/")
    score += 0.5 if not path and not sub_brand else 0
    return score - rank * 0.3


def _registrable(host: str) -> str:
    parts = host.lower().split(":")[0].removeprefix("www.").split(".")
    n = 3 if len(parts) >= 3 and parts[-2] in ("co", "com", "net", "org", "gov", "ac") and len(parts[-1]) == 2 else 2
    return ".".join(parts[-n:])


async def find_official_site(brand: str, client: httpx.AsyncClient) -> str | None:
    """The brand's own site: domains are scored across two searches (repeat hits, the brand
    name in the domain and the exact brand spelling in result titles all count)."""
    by_domain: dict[str, dict[str, Any]] = {}
    if settings.tavily_api_key:
        tav = AsyncTavilyClient(api_key=settings.tavily_api_key)
        queries = [f"{brand} official website", f"{brand} official website India"]
        results = await asyncio.gather(
            *(asyncio.wait_for(tav.search(q, max_results=8), timeout=10) for q in queries), return_exceptions=True)
        for res in results:
            if isinstance(res, BaseException):
                logger.info("identity: site search failed for %s: %s", brand, res)
                continue
            for rank, r in enumerate(res.get("results") or []):
                url = r.get("url") or ""
                sc = _site_score(url, brand, rank)
                if sc <= 0:
                    continue
                dom = _registrable(urlparse(url).netloc)
                entry = by_domain.setdefault(dom, {"score": 0.0, "hits": 0, "best": (sc, url)})
                entry["hits"] += 1
                entry["score"] = max(entry["score"], sc)
                if brand in (r.get("title") or ""):
                    entry["score"] += 0.6  # exact spelling ("boAt") in the page title
                if sc > entry["best"][0]:
                    entry["best"] = (sc, url)
    if by_domain:
        dom, entry = max(by_domain.items(), key=lambda kv: kv[1]["score"] + 1.2 * min(kv[1]["hits"], 4))
        url = entry["best"][1]
        p = urlparse(url)
        flat = dom.replace("-", "").replace(".", "")
        if not any(t in flat for t in _brand_tokens(brand) if len(t) >= 4 or t == _brand_tokens(brand)[0]):
            return url.split("#")[0]  # sub-brand page on a parent site: keep the path
        return f"{p.scheme}://www.{dom}/" if p.netloc.startswith("www.") or p.netloc.count(".") > dom.count(".") \
            else f"{p.scheme}://{dom}/"
    # Guess <brand>.in / <brand>.com.
    slug = _slug(brand)
    for host in (f"https://www.{slug}.in/", f"https://www.{slug}.com/", f"https://{slug}.com/"):
        try:
            r = await client.get(host, timeout=5)
            if r.status_code < 400 and "text/html" in r.headers.get("content-type", ""):
                return f"{r.url.scheme}://{r.url.host}/"
        except httpx.HTTPError:
            continue
    return None


# --- page parsing -----------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]+>")
_ATTR_RE = re.compile(r'([a-zA-Z_:][-a-zA-Z0-9_:.]*)\s*=\s*("([^"]*)"|\'([^\']*)\'|([^\s>]+))')


def _attrs(tag: str) -> dict[str, str]:
    out = {}
    for m in _ATTR_RE.finditer(tag):
        out[m.group(1).lower()] = htmllib.unescape(m.group(3) or m.group(4) or m.group(5) or "")
    return out


def _tags(page: str, name: str) -> list[dict[str, str]]:
    return [_attrs(m.group(0)) for m in re.finditer(rf"<{name}\b[^>]*>", page, re.IGNORECASE)]


def _text(fragment: str) -> str:
    return " ".join(htmllib.unescape(_TAG_RE.sub(" ", fragment)).split())


async def _get_capped(client: httpx.AsyncClient, url: str, cap: int, timeout: float,
                      headers: dict[str, str] | None = None) -> tuple[str, bytes] | None:
    try:
        async with client.stream("GET", url, timeout=timeout, headers=headers) as resp:
            if resp.status_code >= 400:
                return None
            chunks, total = [], 0
            async for chunk in resp.aiter_bytes():
                total += len(chunk)
                if total > cap:
                    break
                chunks.append(chunk)
            return resp.headers.get("content-type", ""), b"".join(chunks)
    except (httpx.HTTPError, ValueError):
        return None


_BRANDY_VAR = re.compile(r"--([a-z0-9-]*(?:primary|brand|accent|main|theme|secondary|highlight|cta)[a-z0-9-]*)\s*:\s*([^;}{]+)",
                         re.IGNORECASE)
_RULE_RE = re.compile(r"([^{}]{1,600})\{([^{}]*)\}")
_WEIGHTY_SEL = re.compile(r"\b(button|btn|cta|primary|brand|header|nav|navbar|link|hero|banner|badge|tag|chip|a)\b",
                          re.IGNORECASE)
_HEADING_SEL = re.compile(r"(^|[\s,>+~])(h1|h2|h3|\.?heading|\.?title|\.?headline|\.?display)\b", re.IGNORECASE)
_BODY_SEL = re.compile(r"(^|[\s,])(html|body|p|:root)\b", re.IGNORECASE)
_FONT_FAMILY_RE = re.compile(r"font-family\s*:\s*([^;}{]+)", re.IGNORECASE)
_FONT_FACE_RE = re.compile(r"@font-face\s*\{[^}]*?font-family\s*:\s*([^;}]+)", re.IGNORECASE)


_SYSTEM_FONTS = {"system-ui", "-apple-system", "blinkmacsystemfont", "segoe ui", "arial", "helvetica",
                 "helvetica neue", "roboto", "sf pro", "sf pro text", "sf pro display", "ubuntu", "cantarell",
                 "oxygen", "fira sans", "droid sans", "verdana", "tahoma", "consolas", "menlo", "monaco",
                 "courier new", "times new roman", "georgia", "liberation sans", "noto sans", "open sans"}


def _first_family(decl: str) -> str | None:
    for part in decl.split(","):
        name = part.strip().strip("'\"").strip()
        name = re.sub(r"\s*!important$", "", name)
        if name.startswith("var(") or ":" in name or ";" in name or len(name) > 40:
            return None
        if name and not is_generic(clean_family(name)) and "icomoon" not in name.lower():
            return name
    return None


def analyse_css(css: str, colors: Counter, fonts: dict[str, Counter], bg: Counter) -> None:
    """Accumulate colour weights, font usage (heading/body/any) and background colours from CSS text."""
    for m in _BRANDY_VAR.finditer(css):
        for c in _colors_in(m.group(2)):
            colors[c] += 8
    for face in _FONT_FACE_RE.finditer(css):
        fam = _first_family(face.group(1))
        if fam:
            fonts["face"][fam] += 1
    for m in _RULE_RE.finditer(css):
        sel, body = m.group(1).strip(), m.group(2)
        if sel.startswith("@font-face"):
            continue
        weighty = bool(_WEIGHTY_SEL.search(sel))
        for prop, val in re.findall(r"([a-z-]+)\s*:\s*([^;]+)", body, re.IGNORECASE):
            prop = prop.lower()
            if prop in ("color", "background", "background-color", "border-color", "fill", "border",
                        "border-bottom", "border-top", "outline-color", "stroke"):
                w = 3 if weighty else 1
                if prop.startswith("background") and weighty:
                    w = 4
                for c in _colors_in(val):
                    colors[c] += w
                if prop in ("background", "background-color") and _BODY_SEL.search(sel) and not weighty:
                    for c in _colors_in(val):
                        bg[c] += 1
            elif prop == "font-family":
                fam = _first_family(val)
                if not fam:
                    continue
                if _HEADING_SEL.search(sel):
                    fonts["heading"][fam] += 2
                if _BODY_SEL.search(sel):
                    fonts["body"][fam] += 2
                fonts["any"][fam] += 1


def _google_families(page: str) -> list[str]:
    fams = []
    for href in re.findall(r"fonts\.googleapis\.com/css2?\?([^\"'>\s]+)", page):
        for fam in re.findall(r"family=([^&:]+)", htmllib.unescape(href)):
            fams.append(fam.replace("+", " ").split(":")[0])
    return fams


_LOGO_HINT = re.compile(r"logo(?!ut|n|s?\b-?out)|wordmark|site-?icon|brand-?mark", re.IGNORECASE)


def _logo_candidates(page: str, base: str, brand: str = "", sub_brand: bool = False) -> list[tuple[str, str]]:
    """(url-or-inline-svg, source), best first.

    A logo carrying the brand name (src/alt/class) or sitting in the page header wins; "logo"
    images elsewhere are often press/partner strips ("as seen in Forbes"), so they come last
    and only when they name the brand.
    """
    toks = [t for t in _brand_tokens(brand) if len(t) >= 3] if brand else []
    header_spans = [(m.start(), m.end()) for m in re.finditer(r"<header\b.*?</header>", page,
                                                              re.IGNORECASE | re.DOTALL)]
    if not header_spans:  # no <header>: treat the top of the page as the header
        body = page.lower().find("<body")
        header_spans = [(max(0, body), max(0, body) + 25_000)]

    def in_header(pos: int) -> bool:
        return any(a <= pos <= b for a, b in header_spans)

    def names_brand(s: str) -> bool:
        flat = re.sub(r"[^a-z0-9]", "", s.lower())
        return any(t in flat for t in toks)

    scored: list[tuple[float, str, str]] = []
    for m in re.finditer(r"<svg\b[^>]*>.*?</svg>", page, re.IGNORECASE | re.DOTALL):
        svg = m.group(0)
        if len(svg) > 200_000:
            continue
        # Only the svg's own opening tag/title and its immediate wrapper count as context.
        opening = re.match(r"<svg\b[^>]*>(?:\s*<title>[^<]*</title>)?", svg, re.IGNORECASE)
        wrapper = page[max(0, m.start() - 220): m.start()]
        wrapper = wrapper[wrapper.rfind("<"):] if "<" in wrapper else wrapper
        ctx = (opening.group(0) if opening else "") + " " + wrapper
        head, named, hinted = in_header(m.start()), names_brand(ctx), bool(_LOGO_HINT.search(ctx))
        if not head or not (hinted or named) or (sub_brand and not named):
            continue
        vb = re.search(r'viewBox=["\']\s*[-\d.]+[\s,]+[-\d.]+[\s,]+([\d.]+)[\s,]+([\d.]+)', svg)
        vw, vh = (float(vb.group(1)), float(vb.group(2))) if vb else (0.0, 0.0)
        if vb and max(vw, vh) < 40:
            continue  # UI icon (arrows, carts, search)
        wide = vw > 1.8 * max(1e-3, vh)
        if named:
            score = 10 + (2 if wide else 0)
        elif wide and len(svg) > 1500:
            score = 7
        else:
            continue
        scored.append((score, svg, "inline svg"))
    for m in re.finditer(r"<img\b[^>]*>", page, re.IGNORECASE):
        a = _attrs(m.group(0))
        src = a.get("src") or a.get("data-src") or ""
        hint = " ".join(a.get(k, "") for k in ("src", "alt", "class", "id", "title"))
        if not src or src.startswith("data:") or not _LOGO_HINT.search(hint):
            continue
        head, named = in_header(m.start()), names_brand(hint)
        if (not head and not named) or (sub_brand and not named):
            continue  # press/partner strips, or the parent company's logo on a sub-brand page
        scored.append(((12 if named else 8) + (2 if head else 0), urljoin(base, src), "img logo"))
    scored.sort(key=lambda x: -x[0])
    out = [(ref, src) for _, ref, src in scored]
    icons = []
    for a in _tags(page, "link"):
        rel = a.get("rel", "").lower()
        if "icon" in rel and a.get("href"):
            size = max((int(x) for x in re.findall(r"(\d+)x\d+", a.get("sizes", ""))), default=0)
            if "apple-touch" in rel:
                size = max(size, 180)
            if a["href"].lower().endswith(".svg"):
                size = max(size, 256)
            icons.append((size, urljoin(base, a["href"])))
    if not sub_brand:
        for _, href in sorted(icons, reverse=True):
            out.append((href, "site icon"))
    return out


def _meta(page: str) -> dict[str, str]:
    out = {}
    for a in _tags(page, "meta"):
        key = (a.get("property") or a.get("name") or "").lower()
        if key and a.get("content"):
            out.setdefault(key, a["content"])
    return out


def _rasterise_svg(svg: bytes) -> bytes | None:
    try:
        import cairosvg
    except ImportError:
        logger.info("identity: cairosvg not installed; SVG logos skipped")
        return None
    try:
        return cairosvg.svg2png(bytestring=svg, output_width=600)
    except Exception as exc:  # noqa: BLE001 - malformed SVGs are common
        logger.debug("identity: SVG rasterise failed: %s", exc)
        return None


def _logo_ok(png: bytes) -> bool:
    try:
        with Image.open(io.BytesIO(png)) as img:
            if max(img.size) < 96:
                return False  # favicons are too small to print on a slide
            rgba = img.convert("RGBA")
            rgba.thumbnail((64, 64))
            alpha = rgba.split()[-1]
            opaque = sum(1 for a in alpha.getdata() if a > 40) / (rgba.width * rgba.height)
            lo, hi = rgba.convert("L").getextrema()
            return opaque > 0.03 and (hi - lo > 20 or opaque < 0.9)
    except Exception:  # noqa: BLE001
        return False


async def _fetch_logo(client: httpx.AsyncClient, cands: list[tuple[str, str]]) -> tuple[bytes, str] | None:
    for ref, source in cands[:6]:
        data: bytes | None = None
        if ref.lstrip().startswith("<svg"):
            svg = ref if "xmlns" in ref else ref.replace("<svg", '<svg xmlns="http://www.w3.org/2000/svg"', 1)
            data = _rasterise_svg(svg.encode())
        elif ref.lower().split("?")[0].endswith(".svg"):
            got = await _get_capped(client, ref, 400_000, 5)
            data = _rasterise_svg(got[1]) if got else None
        else:
            img = await fetch_image(client, ref, follow_og=False)
            data = img.data if img else None
            if data and ref.lower().split("?")[0].endswith(".ico"):
                try:
                    with Image.open(io.BytesIO(data)) as ico:
                        buf = io.BytesIO()
                        ico.convert("RGBA").save(buf, "PNG")
                        data = buf.getvalue()
                except Exception:  # noqa: BLE001
                    data = None
        if data and _logo_ok(data):
            return data, source
    return None


# --- motifs -----------------------------------------------------------------

_GENERIC_WORDS = set("""
home about contact login log sign signin signup register cart bag wishlist account profile menu search help
support faq faqs blog blogs careers career press media investors investor terms privacy policy cookie cookies
shop store stores offers offer deals deal sale new all view more explore discover learn read watch download app
apps get started subscribe newsletter track order orders return returns shipping delivery gift card cards
locate locator find us our we you your the and for with from that this to of in on at by is are be best top
official website site page india indian online buy now free trial premium plans plan pricing price prices
english hindi language select country region rewards points club member members membership elite
customer service services company brand brands team story stories news events event community follow
javascript disabled enable browser hiring process leadership sustainability investors careers jobs latest
vision purpose board directors launched known its bold history heritage mission values
popular trending featured international global worldwide corporate categories category collections collection shop-all
social facebook instagram twitter youtube linkedin copyright rights reserved pvt ltd limited inc
""".split())


def _phrases(texts: list[str]) -> Counter:
    c: Counter = Counter()
    for t in texts:
        words = [w for w in re.findall(r"[a-z][a-z'-]{2,}", t.lower())]
        for i, w in enumerate(words):
            if w not in _GENERIC_WORDS:
                c[w] += 1
                if i + 1 < len(words) and words[i + 1] not in _GENERIC_WORDS:
                    c[f"{w} {words[i + 1]}"] += 1.5
    return c


def extract_motifs(brand: str, nav: list[str], headings: list[str], meta_text: str, research_text: str,
                   limit: int = 6) -> list[str]:
    brand_words = set(_brand_tokens(brand)) | {w.lower() for w in brand.split()}
    site = _phrases(headings + [meta_text])
    research = _phrases([research_text[:20000]])
    scored: Counter = Counter()
    for item in nav:
        n = " ".join(item.lower().split())
        if 2 < len(n) <= 28 and len(n.split()) <= 3 and not set(n.split()) & _GENERIC_WORDS \
                and re.fullmatch(r"[a-z &'-]+", n):
            scored[n] += 3 + site[n] + research[n]
    for p, w in site.items():
        if w >= 1.5 or p in research:
            scored[p] += w + 0.5 * research[p]
    for p, w in research.most_common(40):
        if w >= 3:
            scored[p] += 0.4 * w
    out: list[str] = []
    for p, _ in scored.most_common(40):
        toks = set(p.split())
        if toks & brand_words or any(p in o or o in p for o in out):
            continue
        out.append(p)
        if len(out) >= limit:
            break
    return out


# --- main probe -------------------------------------------------------------


async def _logo_image_colors(brand: str, client: httpx.AsyncClient) -> list[tuple[str, float]]:
    """Dominant colours of the top "<brand> logo" image results (the original probe)."""
    if not settings.tavily_api_key:
        return []
    try:
        res = await asyncio.wait_for(
            AsyncTavilyClient(api_key=settings.tavily_api_key).search(f"{brand} logo", include_images=True,
                                                                     max_results=3),
            timeout=10,
        )
    except Exception:  # noqa: BLE001
        return []
    urls = [i.get("url") if isinstance(i, dict) else i for i in res.get("images") or []][:4]
    imgs = await asyncio.gather(*(fetch_image(client, u, follow_og=False) for u in urls if isinstance(u, str)))
    scores: dict[str, float] = {}
    for rank, img in enumerate(imgs):
        for h, share in dominant_image_colors(img.data) if img else []:
            scores[h] = scores.get(h, 0) + share / (1 + 0.5 * rank)
        if img and _LOGO_FALLBACK.get(brand) is None and _transparent_logo(img.data):
            _LOGO_FALLBACK[brand] = img.data  # a cut-out logo from search, used if the site has none
    return _cluster(scores, 30)


_LOGO_FALLBACK: dict[str, bytes] = {}


def _transparent_logo(data: bytes) -> bool:
    try:
        with Image.open(io.BytesIO(data)) as img:
            if img.mode not in ("RGBA", "LA", "P") or min(img.size) < 64:
                return False
            alpha = img.convert("RGBA").split()[-1]
            alpha.thumbnail((64, 64))
            vals = list(alpha.getdata())
            clear = sum(1 for a in vals if a < 30) / len(vals)
            return 0.15 < clear < 0.9 and _logo_ok(data)
    except Exception:  # noqa: BLE001
        return False


async def _read_site(site: str, client: httpx.AsyncClient, ident: BrandIdentity, research_text: str) -> None:
    started = time.monotonic()
    got = await _get_capped(client, site, MAX_TOTAL_BYTES, 5)
    if not got or "html" not in got[0].lower() or len(got[1]) < 2000:
        # Some sites (Akamai-fronted, e.g. nykaa.com) refuse browser-like agents from servers but
        # answer the plain link-preview agent.
        retry = await _get_capped(client, site, MAX_TOTAL_BYTES, 5, headers={"User-Agent": BROWSER_UA})
        if retry and "html" in retry[0].lower() and len(retry[1]) > len(got[1] if got else b""):
            got = retry
            client.headers["User-Agent"] = BROWSER_UA
    if not got or "html" not in got[0].lower():
        return
    page = got[1].decode("utf-8", "ignore")
    meta = _meta(page)
    ident.title = _text(re.search(r"<title[^>]*>(.*?)</title>", page, re.IGNORECASE | re.DOTALL).group(1))[:200] \
        if re.search(r"<title[^>]*>(.*?)</title>", page, re.IGNORECASE | re.DOTALL) else ""
    ident.description = (meta.get("description") or meta.get("og:description") or "")[:400]

    colors: Counter = Counter()
    fonts: dict[str, Counter] = {"heading": Counter(), "body": Counter(), "any": Counter(), "face": Counter()}
    bg: Counter = Counter()
    if tc := meta.get("theme-color"):
        for c in _colors_in(tc):
            colors[c] += 12
            bg[c] += 0.5
    inline = "\n".join(re.findall(r"<style[^>]*>(.*?)</style>", page, re.IGNORECASE | re.DOTALL))
    for m in re.finditer(r"style=\"([^\"]+)\"", page):
        inline += "\n.inline{" + m.group(1) + "}"
    analyse_css(inline[:400_000], colors, fonts, bg)
    for fam in _google_families(page):
        fonts["any"][fam] += 3

    css_links = []
    host = urlparse(site).netloc
    for a in _tags(page, "link"):
        if "stylesheet" in a.get("rel", "").lower() and a.get("href"):
            href = urljoin(site, a["href"])
            if "fonts.googleapis.com" in href:
                continue
            same = urlparse(href).netloc.endswith(host.removeprefix("www."))
            css_links.append((0 if same else 1, href))
    css_links = [h for _, h in sorted(css_links)][:MAX_CSS_FILES]
    left = max(2.0, SITE_BUDGET_S - (time.monotonic() - started))
    sheets = await asyncio.gather(*(_get_capped(client, h, 600_000, left) for h in css_links))
    total = len(page)
    for sheet in sheets:
        if not sheet or total > MAX_TOTAL_BYTES * 2:
            continue
        total += len(sheet[1])
        analyse_css(sheet[1].decode("utf-8", "ignore"), colors, fonts, bg)

    # Background mode from body/html backgrounds (or theme-color).
    if bg:
        top = bg.most_common(1)[0][0]
        ident.site_bg = top
        ident.bg_mode = "dark" if _lum(top) < 0.18 else "light"

    brand_colors = {c: w for c, w in colors.items() if not _neutral(c)}
    ident.color_sources = {c: "site" for c, _ in _cluster(brand_colors)[:4]}
    ident.colors = [c for c, _ in _cluster(brand_colors)[:4]]

    # Fonts: headings first, then body, then the rest.
    order: list[str] = []
    for bucket in ("heading", "body", "face", "any"):
        for fam, _ in fonts[bucket].most_common():
            if fam not in order:
                order.append(fam)
    # System/fallback faces (Arial, system-ui, Roboto...) only count when nothing else is used.
    order = [f for f in order if clean_family(f) not in _SYSTEM_FONTS] + \
        [f for f in order if clean_family(f) in _SYSTEM_FONTS]
    ident.raw_fonts = order[:8]

    # Logo.
    logo = await _fetch_logo(client, _logo_candidates(page, site, ident.brand,
                                                         sub_brand=bool(urlparse(site).path.strip("/"))))
    if logo:
        try:
            ident.logo_url = save_png(logo[0])
            ident.logo_source = logo[1]
        except ValueError:
            pass

    # Product / hero images: og:image + big images in hero/banner/product blocks.
    imgs = []
    for key in ("og:image", "og:image:url", "twitter:image"):
        if meta.get(key):
            imgs.append(urljoin(site, meta[key]))
    for a in _tags(page, "img"):
        src = a.get("src") or a.get("data-src") or ""
        hint = " ".join(a.get(k, "") for k in ("src", "alt", "class"))
        if src and not src.startswith("data:") and not _LOGO_HINT.search(hint) \
                and re.search(r"hero|banner|product|slide|carousel|campaign|kv", hint, re.IGNORECASE):
            imgs.append(urljoin(site, src))
    seen: set[str] = set()
    fetched = await asyncio.gather(*(fetch_image(client, u, follow_og=False) for u in list(dict.fromkeys(imgs))[:6]))
    for img in fetched:
        if not img:
            continue
        try:
            with Image.open(io.BytesIO(img.data)) as im:
                if min(im.size) < 300:
                    continue
        except Exception:  # noqa: BLE001
            continue
        d = image_digest(img.data)
        if d in seen:
            continue
        seen.add(d)
        try:
            ident.product_images.append(save_image(img.data))
        except ValueError:
            continue
        if len(ident.product_images) >= 3:
            break

    # Motifs from navigation, headings, meta text and research.
    nav_html = " ".join(re.findall(r"<(?:nav|header)\b.*?</(?:nav|header)>", page, re.IGNORECASE | re.DOTALL))[:300_000]
    nav = [_text(m) for m in re.findall(r"<a\b[^>]*>(.*?)</a>", nav_html, re.IGNORECASE | re.DOTALL)]
    headings = [_text(h) for h in re.findall(r"<h[1-3][^>]*>(.*?)</h[1-3]>", page, re.IGNORECASE | re.DOTALL)][:30]
    if urlparse(site).path.strip("/"):  # a brand page on a parent site: its menu is the parent's
        nav = []
        headings = headings[:6]
    ident.site_nav, ident.site_headings = nav, headings
    ident.site_jsonld = jsonld_names(page)
    ident.motifs = extract_motifs(ident.brand, nav, headings + [ident.title], ident.description, research_text)


async def _vision_font_category(logo_png: bytes) -> str | None:
    prompt = (
        "Classify the lettering style of this brand logo for choosing a similar Google font. "
        'Reply as JSON {"category": one of "geometric", "humanist", "grotesk", "condensed", "slab", "serif", '
        '"didone", "rounded", "script", "display"}.'
    )
    try:
        with Image.open(io.BytesIO(logo_png)) as img:
            img = img.convert("RGBA")
            canvas = Image.new("RGB", img.size, "white")
            canvas.paste(img, mask=img.split()[-1])
            canvas.thumbnail((512, 512))
            buf = io.BytesIO()
            canvas.save(buf, "JPEG", quality=85)
        ans = await asyncio.wait_for(llm_router.invoke_vision_json(prompt, [(buf.getvalue(), "image/jpeg")]), 20)
    except Exception:  # noqa: BLE001 - vision is optional
        return None
    cat = (ans or {}).get("category") if isinstance(ans, dict) else None
    return cat if cat in CATEGORY_PAIRS else None


def _merge_colors(ident: BrandIdentity, logo_colors: list[tuple[str, float]], own_logo: list[tuple[str, float]]) -> None:
    """Vote across sources: the site's own logo (strongest), logo image search, then site CSS rank.

    CSS often carries framework colours (iOS blue links, Bootstrap), so a CSS colour only leads
    when nothing logo-based contradicts it.
    """
    evidence: list[tuple[str, float, str]] = []
    for c, share in own_logo:
        evidence.append((c, 6.0 + 4 * share, "site logo"))
    for i, (c, _) in enumerate(logo_colors[:3]):
        evidence.append((c, 4.0 - i, "logo images"))
    for i, c in enumerate(ident.colors[:4]):
        evidence.append((c, 3.0 - 0.7 * i, "site css"))
    clusters: list[dict[str, Any]] = []
    for c, w, src in evidence:
        for cl in clusters:
            if _dist(c, cl["color"]) < 45:
                cl["score"] += w
                cl["sources"].add(src)
                if w > cl["lead"]:
                    cl["color"], cl["lead"] = c, w  # the strongest single source names it
                break
        else:
            clusters.append({"color": c, "score": w, "lead": w, "sources": {src}})
    clusters.sort(key=lambda cl: -cl["score"])
    ident.colors = [cl["color"] for cl in clusters[:4]]
    ident.color_sources = {cl["color"]: "+".join(sorted(cl["sources"])) for cl in clusters[:4]}


def cached_identity(brand: str) -> BrandIdentity | None:
    """The identity probed for ``brand`` in the last hour, without any network call."""
    hit = _cache.get(" ".join((brand or "").split())[:80].lower())
    return hit[1].model_copy(deep=True) if hit and hit[0] > time.monotonic() else None


async def probe_brand_identity(brand: str, research_text: str = "") -> BrandIdentity:
    """Best-effort identity of ``brand`` from its own website (+ logo images, research). Never raises."""
    brand = " ".join((brand or "").split())[:80]
    ident = BrandIdentity(brand=brand)
    ident.season, ident.season_source = detect_season_detail(date.today(), research_text)
    if not brand:
        return ident
    hit = _cache.get(brand.lower())
    if hit and hit[0] > time.monotonic():
        cached = hit[1].model_copy(deep=True)
        cached.season, cached.season_source = ident.season, ident.season_source
        if research_text:
            cached.motifs = extract_motifs(brand, cached.site_nav, cached.site_headings + [cached.title],
                                           cached.description, research_text) or cached.motifs
        return cached
    started = time.monotonic()
    # Websites get a normal browser User-Agent (some, like zomato.com, stall on unknown agents);
    # images keep the link-preview agent (Instagram only serves og:image to it).
    headers = {"User-Agent": SITE_UA, "Accept": "text/html,text/css,image/*;q=0.9,*/*;q=0.5",
               "Accept-Language": "en-IN,en;q=0.9"}
    async with httpx.AsyncClient(timeout=httpx.Timeout(8.0, connect=5.0), follow_redirects=True,
                                 headers=headers) as client:

        async def site_part() -> None:
            ident.site_url = await find_official_site(brand, client)
            ident.timings["site_search"] = round(time.monotonic() - started, 2)
            if ident.site_url:
                await _read_site(ident.site_url, client, ident, research_text)
            ident.timings["site_read"] = round(time.monotonic() - started, 2)

        try:
            _, logo_colors = await asyncio.wait_for(
                asyncio.gather(site_part(), _logo_image_colors(brand, client)), timeout=PROBE_TIMEOUT)
        except TimeoutError:
            logger.info("identity: probe for %s hit the %ss cap; using what was found", brand, PROBE_TIMEOUT)
            logo_colors = []
        except Exception:  # noqa: BLE001 - identity is best-effort
            logger.exception("identity: probe failed for %s", brand)
            logo_colors = []
    fallback_logo = _LOGO_FALLBACK.pop(brand, None)
    if not ident.logo_url and fallback_logo:
        try:
            ident.logo_url = save_png(fallback_logo)
            ident.logo_source = "logo image search"
        except ValueError:
            pass
    own_logo: list[tuple[str, float]] = []
    if ident.logo_url:
        from app.services.media import resolve_media

        lp = resolve_media(ident.logo_url)
        own_logo = dominant_image_colors(lp.read_bytes()) if lp else []
    _merge_colors(ident, logo_colors, own_logo)
    try:
        ident.products = await asyncio.wait_for(find_products(
            brand, jsonld=ident.site_jsonld, nav=ident.site_nav, headings=ident.site_headings, title=ident.title,
            description=ident.description, research_text=research_text), timeout=35)
    except Exception as exc:  # noqa: BLE001 - products are best-effort
        logger.info("identity: product lookup failed for %s: %s", brand, exc.__class__.__name__)

    brand_fonts = [f for f in ident.raw_fonts if clean_family(f) not in _SYSTEM_FONTS]
    heading, body, how = map_fonts(brand_fonts)  # system-only sites have no brand font to map
    if how in ("default", "classified") and ident.logo_url:
        from app.services.media import resolve_media

        path = resolve_media(ident.logo_url)
        cat = await _vision_font_category(path.read_bytes()) if path else None
        if cat:
            heading, body = CATEGORY_PAIRS[cat]  # type: ignore[index]
            how = "logo vision"
    ident.heading_font, ident.body_font, ident.font_source = heading, body, how
    ident.timings["total"] = round(time.monotonic() - started, 2)
    if ident.site_url or ident.colors:
        _cache[brand.lower()] = (time.monotonic() + IDENTITY_TTL, ident.model_copy(deep=True))
    logger.info(
        "identity: %s site=%s colors=%s fonts=%s->%s/%s (%s) logo=%s motifs=%s season=%s %.1fs",
        brand, ident.site_url, ident.colors, ident.raw_fonts[:3], heading, body, how, bool(ident.logo_url),
        ident.products or ident.motifs, ident.season, ident.timings["total"],
    )
    return ident
