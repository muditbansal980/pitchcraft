import type { NextRequest } from "next/server";

import { forwardJson } from "@/lib/backend";
import { rememberCanvaReturn } from "@/lib/canva-return";

/** `{url}` of Canva's consent screen. `?return=/your-researches` picks where the callback lands. */
export async function GET(request: NextRequest) {
  const response = await forwardJson(request, {
    method: "GET",
    path: "/api/canva/authorize",
    timeoutMs: 15_000,
    timeoutMessage: "Connecting to Canva timed out.",
    failureMessage: "Connecting to Canva failed",
  });
  if (response.ok) await rememberCanvaReturn(request.nextUrl.searchParams.get("return"));
  return response;
}
