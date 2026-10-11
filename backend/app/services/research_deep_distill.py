"""Deep research, part 2: select chunks, distil per question, verify, reduce (Owner: Agent D).

- Chunk each page to ~800 tokens (paragraph packing, ~100-token overlap). MiniLM only
  reads ~256 tokens, so each chunk is embedded as ~225-token sub-passages and a chunk
  ranks by its best passage.
- Embeddings go into a throwaway in-memory Chroma collection (never the persisted store).
- ~8 research questions each take their best chunks round-robin (a chunk serves one
  question only), packed to a per-call token budget (~2.4K prompt tokens).
- One ``llm_router.invoke_json("distiller", ...)`` per question, in parallel with
  ``prefer=i``. Excerpts are labelled ``S1..Sk``; the model cites labels and the code maps
  them to URLs, so every fact's ``source_url`` is exact.
- Verification in code: every figure in a fact (text and ``number``) must appear in the
  cited source text with the same digits and unit, else the fact is dropped; names of
  products/campaigns/competitors must appear in the excerpts.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from typing import Annotated, Any

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, BeforeValidator, Field

from app.services import llm_router
from app.services.research_deep_fetch import Page, cache_get, cache_put, host_of

logger = logging.getLogger(__name__)

CHUNK_CHARS = 3200  # ~800 tokens
OVERLAP_CHARS = 400
PASSAGE_CHARS = 900  # ~225 tokens: inside MiniLM's 256-token window
DISTILL_MAX_TOKENS = 650
PROMPT_TARGET = {"focused": 2200, "standard": 2400, "flagship": 2800}
MAX_DISTANCE = 0.80  # cosine distance above this = unrelated


# --------------------------------------------------------------------------- questions


@dataclass(frozen=True)
class Question:
    key: str
    query: str  # retrieval query
    ask: str  # what the distiller should pull out
    fields: tuple[str, ...]  # output fields besides "facts"


def questions(brand: str, brief: str | None, season: str | None, scale: str) -> list[Question]:
    qs = [
        Question("products", f"{brand} products, range, menu, flavours, features, new launches, what it sells",
                 "What does the brand sell or offer: named products, variants, sub-brands, app features, new "
                 "launches? Also write a 2-3 sentence factual summary of the brand.",
                 ("summary", "products")),
        Question("audience", f"{brand} customers, target audience, community, fans, who uses {brand}",
                 "Who are the brand's customers and community (age, cities, lifestyle, why they buy)?",
                 ("audience",)),
        Question("tone_campaigns", f"{brand} advertising campaign, ad film, tagline, brand voice, social media campaign",
                 "Recent named campaigns/ad films/taglines (what, when) and how the brand talks (tone of voice).",
                 ("campaigns", "tone_of_voice")),
        Question("numbers", f"{brand} users, customers, revenue, growth, market share, market size, crore, million, percent",
                 "Hard numbers about the brand and its market: users, orders, revenue, growth, share, market size, "
                 "reach. Copy each figure exactly as written.",
                 ("competitors",)),
        Question("competitors", f"{brand} competitors, rivals, alternatives, other brands, versus, compared with, "
                 "market leader, category players",
                 "Competitors and the category: who they are and how they compete or market themselves.",
                 ("competitors",)),
        Question("moments", f"{brand} festive season, {season or 'Diwali'}, IPL, cricket, sale, occasions, events",
                 "Cultural moments, seasons, festivals, sports, sales and occasions the brand ties into.",
                 ("moments", "cultural_hooks")),
        Question("buzz", f"{brand} viral, meme, Instagram reel, trend, Twitter, social media buzz, influencer",
                 "Social buzz: viral posts, memes, trends, creator collaborations, what people say online about the brand.",
                 ("social_signals", "cultural_hooks")),
    ]
    if brief and brief.strip():
        qs.append(Question("brief", f"{brand} {brief.strip()[:300]}",
                           f"Anything relevant to this client brief: {brief.strip()[:300]}",
                           ("campaigns", "audience")))
    if scale == "focused":
        keep = {"products", "audience", "tone_campaigns", "numbers", "buzz", "brief"}
        qs = [q for q in qs if q.key in keep]
    return qs


# --------------------------------------------------------------------------- chunks + selection


@dataclass
class Chunk:
    idx: int
    page: Page
    text: str
    passages: list[str] = field(default_factory=list)


def chunk_pages(pages: list[Page]) -> list[Chunk]:
    chunks: list[Chunk] = []
    for page in pages:
        paras = [p.strip() for p in re.split(r"\n\s*\n", page.markdown) if p.strip()]
        cur: list[str] = []
        size = 0

        def flush() -> None:
            if cur:
                chunks.append(Chunk(len(chunks), page, "\n\n".join(cur)))

        for para in paras:
            while len(para) > CHUNK_CHARS:  # giant paragraph: hard split on sentences
                cut = para.rfind(". ", 0, CHUNK_CHARS)
                cut = cut + 1 if cut > CHUNK_CHARS // 2 else CHUNK_CHARS
                piece, para = para[:cut].strip(), para[cut:].strip()
                if size + len(piece) > CHUNK_CHARS:
                    flush()
                    cur, size = [], 0
                cur.append(piece)
                size += len(piece)
            if size + len(para) > CHUNK_CHARS and cur:
                flush()
                tail: list[str] = []
                tsize = 0
                for prev in reversed(cur):  # overlap: carry the last paragraph(s)
                    if tsize + len(prev) > OVERLAP_CHARS:
                        break
                    tail.insert(0, prev)
                    tsize += len(prev)
                cur, size = tail, tsize
            cur.append(para)
            size += len(para)
        flush()
    for ch in chunks:
        t = ch.text
        step = PASSAGE_CHARS - 150
        ch.passages = [t[i : i + PASSAGE_CHARS] for i in range(0, max(1, len(t) - 150), step)] or [t]
    return chunks


def _embed(texts: list[str]) -> list[list[float]]:
    from app.vectorstore.embeddings import ChromaDefaultEmbeddings

    return ChromaDefaultEmbeddings().embed_documents(texts)


def rank_chunks(chunks: list[Chunk], qs: list[Question]) -> dict[str, list[tuple[float, int, int]]]:
    """question key -> [(distance, chunk idx, passage idx)] best first (sync; run in a thread)."""
    import chromadb

    ids, metas, docs = [], [], []
    for ch in chunks:
        for j, p in enumerate(ch.passages):
            ids.append(f"{ch.idx}:{j}")
            metas.append({"chunk": ch.idx, "passage": j})
            docs.append(f"{ch.page.title}\n{p}")
    if not ids:
        return {q.key: [] for q in qs}
    vectors = _embed(docs)
    qvecs = _embed([q.query for q in qs])
    client = chromadb.EphemeralClient()
    name = f"dossier_{uuid.uuid4().hex[:12]}"
    col = client.create_collection(name=name, metadata={"hnsw:space": "cosine"}, embedding_function=None)
    try:
        for start in range(0, len(ids), 500):
            col.add(ids=ids[start : start + 500], embeddings=vectors[start : start + 500], metadatas=metas[start : start + 500])
        res = col.query(query_embeddings=qvecs, n_results=min(len(ids), 60), include=["metadatas", "distances"])
    finally:
        try:
            client.delete_collection(name)
        except Exception:  # noqa: BLE001
            pass
    ranked: dict[str, list[tuple[float, int, int]]] = {}
    for q, metas_q, dists in zip(qs, res["metadatas"], res["distances"]):
        best: dict[int, tuple[float, int]] = {}
        for m, d in zip(metas_q, dists):
            c = int(m["chunk"])
            if c not in best or d < best[c][0]:
                best[c] = (float(d), int(m["passage"]))
        ranked[q.key] = sorted((d, c, p) for c, (d, p) in best.items() if d <= MAX_DISTANCE)
    return ranked


@dataclass
class Excerpt:
    label: str
    url: str
    title: str
    text: str


def assign_excerpts(chunks: list[Chunk], qs: list[Question], ranked: dict[str, list[tuple[float, int, int]]],
                    budget_tokens: int) -> dict[str, list[Excerpt]]:
    """Round-robin: each question takes its next best unused chunk while it fits its budget."""
    used: set[int] = set()
    out: dict[str, list[Excerpt]] = {q.key: [] for q in qs}
    spent = {q.key: 0 for q in qs}
    per_url: dict[str, dict[str, int]] = {q.key: {} for q in qs}
    cursor = {q.key: 0 for q in qs}
    active = [q.key for q in qs]
    while active:
        for key in list(active):
            cands = ranked.get(key, [])
            placed = False
            while cursor[key] < len(cands):
                _, c, p = cands[cursor[key]]
                cursor[key] += 1
                if c in used:
                    continue
                ch = chunks[c]
                if per_url[key].get(ch.page.url, 0) >= 2:
                    continue
                text = ch.text
                cost = llm_router.estimate_tokens(text) + 30
                if spent[key] + cost > budget_tokens:
                    text = ch.passages[p]  # the matched passage alone may still fit
                    cost = llm_router.estimate_tokens(text) + 30
                    if spent[key] + cost > budget_tokens:
                        continue
                used.add(c)
                spent[key] += cost
                per_url[key][ch.page.url] = per_url[key].get(ch.page.url, 0) + 1
                out[key].append(Excerpt(f"S{len(out[key]) + 1}", ch.page.url, ch.page.title, text))
                placed = True
                break
            if not placed:
                active.remove(key)
    return out


# --------------------------------------------------------------------------- distiller schema


def _src(v: Any) -> str:
    m = re.search(r"\d+", str(v or ""))
    return f"S{m.group(0)}" if m else ""


def _notes(v: Any) -> Any:
    if isinstance(v, list):
        return [{"text": x} if isinstance(x, str) else x for x in v]
    return v


Src = Annotated[str, BeforeValidator(_src)]


class _Fact(BaseModel):
    text: str
    number: str | None = None
    src: Src = ""


class _Product(BaseModel):
    name: str
    detail: str = ""
    src: Src = ""


class _Campaign(BaseModel):
    name: str
    when: str = ""
    what: str = ""
    src: Src = ""


class _Competitor(BaseModel):
    name: str
    angle: str = ""
    src: Src = ""


class _Note(BaseModel):
    text: str
    src: Src = ""


Notes = Annotated[list[_Note], BeforeValidator(_notes)]


class Distilled(BaseModel):
    summary: str = ""
    tone_of_voice: str = ""
    facts: list[_Fact] = Field(default_factory=list)
    products: list[_Product] = Field(default_factory=list)
    campaigns: list[_Campaign] = Field(default_factory=list)
    competitors: list[_Competitor] = Field(default_factory=list)
    audience: Notes = Field(default_factory=list)
    cultural_hooks: Notes = Field(default_factory=list)
    social_signals: Notes = Field(default_factory=list)
    moments: Notes = Field(default_factory=list)


_FIELD_SHAPES = {
    "summary": '"summary": "2-3 factual sentences"',
    "tone_of_voice": '"tone_of_voice": "how the brand talks, one sentence"',
    "products": '"products": [{"name": "...", "detail": "what it is", "src": "S1"}]',
    "campaigns": '"campaigns": [{"name": "...", "when": "month/year as written", "what": "what happened", "src": "S2"}]',
    "competitors": '"competitors": [{"name": "...", "angle": "how they compete", "src": "S1"}]',
    "audience": '"audience": [{"text": "segment + why", "src": "S1"}]',
    "cultural_hooks": '"cultural_hooks": [{"text": "...", "src": "S1"}]',
    "social_signals": '"social_signals": [{"text": "...", "src": "S1"}]',
    "moments": '"moments": [{"text": "...", "src": "S1"}]',
}

_SYSTEM = (
    "You are a research analyst extracting facts about the brand {brand} for an agency pitch deck. "
    "Use ONLY the numbered source excerpts you are given; never add outside knowledge. "
    "Each item cites the excerpt it came from (\"src\": \"S2\"). "
    "Numbers: copy every figure exactly as written in the excerpt (same digits, unit and wording, e.g. "
    "\"20 crore\", \"35%\", \"Rs 1,200 crore\"); never round, convert, compute or estimate. "
    "Facts must be specific (names, figures, dates), one claim each, at most 220 characters, and about "
    "{brand}, its products, audience, campaigns, competitors or category: the kind of thing a strategist "
    "would put on a pitch slide (scale, growth, launches, audience insight, campaigns, distribution, pricing, "
    "social performance). Never describe the article or web page itself; skip site navigation, legal text, SEO/"
    "email trivia and anything generic. Write in English. Return one JSON object only."
)


def _prompt(brand: str, q: Question, excerpts: list[Excerpt]) -> list[Any]:
    shapes = ['"facts": [{"text": "one specific claim", "number": "figure as written or null", "src": "S1"}]']
    shapes += [_FIELD_SHAPES[f] for f in q.fields]
    body = "\n\n".join(f"[{e.label}] {e.title} ({host_of(e.url)})\n{e.text}" for e in excerpts)
    human = (
        f"Question: {q.ask}\n\nSOURCE EXCERPTS\n{body}\n\n"
        "Return JSON with these keys (up to 6 facts, up to 5 items per list; empty lists when the excerpts say "
        "nothing):\n{" + ", ".join(shapes) + "}"
    )
    return [SystemMessage(_SYSTEM.format(brand=brand)), HumanMessage(human)]


def prompt_overhead(brand: str, q: Question) -> int:
    return llm_router.estimate_tokens("".join(m.content for m in _prompt(brand, q, []))) + 160


async def distil_question(brand: str, q: Question, excerpts: list[Excerpt], prefer: int) -> Distilled | None:
    if not excerpts:
        return None
    messages = _prompt(brand, q, excerpts)
    key = "distil:" + hashlib.sha1(json.dumps([m.content for m in messages]).encode()).hexdigest()
    cached = cache_get(brand, key)
    if cached:
        try:
            return Distilled.model_validate(cached)
        except ValueError:
            pass
    try:
        out = await llm_router.invoke_json("distiller", messages, Distilled,
                                           max_tokens=DISTILL_MAX_TOKENS, temperature=0.2, prefer=prefer)
    except Exception as exc:  # noqa: BLE001 - one question failing must not sink the dossier
        logger.warning("deep research distil %s/%s failed: %s", brand, q.key, str(exc)[:200])
        return None
    cache_put(brand, key, out.model_dump())
    return out


# --------------------------------------------------------------------------- verification


_UNIT_ALIASES = {
    "%": "pct", "percent": "pct", "per cent": "pct", "pc": "pct",
    "x": "x", "×": "x", "times": "x",
    "m": "M", "mn": "M", "million": "M", "millions": "M", "b": "B", "bn": "B", "billion": "B", "billions": "B",
    "crore": "cr", "crores": "cr", "cr": "cr", "lakh": "lakh", "lakhs": "lakh", "lac": "lakh", "k": "K",
    "thousand": "K", "tonnes": "t", "tons": "t", "plus": "+", "+": "+",
}
_FIG_RE = re.compile(
    r"(?<![\w.])(\d{1,3}(?:,\d{2,3})+(?:\.\d+)?|\d+(?:\.\d+)?)\s?"
    r"(%|per ?cent|percent|x\b|×|times\b|crores?\b|cr\b|lakhs?\b|lac\b|millions?\b|mn\b|m\b|billions?\b|bn\b|b\b|"
    r"k\b|thousand\b|tonnes\b|tons\b|\+)?",
    re.IGNORECASE,
)


def _norm_num(n: str) -> str:
    n = n.replace(",", "")
    if "." in n:
        n = n.rstrip("0").rstrip(".")
    return n


def figures(text: str) -> list[tuple[str, str, str]]:
    """(normalised number, normalised unit, as written) for each figure in ``text``."""
    out = []
    for m in _FIG_RE.finditer(text or ""):
        unit = (m.group(2) or "").lower()
        unit = _UNIT_ALIASES.get(unit, unit)
        out.append((_norm_num(m.group(1)), unit, m.group(0).strip()))
    return out


def _source_figures(text: str) -> tuple[set[str], set[tuple[str, str]]]:
    figs = figures(text)
    return {n for n, _, _ in figs}, {(n, u) for n, u, _ in figs if u}


def number_supported(claim: str, source: str) -> bool:
    """Every figure in ``claim`` appears in ``source`` with the same digits (and unit, when it has one)."""
    nums, with_units = _source_figures(source)
    for n, u, _ in figures(claim):
        if n not in nums:
            return False
        if u and u != "+" and (n, u) not in with_units:
            return False
    return True


def headline_figure(text: str) -> str | None:
    """The figure worth showing for a fact: has a unit/currency, or is a big number (not "Tier 1" or a year)."""
    for m in _FIG_RE.finditer(text or ""):
        n, unit = _norm_num(m.group(1)), (m.group(2) or "")
        before = text[max(0, m.start() - 4) : m.start()]
        currency = bool(re.search(r"(₹|rs\.?|inr|\$|usd)\s*$", before, re.IGNORECASE))
        if re.fullmatch(r"(19|20)\d\d", n) and not unit:
            continue
        if unit or currency or "," in m.group(1) or float(n or 0) >= 100:
            start = m.start() - len(re.search(r"(₹|rs\.?\s?|inr\s?|\$|usd\s?)?$", before, re.IGNORECASE).group(0))
            end = m.end()
            rng = re.match(r"\s?[-–]\s?\d[\d,.]*\s?(%|crore|cr|lakh|million|mn|billion|bn|k)?", text[end:], re.IGNORECASE)
            if rng and not unit:
                end += rng.end()
            return text[start:end].strip()
    return None


_META_RE = re.compile(r"(the|this) (article|excerpt|page|post|blog|source|text|website)\b|.*\b(spam rating|subject lines?|"
                      r"\bseo\b|meta description|page speed|bounce rate|domain authority)", re.IGNORECASE)
_WORD_RE = re.compile(r"[a-z][a-z'’-]{3,}")
_STOP = set("this that with from have their they them were been into about which while where there these those "
            "brand also more most such than very will would could should other over under after before being".split())


def _content_words(text: str) -> set[str]:
    return {w for w in _WORD_RE.findall(text.lower()) if w not in _STOP}


def grounded(text: str, source: str, share: float = 0.3) -> bool:
    words = _content_words(text)
    if not words:
        return False
    src = source.lower()
    return sum(1 for w in words if w in src or w[:-1] in src) / len(words) >= share


def name_in(name: str, source: str) -> bool:
    low = source.lower()
    if name.lower() in low:
        return True
    toks = [t for t in re.findall(r"[a-z0-9]{3,}", name.lower()) if t not in _STOP]
    return bool(toks) and sum(t in low for t in toks) >= max(1, (len(toks) + 1) // 2)


# --------------------------------------------------------------------------- reduce


@dataclass
class Item:
    """A verified distilled item with its exact source."""

    kind: str  # fact | product | campaign | competitor | audience | cultural_hooks | social_signals | moments
    q: str
    data: dict[str, Any]
    url: str
    title: str


def verify(brand: str, q: Question, d: Distilled, excerpts: list[Excerpt]) -> tuple[list[Item], dict[str, str], int]:
    """Verified items, scalar fields (summary/tone) and the number of dropped items."""
    by_label = {e.label: e for e in excerpts}
    all_text = "\n".join(e.text for e in excerpts)
    items: list[Item] = []
    dropped = 0

    def source(label: str, probe: str) -> Excerpt | None:
        if label in by_label:
            return by_label[label]
        # Missing/wrong label: the excerpt that actually contains the claim's figures/words.
        for e in excerpts:
            if (figures(probe) and number_supported(probe, e.text)) or (not figures(probe) and grounded(probe, e.text, 0.5)):
                return e
        return None

    for f in d.facts:
        text = " ".join(f.text.split())[:300]
        number = (f.number or "").strip() or None
        if number and (number.lower() in ("null", "none", "n/a", "-") or not re.search(r"\d", number)):
            number = None
        probe = text + (" " + number if number else "")
        ex = source(f.src, probe)
        if not ex or len(text) < 15 or _META_RE.match(text):
            dropped += 1
            continue
        same_url = "\n".join(e.text for e in excerpts if e.url == ex.url)
        if not number_supported(probe, same_url) or not grounded(text, same_url):
            dropped += 1
            continue
        if not number:
            number = headline_figure(text)
        items.append(Item("fact", q.key, {"text": text, "number": number}, ex.url, ex.title))

    def named(kind: str, rows: list[Any], name_attr: str, fields: tuple[str, ...]) -> None:
        nonlocal dropped
        for r in rows:
            name = " ".join(getattr(r, name_attr).split())
            if not name or not name_in(name, all_text) or name.lower() == brand.lower() and kind != "product":
                dropped += 1
                continue
            data = {name_attr: name, **{f: " ".join(str(getattr(r, f) or "").split()) for f in fields}}
            blob = " ".join(str(v) for v in data.values())
            ex = source(r.src, blob) or excerpts[0]
            if not number_supported(blob, all_text):  # an invented figure in a detail: keep the name only
                data = {name_attr: name, **{f: "" for f in fields}}
            items.append(Item(kind, q.key, data, ex.url, ex.title))

    named("product", d.products, "name", ("detail",))
    named("campaign", d.campaigns, "name", ("when", "what"))
    named("competitor", d.competitors, "name", ("angle",))
    for kind in ("audience", "cultural_hooks", "social_signals", "moments"):
        for n in getattr(d, kind):
            text = " ".join(n.text.split())
            if len(text) < 8 or not number_supported(text, all_text) or not grounded(text, all_text, 0.25):
                dropped += 1
                continue
            ex = source(n.src, text) or excerpts[0]
            items.append(Item(kind, q.key, {"text": text}, ex.url, ex.title))
    scalars = {}
    for key in ("summary", "tone_of_voice"):
        val = " ".join(getattr(d, key).split())
        if val and number_supported(val, all_text):
            scalars[key] = val
    return items, scalars, dropped


def _norm_text(t: str, drop: frozenset[str] = frozenset()) -> set[str]:
    return {w[:5] for w in _content_words(t.replace("’", "'")) if w.split("'")[0] not in drop}


def similar(a: str, b: str, threshold: float = 0.6, drop: frozenset[str] = frozenset()) -> bool:
    wa, wb = _norm_text(a, drop), _norm_text(b, drop)
    if not wa or not wb:
        return a.strip().lower() == b.strip().lower()
    return len(wa & wb) / min(len(wa), len(wb)) >= threshold


def dedupe_facts(items: list[Item], qs_order: list[str], brand: str = "") -> list[Item]:
    """Drop near-duplicate facts (same figure + similar words, or very similar words); round-robin by question."""
    facts = [i for i in items if i.kind == "fact"]
    drop = frozenset(w for w in re.findall(r"[a-z]+", brand.lower()) if len(w) >= 4)
    kept: list[Item] = []

    def figs(it: Item) -> set[str]:
        return {n for n, u, _ in figures(it.data["text"] + " " + (it.data.get("number") or ""))
                if u or float(n or 0) >= 10}

    for f in facts:
        fn = figs(f)
        dup = False
        for k in kept:
            kn = figs(k)
            a, b = f.data["text"], k.data["text"]
            if (fn and fn == kn and similar(a, b, 0.25, drop)) or (fn and fn & kn and similar(a, b, 0.45, drop)) \
                    or (not fn and not kn and similar(a, b, 0.7, drop)):
                dup = True
                if len(f.data["text"]) > len(k.data["text"]) and fn >= kn:
                    k.data, k.url, k.title = f.data, f.url, f.title
                break
        if not dup:
            kept.append(f)
    by_q: dict[str, list[Item]] = {}
    for f in kept:
        by_q.setdefault(f.q, []).append(f)
    for lst in by_q.values():
        lst.sort(key=lambda i: 0 if i.data.get("number") else 1)
    order = [q for q in qs_order if q in by_q] + [q for q in by_q if q not in qs_order]
    out: list[Item] = []
    while any(by_q.get(q) for q in order):
        for q in order:
            if by_q.get(q):
                out.append(by_q[q].pop(0))
    return out


_NAME_FILLER = {"the", "and", "of", "for", "with", "range", "pack", "packs", "mix", "bottle", "bottles", "bag", "bags",
                "new", "series", "collection", "campaign", "ad", "film"}


def name_tokens(name: str) -> frozenset[str]:
    toks = re.findall(r"[a-z0-9]+", name.lower().replace("‑", "-"))
    return frozenset(t.rstrip("s") if len(t) > 3 else t for t in toks if t not in _NAME_FILLER) or frozenset(toks)


def merge_named(items: list[Item], kind: str, key: str) -> list[Item]:
    """Merge items whose names match after normalising (plurals, filler words, one name inside the other)."""
    out: dict[frozenset[str], Item] = {}
    for it in items:
        if it.kind != kind:
            continue
        norm = name_tokens(it.data[key])
        match = next((k for k in out if k == norm or (min(len(k), len(norm)) >= 2 and (k <= norm or norm <= k))), None)
        prev = out.get(match) if match is not None else None
        if prev is None:
            out[norm] = it
            continue
        for f, v in it.data.items():
            if len(str(v)) > len(str(prev.data.get(f) or "")):
                prev.data[f] = v
    return list(out.values())


def merge_notes(items: list[Item], kind: str) -> list[Item]:
    out: list[Item] = []
    for it in items:
        if it.kind == kind and not any(similar(it.data["text"], o.data["text"], 0.6) for o in out):
            out.append(it)
    return out
