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

import { branchForAppend, branchOf, metaWithBranch } from './branching';
import type { ClarificationResponse } from './clarification';
import { getHistoryStore, newId } from './history';
import { foldModelContent } from './pasted';
import type { ChatPrefs } from './prefs';
import { toClientError } from './errorTypes';
import { foldStreamState, mergeStep, readChatStream } from './sse';
import type { BranchMeta, ChatMessage } from './types';

export type StreamStatus =
  | 'streaming'
  | 'done'
  | 'stopped'
  | 'error'
  | 'unreachable';

export interface LiveStreamView {
  conversationId: string;
  messages: ChatMessage[];
  status: StreamStatus;
}

interface LiveStream extends LiveStreamView {
  controller: AbortController;
  assistantId: string;
  reasoningStartedAt: number | null;
  /** First research event's timestamp — drives the panel's elapsed clock. */
  researchStartedAt: number | null;
  reasoningSeconds?: number;
  sawToken: boolean;
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

/** Conversations the SERVER is still generating for (survives reloads). */
export async function fetchServerActive(): Promise<string[]> {
  try {
    const res = await fetch('/api/chat/active');
    if (!res.ok) return [];
    const data = (await res.json()) as { active?: unknown };
    return Array.isArray(data.active)
      ? data.active.filter((x): x is string => typeof x === 'string')
      : [];
  } catch {
    return [];
  }
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

function updateAssistant(
  s: LiveStream,
  patch: Partial<ChatMessage> | ((m: ChatMessage) => ChatMessage),
): void {
  s.messages = s.messages.map((m) =>
    m.id === s.assistantId
      ? typeof patch === 'function'
        ? patch(m)
        : { ...m, ...patch }
      : m,
  );
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

function finalize(s: LiveStream, patch: Partial<ChatMessage>): void {
  updateAssistant(s, withLiveProgressRetired(patch));
  s.status = (patch.status as StreamStatus) ?? 'done';
  // Persist regardless of which conversation is on screen.
  getHistoryStore().saveMessages(s.conversationId, s.messages);
  notify(s.conversationId);
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
  updateAssistant(
    s,
    withLiveProgressRetired({
      status: 'error',
      errorMessage: err.message,
      errorStatus: err.status,
      errorCode: err.code,
    }),
  );
  s.status = 'unreachable';
  getHistoryStore().saveMessages(s.conversationId, s.messages);
  notify(s.conversationId);
}

/**
 * The stream died AFTER the orchestrator accepted the request. That is a very
 * different story from "unreachable": the generation is DETACHED server-side
 * (LiveGeneration), so it is still running, and re-sending would start a
 * SECOND one rather than recover this one. Reopening the conversation re-joins
 * it through GET /api/chat/attach/{id}. Whatever already streamed stays on the
 * message — this patches status/errorMessage and leaves `content` alone.
 */
function markInterrupted(s: LiveStream): void {
  updateAssistant(
    s,
    withLiveProgressRetired({
      status: 'error',
      errorMessage:
        'The connection to this answer was interrupted. The model is still working on it — reopen this chat to re-join.',
    }),
  );
  s.status = 'error';
  getHistoryStore().saveMessages(s.conversationId, s.messages);
  notify(s.conversationId);
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
  for await (const ev of readChatStream(body)) {
    if (ev.kind === 'token') {
      if (!s.sawToken) {
        s.sawToken = true;
        settleReasoningClock(s);
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
      updateAssistant(s, (m) => ({
        ...m,
        research: m.research ? { ...m.research, active: false } : undefined,
        // The star must stop when the answer arrives, not when a timer says so.
        phaseStatus: undefined,
        // The server's meta REPLACES the local one, so the answer's tree
        // position has to be carried across explicitly — losing it would
        // orphan the answer from the question it belongs to.
        meta: metaWithBranch(
          foldStreamState(ev.meta, {
            reasoning: m.reasoning,
            reasoningSeconds: m.reasoningSeconds ?? s.reasoningSeconds,
            steps: m.steps,
            research: m.research,
            phaseStatus: m.phaseStatus,
          }),
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
    notify(s.conversationId);
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
  notify(conversationId);
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
   * Salesforce Intelligence Mode: the answer to a clarifying question this
   * conversation is waiting on. Present → the server resumes the ORIGINAL
   * request with this answer folded in, instead of treating the message as a
   * new question. Carries its own idempotency key, so a double-click or a
   * retried send resolves to the first answer rather than a second generation.
   */
  clarification?: ClarificationResponse | null;
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
  const s = register(conversationId, turns, opts.assistantBranch);
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
            content: foldModelContent(m.content, m.meta?.pasted),
          }))
          .filter((m) => m.content),
        session_id: conversationId,
        conversation_id: conversationId,
        mode: prefs.salesforce ? 'salesforce' : 'assistant',
        sf_live: prefs.salesforce && prefs.sfLive,
        model: prefs.model,
        effort: prefs.effort,
        agent: prefs.agent,
        web_search: prefs.webSearch,
        // The single-image spelling stays for the proxy's v1 contract; the
        // full list rides alongside when more than one image is attached.
        ...(opts.images?.length ? { image: opts.images[0] } : {}),
        ...(opts.images && opts.images.length > 1
          ? { images: opts.images }
          : {}),
        ...(opts.pdf
          ? { pdf: opts.pdf, pdf_filename: opts.pdfName ?? undefined }
          : {}),
        ...(opts.clarification ? { clarification: opts.clarification } : {}),
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
 * Re-join a server-side generation after a reload. Replays the buffered
 * events (instant partial answer) and then streams live. Returns false when
 * there is nothing to attach to any more — load history instead.
 */
export async function attachStream(conversationId: string): Promise<boolean> {
  if (streams.get(conversationId)?.status === 'streaming') return true;
  // Seed from SERVER truth, never from whatever this browser happens to have
  // cached. A cache entry can be empty (a chat listed but never opened here,
  // or evicted by a quota purge); seeding from it made the replayed answer
  // look like the entire conversation, and the sync then tried to shrink the
  // real thread down to it.
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
  try {
    const res = await fetch(
      `/api/chat/attach/${encodeURIComponent(conversationId)}`,
      { signal: s.controller.signal },
    );
    if (!res.ok || !res.body) {
      streams.delete(conversationId); // finished already — history has it
      notify(conversationId);
      return false;
    }
    await consume(s, res.body);
    return true;
  } catch (err) {
    if (err instanceof DOMException && err.name === 'AbortError') {
      settleReasoningClock(s);
      finalize(s, { status: 'stopped' });
      return true;
    }
    streams.delete(conversationId);
    notify(conversationId);
    return false;
  }
}
