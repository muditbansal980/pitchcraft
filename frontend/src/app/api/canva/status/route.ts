import { forwardJson } from "@/lib/backend";

/** `{connected, configured}` for the logged-in user. */
export async function GET(request: Request) {
  return forwardJson(request, {
    method: "GET",
    path: "/api/canva/status",
    timeoutMs: 15_000,
    timeoutMessage: "Checking Canva timed out.",
    failureMessage: "Checking the Canva connection failed",
  });
}
