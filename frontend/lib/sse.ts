/**
 * Hand-rolled Server-Sent Events parsing.
 *
 * Decision (per §9 "Tech"): we deliberately do NOT use the Vercel AI SDK.
 * The orchestrator's contract (§10) carries a custom `meta` event with the
 * proof payload (sql / data / chart / citations / report_files), and the AI
 * SDK's data-stream protocol does not pass foreign SSE event types through
 * cleanly. A ~60-line spec-compliant parser is smaller than the adapter
 * would be, and it is fully unit-tested (tests/sse.test.ts).
 *
 * The parser is incremental: feed() accepts arbitrary chunk boundaries
 * (events split mid-line, mid-multibyte handled upstream by TextDecoder)
 * and emits complete events only.
 */

import { parsePhaseStatus } from './phases';

export interface SSEEvent {
  /** Event type; defaults to "message" when the `event:` field is absent. */
  event: string;
  /** Joined data lines (SSE spec: multiple `data:` lines join with \n). */
  data: string;
}

export class SSEParser {
  private buffer = '';
  private eventType = '';
  private dataLines: string[] = [];
  /** True when the previous chunk ended in \r (maybe half of a CRLF). */
  private pendingCR = false;

  /**
   * Feed a decoded text chunk; returns every event completed by this chunk.
   * Handles \n, \r\n and \r line endings and events split across chunks.
   */
  feed(chunk: string): SSEEvent[] {
    if (chunk === '') return [];
    if (this.pendingCR) {
      // The \r was already consumed as a line ending; swallow the matching
      // \n of a CRLF pair split across chunks.
      this.pendingCR = false;
      if (chunk.startsWith('\n')) chunk = chunk.slice(1);
    }
    this.buffer += chunk;
    const events: SSEEvent[] = [];

    // Process every complete line currently in the buffer.
    for (;;) {
      const nl = this.buffer.search(/[\r\n]/);
      if (nl === -1) break;
      const line = this.buffer.slice(0, nl);
      let sepLen = 1;
      if (this.buffer[nl] === '\r') {
        if (nl + 1 === this.buffer.length) {
          this.pendingCR = true; // may be half of a CRLF pair
        } else if (this.buffer[nl + 1] === '\n') {
          sepLen = 2;
        }
      }
      this.buffer = this.buffer.slice(nl + sepLen);

      const done = this.processLine(line);
      if (done) events.push(done);
    }
    return events;
  }

  private processLine(line: string): SSEEvent | null {
    if (line === '') {
      // Blank line = dispatch the pending event (if it has any data).
      if (this.dataLines.length === 0 && this.eventType === '') return null;
      const ev: SSEEvent = {
        event: this.eventType || 'message',
        data: this.dataLines.join('\n'),
      };
      this.eventType = '';
      this.dataLines = [];
      return ev;
    }
    if (line.startsWith(':')) return null; // comment / keep-alive

    let field: string;
    let value: string;
    const colon = line.indexOf(':');
    if (colon === -1) {
      field = line;
      value = '';
    } else {
      field = line.slice(0, colon);
      value = line.slice(colon + 1);
      if (value.startsWith(' ')) value = value.slice(1);
    }

    if (field === 'event') this.eventType = value;
    else if (field === 'data') this.dataLines.push(value);
    // `id:` and `retry:` are legal SSE fields we don't need — ignored.
    return null;
  }
}

/**
 * Typed view of the SSE contract on top of raw SSE events.
 * §10 (v1): token / meta / done / error. V2-DESIGN §2 adds `reasoning`
 * (model thinking deltas) and `step` (agent progress) — both optional and
 * backward-compatible.
 */
export type ChatStreamEvent =
  | { kind: 'token'; text: string }
  | { kind: 'reasoning'; text: string }
  | {
      kind: 'status';
      text: string;
      /**
       * Salesforce Intelligence Mode adds a typed phase to the SAME `status`
       * event rather than introducing a new event name — so replay,
       * persistence and the backend allowlist are untouched, and a client that
       * only knows `text` keeps working unchanged.
       */
      phase?: import('./phases').PhaseStatus;
    }
  | { kind: 'step'; step: import('./types').AgentStep }
  | {
      kind: 'research';
      phase: 'query' | 'reading' | 'read';
      query?: import('./types').ResearchQuery;
      count?: number;
    }
  | { kind: 'meta'; meta: import('./types').Meta }
  | { kind: 'done' }
  | { kind: 'error'; message: string };

/**
 * Map one raw SSE event to the chat contract.
 * Unknown event types and malformed JSON are returned as null (skipped) —
 * the stream must never crash the UI (V2 §2: the frontend must tolerate
 * future event types it does not know about).
 */
