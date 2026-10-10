import { endSession, getSessionToken } from "@/lib/session";

const BACKEND_URL = process.env.BACKEND_URL ?? "http://localhost:8000";

const LOGIN_REQUIRED = "Please log in to continue.";

type BackendOptions = {
  timeoutMs: number;
  timeoutMessage: string;
  failureMessage: string;
  /** JWT sent as `Authorization: Bearer <token>`. */
  token?: string;
};

type ForwardOptions = BackendOptions & {
  /** Backend path, e.g. "/api/brand-info". */
  path: string;
};

export type BackendResult =
  | { ok: true; data: unknown }
  | { ok: false; status: number; error: string };

/** FastAPI returns errors as `detail`: a string, or a list of validation errors. */
function readDetail(data: unknown): string | null {
  const detail = (data as { detail?: unknown } | null)?.detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail) && typeof detail[0]?.msg === "string") return detail[0].msg;
  return null;
}

/** POST JSON to the FastAPI backend, normalising network, timeout and error responses. */
export async function postToBackend(
  path: string,
  payload: unknown,
  options: BackendOptions,
): Promise<BackendResult> {
  let res: Response;
  try {
    res = await fetch(`${BACKEND_URL}${path}`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        ...(options.token ? { Authorization: `Bearer ${options.token}` } : {}),
      },
      body: JSON.stringify(payload),
      signal: AbortSignal.timeout(options.timeoutMs),
    });
  } catch (err) {
    const timedOut = err instanceof DOMException && err.name === "TimeoutError";
    return {
      ok: false,
      status: timedOut ? 504 : 502,
      error: timedOut ? options.timeoutMessage : "Backend is not reachable.",
    };
  }

  const data = await res.json().catch(() => null);
  if (!res.ok) {
    return { ok: false, status: res.status, error: readDetail(data) ?? options.failureMessage };
  }
  return { ok: true, data };
}

/**
 * Validate `{brand}` from the incoming request and forward it, with the session
 * token, to the FastAPI backend, mapping FastAPI's `{detail}` errors to `{error}`.
 * Logged-out (or expired) users get a 401, which the client shows as a login prompt.
 */
export async function forwardBrandRequest(request: Request, options: ForwardOptions) {
  const token = await getSessionToken();
  if (!token) {
    return Response.json({ error: LOGIN_REQUIRED }, { status: 401 });
  }

  const body = await request.json().catch(() => null);
  const brand = typeof body?.brand === "string" ? body.brand.trim() : "";

  if (!brand) {
    return Response.json({ error: "Brand name is required" }, { status: 400 });
  }

  const result = await postToBackend(options.path, { brand }, { ...options, token });
  if (!result.ok) {
    if (result.status === 401) {
      // Token expired or invalid: clear the stale session so the UI shows "Log in".
      await endSession();
      return Response.json({ error: LOGIN_REQUIRED }, { status: 401 });
    }
    return Response.json({ error: result.error }, { status: result.status });
  }
  return Response.json(result.data);
}
