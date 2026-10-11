"""Deterministic rule checks for generated decks.

The LLM editor is asked to enforce the rules; these checks find violations
reliably so they can be handed to the editor as must-fix items, enforced in code
where possible (service names) and logged.
"""

from __future__ import annotations

import difflib
import functools
import json
import re
from collections.abc import Iterable
from typing import Any

from app.services.retrieval import Service

_LABEL_WORDS = {
    "concept note", "content reference", "format reference", "distribution science", "sample visual",
    "sample visuals", "sample narrative", "content principle", "influencer suggestions", "influencer campaign",
    "gen z", "on ground", "on ground activation", "brand lift", "content pillars", "content buckets",
    "key content buckets", "program overview", "the big picture", "the solution", "the opportunity",
    "the narrative", "the ideas", "our objective", "tension point", "why these", "how it works",
    "why it works", "segment overview", "admin's handbook", "irl content", "distribution plan",
}
_COMMON = {
    "instagram", "youtube", "reddit", "twitter", "whatsapp", "telegram", "linkedin", "quora", "goodreads",
    "tiktok", "google", "facebook", "snapchat", "spotify wrapped", "india", "indian", "mumbai", "delhi",
    "bangalore", "bengaluru", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    "january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
    "november", "december", "diwali", "holi", "ipl", "world cup", "fifa", "hinglish", "bollywood",
}

# A capitalised phrase containing one of these is a heading/label, not a person or page name.
_GENERIC_TOKENS = {
    "the", "our", "your", "their", "why", "how", "what", "when", "who", "this", "that", "with", "for", "and",
    "reel", "reels", "story", "stories", "content", "creator", "creators", "brand", "brands", "page", "pages",
    "meme", "memes", "video", "videos", "post", "posts", "campaign", "influencer", "influencers", "handles",
    "handle", "fan", "fans", "show", "shows", "episode", "season", "series", "bucket", "buckets", "idea",
    "ideas", "sample", "samples", "ref", "format", "reference", "objective", "hook", "category", "scalability",
    "execution", "concept", "note", "phase", "peak", "burst", "week", "month", "day", "plan", "strategy",
    "community", "amplification", "production", "offline", "solo", "memed", "orm", "twitter", "trend",
    "analytics", "live", "lives", "event", "events", "launch", "drop", "timeline", "kpi", "kpis", "target",
    "instagram", "youtube", "reddit", "whatsapp", "carousel", "static", "statics", "poll", "polls", "comment",
    "comments", "ugc", "irl", "pov", "top", "picks", "news", "culture", "recommendations", "think", "pieces",
}

# Prices/budgets: currency amounts or explicit budget/fee words.
PRICE_RE = re.compile(
    r"(₹\s?\d[\d,.]*\s?(?:k|m|mn|b|bn|lakh|lakhs|crore|crores|cr)?\b|\brs\.?\s?\d|\binr\s?\d|\$\s?\d|\d[\d,.]*\s?(lakh|lakhs|crore|crores|cr)\b|"
    r"\bbudget of\b|\bcosts? (?:of|around|about)\b|\bfees?\b|\bpriced at\b)",
    re.IGNORECASE,
)
FILLER = (
    "fast-paced", "leverage", "synergy", "synergies", "holistic", "360-degree", "game-changer", "game changer",
    "cutting-edge", "seamless", "unlock the", "elevate", "resonate", "revolutionize", "revolutionise",
    "tapestry", "delve", "landscape", "engaging content",
)


def match_service(name: str, catalog: list[Service]) -> Service | None:
    """Catalog service for a model-written name: exact, then base name before '(', then fuzzy."""
    if not name:
        return None
    norm = name.strip().lower()
    for s in catalog:
        if s.name.lower() == norm:
            return s
    base = {s.name.split("(")[0].strip().lower(): s for s in catalog}
    head = norm.split("(")[0].strip()
    if head in base:
        return base[head]
    for key, s in base.items():
        if key and (key in norm or norm in key) and len(min(key, norm, key=len)) >= 4:
            return s
    close = difflib.get_close_matches(head, list(base), n=1, cutoff=0.75)
    if close:
        return base[close[0]]
    # Single-word services by stem: "Influencer Marketing" -> "Influencers".
    words = {w.rstrip("s") for w in re.findall(r"[a-z]{5,}", head)}
    for key, s in base.items():
        if " " not in key and key.rstrip("s") in words:
            return s
    return None


