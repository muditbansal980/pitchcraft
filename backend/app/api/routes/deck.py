"""Deck export endpoints (WORKPLAN C5). Owner: Agent B.

POST /api/deck/pptx  body: Deck  ->  .pptx download
"""

import asyncio
import logging
import re
import unicodedata

from fastapi import APIRouter, HTTPException, Response, status

from app.api.deps import CurrentUser
from app.schemas.deck import Deck
from app.services.deck_render import PPTX_MIME, render_deck

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/deck", tags=["deck"])

MAX_SLIDES = 80


def slugify(value: str, fallback: str = "deck") -> str:
    ascii_text = unicodedata.normalize("NFKD", value or "").encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")
    return slug[:60].rstrip("-") or fallback


@router.post(
    "/pptx",
    response_class=Response,
    responses={200: {"content": {PPTX_MIME: {}}, "description": "The deck as a .pptx file"}},
)
async def deck_pptx(deck: Deck, user: CurrentUser) -> Response:
    """Render a ``Deck`` (as returned by /api/brand-plan) to an editable PowerPoint file."""
    if not deck.slides:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "The deck has no slides")
    if len(deck.slides) > MAX_SLIDES:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"A deck can have at most {MAX_SLIDES} slides")
    try:
        data = await asyncio.to_thread(render_deck, deck)
    except Exception as exc:
        logger.exception("PPTX render failed for user %s", user.id)
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Could not render the deck") from exc
    filename = f"{slugify(deck.brand or deck.title)}-pitch.pptx"
    return Response(
        content=data,
        media_type=PPTX_MIME,
        headers={"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store"},
    )
