// Deck images are stored by the backend and referenced as "/media/<file>". The
// browser can't reach the backend directly, so they're served via /api/media.
const MEDIA_PATH = /^\/media\/([A-Za-z0-9_-]{1,128}\.(?:jpe?g|png|webp|gif))$/;

/**
 * Browser URL for an image in a deck or plan: backend media paths go through the
 * proxy, http(s) URLs pass through, anything else (data:, javascript:, …) is dropped.
 */
export function mediaUrl(url: string | null | undefined): string | null {
  if (!url) return null;
  const media = MEDIA_PATH.exec(url);
  if (media) return `/api/media/${media[1]}`;
  if (url.startsWith("/api/media/")) return url;
  return /^https?:\/\//i.test(url) ? url : null;
}

/** Absolute media URL (e.g. for @react-pdf, which can't resolve relative URLs). Client only. */
export function absoluteMediaUrl(url: string | null | undefined): string | null {
  const proxied = mediaUrl(url);
  if (!proxied) return null;
  return proxied.startsWith("/") ? `${window.location.origin}${proxied}` : proxied;
}
