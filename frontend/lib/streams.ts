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

import { getHistoryStore, newId } from './history';
import { foldModelContent } from './pasted';
import type { ChatPrefs } from './prefs';
import { foldStreamState, mergeStep, readChatStream } from './sse';
import type { ChatMessage } from './types';

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

function finalize(s: LiveStream, patch: Partial<ChatMessage>): void {
  updateAssistant(s, patch);
  s.status = (patch.status as StreamStatus) ?? 'done';
  // Persist regardless of which conversation is on screen.
  getHistoryStore().saveMessages(s.conversationId, s.messages);
  notify(s.conversationId);
}

function markUnreachable(s: LiveStream): void {
  // Record the failure ON the message and PERSIST it. Previously this dropped
  // the placeholder and relied on a banner that only renders for the chat
  // currently on screen — so a send that failed while the user was in another
  // chat left no trace at all: no answer, no error, nothing in history.
  updateAssistant(s, {
    status: 'error',
    errorMessage:
      'The orchestrator is unreachable — your message was kept and can be re-sent.',
  });
  s.status = 'unreachable';
  getHistoryStore().saveMessages(s.conversationId, s.messages);
  notify(s.conversationId);
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
      updateAssistant(s, (m) => ({ ...m, searchStatus: ev.text }));
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
      updateAssistant(s, (m) => ({ ...m, steps: mergeStep(m.steps, ev.step) }));
    } else if (ev.kind === 'meta') {
      settleReasoningClock(s);
      updateAssistant(s, (m) => ({
        ...m,
        research: m.research ? { ...m.research, active: false } : undefined,
        meta: foldStreamState(ev.meta, {
          reasoning: m.reasoning,
          reasoningSeconds: m.reasoningSeconds ?? s.reasoningSeconds,
          steps: m.steps,
          research: m.research,
        }),
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

function register(conversationId: string, turns: ChatMessage[]): LiveStream {
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
    },
  ];
  streams.set(conversationId, s);
  notify(conversationId);
  return s;
}

export interface StartStreamOptions {
  conversationId: string;
  turns: ChatMessage[];
  prefs: ChatPrefs;
  /** 2026-08-05: up to 5 attached images (base64, no data: prefix). */
  images?: string[] | null;
  pdf?: string | null;
  pdfName?: string | null;
}

/** Send a turn and stream the answer in the background. */
export async function startStream(opts: StartStreamOptions): Promise<void> {
  const { conversationId, turns, prefs } = opts;
  const s = register(conversationId, turns);
  try {
    const res = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        messages: turns
          .map((m) => ({
            role: m.role,
            content: foldModelContent(m.content, m.meta?.pasted),
          }))
          .filter((m) => m.content),
        session_id: conversationId,
        conversation_id: conversationId,
        mode: prefs.salesforce ? 'salesforce' : 'assistant',
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
      }),
      signal: s.controller.signal,
    });
    if (!res.ok || !res.body) {
      markUnreachable(s);
      return;
    }
    await consume(s, res.body);
  } catch (err) {
    if (err instanceof DOMException && err.name === 'AbortError') {
      settleReasoningClock(s);
      finalize(s, { status: 'stopped' });
    } else {
      markUnreachable(s);
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
  const s = register(conversationId, turns);
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
