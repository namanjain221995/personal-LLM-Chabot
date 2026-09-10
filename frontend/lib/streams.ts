/**
 * Per-conversation stream manager — ChatGPT-style background generation.
 *
 * Streams used to live inside ChatApp, tied to the open view: switching chats
 * or starting a new one aborted the model mid-answer. This module owns every
 * live stream keyed by conversation id, so:
 *
 * - switching to another chat (or a new chat) leaves the generation running;
 *   the sidebar shows a spinner next to the busy conversation,
 * - coming back re-attaches the view to the live, partially-built answer,
 * - a full page reload re-joins the server-side generation via
 *   GET /api/chat/attach/{id} (the orchestrator keeps generating regardless —
 *   see LiveGeneration in the orchestrator),
 * - Stop explicitly cancels server-side via POST /api/chat/stop — closing the
 *   fetch alone no longer stops the model.
 *
 * Finished streams persist their messages through the history store no matter
 * which conversation is on screen.
 */

import { handleSessionEnd } from './auth';
import { branchForAppend, branchOf, metaWithBranch } from './branching';
import type { ClarificationResponse } from './clarification';
import { getHistoryStore, newId } from './history';
import { foldTurnForModel } from './selectedContext';
import type { ChatPrefs } from './prefs';
import { toClientError } from './errorTypes';
import { foldStreamState, mergeStep, readChatStream } from './sse';
import type {
  BranchMeta,
  ChatMessage,
  PersistedError,
  SendIntent,
  SendIntentState,
} from './types';

export type StreamStatus =
  | 'streaming'
  | 'done'
  | 'stopped'
  | 'error'
  | 'unreachable';

/**
 * What this tab knows about a stream it LOST — the "connection knowledge"
 * half of the contract (docs/upload-reliability/CONTRACT.md).
 *
 * Kept apart from `status` on purpose. `status` describes the generation;
 * this describes our ability to find out about it. "I could not ask" is not
 * "there is nothing there", and rendering the two the same way is how a
 * running answer came to be labelled "never sent".
 */
export interface ReconnectView {
  /** How many times the reconnect loop has asked so far. */
  attempt: number;
  /** The last ask failed (transport, proxy, or an old backend). */
  statusUnknown: boolean;
  /** The loop has given up; only an explicit Retry restarts it. */
  exhausted: boolean;
}

export interface LiveStreamView {
  conversationId: string;
  messages: ChatMessage[];
  status: StreamStatus;
  /** Present only while a lost stream is being re-established. */
  reconnect?: ReconnectView;
}

interface LiveStream extends LiveStreamView {
  controller: AbortController;
  assistantId: string;
  reasoningStartedAt: number | null;
  /** First research event's timestamp — drives the panel's elapsed clock. */
  researchStartedAt: number | null;
  reasoningSeconds?: number;
  sawToken: boolean;
  /** The send this stream serves (V29) — see StartStreamOptions.intentId. */
  intentId?: string;
  /** The user turn carrying `meta.intent`, so its state can be kept true. */
  intentMessageId?: string;
  /** The server's generation, from the FIRST meta event. */
  generationId?: string;
  attempt?: number;
}

const streams = new Map<string, LiveStream>();
const listeners = new Set<(conversationId: string) => void>();

export function subscribeStreams(
  fn: (conversationId: string) => void,
): () => void {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

function notify(id: string): void {
  for (const fn of [...listeners]) fn(id);
}

/**
 * NEW-24 — one visual commit per DISPLAY FRAME, not one per token.
 *
 * The tokens are not touched, delayed or replayed: `s.messages` is updated
 * synchronously by `updateAssistant` exactly as before, and is complete and
 * correct at every instant. What is coalesced is only the NOTIFICATION — how
 * often the view is told to re-read state it can only paint 60 times a second
 * anyway.
 *
 * Why this is needed even though React 19 already batches: React batches
 * within one task, and every `reader.read()` resolution is its OWN task. A
 * generation running at 60-100 tok/s therefore produced 60-100 separate React
 * commits per second, each re-rendering the whole thread and forcing a
 * synchronous layout for the auto-scroll. The browser never got a frame in
 * which to paint, which is exactly what "jerky" looks like: work is thrown
 * away half-done and the next paint jumps several tokens ahead.
 *
 * Because nothing is buffered there is no drain to get wrong. Every terminal
 * path notifies through `notifyNow`, so no token can be left pending — see
 * finalize / markUnreachable / markInterrupted.
 */
const frames = new Map<string, number>();

/** Frame-coalesced notify. Direct notify off-browser (SSR, node tests). */
function notifyFrame(s: LiveStream): void {
  const id = s.conversationId;
  if (typeof requestAnimationFrame !== 'function') {
    notify(id);
    return;
  }
  if (frames.has(id)) return; // a commit is already booked for this frame
  frames.set(
    id,
    requestAnimationFrame(() => {
      frames.delete(id);
      // M-10 ownership: the conversation may have been restarted (or replaced
      // by a newer stream) since this frame was booked. A callback may only
      // ever speak for the stream that scheduled it.
      if (streams.get(id) !== s) return;
      notify(id);
    }),
  );
}

/**
 * Terminal notify: drop the commit booked for this conversation, then deliver
 * synchronously.
 *
 * Both halves matter. Delivering at once is what makes the final state — a
 * `done`, an error, a Stop — land without waiting on a frame; cancelling is
 * what stops the frame that WAS booked from repainting a now-finished stream
 * afterwards.
 */
function notifyNow(id: string): void {
  const handle = frames.get(id);
  if (handle !== undefined) {
    frames.delete(id);
    if (typeof cancelAnimationFrame === 'function') cancelAnimationFrame(handle);
  }
  notify(id);
}

export function getLiveStream(
  id: string | null | undefined,
): LiveStreamView | null {
  return (id && streams.get(id)) || null;
}

export function isStreaming(id: string | null | undefined): boolean {
  return !!id && streams.get(id)?.status === 'streaming';
}

/** Conversations with a stream running in THIS tab. */
export function streamingIds(): string[] {
  return [...streams.values()]
    .filter((s) => s.status === 'streaming')
    .map((s) => s.conversationId);
}

/** Stop a generation: abort the local reader AND cancel it server-side. */
export function stopStream(id: string | null | undefined): void {
  if (!id) return;
  const s = streams.get(id);
  if (s?.status === 'streaming') s.controller.abort();
  // Server-side generations are detached — tell the orchestrator explicitly.
  void fetch('/api/chat/stop', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ conversation_id: id, session_id: id }),
  }).catch(() => undefined);
}

