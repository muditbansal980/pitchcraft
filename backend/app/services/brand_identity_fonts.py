"""Brand font -> Canva/Google font mapping (Agent B).

Brand websites use proprietary or self-hosted fonts (Zomato Sans, Gilroy,
Circular, Netflix Sans...). Decks must use fonts that Canva ships and that are
Google Fonts (so the PPTX imports cleanly), so every brand font is mapped to the
closest entry of a curated table, by exact name, by a table of known
proprietary fonts, and finally by a name-based classification.

Glyph widths for every font in ``CURATED`` live in ``assets/font_metrics.json``
(generated from the google/fonts repo), so text fitting works for all of them.
"""

import re
from dataclasses import dataclass
from typing import Literal

Category = Literal[
    "geometric", "humanist", "grotesk", "condensed", "slab", "serif", "didone", "rounded", "script", "display",
]


@dataclass(frozen=True)
class FontInfo:
    name: str
    category: Category
    bold: bool = True  # has a real bold weight (single-weight display faces must not be bolded)
    italic: bool = True  # has a real italic
    heading_ok: bool = True
    body_ok: bool = True


# All are Google Fonts available in Canva's font library.
CURATED: dict[str, FontInfo] = {f.name.lower(): f for f in [
    # geometric sans
    FontInfo("Montserrat", "geometric"),
    FontInfo("Poppins", "geometric"),
    FontInfo("Outfit", "geometric", italic=False),
    FontInfo("Plus Jakarta Sans", "geometric"),
    FontInfo("DM Sans", "geometric"),
    FontInfo("Urbanist", "geometric"),
    FontInfo("Lexend", "geometric", italic=False),
    FontInfo("Manrope", "geometric", italic=False),
    FontInfo("Sora", "geometric", italic=False),
    FontInfo("Raleway", "geometric"),
    FontInfo("Josefin Sans", "geometric"),
    # humanist sans
    FontInfo("Open Sans", "humanist"),
    FontInfo("Lato", "humanist"),
    FontInfo("Source Sans 3", "humanist"),
    FontInfo("Mulish", "humanist"),
    FontInfo("Work Sans", "humanist"),
    FontInfo("Noto Sans", "humanist"),
    FontInfo("Hind", "humanist", italic=False),
    # grotesk / neo-grotesk
    FontInfo("Inter", "grotesk"),
    FontInfo("Inter Tight", "grotesk"),
    FontInfo("Roboto", "grotesk"),
    FontInfo("Archivo", "grotesk"),
    FontInfo("Barlow", "grotesk"),
    FontInfo("IBM Plex Sans", "grotesk"),
    FontInfo("Karla", "grotesk"),
    FontInfo("Space Grotesk", "grotesk", italic=False),
    FontInfo("Rubik", "grotesk"),
    # condensed display
    FontInfo("Anton", "condensed", bold=False, italic=False, body_ok=False),
    FontInfo("Bebas Neue", "condensed", bold=False, italic=False, body_ok=False),
    FontInfo("Oswald", "condensed", italic=False, body_ok=False),
    FontInfo("Barlow Condensed", "condensed", body_ok=False),
    FontInfo("Fjalla One", "condensed", bold=False, italic=False, body_ok=False),
    FontInfo("Teko", "condensed", italic=False, body_ok=False),
    FontInfo("League Gothic", "condensed", bold=False, italic=False, body_ok=False),
    # heavy display
    FontInfo("Archivo Black", "display", bold=False, italic=False, body_ok=False),
    FontInfo("Bricolage Grotesque", "display", italic=False),
    FontInfo("Syne", "display", italic=False, body_ok=False),
    FontInfo("Righteous", "display", bold=False, italic=False, body_ok=False),
    # slab
    FontInfo("Roboto Slab", "slab", italic=False),
    FontInfo("Zilla Slab", "slab"),
    FontInfo("Arvo", "slab"),
    # serif / didone
    FontInfo("Playfair Display", "didone"),
    FontInfo("DM Serif Display", "didone", bold=False, body_ok=False),
    FontInfo("Bodoni Moda", "didone"),
    FontInfo("Abril Fatface", "didone", bold=False, italic=False, body_ok=False),
    FontInfo("Lora", "serif"),
    FontInfo("Merriweather", "serif"),
    FontInfo("Libre Baskerville", "serif"),
    FontInfo("Cormorant Garamond", "serif"),
    FontInfo("EB Garamond", "serif"),
    FontInfo("Fraunces", "serif"),
    # rounded
    FontInfo("Nunito", "rounded"),
    FontInfo("Quicksand", "rounded", italic=False),
    FontInfo("Comfortaa", "rounded", italic=False, body_ok=False),
    FontInfo("Fredoka", "rounded", italic=False),
    FontInfo("Baloo 2", "rounded", italic=False),
    # script / handwritten (headings only)
    FontInfo("Pacifico", "script", bold=False, italic=False, body_ok=False),
    FontInfo("Caveat", "script", italic=False, body_ok=False),
    FontInfo("Dancing Script", "script", italic=False, body_ok=False),
    FontInfo("Lobster", "script", bold=False, italic=False, body_ok=False),
    FontInfo("Kalam", "script", italic=False, body_ok=False),
    FontInfo("Permanent Marker", "script", bold=False, italic=False, body_ok=False),
]}

