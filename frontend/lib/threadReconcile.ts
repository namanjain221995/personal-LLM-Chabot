/**
 * Local copy vs server copy — the two questions a reload has to answer.
 *
 * 2026-09-10 (upload reliability). Both live here, in a module with no I/O
 * and no dependencies, because BOTH sides of the sync need them: the history
 * store re-applies a local-only tail after the server refuses a conditional
 * write, and the chat view folds server truth into what is on screen without
 * destroying work this tab is in the middle of. Keeping one definition is
 * what stops the store and the view from disagreeing about which turns are
 * "ours" — a disagreement that shows up as a duplicated answer or a
 * disappearing question.
 */

import type { ChatMessage } from './types';

/**
 * May this message be written to history AT ALL?
 *
 * RC-2: an interrupted answer was saved as an assistant row with `content:
 * ''` and `meta: {}` — no text, no error, no action. On reload it rendered as
 * an empty bubble, and because the thread then ended on an assistant message
 * the reload path stopped looking for the answer the orchestrator had
 * actually persisted. A row that says nothing at all is not a record of
 * anything; it is the absence of one, and it must not be stored.
 *
 * Deliberately narrow. Anything with text, anything still streaming, anything
 * carrying an error (the row IS the record of the failure — see
 * `meta.error`), and anything whose meta holds real content (data, a chart, a
 * clarification, video evidence…) is kept. Only "empty and silent" is
 * refused, and `branch` alone does not count: it is a position in the tree,
 * not something the message says.
 */
export function isPersistableMessage(m: ChatMessage): boolean {
  if (m.role !== 'assistant') return true;
  if (m.content.trim() !== '') return true;
  if (m.status === 'streaming') return true; // still being written
  if (m.status === 'error' || m.errorMessage) return true;
  if (!m.meta) return false;
  return Object.keys(m.meta).some((k) => k !== 'branch');
}

/** The identities a server copy already accounts for. */
function serverKeysOf(server: ChatMessage[]): {
  generations: Set<string>;
  intents: Set<string>;
} {
  const generations = new Set<string>();
  const intents = new Set<string>();
  for (const m of server) {
    const gen = m.meta?.generation_id;
    if (typeof gen === 'string') generations.add(gen);
    const intent = m.meta?.intent?.id;
    if (typeof intent === 'string') intents.add(intent);
  }
  return { generations, intents };
}

/**
 * The turns this browser holds that the server's copy does not — the ONLY
 * thing that may be re-applied after adopting server truth.
 *
 * Used on both recovery paths: a 409 `conversation changed` (the server
 * persisted an answer after we last loaded) and the reload reconciliation.
 * Identity, not position, decides: an answer the server persisted ITSELF
 * carries the same `generation_id` as the copy this tab streamed, and a turn
 * the server already stored carries the same `intent.id`. Matching on either
 * is what stops the recovery from appending a second copy of an answer that
 * is already there — the duplicate this whole exercise is about.
 */
export function localOnlyTail(
  local: ChatMessage[],
  server: ChatMessage[],
): ChatMessage[] {
  const { generations, intents } = serverKeysOf(server);
  const out: ChatMessage[] = [];
  for (let i = server.length; i < local.length; i += 1) {
    const m = local[i];
    const gen = m.meta?.generation_id;
    if (typeof gen === 'string' && generations.has(gen)) continue;
    const intent = m.meta?.intent?.id;
    if (typeof intent === 'string' && intents.has(intent)) continue;
    if (!isPersistableMessage(m)) continue;
    out.push(m);
  }
  return out;
}

/**
 * Server truth WITHOUT throwing away what only this tab knows — the reload
 * and poll path (fe-chat F1).
 *
 * The old code replaced the on-screen thread with the server's copy outright,
 * which renumbered every message to `srv-<conversation>-<i>`. That is what
 * killed the upload indicator (keyed on the message id) and made a turn whose
 * files were still uploading in THIS tab reappear as a bare, unsent-looking
 * message — with the composer still locked, and the request still to come.
 *
 * So: keep the client's object (and its id) wherever the server's row says
 * the same thing, adopt the server's row where it does not, and keep the
 * local-only tail on the end. A turn whose send is still in flight keeps its
 * own `meta.intent` and its own attachments' `upload_state`, because those
 * describe work happening HERE that the server cannot know about yet.
 */
export function reconcileThread(
  local: ChatMessage[],
  server: ChatMessage[],
): ChatMessage[] {
  const merged = server.map((s, i) => {
    const l = local[i];
    // The poll runs every 8 seconds and a cache hit hands back the very
    // objects the view already holds. Returning them unchanged is what keeps
    // the memoized rows from re-rendering the whole thread on a tick where
    // nothing moved (M-08).
    if (l === s) return l;
    if (!l || l.role !== s.role) return s;
    // The same words in the same place is the same turn. (An answer that
    // grew server-side is NOT the same turn, and the server's copy wins —
    // that is how the finished answer arrives.)
    if (l.content !== s.content) return s;
    const state = l.meta?.intent?.state;
    const inFlight =
      state === 'waiting_for_attachments' ||
      state === 'submitting' ||
      state === 'accepted' ||
      state === 'processing';
    return {
      ...s,
      // The client id is what the composer, the upload indicator and the
      // per-row callbacks are keyed on; renumbering it mid-upload is the bug.
      id: l.id,
      // Browser-only fields the server never had.
      ...(l.imageDataUrl !== undefined ? { imageDataUrl: l.imageDataUrl } : {}),
      ...(l.imageDataUrls !== undefined
        ? { imageDataUrls: l.imageDataUrls }
        : {}),
      ...(l.pdfName !== undefined ? { pdfName: l.pdfName } : {}),
      meta:
        inFlight && l.meta
          ? // This tab is mid-send: its account of the intent, and of where
            // each file's bytes are, is newer than anything stored.
            { ...s.meta, ...l.meta }
          : s.meta,
    } as ChatMessage;
  });
  return [...merged, ...localOnlyTail(local, server)];
}
