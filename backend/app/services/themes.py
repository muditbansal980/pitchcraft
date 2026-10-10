"""Deck themes (WORKPLAN C7). Owner: Agent B.

Five presets modelled on the past WLDD decks (bold_dark, cinematic, pop_meme,
editorial, sport_energy). The strategist picks a preset; the accent comes from
the brand's logo colours when the probe finds them, and is contrast-checked
against the background.
"""

from pydantic import BaseModel, Field

from app.schemas.deck import Theme, ThemeName

THEME_DESCRIPTIONS: dict[str, str] = {
    "bold_dark": "Black background, one bold brand accent, heavy italic headlines. Default for most brands (FMCG, fitness, tech).",
    "cinematic": "Deep navy/black with a red or gold accent, film-poster feel. OTT, films, music, entertainment.",
    "pop_meme": "Bright, high-contrast, playful. Youth, snacks, quick commerce, meme-first campaigns.",
    "editorial": "Light background, serif headings, refined. Beauty, fashion, premium, books/audio.",
    "sport_energy": "Dark with neon/energetic accent, diagonal energy. Sports, cricket, gaming, energy drinks.",
}


class ThemeChoice(BaseModel):
    """What the strategist decides about the look."""

    name: ThemeName = "bold_dark"
    accent_hint: str | None = Field(None, description="Hex the strategist suggests if no brand colour was probed")
    hero_prompt: str = Field("", description="AI image prompt for the cover/divider background")


async def probe_brand_colors(brand: str) -> list[str]:
    """1-2 dominant hex colours from the brand's logo (Tavily image search + Pillow). Empty on failure."""
    return []


def resolve_theme(choice: ThemeChoice, brand_colors: list[str]) -> Theme:
    """Preset + accent (brand colour > hint > preset default), contrast-adjusted.

    Sets ``hero_image`` to a ``Visual(want="mock", brief=choice.hero_prompt)`` request
    when a hero prompt is given; ``attach_visuals`` fills it.
    """
    return Theme(name=choice.name)
