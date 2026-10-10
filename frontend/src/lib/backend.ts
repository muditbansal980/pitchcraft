import http from "node:http";
import https from "node:https";

import { endSession, getSessionToken, type SessionUser } from "@/lib/session";

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

type BrandForwardOptions = ForwardOptions & {
  /**
   * Extra fields to forward next to `brand`. Gets the parsed request body and
   * returns only validated values; anything it doesn't return is dropped.
   */
  extraFields?: (body: Record<string, unknown>) => Record<string, unknown>;
};

type JsonForwardOptions = ForwardOptions & {
  method: "GET" | "POST" | "DELETE";
  /** Forward the request's JSON body (POST only). */
  forwardBody?: boolean;
  /** Refuse bodies larger than this (default 1 MB). */
  maxBodyBytes?: number;
};

export type BackendResult =
  | { ok: true; data: unknown }
  | { ok: false; status: number; error: string };

type RawResult = { ok: true; res: Response } | { ok: false; status: number; error: string };

const DEFAULT_MAX_BODY_BYTES = 1024 * 1024;

/** FastAPI returns errors as `detail`: a string, or a list of validation errors. */
function readDetail(data: unknown): string | null {
  const detail = (data as { detail?: unknown } | null)?.detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail) && typeof detail[0]?.msg === "string") return detail[0].msg;
  return null;
}

// Node's fetch (undici) gives up after 300 s without response headers, whatever
// the AbortSignal says. Longer requests (flagship deck generation) use node:http.
const FETCH_HEADERS_LIMIT_MS = 290_000;

/** A buffered HTTP request with only our own overall timeout. */
function longRequest(
  url: string,
  init: { method: string; headers: Record<string, string>; body?: string },
  timeoutMs: number,
): Promise<Response> {
  return new Promise((resolve, reject) => {
    const target = new URL(url);
    const client = target.protocol === "https:" ? https : http;
    const headers = { ...init.headers, ...(init.body ? { "Content-Length": String(Buffer.byteLength(init.body)) } : {}) };
    const req = client.request(target, { method: init.method, headers }, (res) => {
      const chunks: Buffer[] = [];
      res.on("data", (chunk: Buffer) => chunks.push(chunk));
      res.on("error", fail);
      res.on("end", () => {
        clearTimeout(timer);
        const status = res.statusCode ?? 502;
        const out = new Headers();
        for (const [name, value] of Object.entries(res.headers)) {
          if (value !== undefined) out.set(name, Array.isArray(value) ? value.join(", ") : value);
        }
        const body = status === 204 || status === 304 ? null : new Uint8Array(Buffer.concat(chunks));
        resolve(new Response(body, { status, headers: out }));
      });
    });
    const timer = setTimeout(() => req.destroy(new DOMException("Request timed out", "TimeoutError")), timeoutMs);
    function fail(err: unknown) {
      clearTimeout(timer);
      reject(err);
    }
    req.on("error", fail);
    req.end(init.body);
  });
}

/** Send a request to the FastAPI backend, normalising network and timeout failures. */
async function sendToBackend(
  method: string,
  path: string,
  payload: unknown,
  options: BackendOptions,
): Promise<RawResult> {
  const headers: Record<string, string> = {
    ...(payload !== undefined ? { "Content-Type": "application/json" } : {}),
    ...(options.token ? { Authorization: `Bearer ${options.token}` } : {}),
  };
  const body = payload !== undefined ? JSON.stringify(payload) : undefined;
  try {
    const res =
      options.timeoutMs > FETCH_HEADERS_LIMIT_MS
        ? await longRequest(`${BACKEND_URL}${path}`, { method, headers, body }, options.timeoutMs)
        : await fetch(`${BACKEND_URL}${path}`, {
            method,
            headers,
            body,
            signal: AbortSignal.timeout(options.timeoutMs),
            cache: "no-store",
          });
    return { ok: true, res };
  } catch (err) {
    const timedOut =
      (err instanceof DOMException && err.name === "TimeoutError") ||
      // undici's own header/body timeouts surface as a TypeError with a coded cause.
      /TIMEOUT/i.test(String((err as { cause?: { code?: unknown } })?.cause?.code ?? ""));
    return {
      ok: false,
      status: timedOut ? 504 : 502,
      error: timedOut ? options.timeoutMessage : "Backend is not reachable.",
    };
  }
}

async function readError(res: Response, failureMessage: string): Promise<{ status: number; error: string }> {
  const data = await res.json().catch(() => null);
  return { status: res.status, error: readDetail(data) ?? failureMessage };
}

/** POST JSON to the FastAPI backend, normalising network, timeout and error responses. */
export async function postToBackend(
  path: string,
  payload: unknown,
  options: BackendOptions,
): Promise<BackendResult> {
  const sent = await sendToBackend("POST", path, payload, options);
  if (!sent.ok) return sent;
  const { res } = sent;
  if (!res.ok) return { ok: false, ...(await readError(res, options.failureMessage)) };
  return { ok: true, data: await res.json().catch(() => null) };
}

/** Map a backend failure to the `{error}` response the client expects. A 401 also ends the session. */
async function errorResponse(status: number, error: string) {
  if (status === 401) {
    // Token expired or invalid: clear the stale session so the UI shows "Log in".
    await endSession();
    return Response.json({ error: LOGIN_REQUIRED }, { status: 401 });
  }
  return Response.json({ error }, { status });
}

