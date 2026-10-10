"""Local media store for deck images (WORKPLAN C7). Owner: Agent B.

Every visual (ref, meme, mock, hero) is downloaded or generated server-side,
validated, resized and saved as ``{settings.media_dir}/{sha1}.jpg``. It is served
by FastAPI at ``/media/<file>`` and referenced in the deck as ``/media/<file>``,
so URLs never expire and PPTX rendering never touches the network.
"""

from pathlib import Path

MEDIA_URL_PREFIX = "/media/"


def save_image(data: bytes) -> str:
    """Validate, resize (max 1600 px) and store an image; return its ``/media/...`` URL.

    Raises ValueError if ``data`` is not a usable image.
    """
    raise NotImplementedError


def resolve_media(url: str | None) -> Path | None:
    """Local file for a ``/media/...`` URL, or None if it is not a stored media file."""
    raise NotImplementedError
