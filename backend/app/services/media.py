"""Local media store for deck images (WORKPLAN C7). Owner: Agent B.

Every visual (ref, meme, mock, hero) is downloaded or generated server-side,
validated, resized and saved as ``{settings.media_dir}/{sha1}.jpg``. It is served
by FastAPI at ``/media/<file>`` and referenced in the deck as ``/media/<file>``,
so URLs never expire and PPTX rendering never touches the network.
"""

import hashlib
import io
import re
import warnings
from pathlib import Path

from PIL import Image, ImageOps

from app.core.config import settings

MEDIA_URL_PREFIX = "/media/"
MAX_SIDE = 1600
MIN_SIDE = 48
MAX_INPUT_BYTES = 15 * 1024 * 1024
# Refuse decompression bombs well below Pillow's own warning threshold.
MAX_PIXELS = 40_000_000
JPEG_QUALITY = 86

_FILE_RE = re.compile(r"^[0-9a-f]{40}\.(?:jpg|png)$")


def media_root() -> Path:
    root = Path(settings.media_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def image_digest(data: bytes) -> str:
    """sha1 of the raw bytes: the stored file name, also used to dedupe images across a deck."""
    return hashlib.sha1(data).hexdigest()


def _to_rgb(img: Image.Image) -> Image.Image:
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        canvas = Image.new("RGB", rgba.size, (255, 255, 255))
        canvas.paste(rgba, mask=rgba.split()[-1])
        return canvas
    return img.convert("RGB")


def save_image(data: bytes) -> str:
    """Validate, resize (max 1600 px) and store an image; return its ``/media/...`` URL.

    Raises ValueError if ``data`` is not a usable image.
    """
    if not data:
        raise ValueError("empty image data")
    if len(data) > MAX_INPUT_BYTES:
        raise ValueError("image too large")

    name = f"{image_digest(data)}.jpg"
    target = media_root() / name
    if target.is_file():
        return MEDIA_URL_PREFIX + name

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as probe:
                probe.verify()
            img = Image.open(io.BytesIO(data))
            if img.width * img.height > MAX_PIXELS:
                raise ValueError("image has too many pixels")
            img.seek(0)  # first frame of GIFs/WebP animations
            img.load()
    except ValueError:
        raise
    except Exception as exc:  # Pillow raises many types for bad data
        raise ValueError(f"not a usable image: {exc.__class__.__name__}") from exc

    img = ImageOps.exif_transpose(img)
    if min(img.size) < MIN_SIDE:
        raise ValueError("image too small")
    img = _to_rgb(img)
    if max(img.size) > MAX_SIDE:
        img.thumbnail((MAX_SIDE, MAX_SIDE), Image.Resampling.LANCZOS)

    tmp = target.with_suffix(".tmp")
    img.save(tmp, "JPEG", quality=JPEG_QUALITY, optimize=True, progressive=True)
    tmp.replace(target)
    return MEDIA_URL_PREFIX + name


def save_png(data: bytes, max_side: int = 800) -> str:
    """Store an image as PNG keeping its transparency (logos); return its ``/media/<sha1>.png`` URL.

    Raises ValueError if ``data`` is not a usable image.
    """
    if not data or len(data) > MAX_INPUT_BYTES:
        raise ValueError("empty or oversized image data")
    name = f"{image_digest(data)}.png"
    target = media_root() / name
    if target.is_file():
        return MEDIA_URL_PREFIX + name
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            img = Image.open(io.BytesIO(data))
            if img.width * img.height > MAX_PIXELS:
                raise ValueError("image has too many pixels")
            img.seek(0)
            img.load()
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"not a usable image: {exc.__class__.__name__}") from exc
    if min(img.size) < 16:
        raise ValueError("image too small")
    img = img.convert("RGBA")
    if max(img.size) > max_side:
        img.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    tmp = target.with_suffix(".tmp")
    img.save(tmp, "PNG", optimize=True)
    tmp.replace(target)
    return MEDIA_URL_PREFIX + name


def resolve_media(url: str | None) -> Path | None:
    """Local file for a ``/media/...`` URL, or None if it is not a stored media file.

    Accepts ``/media/<sha1>.jpg|png`` and absolute/proxied forms ending in ``/media/<file>``
    (e.g. ``http://host/media/x.jpg``, ``/api/media/x.jpg``). The file name must be a
    sha1 + ``.jpg``/``.png``, so no path can escape the media directory.
    """
    if not url or not isinstance(url, str):
        return None
    path = url.split("?", 1)[0].split("#", 1)[0]
    marker = path.rfind(MEDIA_URL_PREFIX)
    if marker < 0:
        return None
    name = path[marker + len(MEDIA_URL_PREFIX):]
    if not _FILE_RE.fullmatch(name):
        return None
    root = media_root()
    candidate = (root / name).resolve()
    if candidate.parent != root or not candidate.is_file():
        return None
    return candidate
