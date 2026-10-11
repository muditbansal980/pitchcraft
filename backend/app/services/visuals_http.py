"""HTTP helpers shared by visuals, trends and themes (Agent B).

``fetch_image`` downloads one candidate image with a size cap and MIME check.
Some image-search results (Instagram ``lookaside`` links) point at an HTML page
rather than an image; for those the page's ``og:image`` is followed once.
"""

import html
import logging
import re
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

# A neutral, honest link-preview agent: Instagram serves og:image tags to it (a full
# desktop-browser UA gets a login shell without them).
BROWSER_UA = "Mozilla/5.0 (compatible; PitchCraft/0.2; link preview)"
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_HTML_BYTES = 1_500_000
IMAGE_MIMES = ("image/jpeg", "image/png", "image/webp", "image/gif")

_OG_IMAGE_RE = re.compile(
    r"""<meta[^>]+(?:property|name)=["'](?:og:image|twitter:image)(?::src)?["'][^>]*content=["']([^"']+)["']"""
    r"""|<meta[^>]+content=["']([^"']+)["'][^>]*(?:property|name)=["'](?:og:image|twitter:image)["']""",
    re.IGNORECASE,
)


# Stock libraries whose previews carry a visible watermark: never used in a client deck.
WATERMARK_HOSTS = (
    "dreamstime.com", "shutterstock.com", "alamy.com", "istockphoto.com", "gettyimages", "123rf.com",
    "depositphotos.com", "ftcdn.net", "stock.adobe.com", "vecteezy.com", "canstockphoto", "bigstockphoto",
    "pond5.com", "agefotostock", "colourbox", "stockfreeimages", "megapixl", "pixtastock", "photodune",
    "envato", "storyblocks", "fotosearch", "featurepics", "stocksy", "masterfile", "superstock", "pngtree.com",
)


def watermarked_url(*urls: str | None) -> bool:
    """True if any URL points at a stock library known to watermark its previews."""
    return any(u and any(h in u.lower() for h in WATERMARK_HOSTS) for u in urls)


@dataclass(frozen=True)
class FetchedImage:
    data: bytes
    mime: str
    url: str  # final URL the bytes came from
    page_url: str | None = None  # the HTML page whose og:image this is (e.g. the Instagram reel)


def new_client(timeout: float = 10.0) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(timeout, connect=5.0),
        follow_redirects=True,
        headers={"User-Agent": BROWSER_UA, "Accept": "image/avif,image/webp,image/*,text/html;q=0.8,*/*;q=0.5"},
    )


def _sniff_mime(data: bytes) -> str | None:
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return None


async def _read_capped(resp: httpx.Response, cap: int) -> bytes | None:
    declared = resp.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > cap:
        return None
    chunks: list[bytes] = []
    total = 0
    async for chunk in resp.aiter_bytes():
        total += len(chunk)
        if total > cap:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


_YT_THUMB_RE = re.compile(r"^(https://i\.ytimg\.com/vi/[^/]+/)(?:default|mqdefault|sddefault|\d)\.jpg", re.IGNORECASE)


def upgrade_url(url: str) -> str:
    """Ask for a bigger rendition where the URL scheme allows it (YouTube thumbnails)."""
    m = _YT_THUMB_RE.match(url)
    return f"{m.group(1)}hqdefault.jpg" if m else url


async def fetch_image(client: httpx.AsyncClient, url: str, *, follow_og: bool = True) -> FetchedImage | None:
    """Download ``url`` as an image (<= 8 MB, real image bytes). None on any failure."""
    if not url or not url.startswith(("http://", "https://")):
        return None
    url = upgrade_url(url)
    try:
        async with client.stream("GET", url) as resp:
            if resp.status_code != 200:
                return None
            ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
            if ctype.startswith("text/html") and follow_og:
                body = await _read_capped(resp, MAX_HTML_BYTES)
                if not body:
                    return None
                match = _OG_IMAGE_RE.search(body.decode("utf-8", "ignore"))
                og = html.unescape(match.group(1) or match.group(2)) if match else None
                if not og:
                    return None
                img = await fetch_image(client, og, follow_og=False)
                return FetchedImage(img.data, img.mime, img.url, page_url=str(resp.url)) if img else None
            if ctype and not ctype.startswith("image/") and ctype != "application/octet-stream":
                return None
            data = await _read_capped(resp, MAX_IMAGE_BYTES)
    except (httpx.HTTPError, ValueError) as exc:
        logger.debug("image fetch failed %s: %s", url[:120], exc)
        return None
    if not data:
        return None
    mime = _sniff_mime(data)
    if mime not in IMAGE_MIMES:
        return None
    return FetchedImage(data=data, mime=mime, url=str(resp.url))
