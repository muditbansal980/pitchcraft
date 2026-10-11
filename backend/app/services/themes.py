"""Deck themes (WORKPLAN C7). Owner: Agent B.

Five presets modelled on the past WLDD decks (bold_dark, cinematic, pop_meme,
editorial, sport_energy). The strategist picks a preset; the accent comes from
the brand's logo colours when the probe finds them, and is contrast-checked
against the background.

Fonts are Google Fonts that Canva ships, so a Canva import keeps them; PowerPoint
without them installed falls back to its default sans (the renderer's text
fitting leaves headroom for that).
"""

import colorsys
import io
import logging
from typing import TYPE_CHECKING

from PIL import Image
from pydantic import BaseModel, Field

from app.core.config import settings
from app.schemas.deck import Theme, ThemeName, Visual

if TYPE_CHECKING:
    from app.services.brand_identity import BrandIdentity

logger = logging.getLogger(__name__)

THEME_DESCRIPTIONS: dict[str, str] = {
    "bold_dark": "Black background, one bold brand accent, heavy italic headlines. Default for most brands (FMCG, fitness, tech).",
    "cinematic": "Deep navy/black with a red or gold accent, film-poster feel. OTT, films, music, entertainment.",
    "pop_meme": "Bright, high-contrast, playful. Youth, snacks, quick commerce, meme-first campaigns.",
    "editorial": "Light background, serif headings, refined. Beauty, fashion, premium, books/audio.",
    "sport_energy": "Dark with neon/energetic accent, diagonal energy. Sports, cricket, gaming, energy drinks.",
}

# Full palettes. ``accent`` is the default when no brand colour / hint is usable.
PRESETS: dict[str, dict[str, str]] = {
    "bold_dark": {
        "bg": "#0B0B0B", "surface": "#1A1A1A", "accent": "#F5B700", "accent_2": "#FFFFFF",
        "text": "#FFFFFF", "muted": "#B5B5B5", "heading_font": "Anton", "body_font": "Montserrat",
    },
    "cinematic": {
        "bg": "#070B18", "surface": "#121A2E", "accent": "#E50914", "accent_2": "#F2C14E",
        "text": "#F4F4F6", "muted": "#A9B0C3", "heading_font": "Bebas Neue", "body_font": "Inter",
    },
    "pop_meme": {
        "bg": "#FFF6DF", "surface": "#FFFFFF", "accent": "#2F3CFF", "accent_2": "#FFD000",
        "text": "#111111", "muted": "#4A4A4A", "heading_font": "Archivo Black", "body_font": "Poppins",
    },
    "editorial": {
        "bg": "#F5F0E8", "surface": "#FFFFFF", "accent": "#8C2F39", "accent_2": "#1F1F1F",
        "text": "#1C1C1C", "muted": "#5C5650", "heading_font": "Playfair Display", "body_font": "Montserrat",
    },
    "sport_energy": {
        "bg": "#06101F", "surface": "#0F1E36", "accent": "#C6FF00", "accent_2": "#00E0FF",
        "text": "#FFFFFF", "muted": "#9FB2CC", "heading_font": "Oswald", "body_font": "Poppins",
    },
}

HERO_STYLE = {
    "bold_dark": "dramatic low-key lighting, deep black shadows, warm rim light",
    "cinematic": "cinematic film still, moody teal and red lighting, anamorphic look",
    "pop_meme": "bright saturated colours, playful flash photography, pop-art energy",
    "editorial": "soft natural light, editorial magazine photography, clean minimal backdrop",
    "sport_energy": "high-energy action shot, motion blur, stadium floodlights, neon glow",
}

MIN_CONTRAST = 4.5


class ThemeChoice(BaseModel):
    """What the strategist decides about the look."""

    name: ThemeName = "bold_dark"
    accent_hint: str | None = Field(None, description="Hex the strategist suggests if no brand colour was probed")
    hero_prompt: str = Field("", description="AI image prompt for the cover/divider background")


# --- colour maths -----------------------------------------------------------