_NAME_RE = re.compile(r"\b([A-Z][a-z]+(?:[ ]+(?:[A-Z][a-z]+|[A-Z]{2,})){1,2})\b")


@functools.lru_cache(maxsize=1)
def _dictionary() -> frozenset[str]:
    """Lower-case English words (system word list when present); empty if unavailable."""
    for path in ("/usr/share/dict/words", "/usr/share/dict/american-english", "/usr/share/dict/british-english"):
        try:
            with open(path, encoding="utf-8", errors="ignore") as fh:
                return frozenset(w.strip() for w in fh if w.strip() and w[0].islower())
        except OSError:
            continue
    return frozenset()


def _looks_like_name(phrase: str) -> bool:
    """A person/page name has at least one token that is not an ordinary English word."""
    words = _dictionary()
    if not words:
        return True
    return any(tok.lower().strip("'") not in words for tok in phrase.split())
_CAMEL_RE = re.compile(r"\b([A-Z][a-z]+[A-Z][A-Za-z]+)\b")
_HANDLE_RE = re.compile(r"@([A-Za-z0-9_.]{3,30})")
_AKA_RE = re.compile(r"\baka\s+([A-Za-z0-9_.]{3,30})", re.IGNORECASE)


def banned_terms(
    brand: str,
    past: Iterable[tuple[str, str]],
    shown_texts: Iterable[str],
    allowed_text: str,
) -> list[str]:
    """Names the deck must not contain.

    - ``past``: (client, campaign) of every past pitch shown to the models.
    - ``shown_texts``: past-deck text shown to the models; person-like names,
      CamelCase names and @handles in it are candidates for creator/partner names.
    - ``allowed_text``: research + catalog text; names that appear there are fine.
    The current brand is never banned.
    """
    brand_l = brand.lower().strip()
    allowed = allowed_text.lower()
    terms: dict[str, None] = {}

    def add(term: str, generic_filter: bool = True) -> None:
        term = " ".join(term.split()).strip(" .,:;-–|")
        low = term.lower()
        if len(low) < 4 or low in _LABEL_WORDS or low in _COMMON:
            return
        if generic_filter and " " in low and any(tok in _GENERIC_TOKENS for tok in re.split(r"[\s']+", low)):
            return
        if low in brand_l or brand_l in low or low in allowed:
            return
        terms.setdefault(term, None)

    for client, campaign in past:
        add(client, generic_filter=False)
        # Campaign names often read "<Brand> x <Idea>"; ban the parts, not the generic "x WLDD".
        for part in re.split(r"\s+(?:x|×|–|-)\s+", campaign):
            part = re.sub(r"\(.*?\)", "", part).strip()
            low = part.lower()
            if low in ("wldd", "") or any(c in low for c in _COMMON if " " in c or c in low.split()):
                continue  # "FIFA World Cup", "Diwali ..." are public moments, not secrets
            if low.startswith("the "):  # show titles: "The Traitors"
                add(part, generic_filter=False)
                add(part[4:], generic_filter=False)
            elif _looks_like_name(part):  # "Toofani Biryani Hunt", not "Always On" / "Retainer"
                add(part, generic_filter=False)
    for text in shown_texts:
        for m in _HANDLE_RE.finditer(text):
            add("@" + m.group(1))
            add(m.group(1))
        for m in _AKA_RE.finditer(text):
            add(m.group(1))
        for m in _CAMEL_RE.finditer(text):
            add(m.group(1))
        for m in _NAME_RE.finditer(text):
            if _looks_like_name(m.group(1)):
                add(m.group(1))
    return list(terms)


def slide_text(slide: Any) -> str:
    data = slide.model_dump(exclude={"notes"}) if hasattr(slide, "model_dump") else slide
    return json.dumps(data, ensure_ascii=False)


