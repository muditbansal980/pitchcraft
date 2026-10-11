"""Local meme captioning (Agent B): caption boxes for popular Imgflip templates.

Without Imgflip credentials, captions are drawn locally with Pillow. Box
rectangles (fractions of the template image, x0, y0, x1, y1) were measured from
the template images for the most used templates; each box has a style:
- ``impact``: classic meme text, white caps with a black outline (top/bottom memes)
- ``panel``:  black text centred in a white panel or speech bubble (Drake, Gru, Expanding Brain)
- ``label``:  white outlined text placed over a person/object (Distracted Boyfriend)
Unknown templates with 1-2 boxes get top/bottom ``impact`` text; others get a
caption band above the picture.

The Impact-like face is Anton (OFL), fetched once into ``{media_dir}/fonts``;
DejaVu Sans Bold is the offline fallback.
"""

import io
import logging
from pathlib import Path

import httpx
from PIL import Image, ImageDraw, ImageFont

from app.core.config import settings

logger = logging.getLogger(__name__)

Box = tuple[float, float, float, float, str]

TOP = (0.03, 0.02, 0.97, 0.24, "impact")
BOTTOM = (0.03, 0.76, 0.97, 0.98, "impact")

LAYOUTS: dict[str, list[Box]] = {
    "181913649": [(0.52, 0.03, 0.97, 0.47, "panel"), (0.52, 0.53, 0.97, 0.97, "panel")],  # Drake Hotline Bling
    "87743020": [(0.10, 0.07, 0.43, 0.28, "label"), (0.45, 0.04, 0.78, 0.25, "label"),
                 (0.05, 0.80, 0.95, 0.97, "impact")],  # Two Buttons
    "112126428": [(0.12, 0.62, 0.48, 0.80, "label"), (0.44, 0.30, 0.76, 0.48, "label"),
                  (0.74, 0.42, 0.99, 0.62, "label")],  # Distracted Boyfriend (new thing, him, current)
    "222403160": [(0.03, 0.02, 0.72, 0.24, "impact"), (0.03, 0.84, 0.97, 0.99, "impact")],  # Bernie asking
    "217743513": [(0.10, 0.30, 0.44, 0.56, "panel"), (0.54, 0.02, 0.98, 0.22, "impact")],  # UNO Draw 25
    "252600902": [(0.05, 0.05, 0.55, 0.25, "label"), (0.55, 0.05, 0.98, 0.25, "label")],  # Always Has Been
    "124822590": [(0.24, 0.12, 0.46, 0.34, "label"), (0.47, 0.12, 0.76, 0.34, "label"),
                  (0.22, 0.60, 0.78, 0.90, "label")],  # Left Exit 12 Off Ramp
    "322841258": [(0.02, 0.38, 0.49, 0.49, "impact"), (0.51, 0.38, 0.98, 0.49, "impact"),
                  (0.51, 0.87, 0.98, 0.99, "impact")],  # Anakin Padme 4 Panel
    "135256802": [(0.02, 0.58, 0.36, 0.82, "label"), (0.62, 0.58, 0.98, 0.82, "label"),
                  (0.25, 0.02, 0.75, 0.22, "label")],  # Epic Handshake
    "131940431": [(0.30, 0.10, 0.49, 0.46, "panel"), (0.80, 0.10, 0.99, 0.46, "panel"),
                  (0.30, 0.60, 0.49, 0.96, "panel"), (0.80, 0.60, 0.99, 0.96, "panel")],  # Gru's Plan
    "131087935": [(0.05, 0.33, 0.48, 0.50, "label"), (0.66, 0.08, 0.98, 0.34, "label"),
                  (0.02, 0.62, 0.32, 0.80, "label"), (0.36, 0.84, 0.70, 0.98, "label"),
                  (0.68, 0.58, 0.99, 0.82, "label")],  # Running Away Balloon
    "97984": [TOP, BOTTOM],  # Disaster Girl
    "4087833": [TOP, BOTTOM],  # Waiting Skeleton
    "80707627": [(0.02, 0.38, 0.98, 0.49, "impact"), (0.02, 0.86, 0.49, 0.98, "impact"),
                 (0.51, 0.86, 0.98, 0.98, "impact")],  # Sad Pablo Escobar
    "91538330": [TOP, BOTTOM],  # X, X Everywhere
    "309868304": [(0.03, 0.20, 0.47, 0.34, "label"), (0.53, 0.20, 0.97, 0.34, "label"),
                  (0.05, 0.84, 0.95, 0.98, "impact")],  # Trade Offer
    "124055727": [TOP, BOTTOM],  # Y'all Got Any More Of That
    "129242436": [(0.43, 0.66, 0.97, 0.82, "panel"), (0.03, 0.02, 0.97, 0.20, "impact")],  # Change My Mind
    "188390779": [(0.02, 0.00, 0.50, 0.18, "panel"), (0.52, 0.00, 0.98, 0.18, "panel")],  # Woman Yelling At Cat
    "438680": [(0.04, 0.02, 0.46, 0.24, "panel"), (0.54, 0.02, 0.96, 0.22, "panel")],  # Batman Slapping Robin
    "161865971": [(0.10, 0.53, 0.90, 0.72, "panel"), (0.05, 0.00, 0.95, 0.12, "panel")],  # Marked Safe From
    "79132341": [(0.05, 0.02, 0.60, 0.16, "label"), (0.50, 0.38, 0.98, 0.52, "label"),
                 (0.05, 0.66, 0.62, 0.78, "label")],  # Bike Fall
    "101470": [TOP, BOTTOM],  # Ancient Aliens
    "102156234": [TOP, BOTTOM],  # Mocking Spongebob
    "93895088": [(0.02, 0.02, 0.49, 0.23, "panel"), (0.02, 0.27, 0.49, 0.48, "panel"),
                 (0.02, 0.53, 0.49, 0.73, "panel"), (0.02, 0.78, 0.49, 0.98, "panel")],  # Expanding Brain
    "100777631": [(0.05, 0.55, 0.55, 0.75, "label"), (0.62, 0.10, 0.98, 0.30, "label"),
                  (0.05, 0.84, 0.95, 0.98, "impact")],  # Is This A Pigeon
    "247375501": [(0.02, 0.00, 0.48, 0.13, "panel"), (0.52, 0.00, 0.98, 0.13, "panel"),
                  (0.02, 0.72, 0.48, 0.97, "panel"), (0.52, 0.72, 0.98, 0.97, "panel")],  # Buff Doge vs. Cheems
    "27813981": [(0.03, 0.02, 0.97, 0.16, "impact"), (0.03, 0.84, 0.97, 0.98, "impact")],  # Hide the Pain Harold
    "89370399": [TOP, BOTTOM],  # Roll Safe
    "61579": [TOP, BOTTOM],  # One Does Not Simply
    "148909805": [(0.02, 0.02, 0.98, 0.30, "panel"), (0.03, 0.84, 0.97, 0.98, "impact")],  # Monkey Puppet
    "178591752": [(0.52, 0.03, 0.98, 0.47, "panel"), (0.52, 0.53, 0.98, 0.97, "panel")],  # Tuxedo Winnie The Pooh
    "1035805": [(0.25, 0.00, 0.72, 0.12, "panel"), (0.02, 0.35, 0.24, 0.43, "panel"),
                (0.25, 0.35, 0.46, 0.43, "panel"), (0.50, 0.33, 0.86, 0.45, "panel")],  # Boardroom Meeting
    "155067746": [(0.02, 0.01, 0.98, 0.13, "panel"), (0.02, 0.14, 0.98, 0.26, "panel"),
                  (0.02, 0.27, 0.98, 0.39, "panel")],  # Surprised Pikachu
    "180190441": [(0.06, 0.07, 0.42, 0.27, "panel"), (0.58, 0.11, 0.95, 0.31, "panel"),
                  (0.05, 0.86, 0.95, 0.98, "impact")],  # They're The Same Picture
    "195515965": [(0.02, 0.01, 0.60, 0.24, "panel"), (0.02, 0.26, 0.60, 0.49, "panel"),
                  (0.02, 0.51, 0.60, 0.74, "panel"), (0.02, 0.76, 0.60, 0.99, "panel")],  # Clown Applying Makeup
    "61556": [TOP, BOTTOM],  # Grandma Finds The Internet
    "21735": [(0.56, 0.04, 0.97, 0.21, "panel"), (0.58, 0.38, 0.97, 0.54, "panel")],  # The Rock Driving
    "5496396": [TOP, BOTTOM],  # Leonardo Dicaprio Cheers
    "226297822": [(0.03, 0.02, 0.48, 0.32, "panel"), (0.03, 0.35, 0.48, 0.65, "panel"),
                  (0.03, 0.68, 0.48, 0.98, "panel")],  # Panik Kalm Panik
    "370867422": [TOP, BOTTOM],  # Megamind peeking
    "99683372": [(0.02, 0.02, 0.48, 0.48, "panel"), (0.02, 0.52, 0.48, 0.98, "panel")],  # Sleeping Shaq
    "259237855": [TOP, BOTTOM],  # Laughing Leo
    "119139145": [(0.10, 0.42, 0.50, 0.62, "label"), (0.52, 0.05, 0.98, 0.26, "label")],  # Blank Nut Button
    "67452763": [TOP, BOTTOM],  # Squidward window
    "61585": [TOP, BOTTOM],  # Bad Luck Brian
}

