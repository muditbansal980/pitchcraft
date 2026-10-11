import { fetchBackendPublic } from "@/lib/backend";

// Deck images are saved by the backend as /media/<sha1>.<ext> and never change.
const MEDIA_FILE = /^[A-Za-z0-9_-]{1,128}\.(?:jpe?g|png|webp|gif)$/;
const MAX_IMAGE_BYTES = 15 * 1024 * 1024;

/** Proxy a deck image from the backend's public /media mount. */
export async function GET(_request: Request, ctx: RouteContext<"/api/media/[file]">) {
  const { file } = await ctx.params;
  if (!MEDIA_FILE.test(file)) {
    return Response.json({ error: "Not found" }, { status: 404 });
  }

  const res = await fetchBackendPublic(`/media/${file}`, 20_000);
  if (!res) return Response.json({ error: "Backend is not reachable." }, { status: 502 });
  if (!res.ok) return Response.json({ error: "Not found" }, { status: res.status === 404 ? 404 : 502 });

  const type = res.headers.get("content-type") ?? "";
  const length = Number(res.headers.get("content-length"));
  if (!type.startsWith("image/") || length > MAX_IMAGE_BYTES) {
    await res.body?.cancel();
    return Response.json({ error: "Not an image" }, { status: 502 });
  }

  const headers = new Headers({
    "Content-Type": type,
    "Cache-Control": "public, max-age=31536000, immutable",
    "X-Content-Type-Options": "nosniff",
  });
  if (length) headers.set("Content-Length", String(length));
  return new Response(res.body, { status: 200, headers });
}
