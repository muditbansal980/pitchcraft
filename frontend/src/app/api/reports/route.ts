import multer from "multer";

import { CloudinaryConfigError, deletePdf, uploadPdf, userReportsFolder } from "@/lib/cloudinary";
import { readSingleFile } from "@/lib/multer";
import { getSessionUser } from "@/lib/session";

const MAX_PDF_BYTES = 10 * 1024 * 1024;

class InvalidFileError extends Error {}

/** Upload a report PDF (multipart field `file`) to Cloudinary. Returns `{url, publicId}`. */
export async function POST(request: Request) {
  const user = await getSessionUser();
  if (!user) {
    return Response.json({ error: "Log in to save reports." }, { status: 401 });
  }

  let file: Express.Multer.File | undefined;
  try {
    file = await readSingleFile(request, "file", {
      limits: { fileSize: MAX_PDF_BYTES, files: 1, fields: 5 },
      fileFilter: (_req, upload, cb) =>
        upload.mimetype === "application/pdf"
          ? cb(null, true)
          : cb(new InvalidFileError("Only PDF files can be uploaded.")),
    });
  } catch (err) {
    if (err instanceof multer.MulterError) {
      const tooLarge = err.code === "LIMIT_FILE_SIZE";
      return Response.json(
        { error: tooLarge ? "The PDF is too large (max 10 MB)." : err.message },
        { status: tooLarge ? 413 : 400 },
      );
    }
    if (err instanceof InvalidFileError) {
      return Response.json({ error: err.message }, { status: 400 });
    }
    return Response.json({ error: "Could not read the upload." }, { status: 400 });
  }

  if (!file || file.buffer.subarray(0, 5).toString("latin1") !== "%PDF-") {
    return Response.json({ error: "Attach a PDF in the `file` field." }, { status: 400 });
  }

  const baseName =
    file.originalname
      .replace(/\.pdf$/i, "")
      .replace(/[^a-zA-Z0-9_-]+/g, "-")
      .replace(/^-+|-+$/g, "")
      .slice(0, 80) || "report";

  try {
    const result = await uploadPdf(file.buffer, userReportsFolder(user), baseName);
    return Response.json({ url: result.secure_url, publicId: result.public_id });
  } catch (err) {
    if (err instanceof CloudinaryConfigError) {
      return Response.json({ error: err.message }, { status: 500 });
    }
    console.error("Cloudinary upload failed", err);
    return Response.json({ error: "Saving the PDF failed. Please try again." }, { status: 502 });
  }
}

/** Delete one of the current user's report PDFs: body `{publicId}`. */
export async function DELETE(request: Request) {
  const user = await getSessionUser();
  if (!user) {
    return Response.json({ error: "Log in to manage reports." }, { status: 401 });
  }

  const body = await request.json().catch(() => null);
  const publicId = typeof body?.publicId === "string" ? body.publicId : "";
  if (!publicId.startsWith(`${userReportsFolder(user)}/`) || publicId.includes("..")) {
    return Response.json({ error: "Report not found." }, { status: 404 });
  }

  try {
    await deletePdf(publicId);
    return new Response(null, { status: 204 });
  } catch (err) {
    if (err instanceof CloudinaryConfigError) {
      return Response.json({ error: err.message }, { status: 500 });
    }
    console.error("Cloudinary delete failed", err);
    return Response.json({ error: "Deleting the PDF failed." }, { status: 502 });
  }
}
