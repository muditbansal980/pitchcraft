"use client";

import Link from "next/link";

import { PlanMarkdown } from "@/components/plan-markdown";
import { DownloadReportButton } from "@/components/download-report-button";
import { ResearchSources } from "@/components/research-sources";
import { deleteUploadedReport } from "@/lib/report-files";
import { deleteResearch, useResearches, type SavedResearch } from "@/lib/researches";

const dateFormat = new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" });

function handleDelete(item: SavedResearch) {
  deleteResearch(item.id);
  if (item.pdfPublicId) deleteUploadedReport(item.pdfPublicId);
}

export default function YourResearchesPage() {
  const researches = useResearches();

  return (
    <div className="mx-auto flex w-full max-w-3xl flex-col gap-8 px-4 py-10 md:px-8 md:py-14">
      <header className="flex flex-col gap-2">
        <h1 className="text-4xl font-bold tracking-tight uppercase md:text-5xl">Your researches</h1>
        <p className="font-mono text-sm text-muted">Plans you&apos;ve generated, newest first.</p>
      </header>

      {researches === null ? null : researches.length === 0 ? (
        <div className="brutal-box flex flex-col items-start gap-4 p-6">
          <p className="text-lg font-bold">No researches yet.</p>
          <Link href="/create" className="brutal-btn h-11 px-5">
            + New research
          </Link>
        </div>
      ) : (
        <ul className="flex flex-col gap-5">
          {researches.map((item) => (
            <li key={item.id} className="brutal-box">
              <details className="group">
                <summary className="flex cursor-pointer list-none items-center justify-between gap-3 p-4 select-none hover:bg-accent hover:text-black [&::-webkit-details-marker]:hidden">
                  <div className="min-w-0">
                    <p className="truncate text-xl font-bold uppercase">{item.brand}</p>
                    <p className="font-mono text-xs">{dateFormat.format(new Date(item.createdAt))}</p>
                  </div>
                  <span aria-hidden="true" className="font-mono text-lg transition-transform group-open:rotate-90">
                    ▶
                  </span>
                </summary>
                <div className="flex flex-col gap-6 border-t-3 border-foreground p-4 md:p-6">
                  <PlanMarkdown>{item.plan}</PlanMarkdown>
                  <ResearchSources research={item.research} />
                  <div className="flex flex-wrap gap-3">
                    <DownloadReportButton research={item} />
                    <button
                      type="button"
                      onClick={() => handleDelete(item)}
                      className="brutal-btn h-9 bg-danger px-3 text-xs"
                    >
                      Delete
                    </button>
                  </div>
                </div>
              </details>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
