"use client";

import { useMemo, useSyncExternalStore } from "react";

import type { BrandPlan } from "@/lib/types";

// Saved in this browser only, until the backend can store researches per user.
const STORAGE_KEY = "pitchcraft:researches";
const MAX_SAVED = 50;
const CHANGE_EVENT = "pitchcraft:researches-changed";

export type SavedResearch = BrandPlan & {
  id: string;
  createdAt: string;
  /** Cloudinary copy of the report PDF, once uploaded. */
  pdfUrl?: string;
  pdfPublicId?: string;
};

function readRaw(): string | null {
  try {
    return localStorage.getItem(STORAGE_KEY);
  } catch {
    return null;
  }
}

function parse(raw: string | null): SavedResearch[] {
  try {
    const items = JSON.parse(raw ?? "[]");
    return Array.isArray(items) ? items : [];
  } catch {
    return [];
  }
}

function write(items: SavedResearch[]) {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(items.slice(0, MAX_SAVED)));
  } catch {
    // Storage full or blocked: history just won't persist.
  }
  window.dispatchEvent(new Event(CHANGE_EVENT));
}

export function saveResearch(plan: BrandPlan): SavedResearch {
  const item: SavedResearch = {
    ...plan,
    id: crypto.randomUUID(),
    createdAt: new Date().toISOString(),
  };
  write([item, ...parse(readRaw())]);
  return item;
}

export function updateResearch(id: string, patch: Partial<Omit<SavedResearch, "id">>) {
  write(parse(readRaw()).map((item) => (item.id === id ? { ...item, ...patch } : item)));
}

export function deleteResearch(id: string) {
  write(parse(readRaw()).filter((item) => item.id !== id));
}

function subscribe(onChange: () => void) {
  window.addEventListener("storage", onChange);
  window.addEventListener(CHANGE_EVENT, onChange);
  return () => {
    window.removeEventListener("storage", onChange);
    window.removeEventListener(CHANGE_EVENT, onChange);
  };
}

/** Saved researches, newest first. `null` until read on the client. */
export function useResearches(): SavedResearch[] | null {
  // The raw string is the snapshot: it's stable between reads, unlike a parsed array.
  const raw = useSyncExternalStore(subscribe, readRaw, () => undefined);
  return useMemo(() => (raw === undefined ? null : parse(raw)), [raw]);
}
