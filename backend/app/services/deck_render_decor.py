"""Decorative pattern layers for the PPTX renderer (Agent B).

``draw_decor`` paints ``theme.decor`` behind a slide's content: subtle, mostly at
the slide edges, in colours derived from the brand palette (or the season's
accents), so it adds brand/season flavour without hurting legibility. ``strong``
(cover, divider, fin) shows the full pattern; content slides get a light corner
version.
"""

import io
import random
from functools import lru_cache

from lxml import etree
from PIL import Image
from pptx.enum.shapes import MSO_SHAPE
from pptx.oxml.ns import qn
from pptx.util import Inches, Pt

from app.services.deck_render_kit import box, rgb
from app.services.themes import luminance, mix

W, H = 13.333, 7.5


def _line_alpha(shape, hex_color: str, width_pt: float, alpha: float) -> None:
    shape.line.color.rgb = rgb(hex_color)
    shape.line.width = Pt(width_pt)
    clr = shape.line._get_or_add_ln().find(qn("a:solidFill")).find(qn("a:srgbClr"))
    etree.SubElement(clr, qn("a:alpha"), val=str(int(alpha * 100000)))


def _outline(slide, x, y, w, h, color, width, alpha, shape=MSO_SHAPE.OVAL):
    shp = box(slide, x, y, w, h, None, shape=shape)
    _line_alpha(shp, color, width, alpha)
    return shp


@lru_cache(maxsize=8)
def _grain_png(dark: bool, strength: int) -> bytes:
    rnd = random.Random(7)
    w, h = 480, 270
    img = Image.new("RGBA", (w, h))
    px = img.load()
    for yy in range(h):
        for xx in range(w):
            v = rnd.randint(0, 255)
            a = rnd.randint(0, strength)
            px[xx, yy] = (v, v, v, a) if dark else (int(v * 0.4), int(v * 0.4), int(v * 0.4), a)
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def _grain(slide, dark: bool, strong: bool) -> None:
    # Grain reads as texture on dark slides but as dirt on light ones: keep it faint there.
    data = _grain_png(dark, (34 if strong else 24) if dark else (12 if strong else 8))
    slide.shapes.add_picture(io.BytesIO(data), 0, 0, Inches(W), Inches(H))


def _palette(theme) -> list[str]:
    from app.services.brand_identity_season import get_season

    sea = get_season(theme.season)
    cols = [theme.accent, theme.accent_2]
    if sea:
        cols = [theme.accent, *sea.accents]
    return [c for c in cols if c]


def _edge_points(rnd: random.Random, n: int) -> list[tuple[float, float]]:
    """Random points in the slide margins (never over the central content area)."""
    pts = []
    while len(pts) < n:
        x, y = rnd.uniform(0.1, W - 0.3), rnd.uniform(0.1, H - 0.3)
        if 1.2 < x < W - 1.4 and 1.0 < y < H - 0.9:
            continue
        pts.append((x, y))
    return pts


