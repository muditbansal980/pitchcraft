import { forwardBrandRequest } from "@/lib/backend";
import { planRequestFields } from "@/lib/plan-request";

/** Start a background plan job: body `{brand, brief?, scale?}` -> `{job_id}`. */
export async function POST(request: Request) {
  return forwardBrandRequest(request, {
    path: "/api/brand-plan/jobs",
    timeoutMs: 30_000,
    timeoutMessage: "Starting the plan timed out. Please try again.",
    failureMessage: "Starting the plan failed",
    extraFields: planRequestFields,
  });
}
