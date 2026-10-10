"use client";

import { useEffect, useState } from "react";

import {
  ApiError,
  CANVA_NOT_CONFIGURED,
  forgetCanvaStatus,
  getCanvaStatus,
  importToCanva,
  peekCanvaStatus,
  startCanvaConnect,
  type CanvaReturnPath,
} from "@/lib/canva";
import { deckFileName, downloadDeckPptx } from "@/lib/report-files";
import type { Deck } from "@/lib/types";

type Props = {
  deck: Deck;
  brand: string;
  createdAt?: string;
  /** Saved research to reopen after the Canva consent redirect. */
  researchId?: string;
  /** Page Canva's redirect should return to. */
  returnPath: CanvaReturnPath;
};

type CanvaState =
  | { state: "idle" }
  | { state: "busy"; label: string }
  | { state: "done"; editUrl: string; blocked: boolean }
  | { state: "error"; error: string };

/**
 * Open a tab right away, while the click still counts as a user gesture, so the
 * import result can be shown in it without the popup blocker stepping in.
 */
function openPendingTab(): Window | null {
  const tab = window.open("", "_blank");
  if (!tab) return null;
  try {
    tab.opener = null;
    tab.document.title = "Opening in Canva…";
    tab.document.body.style.font = "16px system-ui, sans-serif";
    tab.document.body.style.padding = "2rem";
    tab.document.body.textContent = "Importing your deck into Canva… this can take up to a minute.";
  } catch {
    // Some browsers restrict the blank tab; it still navigates later.
  }
  return tab;
}

export function DeckActions({ deck, brand, createdAt, researchId, returnPath }: Props) {
  const [pptx, setPptx] = useState<{ busy: boolean; error?: string }>({ busy: false });
  const [canva, setCanva] = useState<CanvaState>({ state: "idle" });

  // Warm the (shared, cached) Canva status so the first click can open its tab synchronously.
  useEffect(() => {
    getCanvaStatus().catch(() => {});
  }, []);

  async function handleDownload() {
    setPptx({ busy: true });
    try {
      await downloadDeckPptx(deck, deckFileName({ brand, createdAt }));
      setPptx({ busy: false });
    } catch (err) {
      setPptx({ busy: false, error: err instanceof Error ? err.message : "Building the PPTX failed." });
    }
  }

  async function handleCanva() {
    const known = peekCanvaStatus();
    let tab = known?.configured && known.connected ? openPendingTab() : null;
    setCanva({ state: "busy", label: "Checking Canva…" });
    try {
      const status = known ?? (await getCanvaStatus());
      if (!status.configured) throw new ApiError(CANVA_NOT_CONFIGURED, 503);
      if (!status.connected) {
        tab?.close();
        tab = null;
        setCanva({ state: "busy", label: "Opening Canva sign-in…" });
        await startCanvaConnect(returnPath, researchId);
        return; // the browser is leaving for canva.com
      }

      setCanva({ state: "busy", label: "Importing into Canva…" });
      const { edit_url } = await importToCanva(deck);
      let blocked = false;
      if (tab && !tab.closed) {
        tab.location.href = edit_url;
      } else {
        blocked = window.open(edit_url, "_blank", "noopener,noreferrer") === null;
      }
      setCanva({ state: "done", editUrl: edit_url, blocked });
    } catch (err) {
      tab?.close();
      let error = err instanceof Error ? err.message : "Opening in Canva failed.";
      if (err instanceof ApiError && err.status === 409) {
        // Not connected any more (revoked or expired): the next click reconnects.
        forgetCanvaStatus();
        error = `${error} Press “Open in Canva” to reconnect.`;
      }
      setCanva({ state: "error", error });
    }
  }

  const canvaBusy = canva.state === "busy";

  return (
    <div className="flex flex-col gap-2">
      <div className="flex flex-wrap gap-3">
        <button
          type="button"
          onClick={handleDownload}
          disabled={pptx.busy}
          className="brutal-btn h-9 px-3 text-xs"
        >
          {pptx.busy ? "Building PPTX…" : pptx.error ? "Retry PPTX" : "Download PPTX"}
        </button>
        <button
          type="button"
          onClick={handleCanva}
          disabled={canvaBusy}
          className="brutal-btn h-9 bg-accent-2 px-3 text-xs"
        >
          {canvaBusy ? canva.label : "Open in Canva"}
        </button>
      </div>
      <div aria-live="polite" className="flex flex-col gap-1 font-mono text-xs">
        {pptx.error && <p className="border-2 border-foreground bg-danger px-2 py-1 font-bold text-black">{pptx.error}</p>}
        {canva.state === "error" && (
          <p className="border-2 border-foreground bg-danger px-2 py-1 font-bold text-black">{canva.error}</p>
        )}
        {canvaBusy && canva.label.startsWith("Importing") && (
          <p className="text-muted">Building the PPTX and importing it. Canva can take up to a minute.</p>
        )}
        {canva.state === "done" && (
          <p>
            {canva.blocked ? "Imported. " : "Opened in a new tab. "}
            <a
              href={canva.editUrl}
              target="_blank"
              rel="noopener noreferrer"
              className="font-bold underline decoration-2 underline-offset-2 hover:bg-accent hover:text-black"
            >
              Open the Canva design ↗
            </a>
          </p>
        )}
      </div>
    </div>
  );
}
