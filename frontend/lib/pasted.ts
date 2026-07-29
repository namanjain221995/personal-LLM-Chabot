/**
 * Pasted-content helpers (V5, 2026-07-23).
 *
 * When a user pastes a long block of text or code into the composer we don't
 * dump it into the textarea — we turn it into a compact "PASTED" attachment
 * chip (ChatGPT-style). The block is stored on the user message's `meta.pasted`
 * (so it round-trips through server history untouched) and folded back into the
 * text the model actually receives at request time.
 *
 * Pure module — no React, no DOM — so it is unit-testable in isolation.
 */

import type { PastedText } from './types';

/** A paste becomes a chip past either threshold — matches ChatGPT's feel. */
export const PASTE_MIN_CHARS = 1200;
export const PASTE_MIN_LINES = 12;

export function countLines(text: string): number {
  if (!text) return 0;
  return text.split('\n').length;
}

/** Should this pasted text become a chip instead of inline textarea content? */
export function shouldAttachPaste(text: string): boolean {
  if (!text) return false;
  return text.length >= PASTE_MIN_CHARS || countLines(text) >= PASTE_MIN_LINES;
}

export function makePastedText(content: string, id: string): PastedText {
  return { id, content, lines: countLines(content), chars: content.length };
}

/**
 * Combine a user message's pasted blocks with its typed text into the single
 * string the model sees. Blocks come first (context), then the instruction.
 * Empty/whitespace-only parts are dropped; code is preserved verbatim (no
 * fences added, so nothing corrupts the pasted content).
 */
export function foldModelContent(
  content: string,
  pasted?: PastedText[] | null,
): string {
  const parts = [...(pasted ?? []).map((p) => p.content), content].filter(
    (s) => s != null && s.trim().length > 0,
  );
  return parts.join('\n\n');
}

/** File extension for a pasted image blob, which carries no filename. */
export function imageExtFromMime(mime: string): string {
  const map: Record<string, string> = {
    'image/png': 'png',
    'image/jpeg': 'jpg',
    'image/jpg': 'jpg',
    'image/webp': 'webp',
    'image/gif': 'gif',
    'image/svg+xml': 'svg',
    'image/bmp': 'bmp',
    'image/avif': 'avif',
    'image/heic': 'heic',
    'image/tiff': 'tiff',
  };
  return map[mime.toLowerCase()] ?? mime.split('/')[1] ?? 'png';
}
