import { forwardJson } from "@/lib/backend";

/** Body: a `Deck`. Renders it, imports it into the user's Canva and returns `{edit_url, view_url}`. */
export async function POST(request: Request) {
  return forwardJson(request, {
    method: "POST",
    path: "/api/canva/import",
    forwardBody: true,
    maxBodyBytes: 2 * 1024 * 1024,
    // PPTX render + upload + up to ~60 s of Canva processing.
    timeoutMs: 180_000,
    timeoutMessage: "Canva took too long. Check your Canva designs, or try again.",
    failureMessage: "Opening in Canva failed",
  });
}
