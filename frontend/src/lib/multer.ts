import { Readable } from "node:stream";
import type { ReadableStream as NodeReadableStream } from "node:stream/web";

import type { RequestHandler } from "express";
import multer from "multer";

/**
 * Run multer on a Route Handler's Web `Request` and return the single file in `field`.
 *
 * Multer is Express middleware, so the request body is turned into a Node stream and
 * given the `headers`/`method` it reads. Files are kept in memory (`file.buffer`).
 * Rejects with a `MulterError` (e.g. `LIMIT_FILE_SIZE`) or the `fileFilter`'s error.
 */
export async function readSingleFile(
  request: Request,
  field: string,
  options: Omit<multer.Options, "storage" | "dest">,
): Promise<Express.Multer.File | undefined> {
  if (!request.body) return undefined;

  const headers = Object.fromEntries(request.headers);
  // Multer skips bodies with neither header (type-is `hasBody`), and Next can strip
  // `transfer-encoding` when proxying, so mark a length-less body as chunked.
  if (!headers["content-length"]) headers["transfer-encoding"] ??= "chunked";

  const req = Object.assign(Readable.fromWeb(request.body as NodeReadableStream), {
    headers,
    method: request.method,
    url: request.url,
  });
  const middleware = multer({ ...options, storage: multer.memoryStorage() }).single(field);
  type Args = Parameters<RequestHandler>;

  await new Promise<void>((resolve, reject) => {
    middleware(req as unknown as Args[0], {} as Args[1], (err?: unknown) =>
      err ? reject(err) : resolve(),
    );
  });
  return (req as { file?: Express.Multer.File }).file;
}
