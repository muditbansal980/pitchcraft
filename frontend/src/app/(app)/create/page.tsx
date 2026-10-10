"use client";

import { useRouter } from "next/navigation";
import { useRef, useState } from "react";

import { LoginRequiredDialog } from "@/components/login-required-dialog";
import { PlanMarkdown } from "@/components/plan-markdown";
import { ResearchSources } from "@/components/research-sources";
import { renderReport, reportFileName, uploadReport } from "@/lib/report-files";
import { saveResearch, updateResearch, type SavedResearch } from "@/lib/researches";
import type { BrandPlan } from "@/lib/types";

type BrandPlanResponse = Partial<BrandPlan> & { error?: string };

type PdfStatus = { state: "idle" | "saving" | "saved" } | { state: "failed"; error: string };

export default function CreatePage() {
  const router = useRouter();
  const [brand, setBrand] = useState("");
  const [loginRequired, setLoginRequired] = useState(false);
  const [response, setResponse] = useState<BrandPlanResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [copied, setCopied] = useState(false);
  const [pdfStatus, setPdfStatus] = useState<PdfStatus>({ state: "idle" });
  // Ignore PDF results from an earlier submission once a new one has started.
  const pdfJob = useRef(0);
  const copiedTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  async function handleSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!brand.trim()) return;

    setLoading(true);
    setResponse(null);
    setCopied(false);
    setPdfStatus({ state: "idle" });
    pdfJob.current += 1;
    try {
      const res = await fetch("/api/brand-plan", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ brand }),
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
        const saved = saveResearch({
          brand: data.brand ?? brand.trim(),
          plan: data.plan,
          research: data.research ?? [],
          references: data.references ?? [],
        });
        void savePdf(saved);
      }
    } catch {
      setResponse({ error: "Could not reach the server. Please try again." });
    } finally {
      setLoading(false);
    }
  }

  /** Render the report as a PDF and store it in Cloudinary, linked to the saved research. */
  async function savePdf(saved: SavedResearch) {
    const job = pdfJob.current;
    setPdfStatus({ state: "saving" });
    try {
      const pdf = await renderReport(saved);
      const { url, publicId } = await uploadReport(pdf, reportFileName(saved));
      updateResearch(saved.id, { pdfUrl: url, pdfPublicId: publicId });
      if (job === pdfJob.current) setPdfStatus({ state: "saved" });
    } catch (err) {
      const error = err instanceof Error ? err.message : "Saving the PDF failed.";
      if (job === pdfJob.current) setPdfStatus({ state: "failed", error });
    }
  }

  async function handleCopy() {
    if (!response?.plan) return;
    try {
      await navigator.clipboard.writeText(response.plan);
      setCopied(true);
      if (copiedTimer.current) clearTimeout(copiedTimer.current);
      copiedTimer.current = setTimeout(() => setCopied(false), 2000);
    } catch {
      // Clipboard can be unavailable (e.g. insecure context); fail silently.
    }
  }

  const references = response?.references ?? [];

  return (
    <div className="mx-auto flex w-full max-w-3xl flex-col gap-8 px-4 py-10 md:px-8 md:py-14">
      <LoginRequiredDialog open={loginRequired} onClose={() => setLoginRequired(false)} />

      <header className="flex flex-col gap-2">
        <h1 className="text-4xl font-bold tracking-tight uppercase md:text-5xl">New research</h1>
        <p className="font-mono text-sm text-muted">
          Name a brand. We research it and draft a pitch plan from WLDD&apos;s services.
        </p>
      </header>

      <form onSubmit={handleSubmit} className="flex flex-col gap-3 sm:flex-row">
        <label htmlFor="brand" className="sr-only">
          Brand name
        </label>
        <input
          id="brand"
          type="text"
          value={brand}
          onChange={(e) => setBrand(e.target.value)}
          placeholder="e.g. Coca-Cola"
          className="brutal-input h-14 w-full text-lg sm:flex-1"
        />
        <button type="submit" disabled={loading || !brand.trim()} className="brutal-btn h-14 px-8 text-lg">
          {loading ? "Working..." : "OK"}
        </button>
      </form>

      <section aria-live="polite" aria-busy={loading} className="brutal-box min-h-40 p-5 md:p-6">
        <div className="mb-4 flex items-center justify-between gap-3 border-b-3 border-foreground pb-3">
          <h2 className="font-mono text-sm font-bold uppercase">
            {response?.plan ? `Plan for ${response.brand ?? brand}` : "Response"}
          </h2>
          {response?.plan && !loading && (
            <button type="button" onClick={handleCopy} className="brutal-btn h-9 bg-accent-2 px-3 text-xs">
              {copied ? "Copied" : "Copy plan"}
            </button>
          )}
        </div>

        {loading ? (
          <div className="flex flex-col gap-1">
            <p className="font-bold">Researching the brand and drafting the plan…</p>
            <p className="font-mono text-sm text-muted">This usually takes 10–30 seconds.</p>
          </div>
        ) : response?.error ? (
          <p className="border-3 border-foreground bg-danger px-3 py-2 font-bold text-black">
            {response.error}
          </p>
        ) : response?.plan ? (
          <div className="flex flex-col gap-6">
            <PlanMarkdown>{response.plan}</PlanMarkdown>

            {references.length > 0 && (
              <p className="font-mono text-xs text-muted">
                Inspired by past work:{" "}
                {references.map((ref) => `${ref.pitch_id} ${ref.client} – ${ref.campaign}`).join(" · ")}
              </p>
            )}

            <ResearchSources research={response.research ?? []} />
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
