"""Deck export endpoints (WORKPLAN C5). Owner: Agent B.

POST /api/deck/pptx  body: Deck  ->  .pptx download
"""

from fastapi import APIRouter

router = APIRouter(prefix="/api/deck", tags=["deck"])
