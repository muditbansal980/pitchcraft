"use client";

import { useRouter } from "next/navigation";
import { useEffect, useEffectEvent, useRef, useState } from "react";

import { DeckActions } from "@/components/deck-actions";
import { DossierPanel } from "@/components/dossier-panel";
import { LoginRequiredDialog } from "@/components/login-required-dialog";
import { PlanMarkdown } from "@/components/plan-markdown";
import { ResearchSources } from "@/components/research-sources";
import { SlideStrip } from "@/components/slide-strip";
import { canvaReturnMessage, clearCanvaReturn, useCanvaReturn } from "@/lib/canva";
import { MAX_BRIEF_CHARS } from "@/lib/plan-request";
import {
  clearPendingJob,
  fetchPlanJob,
  JOB_STAGES,
  JobRequestError,
  POLL_INTERVAL_MS,
  stageIndex,
  startPlanJob,
  usePendingJob,
  type PendingJob,
} from "@/lib/plan-jobs";
import { deleteUploadedReport, renderReport, reportFileName, uploadReport } from "@/lib/report-files";
import { saveResearch, updateResearch, useResearches, type SavedResearch } from "@/lib/researches";
import type { BrandPlan, PlanJob, PlanScale } from "@/lib/types";

type BrandPlanResponse = Partial<BrandPlan> & { error?: string };

type PdfStatus = { state: "idle" | "saving" | "saved" } | { state: "failed"; error: string };

const SCALE_OPTIONS: { value: PlanScale; label: string }[] = [
  { value: "auto", label: "Auto (fits the brief)" },
  { value: "focused", label: "Focused · 6–10 slides" },
  { value: "standard", label: "Standard · 15–22 slides" },
  { value: "flagship", label: "Flagship · 30–45 slides" },
];

