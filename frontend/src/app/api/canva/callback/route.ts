import type { NextRequest } from "next/server";

import { postToBackend } from "@/lib/backend";
import { takeCanvaReturn } from "@/lib/canva-return";
import { getSessionToken } from "@/lib/session";

/**
 * Canva's OAuth redirect URI (register it on the integration, e.g.
 * http://127.0.0.1:3000/api/canva/callback). The browser arrives with `?code&state`
 * (or `?error` when the user declines); the backend swaps the code for tokens, and
 * the user is sent back to the page they came from with `?canva=connected|error`.
 */
export async function GET(request: NextRequest) {
  const params = request.nextUrl.searchParams;
  const code = params.get("code");
  const state = params.get("state");
  const returnPath = await takeCanvaReturn();

  // A relative Location keeps the browser on the host it used. (nextUrl.origin
  // rewrites 127.0.0.1 to localhost, whose cookies, i.e. the session, differ.)
  const back = (result: "connected" | "error", reason?: string) => {
    const query = new URLSearchParams({ canva: result, ...(reason ? { reason } : {}) });
    return new Response(null, { status: 303, headers: { Location: `${returnPath}?${query}` } });
  };

  if (params.get("error")) return back("error", "denied");
  if (!code || !state || code.length > 4096 || state.length > 4096) return back("error", "invalid");

  const token = await getSessionToken();
  if (!token) return back("error", "login");

  const result = await postToBackend(
    "/api/canva/callback",
    { code, state },
    {
      token,
      timeoutMs: 30_000,
      timeoutMessage: "Connecting to Canva timed out.",
      failureMessage: "Connecting to Canva failed",
    },
  );
  if (!result.ok) {
    console.error("Canva callback failed", result.status, result.error);
    return back("error", result.status === 401 ? "login" : result.status === 403 ? "account" : "failed");
  }
  return back("connected");
}