/**
 * The status question could not be ASKED.
 *
 * fe-chat F4 / INF-2: `fetchServerActive` used to answer `[]` for a 503, a
 * dead network and a healthy idle server alike, and every caller read that
 * one answer as "nothing is running anywhere". On a reload during the
 * orchestrator's recreate window that skipped the re-attach, unlocked the
 * composer over a live generation, and — with a turn still marked
 * `send_state` — put the red "never sent" notice on a question that was
 * being answered. So "could not ask" is now a rejection, and every caller
 * has to decide what to do about it.
 */
export class ServerStatusUnavailable extends Error {
  constructor(public status: number | null) {
    super('Could not ask the server what is running.');
    this.name = 'ServerStatusUnavailable';
  }
}

/**
 * Conversations the SERVER is still generating for (survives reloads).
 *
 * Resolves ONLY when the server actually answered. Rejects with
 * `ServerStatusUnavailable` when it could not be reached — see above.
 */
export async function fetchServerActive(): Promise<string[]> {
  let res: Response;
  try {
    res = await fetch('/api/chat/active');
  } catch {
    throw new ServerStatusUnavailable(null);
  }
  if (res.status === 401) {
    // This is the app's heartbeat (ChatApp polls it every 8s), so it is
    // where a mid-session sign-out surfaces first. A 401 here is session
    // death, not "nothing active" — route to sign-in instead of letting
    // the app degrade feature by feature.
    void handleSessionEnd();
    return [];
  }
  if (!res.ok) throw new ServerStatusUnavailable(res.status);
  let data: { active?: unknown };
  try {
    data = (await res.json()) as { active?: unknown };
  } catch {
    // A 200 with a body we cannot read is not an answer about anything.
    throw new ServerStatusUnavailable(res.status);
  }
  // A successful answer that lists nothing IS an answer: nothing is running.
  return Array.isArray(data.active)
    ? data.active.filter((x): x is string => typeof x === 'string')
    : [];
}

/**
 * What the server says about one send intent — GET /chat/requests/{id}.
 *
 * Four outcomes, and the difference between the last three is the whole
 * point. `unknown-intent` is the server STATING that this send never reached
 * it (the only thing that may become "never sent"); `unavailable` is any
 * failure to find out, INCLUDING the 404 a backend without this route
 * returns, which must read as "checking", never as "never sent"
 * (CONTRACT.md, Compatibility).
 */
export type ChatRequestReport =
  | {
      kind: 'known';
      status: string;
      generationId: string | null;
      attempt: number;
      resumable: boolean;
      answerPersisted: boolean;
      live: boolean;
    }
  | { kind: 'unknown-intent' }
  | { kind: 'unavailable'; status: number | null }
  | { kind: 'unauthenticated' };

export async function fetchChatRequest(
  intentId: string,
): Promise<ChatRequestReport> {
  let res: Response;
  try {
    res = await fetch(`/api/chat/requests/${encodeURIComponent(intentId)}`, {
      cache: 'no-store',
    });
  } catch {
    return { kind: 'unavailable', status: null };
  }
  if (res.status === 401) return { kind: 'unauthenticated' };
  let body: Record<string, unknown> | null = null;
  try {
    body = (await res.json()) as Record<string, unknown>;
  } catch {
    body = null;
  }
  if (res.status === 404) {
    // The orchestrator's own refusal says `unknown intent`; FastAPI's
    // missing-route 404 says `Not Found`. Only the first is an answer about
    // the intent — the second means this backend predates the route.
    return body && body.detail === 'unknown intent'
      ? { kind: 'unknown-intent' }
      : { kind: 'unavailable', status: 404 };
  }
  if (!res.ok || !body || typeof body.status !== 'string') {
    return { kind: 'unavailable', status: res.status };
  }
  return {
    kind: 'known',
    status: body.status,
    generationId:
      typeof body.generation_id === 'string' ? body.generation_id : null,
    attempt: typeof body.attempt === 'number' ? body.attempt : 1,
    resumable: body.resumable === true,
    answerPersisted: body.answer_persisted === true,
    live: body.live === true,
  };
}

/** Attach base: the turns up to (and including) the last user message —
 * the server replay rebuilds the assistant answer from scratch. */
export function attachBaseTurns(messages: ChatMessage[]): ChatMessage[] {
  let end = messages.length;
  while (end > 0 && messages[end - 1].role !== 'user') end -= 1;
  return messages.slice(0, end);
}

/**
 * How many messages regenerating `messageId` would throw away.
 *
 * Regenerate restarts from the user turn that produced the target answer, so
 * everything after the target is discarded. On the LAST answer that is just
 * the answer itself (0 extra) — the expected behavior. Deeper in the thread
 * it silently destroys every later turn, which needs confirmation first.
 */
export function messagesDiscardedByRegenerate(
  messages: ChatMessage[],
  messageId: string,
): number {
  const idx = messages.findIndex((m) => m.id === messageId);
  if (idx === -1) return 0;
  return messages.length - idx - 1;
}

