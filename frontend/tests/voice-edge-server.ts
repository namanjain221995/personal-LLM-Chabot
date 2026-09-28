/**
 * A fake orchestrator for the recorder's EDGES (2026-09-29): several
 * sessions, several accounts, the server's own idle close, continuation
 * sessions, and GET /api/auth/me. tests/voice-session-fake.ts models one
 * session of one person, which is all the builder's tests needed; the defects
 * fixed on fix/voice-recorder-edges live exactly where that model stops.
 *
 * The rules are the backend's, read from orchestrator/app/dictation.py and
 * audio_api.py on wip/backend-long-form-c32-6 (not merged, not deployed):
 *
 *  - `_owned_row`: another account's session, or a cancelled one, is 404.
 *  - `_create_row`: ONE session in status 'recording' per account; a second
 *    create is 409 session_active with the live one's id. A create repeated
 *    with the same client_key answers the same session (200).
 *  - `append_part`: a replay (seq < next_part) is 200 duplicate when its
 *    SHA-256 matches, else 409 part_conflict; a session no longer
 *    'recording' refuses a new part with 409 session_closed {status,
 *    ended_by}; a gap is 409 out_of_order.
 *  - `_lapsed_and_idle` / maintain_once: a 'recording' session with no part
 *    for VOICE_SESSION_IDLE_S (600 s) is finished with ended_by 'idle'. The
 *    server's maintenance runs whether or not the browser can reach it, so it
 *    is modelled on every request attempt and on `tick()`.
 *  - `finish`: 'recording' becomes 'finishing' (202); anything else answers
 *    its state (200); last_part >= next_part is 409 parts_missing.
 *  - `discard`: 204 for the owner in any state, including cancelled.
 *  - CONTINUATION (fix/voice-server-hardening, the coordinator's decision of
 *    2026-09-29): a create may carry `continues: <session id>`; the new
 *    session is linked and transcribed separately.
 *
 * Every slice a test records carries its own index in its first four bytes,
 * as in voice-session-fake.ts, so the server can say which slices each
 * session stored. A continuation's part 0 begins with a front of
 * `frontBytes` bytes (the test's own ContainerCuts supplies it), which the
 * server records and skips.
 */
import { createHash } from 'node:crypto';
import { vi } from 'vitest';

export const EDGE_SLICE = 4000;

export function edgeSlice(idx: number, size = EDGE_SLICE): Blob {
  const b = new Uint8Array(size);
  b[0] = idx & 0xff;
  b[1] = (idx >> 8) & 0xff;
  b[2] = (idx >> 16) & 0xff;
  b[3] = (idx >> 24) & 0xff;
  for (let i = 4; i < size; i += 251) b[i] = (idx * 17 + i) & 0xff;
  return new Blob([b], { type: 'audio/webm' });
}

export interface EdgeSession {
  id: string;
  user: string;
  clientKey: string;
  status: 'recording' | 'finishing' | 'done' | 'cancelled';
  endedBy: string | null;
  continues: string | null;
  nextPart: number;
  partSha: Map<number, string>;
  slices: number[];
  /** The raw bytes stored, in order, when `keepBytes` is on. */
  bytes: Uint8Array[];
  /** How many bytes are stored, always counted. */
  storedBytes: number;
  /** The front a standalone continuation's part 0 carried (null: byte for byte). */
  front: Uint8Array | null;
  lastPartAt: number;
  rev: number;
  polls: number;
  deletes: number;
}

export interface EdgeRequest {
  method: string;
  path: string;
  user: string | null;
  keepalive: boolean;
  body: string | null;
}

function reply(status: number, body?: unknown): Response {
  const text = body === undefined ? '' : JSON.stringify(body);
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: () => null },
    text: async () => text,
    json: async () => JSON.parse(text),
  } as unknown as Response;
}

export class EdgeServer {
  /** The account whose cookie the browser sends; null is signed out. */
  signedIn: string | null = 'u1';
  online = true;
  /** PUTs hang without an answer, like a socket that died without FIN or RST. */
  hangPuts = false;
  /** The next N finish POSTs hang; the next N long-polls hang. */
  hangFinishes = 0;
  hangPolls = 0;
  idleS = 600;
  pollsBeforeDone = 1;
  capacityFull = false;
  /** Continuation creates are refused with this status/reason, when set. */
  refuseContinuation: { status: number; reason: string } | null = null;
  keepBytes = false;
  frontBytes = 0;
  /** Outcome overrides, per session id. */
  finalState: (s: EdgeSession) => Record<string, unknown> = () => ({});
  sessions = new Map<string, EdgeSession>();
  log: EdgeRequest[] = [];
  private n = 0;

