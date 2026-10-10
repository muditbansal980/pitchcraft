"use client";

import type { ReportInput } from "@/components/report-pdf";
import type { Deck } from "@/lib/types";

export type UploadedReport = { url: string; publicId: string };

function fileStem(report: { brand: string; createdAt?: string }, kind: string): string {
  const slug = report.brand.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "") || "brand";
  const date = (report.createdAt ?? new Date().toISOString()).slice(0, 10);
  return `${slug}-${kind}-${date}`;
}

/** e.g. "coca-cola-pitch-plan-2026-10-10.pdf" */
export function reportFileName(report: { brand: string; createdAt?: string }): string {
  return `${fileStem(report, "pitch-plan")}.pdf`;
}

/** e.g. "coca-cola-pitch-deck-2026-10-10.pptx" */
export function deckFileName(report: { brand: string; createdAt?: string }): string {
  return `${fileStem(report, "pitch-deck")}.pptx`;
}

/** Render the report to a PDF in the browser. react-pdf is loaded only when needed. */
export async function renderReport(report: ReportInput): Promise<Blob> {
  const { renderReportPdf } = await import("@/components/report-pdf");
  return renderReportPdf(report);
}

/** Upload a rendered PDF to /api/reports (multer → Cloudinary). */
export async function uploadReport(pdf: Blob, fileName: string): Promise<UploadedReport> {
  const form = new FormData();
  form.append("file", pdf, fileName);
  const res = await fetch("/api/reports", { method: "POST", body: form });
  const data = await res.json().catch(() => null);
  if (!res.ok || typeof data?.url !== "string" || typeof data?.publicId !== "string") {
    throw new Error(data?.error ?? "Saving the PDF failed.");
  }
  return { url: data.url, publicId: data.publicId };
}

/** Best effort: a leftover file in Cloudinary shouldn't block deleting a research. */
export function deleteUploadedReport(publicId: string) {
  fetch("/api/reports", {
    method: "DELETE",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ publicId }),
  }).catch(() => {});
}

function saveBlob(blob: Blob, fileName: string) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = fileName;
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

/** Download the saved Cloudinary copy; if there isn't one (or it can't be fetched), render it locally. */
export async function downloadReport(report: ReportInput & { pdfUrl?: string }) {
  const fileName = reportFileName(report);
  if (report.pdfUrl) {
    try {
      const res = await fetch(report.pdfUrl);
      if (res.ok) return saveBlob(await res.blob(), fileName);
    } catch {
      // Network/CORS failure: fall through and render a fresh copy.
    }
  }
  saveBlob(await renderReport(report), fileName);
}

/** Render the deck to .pptx on the backend and download it. */
export async function downloadDeckPptx(deck: Deck, fileName: string) {
  let res: Response;
  try {
    res = await fetch("/api/deck/pptx", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(deck),
    });
  } catch {
    throw new Error("Could not reach the server. Please try again.");
  }
  if (!res.ok) {
    const data = await res.json().catch(() => null);
    throw new Error(data?.error ?? "Building the PPTX failed.");
  }
  saveBlob(await res.blob(), fileName);
}
