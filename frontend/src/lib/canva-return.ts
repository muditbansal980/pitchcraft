import { cookies } from "next/headers";

// Where /api/canva/callback sends the browser back to after Canva's consent screen.
// Only these paths are allowed, so the cookie can't become an open redirect.
export const CANVA_RETURN_PATHS = ["/create", "/your-researches"] as const;
export type CanvaReturnPath = (typeof CANVA_RETURN_PATHS)[number];

const RETURN_COOKIE = "pc_canva_return";

function isReturnPath(value: unknown): value is CanvaReturnPath {
  return CANVA_RETURN_PATHS.includes(value as CanvaReturnPath);
}

export async function rememberCanvaReturn(path: string | null) {
  const store = await cookies();
  store.set(RETURN_COOKIE, isReturnPath(path) ? path : "/create", {
    httpOnly: true,
    secure: process.env.NODE_ENV === "production",
    // Lax: the cookie must survive the top-level redirect back from canva.com.
    sameSite: "lax",
    path: "/api/canva",
    // Matches the backend's OAuth state lifetime.
    maxAge: 10 * 60,
  });
}

export async function takeCanvaReturn(): Promise<CanvaReturnPath> {
  const store = await cookies();
  const value = store.get(RETURN_COOKIE)?.value;
  store.delete({ name: RETURN_COOKIE, path: "/api/canva" });
  return isReturnPath(value) ? value : "/create";
}
