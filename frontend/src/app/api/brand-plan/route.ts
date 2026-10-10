import { forwardBrandRequest } from "@/lib/backend";
import { PLAN_SCALES, type PlanScale } from "@/lib/types";

const MAX_BRIEF_CHARS = 2000;

export async function POST(request: Request) {
  // Research, multi-model writing and visuals: a flagship deck can take a few minutes.
  return forwardBrandRequest(request, {
    path: "/api/brand-plan",
    timeoutMs: 360_000,
    timeoutMessage: "Plan generation timed out. Please try again.",
    failureMessage: "Plan generation failed",
    extraFields: (body) => {
      const brief = typeof body.brief === "string" ? body.brief.trim().slice(0, MAX_BRIEF_CHARS) : "";
      const scale = PLAN_SCALES.includes(body.scale as PlanScale) ? (body.scale as PlanScale) : "auto";
      return brief ? { brief, scale } : { scale };
    },
  });
}
