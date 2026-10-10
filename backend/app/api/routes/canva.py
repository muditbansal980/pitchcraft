"""Canva Connect endpoints (WORKPLAN C5). Owner: Agent C.

GET    /api/canva/status       -> {"connected": bool, "configured": bool}
GET    /api/canva/authorize    -> {"url": str}   (OAuth Authorization Code + PKCE)
POST   /api/canva/callback     body {"code", "state"} -> {"connected": true}
DELETE /api/canva/connection   -> 204
POST   /api/canva/import       body: Deck -> {"edit_url": str, "view_url": str}
"""

from fastapi import APIRouter

router = APIRouter(prefix="/api/canva", tags=["canva"])
