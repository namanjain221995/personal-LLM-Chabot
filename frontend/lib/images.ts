/**
 * Client-side image downscaling before an upload becomes base64 (2026-08-29).
 *
 * Why: the composer used to send screenshots at full resolution. The main
 * model's image tokens grow with pixel count — a 1280x800 screenshot is
 * ~1,000 tokens, a 2560x1440 one ~4,000 — while measured answer accuracy
 * plateaus at ~1280 px on the long edge. Capping the long edge at
 * MAX_IMAGE_EDGE keeps the text crisp (PNG stays PNG), cuts prompt tokens and
 * upload size up to 4x, and shortens the time to the first token.
 *
 * The pure helpers are unit-tested; `downscaleImageFile` needs a browser
 * (createImageBitmap + canvas) and falls back to `null`, meaning "send the
 * original", on any failure so an upload can never be lost to this step.
 */

export const MAX_IMAGE_EDGE = 1600;
/** JPEG quality for photos; screenshots/PNGs are re-encoded losslessly. */
export const JPEG_QUALITY = 0.92;

export interface FitResult {
  width: number;
  height: number;
  scaled: boolean;
}

/** Largest size with the same aspect ratio whose long edge is <= maxEdge. */
export function fitWithin(
  width: number,
  height: number,
  maxEdge: number = MAX_IMAGE_EDGE,
): FitResult {
  const long = Math.max(width, height);
  if (!(width > 0 && height > 0) || long <= maxEdge) {
    return { width, height, scaled: false };
  }
  const ratio = maxEdge / long;
  return {
    width: Math.max(1, Math.round(width * ratio)),
    height: Math.max(1, Math.round(height * ratio)),
    scaled: true,
  };
}

/** PNG for anything with sharp edges (screenshots, diagrams, transparency);
 *  JPEG only when the source already was a lossy photo format. */
export function outputMime(sourceMime: string): 'image/png' | 'image/jpeg' {
  const m = (sourceMime || '').toLowerCase();
  return m === 'image/jpeg' || m === 'image/jpg' || m === 'image/webp'
    ? 'image/jpeg'
    : 'image/png';
}

export interface DownscaledImage {
  dataUrl: string;
  width: number;
  height: number;
  mime: 'image/png' | 'image/jpeg';
}

/**
 * Downscale `file` so its long edge is <= maxEdge. Resolves to `null` when the
 * image already fits, when the browser lacks the APIs, or on any error — the
 * caller then sends the original bytes exactly as before.
 */
export async function downscaleImageFile(
  file: File,
  maxEdge: number = MAX_IMAGE_EDGE,
): Promise<DownscaledImage | null> {
  if (typeof createImageBitmap !== 'function' || typeof document === 'undefined') {
    return null;
  }
  let bitmap: ImageBitmap | null = null;
  try {
    bitmap = await createImageBitmap(file);
    const fit = fitWithin(bitmap.width, bitmap.height, maxEdge);
    if (!fit.scaled) return null;
    const canvas = document.createElement('canvas');
    canvas.width = fit.width;
    canvas.height = fit.height;
    const ctx = canvas.getContext('2d');
    if (!ctx) return null;
    ctx.imageSmoothingEnabled = true;
    ctx.imageSmoothingQuality = 'high';
    ctx.drawImage(bitmap, 0, 0, fit.width, fit.height);
    const mime = outputMime(file.type);
    const dataUrl = canvas.toDataURL(mime, mime === 'image/jpeg' ? JPEG_QUALITY : undefined);
    if (!dataUrl.startsWith(`data:${mime};base64,`)) return null;
    return { dataUrl, width: fit.width, height: fit.height, mime };
  } catch {
    return null;
  } finally {
    bitmap?.close?.();
  }
}
