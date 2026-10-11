"use client";

import { useMemo, useSyncExternalStore } from "react";

import type { Deck } from "@/lib/types";

export type CanvaStatus = { connected: boolean; configured: boolean };
export type CanvaReturnPath = "/create" | "/your-researches";

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
  }
}

export const CANVA_NOT_CONFIGURED = "Canva isn't configured on this server yet, so decks can't be opened in Canva.";

async function call<T>(input: string, init: RequestInit, fallback: string): Promise<T> {
  let res: Response;
  try {
    res = await fetch(input, init);
  } catch {
    throw new ApiError("Could not reach the server. Please try again.", 0);
  }
  const data = await res.json().catch(() => null);
  if (!res.ok) {
    const message = res.status === 503 ? CANVA_NOT_CONFIGURED : (data?.error ?? fallback);
    throw new ApiError(message, res.status);
  }
  return data as T;
}

// One status request per page load, shared by every "Open in Canva" button.
let statusCache: { value?: CanvaStatus; promise?: Promise<CanvaStatus> } = {};

export function peekCanvaStatus(): CanvaStatus | undefined {
  return statusCache.value;
}

export function getCanvaStatus(): Promise<CanvaStatus> {
  if (statusCache.promise) return statusCache.promise;
  const promise = call<CanvaStatus>("/api/canva/status", { cache: "no-store" }, "Checking Canva failed.").then(
    (value) => {
      if (statusCache.promise === promise) statusCache.value = value;
      return value;
    },
  );
  statusCache = { promise };
  promise.catch(() => {
    // Don't cache failures (e.g. logged out): the next click asks again.
    if (statusCache.promise === promise) statusCache = {};
  });
  return promise;
}

export function forgetCanvaStatus() {
  statusCache = {};
}

// --- connect (OAuth redirect) and return -------------------------------------

const RESUME_KEY = "pitchcraft:canva-resume";

/**
 * Send the browser to Canva's consent screen. `resumeId` is the saved research
 * to show again when Canva redirects back (the page reloads in between).
 */
export async function startCanvaConnect(returnPath: CanvaReturnPath, resumeId?: string) {
  const { url } = await call<{ url: string }>(
    `/api/canva/authorize?return=${encodeURIComponent(returnPath)}`,
    { cache: "no-store" },
    "Connecting to Canva failed.",
  );
  if (typeof url !== "string" || !url.startsWith("https://www.canva.com/")) {
    throw new ApiError("Unexpected response from the server.", 502);
  }
  try {
    if (resumeId) sessionStorage.setItem(RESUME_KEY, resumeId);
    else sessionStorage.removeItem(RESUME_KEY);
  } catch {
    // Storage blocked: the user just won't land back on the same research.
  }
  forgetCanvaStatus();
  window.location.assign(url);
}

export type CanvaReturn = {
  /** Set when the page was opened by /api/canva/callback. */
  result: "connected" | "error" | null;
  reason: string | null;
  /** Saved research the user was on when they left for Canva. */
  resumeId: string | null;
};

const RETURN_EVENT = "pitchcraft:canva-return-changed";

function readReturn(): string {
  const params = new URLSearchParams(window.location.search);
  let resume = "";
  try {
    resume = sessionStorage.getItem(RESUME_KEY) ?? "";
  } catch {
    // ignore
  }
  return JSON.stringify([params.get("canva") ?? "", params.get("reason") ?? "", resume]);
}

function subscribeReturn(onChange: () => void) {
  window.addEventListener("popstate", onChange);
  window.addEventListener(RETURN_EVENT, onChange);
  return () => {
    window.removeEventListener("popstate", onChange);
    window.removeEventListener(RETURN_EVENT, onChange);
  };
}

/** The `?canva=` result of a Canva redirect, read on the client (null during prerender). */
export function useCanvaReturn(): CanvaReturn {
  const raw = useSyncExternalStore(subscribeReturn, readReturn, () => "");
  return useMemo(() => {
    if (!raw) return { result: null, reason: null, resumeId: null };
    const [result, reason, resume] = JSON.parse(raw) as [string, string, string];
    const known = result === "connected" || result === "error" ? result : null;
    return { result: known, reason: known ? reason || null : null, resumeId: known ? resume || null : null };
  }, [raw]);
}

/** Drop `?canva=` from the URL and forget the research to resume. */
export function clearCanvaReturn() {
  try {
    sessionStorage.removeItem(RESUME_KEY);
  } catch {
    // ignore
  }
  const url = new URL(window.location.href);
  if (url.searchParams.has("canva") || url.searchParams.has("reason")) {
    url.searchParams.delete("canva");
    url.searchParams.delete("reason");
    window.history.replaceState(window.history.state, "", url);
  }
  window.dispatchEvent(new Event(RETURN_EVENT));
}

export function canvaReturnMessage(ret: CanvaReturn): string | null {
  if (ret.result === "connected") return "Canva is connected. Press “Open in Canva” again to import the deck.";
  if (ret.result !== "error") return null;
  switch (ret.reason) {
    case "denied":
      return "Canva access wasn't granted, so the deck wasn't imported.";
    case "login":
      return "Your session ended while connecting Canva. Log in and try again.";
    case "account":
      return "That Canva sign-in was started from a different PitchCraft account.";
    default:
      return "Connecting Canva failed. Please try again.";
  }
}

// --- import -------------------------------------------------------------------

export async function importToCanva(deck: Deck): Promise<{ edit_url: string; view_url: string }> {
  const data = await call<{ edit_url?: unknown; view_url?: unknown }>(
    "/api/canva/import",
    { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(deck) },
    "Opening in Canva failed.",
  );
  if (typeof data?.edit_url !== "string" || !/^https:\/\/[^/]*canva\.com\//.test(data.edit_url)) {
    throw new ApiError("Canva returned an unexpected response.", 502);
  }
  return { edit_url: data.edit_url, view_url: typeof data.view_url === "string" ? data.view_url : data.edit_url };
}
