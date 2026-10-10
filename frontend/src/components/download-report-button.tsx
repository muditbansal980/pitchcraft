"use client";

import { useState } from "react";

import { downloadReport } from "@/lib/report-files";
import type { SavedResearch } from "@/lib/researches";

export function DownloadReportButton({ research }: { research: SavedResearch }) {
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState(false);

  async function handleClick() {
    setBusy(true);
    setFailed(false);
    try {
      await downloadReport(research);
    } catch {
      setFailed(true);
    } finally {
      setBusy(false);
    }
  }

  return (
    <button type="button" onClick={handleClick} disabled={busy} className="brutal-btn h-9 px-3 text-xs">
      {busy ? "Preparing PDF…" : failed ? "Retry download" : "Download PDF"}
    </button>
  );
}