/**
 * Patch the streaming answer — M-09.
 *
 * This ran `s.messages.map()` per token: a closure call and an id comparison
 * for every historical message in the conversation, hundreds of times per
 * answer, to change exactly one element that is almost always the last one.
 *
 * The placeholder is APPENDED by `register` and nothing inserts after it, so
 * the tail is the answer by construction; the `findIndex` behind it is the
 * honest fallback rather than an assumption. Immutability is unchanged — a
 * new array and a new object for the message that changed — and every
 * historical message keeps its exact object identity, which is what lets the
 * memoized rows in the view skip re-rendering (M-08).
 */
function updateAssistant(
  s: LiveStream,
  patch: Partial<ChatMessage> | ((m: ChatMessage) => ChatMessage),
): void {
  const tail = s.messages.length - 1;
  const at =
    tail >= 0 && s.messages[tail].id === s.assistantId
      ? tail
      : s.messages.findIndex((m) => m.id === s.assistantId);
  if (at === -1) return;
  const current = s.messages[at];
  const next =
    typeof patch === 'function' ? patch(current) : { ...current, ...patch };
  if (next === current) return;
  const messages = s.messages.slice();
  messages[at] = next;
  s.messages = messages;
}

/**
 * Move the send intent on the USER turn this stream serves (V29).
 *
 * The intent is the durable account of one logical send: it is what a
 * reloaded tab reconciles against the server, and what stops a retry from
 * becoming a second generation. It lives on the message rather than in this
 * module's memory precisely because this module's memory does not survive the
 * thing it is describing — a reload, a crash, a closed tab.
 *
 * `persist` is deliberately explicit per call site. Every transition matters
 * on screen, but only the ones a reload must find (accepted, interrupted,
 * failed, unsent) are worth a write; `processing` is a live label, and
 * `completed` rides out with the answer's own save.
 */
function patchIntent(
  s: LiveStream,
  patch: Partial<SendIntent> & { state: SendIntentState },
  options?: { persist?: boolean },
): void {
  const targetId = s.intentMessageId;
  if (!targetId) return;
  const at = s.messages.findIndex((m) => m.id === targetId);
  if (at === -1) return;
  const current = s.messages[at];
  const existing = current.meta?.intent;
  const id = patch.id ?? existing?.id ?? s.intentId;
  if (!id) return;
  const next: SendIntent = { ...existing, ...patch, id };
  if (
    existing &&
    existing.state === next.state &&
    existing.generation_id === next.generation_id &&
    existing.attempt === next.attempt &&
    existing.reason === next.reason
  ) {
    return;
  }
  const messages = s.messages.slice();
  messages[at] = { ...current, meta: { ...(current.meta ?? {}), intent: next } };
  s.messages = messages;
  if (options?.persist) {
    // The assistant placeholder is NOT part of what gets stored here: an
    // answer that has not been written yet is not a record of anything
    // (RC-2). Only the question and its intent are durable at this point.
    getHistoryStore().saveMessages(
      s.conversationId,
      s.messages.filter((m) => m.id !== s.assistantId),
    );
  }
}

/** Client-measured thinking time: first reasoning delta → first token. */
function settleReasoningClock(s: LiveStream): void {
  if (s.reasoningStartedAt !== null && s.reasoningSeconds === undefined) {
    s.reasoningSeconds = Math.max(
      1,
      Math.round((Date.now() - s.reasoningStartedAt) / 1000),
    );
    updateAssistant(s, { reasoningSeconds: s.reasoningSeconds });
  }
}

/**
 * Terminal states retire the live progress line and phase marker: both are
 * persisted with the message, and a saved "Searching the web…" made a
 * reloaded or errored answer tick a fake clock forever (review 2026-08-30).
 */
export function withLiveProgressRetired(
  patch: Partial<ChatMessage>,
): Partial<ChatMessage> {
  return { searchStatus: undefined, phaseStatus: undefined, ...patch };
}

/**
 * Put the failure ON the answer, where it survives being saved.
 *
 * RC-2: `status` and `errorMessage` are fields of the in-memory message
 * object, and the history wire shape is `{role, content, meta}` — so an
 * interrupted or failed answer was stored as `content: ''` with nothing
 * else, and came back after a reload as an empty bubble with no error and no
 * action. `meta.error` is the same information in the one place that
 * round-trips (the server stores meta as opaque JSON), so the reloaded row
 * can say what happened and offer the way out.
 */
function markPersistedError(s: LiveStream, error: PersistedError): void {
  updateAssistant(s, (m) => ({
    ...m,
    meta: { ...(m.meta ?? {}), error },
  }));
}

function finalize(s: LiveStream, patch: Partial<ChatMessage>): void {
  updateAssistant(s, withLiveProgressRetired(patch));
  s.status = (patch.status as StreamStatus) ?? 'done';
  // A stream that reaches an end is not reconnecting any more, whatever the
  // end was.
  s.reconnect = undefined;
  // The send is over: `completed` once the answer exists, `failed` with the
  // reason when the stream itself said no. Both ride out on the save below,
  // so the reload after them finds a turn that agrees with its answer.
  if (patch.status === 'error') {
    patchIntent(s, {
      state: 'failed',
      reason: typeof patch.errorMessage === 'string' ? patch.errorMessage : undefined,
    });
    // An answer that ends in an error is still a record of the failure, so
    // it is stored — but only when there is something to store. An empty
    // error-less row is exactly the blank bubble of RC-2.
    markPersistedError(s, {
      message:
        typeof patch.errorMessage === 'string'
          ? patch.errorMessage
          : 'This answer failed.',
      resumable: false,
    });
  } else if (patch.status === 'done') {
    patchIntent(s, { state: 'completed' });
  } else if (patch.status === 'stopped') {
    patchIntent(s, { state: 'cancelled' });
  }
  // Persist regardless of which conversation is on screen.
  getHistoryStore().saveMessages(s.conversationId, s.messages);
  // Terminal: any commit booked for this frame is dropped and the FINAL
  // state — every token included, `s.messages` was never behind — is
  // delivered synchronously. Nothing can repaint after this.
  notifyNow(s.conversationId);
}