  readonly fetch = vi.fn(async (input: string | URL | Request, init: RequestInit = {}) => {
    const url = new URL(String(input), 'http://browser.test');
    const method = (init.method ?? 'GET').toUpperCase();
    const body = typeof init.body === 'string' ? init.body : null;
    this.log.push({
      method,
      path: url.pathname,
      user: this.signedIn,
      keepalive: init.keepalive === true,
      body,
    });
    this.tick();
    if (!this.online) throw new TypeError('Failed to fetch');
    if (method === 'PUT' && this.hangPuts) return this.hang(init);
    if (method === 'POST' && url.pathname.endsWith('/finish') && this.hangFinishes > 0) {
      this.hangFinishes -= 1;
      return this.hang(init);
    }
    if (method === 'GET' && /\/api\/audio\/sessions\/[0-9a-f]{32}$/.test(url.pathname) && this.hangPolls > 0) {
      this.hangPolls -= 1;
      return this.hang(init);
    }
    return this.route(method, url, init);
  });

  /** The server's maintenance loop: idle close. */
  tick(): void {
    const now = Date.now();
    for (const s of this.sessions.values()) {
      if (s.status === 'recording' && now - s.lastPartAt > this.idleS * 1000) {
        s.status = 'finishing';
        s.endedBy = 'idle';
        s.rev += 1;
      }
    }
  }

  requestsFor(id: string): EdgeRequest[] {
    return this.log.filter((r) => r.path.includes(id));
  }

  only(user: string): EdgeSession[] {
    return [...this.sessions.values()].filter((s) => s.user === user);
  }

  private hang(init: RequestInit): Promise<Response> {
    return new Promise<Response>((_, reject) => {
      const signal = init.signal;
      if (signal?.aborted) reject(new DOMException('aborted', 'AbortError'));
      signal?.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')));
    });
  }

  private state(s: EdgeSession, cursor = 0): Record<string, unknown> {
    const words = s.slices.map((i) => `w${i}`);
    const done = s.status === 'done';
    return {
      session_id: s.id,
      status: s.status,
      rev: s.rev,
      next_part: s.nextPart,
      bytes_stored: s.slices.length * EDGE_SLICE,
      audio_ms: s.slices.length * 5000,
      transcribed_ms: s.slices.length * 5000,
      backlog_ms: 0,
      waiting_on: 'none',
      progressive: true,
      cursor: s.slices.length,
      segments: s.slices
        .map((idx, i) => ({ i, start_ms: i * 5000, end_ms: (i + 1) * 5000, text: `w${idx}`, language: 'en', low: false }))
        .filter((seg) => seg.i >= cursor),
      tentative: '',
      gaps: [],
      outcome: done ? (words.length ? 'transcribed' : 'no_speech') : null,
      text: done ? words.join(' ') : null,
      language: done ? 'English' : null,
      language_code: done ? 'en' : null,
      speech_ms: s.slices.length * 5000,
      ended_by: s.endedBy,
      continues_session_id: s.continues,
      stored: { kept: true, retention_days: 0, delete_after: null, bytes: s.slices.length * EDGE_SLICE },
      error: null,
      ...(done ? this.finalState(s) : {}),
    };
  }

  private owned(id: string, allowCancelled = false): EdgeSession | null {
    const s = this.sessions.get(id);
    if (!s || s.user !== this.signedIn) return null;
    if (s.status === 'cancelled' && !allowCancelled) return null;
    return s;
  }

