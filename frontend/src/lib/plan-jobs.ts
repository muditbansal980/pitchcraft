"use client";

import { useMemo, useSyncExternalStore } from "react";

import type { PlanJob, PlanJobStage, PlanScale } from "@/lib/types";

/** A plan job this tab started and is waiting for. Kept in sessionStorage so a reload resumes it. */
export type PendingJob = {
  id: string;
  brand: string;
  brief: string;
  scale: PlanScale;
  startedAt: number;
};

const PENDING_KEY = "pitchcraft:plan-job";
const CHANGE_EVENT = "pitchcraft:plan-job-changed";

export const POLL_INTERVAL_MS = 3000;

export const JOB_STAGES: { stage: PlanJobStage; label: string }[] = [
  { stage: "research", label: "Researching the brand" },
  { stage: "identity", label: "Pinning down the brand identity" },
  { stage: "deep_research", label: "Reading the brand's site, blog and news" },
  { stage: "strategy", label: "Building the strategy" },
  { stage: "writing", label: "Writing slides" },
  { stage: "editing", label: "Editing for one voice" },
  { stage: "visuals", label: "Finding memes & refs, designing the deck" },
  { stage: "done", label: "Wrapping up" },
];

/** Index into JOB_STAGES (-1 while queued or for an unknown stage). */
export function stageIndex(stage: string): number {
  return JOB_STAGES.findIndex((item) => item.stage === stage);
}

function readRaw(): string {
  try {
    return sessionStorage.getItem(PENDING_KEY) ?? "";
  } catch {
    return "";
  }
}

function subscribe(onChange: () => void) {
  window.addEventListener(CHANGE_EVENT, onChange);
  return () => window.removeEventListener(CHANGE_EVENT, onChange);
}

function parse(raw: string): PendingJob | null {
  if (!raw) return null;
  try {
    const job = JSON.parse(raw);
    return typeof job?.id === "string" && typeof job?.brand === "string" && typeof job?.startedAt === "number"
      ? job
      : null;
  } catch {
    return null;
  }
}

/** The job this tab is waiting for (null on the server and when there is none). */
export function usePendingJob(): PendingJob | null {
  const raw = useSyncExternalStore(subscribe, readRaw, () => "");
  return useMemo(() => parse(raw), [raw]);
}

function setPending(job: PendingJob | null) {
  try {
    if (job) sessionStorage.setItem(PENDING_KEY, JSON.stringify(job));
    else sessionStorage.removeItem(PENDING_KEY);
  } catch {
    // Storage blocked: polling still works until the page is reloaded.
  }
  window.dispatchEvent(new Event(CHANGE_EVENT));
}

export function clearPendingJob(id?: string) {
  if (id && parse(readRaw())?.id !== id) return;
  setPending(null);
}

export class JobRequestError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
  }
}

async function readJson(res: Response) {
  return res.json().catch(() => null);
}

/** Start a job and remember it; resolves to the pending job. */
export async function startPlanJob(input: { brand: string; brief: string; scale: PlanScale }): Promise<PendingJob> {
  let res: Response;
  try {
    res = await fetch("/api/brand-plan/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ brand: input.brand, brief: input.brief || undefined, scale: input.scale }),
    });
  } catch {
    throw new JobRequestError("Could not reach the server. Please try again.", 0);
  }
  const data = await readJson(res);
  if (!res.ok || typeof data?.job_id !== "string") {
    throw new JobRequestError(data?.error ?? "Starting the plan failed. Please try again.", res.status);
  }
  const job: PendingJob = { id: data.job_id, ...input, startedAt: Date.now() };
  setPending(job);
  return job;
}

export async function fetchPlanJob(id: string): Promise<PlanJob> {
  let res: Response;
  try {
    res = await fetch(`/api/brand-plan/jobs/${encodeURIComponent(id)}`, { cache: "no-store" });
  } catch {
    throw new JobRequestError("Could not reach the server.", 0);
  }
  const data = await readJson(res);
  if (!res.ok || typeof data?.status !== "string") {
    throw new JobRequestError(data?.error ?? "Checking the plan failed.", res.status);
  }
  return data as PlanJob;
}
