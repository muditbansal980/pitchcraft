"use client";

import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";

import { DeckActions } from "@/components/deck-actions";
import { LoginRequiredDialog } from "@/components/login-required-dialog";
import { PlanMarkdown } from "@/components/plan-markdown";
import { ResearchSources } from "@/components/research-sources";
import { SlideStrip } from "@/components/slide-strip";
import { canvaReturnMessage, clearCanvaReturn, useCanvaReturn } from "@/lib/canva";
import { deleteUploadedReport, renderReport, reportFileName, uploadReport } from "@/lib/report-files";
import { saveResearch, updateResearch, useResearches, type SavedResearch } from "@/lib/researches";
import type { BrandPlan, PlanScale } from "@/lib/types";

type BrandPlanResponse = Partial<BrandPlan> & { error?: string };

type PdfStatus = { state: "idle" | "saving" | "saved" } | { state: "failed"; error: string };

const MAX_BRIEF_CHARS = 2000;

const SCALE_OPTIONS: { value: PlanScale; label: string }[] = [
  { value: "auto", label: "Auto (fits the brief)" },
  { value: "focused", label: "Focused · 6–10 slides" },
  { value: "standard", label: "Standard · 15–22 slides" },
  { value: "flagship", label: "Flagship · 30–45 slides" },
];

// Rough timings of the generation pipeline, so the wait shows what's happening.
const STAGES: { from: number; label: string }[] = [
  { from: 0, label: "Researching the brand" },
  { from: 12, label: "Reading the internet's mood" },
  { from: 28, label: "Building the strategy" },
  { from: 55, label: "Writing slides" },
  { from: 90, label: "Finding memes & refs" },
  { from: 130, label: "Designing the deck" },
];

function formatElapsed(seconds: number) {
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
}