# Proprietary / self-hosted brand fonts -> nearest Google font (heading, body).
PROPRIETARY: dict[str, tuple[str, str]] = {
    "gilroy": ("Plus Jakarta Sans", "Plus Jakarta Sans"),
    "source sans pro": ("Source Sans 3", "Source Sans 3"),
    "open sans hebrew": ("Open Sans", "Open Sans"),
    "circular": ("DM Sans", "DM Sans"),
    "circular std": ("DM Sans", "DM Sans"),
    "spotify mix": ("DM Sans", "DM Sans"),
    "spotifymix": ("DM Sans", "DM Sans"),
    "proxima nova": ("Montserrat", "Montserrat"),
    "proximanova": ("Montserrat", "Montserrat"),
    "netflix sans": ("Bebas Neue", "Inter Tight"),
    "zomato sans": ("Plus Jakarta Sans", "Inter"),
    "okra": ("Plus Jakarta Sans", "Inter"),
    "metropolis": ("Montserrat", "Montserrat"),
    "avenir": ("Nunito", "Nunito"),
    "avenir next": ("Nunito", "Nunito"),
    "futura": ("Josefin Sans", "Montserrat"),
    "futura pt": ("Josefin Sans", "Montserrat"),
    "helvetica": ("Inter", "Inter"),
    "helvetica neue": ("Inter", "Inter"),
    "neue haas grotesk": ("Inter", "Inter"),
    "neue haas unica": ("Inter", "Inter"),
    "arial": ("Roboto", "Roboto"),
    "segoe ui": ("Open Sans", "Open Sans"),
    "sf pro": ("Inter", "Inter"),
    "sf pro display": ("Inter", "Inter"),
    "-apple-system": ("Inter", "Inter"),
    "system-ui": ("Inter", "Inter"),
    "blinkmacsystemfont": ("Inter", "Inter"),
    "graphik": ("Inter", "Inter"),
    "gotham": ("Montserrat", "Montserrat"),
    "brandon grotesque": ("Josefin Sans", "Lato"),
    "sofia pro": ("Outfit", "Outfit"),
    "tt norms": ("Outfit", "Outfit"),
    "tt commons": ("Outfit", "Outfit"),
    "euclid circular": ("DM Sans", "DM Sans"),
    "euclid circular a": ("DM Sans", "DM Sans"),
    "aeonik": ("Manrope", "Manrope"),
    "general sans": ("Manrope", "Manrope"),
    "satoshi": ("Outfit", "Outfit"),
    "clash display": ("Syne", "Inter"),
    "druk": ("Anton", "Inter"),
    "druk wide": ("Archivo Black", "Inter"),
    "knockout": ("Oswald", "Inter"),
    "trade gothic": ("Barlow Condensed", "Barlow"),
    "din": ("Barlow", "Barlow"),
    "din pro": ("Barlow", "Barlow"),
    "din next": ("Barlow", "Barlow"),
    "amazon ember": ("Inter", "Inter"),
    "bookerly": ("Merriweather", "Merriweather"),
    "nykaa": ("Plus Jakarta Sans", "Inter"),
    "noto sans display": ("Noto Sans", "Noto Sans"),
    "sharp grotesk": ("Space Grotesk", "Inter"),
    "gt walsheim": ("DM Sans", "DM Sans"),
    "gt america": ("Inter", "Inter"),
    "apercu": ("Work Sans", "Work Sans"),
    "brown": ("Outfit", "Outfit"),
    "larsseit": ("DM Sans", "DM Sans"),
    "cera pro": ("Outfit", "Outfit"),
    "museo sans": ("Nunito", "Nunito"),
    "museo": ("Zilla Slab", "Nunito"),
    "objektiv": ("Lexend", "Lexend"),
    "averta": ("DM Sans", "DM Sans"),
    "lufga": ("Urbanist", "Urbanist"),
    "recoleta": ("Fraunces", "DM Sans"),
    "canela": ("Cormorant Garamond", "Lato"),
    "didot": ("Bodoni Moda", "Lato"),
    "bodoni": ("Bodoni Moda", "Lato"),
    "baskerville": ("Libre Baskerville", "Lato"),
    "garamond": ("EB Garamond", "Lato"),
    "times new roman": ("Libre Baskerville", "Lato"),
    "georgia": ("Merriweather", "Lato"),
    "coca-cola": ("Lobster", "Montserrat"),
    "tcccunity": ("Montserrat", "Montserrat"),
}