# What each template is good for (helps match templates to a bucket/brief).
USES: dict[str, str] = {
    "181913649": "preference choice reject approve better option upgrade",
    "87743020": "dilemma choice hard decision torn between two",
    "112126428": "temptation switching distracted new thing loyalty",
    "222403160": "asking again request plea support",
    "217743513": "avoid doing refuse stubborn rather",
    "252600902": "realisation always been truth reveal",
    "124822590": "choosing impulse swerve sudden switch",
    "322841258": "expectation assumption awkward plan going wrong",
    "135256802": "agreement common ground unity both",
    "131940431": "plan backfire steps oops",
    "131087935": "distraction pulled away goal",
    "97984": "chaos evil grin trouble",
    "4087833": "waiting forever delay patience",
    "80707627": "lonely waiting bored alone",
    "91538330": "everywhere trend ubiquitous",
    "309868304": "deal exchange offer trade",
    "124055727": "craving addicted want more",
    "129242436": "opinion hot take debate",
    "188390779": "argument conflict blame confused",
    "438680": "shut up correction slap",
    "161865971": "relief escaped avoided",
    "79132341": "self sabotage blame own fault",
    "101470": "explanation conspiracy it's always",
    "102156234": "mocking sarcasm repeat",
    "93895088": "levels escalation smarter galaxy brain",
    "100777631": "misidentify confusion is this",
    "247375501": "then vs now strong weak comparison",
    "27813981": "pain hiding awkward smile",
    "89370399": "smart hack life hack loophole",
    "61579": "difficult impossible simply",
    "148909805": "awkward look away guilty",
    "178591752": "fancy classy upgrade sophisticated",
    "1035805": "bad idea meeting suggestion fired",
    "155067746": "obvious consequence shocked surprised",
    "180190441": "same identical no difference",
    "195515965": "becoming a clown escalating foolish",
    "61556": "first time new user confused",
    "21735": "panic realisation car ride",
    "5496396": "cheers toast celebrate success",
    "226297822": "panic calm panic plot twist",
    "370867422": "peeking curious no",
    "99683372": "don't care vs excited real",
    "259237855": "laughing mocking amused",
    "119139145": "can't resist urge press",
    "67452763": "fomo jealous others having fun",
    "61585": "bad luck unlucky",
}