def draw_decor(slide, theme, *, strong: bool, seed: int = 0) -> None:
    kind = getattr(theme, "decor", "none") or "none"
    if kind == "none":
        return
    dark = luminance(theme.bg) < 0.35
    pal = _palette(theme)
    soft = [mix(theme.bg, c, 0.35 if dark else 0.45) for c in pal]
    rnd = random.Random(seed * 7919 + 13)

    if kind in ("grain", "film_grain"):
        _grain(slide, dark, strong)
        if kind == "film_grain" and strong:
            perf = mix(theme.bg, theme.text, 0.12)
            for i in range(int(W / 0.42)):
                box(slide, 0.15 + i * 0.42, 0.12, 0.2, 0.13, perf, radius=0.03)
                box(slide, 0.15 + i * 0.42, H - 0.25, 0.2, 0.13, perf, radius=0.03)
        return

    if kind == "dots":
        rows, cols = (6, 9) if strong else (4, 6)
        x0, y0, gap = W - 0.35 - cols * 0.3, 0.35, 0.3
        for r in range(rows):
            for c in range(cols):
                d = 0.07 + 0.05 * (c / max(1, cols - 1))
                box(slide, x0 + c * gap, y0 + r * gap, d, d, soft[(r + c) % len(soft)], shape=MSO_SHAPE.OVAL)
        if strong:
            for r in range(3):
                for c in range(5):
                    box(slide, 0.35 + c * gap, H - 1.3 + r * gap, 0.07, 0.07, soft[0], shape=MSO_SHAPE.OVAL)
        return

    if kind == "stripes":
        n = 5 if strong else 3
        for i in range(n):
            box(slide, W - 3.6 + i * 0.55, -0.3, 0.22, H + 0.6, pal[i % len(pal)], alpha=0.10 if strong else 0.07,
                shape=MSO_SHAPE.PARALLELOGRAM, adj=0.5)
        return

    if kind == "waves":
        n = 5 if strong else 3
        for i in range(n):
            s = 4.0 + i * 1.1
            _outline(slide, W - s * 0.62, H - s * 0.42, s, s, pal[i % len(pal)], 1.5, 0.22 if strong else 0.14)
        return

    if kind == "confetti":
        count = 26 if strong else 10
        shapes = (MSO_SHAPE.RECTANGLE, MSO_SHAPE.OVAL, MSO_SHAPE.ISOSCELES_TRIANGLE, MSO_SHAPE.RECTANGLE)
        for i, (x, y) in enumerate(_edge_points(rnd, count)):
            shp = shapes[i % len(shapes)]
            w = rnd.uniform(0.08, 0.18)
            h = w * (2.2 if shp == MSO_SHAPE.RECTANGLE else 1.0)
            el = box(slide, x, y, w, h, pal[i % len(pal)], alpha=0.75 if strong else 0.45, shape=shp)
            el.rotation = rnd.uniform(0, 360)
        return

    if kind == "diyas":
        marigold = ["#FF8F00", "#FFB300", "#F57C00", "#FFCA28"]
        # Marigold garland swags along the top edge.
        swags = 3 if strong else 2
        span = W / swags
        for sw in range(swags):
            for k in range(15):
                t = k / 14
                x = sw * span + t * span
                y = 0.05 + 0.55 * (4 * t * (1 - t)) * (1.0 if strong else 0.6)
                d = 0.17 if strong else 0.13
                box(slide, x - d / 2, y, d, d, marigold[(k + sw) % 4], alpha=0.95 if strong else 0.6, shape=MSO_SHAPE.OVAL)
        if strong:
            for x in (0.45, 1.35, W - 2.0, W - 1.1):
                _diya(slide, x, H - 0.75, theme)
        else:
            _diya(slide, W - 1.15, H - 0.95, theme, scale=0.7)
        return

    if kind == "pitch_lines":
        line = mix(theme.bg, theme.text, 0.25 if dark else 0.3)
        if strong:
            _outline(slide, W - 6.0, -1.5, 9.5, 9.5, line, 1.25, 0.6)
            _outline(slide, W - 4.3, 0.2, 6.0, 6.0, line, 0.75, 0.45)
        px, pw = W - 1.55, 0.95
        _outline(slide, px, 0.6 if strong else 4.2, pw, (H - 1.2) if strong else 2.6, line, 1.0, 0.5,
                 shape=MSO_SHAPE.RECTANGLE)
        for yy in ((1.3, H - 1.3) if strong else (4.7,)):
            ln = box(slide, px - 0.3, yy, pw + 0.6, 0.012, line, alpha=0.5)
            ln.name = "crease"
        return


def _diya(slide, x: float, y: float, theme, scale: float = 1.0) -> None:
    """A small clay diya: bowl (half disc) with a flame and a soft glow, bottom-left at (x, y)."""
    s = scale
    bw, bh = 0.62 * s, 0.24 * s
    glow = box(slide, x + bw / 2 - 0.42 * s, y - 0.62 * s, 0.84 * s, 0.84 * s, "#FFC107", alpha=0.22,
               shape=MSO_SHAPE.OVAL)
    glow.name = "diya glow"
    # FLOWCHART_DELAY is a D shape (flat left, round right); turned 90 degrees it is a bowl.
    # Rotation is about the centre, so the unrotated box is bh wide and bw tall around that centre.
    cx, cy = x + bw / 2, y + bh / 2
    bowl = box(slide, cx - bh / 2, cy - bw / 2, bh, bw, "#B5541B", shape=MSO_SHAPE.FLOWCHART_DELAY)
    bowl.rotation = 90
    box(slide, x + 0.04 * s, y - 0.03 * s, bw - 0.08 * s, 0.07 * s, "#8A3B12", shape=MSO_SHAPE.OVAL)
    fw, fh = 0.15 * s, 0.24 * s
    flame = box(slide, cx - fw / 2, y - fh - 0.02 * s, fw, fh, "#FFB300", shape=MSO_SHAPE.TEAR)
    flame.rotation = 315
    box(slide, cx - fw / 4, y - fh * 0.55, fw / 2, fh * 0.45, "#FFF3C4", shape=MSO_SHAPE.OVAL)