export default function CreatePage() {
  const router = useRouter();
  const [brand, setBrand] = useState("");
  const [brief, setBrief] = useState("");
  const [scale, setScale] = useState<PlanScale>("auto");
  const [loginRequired, setLoginRequired] = useState(false);
  const [response, setResponse] = useState<BrandPlanResponse | null>(null);
  // The saved copy of the latest plan (gives the deck buttons a research id).
  const [saved, setSaved] = useState<SavedResearch | null>(null);
  const [loading, setLoading] = useState(false);
  const [elapsed, setElapsed] = useState(0);
  const [copied, setCopied] = useState(false);
  const [noticeDismissed, setNoticeDismissed] = useState(false);
  const [pdfStatus, setPdfStatus] = useState<PdfStatus>({ state: "idle" });
  // Ignore PDF results from an earlier submission once a new one has started.
  const pdfJob = useRef(0);
  const copiedTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Back from Canva's consent screen: show the research the user was on.
  const canvaReturn = useCanvaReturn();
  const researches = useResearches();
  const resumed =
    !response && canvaReturn.resumeId
      ? (researches?.find((item) => item.id === canvaReturn.resumeId) ?? null)
      : null;
  const notice = noticeDismissed ? null : canvaReturnMessage(canvaReturn);

  const plan: BrandPlanResponse | null = response ?? resumed;
  const current: SavedResearch | null = response ? saved : resumed;

  useEffect(() => {
    if (!loading) return;
    const started = Date.now();
    const timer = setInterval(() => setElapsed(Math.floor((Date.now() - started) / 1000)), 1000);
    return () => clearInterval(timer);
  }, [loading]);

  async function handleSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!brand.trim()) return;

    if (canvaReturn.result) clearCanvaReturn();
    setLoading(true);
    setElapsed(0);
    setResponse(null);
    setSaved(null);
    setCopied(false);
    setPdfStatus({ state: "idle" });
    pdfJob.current += 1;
    try {
      const res = await fetch("/api/brand-plan", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ brand, brief: brief.trim() || undefined, scale }),
      });
      const data: BrandPlanResponse | null = await res.json().catch(() => null);
      if (res.status === 401) {
        // Logged out or session expired: prompt to log in instead of showing an error.
        setLoginRequired(true);
        router.refresh();
      } else if (!res.ok || !data?.plan) {
        setResponse({ error: data?.error ?? "Plan generation failed. Please try again." });
      } else {
        setResponse(data);
        const item = saveResearch({
          brand: data.brand ?? brand.trim(),
          plan: data.plan,
          research: data.research ?? [],
          references: data.references ?? [],
          deck: data.deck ?? undefined,
          scale: data.scale,
          brief: data.brief ?? (brief.trim() || null),
        });
        setSaved(item);
        void savePdf(item);
      }
    } catch {
      setResponse({ error: "Could not reach the server. Please try again." });
    } finally {
      setLoading(false);
    }
  }

  /** Render the report as a PDF and store it in Cloudinary, linked to the saved research. */
  async function savePdf(item: SavedResearch) {
    const job = pdfJob.current;
    setPdfStatus({ state: "saving" });
    try {
      const pdf = await renderReport(item);
      const { url, publicId } = await uploadReport(pdf, reportFileName(item));
      // Deleted from Your researches while uploading: don't leave the file behind.
      if (!updateResearch(item.id, { pdfUrl: url, pdfPublicId: publicId })) deleteUploadedReport(publicId);
      if (job === pdfJob.current) setPdfStatus({ state: "saved" });
    } catch (err) {
      const error = err instanceof Error ? err.message : "Saving the PDF failed.";
      if (job === pdfJob.current) setPdfStatus({ state: "failed", error });
    }
  }

  async function handleCopy() {
    if (!plan?.plan) return;
    try {
      await navigator.clipboard.writeText(plan.plan);
      setCopied(true);
      if (copiedTimer.current) clearTimeout(copiedTimer.current);
      copiedTimer.current = setTimeout(() => setCopied(false), 2000);
    } catch {
      // Clipboard can be unavailable (e.g. insecure context); fail silently.
    }
  }

  const references = plan?.references ?? [];
  const deck = plan?.deck;
  const stageIndex = STAGES.reduce((found, stage, i) => (elapsed >= stage.from ? i : found), 0);

  return (
    <div className="mx-auto flex w-full max-w-3xl flex-col gap-8 px-4 py-10 md:px-8 md:py-14">
      <LoginRequiredDialog open={loginRequired} onClose={() => setLoginRequired(false)} />

      <header className="flex flex-col gap-2">
        <h1 className="text-4xl font-bold tracking-tight uppercase md:text-5xl">New research</h1>
        <p className="font-mono text-sm text-muted">
          Name a brand. We research it and build a designed pitch deck from WLDD&apos;s services.
        </p>
      </header>

      {notice && (
        <div
          role="status"
          className={`flex items-start justify-between gap-3 border-3 border-foreground px-3 py-2 font-bold text-black ${
            canvaReturn.result === "connected" ? "bg-accent-2" : "bg-danger"
          }`}
        >
          <p>{notice}</p>
          <button
            type="button"
            onClick={() => setNoticeDismissed(true)}
            aria-label="Dismiss"
            className="font-mono text-sm underline decoration-2 underline-offset-2"
          >
            ✕
          </button>
        </div>
      )}

      <form onSubmit={handleSubmit} className="flex flex-col gap-4">
        <div className="flex flex-col gap-3 sm:flex-row">
          <label htmlFor="brand" className="sr-only">
            Brand name
          </label>
          <input
            id="brand"
            type="text"
            value={brand}
            onChange={(e) => setBrand(e.target.value)}
            placeholder="e.g. Coca-Cola"
            maxLength={200}
            className="brutal-input h-14 w-full text-lg sm:flex-1"
          />
          <button type="submit" disabled={loading || !brand.trim()} className="brutal-btn h-14 px-8 text-lg">
            {loading ? "Working..." : "OK"}
          </button>
        </div>

        <div className="flex flex-col gap-1.5">
          <label htmlFor="brief" className="font-mono text-xs font-bold uppercase">
            Brief <span className="font-normal text-muted normal-case">(optional)</span>
          </label>
          <textarea
            id="brief"
            value={brief}
            onChange={(e) => setBrief(e.target.value)}
            maxLength={MAX_BRIEF_CHARS}
            rows={3}
            placeholder="e.g. Launch the new zero-sugar variant with Gen Z in metros; 6-week always-on Instagram push around IPL."
            className="brutal-input min-h-24 w-full resize-y py-3 text-sm"
          />
          <p className="self-end font-mono text-[11px] text-muted">
            {brief.length}/{MAX_BRIEF_CHARS}
          </p>
        </div>

        <div className="flex flex-col gap-1.5 sm:flex-row sm:items-center sm:gap-3">
          <label htmlFor="scale" className="font-mono text-xs font-bold uppercase">
            Deck size
          </label>
          <select
            id="scale"
            value={scale}
            onChange={(e) => setScale(e.target.value as PlanScale)}
            className="brutal-input h-11 w-full text-sm sm:w-auto"
          >
            {SCALE_OPTIONS.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </div>
      </form>

      <section aria-live="polite" aria-busy={loading} className="brutal-box min-h-40 min-w-0 p-5 md:p-6">
        <div className="mb-4 flex items-center justify-between gap-3 border-b-3 border-foreground pb-3">
          <h2 className="min-w-0 truncate font-mono text-sm font-bold uppercase">
            {plan?.plan ? `Plan for ${plan.brand ?? brand}` : "Response"}
          </h2>
          {plan?.plan && !loading && (
            <button type="button" onClick={handleCopy} className="brutal-btn h-9 shrink-0 bg-accent-2 px-3 text-xs">
              {copied ? "Copied" : "Copy plan"}
            </button>
          )}
        </div>

        {loading ? (
          <div className="flex flex-col gap-3">
            <div className="flex items-baseline justify-between gap-3">
              <p className="font-bold">{STAGES[stageIndex].label}…</p>
              <span className="font-mono text-xs text-muted tabular-nums">{formatElapsed(elapsed)}</span>
            </div>
            <ol className="flex flex-col gap-1 font-mono text-xs">
              {STAGES.map((stage, i) => (
                <li key={stage.label} className={i === stageIndex ? "font-bold" : i < stageIndex ? "" : "text-muted"}>
                  <span aria-hidden="true" className="inline-block w-5">
                    {i < stageIndex ? "✓" : i === stageIndex ? "▶" : "·"}
                  </span>
                  {stage.label}
                </li>
              ))}
            </ol>
            <p className="font-mono text-xs text-muted">
              Focused decks take under a minute; flagship decks take a couple of minutes.
            </p>
          </div>
        ) : response?.error ? (
          <p className="border-3 border-foreground bg-danger px-3 py-2 font-bold text-black">{response.error}</p>
        ) : plan?.plan ? (
          <div className="flex min-w-0 flex-col gap-6">
            {deck && deck.slides.length > 0 && (
              <div className="flex min-w-0 flex-col gap-3">
                <SlideStrip deck={deck} />
                <DeckActions
                  deck={deck}
                  brand={plan.brand ?? brand}
                  createdAt={current?.createdAt}
                  researchId={current?.id}
                  returnPath="/create"
                />
              </div>
            )}

            <PlanMarkdown>{plan.plan}</PlanMarkdown>

            {references.length > 0 && (
              <p className="font-mono text-xs text-muted">
                Inspired by past work:{" "}
                {references.map((ref) => `${ref.pitch_id} ${ref.client} – ${ref.campaign}`).join(" · ")}
              </p>
            )}

            <ResearchSources research={plan.research ?? []} />
            <p className="font-mono text-xs text-muted">
              Saved to Your researches.{" "}
              {pdfStatus.state === "saving"
                ? "Creating the PDF report…"
                : pdfStatus.state === "saved"
                  ? "PDF report ready to download there."
                  : pdfStatus.state === "failed"
                    ? `PDF upload failed (${pdfStatus.error}) – you can still download it from Your researches.`
                    : null}
            </p>
          </div>
        ) : (
          <p className="text-muted">Enter a brand name and press OK to generate a plan.</p>
        )}
      </section>
    </div>
  );
}
