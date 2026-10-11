"""Canva Connect endpoints (WORKPLAN C5). Owner: Agent C.

GET    /api/canva/status       -> {"connected": bool, "configured": bool}
GET    /api/canva/authorize    -> {"url": str}   (OAuth Authorization Code + PKCE)
POST   /api/canva/callback     body {"code", "state"} -> {"connected": true}
DELETE /api/canva/connection   -> 204
POST   /api/canva/import       body: Deck -> {"edit_url": str, "view_url": str}

Every endpoint needs the logged-in user. Authorize, callback and import answer 503
while CANVA_CLIENT_ID / CANVA_CLIENT_SECRET are unset; status reports
``configured: false`` instead.
"""

import asyncio
import logging

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from app.api.deps import CurrentUser, DbSession
from app.schemas.deck import Deck
from app.services import canva
from app.services.deck_render import render_deck

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/canva", tags=["canva"])


class CanvaStatus(BaseModel):
    connected: bool
    configured: bool


class AuthorizeResponse(BaseModel):
    url: str


class CallbackRequest(BaseModel):
    code: str = Field(min_length=1, max_length=4096)
    state: str = Field(min_length=1, max_length=4096)


class CallbackResponse(BaseModel):
    connected: bool = True


class ImportResponse(BaseModel):
    edit_url: str
    view_url: str


def _http_error(err: canva.CanvaError) -> HTTPException:
    return HTTPException(err.status_code, err.detail)


@router.get("/status", response_model=CanvaStatus)
def canva_status(user: CurrentUser, db: DbSession) -> CanvaStatus:
    configured = canva.is_configured()
    return CanvaStatus(configured=configured, connected=configured and canva.is_connected(db, user.id))


@router.get("/authorize", response_model=AuthorizeResponse)
def authorize(user: CurrentUser) -> AuthorizeResponse:
    try:
        return AuthorizeResponse(url=canva.build_authorize_url(user.id))
    except canva.CanvaError as err:
        raise _http_error(err) from None


@router.post("/callback", response_model=CallbackResponse)
async def callback(payload: CallbackRequest, user: CurrentUser, db: DbSession) -> CallbackResponse:
    try:
        await canva.exchange_code(db, user.id, payload.code, payload.state)
    except canva.CanvaError as err:
        raise _http_error(err) from None
    return CallbackResponse()


@router.delete("/connection", status_code=status.HTTP_204_NO_CONTENT)
async def disconnect(user: CurrentUser, db: DbSession) -> None:
    # Works without Canva keys too, so a stored connection can always be removed.
    await canva.disconnect(db, user.id)


@router.post("/import", response_model=ImportResponse)
async def import_deck(deck: Deck, user: CurrentUser, db: DbSession) -> ImportResponse:
    try:
        canva.require_configured()
        if not canva.is_connected(db, user.id):
            raise canva.CanvaNotConnectedError()
    except canva.CanvaError as err:
        raise _http_error(err) from None

    if not deck.slides:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "The deck has no slides.")

    try:
        pptx = await asyncio.to_thread(render_deck, deck)
    except NotImplementedError:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "PPTX rendering isn't available yet.") from None
    except Exception:
        logger.exception("Rendering the deck for Canva failed")
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Rendering the deck failed.") from None

    title = deck.title or f"{deck.brand} pitch"
    try:
        urls = await canva.import_pptx(db, user.id, pptx, title)
    except canva.CanvaError as err:
        raise _http_error(err) from None
    return ImportResponse(**urls)