_font_path: Path | None = None


def _anton_path() -> Path | None:
    """Anton (OFL, Impact-like), downloaded once into the media dir; None if unavailable."""
    global _font_path
    if _font_path and _font_path.is_file():
        return _font_path
    target = Path(settings.media_dir) / "fonts" / "Anton-Regular.ttf"
    if target.is_file():
        _font_path = target
        return target
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        resp = httpx.get("https://raw.githubusercontent.com/google/fonts/main/ofl/anton/Anton-Regular.ttf",
                         timeout=15, follow_redirects=True)
        if resp.status_code == 200 and resp.content[:4] in (b"\x00\x01\x00\x00", b"true", b"OTTO"):
            target.write_bytes(resp.content)
            _font_path = target
            return target
    except httpx.HTTPError as exc:
        logger.info("memes: could not fetch the Anton font (%s); using DejaVu", exc)
    return None


def _system_bold() -> str | None:
    for path in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                 "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
                 "/Library/Fonts/Arial Bold.ttf", "C:/Windows/Fonts/arialbd.ttf"):
        if Path(path).is_file():
            return path
    return None


def _font(style: str, size: int):
    path = (_anton_path() if style == "impact" else None) or _system_bold()
    if path:
        return ImageFont.truetype(str(path), size)
    return ImageFont.load_default(size=size)


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, width: float) -> list[str]:
    lines, line = [], ""
    for word in text.split():
        trial = f"{line} {word}".strip()
        if draw.textlength(trial, font=font) <= width or not line:
            line = trial
        else:
            lines.append(line)
            line = word
    if line:
        lines.append(line)
    return lines


