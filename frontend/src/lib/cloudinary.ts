import { v2 as cloudinary, type UploadApiResponse } from "cloudinary";

import type { SessionUser } from "@/lib/session";

// Server-only. Reads CLOUDINARY_CLOUD_NAME / CLOUDINARY_API_KEY / CLOUDINARY_API_SECRET.
const REPORTS_FOLDER = "pitchcraft/reports";

export class CloudinaryConfigError extends Error {}

function configure() {
  const { CLOUDINARY_CLOUD_NAME, CLOUDINARY_API_KEY, CLOUDINARY_API_SECRET } = process.env;
  if (!CLOUDINARY_CLOUD_NAME || !CLOUDINARY_API_KEY || !CLOUDINARY_API_SECRET) {
    throw new CloudinaryConfigError("Cloudinary is not configured on the server.");
  }
  cloudinary.config({
    cloud_name: CLOUDINARY_CLOUD_NAME,
    api_key: CLOUDINARY_API_KEY,
    api_secret: CLOUDINARY_API_SECRET,
    secure: true,
  });
}

/** Each user's reports live under their own folder, so ownership can be checked from the public id. */
export function userReportsFolder(user: SessionUser): string {
  const key = String(user.id ?? user.username).replace(/[^a-zA-Z0-9_-]/g, "_");
  return `${REPORTS_FOLDER}/${key}`;
}

/**
 * Upload a PDF as a "raw" asset. Raw delivery isn't affected by Cloudinary's default
 * block on serving PDFs as images, and the public id keeps the `.pdf` extension.
 */
export async function uploadPdf(buffer: Buffer, folder: string, baseName: string): Promise<UploadApiResponse> {
  configure();
  const suffix = crypto.randomUUID().slice(0, 8);
  return new Promise((resolve, reject) => {
    cloudinary.uploader
      .upload_stream(
        {
          resource_type: "raw",
          public_id: `${folder}/${baseName}-${suffix}.pdf`,
          asset_folder: folder,
          overwrite: false,
        },
        (error, result) => (error || !result ? reject(error ?? new Error("Empty upload result")) : resolve(result)),
      )
      .end(buffer);
  });
}

export async function deletePdf(publicId: string): Promise<void> {
  configure();
  await cloudinary.uploader.destroy(publicId, { resource_type: "raw", invalidate: true });
}
