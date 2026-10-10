"""PPTX renderer (WORKPLAN C7). Owner: Agent B.

``render_deck`` turns a ``Deck`` into a themed 16:9 .pptx (python-pptx), one
builder per slide layout. It only reads images from the local media store
(``media.resolve_media``) and never touches the network. Sync and CPU-bound:
call it via ``asyncio.to_thread`` from async code.

The look follows the WLDD decks (PC02 Avvatar, PC12 Traitors, PC11 Netflix):
dark 16:9 slides, one accent colour, big condensed headlines, an accent
"highlight strip" under headlines, darkened hero photos behind statements and
dividers, labelled chips on idea slides, framed visuals and native, editable
tables. Everything is driven by ``deck.theme``; every builder tolerates empty
fields, and a builder that still fails falls back to a plain title + text slide.
"""

import io
import logging
import math
from dataclasses import dataclass, field

from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE
from pptx.util import Inches, Pt

from app.schemas.deck import (
    CardGridSlide,
    ChipsSlide,
    CoverSlide,
    Deck,
    DividerSlide,
    FinSlide,
    FormulaSlide,
    FromToSlide,
    IdeaSlide,
    KpiTableSlide,
    RefSlide,
    SamplesSlide,
    StatementSlide,
    TableSlide,
    Theme,
    TimelineSlide,
    TwoColumnSlide,
    Visual,
)
from app.services.brand_identity_fonts import font_info
from app.services.deck_render_decor import draw_decor
from app.services.deck_render_kit import (
    Run,
    box,
    clip_lines,
    clip_to_box,
    fit_or_clip,
    fit_text,
    measure,
    picture,
    rgb,
    set_gradient,
    set_line,
    set_notes,
    set_table_style,
    table_cell_style,
    text,
    wrap_count,
)
from app.services.media import resolve_media
from app.services.themes import contrast, luminance, mix, on_color

logger = logging.getLogger(__name__)

PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"

W, H = 13.333, 7.5
MX = 0.75  # side margin
CONTENT_W = W - 2 * MX
FOOT = 0.7  # space kept free at the bottom for the footer

# Display faces with a single weight: asking for bold makes renderers smear a fake bold.
_SINGLE_WEIGHT = {"anton", "bebas neue", "archivo black"}


# --- look -------------------------------------------------------------------


@dataclass
class Look:
    t: Theme
    dark: bool
    upper: bool
    italic: bool
    head_bold: bool
    head_color: str
    on_accent: str
    line: str  # hairlines / card borders
    head_spacing: float
    glow: bool

    @property
    def hf(self) -> str:
        return self.t.heading_font

    @property
    def bf(self) -> str:
        return self.t.body_font


def make_look(theme: Theme) -> Look:
    dark = luminance(theme.bg) < 0.35
    name = theme.name
    head = theme.accent if name in ("bold_dark", "sport_energy") else theme.text
    if contrast(head, theme.bg) < 4.5:
        head = theme.text
    hinfo = font_info(theme.heading_font)
    script = hinfo.category == "script"
    return Look(
        t=theme,
        dark=dark,
        # Brand script/serif faces keep their own casing; the preset sets it otherwise.
        upper=name in ("bold_dark", "cinematic", "sport_energy", "pop_meme") and not script
        and hinfo.category not in ("serif",),
        italic=name in ("bold_dark", "sport_energy") and not script and hinfo.category not in ("serif", "didone"),
        head_bold=hinfo.bold and (theme.heading_font or "").strip().lower() not in _SINGLE_WEIGHT,
        head_color=head,
        on_accent=on_color(theme.accent),
        line=mix(theme.surface, theme.accent, 0.45) if dark else mix(theme.surface, theme.text, 0.18),
        head_spacing=1.12 if name == "editorial" else 1.0,
        glow=dark,
    )


@dataclass
class Ctx:
    deck: Deck
    look: Look
    index: int
    total: int
    credits: list[str] = field(default_factory=list)
    prev_hero: bool = False  # the previous slide used the hero image
    used_hero: bool = False
    strong_decor: bool = False  # cover/divider/fin show the full decor pattern


# --- shared pieces ----------------------------------------------------------


def _background(slide, ctx: Ctx, *, glow_side: str = "right") -> None:
    lk, t = ctx.look, ctx.look.t
    slide.background.fill.solid()
    slide.background.fill.fore_color.rgb = rgb(t.bg)
    if lk.glow:
        g = box(slide, 0, 0, W, H, t.bg)
        ang = 0 if glow_side == "right" else 180
        set_gradient(g, [(0, t.bg, 0.0), (0.45, t.bg, 0.0), (1, mix(t.bg, t.accent, 0.42), 0.55)], ang)
        v = box(slide, 0, 0, W, H, t.bg)
        set_gradient(v, [(0, t.bg, 0.0), (0.6, t.bg, 0.0), (1, t.bg, 0.65)], 90)
    if t.decor and t.decor != "none":
        draw_decor(slide, t, strong=ctx.strong_decor, seed=ctx.index)
    elif t.name == "sport_energy":
        box(slide, W - 4.4, -0.2, 2.4, H + 0.4, t.accent, alpha=0.07, shape=MSO_SHAPE.PARALLELOGRAM, adj=0.45)
        box(slide, W - 2.6, -0.2, 0.5, H + 0.4, t.accent_2, alpha=0.06, shape=MSO_SHAPE.PARALLELOGRAM, adj=0.45)
    elif t.name == "pop_meme":
        box(slide, W - 1.45, -1.35, 2.7, 2.7, t.accent_2, shape=MSO_SHAPE.OVAL)
        box(slide, -0.55, H - 0.55, 1.0, 1.0, t.accent, alpha=0.9, shape=MSO_SHAPE.OVAL)
    elif t.name == "editorial":
        box(slide, MX, H - 0.62, CONTENT_W, 0.012, t.text, alpha=0.35)


def _hero_background(slide, ctx: Ctx, overlay: float, *, optional: bool = False) -> bool:
    """Full-bleed hero image under a bg-coloured overlay. False if there is no hero image.

    ``optional`` slides (statements, From/To) skip the hero right after another hero slide,
    so a run of statements alternates photo and glow backgrounds instead of repeating one image.
    """
    if optional and ctx.prev_hero:
        return False
    hero = ctx.deck.theme.hero_image
    path = resolve_media(hero.url) if hero and hero.kind != "placeholder" else None
    if not path:
        return False
    t = ctx.look.t
    slide.background.fill.solid()
    slide.background.fill.fore_color.rgb = rgb(t.bg)
    pic, _ = picture(slide, path, 0, 0, W, H, mode="cover", top_bias=0.5)
    if pic is None:
        return False
    if overlay > 0:
        if not ctx.look.dark:
            overlay = max(overlay, 0.8)  # light themes need more wash for dark text to read
        box(slide, 0, 0, W, H, t.bg, alpha=overlay)
    if t.decor and t.decor not in ("none", "grain"):
        draw_decor(slide, t, strong=False, seed=ctx.index)
    if hero.credit:
        ctx.credits.append(f"Background: {hero.credit}")
    ctx.used_hero = True
    return True


