import { forwardBrandRequest } from "@/lib/backend";

export async function POST(request: Request) {
  return forwardBrandRequest(request, {
    path: "/api/brand-info",
    timeoutMs: 90_000,
    timeoutMessage: "Brand research timed out. Please try again.",
    failureMessage: "Brand research failed",
  });
}