# Category of a name we don't know, from hints in the name itself.
_HINTS: list[tuple[str, Category]] = [
    (r"condensed|compressed|narrow|gothic|impact|bebas|anton|oswald|knockout|druk", "condensed"),
    (r"slab|rockwell|clarendon|egyptian", "slab"),
    (r"didot|bodoni|playfair|vogue", "didone"),
    (r"serif|garamond|baskerville|times|georgia|caslon|minion|tiempos|lyon|canela|freight", "serif"),
    (r"script|hand|brush|marker|signature|cursive|pacifico|lobster", "script"),
    (r"round|nunito|comfortaa|quicksand|fredoka|baloo|varela", "rounded"),
    (r"mono|code|courier", "grotesk"),
    (r"grotesk|grotesque|helvetica|inter|roboto|arial|sf pro|neue", "grotesk"),
    (r"display|black|heavy|ultra|poster", "display"),
]

# Default heading/body pair per category of the brand's own font.
CATEGORY_PAIRS: dict[Category, tuple[str, str]] = {
    "geometric": ("Montserrat", "Montserrat"),
    "humanist": ("Lato", "Open Sans"),
    "grotesk": ("Inter Tight", "Inter"),
    "condensed": ("Oswald", "Barlow"),
    "slab": ("Roboto Slab", "Lato"),
    "serif": ("Lora", "Lato"),
    "didone": ("Playfair Display", "Montserrat"),
    "rounded": ("Nunito", "Nunito"),
    "script": ("Pacifico", "Poppins"),
    "display": ("Archivo Black", "Inter"),
}

_GENERIC = {"sans-serif", "serif", "monospace", "cursive", "fantasy", "inherit", "initial", "unset", "revert",
            "ui-sans-serif", "ui-serif", "ui-monospace", "emoji", "math", "fangsong", "var", "icons", "icon",
            "fontawesome", "font awesome", "material icons", "material symbols outlined", "glyphicons halflings",
            "swiper-icons", "slick", "apple color emoji", "segoe ui emoji", "segoe ui symbol", "noto color emoji"}