def _claims_text(slide: Any) -> str:
    """Slide text minus proposal fields (tiers, scale, timing), where numbers are plans, not facts."""

    def walk(v: Any) -> list[str]:
        if isinstance(v, dict):
            return [t for k, x in v.items() if k not in PROPOSAL_KEYS for t in walk(x)]
        if isinstance(v, list):
            return [t for x in v for t in walk(x)]
        return [v] if isinstance(v, str) else []

    data = slide.model_dump(exclude={"notes"}) if hasattr(slide, "model_dump") else slide
    return "\n".join(walk(data))


def find_issues(slides: list[Any], banned: list[str], research_text: str = "") -> dict[int, list[str]]:
    """Per slide index: banned names, @handles, unsupported stats, prices, filler, repeated hooks, duplicates."""
    issues: dict[int, list[str]] = {}
    seen_titles: list[tuple[int, str, str]] = []
    patterns = [(t, re.compile(r"(?<![A-Za-z0-9])" + re.escape(t) + r"(?![A-Za-z0-9])", re.IGNORECASE)) for t in banned]
    seen_hooks: dict[str, int] = {}
    for idx, slide in enumerate(slides):
        found: list[str] = []
        text = slide_text(slide)
        notes = getattr(slide, "notes", "") or ""
        full = text + " " + notes
        for term, pattern in patterns:
            if pattern.search(full):
                found.append(f"mentions confidential name '{term}'")
        if PRICE_RE.search(full):
            found.append("mentions a price/budget/fee")
        if BANNED_PLATFORM_RE.search(full):
            found.append("mentions TikTok, which is banned in India (use Instagram Reels / YouTube Shorts)")
        handles = HANDLE_RE.findall(full)
        if handles:
            found.append("names creator handles " + ", ".join(sorted(set(handles))[:5]) + " (use archetype + tier)")
        layout = getattr(slide, "layout", "")
        if research_text and layout != "kpi_table":
            stats = unsupported_stats(_claims_text(slide), research_text, allow_targets=layout not in NARRATIVE_LAYOUTS)
            if stats:
                found.append("statistic not in the research: " + ", ".join(stats[:4]) + " (remove, or label it as a target)")
        title = getattr(slide, "title", "") or ""
        for j, lay, other in seen_titles:
            if lay == layout and similar_titles(title, other):
                found.append(f"duplicates slide {j}")
                break
        seen_titles.append((idx, layout, title))
        for card in getattr(slide, "cards", []) or []:
            if not (card.body or "").strip():
                found.append(f"card '{card.title}' has no body")
        low = full.lower()
        filler = [w for w in FILLER if w in low]
        if filler:
            found.append("filler words: " + ", ".join(filler))
        hook = (getattr(slide, "hook", "") or "").strip().lower()
        if hook:
            if hook in seen_hooks:
                found.append(f"repeats the hook of slide {seen_hooks[hook]}")
            seen_hooks.setdefault(hook, idx)
        if found:
            issues[idx] = found
    return issues


# --------------------------------------------------------------------------- handles, stats, duplicates

HANDLE_RE = re.compile(r"(?<![\w.])@[A-Za-z0-9_.]{2,30}")
# Statistic-like numbers: percentages, multipliers and big counts ("27%", "3x", "2.4M", "10 crore", "1 billion").
STAT_RE = re.compile(
    r"(?<![\w.])(\.?\d+(?:[.,]\d+)?)\s?(%|x\b|×|m\b|mn\b|million|b\b|bn\b|billion|crore|cr\b|lakh|k\b)",
    re.IGNORECASE,
)
_TIER_CONTEXT = re.compile(r"\b(nano|micro|mid|macro|mega|tier|followers?|upto|up to|under|over)\b[^.]*$", re.IGNORECASE)
# Slides that state facts (context, tension, insight): a figure there is a claim even if called a "target".
NARRATIVE_LAYOUTS = {"cover", "statement", "from_to", "formula", "divider", "two_column", "ref"}
BANNED_PLATFORM_RE = re.compile(r"\btik\s?tok\b", re.IGNORECASE)
# Fields that hold our proposals (creator tiers, scale, timing), never claims about the brand or market.
PROPOSAL_KEYS = {"creator", "scalability", "format", "when", "tags", "query", "brief", "meme_boxes"}
_TARGET_CONTEXT = re.compile(r"target|goal|aim|we will|we'll|plan to|month \d|week \d|kpi", re.IGNORECASE)