export function toChatStreamEvent(ev: SSEEvent): ChatStreamEvent | null {
  try {
    switch (ev.event) {
      case 'token':
      case 'reasoning': {
        const parsed = JSON.parse(ev.data) as { text?: unknown };
        return typeof parsed.text === 'string'
          ? { kind: ev.event, text: parsed.text }
          : null;
      }
      case 'status': {
        const parsed = JSON.parse(ev.data) as { text?: unknown };
        if (typeof parsed.text !== 'string') return null;
        const phase = parsePhaseStatus(parsed);
        return { kind: 'status', text: parsed.text, ...(phase ? { phase } : {}) };
      }
      case 'step': {
        const parsed = JSON.parse(ev.data) as {
          id?: unknown;
          title?: unknown;
          status?: unknown;
          detail?: unknown;
        };
        if (typeof parsed.id !== 'number' || typeof parsed.title !== 'string') {
          return null;
        }
        if (
          parsed.status !== 'running' &&
          parsed.status !== 'done' &&
          parsed.status !== 'failed'
        ) {
          return null;
        }
        return {
          kind: 'step',
          step: {
            id: parsed.id,
            title: parsed.title,
            status: parsed.status,
            ...(typeof parsed.detail === 'string'
              ? { detail: parsed.detail }
              : {}),
          },
        };
      }
      case 'research': {
        const parsed = JSON.parse(ev.data) as {
          phase?: unknown;
          query?: unknown;
          results?: unknown;
          count?: unknown;
        };
        if (parsed.phase === 'query') {
          if (typeof parsed.query !== 'string') return null;
          const results = Array.isArray(parsed.results) ? parsed.results : [];
          return {
            kind: 'research',
            phase: 'query',
            query: {
              query: parsed.query,
              // Drop anything malformed rather than rendering blanks.
              results: results.flatMap((r) => {
                const o = r as Record<string, unknown>;
                return typeof o?.url === 'string'
                  ? [{
                      url: o.url,
                      title: typeof o.title === 'string' ? o.title : o.url,
                      domain: typeof o.domain === 'string' ? o.domain : '',
                    }]
                  : [];
              }),
            },
          };
        }
        if (parsed.phase === 'reading' || parsed.phase === 'read') {
          return typeof parsed.count === 'number'
            ? { kind: 'research', phase: parsed.phase, count: parsed.count }
            : null;
        }
        return null;
      }
      case 'meta':
        return { kind: 'meta', meta: JSON.parse(ev.data) };
      case 'done':
        return { kind: 'done' };
      case 'error': {
        const parsed = JSON.parse(ev.data) as { message?: unknown };
        return {
          kind: 'error',
          message:
            typeof parsed.message === 'string'
              ? parsed.message
              : 'The engine reported an error without details.',
        };
      }
      default:
        return null;
    }
  } catch {
    return null;
  }
}

/**
 * Merge one incoming `step` event into the live timeline (V2 §4e):
 * same id updates the row in place (running → done/failed), keeping an
 * earlier detail when the update carries none; new ids append in order.
 */
export function mergeStep(
  steps: import('./types').AgentStep[] | undefined,
  step: import('./types').AgentStep,
): import('./types').AgentStep[] {
  const list = steps ? [...steps] : [];
  const idx = list.findIndex((s) => s.id === step.id);
  if (idx === -1) list.push(step);
  else list[idx] = { ...list[idx], ...step };
  return list;
}

/**
 * Fold the client-accumulated stream state (reasoning text, thinking
 * seconds, live steps) into the final `meta` event so it persists in history
 * (meta.reasoning / meta.steps per V2 §4d/§4e). Live steps win on detail;
 * meta steps win on final status; meta-only steps are appended.
 */
export function foldStreamState(
  meta: import('./types').Meta,
  live: {
    reasoning?: string;
    reasoningSeconds?: number;
    steps?: import('./types').AgentStep[];
    research?: import('./types').Research;
    phaseStatus?: import('./phases').PhaseStatus;
  },
): import('./types').Meta {
  const out = { ...meta };
  // The last phase the backend reported, kept so a reopened chat can show how
  // the answer was reached. The server's own final `meta.status` wins — it
  // knows whether the run completed or failed; the client only saw the labels.
  if (live.phaseStatus && !out.status) out.status = live.phaseStatus;
  // Research is measured entirely client-side (the server streams the
  // searches; the clock runs here), so it is carried onto the persisted meta
  // the same way reasoning is — otherwise reopening the chat loses the panel.
  if (live.research?.queries.length && !out.research) {
    out.research = { ...live.research, active: false };
  }
  if (live.reasoning && !out.reasoning) out.reasoning = live.reasoning;
  if (live.reasoningSeconds != null && out.reasoning_seconds == null) {
    out.reasoning_seconds = live.reasoningSeconds;
  }
  if (live.steps?.length) {
    const metaSteps = out.steps ?? [];
    const merged = live.steps.map((s) => {
      const final = metaSteps.find((x) => x.id === s.id);
      return final ? { ...s, status: final.status } : s;
    });
    for (const ms of metaSteps) {
      if (!merged.some((s) => s.id === ms.id)) merged.push(ms);
    }
    out.steps = merged;
  }
  return out;
}

/**
 * Async iterator over a fetch() SSE body, yielding §10 chat events.
 */
export async function* readChatStream(
  body: ReadableStream<Uint8Array>,
): AsyncGenerator<ChatStreamEvent> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  const parser = new SSEParser();
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      for (const raw of parser.feed(decoder.decode(value, { stream: true }))) {
        const ev = toChatStreamEvent(raw);
        if (ev) yield ev;
      }
    }
  } finally {
    reader.releaseLock();
  }
}
