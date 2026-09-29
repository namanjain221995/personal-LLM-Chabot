/**
 * A fake orchestrator for the recording-session contract (2026-09-29), shared
 * by the session tests. It implements the parts of the contract the browser
 * depends on, the way the contract words them:
 *
 *   - POST /audio/sessions creates a session and returns its state + config.
 *   - PUT .../parts/{seq} appends only when seq == next_part, answers a replay
 *     (seq < next_part) by SHA-256 as `duplicate` or 409 part_conflict, and
 *     refuses a gap with 409 out_of_order. It checks X-Part-SHA256 against the
 *     body itself, so a test cannot pass with a wrong hash.
 *   - Each acknowledged part is "transcribed" at once into one final segment,
 *     returned with every later answer from the browser's cursor.
 *   - POST .../finish, then GET long-polls until done.
 *
 * THE AUDIO IS MADE CHECKABLE WITHOUT BEING KEPT. Every slice a test records
 * is SLICE_BYTES long and carries its own index in its first four bytes, so
 * the server can read which slices each part carried. A two-hour recording is
 * 115 MB; keeping it to compare would make the test measure its own memory.
 * The list of slice indices the server appended is the recording's identity:
 * 0, 1, 2 … N-1 exactly means no slice was lost, repeated or reordered.
 *
 * Silence is modelled too: slices below `quietSlices` carry no words, which is
 * what a person pausing for the first 30 seconds produces.
 */
import { createHash } from 'node:crypto';
import { vi } from 'vitest';

/** 5 s at the 128.7 kb/s Chrome records Opus at (measured 2026-09-28). */
export const SLICE_BYTES = 80_437;

export const SESSION_ID = 'a'.repeat(32);

/** One recorder timeslice, with its index written into its first 4 bytes. */
export function sliceBytes(idx: number, size = SLICE_BYTES): Uint8Array<ArrayBuffer> {
  const b = new Uint8Array(size);
  b[0] = idx & 0xff;
  b[1] = (idx >> 8) & 0xff;
  b[2] = (idx >> 16) & 0xff;
  b[3] = (idx >> 24) & 0xff;
  for (let i = 4; i < size; i += 997) b[i] = (idx * 31 + i) & 0xff;
  return b;
}

export function sliceBlob(idx: number, size = SLICE_BYTES): Blob {
  return new Blob([sliceBytes(idx, size)], { type: 'audio/webm' });
}

export type Injection =
  | { network: true }
  | { dropResponse: true }
  | { status: number; body?: Record<string, unknown>; headers?: Record<string, string> }
  | { html: number };

export interface RequestLog {
  method: string;
  url: string;
  path: string;
  seq: number | null;
  headers: Record<string, string>;
  bytes: number;
}

export interface FakeOptions {
  /** Slices at the start that carry no speech (a quiet opening). */
  quietSlices?: number;
  /** Size of every slice, to read indices back out of a part. */
  sliceSize?: number;
  /** GETs after finish that still answer `finishing`. */
  pollsBeforeDone?: number;
  /** Override the finished state (outcome, gaps, text…). */
  finalState?: (words: string[], server: FakeSessionServer) => Record<string, unknown>;
  /** Called for every request before it is handled; return an injection to fake a fault. */
  inject?: (req: { method: string; path: string; seq: number | null; attempt: number }) => Injection | undefined;
  config?: Record<string, unknown>;
}

function reply(status: number, body: unknown, headers: Record<string, string> = {}): Response {
  const text = body === undefined ? '' : JSON.stringify(body);
  const lower = Object.fromEntries(Object.entries(headers).map(([k, v]) => [k.toLowerCase(), v]));
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: (name: string) => lower[name.toLowerCase()] ?? null },
    text: async () => text,
    json: async () => JSON.parse(text),
  } as unknown as Response;
}

function htmlPage(status: number): Response {
  return {
    ok: false,
    status,
    headers: { get: () => 'text/html' },
    text: async () => '<html><body>Bad gateway</body></html>',
    json: async () => {
      throw new SyntaxError('Unexpected token <');
    },
  } as unknown as Response;
}

