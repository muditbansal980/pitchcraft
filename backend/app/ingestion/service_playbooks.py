"""Build the service playbooks from past pitches (Owner: Agent D).

    uv run python -m app.ingestion.service_playbooks [--only "SOLO,Memed"] [--dry-run]

For every catalog service (``load_services_catalog()``, MISCL excluded):
1. Pitches that sold it: the CSV ``services_pitched`` column (matched to catalog names).
2. Their deck chunks from the Chroma pitches store: chunks naming the service first, then
   the most similar ones, at most 2 per pitch, packed to ~2.3K tokens.
3. Anonymised before the LLM sees them: client/campaign/creator names and @handles found
   with ``plan_checks.banned_terms`` are replaced by "[name]".
4. One LLM call ("writer" role) distils a ``ServicePlaybook``. Services nobody pitched get
   one built from the services PDF description + general agency practice (``basis="general"``).
5. Output is scrubbed again (items naming a banned term or a handle are dropped) and written
   to ``settings.service_playbooks_path`` — gitignored: derived from confidential decks.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from app.core.config import settings
from app.services import llm_router
from app.services.plan_checks import _COMMON, _HANDLE_RE, _looks_like_name, banned_terms, match_service
from app.services.retrieval import Service, load_services_catalog
from app.services.service_playbooks import ServicePlaybook
from app.vectorstore.chroma import get_pitches_store

logger = logging.getLogger(__name__)

CHUNK_BUDGET_TOKENS = 2300
MAX_TOKENS = 1100
CSV_NAME = "past_pitches.csv"

# Words that identify a service inside a deck chunk.
_SERVICE_KEYWORDS = {
    "Amplification": ["amplification", "voxpop", "vox pop", "meme page", "meme pages", "distribution science", "dp ",
                      "display picture", "seeding", "handles"],
    "Influencers": ["influencer", "macro", "micro", "nano", "creator collab"],
    "SOLO": ["solo", "ugc", "marketplace", "creators at scale"],
    "Memed": ["memed", "meme creator", "clips"],
    "Production": ["production", "shoot", "dvc", "film", "set ", "crew", "script"],
    "ORM": ["orm", "whatsapp", "telegram", "sentiment", "reputation", "comments"],
    "Content Creation": ["illustration", "motion graphic", "static", "carousel", "design"],
    "Advanced Campaign Analytics": ["dashboard", "analytics", "reach", "engagement rate", "benchmark"],
    "Offline": ["on-ground", "on ground", "college", "fest", "pop-up", "meetup", "irl"],
    "Twitter Trend": ["trend", "hashtag", "twitter", " x "],
}


# Public platforms/moments/jargon that are not client secrets.
_GENERIC_EXTRA = {"wldd", "fmcg", "chatgpt", "hollywood", "fifa", "ipl", "mon", "tue", "wed", "thu", "fri", "sat", "sun",
                  "instagram", "youtube", "reels", "asia", "pacific", "dvc", "ugc", "orm", "solo", "memed", "gen"}


class _Draft(BaseModel):
    mechanics: list[str] = Field(default_factory=list)
    formats: list[str] = Field(default_factory=list)
    typical_volumes: list[str] = Field(default_factory=list)
    creator_tiers: list[str] = Field(default_factory=list)
    handle_categories: list[str] = Field(default_factory=list)
    phase_fit: list[str] = Field(default_factory=list)
    deliverables: list[str] = Field(default_factory=list)
    kpis: list[str] = Field(default_factory=list)
    best_for: list[str] = Field(default_factory=list)
    pairs_with: list[str] = Field(default_factory=list)
    examples: list[str] = Field(default_factory=list)


def _keywords(service: Service) -> list[str]:
    for key, words in _SERVICE_KEYWORDS.items():
        if service.name.lower().startswith(key.lower()):
            return words
    return [service.name.split("(")[0].strip().lower()]


def _read_csv() -> list[dict[str, str]]:
    path = Path(settings.archive_dir) / CSV_NAME
    with path.open(encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def sold_by(catalog: list[Service], rows: list[dict[str, str]]) -> dict[str, list[str]]:
    """Catalog service name -> pitch ids whose ``services_pitched`` includes it."""
    out: dict[str, list[str]] = {s.name: [] for s in catalog}
    for row in rows:
        for raw in re.split(r"[;,]", row.get("services_pitched") or ""):
            svc = match_service(raw.strip(), catalog)
            if svc and row["pitch_id"] not in out[svc.name]:
                out[svc.name].append(row["pitch_id"])
    return out


def _deck_chunks(pitch_ids: list[str]) -> list[tuple[str, int, str]]:
    """(pitch_id, chunk_index, text without the "Past pitch ..." header) for these pitches."""
    if not pitch_ids:
        return []
    data = get_pitches_store()._collection.get(  # noqa: SLF001 - same access pattern as retrieval.py
        where={"$and": [{"kind": "deck"}, {"pitch_id": {"$in": pitch_ids}}]}, include=["documents", "metadatas"])
    out = []
    for doc, meta in zip(data.get("documents") or [], data.get("metadatas") or []):
        text = doc.split("\n", 1)[1] if doc.startswith("Past pitch") and "\n" in doc else doc
        out.append((str(meta.get("pitch_id")), int(meta.get("chunk_index", 0)), text.strip()))
    return out


def _select(service: Service, pitch_ids: list[str]) -> list[tuple[str, str]]:
    """Up to ~CHUNK_BUDGET_TOKENS of the most service-relevant chunks, spread over pitches."""
    chunks = _deck_chunks(pitch_ids)
    if not chunks:
        return []
    words = _keywords(service)
    by_key = {(p, i): t for p, i, t in chunks}
    scored: dict[tuple[str, int], float] = {}
    for p, i, t in chunks:
        low = f" {t.lower()} "
        hits = sum(low.count(w) for w in words)
        if hits:
            scored[(p, i)] = 1.0 + min(hits, 6) * 0.3
    try:
        sims = get_pitches_store().similarity_search_with_score(
            f"{service.name}: {service.description[:500]}", k=40,
            filter={"$and": [{"kind": "deck"}, {"pitch_id": {"$in": pitch_ids}}]})
        for doc, dist in sims:
            key = (str(doc.metadata.get("pitch_id")), int(doc.metadata.get("chunk_index", 0)))
            scored[key] = scored.get(key, 0.0) + max(0.0, 1.0 - float(dist))
    except Exception as exc:  # noqa: BLE001 - keyword ranking still works
        logger.info("playbooks: similarity search failed for %s: %s", service.name, exc)
    ranked = sorted(scored, key=lambda k: -scored[k])
    picked: list[tuple[str, str]] = []
    used: set[tuple[str, int]] = set()
    per_pitch: dict[str, int] = {}
    spent = 0
    for rnd in (1, 2, 3):  # spread first: one chunk per pitch, then a second, a third
        for key in ranked:
            p = key[0]
            if per_pitch.get(p, 0) >= rnd or key in used:
                continue
            text = by_key[key]
            cost = llm_router.estimate_tokens(text) + 12
            if spent + cost > CHUNK_BUDGET_TOKENS:
                continue
            used.add(key)
            picked.append((p, text))
            per_pitch[p] = per_pitch.get(p, 0) + 1
            spent += cost
    return picked


def _banned(rows: list[dict[str, str]], texts: list[str], catalog: list[Service]) -> list[str]:
    past = [(r.get("client", ""), r.get("campaign", "")) for r in rows]
    shown = texts + [r.get("brief", "") for r in rows]
    allowed = " ".join(f"{s.name} {s.description}" for s in catalog)
    terms = banned_terms("WLDD", past, shown, allowed)
    # Brand names from briefs/campaigns that banned_terms' generic filter may keep (sub-brands, parents).
    extra = set()
    for r in rows:
        for part in re.split(r"[(),;:–\-]| x | for |\.", f"{r.get('campaign', '')} {r.get('brief', '')}"):
            for name in re.findall(r"\b[A-Z][a-zA-Z]+(?:\s[A-Z][a-zA-Z]+)*", part):
                toks = set(name.lower().split())
                if (len(name) >= 4 and _looks_like_name(name) and name.lower() not in _COMMON
                        and not toks & (_COMMON | _GENERIC_EXTRA)):
                    extra.add(name)
    return sorted(set(terms) | extra, key=len, reverse=True)


def _scrub(text: str, banned: list[str]) -> str:
    out = text
    for term in banned:
        out = re.sub(r"(?<![A-Za-z0-9])" + re.escape(term) + r"(?![A-Za-z0-9])", "[name]", out, flags=re.IGNORECASE)
    out = _HANDLE_RE.sub("[handle]", out)
    return re.sub(r"(\[name\][\s,&/]*){2,}", "[name] ", out)


def _leaks(text: str, banned: list[str]) -> list[str]:
    found = [t for t in banned if re.search(r"(?<![A-Za-z0-9])" + re.escape(t) + r"(?![A-Za-z0-9])", text, re.IGNORECASE)]
    if _HANDLE_RE.search(text) or "[name]" in text or "[handle]" in text:
        found.append("placeholder/handle")
    return found


_SYSTEM = (
    "You write the internal playbook for one service of WLDD, an Indian meme-marketing and creator agency. "
    "The playbook teaches deck writers how WLDD sells and runs this service: concrete mechanics, formats, "
    "volumes, creator tiers, distribution handle categories (e.g. meme pages, fan pages, regional pages), "
    "phase fit (tease / launch / sustain / always-on), deliverables, KPIs, brand types it suits, and how it "
    "chains with other WLDD services. Every list item is one short, concrete line (max ~140 characters). "
    "CONFIDENTIAL: never name any client, brand, show, film, product, creator, celebrity, page or handle; "
    "describe them generically (\"a streaming thriller\", \"a cola brand\", \"a top comedy creator\"). No prices "
    "or budgets, no hashtags. Examples describe what was proposed (brand type, idea, what the service did, "
    "volumes); never claim results, views or growth achieved. pairs_with lists only the 2-4 strongest pairings. "
    "Return one JSON object only."
)

_SHAPE = (
    '{"mechanics": [...], "formats": [...], "typical_volumes": ["e.g. 40 creators x 2 clips"], '
    '"creator_tiers": [...], "handle_categories": [...], "phase_fit": [...], "deliverables": [...], '
    '"kpis": [...], "best_for": [...], "pairs_with": ["<exact other service name>: how they combine"], '
    '"examples": ["brand type + idea + what the service did, with volumes"]}'
)


def _messages(service: Service, catalog: list[Service], excerpts: list[tuple[str, str]], n_pitches: int) -> list:
    others = "\n".join(f"- {s.name}: {s.description[:110].rsplit(' ', 1)[0]}..." for s in catalog if s.name != service.name)
    if excerpts:
        body = "\n\n".join(f"[Pitch {chr(65 + i % 26)}]\n{t}" for i, (_, t) in enumerate(excerpts))
        evidence = (
            f"Excerpts from {n_pitches} past WLDD pitch decks that sold this service (client and creator names "
            f"replaced by [name]). Base the playbook on what these decks actually propose; generalise volumes "
            f"into typical ranges.\n\n{body}"
        )
        n_examples = "3-4 anonymised examples drawn from the excerpts"
    else:
        evidence = (
            "No past WLDD pitch has sold this service yet. Build the playbook from the description above and "
            "standard Indian agency practice; keep volumes indicative."
        )
        n_examples = "2-3 illustrative example applications (hypothetical, generic brand types)"
    human = (
        f"SERVICE: {service.name}\nCatalog description: {service.description}\n"
        f"Other WLDD services (use these exact names in pairs_with):\n{others}\n\n{evidence}\n\n"
        f"Return JSON with 3-6 items per list and {n_examples}:\n{_SHAPE}"
    )
    return [SystemMessage(_SYSTEM), HumanMessage(human)]


_RESULT_VERBS = (r"(?:drov|driv|boost|achiev|generat|result|deliver|led to|lift|increas|spurr|garner|hit|reach|clock|"
                 r"rack|enter|trend)\w*")
# A claimed outcome: ", driving 5M views" / " that boosted SOV by 15%" / " generating 2 M shares".
_RESULT_RE = re.compile(
    rf"(?:,\s*(?:and\s+|which\s+|that\s+)?{_RESULT_VERBS}|\s+(?:that|which|and)\s+{_RESULT_VERBS}|"
    r"\s+(?:driving|boosting|achieving|generating|resulting in|delivering|lifting|increasing|spurring|garnering|"
    r"hitting|reaching|clocking|racking up))\b.*\d.*$",
    re.IGNORECASE,
)
_DANGLING_RE = re.compile(r"\b(to|and|with|for|of|the|a|an|by|via|trend|that|which)$", re.IGNORECASE)


def _tidy(field: str, v: str) -> str:
    v = re.sub(r"^(anonymi[sz]ed )?(example )?application:\s*", "", v, flags=re.IGNORECASE)
    v = re.sub(r"\s*#\w+", "", v)  # model-invented hashtags
    if field == "examples":
        v = _RESULT_RE.sub("", v)  # proposals, not claimed results
        v = re.sub(r"\s*,\s*,", ",", v).strip(" ,;")
        while _DANGLING_RE.search(v):
            v = _DANGLING_RE.sub("", v).strip(" ,;")
        if len(v) < 35:
            return ""  # too little left to be a usable example
    return v.strip(" ,;")


def _clean(draft: _Draft, service: Service, catalog: list[Service], banned: list[str],
           basis: str, n_pitches: int) -> tuple[ServicePlaybook, list[str]]:
    dropped: list[str] = []
    data: dict[str, list[str]] = {}
    for field, vals in draft.model_dump().items():
        keep = []
        for v in vals:
            v = _tidy(field, " ".join(str(v).split())[:220])
            if len(v) < 3:
                continue
            leaks = _leaks(v, banned)
            if leaks:
                dropped.append(f"{field}: {v[:80]} ({', '.join(leaks[:3])})")
                continue
            if field == "pairs_with":
                head = re.split(r"[:—–-]", v, maxsplit=1)[0]
                svc = match_service(head.strip(), catalog)
                if not svc or svc.name == service.name:
                    dropped.append(f"pairs_with: {v[:60]} (not a catalog service)")
                    continue
                rest = v[len(head):].lstrip(" :—–-")
                v = f"{svc.name}: {rest}" if rest else svc.name
            keep.append(v)
        data[field] = keep[: 4 if field == "pairs_with" else 8]
    return ServicePlaybook(service=service.name, basis=basis, pitches_used=n_pitches, **data), dropped


def reclean() -> int:
    """Re-apply the output cleaning to the stored playbooks (no LLM)."""
    catalog = load_services_catalog()
    rows = _read_csv()
    sold = sold_by(catalog, rows)
    banned = _banned(rows, [t for s in catalog for _, t in _select(s, sold[s.name])], catalog)
    path = Path(settings.service_playbooks_path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    by_name = {s.name: s for s in catalog}
    for name, data in raw.get("playbooks", {}).items():
        if name not in by_name:
            continue
        fields = {k: v for k, v in data.items() if k in _Draft.model_fields}
        pb, dropped = _clean(_Draft(**fields), by_name[name], catalog, banned, data.get("basis", "general"),
                             int(data.get("pitches_used", 0)))
        raw["playbooks"][name] = pb.model_dump(exclude={"service"})
        if dropped:
            logger.info("playbooks reclean %s: dropped %s", name, dropped[:4])
    path.write_text(json.dumps(raw, indent=1, ensure_ascii=False), encoding="utf-8")
    return len(raw.get("playbooks", {}))


async def build(only: set[str] | None = None, dry_run: bool = False) -> dict[str, ServicePlaybook]:
    catalog = load_services_catalog()
    rows = _read_csv()
    sold = sold_by(catalog, rows)
    selected = {s.name: _select(s, sold[s.name]) for s in catalog}
    banned = _banned(rows, [t for ex in selected.values() for _, t in ex], catalog)
    logger.info("playbooks: %d banned terms; sold-by %s", len(banned), {k: len(v) for k, v in sold.items()})
    path = Path(settings.service_playbooks_path)
    existing: dict[str, dict] = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8")).get("playbooks", {})
        except ValueError:
            existing = {}
    results: dict[str, ServicePlaybook] = {}
    sem = asyncio.Semaphore(2)

    async def one(i: int, service: Service) -> None:
        excerpts = [(p, _scrub(t, banned)) for p, t in selected[service.name]]
        n_pitches = len({p for p, _ in excerpts})
        msgs = _messages(service, catalog, excerpts, n_pitches)
        if dry_run:
            print(f"--- {service.name}: {n_pitches} pitches, ~{llm_router.estimate_tokens(msgs[1].content)} tokens")
            return
        async with sem:
            t0 = time.monotonic()
            try:
                draft = await llm_router.invoke_json("writer", msgs, _Draft, max_tokens=MAX_TOKENS, temperature=0.4,
                                                     prefer=i)
            except Exception as exc:  # noqa: BLE001 - keep going with the other services
                logger.warning("playbooks: %s failed: %s", service.name, str(exc)[:200])
                return
        pb, dropped = _clean(draft, service, catalog, banned, "past_pitches" if excerpts else "general", n_pitches)
        results[service.name] = pb
        logger.info("playbooks: %s built (%s, %d pitches, %.1fs); dropped %d items: %s", service.name, pb.basis,
                    n_pitches, time.monotonic() - t0, len(dropped), dropped[:4])

    todo = [s for s in catalog if not only or any(o.lower() in s.name.lower() for o in only)]
    await asyncio.gather(*(one(i, s) for i, s in enumerate(todo)))
    if dry_run:
        return {}
    merged = {**existing, **{k: v.model_dump(exclude={"service"}) for k, v in results.items()}}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"version": 1, "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                               "confidential": "derived from past WLDD decks; never commit",
                               "playbooks": merged}, indent=1, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)
    logger.info("playbooks: wrote %d services to %s", len(merged), path)
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--only", help="Comma-separated service names (substring match)")
    parser.add_argument("--dry-run", action="store_true", help="Show selection sizes, no LLM calls")
    parser.add_argument("--reclean", action="store_true", help="Re-apply output cleaning to the stored file, no LLM")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    for noisy in ("httpx", "chromadb", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    if args.reclean:
        print("recleaned", reclean(), "playbooks")
        return
    only = {s.strip() for s in args.only.split(",")} if args.only else None
    with llm_router.track_calls() as records:
        asyncio.run(build(only, args.dry_run))
    tokens: dict[str, list[int]] = {}
    for r in records:
        t = tokens.setdefault(r.model, [0, 0, 0])
        t[0] += 1
        t[1] += r.input_tokens or 0
        t[2] += r.output_tokens or 0
    if tokens:
        print("LLM calls/tokens per model (calls, in, out):", tokens)


if __name__ == "__main__":
    main()