/**
 * A fatal request-level failure: the send never became a stream.
 *
 * Record it ON the message and PERSIST it. This dropped the placeholder once
 * and relied on a banner that only renders for the chat currently on screen —
 * so a send that failed while the user was in another chat left no trace at
 * all: no answer, no error, nothing in history.
 *
 * `errorStatus`/`errorCode` are what the error page renders. `errorMessage`
 * holds the SAFE public sentence, never the upstream body: it is persisted to
 * history and exported to Markdown, so anything put here outlives the request.
 */
function markUnreachable(
  s: LiveStream,
  status: number | null,
  code?: unknown,
): void {
  const err = toClientError(status, code);
  if (err.code === 'UNAUTHENTICATED') {
    // Session death is not a message. Persisting the usual error bubble
    // would write "Something went wrong" turns into the user's stored
    // thread for something no retry can fix — drop the placeholder and
    // route to sign-in instead.
    streams.delete(s.conversationId);
    notifyNow(s.conversationId);
    void handleSessionEnd();
    return;
  }
  updateAssistant(
    s,
    withLiveProgressRetired({
      status: 'error',
      errorMessage: err.message,
      errorStatus: err.status,
      errorCode: err.code,
    }),
  );
  // Persisted alongside the browser-only fields above, so a reload shows the
  // failure and its Retry rather than an empty answer.
  markPersistedError(s, {
    message: err.message,
    code: err.code,
    status: err.status,
    resumable: false,
  });
  s.status = 'unreachable';
  s.reconnect = undefined;
  // The request never reached the server, so the send did not happen. Said on
  // the TURN, where a reload can still see it: `unsent` (not `failed`) is
  // what makes Send now offer to send the same intent again rather than
  // start a second one.
  patchIntent(s, { state: 'unsent', reason: err.message });
  getHistoryStore().saveMessages(s.conversationId, s.messages);
  notifyNow(s.conversationId);
}

/**
 * The stream died AFTER the orchestrator accepted the request. That is a very
 * different story from "unreachable": the generation is DETACHED server-side
 * (LiveGeneration), so it is still running, and re-sending would start a
 * SECOND one rather than recover this one. Reopening the conversation re-joins
 * it through GET /api/chat/attach/{id}. Whatever already streamed stays on the
 * message — this patches status/errorMessage and leaves `content` alone.
 */
const INTERRUPTED_MESSAGE =
  'The connection to this answer was interrupted. The model is still working on it — reconnecting…';

function markInterrupted(s: LiveStream): void {
  updateAssistant(
    s,
    withLiveProgressRetired({
      status: 'error',
      errorMessage: INTERRUPTED_MESSAGE,
    }),
  );
  markPersistedError(s, {
    message: INTERRUPTED_MESSAGE,
    // The server owns the request and can resume it — which is why nothing
    // here re-sends, and why the row offers Resume rather than Retry.
    resumable: true,
  });
  s.status = 'error';
  s.reconnect = { attempt: 0, statusUnknown: false, exhausted: false };
  // NOT the placeholder. The generation is still the server's, so an answer
  // that has not been written yet must not be stored as though it had (RC-2:
  // a blank assistant row also stopped every later reload from looking for
  // the real answer). What IS durable is the state of the send.
  patchIntent(s, { state: 'interrupted' }, { persist: true });
  notifyNow(s.conversationId);
  void reconnectInterrupted(s);
}

/**
 * How hard, and for how long, a lost stream is chased.
 *
 * Exported so a test can drive the whole loop deterministically (base 0, no
 * jitter) instead of waiting on wall-clock backoff. Nothing in the app
 * writes to it.
 */
export const reconnectPolicy = {
  baseMs: 1000,
  capMs: 30_000,
  attempts: 20,
  jitter: true,
};

function backoffFor(attempt: number): number {
  const base = Math.min(
    reconnectPolicy.capMs,
    reconnectPolicy.baseMs * 2 ** attempt,
  );
  // Jitter, so a restart does not bring every open tab back in lockstep.
  return reconnectPolicy.jitter ? base * (0.5 + Math.random() / 2) : base;
}

