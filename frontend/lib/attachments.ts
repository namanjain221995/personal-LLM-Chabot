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

const sent = new Map<string, SentAttachment>();

/** Remember what was attached to the user message with this id. */
export function rememberAttachment(
  messageId: string,
  attachment: SentAttachment,
): void {
  sent.set(messageId, attachment);
}

/** Strip a `data:...;base64,` prefix, returning the raw payload. */
export function base64FromDataUrl(dataUrl?: string | null): string | null {
  if (!dataUrl) return null;
  const comma = dataUrl.indexOf(',');
  if (!dataUrl.startsWith('data:') || comma === -1) return null;
  const payload = dataUrl.slice(comma + 1);
  return payload || null;
}

export interface AttachmentLookup {
  /** The attachment to re-send, when we still have it. */
  attachment: SentAttachment | null;
  /** True when the turn HAD an attachment we can no longer reconstruct. */
  missing: boolean;
}

/**
 * Recover the attachment for a user turn being re-sent.
 *
 * Images survive a reload: the persisted `imageDataUrl` IS the payload.
 * PDFs do not — only the filename is kept — so after a reload they report
 * `missing`, and the caller must ask the user to re-attach instead of
 * quietly sending a text-only prompt.
 */
export function attachmentForResend(message: {
  id: string;
  imageDataUrl?: string;
  pdfName?: string;
}): AttachmentLookup {
  const remembered = sent.get(message.id);
  if (remembered) return { attachment: remembered, missing: false };

  const fromPreview = base64FromDataUrl(message.imageDataUrl);
  if (fromPreview) {
    return {
      attachment: { kind: 'image', name: 'image', base64: fromPreview },
      missing: false,
    };
  }
  return { attachment: null, missing: Boolean(message.pdfName || message.imageDataUrl) };
}

/** Test seam. */
export function clearAttachments(): void {
  sent.clear();
}