def normalize_hex(value: str | None) -> str | None:
    if not value or not isinstance(value, str):
        return None
    v = value.strip().lstrip("#")
    if len(v) == 3:
        v = "".join(c * 2 for c in v)
    if len(v) != 6:
        return None
    try:
        int(v, 16)
    except ValueError:
        return None
    return "#" + v.upper()


def hex_to_rgb(value: str) -> tuple[int, int, int]:
    v = value.lstrip("#")
    return int(v[0:2], 16), int(v[2:4], 16), int(v[4:6], 16)


def rgb_to_hex(rgb: tuple[float, float, float]) -> str:
    return "#" + "".join(f"{max(0, min(255, round(c))):02X}" for c in rgb)


def luminance(value: str) -> float:
    def chan(c: int) -> float:
        s = c / 255
        return s / 12.92 if s <= 0.03928 else ((s + 0.055) / 1.055) ** 2.4

    r, g, b = hex_to_rgb(value)
    return 0.2126 * chan(r) + 0.7152 * chan(g) + 0.0722 * chan(b)


def contrast(a: str, b: str) -> float:
    la, lb = luminance(a), luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def on_color(fill: str) -> str:
    """Black or white, whichever reads better on ``fill`` (always >= 4.5:1)."""
    return "#000000" if contrast(fill, "#000000") >= contrast(fill, "#FFFFFF") else "#FFFFFF"


def mix(a: str, b: str, t: float) -> str:
    """Blend ``a`` toward ``b`` by ``t`` (0..1)."""
    ra, ga, ba = hex_to_rgb(a)
    rb, gb, bb = hex_to_rgb(b)
    return rgb_to_hex((ra + (rb - ra) * t, ga + (gb - ga) * t, ba + (bb - ba) * t))


def ensure_contrast(color: str, bg: str, minimum: float = MIN_CONTRAST) -> str:
    """Shift ``color``'s lightness away from ``bg`` until the contrast is at least ``minimum``."""
    if contrast(color, bg) >= minimum:
        return color
    r, g, b = (c / 255 for c in hex_to_rgb(color))
    h, lum, s = colorsys.rgb_to_hls(r, g, b)
    lighten = luminance(bg) < 0.4
    for _ in range(40):
        lum = min(1.0, lum + 0.025) if lighten else max(0.0, lum - 0.025)
        candidate = rgb_to_hex(tuple(c * 255 for c in colorsys.hls_to_rgb(h, lum, s)))
        if contrast(candidate, bg) >= minimum:
            return candidate
    return on_color(bg)


def is_neutral(value: str) -> bool:
    r, g, b = (c / 255 for c in hex_to_rgb(value))
    _, lum, sat = colorsys.rgb_to_hls(r, g, b)
    return sat < 0.25 or lum > 0.93 or lum < 0.08


# --- brand colour probe -----------------------------------------------------


def dominant_colors(data: bytes, count: int = 2) -> list[tuple[str, float]]:
    """Saturated dominant colours of an image, as (hex, share of non-neutral pixels)."""
    try:
        img = Image.open(io.BytesIO(data))
        img.thumbnail((96, 96))
        rgba = img.convert("RGBA")
    except Exception:
        return []
    px = [p[:3] for p in rgba.getdata() if p[3] > 200]
    if len(px) < 50:
        return []
    flat = Image.new("RGB", (len(px), 1))
    flat.putdata(px)
    quant = flat.quantize(colors=8, method=Image.Quantize.MEDIANCUT)
    palette = quant.getpalette() or []
    counts = sorted(quant.getcolors() or [], reverse=True)
    out: list[tuple[str, float]] = []
    total = len(px)
    for n, idx in counts:
        hexv = rgb_to_hex(tuple(palette[idx * 3: idx * 3 + 3]))
        if not is_neutral(hexv) and n / total >= 0.03:
            out.append((hexv, n / total))
    return out[: count * 2]


