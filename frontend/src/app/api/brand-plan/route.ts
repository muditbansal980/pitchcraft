import { forwardBrandRequest } from "@/lib/backend";

export async function POST(request: Request) {
  // Research (Tavily) + plan generation (LLM) typically takes 10–30 s; allow headroom.
  return forwardBrandRequest(request, {
    path: "/api/brand-plan",
    timeoutMs: 120_000,
    timeoutMessage: "Plan generation timed out. Please try again.",
    failureMessage: "Plan generation failed",
  });
}
