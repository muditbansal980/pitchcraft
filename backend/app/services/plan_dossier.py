"""Brand dossier helpers for the deck pipeline (WORKPLAN C9).

- ``render_dossier``: the strategist's view of the dossier (``research_deep.dossier_brief``
  when Agent D provides it, else our own rendering), with fact ids like ``[F3]``.
- ``dossier_slice``: the part of the dossier relevant to one writer call.
- ``dossier_corpus``: everything the dossier says, so plan checks accept its numbers.
- ``fact_for_stat`` / ``source_label``: turn supported figures and cited fact ids into
  short footnote labels ("Zomato newsroom · zomato.com").
- ``specific_terms``: real products, campaigns and competitors an idea should name.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

from app.schemas.research import BrandDossier, DossierFact
from app.services import research_deep

FACT_ID_RE = re.compile(r"\s*\[(F\d+)(?:\s*,\s*(F\d+))*\]")
_ID_RE = re.compile(r"F\d+")
_WORD_RE = re.compile(r"[a-z0-9]{3,}")
_STOP = {
    "the", "and", "for", "with", "from", "that", "this", "your", "our", "you", "are", "its", "into", "their",
    "more", "than", "have", "has", "was", "were", "will", "what", "when", "who", "how", "why", "all", "new",
}


def is_deep(dossier: BrandDossier | None) -> bool:
    return bool(dossier) and (dossier.depth == "deep" or len(dossier.facts) >= 5)


def _clip(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0].rstrip(",;:") + "…"


def _fact_line(f: DossierFact, limit: int = 220) -> str:
    src = f" ({_clip(f.source_title, 50)})" if f.source_title else ""
    return f"[{f.id}] {_clip(f.text, limit)}{src}"


def _own_rendering(d: BrandDossier, max_chars: int) -> str:
    """Priority-ordered sections; facts fill whatever room is left."""
    head: list[str] = []
    if d.summary:
        head.append("SUMMARY: " + _clip(d.summary, 600))
    if d.audience:
        head.append("AUDIENCE: " + "; ".join(_clip(a, 120) for a in d.audience[:5]))
    if d.tone_of_voice:
        head.append("TONE: " + _clip(d.tone_of_voice, 200))
    if d.products:
        head.append("PRODUCTS: " + "; ".join(
            f"{p.name}" + (f" ({_clip(p.detail, 70)})" if p.detail else "") for p in d.products[:10]))
    if d.recent_campaigns:
        head.append("RECENT CAMPAIGNS: " + "; ".join(
            f"{c.name}" + (f" [{c.when}]" if c.when else "") + (f": {_clip(c.what, 100)}" if c.what else "")
            for c in d.recent_campaigns[:6]))
    if d.competitors:
        head.append("COMPETITORS: " + "; ".join(
            f"{c.name}" + (f": {_clip(c.angle, 80)}" if c.angle else "") for c in d.competitors[:6]))
    for label, items in (("MOMENTS", d.moments), ("CULTURAL HOOKS", d.cultural_hooks), ("SOCIAL SIGNALS", d.social_signals)):
        if items:
            head.append(f"{label}: " + "; ".join(_clip(x, 100) for x in items[:5]))
    text = "\n".join(head)
    facts: list[str] = []
    room = max_chars - len(text) - 10
    for f in d.facts:
        line = _fact_line(f)
        if room - len(line) - 1 < 0:
            break
        facts.append(line)
        room -= len(line) + 1
    if facts:
        text += "\nFACTS (cite by id):\n" + "\n".join(facts)
    return text[:max_chars]


def render_dossier(d: BrandDossier | None, max_chars: int = 6000) -> str:
    if d is None:
        return ""
    brief_fn = getattr(research_deep, "dossier_brief", None)
    if callable(brief_fn):
        try:
            text = brief_fn(d, max(300, max_chars // 4))  # D's helper budgets in tokens (~4 chars each)
            if isinstance(text, str) and len(text) > 200 and ("[F" in text or not d.facts):
                return text[: max_chars + 400]
        except Exception:  # noqa: BLE001 - D's helper is optional; ours is the fallback
            pass
    return _own_rendering(d, max_chars)


def _words(text: str) -> set[str]:
    return {w for w in _WORD_RE.findall((text or "").lower()) if w not in _STOP}


def dossier_slice(d: BrandDossier | None, about: str, max_chars: int = 2200, n_facts: int = 6) -> str:
    """Products, campaigns and facts most related to ``about`` (a bucket's name/concept/hook)."""
    if d is None:
        return ""
    want = _words(about)

    def score(text: str) -> int:
        return len(want & _words(text))

    lines: list[str] = []
    products = sorted(d.products, key=lambda p: -score(f"{p.name} {p.detail}"))[:4]
    if products:
        lines.append("PRODUCTS: " + "; ".join(p.name + (f" ({_clip(p.detail, 60)})" if p.detail else "") for p in products))
    camps = sorted(d.recent_campaigns, key=lambda c: -score(f"{c.name} {c.what}"))[:2]
    if camps:
        lines.append("CAMPAIGNS: " + "; ".join(f"{c.name}: {_clip(c.what, 90)}" for c in camps))
    if d.audience:
        lines.append("AUDIENCE: " + "; ".join(_clip(a, 90) for a in sorted(d.audience, key=lambda a: -score(a))[:2]))
    signals = sorted([*d.social_signals, *d.cultural_hooks], key=lambda s: -score(s))[:2]
    if signals:
        lines.append("SIGNALS: " + "; ".join(_clip(s, 90) for s in signals))
    facts = sorted(d.facts, key=lambda f: (-score(f.text), f.id))[:n_facts]
    if facts:
        lines.append("FACTS (cite ids in \"sources\"):\n" + "\n".join(_fact_line(f, 180) for f in facts))
    return "\n".join(lines)[:max_chars]


def dossier_corpus(d: BrandDossier | None) -> str:
    """All dossier text (facts with their numbers, products, campaigns...) for the stats check."""
    if d is None:
        return ""
    parts: list[str] = []
    try:
        parts.append(research_deep.dossier_text(d, 30_000) or "")
    except Exception:  # noqa: BLE001
        pass
    parts.append(d.summary)
    for f in d.facts:
        parts.append(f"{f.text} {f.number or ''}")
    parts += [f"{p.name} {p.detail}" for p in d.products]
    parts += [f"{c.name} {c.when} {c.what}" for c in d.recent_campaigns]
    parts += [f"{c.name} {c.angle}" for c in d.competitors]
    parts += [*d.audience, *d.cultural_hooks, *d.social_signals, *d.moments]
    return "\n".join(p for p in parts if p)


def source_label(f: DossierFact) -> str:
    domain = urlparse(f.source_url or "").netloc.removeprefix("www.")
    title = _clip(f.source_title, 70) if f.source_title else ""
    if title and domain and domain.split(".")[0] not in title.lower():
        return _clip(f"{title} · {domain}", 118)
    return _clip(title or domain or f.id, 118)


def facts_by_id(d: BrandDossier | None) -> dict[str, DossierFact]:
    return {f.id: f for f in (d.facts if d else [])}


def extract_fact_ids(text: str) -> list[str]:
    return _ID_RE.findall(text or "")


def strip_fact_ids(text: str) -> str:
    """'Orders doubled [F3].' -> 'Orders doubled.' (ids go into slide.sources instead)."""
    out = FACT_ID_RE.sub("", text or "")
    out = re.sub(r"\(\s*(?:F\d+\s*,?\s*)+\)", "", out)
    return re.sub(r"\s+([.,;:!?])", r"\1", out).strip()


_FIG_RE = re.compile(r"(\.?\d+(?:[.,]\d+)?)\s?(%|percent|x\b|×|m\b|mn\b|million|b\b|bn\b|billion|crore|cr\b|lakh|k\b|thousand)",
                     re.IGNORECASE)
_UNIT = {"%": "%", "percent": "%", "x": "x", "×": "x", "m": "M", "mn": "M", "million": "M", "b": "B", "bn": "B",
         "billion": "B", "crore": "cr", "cr": "cr", "lakh": "lakh", "k": "K", "thousand": "K"}


def _figures(text: str) -> set[tuple[str, str]]:
    out = set()
    for m in _FIG_RE.finditer(text or ""):
        num = m.group(1).replace(",", "")
        if "." in num:
            num = num.rstrip("0").rstrip(".")
        out.add((num, _UNIT.get(m.group(2).lower(), m.group(2).lower())))
    return out


def fact_for_stat(stat: str, d: BrandDossier | None) -> DossierFact | None:
    """The dossier fact that states this figure (same number and unit), if any."""
    if d is None:
        return None
    want = _figures(stat)
    if not want:
        return None
    for f in d.facts:
        if want & _figures(f"{f.number or ''} {f.text}"):
            return f
    return None


def specific_terms(d: BrandDossier | None, extra: list[str] | None = None) -> list[str]:
    """Names an idea can use to be specific: products, campaigns, competitors (longest first)."""
    terms: dict[str, None] = {}
    if d:
        for p in d.products:
            terms.setdefault(p.name.strip(), None)
        for c in d.recent_campaigns:
            terms.setdefault(c.name.strip(), None)
        for c in d.competitors:
            terms.setdefault(c.name.strip(), None)
    for t in extra or []:
        terms.setdefault(t.strip(), None)
    return sorted((t for t in terms if len(t) >= 3), key=len, reverse=True)


def mentions_any(text: str, terms: list[str]) -> bool:
    low = (text or "").lower()
    return any(re.search(r"(?<![a-z0-9])" + re.escape(t.lower()) + r"(?![a-z0-9])", low) for t in terms)