def _hue_distance(a: str, b: str) -> float:
    ha = colorsys.rgb_to_hls(*(c / 255 for c in hex_to_rgb(a)))[0]
    hb = colorsys.rgb_to_hls(*(c / 255 for c in hex_to_rgb(b)))[0]
    d = abs(ha - hb)
    return min(d, 1 - d)


async def probe_brand_colors(brand: str) -> list[str]:
    """1-2 brand hex colours (thin wrapper over ``probe_brand_identity``). Empty on failure."""
    from app.services.brand_identity import probe_brand_identity

    try:
        ident = await probe_brand_identity(brand)
    except Exception:  # noqa: BLE001 - never fail the plan over colours
        logger.exception("brand colour probe failed for %s", brand)
        return []
    return ident.colors[:2]


# --- theme resolution -------------------------------------------------------


# Decor that fits each preset's personality when no season sets one.
PERSONALITY_DECOR = {
    "bold_dark": "grain", "cinematic": "film_grain", "pop_meme": "confetti", "editorial": "none",
    "sport_energy": "stripes",
}
_NEAR_BLACK = "#0B0B0D"


def _image_generator_ready() -> bool:
    if settings.image_gen_provider == "cloudflare":
        return bool(settings.cloudflare_account_id and settings.cloudflare_api_token)
    if settings.image_gen_provider == "pollinations":
        return bool(getattr(settings, "pollinations_api_key", None))
    return False


def _legacy_theme(choice: ThemeChoice, brand_colors: list[str], season: str | None) -> Theme:
    """The original behaviour for a plain colour list: preset palette + brand accent."""
    name = choice.name if choice.name in PRESETS else "bold_dark"
    p = PRESETS[name]
    bg = p["bg"]
    candidates = [normalize_hex(c) for c in brand_colors or []]
    candidates = [c for c in candidates if c and not is_neutral(c)]
    hint = normalize_hex(choice.accent_hint)
    accent = candidates[0] if candidates else (hint if hint and not is_neutral(hint) else p["accent"])
    accent = ensure_contrast(accent, bg)
    accent_2 = p["accent_2"]
    if len(candidates) > 1 and _hue_distance(candidates[1], accent) > 0.06:
        accent_2 = candidates[1]
    text = ensure_contrast(p["text"], bg, 7.0)
    muted = ensure_contrast(p["muted"], bg)
    surface = p["surface"]
    if contrast(text, surface) < MIN_CONTRAST:
        surface = mix(surface, bg, 0.5)
    from app.services.brand_identity_season import get_season

    sea = get_season(season)
    hero = None
    prompt = " ".join((choice.hero_prompt or "").split())
    if prompt:
        extra = f", {sea.prompt}" if sea else ""
        hero = Visual(want="mock", brief=f"{prompt}{extra}, {HERO_STYLE.get(name, '')}".strip(", "))
    return Theme(
        name=name, bg=bg, surface=surface, accent=accent, accent_2=accent_2, text=text, muted=muted,
        heading_font=p["heading_font"], body_font=p["body_font"], hero_image=hero,
        season=sea.key if sea else None, decor=sea.decor if sea else PERSONALITY_DECOR.get(name, "none"),
    )


