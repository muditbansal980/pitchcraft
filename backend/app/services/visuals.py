"""Deck visuals (WORKPLAN C7). Owner: Agent B.

Fills every ``Visual`` request in a deck:
- want="ref":  candidates from Reddit + Tavily images, best one picked by the
  vision role (``llm_router.invoke_vision_json``), fallback: source rank.
- want="meme": Imgflip ``caption_image`` with ``meme_template_id`` + ``meme_boxes``
  (after a caption safety filter).
- want="mock": AI image (``settings.image_gen_provider``).
Results go through ``media.save_image``. Anything that fails stays a
placeholder; this module never raises for a single bad visual.
"""

from dataclasses import dataclass

from app.schemas.deck import Deck


@dataclass(frozen=True)
class MemeTemplate:
    id: str
    name: str
    box_count: int


@dataclass(frozen=True)
class VisualBudget:
    """Max visuals filled per kind; requests beyond the budget stay placeholders."""

    ref: int
    meme: int
    mock: int


async def get_meme_templates() -> list[MemeTemplate]:
    """Imgflip's currently popular templates (cached ~6 h). Empty list on failure."""
    return []


async def attach_visuals(deck: Deck, budget: VisualBudget) -> Deck:
    """Fill the deck's visual requests (incl. ``deck.theme.hero_image``) within ``budget``."""
    return deck