// Polls that may fail in a row (server busy or restarting) before giving up.
const MAX_POLL_FAILURES = 6;

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
  // POST /api/brand-plan/jobs in flight; afterwards the pending job drives the UI.
  const [starting, setStarting] = useState(false);
  const [jobState, setJobState] = useState<{ id: string; stage: string; progress: number } | null>(null);
  const [now, setNow] = useState(0);
  const [copied, setCopied] = useState(false);
  const [noticeDismissed, setNoticeDismissed] = useState(false);
  const [pdfStatus, setPdfStatus] = useState<PdfStatus>({ state: "idle" });
  // Ignore PDF results from an earlier submission once a new one has started.
  const pdfJob = useRef(0);
  const copiedTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  // The job this tab is waiting for; survives a reload (sessionStorage).
  const pending = usePendingJob();
  const loading = starting || pending !== null;

  // Back from Canva's consent screen: show the research the user was on.
  const canvaReturn = useCanvaReturn();
  const researches = useResearches();
  const resumed =
    !response && !loading && canvaReturn.resumeId
      ? (researches?.find((item) => item.id === canvaReturn.resumeId) ?? null)
      : null;
  const notice = noticeDismissed ? null : canvaReturnMessage(canvaReturn);

  const plan: BrandPlanResponse | null = response ?? resumed;
  const current: SavedResearch | null = response ? saved : resumed;

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

  // A one-second clock for the elapsed time while a job runs.
  useEffect(() => {
    if (!pending) return;
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [pending]);

  const onJobDone = useEffectEvent((job: PendingJob, data: PlanJob) => {
    clearPendingJob(job.id);
    const result = data.result;
    if (!result?.plan) {
      setResponse({ error: "Plan generation returned no plan. Please try again." });
      return;
    }
    setResponse(result);
    const item = saveResearch({
      brand: result.brand ?? job.brand,
      plan: result.plan,
      research: result.research ?? [],
      references: result.references ?? [],
      deck: result.deck ?? undefined,
      scale: result.scale,
      brief: result.brief ?? (job.brief || null),
      dossier: result.dossier ?? undefined,
    });
    setSaved(item);
    void savePdf(item);
  });

  const onJobFailed = useEffectEvent((job: PendingJob, error: string, status?: number) => {
    clearPendingJob(job.id);
    if (status === 401) {
      // Logged out or session expired: prompt to log in instead of showing an error.
      setLoginRequired(true);
      router.refresh();
      return;
    }
    setResponse({ error });
  });

  // Poll the pending job every few seconds until it finishes (also after a reload).
  useEffect(() => {
    if (!pending) return;
    const job = pending;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let failures = 0;

    async function tick() {
      try {
        const data = await fetchPlanJob(job.id);
        if (cancelled) return;
        failures = 0;
        if (data.status === "done") return onJobDone(job, data);
        if (data.status === "error") {
          return onJobFailed(job, data.error ?? "Plan generation failed. Please try again.", data.status_code ?? 502);
        }
        setJobState({ id: job.id, stage: data.stage, progress: data.progress });
      } catch (err) {
        if (cancelled) return;
        const status = err instanceof JobRequestError ? err.status : 0;
        if (status === 401) return onJobFailed(job, "Please log in to continue.", 401);
        if (status === 404) {
          return onJobFailed(job, "This plan is no longer available (the server may have restarted). Please generate it again.");
        }
        failures += 1;
        if (failures >= MAX_POLL_FAILURES) {
          return onJobFailed(job, "Lost contact with the server while the plan was generating. Please try again.");
        }
      }
      timer = setTimeout(tick, POLL_INTERVAL_MS);
    }

    void tick();
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [pending]);

  async function handleSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!brand.trim() || loading) return;

    if (canvaReturn.result) clearCanvaReturn();
    setStarting(true);
    setJobState(null);
    setNow(0);
    setResponse(null);
    setSaved(null);
    setCopied(false);
    setPdfStatus({ state: "idle" });
    pdfJob.current += 1;
    try {
      await startPlanJob({ brand: brand.trim(), brief: brief.trim(), scale });
    } catch (err) {
      if (err instanceof JobRequestError && err.status === 401) {
        setLoginRequired(true);
        router.refresh();
      } else {
        setResponse({ error: err instanceof Error ? err.message : "Starting the plan failed. Please try again." });
      }
    } finally {
      setStarting(false);
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
  const live = pending && jobState?.id === pending.id ? jobState : null;
  const currentStage = live ? stageIndex(live.stage) : -1;
  const progressPercent = Math.round((live?.progress ?? 0) * 100);
  const elapsed = pending && now ? Math.max(0, Math.floor((now - pending.startedAt) / 1000)) : 0;
  const stageLabel =
    currentStage >= 0 ? JOB_STAGES[currentStage].label : pending ? "Queued, starting shortly" : "Starting";

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
            {pending
              ? `Generating plan for ${pending.brand}`
              : plan?.plan
                ? `Plan for ${plan.brand ?? brand}`
                : "Response"}
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
              <p className="font-bold">{stageLabel}…</p>
              <span className="font-mono text-xs text-muted tabular-nums">
                {progressPercent}% · {formatElapsed(elapsed)}
              </span>
            </div>
            <div
              role="progressbar"
              aria-label="Plan progress"
              aria-valuemin={0}
              aria-valuemax={100}
              aria-valuenow={progressPercent}
              aria-valuetext={stageLabel}
              className="h-4 w-full border-3 border-foreground bg-background"
            >
              <div
                className="h-full bg-accent transition-[width] duration-700 ease-out"
                style={{ width: `${Math.max(progressPercent, 2)}%` }}
              />
            </div>
            <ol className="flex flex-col gap-1 font-mono text-xs">
              {JOB_STAGES.map((item, i) => (
                <li
                  key={item.stage}
                  className={i === currentStage ? "font-bold" : i < currentStage ? "" : "text-muted"}
                >
                  <span aria-hidden="true" className="inline-block w-5">
                    {i < currentStage ? "✓" : i === currentStage ? "▶" : "·"}
                  </span>
                  {item.label}
                </li>
              ))}
            </ol>
            <p className="font-mono text-xs text-muted">
              Focused decks take about a minute; flagship decks take 3–5 minutes. It runs on the server, so
              reloading this tab is fine.
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

            {plan.dossier && <DossierPanel dossier={plan.dossier} />}
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
