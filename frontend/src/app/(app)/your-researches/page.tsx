"use client";

import Link from "next/link";
import { useState } from "react";

import { DeckActions } from "@/components/deck-actions";
import { DossierPanel } from "@/components/dossier-panel";
import { PlanMarkdown } from "@/components/plan-markdown";
import { DownloadReportButton } from "@/components/download-report-button";
import { ResearchSources } from "@/components/research-sources";
import { SlideStrip } from "@/components/slide-strip";
import { canvaReturnMessage, useCanvaReturn } from "@/lib/canva";
import { deleteUploadedReport } from "@/lib/report-files";
import { deleteResearch, useResearches, type SavedResearch } from "@/lib/researches";

const dateFormat = new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" });

function handleDelete(item: SavedResearch) {
  deleteResearch(item.id);
  if (item.pdfPublicId) deleteUploadedReport(item.pdfPublicId);
}

export default function YourResearchesPage() {
  const researches = useResearches();
  // Back from Canva's consent screen: reopen the research the user was on.
  const canvaReturn = useCanvaReturn();
  const [noticeDismissed, setNoticeDismissed] = useState(false);
  const notice = noticeDismissed ? null : canvaReturnMessage(canvaReturn);

  return (
    <div className="mx-auto flex w-full max-w-3xl flex-col gap-8 px-4 py-10 md:px-8 md:py-14">
      <header className="flex flex-col gap-2">
        <h1 className="text-4xl font-bold tracking-tight uppercase md:text-5xl">Your researches</h1>
        <p className="font-mono text-sm text-muted">Plans you&apos;ve generated, newest first.</p>
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
              <details className="group" open={item.id === canvaReturn.resumeId || undefined}>
                <summary className="flex cursor-pointer list-none items-center justify-between gap-3 p-4 select-none hover:bg-accent hover:text-black [&::-webkit-details-marker]:hidden">
                  <div className="min-w-0">
                    <p className="truncate text-xl font-bold uppercase">{item.brand}</p>
                    <p className="font-mono text-xs">{dateFormat.format(new Date(item.createdAt))}</p>
                  </div>
                  <span aria-hidden="true" className="font-mono text-lg transition-transform group-open:rotate-90">
                    ▶
                  </span>
                </summary>
                <div className="flex min-w-0 flex-col gap-6 border-t-3 border-foreground p-4 md:p-6">
                  {item.deck && item.deck.slides.length > 0 && (
                    <div className="flex min-w-0 flex-col gap-3">
                      <SlideStrip deck={item.deck} />
                      <DeckActions
                        deck={item.deck}
                        brand={item.brand}
                        createdAt={item.createdAt}
                        researchId={item.id}
                        returnPath="/your-researches"
                      />
                    </div>
                  )}
                  <PlanMarkdown>{item.plan}</PlanMarkdown>
                  {item.dossier && <DossierPanel dossier={item.dossier} />}
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
