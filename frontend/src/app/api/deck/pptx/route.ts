import { forwardBinary } from "@/lib/backend";

/** Body: a `Deck`. Streams the rendered .pptx back as an attachment. */
export async function POST(request: Request) {
  return forwardBinary(request, {
    path: "/api/deck/pptx",
    timeoutMs: 120_000,
    timeoutMessage: "Building the PPTX timed out. Please try again.",
    failureMessage: "Building the PPTX failed",
    // Decks carry image URLs only, so even a 45-slide deck is far below this.
    maxBodyBytes: 2 * 1024 * 1024,
  });
}