def draw_box(img: Image.Image, text: str, box: Box) -> None:
    fx0, fy0, fx1, fy1, style = box
    text = " ".join((text or "").split())
    if not text:
        return
    if style == "impact":
        text = text.upper()
    draw = ImageDraw.Draw(img)
    x0, y0, x1, y1 = fx0 * img.width, fy0 * img.height, fx1 * img.width, fy1 * img.height
    bw, bh = x1 - x0, y1 - y0
    size = int(min(bh / (1.15 if style == "impact" else 1.6), img.width / 9))
    while True:
        font = _font(style, max(size, 10))
        lines = _wrap(draw, text, font, bw)
        widest = max((draw.textlength(ln, font=font) for ln in lines), default=0)
        if (len(lines) * size * 1.15 <= bh and widest <= bw) or size <= 11:
            break
        size -= 2
    stroke = max(1, size // 12) if style in ("impact", "label") else 0
    fill = (0, 0, 0) if style == "panel" else (255, 255, 255)
    line_h = size * 1.15
    y = y0 + (bh - len(lines) * line_h) / 2 if style != "impact" or fy0 > 0.5 or fy1 < 0.5 else y0
    if style == "impact" and fy0 > 0.5:
        y = y1 - len(lines) * line_h  # bottom text sits on the bottom edge
    for line in lines:
        lw = draw.textlength(line, font=font)
        draw.text((x0 + (bw - lw) / 2, y), line, font=font, fill=fill, stroke_width=stroke, stroke_fill=(0, 0, 0))
        y += line_h


def caption_band(img: Image.Image, boxes: list[str]) -> Image.Image:
    """Caption above the picture (modern meme format) for templates without known boxes."""
    w = img.width
    pad = max(18, w // 28)
    text = "\n".join(b.strip() for b in boxes if b.strip())
    probe = ImageDraw.Draw(img)
    size = max(22, w // 16)
    while True:
        font = _font("panel", size)
        lines = [ln for para in text.split("\n") for ln in _wrap(probe, para, font, w - 2 * pad)]
        if len(lines) <= 5 or size <= 18:
            break
        size -= 2
    line_h = int(size * 1.25)
    band = pad * 2 + line_h * len(lines)
    out = Image.new("RGB", (w, img.height + band), "white")
    out.paste(img, (0, band))
    draw = ImageDraw.Draw(out)
    y = pad
    for line in lines:
        draw.text((pad, y), line, font=font, fill=(0, 0, 0))
        y += line_h
    return out


def render_meme(template: bytes, template_id: str, boxes: list[str], box_count: int | None = None) -> bytes:
    """Caption a template locally: measured boxes, else top/bottom (<= 2 boxes), else a caption band."""
    with Image.open(io.BytesIO(template)) as src:
        img = src.convert("RGB")
    img.thumbnail((1000, 1000))
    texts = [b for b in boxes if b and b.strip()]
    layout = LAYOUTS.get(str(template_id))
    if layout is None and len(texts) <= 2 and (box_count or 2) <= 2:
        layout = [TOP, BOTTOM] if len(texts) == 2 else [BOTTOM]
    if layout and len(texts) <= len(layout):
        for text, box in zip(texts, layout, strict=False):
            draw_box(img, text, box)
        out = img
    else:
        out = caption_band(img, texts)
    buf = io.BytesIO()
    out.save(buf, "JPEG", quality=90)
    return buf.getvalue()