export class FakeSessionServer {
  readonly opts: FakeOptions;
  status: 'recording' | 'finishing' | 'done' | 'failed' | 'cancelled' | 'none' = 'none';
  created = 0;
  nextPart = 0;
  bytesStored = 0;
  /** seq -> sha of the part the server holds. */
  partSha = new Map<number, string>();
  /** Every slice index appended, in order: the recording's identity. */
  appendedSlices: number[] = [];
  segments: Array<{ i: number; start_ms: number; end_ms: number; text: string; language: string; low: boolean }> = [];
  rev = 0;
  pollsAfterFinish = 0;
  finishBody: Record<string, unknown> | null = null;
  deleted = false;
  retranscribes = 0;
  log: RequestLog[] = [];
  private attempts = new Map<string, number>();

  readonly fetch = vi.fn(async (input: string | URL | Request, init: RequestInit = {}) =>
    this.handle(String(input), init),
  );

  constructor(opts: FakeOptions = {}) {
    this.opts = opts;
  }

  get sliceSize(): number {
    return this.opts.sliceSize ?? SLICE_BYTES;
  }

  words(): string[] {
    return this.segments.map((s) => s.text);
  }

  putsFor(seq: number): RequestLog[] {
    return this.log.filter((r) => r.method === 'PUT' && r.seq === seq);
  }

  state(cursor = 0, extra: Record<string, unknown> = {}): Record<string, unknown> {
    const audioMs = this.appendedSlices.length * 5000;
    const done = this.status === 'done';
    const base: Record<string, unknown> = {
      session_id: SESSION_ID,
      status: this.status === 'none' ? 'recording' : this.status,
      rev: this.rev,
      next_part: this.nextPart,
      bytes_stored: this.bytesStored,
      audio_ms: audioMs,
      transcribed_ms: audioMs,
      backlog_ms: 0,
      waiting_on: 'none',
      progressive: true,
      cursor: this.segments.length,
      segments: this.segments.filter((s) => s.i >= cursor),
      tentative: '',
      gaps: [],
      outcome: null,
      text: null,
      language: null,
      language_code: null,
      speech_ms: this.segments.length * 5000,
      ended_by: null,
      stored: { kept: true, retention_days: 0, delete_after: null, bytes: this.bytesStored },
      error: null,
    };
    if (done) {
      const words = this.words();
      Object.assign(base, {
        outcome: words.length ? 'transcribed' : 'no_speech',
        text: words.join(' '),
        language: 'English',
        language_code: 'en',
        ...(this.opts.finalState ? this.opts.finalState(words, this) : {}),
      });
    }
    return { ...base, ...extra };
  }

  private async handle(url: string, init: RequestInit): Promise<Response> {
    const u = new URL(url, 'http://browser.test');
    const method = (init.method ?? 'GET').toUpperCase();
    const path = u.pathname;
    const partMatch = path.match(/\/parts\/(\d+)$/);
    const seq = partMatch ? Number(partMatch[1]) : null;
    const key = `${method} ${path}`;
    const attempt = this.attempts.get(key) ?? 0;
    this.attempts.set(key, attempt + 1);
    const headers: Record<string, string> = {};
    const h = init.headers as Record<string, string> | undefined;
    if (h) for (const [k, v] of Object.entries(h)) headers[k.toLowerCase()] = v;
    let bytes: Uint8Array | null = null;
    if (init.body instanceof ArrayBuffer || ArrayBuffer.isView(init.body)) {
      bytes = ArrayBuffer.isView(init.body)
        ? new Uint8Array(init.body.buffer, init.body.byteOffset, init.body.byteLength)
        : new Uint8Array(init.body as ArrayBuffer);
    } else if (init.body && typeof (init.body as { byteLength?: number }).byteLength === 'number') {
      bytes = new Uint8Array(init.body as unknown as ArrayBuffer);
    }
    this.log.push({ method, url, path, seq, headers, bytes: bytes?.byteLength ?? 0 });

    const injected = this.opts.inject?.({ method, path, seq, attempt });
    if (injected && 'network' in injected) throw new TypeError('Failed to fetch');
    if (injected && 'html' in injected) return htmlPage(injected.html);
    if (injected && 'status' in injected) {
      // The contract's side effects of two refusals: a 507 auto-finishes the
      // session, and `session_closed` means it was already finishing.
      const reason = injected.body?.reason;
      if ((injected.status === 507 || reason === 'session_closed') && this.status === 'recording') {
        this.status = 'finishing';
        this.rev += 1;
      }
      return reply(injected.status, injected.body ?? { detail: 'refused' }, injected.headers);
    }
    const response = this.route(method, path, u, seq, bytes, init);
    if (injected && 'dropResponse' in injected) throw new TypeError('network changed');
    return response;
  }

