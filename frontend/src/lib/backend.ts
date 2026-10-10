const BACKEND_URL = process.env.BACKEND_URL ?? "http://localhost:8000";

type ForwardOptions = {
  /** Backend path, e.g. "/api/brand-info". */
  path: string;
  timeoutMs: number;
  timeoutMessage: string;
  failureMessage: string;
};

/**
 * Validate `{brand}` from the incoming request and forward it to the FastAPI backend,
 * mapping FastAPI's `{detail}` errors to `{error}` for the client.
 */
export async function forwardBrandRequest(request: Request, options: ForwardOptions) {
  const body = await request.json().catch(() => null);
  const brand = typeof body?.brand === "string" ? body.brand.trim() : "";

  if (!brand) {
    return Response.json({ error: "Brand name is required" }, { status: 400 });
  }

  let res: Response;
  try {
    res = await fetch(`${BACKEND_URL}${options.path}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ brand }),
      signal: AbortSignal.timeout(options.timeoutMs),
    });
  } catch (err) {
    const timedOut = err instanceof DOMException && err.name === "TimeoutError";
    return Response.json(
      { error: timedOut ? options.timeoutMessage : "Backend is not reachable." },
      { status: timedOut ? 504 : 502 },
    );
  }

  const data = await res.json().catch(() => null);
  if (!res.ok) {
    // FastAPI returns errors as `detail` (a string, or a list for validation errors).
    const detail = typeof data?.detail === "string" ? data.detail : options.failureMessage;
    return Response.json({ error: detail }, { status: res.status });
  }

  return Response.json(data);
}
