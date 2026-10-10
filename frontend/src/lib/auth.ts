import { postToBackend } from "@/lib/backend";
import { startSession, type SessionUser } from "@/lib/session";

type AuthPayload = { email: string; password: string; username?: string };

type AuthResponse = {
  access_token?: unknown;
  expires_in?: unknown;
  user?: Partial<SessionUser>;
};

export async function readJson(request: Request): Promise<Record<string, unknown>> {
  const body = await request.json().catch(() => null);
  return body && typeof body === "object" ? body : {};
}

export function str(value: unknown): string {
  return typeof value === "string" ? value : "";
}

/**
 * Send credentials to the backend auth endpoint and, on success, store the
 * returned token in an httpOnly cookie. Expects `{access_token, expires_in, user}` back.
 */
export async function authenticate(path: string, payload: AuthPayload, failureMessage: string) {
  const result = await postToBackend(path, payload, {
    timeoutMs: 15_000,
    timeoutMessage: "The server took too long to respond. Please try again.",
    failureMessage,
  });
  if (!result.ok) {
    return Response.json({ error: result.error }, { status: result.status });
  }

  const data = result.data as AuthResponse | null;
  if (typeof data?.access_token !== "string") {
    return Response.json({ error: "Unexpected response from the server" }, { status: 502 });
  }

  const user: SessionUser = {
    id: data.user?.id,
    username: data.user?.username ?? payload.username ?? payload.email.split("@")[0],
    email: data.user?.email ?? payload.email,
  };
  const maxAge = typeof data.expires_in === "number" && data.expires_in > 0 ? data.expires_in : undefined;
  await startSession(data.access_token, user, maxAge);
  return Response.json({ user });
}