function wait(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function setReconnect(s: LiveStream, view: ReconnectView): void {
  s.reconnect = view;
  notifyNow(s.conversationId);
}

/**
 * Chase an interrupted generation until the server gives a straight answer.
 *
 * RC-3: an accepted generation lived only in the orchestrator's memory, so a
 * deploy, a crash or a host reboot ended it — and the browser's re-attach got
 * a 404 it could not tell from "finished". Now the REQUEST is durable, so
 * there are only four honest conclusions, and this loop waits for one of
 * them: it is live (attach), it finished (load the answer), it definitively
 * never existed (unsent), or we still cannot tell (status unknown, and the
 * row says exactly that).
 *
 * Bounded on purpose: ~20 asks with exponential backoff to a 30 s cap, then
 * it stops and leaves an explicit Retry. A tab left open for a week must not
 * poll a dead endpoint for a week.
 */
async function reconnectInterrupted(s: LiveStream): Promise<void> {
  const conversationId = s.conversationId;
  const intentId = s.intentId;
  if (!intentId) {
    // Nothing to ask about (an old turn, or a send that predates intents).
    // A re-attach is still worth one try; beyond that the poll's own
    // reconciliation is what will find the answer.
    const outcome = await attachStream(conversationId);
    if (outcome !== 'attached' && streams.get(conversationId) === s) {
      setReconnect(s, { attempt: 1, statusUnknown: outcome !== 'ended', exhausted: true });
    }
    return;
  }
  for (let attempt = 0; attempt < reconnectPolicy.attempts; attempt += 1) {
    await wait(backoffFor(attempt));
    // Another stream took this conversation over (the user re-sent, or an
    // attach succeeded elsewhere): this loop no longer speaks for it.
    if (streams.get(conversationId) !== s) return;
    const report = await fetchChatRequest(intentId);
    if (streams.get(conversationId) !== s) return;
    if (report.kind === 'unauthenticated') {
      // Session death is not a retryable condition, and handleSessionEnd is
      // already routing this tab to sign-in.
      void handleSessionEnd();
      return;
    }
    if (report.kind === 'unavailable') {
      setReconnect(s, { attempt: attempt + 1, statusUnknown: true, exhausted: false });
      continue;
    }
    if (report.kind === 'unknown-intent') {
      // The server states it has no such request: this send never landed.
      patchIntent(s, { state: 'unsent', reason: 'The request never reached the server.' }, { persist: true });
      s.reconnect = undefined;
      notifyNow(conversationId);
      return;
    }
    setReconnect(s, { attempt: attempt + 1, statusUnknown: false, exhausted: false });
    if (report.live || (report.status === 'interrupted' && report.resumable)) {
      const outcome = await attachStream(conversationId);
      if (outcome === 'attached') return;
      if (outcome === 'unauthenticated') return;
      // 'ended' here means the generation finished between the two calls;
      // the next pass reads `completed` and loads the answer.
      continue;
    }
    if (report.status === 'completed' || report.answerPersisted) {
      await adoptPersistedAnswer(s);
      return;
    }
    if (report.status === 'failed' || report.status === 'cancelled') {
      patchIntent(
        s,
        {
          state: report.status === 'cancelled' ? 'cancelled' : 'failed',
          reason: 'The server stopped working on this answer.',
        },
        { persist: true },
      );
      s.reconnect = undefined;
      notifyNow(conversationId);
      return;
    }
    // accepted / running with nothing live yet: keep asking.
  }
  if (streams.get(conversationId) === s) {
    setReconnect(s, {
      attempt: reconnectPolicy.attempts,
      statusUnknown: true,
      exhausted: true,
    });
  }
}

/**
 * The answer is on the server: show THAT, not our placeholder.
 *
 * The stream object stays in the registry holding the loaded thread, because
 * that is the channel the view already listens on — replacing the messages
 * here is what makes the interrupted row disappear the moment the real
 * answer is in hand, in whichever conversation happens to be on screen.
 */
async function adoptPersistedAnswer(s: LiveStream): Promise<void> {
  const conv = await getHistoryStore()
    .load(s.conversationId, { force: true })
    .catch(() => null);
  if (streams.get(s.conversationId) !== s) return;
  if (conv) s.messages = conv.messages;
  s.status = 'done';
  s.reconnect = undefined;
  notifyNow(s.conversationId);
}

/**
 * A send that never became a stream.
 *
 * Classification is driven by the STATUS, never by the error sentence. The
 * previous version ran `/orchestrator is unreachable/i` over the body text,
 * which meant a backend 500, a real 404 and a model timeout were reported
 * identically — and any non-JSON body (an intermediary's own error page) fell
 * into the same bucket for want of a string to match. The status is a fact
 * and it is always present; the prose was neither.
 */
async function markSendFailed(s: LiveStream, res: Response): Promise<void> {
  let code: unknown;
  try {
    code = ((await res.json()) as { code?: unknown }).code;
  } catch {
    // Non-JSON body (an intermediary's own error page). The status still
    // says everything the page needs.
  }
  markUnreachable(s, res.status, code);
}

async function consume(s: LiveStream, body: ReadableStream<Uint8Array>) {
  let sawTerminal = false;
  /**
   * TTFT is untouched. The first event of a stream — and the first TOKEN of
   * an answer, which is the one the reader is waiting for — commits
   * immediately; only the high-frequency deltas after it ride the frame
   * clock. Waiting a frame for the first token would be the one delay a
   * reader can actually perceive, and it buys nothing: there is no second
   * update to coalesce it with yet.
   */
  let committed = false;
  let firstToken = false;
  for await (const ev of readChatStream(body)) {
    if (ev.kind === 'token') {
      if (!s.sawToken) {
        s.sawToken = true;
        firstToken = true;
        settleReasoningClock(s);
        // Tokens are the answer being written: the send is past acceptance.
        // Not persisted — a reload reconciles with the server, whose account
        // of a running generation is better than this label.
        patchIntent(s, { state: 'processing' });
      }
      updateAssistant(s, (m) => ({
        ...m,
        content: m.content + ev.text,
        searchStatus: undefined,
      }));
    } else if (ev.kind === 'status') {
      // Two progress systems share this event. A payload with a typed `phase`
      // is Salesforce Intelligence Mode and drives the ReasoningStar; a bare
      // `text` is the older web-search/URL line and keeps its own row, so the
      // two never render on top of each other.
      updateAssistant(s, (m) =>
        ev.phase
          ? { ...m, phaseStatus: ev.phase, searchStatus: undefined }
          : { ...m, searchStatus: ev.text },
      );
    } else if (ev.kind === 'reasoning') {
      if (s.reasoningStartedAt === null) s.reasoningStartedAt = Date.now();
      updateAssistant(s, (m) => ({
        ...m,
        reasoning: (m.reasoning ?? '') + ev.text,
      }));
    } else if (ev.kind === 'research') {
      if (s.researchStartedAt === null) s.researchStartedAt = Date.now();
      const elapsedMs = Date.now() - s.researchStartedAt;
      updateAssistant(s, (m) => {
        const prev = m.research ?? { queries: [] };
        if (ev.phase === 'query' && ev.query) {
          const q = ev.query;
          // A plan's steps can repeat a search; merge rather than duplicate.
          const at = prev.queries.findIndex((x) => x.query === q.query);
          const queries =
            at === -1
              ? [...prev.queries, q]
              : prev.queries.map((x, i) => (i === at ? q : x));
          return { ...m, research: { ...prev, queries, elapsedMs, active: true } };
        }
        if (ev.phase === 'reading') {
          return {
            ...m,
            research: {
              ...prev,
              reading: (prev.reading ?? 0) + (ev.count ?? 0),
              elapsedMs,
              active: true,
            },
          };
        }
        return {
          ...m,
          research: {
            ...prev,
            read: (prev.read ?? 0) + (ev.count ?? 0),
            elapsedMs,
            active: true,
          },
        };
      });
    } else if (ev.kind === 'step') {
      // The first step RETIRES the planning line. `describe(plan)` sets a
      // single static label ("Planning the steps for this task") when the run
      // is classified, and nothing ever cleared it — so it sat above the
      // timeline for the whole run, still claiming to be planning while the
      // plan was already executing underneath. The steps carry the real,
      // task-specific text; once they exist the placeholder is not just
      // redundant, it is wrong (2026-08-29).
      updateAssistant(s, (m) => ({
        ...m,
        steps: mergeStep(m.steps, ev.step),
        searchStatus: undefined,
      }));
    } else if (ev.kind === 'meta') {
      settleReasoningClock(s);
      // V29: a stream now carries SEVERAL meta events. The first one names
      // the generation and arrives before any token — that is what lets the
      // turn be marked `accepted` while the answer is still empty, so a
      // reload in that window has an id to reconcile with instead of a
      // guess. Every later meta is the engine's, and wins for everything
      // else; the generation id is carried across explicitly because the
      // browser must never lose the handle on the answer it is watching.
      const extra = ev.meta as typeof ev.meta & {
        attempt?: number;
        intent_id?: string;
      };
      if (typeof extra.generation_id === 'string' && !s.generationId) {
        s.generationId = extra.generation_id;
        s.attempt = typeof extra.attempt === 'number' ? extra.attempt : 1;
        patchIntent(
          s,
          {
            state: 'accepted',
            ...(typeof extra.intent_id === 'string' ? { id: extra.intent_id } : {}),
            generation_id: extra.generation_id,
            attempt: s.attempt,
          },
          { persist: true },
        );
      }
      updateAssistant(s, (m) => ({
        ...m,
        research: m.research ? { ...m.research, active: false } : undefined,
        // The star must stop when the answer arrives, not when a timer says so.
        phaseStatus: undefined,
        // The server's meta REPLACES the local one, so the answer's tree
        // position has to be carried across explicitly — losing it would
        // orphan the answer from the question it belongs to.
        meta: metaWithBranch(
          {
            // The handle on the answer, kept whatever a later meta omits:
            // it is how the server dedupes the persist and how a reload
            // matches this answer to the send it belongs to.
            ...(s.generationId ? { generation_id: s.generationId } : {}),
            ...foldStreamState(ev.meta, {
              reasoning: m.reasoning,
              reasoningSeconds: m.reasoningSeconds ?? s.reasoningSeconds,
              steps: m.steps,
              research: m.research,
              phaseStatus: m.phaseStatus,
            }),
          },
          branchOf(m),
        ),
      }));
    } else if (ev.kind === 'error') {
      sawTerminal = true;
      settleReasoningClock(s);
      finalize(s, { status: 'error', errorMessage: ev.message });
      break;
    } else if (ev.kind === 'done') {
      sawTerminal = true;
      settleReasoningClock(s);
      finalize(s, { status: 'done' });
      break;
    }
    if (committed && !firstToken) {
      notifyFrame(s);
    } else {
      committed = true;
      firstToken = false;
      notifyNow(s.conversationId);
    }
  }
  if (!sawTerminal) {
    // Stream ended without a terminal event — treat as complete.
    settleReasoningClock(s);
    finalize(s, { status: 'done' });
  }
}

/**
 * Open a stream and put its answer placeholder at the end of `turns`.
 *
 * `turns` is everything the conversation STORES — sibling branches included —
 * not the path being sent to the model. Those were the same list until edits
 * became non-destructive; keeping them the same would have meant a stream
 * saving only the branch it answered and quietly dropping the others.
 * `assistantBranch` is what files the answer under the right question.
 */
function register(
  conversationId: string,
  turns: ChatMessage[],
  assistantBranch?: BranchMeta,
): LiveStream {
  const s: LiveStream = {
    conversationId,
    assistantId: newId(),
    messages: [],
    status: 'streaming',
    controller: new AbortController(),
    reasoningStartedAt: null,
    researchStartedAt: null,
    sawToken: false,
  };
  s.messages = [
    ...turns,
    {
      id: s.assistantId,
      role: 'assistant',
      content: '',
      status: 'streaming',
      createdAt: Date.now(),
      ...(assistantBranch
        ? { meta: metaWithBranch(undefined, assistantBranch) }
        : {}),
    },
  ];
  streams.set(conversationId, s);
  // A brand-new stream replaces whatever was registered for this conversation;
  // a frame booked by the previous one must not speak for this one.
  notifyNow(conversationId);
  return s;
}

export interface StartStreamOptions {
  conversationId: string;
  /** Everything the conversation STORES, sibling branches included. */
  turns: ChatMessage[];
  /**
   * The single path down the tree to send to the model. Defaults to `turns`,
   * which is correct for every conversation that has never been edited.
   *
   * Kept separate so an edited conversation stores both versions of a turn
   * while the prompt still carries exactly one — sending both would put two
   * contradictory histories of the same question in one context window.
   */
  context?: ChatMessage[];
  /** Where the answer belongs in the tree (see BranchMeta). */
  assistantBranch?: BranchMeta;
  prefs: ChatPrefs;
  /** 2026-08-05: up to 5 attached images (base64, no data: prefix). */
  images?: string[] | null;
  pdf?: string | null;
  pdfName?: string | null;
  /**
   * 2026-09-02: documents that STREAMED to /api/upload (purpose=document)
   * instead of riding inline — 512 MB of base64 through this JSON body would
   * kill the tab and both servers. The orchestrator reads the stored bytes
   * by reference; up to five per message.
   */
  pdfUploads?: { upload_id: string; name: string }[] | null;
  /**
   * 2026-09-09: videos that streamed to /api/upload (purpose=video). The
   * orchestrator attaches to the analysis job it started at upload time.
   */
  videoUploads?: { upload_id: string; name: string }[] | null;
  /**
   * NEW-14: this turn has an uploaded dataset.
   *
   * A flag rather than a payload, because that is genuinely all there is to
   * send: the file already streamed to /api/upload and the orchestrator finds
   * it through `conversation_id`. Its only job is to let the proxy tell a
   * dataset send apart from an empty one when neither carries text — without
   * it, both look identical on the wire and both were answered with a 400.
   */
  dataset?: boolean;
  /**
   * Salesforce Intelligence Mode: the answer to a clarifying question this
   * conversation is waiting on. Present → the server resumes the ORIGINAL
   * request with this answer folded in, instead of treating the message as a
   * new question. Carries its own idempotency key, so a double-click or a
   * retried send resolves to the first answer rather than a second generation.
   */
  clarification?: ClarificationResponse | null;
  /**
   * V29: this send's intent id, minted by the browser when Send was pressed
   * and REUSED by every retry of the same turn. The server records it, so a
   * second POST carrying it attaches to the generation that already exists
   * instead of cancelling it and starting another (F14). Absent for sends
   * that predate the intent (nothing changes for them).
   */
  intentId?: string;
  /**
   * The user turn that carries `meta.intent` — the one this stream keeps
   * honest as the send moves accepted → processing → completed / failed.
   */
  intentMessageId?: string;
}

/** Conversations whose in-flight send already carries a clarification answer. */
const submittedClarifications = new Set<string>();

/**
 * Has this exact answer already been sent? Guards the double-click at the
 * CLIENT edge too, so the second click never even opens a second stream — the
 * server-side guard is the one that matters, this one just avoids the flicker.
 */
export function clarificationAlreadySubmitted(key: string): boolean {
  return submittedClarifications.has(key);
}

export function markClarificationSubmitted(key: string): void {
  submittedClarifications.add(key);
  // Bounded: this is a within-session dedupe, not a persistent log.
  if (submittedClarifications.size > 200) {
    const oldest = submittedClarifications.values().next().value;
    if (oldest !== undefined) submittedClarifications.delete(oldest);
  }
}

/** Send a turn and stream the answer in the background. */
export async function startStream(opts: StartStreamOptions): Promise<void> {
  const { conversationId, turns, prefs } = opts;
  const context = opts.context ?? turns;
  // NEW-14: the turn being sent, read HERE — before the filter below removes
  // it. Every caller (send, retry, regenerate, edit) ends `context` at the
  // user turn being asked, so this is the question by construction, and it is
  // folded the same way `messages` is so a pasted-only turn still counts as
  // having said something. Empty means the send carried attachments alone,
  // and the proxy needs that fact stated rather than guessed at.
  const currentTurn = context[context.length - 1];
  const currentText =
    currentTurn?.role === 'user' ? foldTurnForModel(currentTurn) : '';
  const s = register(conversationId, turns, opts.assistantBranch);
  s.intentId = opts.intentId;
  // The turn to keep honest. Stated by the caller where it knows (the send
  // path); otherwise the last user turn, which is the question by
  // construction for every caller of this function.
  s.intentMessageId =
    opts.intentMessageId ??
    [...turns].reverse().find((m) => m.role === 'user' && m.meta?.intent?.id)?.id;
  // Said before the request goes out, so a reload in the gap between this
  // line and the server's first event finds a turn that admits it does not
  // know yet — and reconciles it against the server rather than guessing.
  if (opts.intentId) patchIntent(s, { state: 'submitting', id: opts.intentId });
  // Did the orchestrator accept the request? Decides whether a failure below
  // is "unreachable" (retry) or "interrupted" (re-join) — see markInterrupted.
  let connected = false;
  try {
    const res = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        messages: context
          .map((m) => ({
            role: m.role,
            // Pasted blocks AND a quoted excerpt are folded in here, at the
            // one point a stored turn becomes model text — which is why
            // neither is ever written into `content`, and why replaying the
            // transcript cannot duplicate them.
            content: foldTurnForModel(m),
          }))
          .filter((m) => m.content),
        // NEW-14: what the transcript above can no longer say, because the
        // filter drops exactly the turn a wordless send is made of.
        current_text: currentText,
        session_id: conversationId,
        conversation_id: conversationId,
        mode: prefs.salesforce ? 'salesforce' : 'assistant',
        sf_live: prefs.salesforce && prefs.sfLive,
        model: prefs.model,
        effort: prefs.effort,
        agent: prefs.agent,
        web_search: prefs.webSearch,
        deep_research: prefs.deepResearch,
        // The single-image spelling stays for the proxy's v1 contract; the
        // full list rides alongside when more than one image is attached.
        ...(opts.images?.length ? { image: opts.images[0] } : {}),
        ...(opts.images && opts.images.length > 1
          ? { images: opts.images }
          : {}),
        ...(opts.pdf
          ? { pdf: opts.pdf, pdf_filename: opts.pdfName ?? undefined }
          : {}),
        ...(opts.pdfUploads?.length ? { pdf_uploads: opts.pdfUploads } : {}),
        ...(opts.videoUploads?.length ? { video_uploads: opts.videoUploads } : {}),
        // NEW-14. Sent only when true, so every other request keeps exactly
        // the key set it had — this is a proxy hint, not part of the contract
        // with the orchestrator, which never sees it.
        ...(opts.dataset ? { dataset: true } : {}),
        ...(opts.clarification ? { clarification: opts.clarification } : {}),
        // V29: one logical send, however many times it is retried.
        ...(opts.intentId ? { intent_id: opts.intentId } : {}),
      }),
      signal: s.controller.signal,
    });
    if (!res.ok || !res.body) {
      await markSendFailed(s, res);
      return;
    }
    // Past this point the orchestrator HAS the request and owns the answer, so
    // a later failure is a dropped pipe, not an unreachable service.
    connected = true;
    await consume(s, res.body);
  } catch (err) {
    if (err instanceof DOMException && err.name === 'AbortError') {
      settleReasoningClock(s);
      finalize(s, { status: 'stopped' });
    } else if (connected) {
      markInterrupted(s);
    } else {
      // The request never reached a response, so there is no status to
      // report — null renders "Error / Connection unavailable" rather than
      // a number nothing actually sent.
      markUnreachable(s, null, 'NETWORK_ERROR');
    }
  }
}