  private route(method: string, url: URL, init: RequestInit): Response {
    const path = url.pathname;
    if (path === '/api/auth/me') {
      if (!this.signedIn) return reply(401, { detail: 'Not authenticated' });
      const id = Number(this.signedIn.slice(1));
      return reply(200, { username: `person${id}`, user: { id, name: `Person ${id}`, email: `p${id}@example.test` } });
    }
    if (!this.signedIn) return reply(401, { detail: 'Not authenticated' });
    if (method === 'POST' && path === '/api/audio/sessions') {
      const body = JSON.parse(String(init.body ?? '{}')) as Record<string, unknown>;
      const same = [...this.sessions.values()].find(
        (s) => s.user === this.signedIn && s.clientKey === body.client_key,
      );
      if (same) {
        if (same.status === 'cancelled') return reply(404, { detail: 'gone', reason: 'not_found' });
        return reply(200, { ...this.state(same), config: { part_ms: 5000 } });
      }
      if (body.continues && this.refuseContinuation) {
        return reply(this.refuseContinuation.status, { detail: 'no', reason: this.refuseContinuation.reason });
      }
      if (typeof body.continues === 'string') {
        // `_continuation_of`: only an idle-closed session of your own, once.
        const prev = this.sessions.get(body.continues);
        if (!prev || prev.user !== this.signedIn || prev.status === 'cancelled') {
          return reply(404, { detail: 'gone', reason: 'not_found' });
        }
        if (prev.endedBy !== 'idle' || prev.status === 'recording') {
          return reply(409, { detail: 'no', reason: 'not_continuable', session_id: prev.id, ended_by: prev.endedBy });
        }
        const other = [...this.sessions.values()].find((x) => x.continues === prev.id && x.status !== 'cancelled');
        if (other) return reply(409, { detail: 'continued', reason: 'already_continued', session_id: other.id });
      }
      if (this.capacityFull) return reply(503, { detail: 'full', reason: 'capacity_full' });
      const live = this.only(this.signedIn).find((s) => s.status === 'recording');
      if (live) {
        return reply(409, {
          detail: "You're already recording in another tab or on another device.",
          reason: 'session_active',
          session_id: live.id,
          audio_ms: live.slices.length * 5000,
        });
      }
      this.n += 1;
      const s: EdgeSession = {
        id: this.n.toString(16).padStart(32, '0'),
        user: this.signedIn,
        clientKey: String(body.client_key),
        status: 'recording',
        endedBy: null,
        continues: typeof body.continues === 'string' ? body.continues : null,
        nextPart: 0,
        partSha: new Map(),
        slices: [],
        bytes: [],
        storedBytes: 0,
        front: null,
        lastPartAt: Date.now(),
        rev: 0,
        polls: 0,
        deletes: 0,
      };
      this.sessions.set(s.id, s);
      return reply(201, { ...this.state(s), config: { part_ms: 5000, idle_close_s: this.idleS } });
    }
    const m = path.match(/^\/api\/audio\/sessions\/([0-9a-f]{32})(\/.*)?$/);
    if (!m) return reply(404, { detail: 'no route', reason: 'not_found' });
    const [, id, rest = ''] = m;
    if (method === 'DELETE') {
      const s = this.owned(id!, true);
      if (!s) return reply(404, { detail: 'gone', reason: 'not_found' });
      s.status = 'cancelled';
      s.deletes += 1;
      return reply(204);
    }
    const s = this.owned(id!);
    if (!s) return reply(404, { detail: 'gone', reason: 'not_found' });
    const cursor = Number(url.searchParams.get('cursor') ?? 0);
    const partMatch = rest.match(/^\/parts\/(\d+)$/);
    if (method === 'PUT' && partMatch) {
      const seq = Number(partMatch[1]);
      const raw = init.body as ArrayBuffer;
      const bytes = new Uint8Array(raw);
      const sha = createHash('sha256').update(bytes).digest('hex');
      if (sha !== (init.headers as Record<string, string>)['x-part-sha256']) {
        return reply(422, { detail: 'corrupt', reason: 'part_corrupt' });
      }
      if (seq < s.nextPart) {
        if (s.partSha.get(seq) === sha) return reply(200, { accepted: seq, duplicate: true, ...this.state(s, cursor) });
        return reply(409, { detail: 'conflict', reason: 'part_conflict', next_part: s.nextPart });
      }
      if (s.status !== 'recording') {
        return reply(409, { detail: 'closed', reason: 'session_closed', status: s.status, ended_by: s.endedBy });
      }
      if (seq > s.nextPart) return reply(409, { detail: 'gap', reason: 'out_of_order', next_part: s.nextPart });
      let from = 0;
      // A recording of its own that continues held audio opens with a front
      // (the test's stand-in init segment is 0xEE bytes): recorded, skipped.
      if (seq === 0 && this.frontBytes > 0 && bytes[0] === 0xee) {
        s.front = bytes.slice(0, this.frontBytes);
        from = this.frontBytes;
      }
      for (let off = from; off + 4 <= bytes.byteLength; off += EDGE_SLICE) {
        s.slices.push(bytes[off]! | (bytes[off + 1]! << 8) | (bytes[off + 2]! << 16) | (bytes[off + 3]! << 24));
      }
      if (this.keepBytes) s.bytes.push(bytes.slice());
      s.storedBytes += bytes.byteLength;
      s.partSha.set(seq, sha);
      s.nextPart += 1;
      s.lastPartAt = Date.now();
      s.rev += 1;
      return reply(200, { accepted: seq, duplicate: false, ...this.state(s, cursor) });
    }
    if (method === 'POST' && rest === '/finish') {
      const body = JSON.parse(String(init.body ?? '{}')) as { last_part?: number | null; ended_by?: string };
      if (s.status === 'recording') {
        if (typeof body.last_part === 'number' && body.last_part >= s.nextPart) {
          return reply(409, { detail: 'missing', reason: 'parts_missing', next_part: s.nextPart, last_part: body.last_part });
        }
        s.status = 'finishing';
        s.endedBy = body.ended_by ?? 'person';
        s.rev += 1;
        return reply(202, this.state(s, cursor));
      }
      return reply(200, this.state(s, cursor));
    }
    if (method === 'POST' && rest === '/retranscribe') {
      if (s.status === 'recording' || s.status === 'finishing') {
        return reply(409, { detail: 'busy', reason: 'session_busy' });
      }
      s.status = 'finishing';
      s.polls = 0;
      s.rev += 1;
      return reply(202, this.state(s, cursor));
    }
    if (method === 'GET' && rest === '') {
      if (s.status === 'finishing') {
        s.polls += 1;
        if (s.polls > this.pollsBeforeDone) {
          s.status = 'done';
          s.rev += 1;
        }
      }
      return reply(200, this.state(s, cursor));
    }
    return reply(404, { detail: 'no route', reason: 'not_found' });
  }
}