def resolve_theme(choice: ThemeChoice, brand_colors: "list[str] | BrandIdentity", season: str | None = None) -> Theme:
    """Theme from the brand's identity (or, for the old call, a preset + brand colours).

    - ``brand_colors`` as ``list[str]`` keeps the original behaviour: preset palette, brand accent.
    - As a ``BrandIdentity``, the theme is built from the brand: dark or light from the site
      (a bright brand colour on a light site still gets a dark deck, where it reads), a
      brand-tinted bg/surface, accent = primary brand colour, accent_2 = secondary, the brand's
      fonts mapped to Canva/Google fonts, motif, season decor, logo, and a hero that is the
      brand's product image or a season-flavoured AI mock. The preset only supplies the
      layout personality (casing, italics) and fallbacks.
    WCAG contrast (>= 4.5 for text and accent text) is enforced either way.
    ``season`` overrides the identity's detected season.
    """
    from app.services.brand_identity import BrandIdentity
    from app.services.brand_identity_season import get_season

    if not isinstance(brand_colors, BrandIdentity):
        return _legacy_theme(choice, list(brand_colors or []), season)
    ident = brand_colors
    name = choice.name if choice.name in PRESETS else "bold_dark"
    p = PRESETS[name]
    sea = get_season(season or ident.season)
    # A season named in the brief/research (or passed in) shapes decor and palette; one that only
    # comes from the calendar just flavours the image prompts.
    sea_full = bool(sea) and (bool(season) or ident.season_source != "calendar")

    colors = [c for c in (normalize_hex(x) for x in ident.colors) if c and not is_neutral(c)]
    hint = normalize_hex(choice.accent_hint)
    primary = colors[0] if colors else (hint if hint and not is_neutral(hint) else p["accent"])
    secondary = next((c for c in colors[1:] if _hue_distance(c, primary) > 0.06 or abs(luminance(c) - luminance(primary)) > 0.25), None)
    if secondary is None:
        secondary = sea.accents[0] if sea and sea_full else p["accent_2"]

    preset_dark = luminance(p["bg"]) < 0.35
    mode = ident.bg_mode or ("dark" if preset_dark else "light")
    light_bg = mix("#FFFFFF", primary, 0.07)
    if mode == "light" and contrast(primary, light_bg) < 2.6:
        mode = "dark"  # a bright brand colour (yellow, lime, cyan) only sings on dark

    if mode == "dark":
        site_bg = normalize_hex(ident.site_bg)
        base = site_bg if site_bg and luminance(site_bg) < 0.03 else _NEAR_BLACK
        bg = mix(base, primary, 0.08)
        surface = mix(bg, "#FFFFFF", 0.06)
        surface = mix(surface, primary, 0.06)
        text = "#FFFFFF"
        muted = mix("#FFFFFF", bg, 0.32)
    else:
        bg = light_bg
        surface = "#FFFFFF"
        text = mix("#121212", primary, 0.12)
        muted = mix(text, bg, 0.38)
    accent = ensure_contrast(primary, bg)
    text = ensure_contrast(text, bg, 7.0)
    muted = ensure_contrast(muted, bg)
    if contrast(text, surface) < MIN_CONTRAST:
        surface = mix(surface, bg, 0.5)

    if ident.font_source == "default":
        heading_font, body_font = p["heading_font"], p["body_font"]
    else:
        heading_font, body_font = ident.heading_font, ident.body_font

    motif = ", ".join(ident.motifs[:4])
    logo = None
    if ident.logo_url:
        logo = Visual(want="ref", brief=f"{ident.brand} logo", kind="ref", url=ident.logo_url,
                      source_url=ident.site_url, credit=f"Logo: {ident.site_url or 'brand'}")

    hero = None
    prompt = " ".join((choice.hero_prompt or "").split())
    if ident.product_images and not (prompt and _image_generator_ready()):
        hero = Visual(want="mock", brief=prompt or f"{ident.brand} product", kind="ref", url=ident.product_images[0],
                      source_url=ident.site_url, credit=f"Brand website: {ident.site_url or ''}".strip())
    elif prompt or sea or motif:
        parts = [prompt or f"{ident.brand} brand world"]
        if motif:
            parts.append(f"featuring {motif}")
        if sea:
            parts.append(sea.prompt)
        parts.append(HERO_STYLE.get(name, ""))
        hero = Visual(want="mock", brief=", ".join(x for x in parts if x))

    return Theme(
        name=name, bg=bg, surface=surface, accent=accent, accent_2=secondary, text=text, muted=muted,
        heading_font=heading_font, body_font=body_font, hero_image=hero, logo=logo, motif=motif,
        season=sea.key if sea else None,
        decor=sea.decor if sea and sea_full else PERSONALITY_DECOR.get(name, "none"),
        identity_source=ident.site_url,
    )