def clean_family(raw: str) -> str:
    """'"Gilroy-Bold", sans-serif' piece -> 'gilroy'; drops weights/styles/version suffixes."""
    name = raw.strip().strip("'\"").strip()
    name = re.sub(r"\s*!important\s*$", "", name)
    name = re.sub(r"[-_ ]?(thin|extralight|ultralight|light|regular|book|medium|semibold|demibold|bold|extrabold|"
                  r"ultrabold|black|heavy|italic|oblique|variable|vf|webfont|web|woff2?|\d{3})$", "", name,
                  flags=re.IGNORECASE)
    name = re.sub(r"[-_ ]?(thin|light|regular|medium|semibold|bold|black|italic)$", "", name, flags=re.IGNORECASE)
    name = re.sub(r"[_]+", " ", name)
    name = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", name)  # ProximaNova -> Proxima Nova
    return " ".join(name.split()).lower()


def is_generic(name: str) -> bool:
    n = name.lower()
    return not n or n in _GENERIC or n.startswith("var(") or "icon" in n or "awesome" in n or "emoji" in n


def classify(name: str) -> Category | None:
    n = name.lower()
    if n in CURATED:
        return CURATED[n].category
    for pattern, cat in _HINTS:
        if re.search(pattern, n):
            return cat
    return None


def _lookup(name: str) -> tuple[str, str] | None:
    n = clean_family(name)
    if not n or is_generic(n):
        return None
    if n in CURATED:
        f = CURATED[n]
        body = f.name if f.body_ok else CATEGORY_PAIRS[f.category][1]
        return f.name, body
    if n in PROPRIETARY:
        return PROPRIETARY[n]
    squashed = n.replace(" ", "")
    for key, pair in PROPRIETARY.items():
        if key.replace(" ", "") == squashed or squashed.startswith(key.replace(" ", "")):
            return pair
    for key, f in CURATED.items():
        if key.replace(" ", "") == squashed:
            return f.name, (f.name if f.body_ok else CATEGORY_PAIRS[f.category][1])
    for key, f in sorted(CURATED.items(), key=lambda kv: -len(kv[0])):  # "Our Lexend", "Brand Poppins"
        if len(key) >= 5 and re.search(rf"(^|\s){re.escape(key)}(\s|$)", n):
            return f.name, (f.name if f.body_ok else CATEGORY_PAIRS[f.category][1])
    return None


def map_fonts(raw_fonts: list[str], *, category_hint: Category | None = None) -> tuple[str, str, str]:
    """Brand font names (most important first) -> (heading, body, how) using curated Google fonts.

    ``how`` says which rule matched ("exact", "proprietary", "classified", "hint", "default").
    """
    heading = body = None
    how = "default"
    mapped: list[tuple[str, str, str]] = []  # (heading, body, how) per distinct brand font, in order
    for raw in raw_fonts:
        pair = _lookup(raw)
        if pair and all(pair[0] != m[0] for m in mapped):
            mapped.append((pair[0], pair[1], "exact" if clean_family(raw) in CURATED else "proprietary"))
        if len(mapped) >= 3:
            break
    if mapped:
        # A display face among the brand's top fonts (condensed, didone, script, heavy) is the
        # headline font; the first text face is the body font.
        display = next((m for m in mapped if font_info(m[0]).category in ("condensed", "display", "didone", "script")
                        and not font_info(m[0]).body_ok), None)
        first = mapped[0]
        if display:
            heading, how = display[0], display[2]
            texty = [m for m in mapped if font_info(m[0]).body_ok and m[0] != heading]
            body = texty[0][0] if texty else display[1]
        else:
            heading, body, how = first
        if not font_info(body).body_ok:
            body = CATEGORY_PAIRS[font_info(heading).category][1]
    if heading is None:
        for raw in raw_fonts:
            n = clean_family(raw)
            if is_generic(n):
                continue
            cat = classify(n)
            if cat:
                heading, body = CATEGORY_PAIRS[cat]
                how = "classified"
                break
    if heading is None and category_hint:
        heading, body = CATEGORY_PAIRS[category_hint]
        how = "hint"
    if heading is None:
        heading, body, how = "Montserrat", "Montserrat", "default"
    return heading, body or heading, how


def font_info(name: str) -> FontInfo:
    return CURATED.get((name or "").lower()) or FontInfo(name or "", classify(name or "") or "grotesk")
