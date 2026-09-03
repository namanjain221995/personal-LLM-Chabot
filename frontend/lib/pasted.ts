/**
 * Pasted-content helpers.
 *
 * HISTORY. Between V5 (2026-07-23) and 2026-09-04 a long paste was swallowed
 * out of the composer into a compact "PASTED" attachment chip. That is gone:
 * text pasted into the composer now goes into the textarea, at any length
 * (see components/Composer.tsx handlePaste). What remains here is the read
 * side, which must keep working forever — turns SENT under the old behaviour
 * carry their blocks on `meta.pasted`, so they still render as chips
 * (MessageRow) and are still folded into the model-visible text when such a
 * turn is resent or edited.
 *
 * Pure module — no React, no DOM — so it is unit-testable in isolation.
 */

import type { PastedText } from './types';

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