/**
 * What a re-attach found. The three failures are NOT interchangeable
 * (fe-chat F3).
 *
 * `ended`         — the server says there is nothing live and nothing to
 *                   resume: the answer is in history, go and load it. This is
 *                   the only outcome that may unlock the composer.
 * `unreachable`   — we could not find out (a 502 while the orchestrator
 *                   restarts, a dead network). The generation is very
 *                   probably still running; reading this as "finished" is
 *                   what detached tabs permanently and let the next send
 *                   cancel a live generation.
 * `unauthenticated` — the session died; sign-in is already being routed to.
 */
export type AttachOutcome =
  | 'attached'
  | 'ended'
  | 'unreachable'
  | 'unauthenticated';

/**
 * Re-join a server-side generation after a reload. Replays the buffered
 * events (instant partial answer) and then streams live.
 */
/**
 * Attaches in flight, by conversation.
 *
 * Two callers routinely ask at once — the mount reconciliation and the poll's
 * first tick — and, since the request now goes out before anything is
 * registered, both would open their own SSE connection to the same
 * generation. They share one instead. (Before 2026-09-10 the placeholder
 * registration happened to serve as this lock, at the cost of an empty
 * bubble on screen whenever the attach failed.)
 */
const attaching = new Map<string, Promise<AttachOutcome>>();

