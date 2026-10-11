import { forwardJson } from "@/lib/backend";

/** Disconnect the user's Canva account (204). */
export async function DELETE(request: Request) {
  return forwardJson(request, {
    method: "DELETE",
    path: "/api/canva/connection",
    timeoutMs: 15_000,
    timeoutMessage: "Disconnecting Canva timed out.",
    failureMessage: "Disconnecting Canva failed",
  });
}