def _norm_number(num: str) -> str:
    return num.replace(",", "").rstrip("0").rstrip(".") if "." in num else num.replace(",", "")


# Research figures, incl. spelled-out units ("1.2 million", "17 %", "₹6,500 crore").
_FIGURE_RE = re.compile(
    r"(?<![\w.])(\d+(?:[.,]\d+)?)\s?(%|percent|x\b|×|m\b|mn\b|million|b\b|bn\b|billion|crore|cr\b|lakh|k\b|thousand)",
    re.IGNORECASE,
)
_UNITS = {"%": "pct", "percent": "pct", "x": "x", "×": "x", "m": "M", "mn": "M", "million": "M", "b": "B", "bn": "B",
          "billion": "B", "crore": "cr", "cr": "cr", "lakh": "lakh", "k": "K", "thousand": "K"}


def _unit(raw: str) -> str:
    return _UNITS.get(raw.lower(), raw.lower())


def unsupported_stats(text: str, research_text: str, allow_targets: bool = True) -> list[str]:
    """Statistic-like numbers in ``text`` that the research never states (targets excepted).

    A number is supported when the same figure appears in the research; it is a
    target when the 40 characters before it say so ("Target: 2M views").
    """
    research_figures: dict[tuple[str, str], list[str]] = {}
    for m in _FIGURE_RE.finditer(research_text):
        key = (_norm_number(m.group(1)), _unit(m.group(2)))
        research_figures.setdefault(key, []).append(research_text[m.end() : m.end() + 80].lower())
    found = []
    for m in STAT_RE.finditer(text):
        key = (_norm_number(m.group(1)), _unit(m.group(2)))
        if key in research_figures:
            # Same figure and unit; it must also count the same thing ("1.2M followers" != "1.2M restaurants").
            noun = re.match(r"[\s+]*(?:of\s+|monthly\s+|active\s+|more\s+)?([a-z]{4,})", text[m.end() : m.end() + 30].lower())
            if not noun or any(noun.group(1)[:5] in after for after in research_figures[key]):
                continue
        before = text[max(0, m.start() - 40) : m.start()]
        if allow_targets and _TARGET_CONTEXT.search(before):
            continue
        # Creator tiers and ranges are proposals, not facts: "micro 50-200K", "100K+ followers".
        if re.search(r"\d\s*[-–‑~]\s*$", before) or _TIER_CONTEXT.search(before[-25:]):
            continue
        if re.match(r"\s*\+?\s*(followers|subs|subscribers)|\s*[-–‑]\s*\d", text[m.end() : m.end() + 16]):
            continue
        found.append(m.group(0).strip())
    return found


def strip_handles(text: str) -> str:
    """Drop @handles (creators are described by archetype + tier, never named)."""
    handle = r"@[A-Za-z0-9_.]{2,30}"
    out = re.sub(rf"(?<![\w.]){handle}(?:\s*(?:,|and|&|/|or)\s*{handle})*(?:\s*(?:,|and|&|or)\s+)?", "", text)
    out = re.sub(r":\s*[,;]", ":", out)
    out = re.sub(r"\s*,\s*(,\s*)+", ", ", out)
    out = re.sub(r"\(\s*[,;]?\s*\)", "", out)
    return re.sub(r"\s{2,}", " ", out).strip(" ,;")


def soften_stats(text: str, stats: list[str]) -> str:
    """Remove the sentences (or, in a one-sentence field, the clauses) that carry an unsupported figure."""
    if not stats:
        return text
    sentences = re.split(r"(?<=[.!?])\s+", text)
    kept = [s for s in sentences if not any(st in s for st in stats)]
    if kept:
        return " ".join(kept)
    clauses = re.split(r"\s*(?:;|,|\u2014|\s-\s|\|)\s*", text)
    kept = [c for c in clauses if c.strip() and not any(st in c for st in stats)]
    return ", ".join(kept).strip(" ,;:")


def similar_titles(a: str, b: str) -> bool:
    a, b = a.lower().strip(), b.lower().strip()
    return a == b or difflib.SequenceMatcher(None, a, b).ratio() >= 0.8