/** Parse the incoming JSON body as an object, refusing oversized or malformed ones. */
async function readJsonBody(
  request: Request,
  maxBytes: number,
): Promise<{ ok: true; body: Record<string, unknown> } | { ok: false; response: Response }> {
  if (Number(request.headers.get("content-length")) > maxBytes) {
    return { ok: false, response: Response.json({ error: "Request is too large." }, { status: 413 }) };
  }
  const text = await request.text().catch(() => "");
  if (text.length > maxBytes) {
    return { ok: false, response: Response.json({ error: "Request is too large." }, { status: 413 }) };
  }
  try {
    const body = JSON.parse(text);
    if (body && typeof body === "object" && !Array.isArray(body)) return { ok: true, body };
  } catch {
    // fall through
  }
  return { ok: false, response: Response.json({ error: "Invalid request body." }, { status: 400 }) };
}

/**
 * Validate `{brand}` from the incoming request and forward it, with the session
 * token, to the FastAPI backend, mapping FastAPI's `{detail}` errors to `{error}`.
 * Logged-out (or expired) users get a 401, which the client shows as a login prompt.
 */
export async function forwardBrandRequest(request: Request, options: BrandForwardOptions) {
  const token = await getSessionToken();
  if (!token) {
    return Response.json({ error: LOGIN_REQUIRED }, { status: 401 });
  }

  const body = await request.json().catch(() => null);
  const brand = typeof body?.brand === "string" ? body.brand.trim() : "";

  if (!brand) {
    return Response.json({ error: "Brand name is required" }, { status: 400 });
  }

  const extra = options.extraFields && body && typeof body === "object" ? options.extraFields(body) : {};
  const result = await postToBackend(options.path, { ...extra, brand }, { ...options, token });
  if (!result.ok) return errorResponse(result.status, result.error);
  return Response.json(result.data);
}

/**
 * Forward a JSON request (optionally with the incoming JSON body) with the
 * session token and return the backend's JSON, or `{error}` on failure.
 * A backend 204 is passed through without a body.
 */
export async function forwardJson(request: Request, options: JsonForwardOptions) {
  const token = await getSessionToken();
  if (!token) {
    return Response.json({ error: LOGIN_REQUIRED }, { status: 401 });
  }

  let payload: unknown;
  if (options.forwardBody) {
    const parsed = await readJsonBody(request, options.maxBodyBytes ?? DEFAULT_MAX_BODY_BYTES);
    if (!parsed.ok) return parsed.response;
    payload = parsed.body;
  }

  const sent = await sendToBackend(options.method, options.path, payload, { ...options, token });
  if (!sent.ok) return errorResponse(sent.status, sent.error);
  const { res } = sent;
  if (!res.ok) {
    const { status, error } = await readError(res, options.failureMessage);
    return errorResponse(status, error);
  }
  if (res.status === 204) return new Response(null, { status: 204 });
  const data = await res.json().catch(() => null);
  return Response.json(data, { status: res.status });
}

const PASSTHROUGH_HEADERS = ["content-type", "content-disposition", "content-length"];

/**
 * POST the incoming JSON body to the backend and stream a binary response
 * (e.g. a .pptx) back with its content headers. Errors come back as `{error}`.
 */
export async function forwardBinary(request: Request, options: ForwardOptions & { maxBodyBytes?: number }) {
  const token = await getSessionToken();
  if (!token) {
    return Response.json({ error: LOGIN_REQUIRED }, { status: 401 });
  }

  const parsed = await readJsonBody(request, options.maxBodyBytes ?? DEFAULT_MAX_BODY_BYTES);
  if (!parsed.ok) return parsed.response;

  const sent = await sendToBackend("POST", options.path, parsed.body, { ...options, token });
  if (!sent.ok) return errorResponse(sent.status, sent.error);
  const { res } = sent;
  if (!res.ok) {
    const { status, error } = await readError(res, options.failureMessage);
    return errorResponse(status, error);
  }

  const headers = new Headers({ "Cache-Control": "no-store" });
  for (const name of PASSTHROUGH_HEADERS) {
    const value = res.headers.get(name);
    if (value) headers.set(name, value);
  }
  return new Response(res.body, { status: 200, headers });
}

/** Fetch a public backend path (no session), e.g. a `/media/<file>` image. */
export async function fetchBackendPublic(path: string, timeoutMs: number): Promise<Response | null> {
  try {
    return await fetch(`${BACKEND_URL}${path}`, { signal: AbortSignal.timeout(timeoutMs), cache: "no-store" });
  } catch {
    return null;
  }
}

/**
 * The logged-in user as confirmed by the backend (`GET /api/auth/me` validates the JWT).
 * Use this, not `getSessionUser()`, when the user's identity authorises an action:
 * the `pc_user` cookie is only a display copy and isn't signed.
 */
export async function getVerifiedUser(): Promise<SessionUser | null> {
  const token = await getSessionToken();
  if (!token) return null;
  try {
    const res = await fetch(`${BACKEND_URL}/api/auth/me`, {
      headers: { Authorization: `Bearer ${token}` },
      signal: AbortSignal.timeout(10_000),
    });
    if (!res.ok) return null;
    const user = await res.json();
    return typeof user?.id === "number" && typeof user?.username === "string" ? user : null;
  } catch {
    return null;
  }
}
