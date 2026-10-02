/**
 * Client-side image downscaling before an upload becomes base64 (2026-08-29).
 *
 * Why: the composer used to send screenshots at full resolution. The main
 * model's image tokens grow with pixel count — measured on the served model's
 * /tokenize endpoint: 1,013 tokens at 1280x800, 1,413 at 1600x900, 3,613 at
 * 2560x1440, 8,173 at 3840x2160. Capping the long edge at MAX_IMAGE_EDGE
 * keeps text crisp (PNG stays PNG) and cuts a 1440p screenshot 2.6x and a 4K
 * one 5.8x, which shortens the time to the first token.
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

/**
 * PNG for anything with sharp edges or transparency (screenshots, diagrams,
 * UI assets); JPEG only when the source already was a lossy photo.
 *
 * WebP deliberately stays on the PNG path even though it is often lossy: it
 * is also routinely lossless and carries an alpha channel, and
 * `canvas.toDataURL('image/jpeg')` composites transparency onto black, which
 * would turn a transparent diagram into black-on-black.
 */
export function outputMime(sourceMime: string): 'image/png' | 'image/jpeg' {
  const m = (sourceMime || '').toLowerCase();
  return m === 'image/jpeg' || m === 'image/jpg' ? 'image/jpeg' : 'image/png';
}

export interface DownscaledImage {
  dataUrl: string;
  width: number;
  height: number;
  mime: 'image/png' | 'image/jpeg';
}

/**
 * How many photos are decoded at once (2026-10-03). A message may carry any
 * number of photos now (LIMITS.md), and a decoded 48 MP phone photo is about
 * 190 MB of pixels: a hundred picked together and decoded in parallel would
 * take the tab down. The rest wait their turn; each chip still lands as soon
 * as its own photo is done.
 */
export const MAX_PARALLEL_DECODES = 3;
let decoding = 0;
const decodeQueue: Array<() => void> = [];

async function withDecodeSlot<T>(run: () => Promise<T>): Promise<T> {
  // A freed slot is handed straight to the next in line, so a newcomer can
  // never slip in between and make it MAX_PARALLEL_DECODES + 1.
  if (decoding < MAX_PARALLEL_DECODES) decoding += 1;
  else await new Promise<void>((resolve) => decodeQueue.push(resolve));
  try {
    return await run();
  } finally {
    const next = decodeQueue.shift();
    if (next) next();
    else decoding -= 1;
  }
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
  return withDecodeSlot(() => downscaleNow(file, maxEdge));
}

async function downscaleNow(file: File, maxEdge: number): Promise<DownscaledImage | null> {
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

/**
 * How many bytes a base64 data URL decodes to, without decoding it. The
 * composer's photo size rule is measured on this (2026-10-03): what is sent,
 * not what was picked.
 */
export function dataUrlByteLength(dataUrl: string): number {
  const b64 = dataUrl.slice(dataUrl.indexOf(',') + 1);
  const padding = b64.endsWith('==') ? 2 : b64.endsWith('=') ? 1 : 0;
  return Math.max(0, Math.floor((b64.length * 3) / 4) - padding);
}
