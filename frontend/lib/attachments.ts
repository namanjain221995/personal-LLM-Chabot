/**
 * In-memory store of the attachment that was sent with a user turn.
 *
 * Regenerate/retry re-send an earlier turn, but the message we persist keeps
 * only a preview (`imageDataUrl`) or a filename (`pdfName`) — never the raw
 * payload. Re-sending without it silently changed the question: the model was
 * asked to re-answer "what's in this invoice?" with no invoice attached.
 *
 * The payload deliberately does NOT go into localStorage. A 25 MB PDF as
 * base64 would blow the quota, and quota eviction is exactly what caused the
 * conversation-destroying bug this codebase already had to fix. Holding it in
 * memory means a regenerate works for the lifetime of the tab, and after a
 * reload the user is told to re-attach rather than being handed a silently
 * different answer.
 */

export interface SentAttachment {
  kind: 'image' | 'pdf';
  name: string;
  /** Raw base64, no data: prefix — what POST /chat expects. */
  base64: string;
}

const sent = new Map<string, SentAttachment[]>();

/** Remember what was attached to the user message with this id —
    up to 5 images or a single PDF (2026-08-05 multi-upload). */
export function rememberAttachments(
  messageId: string,
  attachments: SentAttachment[],
): void {
  if (attachments.length) sent.set(messageId, attachments);
}

/** Strip a `data:...;base64,` prefix, returning the raw payload. */
export function base64FromDataUrl(dataUrl?: string | null): string | null {
  if (!dataUrl) return null;
  const comma = dataUrl.indexOf(',');
  if (!dataUrl.startsWith('data:') || comma === -1) return null;
  const payload = dataUrl.slice(comma + 1);
  return payload || null;
}

export interface AttachmentsLookup {
  /** The attachments to re-send, when we still have them (may be empty). */
  attachments: SentAttachment[];
  /** True when the turn HAD attachments we can no longer reconstruct. */
  missing: boolean;
}

/**
 * Recover the attachments for a user turn being re-sent.
 *
 * Images survive a reload: the persisted `imageDataUrls` (or the legacy
 * single `imageDataUrl`) ARE the payloads. PDFs do not — only the filename
 * is kept — so after a reload they report `missing`, and the caller must ask
 * the user to re-attach instead of quietly sending a text-only prompt.
 */
export function attachmentsForResend(message: {
  id: string;
  imageDataUrl?: string;
  imageDataUrls?: string[];
  pdfName?: string;
}): AttachmentsLookup {
  const remembered = sent.get(message.id);
  if (remembered) return { attachments: remembered, missing: false };

  const previews = message.imageDataUrls?.length
    ? message.imageDataUrls
    : message.imageDataUrl
      ? [message.imageDataUrl]
      : [];
  const fromPreviews = previews
    .map((p) => base64FromDataUrl(p))
    .filter((b): b is string => Boolean(b))
    .map((base64) => ({ kind: 'image' as const, name: 'image', base64 }));
  if (fromPreviews.length === previews.length && fromPreviews.length > 0) {
    return { attachments: fromPreviews, missing: false };
  }
  return {
    attachments: [],
    missing: Boolean(
      message.pdfName || message.imageDataUrl || message.imageDataUrls?.length,
    ),
  };
}

/** Test seam. */
export function clearAttachments(): void {
  sent.clear();
}
