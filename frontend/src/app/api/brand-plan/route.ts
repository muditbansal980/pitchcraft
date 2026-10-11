import { forwardBrandRequest } from "@/lib/backend";
import { planRequestFields } from "@/lib/plan-request";

/**
 * Synchronous plan generation (kept for API clients; the UI uses /api/brand-plan/jobs).
 * A flagship deck can take a few minutes.
 */
export async function POST(request: Request) {
  return forwardBrandRequest(request, {
    path: "/api/brand-plan",
    timeoutMs: 360_000,
    timeoutMessage: "Plan generation timed out. Please try again.",
    failureMessage: "Plan generation failed",
    extraFields: planRequestFields,
  });
}