def _logo_stats(path) -> tuple[float, float] | None:
    """(mean luminance of the logo's opaque pixels, share of opaque pixels)."""
    try:
        from PIL import Image

        with Image.open(path) as img:
            rgba = img.convert("RGBA")
            rgba.thumbnail((96, 96))
            px = [p for p in rgba.getdata() if p[3] > 60]
            if not px:
                return None
            lum = sum((0.2126 * r + 0.7152 * g + 0.0722 * b) / 255 for r, g, b, _ in px) / len(px)
            return lum, len(px) / (rgba.width * rgba.height)
    except Exception:  # noqa: BLE001 - a bad logo file is just skipped
        return None


def _logo(slide, ctx: Ctx, x: float, y: float, max_w: float, max_h: float, *, align: str = "left") -> float | None:
    """Brand logo from the media store; on a contrasting chip if it would vanish. Returns its width."""
    lg = ctx.deck.theme.logo
    path = resolve_media(lg.url) if lg is not None else None
    if not path:
        return None
    from app.services.deck_render_kit import image_size

    size = image_size(path)
    stats = _logo_stats(path)
    if not size or not stats:
        return None
    iw, ih = size
    w = min(max_w, max_h * iw / ih)
    h = w * ih / iw
    if align == "center":
        x = x + (max_w - w) / 2
    t = ctx.look.t
    r, g, b = (int(t.bg[i:i + 2], 16) for i in (1, 3, 5))
    bg_light = (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255
    logo_l, opaque = stats
    # A dark logo on a dark slide (or light on light) sits on a contrasting rounded chip;
    # a logo that carries its own opaque background needs none.
    if abs(logo_l - bg_light) < 0.3 and opaque < 0.92:
        chip = "#FFFFFF" if logo_l < 0.5 else "#111111"
        pad = 0.12
        box(slide, x - pad, y - pad, w + 2 * pad, h + 2 * pad, chip, radius=0.1)
    slide.shapes.add_picture(str(path), Inches(x), Inches(y), Inches(w), Inches(h))
    return w


def _footer(slide, ctx: Ctx) -> None:
    lk, t = ctx.look, ctx.look.t
    text(slide, MX, H - 0.46, 6.0, 0.22, (ctx.deck.brand or "").upper()[:60], font=lk.bf, size=8,
         color=t.muted, bold=True, tracking=1.5)
    text(slide, W - MX - 1.5, H - 0.46, 1.5, 0.22, f"{ctx.index:02d}", font=lk.bf, size=8,
         color=t.muted, bold=True, align="right", tracking=1.5)


def _headline_runs(title: str, lk: Look) -> list[list[Run]]:
    t = lk.t
    cut = title.find(":")
    if 0 < cut <= 32 and cut < len(title) - 1:
        rest = title[cut + 1:].strip()
        rest_color = t.text if lk.head_color == t.accent else lk.head_color
        return [[Run(title[: cut + 1] + " ", t.accent), Run(rest, rest_color)]]
    return [[Run(title, lk.head_color)]]


def _head_fit(ctx: Ctx, title: str, w: float, h: float, max_pt: float, min_pt: float, max_lines: int | None):
    lk = ctx.look
    return fit_text(title, w, h, lk.hf, max_pt=max_pt, min_pt=min_pt, upper=lk.upper, bold=lk.head_bold,
                    spacing=lk.head_spacing, max_lines=max_lines)


def _headline(slide, ctx: Ctx, title: str, x: float, y: float, w: float, h: float, *, max_pt: float,
              min_pt: float = 26, align: str = "left", anchor: str = "top", max_lines: int | None = None) -> float:
    """Draw a headline; return the bottom y of the text actually used."""
    lk = ctx.look
    title = (title or "").strip()
    if not title:
        return y
    f = _head_fit(ctx, title, w, h, max_pt, min_pt, max_lines)
    used = min(h, f.height + 0.06)
    top = y if anchor == "top" else (y + h - used if anchor == "bottom" else y + (h - used) / 2)
    text(slide, x, top, w, used, _headline_runs(title, lk), font=lk.hf, size=f.size, color=lk.head_color,
         bold=lk.head_bold, italic=lk.italic, align=align, spacing=lk.head_spacing, upper=lk.upper)
    return top + used


def _strip(slide, ctx: Ctx, label: str, x: float, y: float, max_w: float, *, align: str = "left",
           size: float = 15) -> float:
    """Accent highlight strip with a subtitle; return its bottom y."""
    lk, t = ctx.look, ctx.look.t
    label = (label or "").strip()
    if not label:
        return y
    pad_l, pad_r = 0.32, (0.32 if align == "center" else 0.8)
    inner_max = max_w - pad_l - pad_r
    f = fit_text(label, inner_max, 0.9, lk.bf, max_pt=size, min_pt=11, spacing=1.25, max_lines=2)
    one_line = measure(label, f.size, lk.bf)
    inner_w = min(inner_max, one_line + 0.05) if f.lines == 1 else inner_max
    h = f.height + 0.2
    sw = inner_w + pad_l + pad_r
    sx = x if align == "left" else x + (max_w - sw) / 2
    shp = box(slide, sx, y, sw, h, t.accent, shape=MSO_SHAPE.PARALLELOGRAM, adj=0.18)
    if align == "left":
        fade = (inner_w + pad_l + 0.2) / sw
        set_gradient(shp, [(0, t.accent, 1), (min(0.98, fade), t.accent, 1), (1, t.accent, 0.0)], 0)
    text(slide, sx + pad_l, y + 0.1, inner_w + 0.05, f.height + 0.02, label, font=lk.bf, size=f.size,
         color=lk.on_accent, align="left" if align == "left" else "center", spacing=1.25)
    return y + h


def _title_block(slide, ctx: Ctx, title: str, strip: str = "", *, w: float = CONTENT_W, max_pt: float = 46,
                 y: float = 0.55, h: float = 1.45, align: str = "left", max_lines: int = 2) -> float:
    """Top-left (or centred) headline + strip; return the y where content may start."""
    x = MX if align == "left" else (W - w) / 2
    if ctx.look.t.name == "pop_meme" and align == "left":
        w = min(w, CONTENT_W - 0.9)  # keep clear of the corner sticker
    bottom = _headline(slide, ctx, title, x, y, w, h, max_pt=max_pt, min_pt=26, align=align, max_lines=max_lines)
    if ctx.look.t.name == "cinematic" and align == "left" and title:
        box(slide, MX - 0.24, y + 0.04, 0.07, max(0.3, bottom - y - 0.08), ctx.look.t.accent)
    if strip:
        bottom = _strip(slide, ctx, strip, x, bottom + 0.16, min(w, 9.5) if align == "left" else w, align=align)
    return bottom + 0.32


def _visual(slide, ctx: Ctx, v: Visual | None, x: float, y: float, w: float, h: float, *, mode: str = "cover",
            frame: float = 2.0, tag: str | None = None, credit: bool = True) -> tuple[float, float, float, float]:
    """Image from the media store in an accent frame, or a styled "Sample visual" placeholder."""
    lk, t = ctx.look, ctx.look.t
    path = resolve_media(v.url) if v is not None and v.kind != "placeholder" else None
    rect = (x, y, w, h)
    if path:
        pic, rect = picture(slide, path, x, y, w, h, mode=mode)
        if pic is None:
            path = None
        else:
            if frame:
                set_line(pic, t.accent, frame)
            if v.credit or v.source_url:
                ctx.credits.append(" | ".join(p for p in (v.credit, v.source_url) if p))
    if not path:
        rect = (x, y, w, h)
        ph = box(slide, x, y, w, h, t.surface, line=t.accent, line_w=1.25, dash=True)
        ph.name = "Sample visual placeholder"
        brief = ((v.brief or v.query) if v is not None else "") or "Visual to be designed"
        inner = w - 0.5
        small = w < 2.6 or h < 1.6
        label_size = 9 if small else 11
        brief, f = fit_or_clip(brief, inner, max(0.25, h - 0.75), lk.bf, max_pt=11 if small else 14, min_pt=8,
                               spacing=1.3)
        block = 0.38 + f.height
        top = y + max(0.2, (h - block) / 2)
        text(slide, x + 0.25, top, inner, 0.3, "SAMPLE VISUAL", font=lk.bf, size=label_size, color=t.accent,
             bold=True, tracking=1.5, align="center")
        text(slide, x + 0.25, top + 0.38, inner, min(h - 0.6, f.height + 0.1), brief, font=lk.bf, size=f.size,
             color=t.muted, italic=True, align="center", spacing=1.3)
    if tag:
        rx, ry, rw, _ = rect
        tw = measure(tag, 9, lk.bf, upper=True, bold=True) + 0.3
        box(slide, rx + rw - tw - 0.1, ry + 0.1, tw, 0.28, t.accent, radius=0.06)
        text(slide, rx + rw - tw - 0.1, ry + 0.1, tw, 0.28, tag, font=lk.bf, size=9, color=lk.on_accent,
             bold=True, align="center", anchor="middle", upper=True, tracking=1)
    if credit and path and v is not None and v.credit:
        rx, ry, rw, rh = rect
        if ry + rh + 0.26 < H - 0.45:
            text(slide, rx, ry + rh + 0.06, rw, 0.2, v.credit[:80], font=lk.bf, size=7.5, color=t.muted,
                 align="center")
    return rect


def _pill_w(item: str, size: float, font: str) -> float:
    return measure(item, size, font) + 2 * (size / 72 * 1.1)


def _pill_rows(items: list[str], w: float, font: str, size: float) -> float:
    items = [i for i in items if i]
    if not items:
        return 0.0
    ph, gap = size / 72 * 1.9, 0.1
    rows, cx = 1, 0.0
    for item in items:
        pw = min(w, _pill_w(item, size, font))
        if cx > 0 and cx + pw > w:
            rows, cx = rows + 1, 0.0
        cx += pw + gap
    return rows * ph + (rows - 1) * gap


def _pills(slide, ctx: Ctx, items: list[str], x: float, y: float, w: float, *, size: float = 10.5,
           fill: str | None = None, color: str | None = None, max_h: float = 9.0) -> float:
    """Wrap rounded "chip" labels left-to-right; return the height used."""
    lk, t = ctx.look, ctx.look.t
    ph, gap = size / 72 * 1.9, 0.1
    cx, cy = x, y
    items = [i for i in items if i]
    for item in items:
        pw = min(w, _pill_w(item, size, lk.bf))
        if pw >= w - 1e-6:
            item = clip_lines(item, w - 2 * (size / 72 * 1.1), size, lk.bf, 1)
        if cx > x and cx + pw > x + w:
            cx, cy = x, cy + ph + gap
        if cy + ph - y > max_h + 0.01:
            break
        box(slide, cx, cy, pw, ph, fill, line=t.accent, line_w=1.0, radius=ph / 2)
        text(slide, cx, cy, pw, ph, item, font=lk.bf, size=size, color=color or t.text, align="center",
             anchor="middle", spacing=1.0, wrap=False)
        cx += pw + gap
    return (cy + ph - y) if items else 0.0


def _body(slide, ctx: Ctx, content: str, x: float, y: float, w: float, h: float, *, max_pt: float = 18,
          min_pt: float = 11, align: str = "left", color: str | None = None, italic: bool = False,
          anchor: str = "top", lead: bool = False) -> float:
    """Body text fitted to its box; ``lead`` puts the first sentence in the accent colour. Returns bottom y."""
    lk, t = ctx.look, ctx.look.t
    content = (content or "").strip()
    if not content or h <= 0.1:
        return y
    runs: str | list[list[Run]] = content
    measured = content
    if lead:
        cut = next((i + 1 for i, ch in enumerate(content) if ch in ".?!" and 0 < i < len(content) - 2), -1)
        if 0 < cut < len(content) * 0.75:
            first, rest = content[:cut].strip(), content[cut:].strip()
            runs = [[Run(first, t.accent, bold=True)], [Run(rest)]]
            measured = first + "\n" + rest
    f = fit_text(measured, w, h, lk.bf, max_pt=max_pt, min_pt=min_pt, spacing=1.32, bold=lead)
    if f.height > h + 1e-6:  # still too long at the minimum size: clip instead of overflowing
        runs, f = fit_or_clip(content, w, h, lk.bf, max_pt=f.size, min_pt=f.size, spacing=1.32)
    used = min(h, f.height + 0.05)
    top = y if anchor == "top" else y + (h - used) / 2
    text(slide, x, top, w, used, runs, font=lk.bf, size=f.size, color=color or t.text, align=align,
         italic=italic, spacing=1.32)
    return top + used


# --- layout builders --------------------------------------------------------


def build_cover(slide, s: CoverSlide, ctx: Ctx) -> None:
    lk, t = ctx.look, ctx.look.t
    if _hero_background(slide, ctx, overlay=0.0):
        g = box(slide, 0, 0, W, H, t.bg)
        set_gradient(g, [(0, t.bg, 0.96), (0.5, t.bg, 0.8), (1, t.bg, 0.2)], 0)
    else:
        _background(slide, ctx)
        ghost = ((ctx.deck.brand or s.title or "").split() or [""])[0].upper()[:12]
        if ghost:
            per_pt = measure(ghost, 1, lk.hf, upper=True, bold=lk.head_bold)
            size = max(120.0, min(300.0, 8.6 / max(per_pt, 1e-3)))
            gw = per_pt * size
            text(slide, W - gw - 0.1, H - size / 72 * 1.05 - 0.15, gw + 1.0, size / 72 * 1.1, ghost, font=lk.hf,
                 size=size, color=mix(t.bg, t.accent, 0.12 if lk.dark else 0.1), bold=lk.head_bold,
                 italic=lk.italic, spacing=1.0, wrap=False)
    brand = (ctx.deck.brand or "").upper()[:60]
    if _logo(slide, ctx, MX, 0.6, 2.6, 0.8) is None and brand:
        box(slide, MX, 0.95, 0.55, 0.06, t.accent)
        text(slide, MX, 1.12, 8, 0.35, brand, font=lk.bf, size=14, color=t.accent if lk.dark else t.text,
             bold=True, tracking=3)
    bottom = _headline(slide, ctx, s.title, MX, 1.75, 8.8, 3.25, max_pt=110, min_pt=40, anchor="bottom",
                       max_lines=3)
    bottom = _strip(slide, ctx, s.subtitle, MX, bottom + 0.25, 8.8, size=17)
    if s.tagline:
        text(slide, MX, max(bottom + 0.35, 5.9), 8.0, 0.45, s.tagline, font=lk.bf, size=16, color=t.text,
             bold=True, italic=True)


def build_statement(slide, s: StatementSlide, ctx: Ctx) -> None:
    lk = ctx.look
    if not _hero_background(slide, ctx, overlay=0.8, optional=True):
        _background(slide, ctx)
    hw = 11.3
    hf = _head_fit(ctx, s.title, hw, 2.4, 76, 34, 2)
    body_f = fit_text(s.body, 9.6, 2.3, lk.bf, max_pt=22, min_pt=13, spacing=1.32) if s.body else None
    total = hf.height + (0.8 if s.strip else 0) + ((body_f.height + 0.75) if body_f else 0)
    y = max(0.7, (H - total) / 2 - 0.2)
    bottom = _headline(slide, ctx, s.title, (W - hw) / 2, y, hw, hf.height + 0.08, max_pt=hf.size,
                       min_pt=hf.size, align="center")
    if s.strip:
        bottom = _strip(slide, ctx, s.strip, (W - 10.6) / 2, bottom + 0.2, 10.6, align="center", size=16)
    if s.body:
        _body(slide, ctx, s.body, (W - 9.6) / 2, bottom + 0.45, 9.6, H - bottom - 1.2, max_pt=22, min_pt=13,
              align="center", lead=True)
    _footer(slide, ctx)


def build_from_to(slide, s: FromToSlide, ctx: Ctx) -> None:
    lk, t = ctx.look, ctx.look.t
    if not _hero_background(slide, ctx, overlay=0.86, optional=True):
        _background(slide, ctx)
    y = _title_block(slide, ctx, s.title, s.strip, w=11.0, max_pt=66, y=0.8, h=1.5, align="center", max_lines=1)
    cw, gap = 5.15, 1.2
    x0 = (W - (2 * cw + gap)) / 2
    cy = max(y + 0.25, 3.25)
    ch = min(2.8, H - cy - 0.9)
    for i, (label, body) in enumerate((("From", s.from_text), ("To", s.to_text))):
        x = x0 + i * (cw + gap)
        to = i == 1
        box(slide, x, cy, cw, ch, t.surface, line=t.accent if to else lk.line, line_w=2.25 if to else 1.0,
            radius=0.14)
        text(slide, x + 0.4, cy + 0.3, cw - 0.8, 0.45, label, font=lk.hf, size=26,
             color=t.accent if to else t.muted, bold=lk.head_bold, italic=lk.italic, upper=lk.upper, tracking=1)
        quote = (body or "").strip()
        quote, f = fit_or_clip(quote, cw - 0.8, ch - 1.2, lk.bf, max_pt=24 if to else 21, min_pt=12, spacing=1.3,
                               bold=to)
        text(slide, x + 0.4, cy + 0.95, cw - 0.8, ch - 1.15, quote, font=lk.bf, size=f.size,
             color=t.text if to else mix(t.text, t.bg, 0.25), bold=to, italic=True, spacing=1.3)
    box(slide, x0 + cw + 0.28, cy + ch / 2 - 0.32, gap - 0.56, 0.64, t.accent, shape=MSO_SHAPE.RIGHT_ARROW, adj=0.5)
    _footer(slide, ctx)


def build_formula(slide, s: FormulaSlide, ctx: Ctx) -> None:
    lk, t = ctx.look, ctx.look.t
    _background(slide, ctx)
    y = _title_block(slide, ctx, s.title, "", max_pt=56)
    cards = [p for p in s.parts if p.label or p.text] + [s.result]
    n = len(cards)
    op_w = 0.7
    cw = (CONTENT_W - op_w * (n - 1)) / n
    cy = max(y + 0.15, 2.3)
    ch = min(3.9, H - cy - 1.0)
    inner = cw - 0.6
    label_fits = [_head_fit(ctx, c.label, inner, 1.0, 30, 16, 2) for c in cards]
    lsize = min(f.size for f in label_fits)
    l_h = max(wrap_count(c.label, inner, lsize, lk.hf, bold=lk.head_bold, upper=lk.upper) for c in cards) * lsize / 72
    bsize = min(fit_text(c.text, inner, ch - l_h - 0.95, lk.bf, max_pt=18, min_pt=11, spacing=1.32).size
                for c in cards)
    # Cards hug their content and the row sits centred in the space under the title.
    b_h = max(wrap_count(c.text, inner, bsize, lk.bf) for c in cards) * bsize * 1.32 / 72
    area = H - FOOT - cy
    ch = min(ch, max(2.4, l_h + b_h + 1.15))
    cy += max(0.0, (area - ch) / 2 - 0.2)
    for i, c in enumerate(cards):
        x = MX + i * (cw + op_w)
        res = i == n - 1
        fg = lk.on_accent if res else t.text
        box(slide, x, cy, cw, ch, t.accent if res else t.surface, line=None if res else lk.line, radius=0.14)
        text(slide, x + 0.3, cy + 0.32, inner, l_h + 0.05, c.label, font=lk.hf, size=lsize,
             color=fg if res else t.accent, bold=lk.head_bold, italic=lk.italic, upper=lk.upper, spacing=1.0)
        body = clip_to_box(c.text, inner, ch - l_h - 0.8, bsize, lk.bf, spacing=1.32)
        text(slide, x + 0.3, cy + 0.32 + l_h + 0.28, inner, ch - l_h - 0.8, body, font=lk.bf, size=bsize,
             color=fg, spacing=1.32)
        if i < n - 1:
            text(slide, x + cw, cy + ch / 2 - 0.4, op_w, 0.8, "=" if i == n - 2 else "+", font=lk.bf, size=40,
                 color=t.accent, bold=True, align="center", anchor="middle", spacing=1.0)
    _footer(slide, ctx)


def build_card_grid(slide, s: CardGridSlide, ctx: Ctx) -> None:
    lk, t = ctx.look, ctx.look.t
    _background(slide, ctx)
    y = _title_block(slide, ctx, s.title, s.strip, max_pt=46)
    cards = list(s.cards)
    n = len(cards)
    gap = 0.28
    cw = (CONTENT_W - gap * (n - 1)) / n
    cy = max(y, 2.25)
    ch = H - cy - FOOT
    pad = 0.22
    inner = cw - 2 * pad
    has_visual = any(c.visual is not None for c in cards)
    img_h = min(1.9, ch * 0.36) if has_visual else 0.0
    tag_size = 10.0
    max_tag_h = 2 * tag_size / 72 * 1.9 + 0.1
    tag_lists = []
    for c in cards:  # at most two rows of tags per card
        tags = [tg for tg in c.tags if tg]
        while tags and _pill_rows(tags, inner, lk.bf, tag_size) > max_tag_h + 1e-6:
            tags.pop()
        tag_lists.append(tags)
    tag_h = max((_pill_rows(tg, inner, lk.bf, tag_size) for tg in tag_lists), default=0.0)
    num_size = 26
    num_h = num_size / 72 + 0.08
    tsize = min(_head_fit(ctx, c.title, inner, 0.8, 21, 14, 2).size for c in cards)
    t_lines = max(wrap_count(c.title, inner, tsize, lk.hf, bold=lk.head_bold, upper=lk.upper) for c in cards)
    t_h = t_lines * tsize * 1.05 / 72
    fixed = pad * 2 + num_h + t_h + 0.14 + (tag_h + 0.16 if tag_h else 0)
    if has_visual:
        # Give the body room for ~11.5pt text before giving the image its full height.
        want_body = max((wrap_count(c.body, inner, 11.5, lk.bf) for c in cards if c.body), default=0) * 11.5 * 1.3 / 72
        img_h = max(1.0, min(img_h, ch - fixed - 0.16 - want_body))
    used = fixed + (img_h + 0.16 if has_visual else 0)
    body_room = max(0.3, ch - used)
    bsize = min((fit_text(c.body, inner, body_room, lk.bf, max_pt=14, min_pt=9.5, spacing=1.3).size
                 for c in cards if c.body), default=12)
    # Short cards hug their content instead of leaving tall empty boxes.
    body_need = max((wrap_count(c.body, inner, bsize, lk.bf) for c in cards if c.body), default=0) * bsize * 1.3 / 72
    if body_need + 0.25 < body_room:
        ch = max(min(ch, 2.6), used + body_need + 0.25)
        body_room = ch - used
    for i, c in enumerate(cards):
        x = MX + i * (cw + gap)
        box(slide, x, cy, cw, ch, t.surface, line=lk.line, line_w=1.0, radius=0.12)
        yy = cy + pad
        if has_visual:
            _visual(slide, ctx, c.visual, x + pad, yy, inner, img_h, frame=0, credit=False)
            yy += img_h + 0.16
        text(slide, x + pad, yy, inner, num_h, f"{i + 1:02d}", font=lk.hf, size=num_size, color=t.accent,
             bold=lk.head_bold, italic=lk.italic, spacing=1.0)
        yy += num_h
        text(slide, x + pad, yy, inner, t_h + 0.05, c.title, font=lk.hf, size=tsize, color=t.text,
             bold=lk.head_bold, italic=lk.italic, upper=lk.upper, spacing=1.05)
        yy += t_h + 0.14
        if c.body:
            body = clip_to_box(c.body, inner, body_room, bsize, lk.bf, spacing=1.3)
            text(slide, x + pad, yy, inner, body_room, body, font=lk.bf, size=bsize,
                 color=mix(t.text, t.bg, 0.1), spacing=1.3)
        if tag_lists[i]:
            _pills(slide, ctx, tag_lists[i], x + pad, cy + ch - pad - tag_h, inner, size=tag_size)
    _footer(slide, ctx)


def build_divider(slide, s: DividerSlide, ctx: Ctx) -> None:
    lk, t = ctx.look, ctx.look.t
    if not _hero_background(slide, ctx, overlay=0.6):
        _background(slide, ctx, glow_side="left")
    hf = _head_fit(ctx, s.title, 11.5, 2.6, 100, 40, 2)
    sub_f = fit_text(s.subtitle, 9.0, 1.0, lk.bf, max_pt=20, min_pt=13, spacing=1.32) if s.subtitle else None
    total = hf.height + ((sub_f.height + 0.3) if sub_f else 0)
    y = (H - total) / 2 - 0.1
    box(slide, (W - 0.9) / 2, y - 0.6, 0.9, 0.07, t.accent)
    bottom = _headline(slide, ctx, s.title, (W - 11.5) / 2, y, 11.5, hf.height + 0.08, max_pt=hf.size,
                       min_pt=hf.size, align="center")
    if s.subtitle:
        _body(slide, ctx, s.subtitle, (W - 9.0) / 2, bottom + 0.3, 9.0, 1.1, max_pt=20, min_pt=13,
              align="center", color=t.muted, italic=True)


def _quoted(value: str) -> str:
    core = (value or "").strip().strip('"“”')
    return f"“{core}”" if core else ""


def build_idea(slide, s: IdeaSlide, ctx: Ctx) -> None:
    """Labelled chips on the left (PC02 "KYP" slide), accent-framed visual on the right."""
    lk, t = ctx.look, ctx.look.t
    _background(slide, ctx)
    has_visual = s.visual is not None
    col_w = 7.7 if has_visual else CONTENT_W
    title_bottom = _headline(slide, ctx, s.title, MX, 0.5, col_w + (0.6 if has_visual else 0), 1.25,
                             max_pt=46, min_pt=26, max_lines=2)
    # (label, text, italic, stacked): long fields sit under their tab, short ones beside it.
    sections = [
        ("Concept Note", s.concept_note, False, True),
        ("Hook", _quoted(s.hook), True, False),
        ("Format", s.format, False, False),
        ("Creator", s.creator, False, False),
        ("Scalability", s.scalability, True, True),
    ]
    sections = [sec for sec in sections if sec[1] and sec[1].strip()]
    tab_size = 14
    tab_h = 0.36
    inline_tab_w = max((measure(lbl + ":", tab_size, lk.bf, bold=True) for lbl, _, _, st in sections if not st),
                       default=1.2) + 0.55
    top = max(1.6, title_bottom + 0.3)
    bottom = H - FOOT
    gap = 0.2

    def layout_height(size: float) -> float:
        lh = size * 1.3 / 72
        total = 0.0
        for _, body, _, stacked in sections:
            if stacked:
                total += tab_h + 0.1 + wrap_count(body, col_w, size, lk.bf) * lh
            else:
                total += max(tab_h, wrap_count(body, col_w - inline_tab_w - 0.15, size, lk.bf) * lh)
        return total + gap * (len(sections) - 1)

    size = 16.0
    while size > 10 and layout_height(size) > bottom - top:
        size -= 0.5
    lh = size * 1.3 / 72
    y = top
    for label, body, it, stacked in sections:
        tw = (max(2.2, measure(label + ":", tab_size, lk.bf, bold=True) + 0.9)) if stacked else inline_tab_w
        tab = box(slide, MX, y, tw, tab_h, t.accent, shape=MSO_SHAPE.PARALLELOGRAM, adj=0.15)
        if stacked:
            set_gradient(tab, [(0, t.accent, 1), (0.62, t.accent, 1), (1, t.accent, 0.0)], 0)
        text(slide, MX + 0.17, y, tw - 0.25, tab_h, label + ":", font=lk.bf, size=tab_size, color=lk.on_accent,
             bold=True, anchor="middle", spacing=1.0)
        if stacked:
            y += tab_h + 0.1
            bh = wrap_count(body, col_w, size, lk.bf) * lh
            text(slide, MX, y, col_w, bh + 0.05, body, font=lk.bf, size=size, color=t.text, italic=it, spacing=1.3)
            y += bh + gap
        else:
            bw = col_w - inline_tab_w - 0.15
            bh = wrap_count(body, bw, size, lk.bf) * lh
            ty = y + max(0.0, (tab_h - min(bh, lh)) / 2)
            text(slide, MX + inline_tab_w + 0.15, ty, bw, bh + 0.05, body, font=lk.bf, size=size, color=t.text,
                 italic=it, spacing=1.3)
            y += max(tab_h, bh) + gap
    if has_visual:
        _visual(slide, ctx, s.visual, 9.0, 0.95, 3.6, 5.6, frame=3.0)
    _footer(slide, ctx)


def _grid(n: int, x: float, y: float, w: float, h: float, gap: float = 0.3) -> list[tuple[float, float, float, float]]:
    if n <= 0:
        return []
    cols = n if n <= 3 else (2 if n == 4 else 3)
    rows = math.ceil(n / cols)
    cw = (w - gap * (cols - 1)) / cols
    rh = (h - gap * (rows - 1)) / rows
    return [(x + (i % cols) * (cw + gap), y + (i // cols) * (rh + gap), cw, rh) for i in range(n)]


def build_samples(slide, s: SamplesSlide, ctx: Ctx) -> None:
    lk, t = ctx.look, ctx.look.t
    _background(slide, ctx)
    y = _title_block(slide, ctx, s.title, s.strip, max_pt=46)
    vis = list(s.visuals)
    n = len(vis)
    area_y = max(y, 2.15)
    area_h = H - area_y - 1.0
    gap = 0.35
    cw = (CONTENT_W - gap * (n - 1)) / n
    if n == 1:
        cw = min(cw, area_h * 1.4)
    x0 = MX + (CONTENT_W - (cw * n + gap * (n - 1))) / 2
    for i, v in enumerate(vis):
        x = x0 + i * (cw + gap)
        _, ry, _, rh = _visual(slide, ctx, v, x, area_y, cw, area_h, mode="contain", frame=2.5, credit=False)
        text(slide, x, ry + rh + 0.14, cw, 0.25, f"SAMPLE {i + 1:02d}", font=lk.bf, size=9.5, color=t.accent,
             bold=True, align="center", tracking=2)
    _footer(slide, ctx)


def build_ref(slide, s: RefSlide, ctx: Ctx) -> None:
    t = ctx.look.t
    _background(slide, ctx)
    vis = list(s.visuals)
    has_why = bool(s.why and s.why.strip())
    if has_why:
        left_w = 4.4
        bottom = _headline(slide, ctx, s.title, MX, 0.65, left_w, 2.5, max_pt=46, min_pt=24, max_lines=4)
        box(slide, MX, bottom + 0.25, 0.6, 0.06, t.accent)
        _body(slide, ctx, s.why, MX, bottom + 0.55, left_w, H - bottom - 0.55 - FOOT - 0.2, max_pt=17, min_pt=11)
        gx, gy, gw, gh = MX + left_w + 0.55, 0.65, CONTENT_W - left_w - 0.55, H - 0.65 - FOOT - 0.15
    else:
        y = _title_block(slide, ctx, s.title, "", max_pt=44)
        gx, gy, gw, gh = MX, y, CONTENT_W, H - y - FOOT - 0.15
    for v, (x, y, w, h) in zip(vis, _grid(len(vis), gx, gy, gw, gh), strict=False):
        _visual(slide, ctx, v, x, y, w, h - 0.3, mode="contain", frame=1.5, tag="Ref")
    _footer(slide, ctx)


def build_two_column(slide, s: TwoColumnSlide, ctx: Ctx) -> None:
    lk, t = ctx.look, ctx.look.t
    _background(slide, ctx)
    y = max(_title_block(slide, ctx, s.title, "", max_pt=50), 2.0)
    gap = 0.9
    cw = (CONTENT_W - gap) / 2
    cols = (s.left, s.right)
    hsize = min(_head_fit(ctx, c.heading, cw, 0.9, 30, 18, 2).size for c in cols)
    h_h = max(wrap_count(c.heading, cw, hsize, lk.hf, bold=lk.head_bold, upper=lk.upper) for c in cols) * hsize / 72
    list_y = y + h_h + 0.5
    avail = H - list_y - FOOT - 0.1
    bullet_w = cw - 0.4

    def need(size: float) -> float:
        return max(sum(wrap_count(b, bullet_w, size, lk.bf) * size * 1.3 / 72 + size * 0.5 / 72
                       for b in c.bullets if b) for c in cols)

    size = 20.0
    while size > 10 and need(size) > avail:
        size -= 0.5
    if need(size) > avail:
        lh = size * 1.3 / 72
        trimmed = []
        for c in cols:
            bl = [b for b in c.bullets if b]
            budget = max(1, int((avail - len(bl) * size * 0.5 / 72) / lh / max(1, len(bl))))
            trimmed.append(c.model_copy(update={"bullets": [clip_lines(b, bullet_w, size, lk.bf, budget) for b in bl]}))
        cols = tuple(trimmed)
    box(slide, MX + cw + gap / 2, y + 0.05, 0.015, H - y - FOOT - 0.1, lk.line)
    for i, col in enumerate(cols):
        x = MX + i * (cw + gap)
        text(slide, x, y, cw, h_h + 0.05, col.heading, font=lk.hf, size=hsize, color=t.accent, bold=lk.head_bold,
             italic=lk.italic, upper=lk.upper, spacing=1.0)
        box(slide, x, y + h_h + 0.17, 0.5, 0.05, t.accent)
        yy = list_y
        for b in [b for b in col.bullets if b]:
            bh = wrap_count(b, bullet_w, size, lk.bf) * size * 1.3 / 72
            text(slide, x, yy, 0.35, size / 72 * 1.3, "▸", font=lk.bf, size=size, color=t.accent, bold=True,
                 spacing=1.3)
            text(slide, x + 0.4, yy, bullet_w, bh + 0.05, b, font=lk.bf, size=size, color=t.text, spacing=1.3)
            yy += bh + size * 0.5 / 72
    _footer(slide, ctx)


def build_table(slide, s: TableSlide, ctx: Ctx) -> None:
    lk, t = ctx.look, ctx.look.t
    _background(slide, ctx)
    y = _title_block(slide, ctx, s.title, s.strip, max_pt=46)
    headers = list(s.headers) or [""]
    ncols = len(headers)
    rows = [list(r[:ncols]) + [""] * (ncols - len(r[:ncols])) for r in s.rows if any(c for c in r)]
    if not rows:
        rows = [[""] * ncols]
    # Column widths follow content length (clamped).
    lens = [max([len(headers[c]) * 1.3] + [len(r[c]) for r in rows]) + 6 for c in range(ncols)]
    shares = [min(0.55, max(0.1, ln / sum(lens))) for ln in lens]
    widths = [CONTENT_W * sh / sum(shares) for sh in shares]
    top = max(y, 2.1)
    avail = H - top - FOOT - 0.15
    pad_x, pad_y = 0.14, 0.09
    hsize = 12.0

    def heights(size: float) -> tuple[float, list[float]]:
        head = max(wrap_count(h, w - 2 * pad_x, hsize, lk.bf, bold=True, upper=True)
                   for h, w in zip(headers, widths, strict=True)) * hsize * 1.25 / 72 + 2 * pad_y
        body = [max(1, *(wrap_count(c, w - 2 * pad_x, size, lk.bf, bold=(j == 0)) for j, (c, w) in
                         enumerate(zip(r, widths, strict=True)))) * size * 1.25 / 72 + 2 * pad_y for r in rows]
        return head, body

    size = 16.0
    head_h, row_hs = heights(size)
    while size > 9 and head_h + sum(row_hs) > avail:
        size -= 0.5
        head_h, row_hs = heights(size)
    if head_h + sum(row_hs) > avail:  # still too tall at 9pt: clip every cell to an equal line budget
        per_row = (avail - head_h) / len(rows) - 2 * pad_y
        max_lines = max(1, int(per_row / (size * 1.25 / 72)))
        rows = [[clip_lines(c, w - 2 * pad_x, size, lk.bf, max_lines, bold=(j == 0))
                 for j, (c, w) in enumerate(zip(r, widths, strict=True))] for r in rows]
        head_h, row_hs = heights(size)
    if head_h + sum(row_hs) > avail:  # dense table: two-line headers, tight padding
        headers = [clip_lines(h, w - 2 * pad_x, hsize, lk.bf, 2, bold=True, upper=True)
                   for h, w in zip(headers, widths, strict=True)]
        pad_y = 0.045
        head_h, row_hs = heights(size)
    # Breathe: spread spare height over the rows (capped), like a designed table.
    spare = avail - head_h - sum(row_hs)
    if spare > 0:
        extra = min(spare / (len(rows) + 1) * 0.6, 0.22)
        head_h += extra
        row_hs = [rh + extra for rh in row_hs]
    shape = slide.shapes.add_table(len(rows) + 1, ncols, Inches(MX), Inches(top), Inches(CONTENT_W),
                                   Inches(head_h + sum(row_hs)))
    table = shape.table
    set_table_style(table)
    for c, w in enumerate(widths):
        table.columns[c].width = Inches(w)
    table.rows[0].height = Inches(head_h)
    for r, rh in enumerate(row_hs, start=1):
        table.rows[r].height = Inches(rh)
    border = mix(t.bg, t.muted, 0.35) if lk.dark else mix(t.bg, t.text, 0.2)
    kpi = isinstance(s, KpiTableSlide)
    row_a, row_b = t.surface, mix(t.surface, t.bg, 0.55)
    for c, h in enumerate(headers):
        cell = table.cell(0, c)
        table_cell_style(cell, fill=t.accent, border=border, margin=pad_x, margin_y=max(0.015, pad_y - 0.03))
        _cell_text(cell, h, lk.bf, hsize, lk.on_accent, bold=True, upper=True)
    for r, row in enumerate(rows, start=1):
        fill = row_a if r % 2 else row_b
        for c, val in enumerate(row):
            cell = table.cell(r, c)
            table_cell_style(cell, fill=fill, border=border, margin=pad_x, margin_y=max(0.015, pad_y - 0.03))
            color = t.accent if (kpi and c == 0 and contrast(t.accent, fill) >= 3) else t.text
            _cell_text(cell, val, lk.bf, size, color, bold=c == 0 or (kpi and c == 1))
    _footer(slide, ctx)


def _cell_text(cell, value: str, font: str, size: float, color: str, *, bold: bool = False,
               upper: bool = False) -> None:
    tf = cell.text_frame
    tf.word_wrap = True
    para = tf.paragraphs[0]
    para.line_spacing = Pt(size * 1.25)
    run = para.add_run()
    run.text = (value or "").upper() if upper else (value or "")
    run.font.name = font
    run.font.size = Pt(size)
    run.font.bold = bold
    run.font.color.rgb = rgb(color)


def build_chips(slide, s: ChipsSlide, ctx: Ctx) -> None:
    lk, t = ctx.look, ctx.look.t
    _background(slide, ctx)
    y = _title_block(slide, ctx, s.title, s.strip, max_pt=46)
    groups = list(s.groups)
    top = max(y, 2.15)
    cells = _grid(len(groups), MX, top, CONTENT_W, H - top - FOOT, gap=0.28)
    if not cells:
        _footer(slide, ctx)
        return
    lsize = min(_head_fit(ctx, g.label, cells[0][2] - 0.5, 0.8, 24, 14, 2).size for g in groups)
    # Cells hug their content when the chips need far less than the space available.
    cell_w, cell_h = cells[0][2], cells[0][3]
    lab_h = max(wrap_count(g.label, cell_w - 0.5, lsize, lk.hf, bold=lk.head_bold, upper=lk.upper) for g in groups) * lsize / 72
    chip_need = max(_pill_rows([c for c in g.chips if c], cell_w - 0.5, lk.bf, 15.0) for g in groups)
    need_h = lab_h + 0.75 + chip_need
    if need_h < cell_h:
        cols = len(groups) if len(groups) <= 3 else (2 if len(groups) == 4 else 3)
        rows = math.ceil(len(groups) / cols)
        cells = _grid(len(groups), MX, top, CONTENT_W, need_h * rows + 0.28 * (rows - 1), gap=0.28)
    for g, (x, yy, w, h) in zip(groups, cells, strict=False):
        box(slide, x, yy, w, h, t.surface, line=lk.line, radius=0.12)
        l_h = wrap_count(g.label, w - 0.5, lsize, lk.hf, bold=lk.head_bold, upper=lk.upper) * lsize / 72
        text(slide, x + 0.25, yy + 0.22, w - 0.5, l_h + 0.05, g.label, font=lk.hf, size=lsize, color=t.accent,
             bold=lk.head_bold, italic=lk.italic, upper=lk.upper, spacing=1.0)
        chips = [c for c in g.chips if c]
        room = h - l_h - 0.7
        size = 15.0
        while size > 8.5 and _pill_rows(chips, w - 0.5, lk.bf, size) > room:
            size -= 0.5
        _pills(slide, ctx, chips, x + 0.25, yy + 0.22 + l_h + 0.25, w - 0.5, size=size,
               fill=mix(t.surface, t.accent, 0.08), max_h=room)
    _footer(slide, ctx)


def build_timeline(slide, s: TimelineSlide, ctx: Ctx) -> None:
    lk, t = ctx.look, ctx.look.t
    _background(slide, ctx)
    y = _title_block(slide, ctx, s.title, s.strip, max_pt=46)
    phases = list(s.phases)
    n = len(phases)
    overlap = 0.12
    pw = (CONTENT_W + overlap * (n - 1)) / n
    when_y = max(y, 2.2)
    bar_y = when_y + 0.4
    bar_h = 0.8
    pts_y = bar_y + bar_h + 0.35
    col_w = pw - 0.45
    room = H - pts_y - FOOT

    def need(size: float) -> float:
        return max((sum(wrap_count(p, col_w - 0.3, size, lk.bf) * size * 1.3 / 72 + size * 0.45 / 72
                        for p in ph.points if p) for ph in phases), default=0)

    size = 16.0
    while size > 9 and need(size) > room:
        size -= 0.5
    if need(size) > room:  # clip each phase's points to share the column height
        lh = size * 1.3 / 72
        clipped = []
        for ph in phases:
            pts = [p for p in ph.points if p]
            budget = max(1, int((room - len(pts) * size * 0.45 / 72) / lh / max(1, len(pts))))
            clipped.append(ph.model_copy(update={"points": [clip_lines(p, col_w - 0.3, size, lk.bf, budget)
                                                            for p in pts]}))
        phases = clipped
    nsize = min(_head_fit(ctx, ph.name, pw - 0.8, bar_h - 0.12, 20, 10, 2).size for ph in phases)
    for i, ph in enumerate(phases):
        x = MX + i * (pw - overlap)
        lead = i % 2 == 0
        fill = t.accent if lead else mix(t.surface, t.accent, 0.28 if lk.dark else 0.15)
        fg = lk.on_accent if lead else t.text
        box(slide, x, bar_y, pw, bar_h, fill, shape=MSO_SHAPE.PENTAGON if i == 0 else MSO_SHAPE.CHEVRON, adj=0.32)
        text(slide, x + (0.38 if i else 0.2), bar_y, pw - 0.7, bar_h, ph.name, font=lk.hf, size=nsize, color=fg,
             bold=lk.head_bold, italic=lk.italic, upper=lk.upper, align="center", anchor="middle", spacing=1.0)
        if ph.when:
            text(slide, x + 0.15, when_y, pw - 0.3, 0.3, ph.when, font=lk.bf, size=11,
                 color=t.accent if lk.dark else t.text, bold=True, align="center", upper=True, tracking=1.2)
        yy = pts_y
        for p in [p for p in ph.points if p]:
            ph_h = wrap_count(p, col_w - 0.3, size, lk.bf) * size * 1.3 / 72
            text(slide, x + 0.2, yy, 0.3, size / 72 * 1.3, "•", font=lk.bf, size=size, color=t.accent, bold=True)
            text(slide, x + 0.5, yy, col_w - 0.3, ph_h + 0.05, p, font=lk.bf, size=size, color=t.text, spacing=1.3)
            yy += ph_h + size * 0.45 / 72
        if i < n - 1:
            box(slide, x + pw - overlap / 2 + 0.02, pts_y, 0.012, room - 0.1, lk.line)
    _footer(slide, ctx)


def build_fin(slide, s: FinSlide, ctx: Ctx) -> None:
    lk, t = ctx.look, ctx.look.t
    if not _hero_background(slide, ctx, overlay=0.72):
        _background(slide, ctx, glow_side="left")
    title = (s.title or "").strip() or "Fin."
    f = fit_text(title, 11, 2.8, lk.hf, max_pt=160, min_pt=60, spacing=1.0, max_lines=2, bold=lk.head_bold)
    sub_h = 0.7 if s.subtitle else 0
    y = (H - f.height - sub_h) / 2 - 0.2
    color = t.accent if contrast(t.accent, t.bg) >= 4.5 else t.text
    text(slide, (W - 11) / 2, y, 11, f.height + 0.1, title, font=lk.hf, size=f.size, color=color,
         bold=lk.head_bold, italic=lk.italic, align="center", spacing=1.0)
    if s.subtitle:
        _body(slide, ctx, s.subtitle, (W - 9) / 2, y + f.height + 0.35, 9, 1.0, max_pt=20, min_pt=13,
              align="center", color=t.muted)
    brand = (ctx.deck.brand or "").upper()[:60]
    if _logo(slide, ctx, MX, H - 1.45, CONTENT_W, 0.6, align="center") is None and brand:
        text(slide, MX, H - 1.05, CONTENT_W, 0.3, brand, font=lk.bf, size=11, color=t.muted, bold=True,
             align="center", tracking=3)


def build_fallback(slide, s, ctx: Ctx) -> None:
    """Plain title + whatever text the slide has; used when a builder fails."""
    _background(slide, ctx)
    y = max(_title_block(slide, ctx, getattr(s, "title", "") or "", "", max_pt=44), 2.0)
    parts: list[str] = []
    for name in getattr(type(s), "model_fields", {}):
        if name in ("title", "notes", "layout"):
            continue
        val = getattr(s, name, None)
        if isinstance(val, str) and val.strip():
            parts.append(val.strip())
    _body(slide, ctx, "\n".join(parts)[:900], MX, y, CONTENT_W, H - y - FOOT, max_pt=18, min_pt=10)
    _footer(slide, ctx)


BUILDERS = {
    "cover": build_cover,
    "statement": build_statement,
    "from_to": build_from_to,
    "formula": build_formula,
    "card_grid": build_card_grid,
    "divider": build_divider,
    "idea": build_idea,
    "samples": build_samples,
    "ref": build_ref,
    "two_column": build_two_column,
    "table": build_table,
    "kpi_table": build_table,
    "chips": build_chips,
    "timeline": build_timeline,
    "fin": build_fin,
}


def _clear(slide) -> None:
    for shp in list(slide.shapes):
        shp._element.getparent().remove(shp._element)


def render_deck(deck: Deck) -> bytes:
    prs = Presentation()
    prs.slide_width = Inches(W)
    prs.slide_height = Inches(H)
    prs.core_properties.title = (deck.title or deck.brand or "Pitch deck")[:250]
    prs.core_properties.author = "PitchCraft"
    blank = prs.slide_layouts[6]
    look = make_look(deck.theme)
    total = len(deck.slides)
    prev_hero = False

    for i, s in enumerate(deck.slides, start=1):
        slide = prs.slides.add_slide(blank)
        ctx = Ctx(deck=deck, look=look, index=i, total=total, prev_hero=prev_hero,
                  strong_decor=getattr(s, "layout", "") in ("cover", "divider", "fin"))
        builder = BUILDERS.get(getattr(s, "layout", ""), build_fallback)
        try:
            builder(slide, s, ctx)
        except Exception:
            logger.exception("deck_render: %s slide %d failed; using fallback", getattr(s, "layout", "?"), i)
            _clear(slide)
            ctx.credits.clear()
            try:
                build_fallback(slide, s, ctx)
            except Exception:
                logger.exception("deck_render: fallback failed for slide %d", i)
        prev_hero = ctx.used_hero
        notes = (getattr(s, "notes", "") or "").strip()
        if ctx.credits:
            sources = "\n".join(f"- {c}" for c in dict.fromkeys(ctx.credits))
            notes = f"{notes}\n\nImage sources (internal pitching only):\n{sources}".strip()
        set_notes(slide, notes)

    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()
