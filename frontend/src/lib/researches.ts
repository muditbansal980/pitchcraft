"use client";

import { useMemo, useSyncExternalStore } from "react";

import { deleteUploadedReport } from "@/lib/report-files";
import type { BrandPlan, Deck, Visual } from "@/lib/types";

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
  // Decks make entries bigger, so when storage is full drop the oldest researches
  // (and their stored PDFs) until the rest fit, instead of saving nothing.
  let kept = items.slice(0, MAX_SAVED);
  while (kept.length > 0) {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(kept));
      break;
    } catch (err) {
      const quota = err instanceof DOMException && (err.name === "QuotaExceededError" || err.code === 22);
      if (!quota || kept.length === 1) break; // blocked storage: history just won't persist
      const dropped = kept.slice(Math.ceil(kept.length / 2));
      kept = kept.slice(0, Math.ceil(kept.length / 2));
      for (const old of dropped) {
        if (old.pdfPublicId) deleteUploadedReport(old.pdfPublicId);
      }
    }
  }
  window.dispatchEvent(new Event(CHANGE_EVENT));
}

/** Keep image references as URLs only: inline (data:) images would fill localStorage. */
function urlOnly(visual: Visual | null | undefined): Visual | null | undefined {
  if (!visual?.url || /^(\/media\/|https?:\/\/)/i.test(visual.url)) return visual;
  return { ...visual, url: null, kind: "placeholder" };
}

function storableDeck(deck: Deck): Deck {
  return {
    ...deck,
    theme: { ...deck.theme, hero_image: urlOnly(deck.theme.hero_image) },
    slides: deck.slides.map((slide) => {
      switch (slide.layout) {
        case "idea":
          return { ...slide, visual: urlOnly(slide.visual) };
        case "samples":
        case "ref":
          return { ...slide, visuals: slide.visuals.map((v) => urlOnly(v) as Visual) };
        case "card_grid":
          return { ...slide, cards: slide.cards.map((card) => ({ ...card, visual: urlOnly(card.visual) })) };
        default:
          return slide;
      }
    }),
  };
}

export function saveResearch(plan: BrandPlan): SavedResearch {
  const item: SavedResearch = {
    ...plan,
    deck: plan.deck ? storableDeck(plan.deck) : undefined,
    id: crypto.randomUUID(),
    createdAt: new Date().toISOString(),
  };
  const items = [item, ...parse(readRaw())];
  // Researches past the cap are dropped, so drop their stored PDFs too.
  for (const old of items.slice(MAX_SAVED)) {
    if (old.pdfPublicId) deleteUploadedReport(old.pdfPublicId);
  }
  write(items);
  return item;
}

/** Returns false if the research no longer exists (e.g. it was deleted meanwhile). */
export function updateResearch(id: string, patch: Partial<Omit<SavedResearch, "id">>): boolean {
  const items = parse(readRaw());
  if (!items.some((item) => item.id === id)) return false;
  write(items.map((item) => (item.id === id ? { ...item, ...patch } : item)));
  return true;
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