  private route(
    method: string,
    path: string,
    u: URL,
    seq: number | null,
    bytes: Uint8Array | null,
    init: RequestInit,
  ): Response {
    const cursor = Number(u.searchParams.get('cursor') ?? 0);
    if (method === 'POST' && path === '/api/audio/sessions') {
      this.created += 1;
      if (this.status === 'none') this.status = 'recording';
      return reply(201, {
        ...this.state(0),
        config: {
          part_ms: 5000,
          part_limit_bytes: 8_388_608,
          bits_per_second: null,
          idle_close_s: 600,
          long_poll_max_s: 25,
          ...(this.opts.config ?? {}),
        },
      });
    }
    if (!path.startsWith(`/api/audio/sessions/${SESSION_ID}`)) {
      return reply(404, { detail: 'not found', reason: 'not_found' });
    }
    if (this.deleted) return reply(404, { detail: 'not found', reason: 'not_found' });
    if (method === 'PUT' && seq !== null) {
      if (this.status !== 'recording') {
        return reply(409, { detail: 'closed', reason: 'session_closed', status: this.status, ended_by: 'person' });
      }
      if (!bytes || bytes.byteLength === 0) return reply(400, { detail: 'empty', reason: 'bad_request' });
      const sha = createHash('sha256').update(bytes).digest('hex');
      if (sha !== (init.headers as Record<string, string>)['x-part-sha256']) {
        return reply(422, { detail: 'corrupt', reason: 'part_corrupt' });
      }
      if (seq > this.nextPart) {
        return reply(409, { detail: 'out of order', reason: 'out_of_order', next_part: this.nextPart });
      }
      if (seq < this.nextPart) {
        if (this.partSha.get(seq) !== sha) {
          return reply(409, { detail: 'conflict', reason: 'part_conflict', next_part: this.nextPart });
        }
        return reply(200, { accepted: seq, duplicate: true, ...this.state(cursor) });
      }
      // Append: read the slice indices back out of the part.
      const size = this.sliceSize;
      const words: string[] = [];
      for (let off = 0; off + 4 <= bytes.byteLength; off += size) {
        const idx = bytes[off]! | (bytes[off + 1]! << 8) | (bytes[off + 2]! << 16) | (bytes[off + 3]! << 24);
        this.appendedSlices.push(idx);
        if (idx >= (this.opts.quietSlices ?? 0)) words.push(`w${idx}`);
      }
      this.partSha.set(seq, sha);
      this.nextPart += 1;
      this.bytesStored += bytes.byteLength;
      if (words.length) {
        const start = this.appendedSlices.length - words.length;
        this.segments.push({
          i: this.segments.length,
          start_ms: start * 5000,
          end_ms: this.appendedSlices.length * 5000,
          text: words.join(' '),
          language: 'en',
          low: false,
        });
      }
      this.rev += 1;
      return reply(200, { accepted: seq, duplicate: false, ...this.state(cursor) });
    }
    if (method === 'POST' && path.endsWith('/finish')) {
      this.finishBody = JSON.parse(String(init.body ?? '{}'));
      const last = this.finishBody!.last_part as number | null;
      if (this.status === 'recording') {
        if (typeof last === 'number' && last >= this.nextPart) {
          return reply(409, {
            detail: 'missing',
            reason: 'parts_missing',
            next_part: this.nextPart,
            last_part: last,
          });
        }
        this.status = 'finishing';
        this.rev += 1;
        return reply(202, this.state(cursor));
      }
      return reply(200, this.state(cursor));
    }
    if (method === 'GET' && path === `/api/audio/sessions/${SESSION_ID}`) {
      if (this.status === 'finishing') {
        this.pollsAfterFinish += 1;
        if (this.pollsAfterFinish > (this.opts.pollsBeforeDone ?? 1)) {
          this.status = 'done';
          this.rev += 1;
        }
      }
      return reply(200, this.state(cursor));
    }
    if (method === 'POST' && path.endsWith('/retranscribe')) {
      if (this.status === 'recording' || this.status === 'finishing') {
        return reply(409, { detail: 'busy', reason: 'session_busy' });
      }
      this.retranscribes += 1;
      this.status = 'finishing';
      this.pollsAfterFinish = 0;
      this.rev += 1;
      return reply(202, this.state(cursor));
    }
    if (method === 'DELETE') {
      this.deleted = true;
      this.status = 'cancelled';
      return reply(204, undefined);
    }
    return reply(404, { detail: 'no route', reason: 'not_found' });
  }
}
