# PitchCraft — Brand Plan Pipeline: Work Plan

Goal: user enters a brand → backend researches it (Tavily) → retrieves WLDD services + relevant past pitches (Chroma) → Groq LLM writes a **plan addressed to the brand** ("What we can offer": which WLDD services fit and what we'd actually do) → frontend shows it.

Started: 2026-10-10. Coordinator: main Claude session (integration + final end-to-end test).

## Flow

```
frontend page ──POST /api/brand-plan {brand}──▶ Next route ──▶ FastAPI POST /api/brand-plan
                                                                  │
                         ┌────────────────────────────────────────┼─────────────────────────┐
                         ▼                                        ▼                         ▼
              Tavily research (existing)            Chroma: wldd_services (all)   Chroma: pitches (top-k similar)
              app/services/brand_research.py        get_services_store()          get_pitches_store()
                         └──────────────────────────▶ prompt ──▶ Groq (openai/gpt-oss-120b) ──▶ markdown plan
```

## Already done (before agents)

- [x] Tavily research service + `POST /api/brand-info` (`app/services/brand_research.py`, `app/schemas/brand.py`)
- [x] Services ingestion pipeline — committed by user in `5410d73` (`app/ingestion/services_pipeline.py`, PyMuPDF + `SemanticChunker`). 12 chunks / 11 services already in collection `wldd_services`.
- [x] Shared config in `app/core/config.py`: `chroma_collection="pitches"`, `chroma_services_collection`, `archive_dir`, `groq_api_key`, `groq_model="openai/gpt-oss-120b"`, `groq_reasoning_effort="low"`
- [x] `get_pitches_store()` added to `app/vectorstore` (collection `settings.chroma_collection`)
- [x] Installed: `langchain-groq`, `langchain-text-splitters`, `pypdf` (PyMuPDF already present)
- [x] Next.js `/api/brand-info` route forwards to backend; `BACKEND_URL` in `frontend/.env.local`

## Shared contracts (do not change without updating this file)

### C1 — Past-pitches archive (collection `pitches`, via `get_pitches_store()`)

Two document kinds:

| kind | one per | page_content |
|---|---|---|
| `brief` | CSV row | `Past pitch {pitch_id}: {campaign} ({client})\nServices: {services}\nBrief: {brief}` |
| `deck` | deck chunk (≤ 900 chars) | `Past pitch {pitch_id}: {campaign} ({client})\n{chunk text}` |

Metadata on every doc: `source` (CSV or deck file name), `kind`, `pitch_id`, `campaign`, `client`, `services` (string, `"; "`-joined), `chunk_index` (int, 0 for brief).
IDs deterministic (sha1 of source/pitch_id/kind/chunk_index); re-running replaces previous docs.
Chunks ≤ 900 chars because all-MiniLM-L6-v2 truncates input at 256 tokens.

### C2 — Services collection (`wldd_services`, existing, read-only for agents)

Metadata: `service`, `service_number`, `chunk_index`, `page`, `source`. Reassemble a full service by sorting chunks on (`service_number`, `chunk_index`). `MISCL` is a budget line, not a service to recommend.

### C3 — Backend API

`POST /api/brand-plan` body `{"brand": "Zomato"}` →
```json
{
  "brand": "Zomato",
  "plan": "# Zomato × WLDD ...markdown...",
  "research": [ResearchSection, ...],          // same shape as /api/brand-info sections: {title, summary, sources:[{title,url,snippet}]}
  "references": [{"pitch_id": "PC07", "campaign": "...", "client": "..."}]   // past pitches used as inspiration (internal)
}
```
Errors: FastAPI `{"detail": str}` — 422 empty brand, 503 missing `TAVILY_API_KEY`/`GROQ_API_KEY` or empty archive, 429 rate limit, 502 upstream failure.

## Agent assignments

File ownership is exclusive — an agent edits only files it owns.

### Agent 1 — Archive ingestion
Owns: `backend/app/ingestion/pitches_pipeline.py` (+ any new helper in `app/ingestion/` it creates). Must not modify existing ingestion files.
- [x] Load `past_pitches.csv` + `decks/*.pdf` from `settings.archive_dir`
- [x] Clean deck text, chunk (≤ 900 chars), build docs per C1
- [x] Store in `pitches` collection, idempotent re-run
- [x] CLI: `uv run python -m app.ingestion.pitches_pipeline`
- [x] Run it for real; verify counts + sample similarity queries

Record:
- Built `backend/app/ingestion/pitches_pipeline.py` (single file; existing ingestion files untouched). Run: `uv run python -m app.ingestion.pitches_pipeline [archive_dir]` (~15 s).
- Cleaning (per slide): NFKC (fixes "ﬂ" ligatures), strip zero-width/soft-hyphen chars, emoji/UI glyphs and letter-spaced display text ("S A M P L E"); drop noise lines (slide numbers, "SLIDE - 2", "see,", "x", symbol-only) and repeated lines; join wrapped lines into paragraphs; ALL-CAPS headings, bullets and "1." items start new paragraphs; heading + body merged as "HEADING: body"; hyphen-broken words rejoined. Slides joined with blank lines, empty slides skipped.
- Chunking: `RecursiveCharacterTextSplitter`, chunk_size = 900 − len(prefix) so full page_content ≤ 900 (max seen 898), overlap 120, separators slide → paragraph → sentence → clause → word. Chunks < 80 chars or < 10 words dropped.
- Docs exactly per C1. Idempotency: deletes every doc with `kind in [brief, deck]` before adding (collection holds only this archive, so pitches removed from the CSV disappear too). Missing deck → warning, brief still stored.
- Counts: **180 docs = 15 brief + 165 deck**. Per pitch: PC01 14, PC02 16, PC03 10, PC04 19, PC05 1, PC06 7, PC07 6, PC08 7, PC09 25, PC10 9, PC11 11, PC12 19, PC13 12, PC14 1, PC15 8. Ran twice → still 180; `wldd_services` still 12.
- Sample queries (top-8, cosine distance): "protein supplement brand awareness with fitness influencers" → all PC02 (0.38–0.50); "festival sale e-commerce video" → PC01 brief first (0.51), then PC01/PC11/PC03/PC07; "OTT streaming show launch memes" → mixed PC08, PC10, PC13, PC04, PC12 (0.50–0.58), briefs-only → PC13, PC11, PC10; "soft drink sports watch party" → all PC07; "beauty hair care … celebrity ambassador" → all PC03; "music streaming anniversary UGC" → all PC04.
- Notes for retrieval (Agent 2): (1) one pitch's deck chunks often fill the whole top-k → fetch k≈20–30 and dedupe/group by `pitch_id` (best score per pitch). (2) Don't search briefs only: some briefs are short or truncated with "…" (PC02's doesn't even say "protein"; protein query ranks PC07 brief above it), deck chunks carry the signal. (3) Noisy/thin pitches: PC14 (Fizzy Goblet art deck: 1 deck chunk, just slide titles), PC05 (Thums Up: 1 deck chunk); PC09 (25) and PC04/PC12 (19) are the largest. Deck text is slide-ish (labels like "Concept Note: Category:" separated from content), and contains creator names — fine as inspiration, not for verbatim quoting.

### Agent 2 — Retrieval + Groq plan generation (backend)
Owns: `backend/app/services/retrieval.py`, `backend/app/services/brand_plan.py`, `backend/app/schemas/plan.py`, `backend/app/api/routes/plan.py`, `backend/app/main.py` (router include only).
- [x] Retrieval: all services (C2) + top similar past pitches (C1) from research
- [x] Prompt that produces a brand-facing plan with a "What we can offer" section
- [x] Groq `ChatGroq` call within free-tier budget (8K tokens/min incl. completion)
- [x] `POST /api/brand-plan` per C3 with error mapping
- [x] Tests with fake Tavily + fake LLM; real retrieval check

Record:
<!-- agent 2 writes its log here -->
- Files: `app/schemas/plan.py` (`BrandPlanRequest`, `PastPitchRef`, `BrandPlanResponse`), `app/services/retrieval.py`, `app/services/brand_plan.py`, `app/api/routes/plan.py`, `app/main.py` (+`plan.router`).
- Flow (`generate_brand_plan`): load services catalog first (empty → 503, before spending Tavily) → `research_brand()` (reused) → pitch queries → similar pitches → prompt → `ChatGroq.ainvoke` → strip `<think>`/code fences → response. Chroma/embedding calls run in `asyncio.to_thread`.
- Services: all chunks of `wldd_services` reassembled by (service_number, chunk_index), MISCL excluded → 10 services, full catalog given to the LLM.
- Pitches: up to 2 queries (`"{brand}: {overview summary}"`, `"{brand}: {marketing summary}"`, ≤700 chars each, under MiniLM's 256-token limit); similarity search k=25 over briefs + deck chunks together; drop cosine distance > 0.85; group by pitch_id (best distance); top 3; brief fetched by `get(pitch_id, kind=brief)` if not hit; best 1 deck excerpt per pitch in the prompt. Empty collection → plan without pitches (`references: []`).
- Prompt: system prompt makes the model write AS WLDD TO the brand: `# Brand × WLDD: <name>`, Where {brand} is today, The opportunity, The big idea, **What we can offer** (3–6 catalog services, exact names, "Why it fits" + "What we'd do" with formats/platforms/creator tiers/volumes/brand-specific ideas), How it rolls out (phases, weeks), KPIs, Next steps. Rules: facts only from research, no pricing, never mention past pitches/clients/creators named in them or quote decks (pitches ≠ case studies), ~900–1,300 words.
- Token budget: caps per block (summary 1,000 chars, 2 sources × 240-char snippet per section, brief 380, excerpt 320). Realistic Zomato prompt ≈ 2.3K tokens (o200k), long-research fake ≈ 3.0K, worst case (max-length everything) ≈ 3.7K o200k / 3.55K by chars/4. `max_tokens=3500` → worst case ≈ 7.2K < 8K TPM. `reasoning_effort` from settings (low), `max_retries=1`, `timeout=120s`. `finish_reason == "length"` → warning log, partial plan returned; empty content → 502.
- Errors: 422 blank brand (string detail; no `min_length` so detail is always a str), 503 no TAVILY/GROQ key or empty services, 429 Groq `RateLimitError`/Tavily `UsageLimitExceededError`, 502 Groq auth/other status (incl. 413 request-too-large)/connection, Tavily key, empty plan, anything else.
- Tests (TestClient, fake `research_brand` + fake LLM, no Tavily/Groq spend, run with `-W error::DeprecationWarning`): 25/25 pass — success shape, fence stripping, empty pitches, truncated/empty output, every error path, no Tavily call on config errors, ChatGroq params, prompt-size budget. Real retrieval on live `pitches` (180 docs) gives sensible matches (protein → PC02 0.39, Netflix → PC11 0.19, meme-led food brand → PC10/PC04/PC11).
- Open: needs real `GROQ_API_KEY` to verify output quality, actual token usage (logged per request) and that Groq accepts `reasoning_effort` for gpt-oss-120b. Two plans within one minute will likely hit the 8K TPM limit → 429. Frontend timeout should allow ~60–120s (Tavily advanced + Groq).

### Agent 3 — Frontend
Owns: `frontend/src/app/api/brand-plan/route.ts`, `frontend/src/app/page.tsx`, `frontend/src/app/globals.css`, `frontend/package.json` (adding deps).
- [x] Next route `/api/brand-plan` → backend per C3 (timeout, `detail`→`error` mapping)
- [x] Page generates and renders the markdown plan + research sources
- [x] `tsc`, `eslint`, `next build` pass

Record:
<!-- agent 3 writes its log here -->
- Files: new `frontend/src/lib/backend.ts` (`forwardBrandRequest()` — shared brand validation → 400, forward, timeout → 504, unreachable → 502, FastAPI `detail` → `{error}` with original status); new `frontend/src/app/api/brand-plan/route.ts` (120 s timeout); `frontend/src/app/api/brand-info/route.ts` now calls the helper (90 s, same messages — behavior unchanged); `frontend/src/app/page.tsx` rewritten to call `/api/brand-plan`. `globals.css` untouched.
- Packages: `react-markdown@10.1.0`, `remark-gfm@4.0.1` (peer `react >=18`). No typography plugin — markdown styled via a `components` map with Tailwind classes + `dark:` variants.
- UI: same layout/input/OK button. Loading: "Researching the brand and drafting the plan…" + "usually takes 10–30 seconds", button shows "Generating...", `aria-busy` on the aria-live region. Result: header "Plan for {brand}" + "Copy plan" button (copies raw markdown, "Copied" for 2 s); plan rendered as markdown (headings shifted 2 levels so `#` → `<h3>` under page h1/h2; lists, bold, links in new tab, blockquote, code, GFM tables in horizontal scroll). Below: subtle `text-xs` "Inspired by past work: PC07 Coca-Cola – campaign · …", then a collapsed `<details>` "Research sources" listing source links per research section (new tab). Errors show `{error}`; non-OK responses without JSON fall back to a generic message.
- Verification: `npx tsc --noEmit` ✓, `npx eslint src` ✓, `npx next build` ✓ (`/api/brand-plan` dynamic). Runtime via `next start -p 3457` + mock C3 backend on :8767: success → 200 full payload; blank brand → 400; backend `{detail:str}` 503 → `{error}` 503; list `detail` 422 → generic `{error}` 422; backend down → 502; `/api/brand-info` still forwards. Servers stopped.

## Integration (coordinator)
- [x] Review agents' work: file ownership respected, contracts C1–C3 matched
- [x] Relayed Agent 1's retrieval findings to Agent 2 (group by pitch_id, k≈25, search all docs, no creator names)
- [x] Integration check: backend loads with `/api/brand-info`, `/api/brand-plan`, `/health`; services catalog = 10 (MISCL excluded); `/api/brand-plan` returns 503 "GROQ_API_KEY is not configured" without a key; frontend `tsc` + `eslint` pass
- [ ] End-to-end test with real Groq: blocked until `GROQ_API_KEY` is set in `backend/.env`
- [ ] Look at a real plan in the browser (markdown styling has only been checked via build/SSR)

Open items:
- `frontend/src/app/layout.tsx` description still says "Look up brand info for pitches" (user-owned file, not changed)
- npm reported audit warnings when installing react-markdown (not investigated)
- Free tier: two plans within a minute may hit Groq's 8K tokens/min limit → 429 with a "try again in a minute" message
