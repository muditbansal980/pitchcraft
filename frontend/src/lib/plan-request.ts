import { PLAN_SCALES, type PlanScale } from "@/lib/types";

export const MAX_BRIEF_CHARS = 2000;

/** The optional plan fields forwarded next to `brand`: a trimmed brief and a known scale. */
export function planRequestFields(body: Record<string, unknown>): Record<string, unknown> {
  const brief = typeof body.brief === "string" ? body.brief.trim().slice(0, MAX_BRIEF_CHARS) : "";
  const scale = PLAN_SCALES.includes(body.scale as PlanScale) ? (body.scale as PlanScale) : "auto";
  return brief ? { brief, scale } : { scale };
}