export function attachStream(conversationId: string): Promise<AttachOutcome> {
  const inFlight = attaching.get(conversationId);
  if (inFlight) return inFlight;
  const run = runAttach(conversationId).finally(() => {
    attaching.delete(conversationId);
  });
  attaching.set(conversationId, run);
  return run;
}

async function runAttach(conversationId: string): Promise<AttachOutcome> {
  if (streams.get(conversationId)?.status === 'streaming') return 'attached';
  /**
   * The request goes out BEFORE any placeholder exists (2026-09-10).
   *
   * This used to register the stream first, so a refused attach left an
   * empty assistant bubble on screen that nothing was writing into — and,
   * because the registration had already replaced the view, the turn under
   * it was no longer the last message and could say nothing about itself
   * either. Nothing is shown until there is something to show.
   */
  const controller = new AbortController();
  let res: Response;
  try {
    res = await fetch(`/api/chat/attach/${encodeURIComponent(conversationId)}`, {
      signal: controller.signal,
    });
  } catch (err) {
    if (err instanceof DOMException && err.name === 'AbortError') return 'ended';
    // The fetch itself failed: we know nothing about the generation.
    return 'unreachable';
  }
  if (!res.ok || !res.body) {
    if (res.status === 401) {
      void handleSessionEnd();
      return 'unauthenticated';
    }
    // ONLY a 404 is the server saying there is nothing to attach to. The
    // proxy answers 404 for "no active generation" and 502 for everything it
    // could not complete, so the two are told apart by status and never by
    // inference (F3).
    return res.status === 404 ? 'ended' : 'unreachable';
  }
  // Seed from SERVER truth, never from whatever this browser happens to have
  // cached. A cache entry can be empty (a chat listed but never opened here,
  // or evicted by a quota purge); seeding from it made the replayed answer
  // look like the entire conversation, and the sync then tried to shrink the
  // real thread down to it. Read only once the attach has SUCCEEDED: a
  // forced read replaces the cache, and doing that for an attach that turns
  // out to be a 502 would rewrite a thread for no reason at all.
  let base = getHistoryStore().get(conversationId);
  try {
    base = (await getHistoryStore().load(conversationId, { force: true })) ?? base;
  } catch {
    // Offline — fall back to the cache; the server-side shrink guard is the
    // backstop that keeps a stale copy from destroying anything.
  }
  const turns = attachBaseTurns(base?.messages ?? []);
  // The replay rebuilds the answer from scratch, so its placeholder needs the
  // tree position of the question it is answering — otherwise re-joining a
  // generation after a reload would file the answer at the end of the flat
  // list instead of under its own turn.
  const s = register(
    conversationId,
    turns,
    branchForAppend(base?.messages ?? turns, turns),
  );
  // Stop must reach the reader that is now running.
  s.controller = controller;
  // The turn being answered, so the resumed stream keeps its intent honest.
  const question = [...turns].reverse().find((m) => m.role === 'user');
  s.intentMessageId = question?.id;
  s.intentId = question?.meta?.intent?.id;
  try {
    await consume(s, res.body);
  } catch (err) {
    if (err instanceof DOMException && err.name === 'AbortError') {
      settleReasoningClock(s);
      finalize(s, { status: 'stopped' });
      return 'attached';
    }
    // The pipe died after the replay began: the generation is still the
    // server's, so this is an interruption to recover from, not a send to
    // repeat.
    markInterrupted(s);
  }
  return 'attached';
}
