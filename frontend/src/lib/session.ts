import { cookies } from "next/headers";

// Both cookies are httpOnly: the browser never sees the token, and the user is
// read server-side (e.g. by the sidebar) rather than from client JS.
const TOKEN_COOKIE = "pc_token";
const USER_COOKIE = "pc_user";
// Matches the backend JWT lifetime (ACCESS_TOKEN_EXPIRE_MINUTES); the backend's
// `expires_in` overrides it so both cookies expire together with the token.
const DEFAULT_MAX_AGE_SECONDS = 60 * 60 * 3;

export type SessionUser = {
  id?: string | number;
  username: string;
  email: string;
};

function cookieOptions(maxAge: number) {
  return {
    httpOnly: true,
    secure: process.env.NODE_ENV === "production",
    sameSite: "lax",
    path: "/",
    maxAge,
  } as const;
}

export async function startSession(token: string, user: SessionUser, maxAge = DEFAULT_MAX_AGE_SECONDS) {
  const store = await cookies();
  store.set(TOKEN_COOKIE, token, cookieOptions(maxAge));
  store.set(USER_COOKIE, JSON.stringify(user), cookieOptions(maxAge));
}

export async function endSession() {
  const store = await cookies();
  store.delete(TOKEN_COOKIE);
  store.delete(USER_COOKIE);
}

export async function getSessionToken(): Promise<string | null> {
  const store = await cookies();
  return store.get(TOKEN_COOKIE)?.value || null;
}

export async function getSessionUser(): Promise<SessionUser | null> {
  const store = await cookies();
  if (!store.has(TOKEN_COOKIE)) return null;
  try {
    const user = JSON.parse(store.get(USER_COOKIE)?.value ?? "");
    return typeof user?.username === "string" ? user : null;
  } catch {
    return null;
  }
}
