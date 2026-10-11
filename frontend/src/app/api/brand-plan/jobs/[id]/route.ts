import { forwardJson } from "@/lib/backend";

// Backend job ids are secrets.token_urlsafe(16): 22 URL-safe characters.
const JOB_ID = /^[A-Za-z0-9_-]{8,64}$/;

/** Status of a plan job: `{status, stage, progress, result?, error?, status_code?}`. */
export async function GET(request: Request, ctx: RouteContext<"/api/brand-plan/jobs/[id]">) {
  const { id } = await ctx.params;
  if (!JOB_ID.test(id)) {
    return Response.json({ error: "Plan job not found." }, { status: 404 });
  }
  return forwardJson(request, {
    method: "GET",
    path: `/api/brand-plan/jobs/${id}`,
    timeoutMs: 20_000,
    timeoutMessage: "Checking the plan timed out.",
    failureMessage: "Checking the plan failed",
  });
}
