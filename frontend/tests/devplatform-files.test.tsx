// @vitest-environment jsdom
/**
 * The developer console's Files tab.
 *
 * Three things are pinned here, in the order they would cost the most to get
 * wrong:
 *
 *  1. AN UPLOAD SURVIVES A DROPPED CONNECTION. A part whose answer was lost is
 *     asked about before it is sent again; a connection that stays down
 *     PAUSES the upload with its parts kept; a later run — this tab or a
 *     reload — sends only what the server does not hold, and a `complete`
 *     that landed without its answer is finished from the server's record,
 *     never sent again as a second file. Whether an upload can continue is
 *     the server's answer, not a clock in the browser. Browser storage keeps
 *     an upload id and a part size, never the file's name; at most two
 *     uploads send parts at once.
 *  2. A FILE'S STATE IS THE SERVER'S. The list says Queued / Processing n/N /
 *     Processed / Failed in words, follows the events stream to the terminal
 *     frame (and reads the file when there is no stream), and draws only the
 *     facts, stages and derived names the allowlists know — no download link.
 *  3. THE TABLE BEHAVES LIKE THE REST OF THE CONSOLE. A failed load is the
 *     server's sentence with Retry; delete asks first and names the file; the
 *     table keeps its row menu reachable on a 400px phone and fits the column
 *     from 1024px to 1920px.
 *
 * fetch is mocked at the console BFF (`/api/devplatform/…`), the one door
 * the tab uses. Every clock the uploader and the follower wait on is injected,
 * so nothing here sleeps.
 */
import { act, cleanup, configure, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { FilesPanel } from '@/components/devplatform/Files';
import {
  FILES_CONSOLE_OPERATIONS,
  FileUploader,
  MAX_AUTO_RETRY_AFTER_S,
  CANCELLING_SENTENCE,
  RECORD_STALE_AFTER_S,
  UNREACHABLE_CANCEL_SENTENCE,
  fileFacts,
  fileStatus,
  filesOperationFor,
  filesPaths,
  followFileEvents,
  kindLabel,
  resumeKey,
  retryableAnswer,
  serverFilename,
  type ConsoleFile,
  type KeyValueStore,
  type UploadObject,
  type UploadSnapshot,
} from '@/components/devplatform/files-api';
import { formatBytes, formatDay } from '@/lib/format';
import type { Me } from '@/components/admin/api';

configure({ asyncUtilTimeout: 5000 });

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

const PROJECT = {
  id: 'proj_0123456789abcdef01234567',
  name: 'Billing assistant',
  environment: 'test',
  status: 'active',
  allowed_models: [],
  allowed_origins: [],
  ip_allowlist: [],
  retention_days: null,
  limits: {
    rpm: null,
    input_tpm: null,
    output_tpm: null,
    max_concurrency: null,
    daily_token_quota: null,
    max_input_tokens: null,
    max_output_tokens: null,
  },
  created_at: null,
  disabled_at: null,
};

const ADMIN: Me = {
  user: { id: 1, name: 'Grace Hopper', email: 'grace@corp.com' },
  workspace: { id: 'w1', name: 'Corp Workspace', role: 'admin' },
  capabilities: ['api.console.access', 'api.projects.read', 'api.projects.manage'],
  features: {},
};

const READER: Me = { ...ADMIN, capabilities: ['api.console.access', 'api.projects.read'] };

const FILE_ID = 'file-00000000000000000000aaaa';
const UPLOAD_ID = 'upload_00000000000000000000bbbb';
const CREATED = 1_789_300_000;

function processing(overrides: Partial<NonNullable<ConsoleFile['processing']>> = {}) {
  return {
    state: 'processed' as const,
    kind: 'pdf',
    stage: 'finalize',
    step: 6,
    total_steps: 6,
    percent: 100,
    stages: [
      { name: 'sniff', status: 'done' as const },
      { name: 'text', status: 'done' as const },
      { name: 'ocr', status: 'done' as const },
      { name: 'chunk', status: 'done' as const },
      { name: 'index', status: 'done' as const },
      { name: 'finalize', status: 'done' as const },
    ],
    queue_position: null,
    waited_for_capacity_s: 0,
    started_at: CREATED + 5,
    finished_at: CREATED + 125,
    error: null,
    facts: { pages: 12, ocr_pages: 2 },
    derived: ['text.txt', 'pages.json'],
    ...overrides,
  };
}

function fileOf(overrides: Partial<ConsoleFile> = {}): ConsoleFile {
  return {
    id: FILE_ID,
    object: 'file',
    bytes: 2_400_000,
    created_at: CREATED,
    filename: 'quarterly-report.pdf',
    purpose: 'user_data',
    status: 'processed',
    status_details: null,
    expires_at: null,
    mime_type: 'application/pdf',
    processing: processing(),
    ...overrides,
  };
}

const RUNNING = fileOf({
  id: 'file-00000000000000000000cccc',
  filename: 'board-meeting.mp4',
  bytes: 734_000_000,
  status: 'uploaded',
  expires_at: CREATED + 30 * 86400,
  processing: processing({
    state: 'processing',
    kind: 'video',
    stage: 'transcript',
    step: 4,
    total_steps: 11,
    percent: 31,
    stages: [],
    finished_at: null,
    facts: {},
    derived: [],
  }),
});

const listOf = (data: ConsoleFile[], hasMore = false) => ({
  object: 'list',
  data,
  has_more: hasMore,
  first_id: data[0]?.id ?? null,
  last_id: data[data.length - 1]?.id ?? null,
});

const STORAGE = { files: 2, bytes: 736_400_000, derived_bytes: 1_200_000, uploads_pending: 0, uploads_pending_bytes: 0 };

function uploadOf(overrides: Partial<UploadObject> = {}): UploadObject {
  return {
    id: UPLOAD_ID,
    object: 'upload',
    bytes: 10,
    filename: 'notes.txt',
    purpose: 'user_data',
    status: 'pending',
    expires_at: Math.floor(Date.now() / 1000) + 3600,
    file: null,
    part_max_bytes: 64 * 1024 * 1024,
    max_parts: 10_000,
    bytes_received: 0,
    ...overrides,
  };
}

const partOf = (n: number, bytes: number) => ({
  id: `part_${n}`,
  object: 'upload.part',
  part_number: n,
  bytes,
  sha256: null,
});

// ---------------------------------------------------------------------------
// The mocked BFF
// ---------------------------------------------------------------------------

interface Call {
  method: string;
  path: string;
  query: string;
  init: RequestInit;
}

type Handler = (req: { init: RequestInit; count: number }) => Response | Promise<Response>;

/** Every console path and method any test in this file actually sent. */
const EXERCISED: [string, string][] = [];

const json = (body: unknown, status = 200, headers: Record<string, string> = {}) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json', ...headers },
  });

const offline = (): never => {
  throw new TypeError('Failed to fetch');
};

/** Answer `METHOD path` from a table; anything else is the BFF's own 404. */
function route(table: Record<string, Handler>): Call[] {
  const calls: Call[] = [];
  const counts = new Map<string, number>();
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string | URL, init: RequestInit = {}) => {
      const full = String(url);
      const method = (init.method ?? 'GET').toUpperCase();
      const [path = '', query = ''] = full.replace('/api/devplatform/', '').split('?');
      const key = `${method} ${path}`;
      const count = (counts.get(key) ?? 0) + 1;
      counts.set(key, count);
      calls.push({ method, path, query, init });
      EXERCISED.push([path, method]);
      const handler = table[key];
      if (!handler) return json({ message: 'Unknown console endpoint.' }, 404);
      return handler({ init, count });
    }),
  );
  return calls;
}

const encoder = new TextEncoder();
const frame = (name: string, sequence: number, file: ConsoleFile) =>
  `event: ${name}\ndata: ${JSON.stringify({ type: name, sequence_number: sequence, data: file })}\n\n`;

/** An SSE answer: these frames, then either the end of the stream or silence until aborted. */
function sse(init: RequestInit, frames: string[], { close = true } = {}): Response {
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const text of frames) controller.enqueue(encoder.encode(text));
      if (close) controller.close();
      else
        init.signal?.addEventListener('abort', () => {
          try {
            controller.error(Object.assign(new Error('aborted'), { name: 'AbortError' }));
          } catch {
            /* already closed */
          }
        });
    },
  });
  return new Response(body, { status: 200, headers: { 'content-type': 'text/event-stream' } });
}

/** An answer that never comes, and fails the request the moment it is aborted. */
function held(init: RequestInit): Promise<Response> {
  return new Promise((_, reject) => {
    init.signal?.addEventListener('abort', () => reject(Object.assign(new Error('aborted'), { name: 'AbortError' })), {
      once: true,
    });
  });
}

/** A sleep that never ends on its own and gives up the moment it is aborted. */
function stalled(_ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((_, reject) => {
    signal.addEventListener('abort', () => reject(Object.assign(new Error('aborted'), { name: 'AbortError' })), {
      once: true,
    });
  });
}

const instant = async () => undefined;

function memoryStore(): KeyValueStore & { data: Map<string, string> } {
  const data = new Map<string, string>();
  return {
    data,
    getItem: (k) => data.get(k) ?? null,
    setItem: (k, v) => void data.set(k, v),
    removeItem: (k) => void data.delete(k),
  };
}

const tenBytes = () =>
  new File([new Uint8Array([1, 2, 3, 4, 5, 6, 7, 8, 9, 10])], 'notes.txt', {
    type: 'text/plain',
    lastModified: 1_789_000_000_000,
  });

beforeEach(() => {
  try {
    window.localStorage.clear();
  } catch {
    /* no storage in this environment */
  }
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

// ---------------------------------------------------------------------------
// 1. The uploader
// ---------------------------------------------------------------------------

describe('uploading a file in parts', () => {
  const P = PROJECT.id;

  it('asks the server what it holds after a dropped part and does not send a part that already landed', async () => {
    const calls = route({
      [`POST ${filesPaths.uploads(P)}`]: () => json(uploadOf()),
      [`PUT ${filesPaths.uploadPart(P, UPLOAD_ID, 0)}`]: () => json(partOf(0, 4)),
      // The bytes of part 1 arrive; the answer does not.
      [`PUT ${filesPaths.uploadPart(P, UPLOAD_ID, 1)}`]: offline,
      [`GET ${filesPaths.upload(P, UPLOAD_ID)}`]: () =>
        json(uploadOf({ parts: [partOf(0, 4), partOf(1, 4)] as UploadObject['parts'] })),
      [`PUT ${filesPaths.uploadPart(P, UPLOAD_ID, 2)}`]: () => json(partOf(2, 2)),
      [`POST ${filesPaths.completeUpload(P, UPLOAD_ID)}`]: () =>
        json(uploadOf({ status: 'completed', file: fileOf({ status: 'uploaded' }) })),
    });
    const store = memoryStore();
    const states: UploadSnapshot['state'][] = [];
    const uploader = new FileUploader(P, tenBytes(), (s) => states.push(s.state), {
      partBytes: 4,
      sleep: instant,
      random: () => 0,
      store,
      hash: async () => 'ab'.repeat(32),
    });

    const done = await uploader.run();

    expect(done.state).toBe('uploaded');
    expect(done.file?.id).toBe(FILE_ID);
    expect(done).toMatchObject({ partsDone: 3, partsTotal: 3, bytesConfirmed: 10 });
    expect(states).toContain('retrying');
    const puts = calls.filter((c) => c.method === 'PUT').map((c) => c.path.split('/').pop());
    expect(puts).toEqual(['0', '1', '2']);
    expect(calls.filter((c) => c.path.endsWith('/complete'))).toHaveLength(1);
    // Every part carries its digest and goes as raw bytes.
    const put = calls.find((c) => c.method === 'PUT')!;
    expect(new Headers(put.init.headers).get('x-part-sha256')).toBe('ab'.repeat(32));
    expect(new Headers(put.init.headers).get('content-type')).toBe('application/octet-stream');
    // A finished upload leaves nothing behind in the browser.
    expect(store.data.size).toBe(0);
  });

  it('pauses when the connection stays down, remembering only an id and a part size, and a later run sends only the missing parts', async () => {
    let network = false;
    const calls = route({
      [`POST ${filesPaths.uploads(P)}`]: () => json(uploadOf()),
      [`PUT ${filesPaths.uploadPart(P, UPLOAD_ID, 0)}`]: () => json(partOf(0, 4)),
      [`PUT ${filesPaths.uploadPart(P, UPLOAD_ID, 1)}`]: () => (network ? json(partOf(1, 4)) : offline()),
      [`PUT ${filesPaths.uploadPart(P, UPLOAD_ID, 2)}`]: () => (network ? json(partOf(2, 2)) : offline()),
      [`GET ${filesPaths.upload(P, UPLOAD_ID)}`]: () =>
        network ? json(uploadOf({ parts: [partOf(0, 4)] as UploadObject['parts'] })) : offline(),
      [`POST ${filesPaths.completeUpload(P, UPLOAD_ID)}`]: () =>
        json(uploadOf({ status: 'completed', file: fileOf({ status: 'uploaded' }) })),
    });
    const store = memoryStore();
    const deps = { partBytes: 4, sleep: instant, random: () => 0, store, hash: async () => null, maxAttempts: 3 };

    const first = await new FileUploader(P, tenBytes(), () => undefined, deps).run();

    expect(first.state).toBe('paused');
    expect(first.message).toBe(
      'The connection dropped. 1 of 3 parts are safe on the server; resume to send the rest.',
    );
    expect(calls.filter((c) => c.method === 'PUT' && c.path.endsWith('/parts/1'))).toHaveLength(3);
    // What the browser keeps: an upload id, a part size and an expiry. No name.
    expect([...store.data.keys()]).toEqual([resumeKey(P, tenBytes())]);
    const [[key, value]] = [...store.data.entries()] as [[string, string]];
    const records = JSON.parse(value) as Record<string, unknown>[];
    expect(records).toHaveLength(1);
    expect(Object.keys(records[0]!).sort()).toEqual(['expires_at', 'part_bytes', 'upload_id']);
    expect(`${key}${value}`).not.toContain('notes');

    // The page reloads; the person picks the same file again, and the network is back.
    network = true;
    const before = calls.length;
    const second = await new FileUploader(P, tenBytes(), () => undefined, deps).run();

    expect(second.state).toBe('uploaded');
    expect(second.resumed).toBe(true);
    const resumedCalls = calls.slice(before).map((c) => `${c.method} ${c.path.replace(`projects/${P}/`, '')}`);
    expect(resumedCalls).toEqual([
      `GET uploads/${UPLOAD_ID}`,
      `PUT uploads/${UPLOAD_ID}/parts/1`,
      `PUT uploads/${UPLOAD_ID}/parts/2`,
      `POST uploads/${UPLOAD_ID}/complete`,
    ]);
    expect(store.data.size).toBe(0);
  });

  it('starts a fresh upload when the remembered one has expired on the server', async () => {
    const store = memoryStore();
    store.setItem(
      resumeKey(P, tenBytes()),
      JSON.stringify({ upload_id: 'upload_00000000000000000000dead', part_bytes: 4, expires_at: null }),
    );
    const calls = route({
      [`GET ${filesPaths.upload(P, 'upload_00000000000000000000dead')}`]: () =>
        json(uploadOf({ id: 'upload_00000000000000000000dead', status: 'expired' })),
      [`POST ${filesPaths.uploads(P)}`]: () => json(uploadOf()),
      [`PUT ${filesPaths.uploadPart(P, UPLOAD_ID, 0)}`]: () => json(partOf(0, 10)),
      [`POST ${filesPaths.completeUpload(P, UPLOAD_ID)}`]: () => json(uploadOf({ status: 'completed', file: fileOf() })),
    });

    const done = await new FileUploader(P, tenBytes(), () => undefined, {
      store,
      sleep: instant,
      hash: async () => null,
    }).run();

    expect(done.state).toBe('uploaded');
    expect(done.resumed).toBe(false);
    expect(calls.map((c) => c.method + ' ' + c.path.split('/').slice(2).join('/'))).toEqual([
      'GET uploads/upload_00000000000000000000dead',
      'POST uploads',
      `PUT uploads/${UPLOAD_ID}/parts/0`,
      `POST uploads/${UPLOAD_ID}/complete`,
    ]);
    // The request body declares the file; the console never sends a checksum it did not compute.
    expect(JSON.parse(String(calls[1]!.init.body))).toEqual({
      bytes: 10,
      filename: 'notes.txt',
      mime_type: 'text/plain',
      purpose: 'user_data',
    });
  });

  it("fails on a refusal with the server's own sentence, sends it once, and forgets the upload", async () => {
    const calls = route({
      [`POST ${filesPaths.uploads(P)}`]: () => json(uploadOf()),
      [`PUT ${filesPaths.uploadPart(P, UPLOAD_ID, 0)}`]: () =>
        json(
          {
            error: {
              type: 'invalid_request_error',
              code: 'upload_state_conflict',
              message: 'This upload is cancelled; it no longer accepts parts.',
            },
          },
          409,
          { 'x-should-retry': 'false' },
        ),
    });
    const store = memoryStore();

    const done = await new FileUploader(P, tenBytes(), () => undefined, {
      store,
      sleep: instant,
      hash: async () => null,
    }).run();

    expect(done.state).toBe('failed');
    expect(done.message).toBe('This upload is cancelled; it no longer accepts parts.');
    expect(calls.filter((c) => c.method === 'PUT')).toHaveLength(1);
    expect(store.data.size).toBe(0);
  });

  it("finishes from the server's own record when a complete landed but its answer was lost, and sends nothing again", async () => {
    let completed = false;
    const calls = route({
      [`POST ${filesPaths.uploads(P)}`]: () => json(uploadOf()),
      [`PUT ${filesPaths.uploadPart(P, UPLOAD_ID, 0)}`]: () => json(partOf(0, 10)),
      // The server completes the upload every time it is asked; no answer reaches the tab.
      [`POST ${filesPaths.completeUpload(P, UPLOAD_ID)}`]: () => {
        completed = true;
        return offline();
      },
      [`GET ${filesPaths.upload(P, UPLOAD_ID)}`]: () =>
        json(
          uploadOf({
            status: completed ? 'completed' : 'pending',
            file: completed ? fileOf({ status: 'uploaded' }) : null,
            parts: [partOf(0, 10)] as UploadObject['parts'],
          }),
        ),
    });
    const store = memoryStore();
    const uploader = new FileUploader(P, tenBytes(), () => undefined, {
      store,
      sleep: instant,
      random: () => 0,
      hash: async () => null,
      maxAttempts: 2,
    });

    const first = await uploader.run();
    expect(first.state).toBe('paused');
    expect(store.data.size).toBe(1);

    const before = calls.length;
    const second = await uploader.run();

    expect(second.state).toBe('uploaded');
    expect(second.file?.id).toBe(FILE_ID);
    expect(second).toMatchObject({ resumed: true, partsDone: 1, partsTotal: 1, bytesConfirmed: 10 });
    expect(calls.slice(before).map((c) => `${c.method} ${c.path}`)).toEqual([`GET ${filesPaths.upload(P, UPLOAD_ID)}`]);
    expect(calls.filter((c) => c.method === 'POST' && c.path === filesPaths.uploads(P))).toHaveLength(1);
    expect(calls.filter((c) => c.method === 'PUT')).toHaveLength(1);
    expect(store.data.size).toBe(0);
  });

  it('asks for the result again, sending no part, when the remembered upload is still being completed', async () => {
    const store = memoryStore();
    store.setItem(resumeKey(P, tenBytes()), JSON.stringify([{ upload_id: UPLOAD_ID, part_bytes: 4, expires_at: null }]));
    const calls = route({
      [`GET ${filesPaths.upload(P, UPLOAD_ID)}`]: () =>
        json(uploadOf({ status: 'finalizing', parts: [partOf(0, 4), partOf(1, 4), partOf(2, 2)] as UploadObject['parts'] })),
      [`POST ${filesPaths.completeUpload(P, UPLOAD_ID)}`]: ({ count }) =>
        count === 1
          ? json(
              { error: { code: 'upload_state_conflict', message: 'This upload is being completed by another request. Retry to receive its result.' } },
              409,
              { 'retry-after': '2', 'x-should-retry': 'true' },
            )
          : json(uploadOf({ status: 'completed', file: fileOf({ status: 'uploaded' }) })),
    });
    const sleeps: number[] = [];

    const done = await new FileUploader(P, tenBytes(), () => undefined, {
      store,
      sleep: async (ms) => void sleeps.push(ms),
      hash: async () => null,
    }).run();

    expect(done.state).toBe('uploaded');
    expect(done.file?.id).toBe(FILE_ID);
    expect(calls.map((c) => `${c.method} ${c.path}`)).toEqual([
      `GET ${filesPaths.upload(P, UPLOAD_ID)}`,
      `POST ${filesPaths.completeUpload(P, UPLOAD_ID)}`,
      `POST ${filesPaths.completeUpload(P, UPLOAD_ID)}`,
    ]);
    expect(sleeps).toEqual([2000]);
    expect(store.data.size).toBe(0);
  });

  it("resumes an upload the server still holds past the expiry this browser first saw, and keeps the server's newer expiry", async () => {
    const start = 1_789_000_000_000;
    let clock = start;
    const firstExpiry = Math.floor(start / 1000) + 24 * 3600;
    const slid = firstExpiry + 20 * 3600;
    let network = false;
    const calls = route({
      [`POST ${filesPaths.uploads(P)}`]: () => json(uploadOf({ expires_at: firstExpiry })),
      [`PUT ${filesPaths.uploadPart(P, UPLOAD_ID, 0)}`]: () => json(partOf(0, 4)),
      [`PUT ${filesPaths.uploadPart(P, UPLOAD_ID, 1)}`]: () => (network ? json(partOf(1, 4)) : offline()),
      [`PUT ${filesPaths.uploadPart(P, UPLOAD_ID, 2)}`]: () => json(partOf(2, 2)),
      // Every accepted part slid the server's expiry forward.
      [`GET ${filesPaths.upload(P, UPLOAD_ID)}`]: () =>
        json(uploadOf({ expires_at: slid, parts: [partOf(0, 4)] as UploadObject['parts'] })),
      [`POST ${filesPaths.completeUpload(P, UPLOAD_ID)}`]: () =>
        json(uploadOf({ status: 'completed', file: fileOf({ status: 'uploaded' }) })),
    });
    const store = memoryStore();
    const deps = { partBytes: 4, sleep: instant, random: () => 0, store, hash: async () => null, maxAttempts: 2, now: () => clock };

    const first = await new FileUploader(P, tenBytes(), () => undefined, deps).run();
    expect(first.state).toBe('paused');
    const [record] = JSON.parse(store.getItem(resumeKey(P, tenBytes())) ?? '[]') as { expires_at: number }[];
    expect(record?.expires_at).toBe(slid);

    // Two days later, past both expiries the browser ever saw; the server still holds the upload.
    clock = start + 50 * 3600 * 1000;
    network = true;
    const before = calls.length;
    const second = await new FileUploader(P, tenBytes(), () => undefined, deps).run();

    expect(second.state).toBe('uploaded');
    expect(second.resumed).toBe(true);
    expect(calls.slice(before).map((c) => `${c.method} ${c.path.replace(`projects/${P}/`, '')}`)).toEqual([
      `GET uploads/${UPLOAD_ID}`,
      `PUT uploads/${UPLOAD_ID}/parts/1`,
      `PUT uploads/${UPLOAD_ID}/parts/2`,
      `POST uploads/${UPLOAD_ID}/complete`,
    ]);
    expect(calls.filter((c) => c.method === 'POST' && c.path === filesPaths.uploads(P))).toHaveLength(1);
  });

  it('drops a record long past any expiry the server allows, without asking about it', async () => {
    const now = 1_789_000_000_000;
    const store = memoryStore();
    store.setItem(
      resumeKey(P, tenBytes()),
      JSON.stringify([
        { upload_id: 'upload_00000000000000000000dead', part_bytes: 4, expires_at: Math.floor(now / 1000) - RECORD_STALE_AFTER_S - 1 },
      ]),
    );
    const calls = route({
      [`POST ${filesPaths.uploads(P)}`]: () => json(uploadOf()),
      [`PUT ${filesPaths.uploadPart(P, UPLOAD_ID, 0)}`]: () => json(partOf(0, 10)),
      [`POST ${filesPaths.completeUpload(P, UPLOAD_ID)}`]: () => json(uploadOf({ status: 'completed', file: fileOf() })),
    });

    const done = await new FileUploader(P, tenBytes(), () => undefined, { store, sleep: instant, hash: async () => null, now: () => now }).run();

    expect(done.state).toBe('uploaded');
    expect(calls.map((c) => `${c.method} ${c.path}`)).toEqual([
      `POST ${filesPaths.uploads(P)}`,
      `PUT ${filesPaths.uploadPart(P, UPLOAD_ID, 0)}`,
      `POST ${filesPaths.completeUpload(P, UPLOAD_ID)}`,
    ]);
    expect(store.data.size).toBe(0);
  });

  it('keeps two files of the same size and time apart, resuming each only onto its own upload', async () => {
    const A = 'upload_000000000000000000000aa1';
    const B = 'upload_000000000000000000000bb2';
    const volume = (name: string) =>
      new File([new Uint8Array([1, 2, 3, 4, 5, 6, 7, 8, 9, 10])], name, { lastModified: 1_789_000_000_000 });
    const store = memoryStore();
    store.setItem(
      resumeKey(P, volume('backup.7z.001')),
      JSON.stringify([
        { upload_id: A, part_bytes: 4, expires_at: null },
        { upload_id: B, part_bytes: 4, expires_at: null },
      ]),
    );
    const calls = route({
      [`GET ${filesPaths.upload(P, A)}`]: () =>
        json(uploadOf({ id: A, filename: 'backup.7z.001', parts: [partOf(0, 4)] as UploadObject['parts'] })),
      [`GET ${filesPaths.upload(P, B)}`]: () =>
        json(uploadOf({ id: B, filename: 'backup.7z.002', parts: [partOf(0, 4), partOf(1, 4)] as UploadObject['parts'] })),
      [`PUT ${filesPaths.uploadPart(P, A, 1)}`]: () => json(partOf(1, 4)),
      [`PUT ${filesPaths.uploadPart(P, A, 2)}`]: () => json(partOf(2, 2)),
      [`POST ${filesPaths.completeUpload(P, A)}`]: () => json(uploadOf({ id: A, status: 'completed', file: fileOf() })),
    });

    const done = await new FileUploader(P, volume('backup.7z.001'), () => undefined, {
      store,
      sleep: instant,
      hash: async () => null,
    }).run();

    expect(done.state).toBe('uploaded');
    expect(done.resumed).toBe(true);
    expect(calls.map((c) => `${c.method} ${c.path}`)).toEqual([
      `GET ${filesPaths.upload(P, B)}`,
      `GET ${filesPaths.upload(P, A)}`,
      `PUT ${filesPaths.uploadPart(P, A, 1)}`,
      `PUT ${filesPaths.uploadPart(P, A, 2)}`,
      `POST ${filesPaths.completeUpload(P, A)}`,
    ]);
    // The other volume's upload is still remembered for when that file is picked.
    expect(JSON.parse(store.getItem(resumeKey(P, volume('backup.7z.002'))) ?? '[]')).toEqual([
      { upload_id: B, part_bytes: 4, expires_at: null },
    ]);
  });

  it('matches a remembered upload by the name the server stored, not the name the browser picked', async () => {
    expect(serverFilename('C:\\Users\\grace\\notes.txt')).toBe('notes.txt');
    expect(serverFilename('re\u200bport\u202e.pdf')).toBe('report.pdf');
    expect(serverFilename('  ')).toBe('upload');
    const long = serverFilename(`${'a'.repeat(300)}.pdf`);
    expect(Array.from(long)).toHaveLength(255);
    expect(long.endsWith('a.pdf')).toBe(true);

    const picked = new File([new Uint8Array(10)], 'drafts\\notes\u200b.txt', { lastModified: 1_789_000_000_000 });
    const store = memoryStore();
    store.setItem(resumeKey(P, picked), JSON.stringify([{ upload_id: UPLOAD_ID, part_bytes: 4, expires_at: null }]));
    const calls = route({
      [`GET ${filesPaths.upload(P, UPLOAD_ID)}`]: () =>
        json(uploadOf({ filename: 'notes.txt', parts: [partOf(0, 4), partOf(1, 4)] as UploadObject['parts'] })),
      [`PUT ${filesPaths.uploadPart(P, UPLOAD_ID, 2)}`]: () => json(partOf(2, 2)),
      [`POST ${filesPaths.completeUpload(P, UPLOAD_ID)}`]: () => json(uploadOf({ status: 'completed', file: fileOf() })),
    });

    const done = await new FileUploader(P, picked, () => undefined, { store, sleep: instant, hash: async () => null }).run();

    expect(done.state).toBe('uploaded');
    expect(done.resumed).toBe(true);
    expect(calls.some((c) => c.method === 'POST' && c.path === filesPaths.uploads(P))).toBe(false);
  });

  it('never lets two uploaders on one page send parts to the same server upload', async () => {
    const OTHER = 'upload_00000000000000000000cccc';
    let releasePart: () => void = () => undefined;
    const calls = route({
      [`GET ${filesPaths.upload(P, UPLOAD_ID)}`]: () => json(uploadOf({ parts: [] })),
      [`PUT ${filesPaths.uploadPart(P, UPLOAD_ID, 0)}`]: () =>
        new Promise<Response>((resolve) => {
          releasePart = () => resolve(json(partOf(0, 10)));
        }),
      [`POST ${filesPaths.completeUpload(P, UPLOAD_ID)}`]: () => json(uploadOf({ status: 'completed', file: fileOf() })),
      [`POST ${filesPaths.uploads(P)}`]: () => json(uploadOf({ id: OTHER })),
      [`PUT ${filesPaths.uploadPart(P, OTHER, 0)}`]: () => json(partOf(0, 10)),
      [`POST ${filesPaths.completeUpload(P, OTHER)}`]: () => json(uploadOf({ id: OTHER, status: 'completed', file: fileOf() })),
    });
    const store = memoryStore();
    store.setItem(resumeKey(P, tenBytes()), JSON.stringify([{ upload_id: UPLOAD_ID, part_bytes: 8 * 1024 * 1024, expires_at: null }]));
    const deps = { store, sleep: instant, hash: async () => null };

    const first = new FileUploader(P, tenBytes(), () => undefined, deps).run();
    await vi.waitFor(() => expect(calls.some((c) => c.method === 'PUT')).toBe(true));
    const second = await new FileUploader(P, tenBytes(), () => undefined, deps).run();
    releasePart();

    expect((await first).state).toBe('uploaded');
    expect(second.state).toBe('uploaded');
    expect(second.uploadId).toBe(OTHER);
    expect(calls.filter((c) => c.method === 'PUT').map((c) => c.path)).toEqual([
      filesPaths.uploadPart(P, UPLOAD_ID, 0),
      filesPaths.uploadPart(P, OTHER, 0),
    ]);
  });

  it('sends a part again when the server holds it at the wrong length', async () => {
    const store = memoryStore();
    store.setItem(resumeKey(P, tenBytes()), JSON.stringify([{ upload_id: UPLOAD_ID, part_bytes: 4, expires_at: null }]));
    const calls = route({
      [`GET ${filesPaths.upload(P, UPLOAD_ID)}`]: () =>
        json(uploadOf({ parts: [partOf(0, 4), partOf(1, 3)] as UploadObject['parts'] })),
      [`PUT ${filesPaths.uploadPart(P, UPLOAD_ID, 1)}`]: () => json(partOf(1, 4)),
      [`PUT ${filesPaths.uploadPart(P, UPLOAD_ID, 2)}`]: () => json(partOf(2, 2)),
      [`POST ${filesPaths.completeUpload(P, UPLOAD_ID)}`]: () => json(uploadOf({ status: 'completed', file: fileOf() })),
    });

    const done = await new FileUploader(P, tenBytes(), () => undefined, { store, sleep: instant, hash: async () => null }).run();

    expect(done.state).toBe('uploaded');
    expect(calls.filter((c) => c.method === 'PUT').map((c) => c.path.split('/').pop())).toEqual(['1', '2']);
  });

  it('fails and forgets the upload when the server says between retries that it no longer takes parts', async () => {
    const calls = route({
      [`POST ${filesPaths.uploads(P)}`]: () => json(uploadOf()),
      [`PUT ${filesPaths.uploadPart(P, UPLOAD_ID, 0)}`]: offline,
      [`GET ${filesPaths.upload(P, UPLOAD_ID)}`]: () => json(uploadOf({ status: 'cancelled' })),
    });
    const store = memoryStore();

    const done = await new FileUploader(P, tenBytes(), () => undefined, {
      store,
      sleep: instant,
      random: () => 0,
      hash: async () => null,
    }).run();

    expect(done.state).toBe('failed');
    expect(done.message).toBe('This upload is cancelled and no longer takes parts. Upload the file again.');
    expect(calls.filter((c) => c.method === 'PUT')).toHaveLength(1);
    expect(store.data.size).toBe(0);
  });

  it('forgets an upload and tells the server when a part is refused, and keeps it for Try again after a server error', async () => {
    let calls = route({
      [`POST ${filesPaths.uploads(P)}`]: () => json(uploadOf()),
      [`PUT ${filesPaths.uploadPart(P, UPLOAD_ID, 0)}`]: () => json({ message: 'The request body is too large.' }, 413),
      [`POST ${filesPaths.cancelUpload(P, UPLOAD_ID)}`]: () => json(uploadOf({ status: 'cancelled' })),
    });
    const refusedStore = memoryStore();

    const refused = await new FileUploader(P, tenBytes(), () => undefined, {
      store: refusedStore,
      sleep: instant,
      hash: async () => null,
    }).run();

    expect(refused.state).toBe('failed');
    expect(refused.message).toBe('The request body is too large.');
    expect(refusedStore.data.size).toBe(0);
    expect(calls.map((c) => `${c.method} ${c.path}`)).toContain(`POST ${filesPaths.cancelUpload(P, UPLOAD_ID)}`);

    calls = route({
      [`POST ${filesPaths.uploads(P)}`]: () => json(uploadOf()),
      [`PUT ${filesPaths.uploadPart(P, UPLOAD_ID, 0)}`]: ({ count }) =>
        count === 1 ? json({ message: 'Something went wrong on our side.' }, 500) : json(partOf(0, 10)),
      [`GET ${filesPaths.upload(P, UPLOAD_ID)}`]: () => json(uploadOf({ parts: [] })),
      [`POST ${filesPaths.completeUpload(P, UPLOAD_ID)}`]: () => json(uploadOf({ status: 'completed', file: fileOf() })),
    });
    const store = memoryStore();
    const uploader = new FileUploader(P, tenBytes(), () => undefined, { store, sleep: instant, hash: async () => null });

    const crashed = await uploader.run();
    expect(crashed.state).toBe('failed');
    expect(store.data.size).toBe(1);
    expect(calls.some((c) => c.path.endsWith('/cancel'))).toBe(false);

    const again = await uploader.run();
    expect(again.state).toBe('uploaded');
    expect(again.resumed).toBe(true);
    expect(calls.filter((c) => c.method === 'POST' && c.path === filesPaths.uploads(P))).toHaveLength(1);
  });

  it('cancels the upload it was about to continue on the server, even when Cancel lands while the server is still being asked about it', async () => {
    const OTHER = 'upload_00000000000000000000dddd';
    const store = memoryStore();
    // Oldest first: this file's upload is the newest, so it is asked about first.
    store.setItem(
      resumeKey(P, tenBytes()),
      JSON.stringify([
        { upload_id: OTHER, part_bytes: 4, expires_at: null },
        { upload_id: UPLOAD_ID, part_bytes: 4, expires_at: null },
      ]),
    );
    const calls = route({
      [`GET ${filesPaths.upload(P, UPLOAD_ID)}`]: ({ init, count }) =>
        count === 1 ? held(init) : json(uploadOf({ parts: [partOf(0, 4)] as UploadObject['parts'] })),
      // Another file with the same size and time: not this cancel's to drop.
      [`GET ${filesPaths.upload(P, OTHER)}`]: () => json(uploadOf({ id: OTHER, filename: 'other-notes.txt' })),
      [`POST ${filesPaths.cancelUpload(P, UPLOAD_ID)}`]: () => json(uploadOf({ status: 'cancelled' })),
    });
    const uploader = new FileUploader(P, tenBytes(), () => undefined, { store, sleep: instant, hash: async () => null });

    const running = uploader.run();
    await vi.waitFor(() => expect(calls).toHaveLength(1));
    expect(uploader.snapshot).toMatchObject({ state: 'preparing', uploadId: null });
    await uploader.cancel();
    await running;

    expect(uploader.snapshot).toMatchObject({ state: 'cancelled', message: null });
    expect(calls.map((c) => `${c.method} ${c.path}`)).toContain(`POST ${filesPaths.cancelUpload(P, UPLOAD_ID)}`);
    expect(calls.some((c) => c.method === 'PUT' || c.path === filesPaths.cancelUpload(P, OTHER))).toBe(false);
    expect(JSON.parse(store.getItem(resumeKey(P, tenBytes())) ?? '[]')).toEqual([
      { upload_id: OTHER, part_bytes: 4, expires_at: null },
    ]);
  });

  it('cancels on the server the upload a paused resume was continuing, and says so when the server cannot be reached', async () => {
    const scenario = async (cancelReachable: boolean) => {
      let network = true;
      const calls = route({
        [`POST ${filesPaths.uploads(P)}`]: () => json(uploadOf()),
        [`PUT ${filesPaths.uploadPart(P, UPLOAD_ID, 0)}`]: () => json(partOf(0, 4)),
        [`PUT ${filesPaths.uploadPart(P, UPLOAD_ID, 1)}`]: offline,
        [`GET ${filesPaths.upload(P, UPLOAD_ID)}`]: () =>
          network ? json(uploadOf({ parts: [partOf(0, 4)] as UploadObject['parts'] })) : offline(),
        [`POST ${filesPaths.cancelUpload(P, UPLOAD_ID)}`]: () =>
          cancelReachable ? json(uploadOf({ status: 'cancelled' })) : offline(),
      });
      const store = memoryStore();
      const uploader = new FileUploader(P, tenBytes(), () => undefined, {
        store,
        partBytes: 4,
        sleep: instant,
        random: () => 0,
        hash: async () => null,
        maxAttempts: 2,
      });
      expect((await uploader.run()).state).toBe('paused');
      // Resume on a link that is still down: the run pauses again before it gets an answer.
      network = false;
      const again = await uploader.run();
      expect(again).toMatchObject({ state: 'paused', uploadId: UPLOAD_ID });

      // Pressed twice: the server is told once.
      await Promise.all([uploader.cancel(), uploader.cancel()]);

      expect(calls.filter((c) => c.path === filesPaths.cancelUpload(P, UPLOAD_ID))).toHaveLength(1);
      expect(store.data.size).toBe(0);
      return uploader.snapshot;
    };

    expect(await scenario(true)).toMatchObject({ state: 'cancelled', message: null });
    expect(await scenario(false)).toMatchObject({ state: 'cancelled', message: UNREACHABLE_CANCEL_SENTENCE });
  });

  it('tells the server once and says one thing when Cancel is pressed twice while parts are being sent', async () => {
    const calls = route({
      [`POST ${filesPaths.uploads(P)}`]: () => json(uploadOf()),
      // The part never answers; the cancel cannot reach the server.
      [`PUT ${filesPaths.uploadPart(P, UPLOAD_ID, 0)}`]: ({ init }) => held(init),
      [`POST ${filesPaths.cancelUpload(P, UPLOAD_ID)}`]: offline,
    });
    const messages: (string | null)[] = [];
    const uploader = new FileUploader(
      P,
      tenBytes(),
      (snap) => {
        if (snap.state === 'cancelled') messages.push(snap.message);
      },
      { store: memoryStore(), sleep: instant, hash: async () => null },
    );
    const running = uploader.run();
    await vi.waitFor(() => expect(calls.some((c) => c.method === 'PUT')).toBe(true));

    await Promise.all([uploader.cancel(), uploader.cancel()]);
    await running;

    expect(messages).toEqual([CANCELLING_SENTENCE, UNREACHABLE_CANCEL_SENTENCE]);
    expect(calls.filter((c) => c.path === filesPaths.cancelUpload(P, UPLOAD_ID))).toHaveLength(1);
  });

  it('keeps the file, and cancels nothing, when the upload finished in the moment before Cancel reached it', async () => {
    let answerPart: () => void = () => undefined;
    const calls = route({
      [`POST ${filesPaths.uploads(P)}`]: () => json(uploadOf()),
      // These answers were already on their way: the abort does not stop them.
      [`PUT ${filesPaths.uploadPart(P, UPLOAD_ID, 0)}`]: () =>
        new Promise<Response>((resolve) => {
          answerPart = () => resolve(json(partOf(0, 10)));
        }),
      [`POST ${filesPaths.completeUpload(P, UPLOAD_ID)}`]: () => json(uploadOf({ status: 'completed', file: fileOf() })),
      [`POST ${filesPaths.cancelUpload(P, UPLOAD_ID)}`]: () => json(uploadOf({ status: 'cancelled' })),
    });
    const uploader = new FileUploader(P, tenBytes(), () => undefined, { store: memoryStore(), sleep: instant, hash: async () => null });
    const running = uploader.run();
    await vi.waitFor(() => expect(calls.some((c) => c.method === 'PUT')).toBe(true));

    const cancelled = uploader.cancel();
    answerPart();
    await cancelled;
    await running;

    expect(uploader.snapshot.state).toBe('uploaded');
    expect(uploader.snapshot.file?.id).toBe(FILE_ID);
    expect(calls.some((c) => c.path === filesPaths.cancelUpload(P, UPLOAD_ID))).toBe(false);
  });

  it('continues the upload its own earlier run was sending, before any newer one remembered for the same file', async () => {
    const NEWER = 'upload_00000000000000000000ffff';
    let network = true;
    const calls = route({
      [`POST ${filesPaths.uploads(P)}`]: () => json(uploadOf()),
      [`PUT ${filesPaths.uploadPart(P, UPLOAD_ID, 0)}`]: () => json(partOf(0, 4)),
      [`PUT ${filesPaths.uploadPart(P, UPLOAD_ID, 1)}`]: () => (network ? json(partOf(1, 4)) : offline()),
      [`PUT ${filesPaths.uploadPart(P, UPLOAD_ID, 2)}`]: () => json(partOf(2, 2)),
      [`GET ${filesPaths.upload(P, UPLOAD_ID)}`]: () =>
        network ? json(uploadOf({ parts: [partOf(0, 4)] as UploadObject['parts'] })) : offline(),
      // Another tab started its own upload of the same file meanwhile.
      [`GET ${filesPaths.upload(P, NEWER)}`]: () => json(uploadOf({ id: NEWER, parts: [] })),
      [`POST ${filesPaths.completeUpload(P, UPLOAD_ID)}`]: () => json(uploadOf({ status: 'completed', file: fileOf() })),
    });
    const store = memoryStore();
    const uploader = new FileUploader(P, tenBytes(), () => undefined, {
      store,
      partBytes: 4,
      sleep: instant,
      random: () => 0,
      hash: async () => null,
      maxAttempts: 2,
    });
    network = false;
    expect((await uploader.run()).state).toBe('paused');
    const key = resumeKey(P, tenBytes());
    store.setItem(key, JSON.stringify([...JSON.parse(store.getItem(key) ?? '[]'), { upload_id: NEWER, part_bytes: 4, expires_at: null }]));

    network = true;
    const before = calls.length;
    const done = await uploader.run();

    expect(done).toMatchObject({ state: 'uploaded', uploadId: UPLOAD_ID });
    expect(calls.slice(before).map((c) => `${c.method} ${c.path}`)).toEqual([
      `GET ${filesPaths.upload(P, UPLOAD_ID)}`,
      `PUT ${filesPaths.uploadPart(P, UPLOAD_ID, 1)}`,
      `PUT ${filesPaths.uploadPart(P, UPLOAD_ID, 2)}`,
      `POST ${filesPaths.completeUpload(P, UPLOAD_ID)}`,
    ]);
  });

  it("forgets another file's remembered upload with the same size and time once the server says it is dead, and keeps a live one", async () => {
    const DEAD = 'upload_000000000000000000000de1';
    const LIVE = 'upload_000000000000000000000a11';
    const store = memoryStore();
    store.setItem(
      resumeKey(P, tenBytes()),
      JSON.stringify([
        { upload_id: LIVE, part_bytes: 4, expires_at: null },
        { upload_id: DEAD, part_bytes: 4, expires_at: null },
      ]),
    );
    route({
      [`GET ${filesPaths.upload(P, DEAD)}`]: () => json(uploadOf({ id: DEAD, filename: 'volume.002', status: 'cancelled' })),
      [`GET ${filesPaths.upload(P, LIVE)}`]: () => json(uploadOf({ id: LIVE, filename: 'volume.003' })),
      [`POST ${filesPaths.uploads(P)}`]: () => json(uploadOf()),
      [`PUT ${filesPaths.uploadPart(P, UPLOAD_ID, 0)}`]: () => json(partOf(0, 10)),
      [`POST ${filesPaths.completeUpload(P, UPLOAD_ID)}`]: () => json(uploadOf({ status: 'completed', file: fileOf() })),
    });

    const done = await new FileUploader(P, tenBytes(), () => undefined, { store, sleep: instant, hash: async () => null }).run();

    expect(done.state).toBe('uploaded');
    expect(JSON.parse(store.getItem(resumeKey(P, tenBytes())) ?? '[]')).toEqual([{ upload_id: LIVE, part_bytes: 4, expires_at: null }]);
  });

  it('starts a fresh upload when the remembered upload finished but its file has been deleted since', async () => {
    const NEW = 'upload_00000000000000000000eeee';
    const store = memoryStore();
    store.setItem(resumeKey(P, tenBytes()), JSON.stringify([{ upload_id: UPLOAD_ID, part_bytes: 4, expires_at: null }]));
    const calls = route({
      // The resume view reads only a live file: a completed upload whose file was deleted says `file: null`.
      [`GET ${filesPaths.upload(P, UPLOAD_ID)}`]: () => json(uploadOf({ status: 'completed', file: null })),
      [`POST ${filesPaths.completeUpload(P, UPLOAD_ID)}`]: () =>
        json(uploadOf({ status: 'completed', file: fileOf({ status: 'uploaded' }) })),
      [`POST ${filesPaths.uploads(P)}`]: () => json(uploadOf({ id: NEW })),
      [`PUT ${filesPaths.uploadPart(P, NEW, 0)}`]: () => json(partOf(0, 10)),
      [`POST ${filesPaths.completeUpload(P, NEW)}`]: () =>
        json(uploadOf({ id: NEW, status: 'completed', file: fileOf({ id: 'file-00000000000000000000ffff' }) })),
    });

    const done = await new FileUploader(P, tenBytes(), () => undefined, { store, sleep: instant, hash: async () => null }).run();

    expect(done).toMatchObject({ state: 'uploaded', resumed: false, uploadId: NEW });
    expect(done.file?.id).toBe('file-00000000000000000000ffff');
    expect(calls.map((c) => `${c.method} ${c.path}`)).toEqual([
      `GET ${filesPaths.upload(P, UPLOAD_ID)}`,
      `POST ${filesPaths.uploads(P)}`,
      `PUT ${filesPaths.uploadPart(P, NEW, 0)}`,
      `POST ${filesPaths.completeUpload(P, NEW)}`,
    ]);
    expect(store.data.size).toBe(0);
  });

  it('waits out a short Retry-After as the server being busy, and pauses on a long one with the wait in words instead of sleeping through it', async () => {
    let retryAfter = '3';
    route({
      [`POST ${filesPaths.uploads(P)}`]: ({ count }) =>
        count === 1
          ? json({ error: { code: 'rate_limit_exceeded', message: 'Slow down.' } }, 429, { 'retry-after': retryAfter })
          : json(uploadOf()),
      [`PUT ${filesPaths.uploadPart(P, UPLOAD_ID, 0)}`]: () => json(partOf(0, 10)),
      [`POST ${filesPaths.completeUpload(P, UPLOAD_ID)}`]: () => json(uploadOf({ status: 'completed', file: fileOf() })),
    });
    const sleeps: number[] = [];
    const seen: UploadSnapshot[] = [];
    const short = await new FileUploader(P, tenBytes(), (snap) => seen.push(snap), {
      store: null,
      sleep: async (ms) => void sleeps.push(ms),
      hash: async () => null,
    }).run();

    expect(short.state).toBe('uploaded');
    expect(sleeps).toEqual([3000]);
    expect(seen.find((snap) => snap.state === 'retrying')).toMatchObject({ retryInS: 3, retryReason: 'busy' });

    retryAfter = String(MAX_AUTO_RETRY_AFTER_S + 86_370);
    route({
      [`POST ${filesPaths.uploads(P)}`]: () =>
        json({ error: { code: 'rate_limit_exceeded', message: 'Slow down.' } }, 429, { 'retry-after': retryAfter }),
    });
    sleeps.length = 0;
    const long = await new FileUploader(P, tenBytes(), () => undefined, {
      store: null,
      sleep: async (ms) => void sleeps.push(ms),
      hash: async () => null,
    }).run();

    expect(long.state).toBe('paused');
    expect(long.message).toBe('The server is busy and asked for 24 h 0 min before the next request. Resume to try again.');
    expect(sleeps).toEqual([]);
  });

  it('tells a transient failure from a refusal the way the Files codes mark them', () => {
    // x-should-retry decides when it is there.
    expect(retryableAnswer(503, 'true', 2)).toBe(true);
    expect(retryableAnswer(503, 'false', 60)).toBe(false);
    // Without it: offline, 408 incomplete_body and 429 go again...
    expect(retryableAnswer(0, null, null)).toBe(true);
    expect(retryableAnswer(408, null, null)).toBe(true);
    expect(retryableAnswer(429, null, 3)).toBe(true);
    // ...a busy complete (409, Retry-After 2) goes again, a wrong-state 409 does not...
    expect(retryableAnswer(409, null, 2)).toBe(true);
    expect(retryableAnswer(409, null, null)).toBe(false);
    // ...and a full disk (503, Retry-After 60) will not have emptied by the next try.
    expect(retryableAnswer(503, null, 60)).toBe(false);
    expect(retryableAnswer(400, null, null)).toBe(false);
    expect(retryableAnswer(404, null, null)).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// 2. Following a file's processing
// ---------------------------------------------------------------------------

describe("following a file's processing", () => {
  const P = PROJECT.id;
  const queued = fileOf({ status: 'uploaded', processing: processing({ state: 'queued', stage: 'sniff', step: 1, percent: 0 }) });
  const midway = fileOf({ status: 'uploaded', processing: processing({ state: 'processing', stage: 'ocr', step: 3, percent: 40 }) });

  it('reads the events stream to its terminal frame and stops there', async () => {
    const calls = route({
      [`GET ${filesPaths.fileEvents(P, FILE_ID)}`]: ({ init }) =>
        sse(init, [frame('file.processing', 1, midway), ': ping\n\n', frame('file.processed', 2, fileOf())]),
    });
    const seen: string[] = [];

    const last = await followFileEvents(P, FILE_ID, {
      signal: new AbortController().signal,
      onFile: (f) => seen.push(fileStatus(f).label),
      sleep: instant,
    });

    expect(seen).toEqual(['Processing 3/6', 'Processed']);
    expect(last?.processing?.state).toBe('processed');
    expect(new Headers(calls[0]!.init.headers).get('accept')).toBe('text/event-stream');
    expect(calls).toHaveLength(1);
  });

  it('reopens a stream that ended without its terminal frame', async () => {
    const calls = route({
      [`GET ${filesPaths.fileEvents(P, FILE_ID)}`]: ({ init, count }) =>
        count === 1 ? sse(init, [frame('file.processing', 1, midway)]) : sse(init, [frame('file.processed', 1, fileOf())]),
    });
    const sleeps: number[] = [];

    const last = await followFileEvents(P, FILE_ID, {
      signal: new AbortController().signal,
      onFile: () => undefined,
      sleep: async (ms) => void sleeps.push(ms),
    });

    expect(last?.status).toBe('processed');
    expect(calls).toHaveLength(2);
    expect(sleeps).toEqual([1000]);
  });

  it('reads the file on an interval when the events stream is not there', async () => {
    route({
      [`GET ${filesPaths.fileEvents(P, FILE_ID)}`]: () => json({ message: 'Unknown console endpoint.' }, 404),
      [`GET ${filesPaths.file(P, FILE_ID)}`]: ({ count }) => json(count <= 2 ? queued : fileOf()),
    });
    const seen: string[] = [];
    const gone = vi.fn();

    const last = await followFileEvents(P, FILE_ID, {
      signal: new AbortController().signal,
      onFile: (f) => seen.push(fileStatus(f).label),
      onGone: gone,
      sleep: instant,
    });

    expect(seen).toEqual(['Queued', 'Queued', 'Processed']);
    expect(last?.status).toBe('processed');
    expect(gone).not.toHaveBeenCalled();
  });

  it('stops following, instead of polling forever, when the console refuses to read the file', async () => {
    const calls = route({
      [`GET ${filesPaths.fileEvents(P, FILE_ID)}`]: () => json({ message: 'Unknown console endpoint.' }, 404),
      [`GET ${filesPaths.file(P, FILE_ID)}`]: () => json({ detail: 'Not found' }, 403),
    });
    const gone = vi.fn();

    await followFileEvents(P, FILE_ID, { signal: new AbortController().signal, onFile: () => undefined, onGone: gone, sleep: instant });

    expect(calls.map((c) => c.path.split('/').slice(-1)[0])).toEqual(['events', FILE_ID]);
    expect(gone).not.toHaveBeenCalled();
  });

  it('reports a file deleted while it was followed as gone', async () => {
    route({
      [`GET ${filesPaths.fileEvents(P, FILE_ID)}`]: () =>
        json({ error: { code: 'file_not_found', message: 'No file with that id was found for this project.' } }, 404),
      [`GET ${filesPaths.file(P, FILE_ID)}`]: () =>
        json({ error: { code: 'file_not_found', message: 'No file with that id was found for this project.' } }, 404),
    });
    const gone = vi.fn();

    await followFileEvents(P, FILE_ID, { signal: new AbortController().signal, onFile: () => undefined, onGone: gone, sleep: instant });

    expect(gone).toHaveBeenCalledTimes(1);
  });
});

// ---------------------------------------------------------------------------
// 3. The tab
// ---------------------------------------------------------------------------

const P = PROJECT.id;
const FOLLOW_QUIETLY = { follow: { sleep: stalled } };

function baseRoutes(extra: Record<string, Handler> = {}): Record<string, Handler> {
  return {
    'GET projects': () => json({ projects: [PROJECT] }),
    [`GET ${filesPaths.storage(P)}`]: () => json(STORAGE),
    [`GET ${filesPaths.fileEvents(P, RUNNING.id)}`]: ({ init }) =>
      sse(init, [frame('file.processing', 1, RUNNING)], { close: false }),
    ...extra,
  };
}

describe('the files list', () => {
  it('lists each file with its status, kind, size, created and expires, and the project storage', async () => {
    route(baseRoutes({ [`GET ${filesPaths.files(P)}`]: () => json(listOf([fileOf(), RUNNING])) }));
    render(<FilesPanel me={ADMIN} deps={FOLLOW_QUIETLY} />);

    const name = await screen.findByRole('button', { name: 'quarterly-report.pdf' });
    const row = name.closest('tr') as HTMLTableRowElement;
    const cells = within(row);
    expect(cells.getByText('Processed')).toBeTruthy();
    expect(cells.getByText('PDF')).toBeTruthy();
    expect(cells.getByText(formatBytes(2_400_000))).toBeTruthy();
    expect(cells.getByText(formatDay(CREATED))).toBeTruthy();
    expect(cells.getByText('Never')).toBeTruthy();
    expect(cells.getByText(FILE_ID)).toBeTruthy();

    const video = (screen.getByRole('button', { name: 'board-meeting.mp4' }).closest('tr')) as HTMLTableRowElement;
    expect(within(video).getByText('Processing 4/11')).toBeTruthy();
    expect(within(video).getByText('Video')).toBeTruthy();
    expect(within(video).getByText(formatDay(CREATED + 30 * 86400))).toBeTruthy();
    expect(within(video).getByRole('progressbar', { name: 'Processing board-meeting.mp4' }).getAttribute('aria-valuenow')).toBe('31');

    const tiles = screen.getByTestId('files-storage');
    await waitFor(() => expect(within(tiles).getByText(formatBytes(736_400_000))).toBeTruthy());
    expect(within(tiles).getByText(formatBytes(1_200_000))).toBeTruthy();
  });

  it("says why the list could not be loaded, offers Retry, and shows the files once Retry succeeds", async () => {
    const calls = route(
      baseRoutes({
        [`GET ${filesPaths.files(P)}`]: ({ count }) =>
          count === 1 ? json({ detail: 'The files service is restarting.' }, 503) : json(listOf([fileOf()])),
      }),
    );
    render(<FilesPanel me={ADMIN} deps={FOLLOW_QUIETLY} />);

    const alert = await screen.findByRole('alert');
    expect(alert.textContent).toContain('The files service is restarting.');
    // An error is not an empty project.
    expect(screen.queryByText('No files in this project')).toBeNull();

    await userEvent.click(within(alert).getByRole('button', { name: 'Retry' }));

    expect(await screen.findByRole('button', { name: 'quarterly-report.pdf' })).toBeTruthy();
    expect(screen.queryByRole('alert')).toBeNull();
    expect(calls.filter((c) => c.path === filesPaths.files(P))).toHaveLength(2);
  });

  it('sends the status and kind filters to the server as bounded parameters and nothing else', async () => {
    const calls = route(baseRoutes({ [`GET ${filesPaths.files(P)}`]: () => json(listOf([fileOf()])) }));
    render(<FilesPanel me={ADMIN} deps={FOLLOW_QUIETLY} />);
    await screen.findByRole('button', { name: 'quarterly-report.pdf' });

    await userEvent.selectOptions(screen.getByRole('combobox', { name: 'Status' }), 'failed');
    await userEvent.selectOptions(screen.getByRole('combobox', { name: 'Kind' }), 'pdf');

    await waitFor(() =>
      expect(calls.filter((c) => c.path === filesPaths.files(P)).map((c) => c.query)).toEqual([
        'limit=50',
        'limit=50&status=failed',
        'limit=50&status=failed&kind=pdf',
      ]),
    );
    // A server that ignored the filter still cannot put a processed PDF under "Failed".
    await waitFor(() => expect(screen.getByText('No files match these filters')).toBeTruthy());
  });

  it("follows an unfinished file's events and redraws its row when it finishes", async () => {
    route(
      baseRoutes({
        [`GET ${filesPaths.files(P)}`]: () => json(listOf([RUNNING])),
        [`GET ${filesPaths.fileEvents(P, RUNNING.id)}`]: ({ init }) =>
          sse(init, [frame('file.processing', 1, RUNNING), frame('file.processed', 2, { ...RUNNING, status: 'processed', processing: processing({ kind: 'video', stages: [] }) })]),
      }),
    );
    render(<FilesPanel me={ADMIN} deps={FOLLOW_QUIETLY} />);

    const name = await screen.findByRole('button', { name: 'board-meeting.mp4' });
    const row = name.closest('tr') as HTMLTableRowElement;
    await waitFor(() => expect(within(row).getByText('Processed')).toBeTruthy());
  });
});

describe('uploading from the tab', () => {
  it('uploads a picked file in parts and follows its processing from the events stream until it is processed', async () => {
    const uploaded = fileOf({
      filename: 'notes.txt',
      bytes: 10,
      status: 'uploaded',
      processing: processing({ state: 'queued', kind: 'text', stage: 'sniff', step: 1, percent: 0, stages: [] }),
    });
    const calls = route(
      baseRoutes({
        [`GET ${filesPaths.files(P)}`]: ({ count }) => json(listOf(count === 1 ? [] : [uploaded])),
        [`POST ${filesPaths.uploads(P)}`]: () => json(uploadOf()),
        [`PUT ${filesPaths.uploadPart(P, UPLOAD_ID, 0)}`]: () => json(partOf(0, 4)),
        [`PUT ${filesPaths.uploadPart(P, UPLOAD_ID, 1)}`]: () => json(partOf(1, 4)),
        [`PUT ${filesPaths.uploadPart(P, UPLOAD_ID, 2)}`]: () => json(partOf(2, 2)),
        [`POST ${filesPaths.completeUpload(P, UPLOAD_ID)}`]: () => json(uploadOf({ status: 'completed', file: uploaded })),
        [`GET ${filesPaths.fileEvents(P, FILE_ID)}`]: ({ init }) =>
          sse(init, [
            frame('file.processing', 1, { ...uploaded, processing: processing({ state: 'processing', kind: 'text', stage: 'chunk', step: 3, total_steps: 5, stages: [] }) }),
            frame('file.processed', 2, { ...uploaded, status: 'processed', processing: processing({ kind: 'text', stages: [] }) }),
          ]),
      }),
    );
    render(
      <FilesPanel
        me={ADMIN}
        deps={{ ...FOLLOW_QUIETLY, uploader: { partBytes: 4, sleep: instant, hash: async () => null } }}
      />,
    );
    await screen.findByText('No files in this project');

    fireEvent.change(screen.getByTestId('files-upload-input'), { target: { files: [tenBytes()] } });

    const item = await screen.findByTestId('upload-item');
    await waitFor(() => expect(within(item).getByTestId('upload-sentence').textContent).toBe('Uploaded · Processed'));
    expect(within(item).getByRole('progressbar', { name: 'Upload of notes.txt' }).getAttribute('aria-valuenow')).toBe('100');
    expect(calls.filter((c) => c.method === 'PUT').map((c) => c.path.split('/').pop())).toEqual(['0', '1', '2']);
    // The finished upload is in the list, drawn from its live state.
    const row = (await screen.findByRole('button', { name: 'notes.txt' })).closest('tr') as HTMLTableRowElement;
    expect(within(row).getByText('Processed')).toBeTruthy();
    expect(window.localStorage.length).toBe(0);
  });

  it('sends at most two uploads at once, starts each waiting one as a slot frees, and cancels a waiting one without a request', async () => {
    const held: (() => void)[] = [];
    let inFlight = 0;
    let peak = 0;
    let creates = 0;
    const requests: string[] = [];
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string | URL, init: RequestInit = {}) => {
        const method = (init.method ?? 'GET').toUpperCase();
        const path = String(url).replace('/api/devplatform/', '').split('?')[0] ?? '';
        requests.push(`${method} ${path}`);
        if (method === 'GET' && path === 'projects') return json({ projects: [PROJECT] });
        if (method === 'GET' && path === filesPaths.files(P)) return json(listOf([]));
        if (method === 'GET' && path === filesPaths.storage(P)) return json(STORAGE);
        if (method === 'POST' && path === filesPaths.uploads(P)) {
          creates += 1;
          const { filename } = JSON.parse(String(init.body)) as { filename: string };
          return json(uploadOf({ id: `upload_${String(creates).padStart(26, '0')}`, filename }));
        }
        if (method === 'PUT') {
          inFlight += 1;
          peak = Math.max(peak, inFlight);
          await new Promise<void>((resolve, reject) => {
            held.push(resolve);
            init.signal?.addEventListener('abort', () => reject(Object.assign(new Error('aborted'), { name: 'AbortError' })));
          });
          inFlight -= 1;
          return json(partOf(0, 10));
        }
        const complete = /uploads\/upload_0*(\d+)\/complete$/.exec(path);
        if (method === 'POST' && complete) {
          const n = complete[1];
          return json(
            uploadOf({
              status: 'completed',
              file: fileOf({ id: `file-${String(n).padStart(24, '0')}`, filename: `volume-${n}.bin`, bytes: 10 }),
            }),
          );
        }
        return json({ message: 'Unknown console endpoint.' }, 404);
      }),
    );
    render(<FilesPanel me={ADMIN} deps={{ ...FOLLOW_QUIETLY, uploader: { sleep: instant, hash: async () => null } }} />);
    await screen.findByText('No files in this project');

    const picked = Array.from(
      { length: 5 },
      (_, i) => new File([new Uint8Array(10)], `volume-${i + 1}.bin`, { lastModified: 1_789_000_000_000 + i }),
    );
    fireEvent.change(screen.getByTestId('files-upload-input'), { target: { files: picked } });

    await waitFor(() => expect(held).toHaveLength(2));
    expect(creates).toBe(2);
    const sentences = () => screen.getAllByTestId('upload-sentence').map((el) => el.textContent);
    expect(sentences().filter((t) => t === 'Waiting to start. Uploads run 2 at a time.')).toHaveLength(3);

    // The last one waiting is cancelled before it ever asked the server for anything.
    const last = screen.getAllByTestId('upload-item')[4] as HTMLElement;
    expect(within(last).getByText('volume-5.bin')).toBeTruthy();
    await userEvent.click(within(last).getByRole('button', { name: 'Cancel' }));
    expect(within(last).getByTestId('upload-sentence').textContent).toBe('Cancelled. Nothing was kept.');

    for (let released = 0; released < 4; released += 1) {
      await waitFor(() => expect(held.length).toBeGreaterThan(0));
      await act(async () => {
        (held.shift() as () => void)();
      });
    }

    await waitFor(() => expect(sentences().filter((t) => t === 'Uploaded · Processed')).toHaveLength(4));
    expect(peak).toBe(2);
    expect(creates).toBe(4);
    expect(requests.filter((r) => r.includes('volume-5') || r.endsWith('00005'))).toEqual([]);
  });

  it('resumes a picked file from the parts the server already holds, without creating a second upload', async () => {
    const file = tenBytes();
    window.localStorage.setItem(
      resumeKey(P, file),
      JSON.stringify([{ upload_id: UPLOAD_ID, part_bytes: 4, expires_at: Math.floor(Date.now() / 1000) + 3600 }]),
    );
    const calls = route(
      baseRoutes({
        [`GET ${filesPaths.files(P)}`]: () => json(listOf([])),
        [`GET ${filesPaths.upload(P, UPLOAD_ID)}`]: () =>
          json(uploadOf({ parts: [partOf(0, 4), partOf(1, 4)] as UploadObject['parts'] })),
        [`PUT ${filesPaths.uploadPart(P, UPLOAD_ID, 2)}`]: () => json(partOf(2, 2)),
        [`POST ${filesPaths.completeUpload(P, UPLOAD_ID)}`]: () =>
          json(uploadOf({ status: 'completed', file: fileOf({ filename: 'notes.txt' }) })),
      }),
    );
    render(
      <FilesPanel me={ADMIN} deps={{ ...FOLLOW_QUIETLY, uploader: { partBytes: 4, sleep: instant, hash: async () => null } }} />,
    );
    await screen.findByText('No files in this project');

    fireEvent.change(screen.getByTestId('files-upload-input'), { target: { files: [file] } });

    const item = await screen.findByTestId('upload-item');
    await waitFor(() => expect(within(item).getByTestId('upload-sentence').textContent).toBe('Uploaded · Processed'));
    const sent = calls.filter((c) => c.method !== 'GET' || c.path.includes('/uploads/')).map((c) => `${c.method} ${c.path}`);
    expect(sent).toEqual([
      `GET ${filesPaths.upload(P, UPLOAD_ID)}`,
      `PUT ${filesPaths.uploadPart(P, UPLOAD_ID, 2)}`,
      `POST ${filesPaths.completeUpload(P, UPLOAD_ID)}`,
    ]);
    expect(window.localStorage.getItem(resumeKey(P, file))).toBeNull();
  });

  it('offers Resume on a paused upload and continues it when the connection comes back', async () => {
    let network = false;
    route(
      baseRoutes({
        [`GET ${filesPaths.files(P)}`]: () => json(listOf([])),
        [`POST ${filesPaths.uploads(P)}`]: () => json(uploadOf()),
        [`PUT ${filesPaths.uploadPart(P, UPLOAD_ID, 0)}`]: () => (network ? json(partOf(0, 10)) : offline()),
        [`GET ${filesPaths.upload(P, UPLOAD_ID)}`]: () => (network ? json(uploadOf({ parts: [] })) : offline()),
        [`POST ${filesPaths.completeUpload(P, UPLOAD_ID)}`]: () =>
          json(uploadOf({ status: 'completed', file: fileOf({ filename: 'notes.txt' }) })),
      }),
    );
    render(
      <FilesPanel me={ADMIN} deps={{ ...FOLLOW_QUIETLY, uploader: { sleep: instant, hash: async () => null, maxAttempts: 2 } }} />,
    );
    await screen.findByText('No files in this project');
    fireEvent.change(screen.getByTestId('files-upload-input'), { target: { files: [tenBytes()] } });

    const item = await screen.findByTestId('upload-item');
    const resume = await within(item).findByRole('button', { name: 'Resume' });
    expect(within(item).getByTestId('upload-sentence').textContent).toBe(
      'The connection dropped. 0 of 1 parts are safe on the server; resume to send the rest.',
    );

    network = true;
    await act(async () => {
      window.dispatchEvent(new Event('online'));
    });
    await waitFor(() => expect(within(item).getByTestId('upload-sentence').textContent).toBe('Uploaded · Processed'));
    expect(resume.isConnected).toBe(false);
  });

  it('continues the paused upload when the same file is picked again, instead of sending it a second time', async () => {
    let network = false;
    const calls = route(
      baseRoutes({
        [`GET ${filesPaths.files(P)}`]: () => json(listOf([])),
        [`POST ${filesPaths.uploads(P)}`]: () => json(uploadOf()),
        [`PUT ${filesPaths.uploadPart(P, UPLOAD_ID, 0)}`]: () => (network ? json(partOf(0, 10)) : offline()),
        [`GET ${filesPaths.upload(P, UPLOAD_ID)}`]: () => (network ? json(uploadOf({ parts: [] })) : offline()),
        [`POST ${filesPaths.completeUpload(P, UPLOAD_ID)}`]: () =>
          json(uploadOf({ status: 'completed', file: fileOf({ filename: 'notes.txt' }) })),
      }),
    );
    render(
      <FilesPanel me={ADMIN} deps={{ ...FOLLOW_QUIETLY, uploader: { sleep: instant, hash: async () => null, maxAttempts: 2 } }} />,
    );
    await screen.findByText('No files in this project');
    fireEvent.change(screen.getByTestId('files-upload-input'), { target: { files: [tenBytes()] } });
    const item = await screen.findByTestId('upload-item');
    await within(item).findByRole('button', { name: 'Resume' });

    network = true;
    fireEvent.change(screen.getByTestId('files-upload-input'), { target: { files: [tenBytes()] } });

    await waitFor(() => expect(within(item).getByTestId('upload-sentence').textContent).toBe('Uploaded · Processed'));
    expect(screen.getAllByTestId('upload-item')).toHaveLength(1);
    expect(calls.filter((c) => c.method === 'POST' && c.path === filesPaths.uploads(P))).toHaveLength(1);
    expect(calls.filter((c) => c.method === 'PUT' && c.path === filesPaths.uploadPart(P, UPLOAD_ID, 0))).toHaveLength(3);
  });

  it('warns before the page unloads while bytes are being sent, and pauses the upload with its record kept when the tab is left', async () => {
    const parts: RequestInit[] = [];
    route(
      baseRoutes({
        [`GET ${filesPaths.files(P)}`]: () => json(listOf([])),
        [`POST ${filesPaths.uploads(P)}`]: () => json(uploadOf()),
        [`PUT ${filesPaths.uploadPart(P, UPLOAD_ID, 0)}`]: ({ init }) => {
          parts.push(init);
          return held(init);
        },
      }),
    );
    const view = render(<FilesPanel me={ADMIN} deps={{ ...FOLLOW_QUIETLY, uploader: { sleep: instant, hash: async () => null } }} />);
    await screen.findByText('No files in this project');
    const unload = () => {
      const event = new Event('beforeunload', { cancelable: true });
      window.dispatchEvent(event);
      return event.defaultPrevented;
    };
    expect(unload()).toBe(false);

    fireEvent.change(screen.getByTestId('files-upload-input'), { target: { files: [tenBytes()] } });
    await waitFor(() => expect(parts).toHaveLength(1));
    expect(unload()).toBe(true);

    view.unmount();

    expect(parts[0]!.signal?.aborted).toBe(true);
    expect(JSON.parse(window.localStorage.getItem(resumeKey(P, tenBytes())) ?? '[]')).toMatchObject([{ upload_id: UPLOAD_ID }]);
    expect(unload()).toBe(false);
  });

  it('forgets, when the tab opens, the uploads no server can still hold and keeps the rest', async () => {
    const now = Date.now();
    const stale = resumeKey(P, { size: 900, lastModified: 1 });
    const fresh = resumeKey(P, { size: 901, lastModified: 2 });
    const mixed = resumeKey(P, { size: 902, lastModified: 3 });
    const old = Math.floor(now / 1000) - RECORD_STALE_AFTER_S - 60;
    const recent = Math.floor(now / 1000) - 3600;
    window.localStorage.setItem(stale, JSON.stringify([{ upload_id: 'upload_a', part_bytes: 4, expires_at: old }]));
    window.localStorage.setItem(fresh, JSON.stringify([{ upload_id: 'upload_b', part_bytes: 4, expires_at: recent }]));
    window.localStorage.setItem(
      mixed,
      JSON.stringify([
        { upload_id: 'upload_c', part_bytes: 4, expires_at: old },
        { upload_id: 'upload_d', part_bytes: 4, expires_at: null },
      ]),
    );
    window.localStorage.setItem('someone.else', JSON.stringify([{ expires_at: old }]));
    route(baseRoutes({ [`GET ${filesPaths.files(P)}`]: () => json(listOf([])) }));

    render(<FilesPanel me={ADMIN} deps={FOLLOW_QUIETLY} />);
    await screen.findByText('No files in this project');

    expect(window.localStorage.getItem(stale)).toBeNull();
    expect(JSON.parse(window.localStorage.getItem(fresh) ?? '[]')).toHaveLength(1);
    expect(JSON.parse(window.localStorage.getItem(mixed) ?? '[]')).toEqual([{ upload_id: 'upload_d', part_bytes: 4, expires_at: null }]);
    expect(window.localStorage.getItem('someone.else')).not.toBeNull();
  });

  it('offers no upload and no delete to an account that may only read', async () => {
    route(baseRoutes({ [`GET ${filesPaths.files(P)}`]: () => json(listOf([fileOf()])) }));
    render(<FilesPanel me={READER} deps={FOLLOW_QUIETLY} />);
    await screen.findByRole('button', { name: 'quarterly-report.pdf' });

    expect(screen.queryByRole('button', { name: 'Upload files' })).toBeNull();
    expect(screen.queryByTestId('files-upload-input')).toBeNull();
    await userEvent.click(screen.getByRole('button', { name: 'Actions for quarterly-report.pdf' }));
    const items = screen.getAllByRole('menuitem').map((el) => el.textContent);
    expect(items).toEqual(['View details']);
  });
});

describe('deleting a file', () => {
  it('asks first, naming the file and what goes with it, and deletes only on confirmation', async () => {
    const calls = route(
      baseRoutes({
        [`GET ${filesPaths.files(P)}`]: () => json(listOf([fileOf()])),
        [`DELETE ${filesPaths.file(P, FILE_ID)}`]: () => json({ id: FILE_ID, object: 'file', deleted: true }),
      }),
    );
    render(<FilesPanel me={ADMIN} deps={FOLLOW_QUIETLY} />);
    await screen.findByRole('button', { name: 'quarterly-report.pdf' });

    await userEvent.click(screen.getByRole('button', { name: 'Actions for quarterly-report.pdf' }));
    await userEvent.click(screen.getByRole('menuitem', { name: 'Delete' }));
    let confirm = screen.getByRole('alertdialog', { name: 'Delete quarterly-report.pdf?' });
    expect(confirm.textContent).toContain('extracted text, transcripts and the search index');
    expect(confirm.textContent).toContain(FILE_ID);
    // The bytes are shared with any other file of the same content in the project, and the dialog says so.
    expect(confirm.textContent).toContain('unless another file in this project was uploaded with the same content');
    await userEvent.click(within(confirm).getByRole('button', { name: 'Cancel' }));
    expect(calls.some((c) => c.method === 'DELETE')).toBe(false);
    expect(screen.getByRole('button', { name: 'quarterly-report.pdf' })).toBeTruthy();

    await userEvent.click(screen.getByRole('button', { name: 'Actions for quarterly-report.pdf' }));
    await userEvent.click(screen.getByRole('menuitem', { name: 'Delete' }));
    confirm = screen.getByRole('alertdialog', { name: 'Delete quarterly-report.pdf?' });
    await userEvent.click(within(confirm).getByRole('button', { name: 'Delete' }));

    await waitFor(() => expect(screen.queryByRole('button', { name: 'quarterly-report.pdf' })).toBeNull());
    expect(calls.filter((c) => c.method === 'DELETE').map((c) => c.path)).toEqual([filesPaths.file(P, FILE_ID)]);
  });
});

describe("a file's detail view", () => {
  it('lists the stages, the measured facts and the derived outputs by name and size, with no way to download bytes', async () => {
    const withExtras = fileOf({
      derived_bytes: 12_800,
      processing: processing({
        facts: { pages: 12, ocr_pages: 2, engine: 'internal-ocr-model', path: '/data/api-files/x' },
      }),
    });
    route(
      baseRoutes({
        [`GET ${filesPaths.files(P)}`]: () => json(listOf([withExtras])),
        [`GET ${filesPaths.fileDerived(P, FILE_ID)}`]: () =>
          json({
            object: 'list',
            data: [
              { name: 'text.txt', bytes: 12_000, content_type: 'text/plain; charset=utf-8' },
              { name: 'pages.json', bytes: 800, content_type: 'application/json' },
              { name: '../secrets.env', bytes: 5, content_type: 'text/plain' },
              // Well formed, and still not a name the closed set knows.
              { name: 'engine-debug.log', bytes: 7, content_type: 'text/plain' },
            ],
          }),
      }),
    );
    render(<FilesPanel me={ADMIN} deps={FOLLOW_QUIETLY} />);
    await userEvent.click(await screen.findByRole('button', { name: 'quarterly-report.pdf' }));

    const dialog = await screen.findByRole('dialog', { name: 'quarterly-report.pdf' });
    const view = within(dialog);
    expect(view.getByText('Read scanned pages')).toBeTruthy();
    expect(view.getByText('Pages read by OCR')).toBeTruthy();
    expect(view.getByText('2 min 0 s')).toBeTruthy();
    expect(view.getByText(formatBytes(12_800))).toBeTruthy();
    expect(await view.findByText('Extracted text')).toBeTruthy();
    expect(view.getByText('Page map')).toBeTruthy();
    expect(view.getByText(formatBytes(12_000))).toBeTruthy();
    // Nothing outside the allowlists is drawn, and nothing here serves bytes.
    expect(dialog.textContent).not.toContain('internal-ocr-model');
    expect(dialog.textContent).not.toContain('/data/');
    expect(dialog.textContent).not.toContain('secrets');
    expect(dialog.textContent).not.toContain('engine-debug');
    expect(view.queryAllByRole('link')).toHaveLength(0);
    expect(view.queryByRole('button', { name: /download/i })).toBeNull();
    expect(dialog.textContent).toContain(`GET /v1/files/${FILE_ID}/derived/<name>`);
  });

  it("shows a file's derived size once it finishes on screen, though its last frame does not carry one", async () => {
    const finished: ConsoleFile = { ...RUNNING, status: 'processed', processing: processing({ kind: 'video', stages: [], facts: {} }) };
    route(
      baseRoutes({
        [`GET ${filesPaths.files(P)}`]: () => json(listOf([RUNNING])),
        [`GET ${filesPaths.fileEvents(P, RUNNING.id)}`]: ({ init }) => sse(init, [frame('file.processed', 1, finished)]),
        [`GET ${filesPaths.file(P, RUNNING.id)}`]: () => json({ ...finished, derived_bytes: 48_000_000 }),
        [`GET ${filesPaths.fileDerived(P, RUNNING.id)}`]: () => json({ object: 'list', data: [] }),
      }),
    );
    render(<FilesPanel me={ADMIN} deps={FOLLOW_QUIETLY} />);

    await userEvent.click(await screen.findByRole('button', { name: 'board-meeting.mp4' }));
    const dialog = await screen.findByRole('dialog', { name: 'board-meeting.mp4' });
    await waitFor(() => expect(within(dialog).getByText(formatBytes(48_000_000))).toBeTruthy());
    expect(within(dialog).getByText('Processed')).toBeTruthy();
  });

  it('calls a file whose type was never found Unknown once nothing more will happen to it', () => {
    const undetected = processing({ kind: 'unknown', state: 'queued', stage: 'sniff', step: 1, percent: 0 });
    expect(kindLabel(fileOf({ status: 'uploaded', processing: undetected }))).toBe('Detecting');
    expect(
      kindLabel(fileOf({ status: 'error', processing: { ...undetected, state: 'failed', error: { code: 'file_corrupt', message: 'x' } } })),
    ).toBe('Unknown');
    expect(kindLabel(fileOf())).toBe('PDF');
  });

  it("shows a failed file's fixed sentence and never a stage the server did not list", () => {
    const failed = fileOf({
      status: 'error',
      status_details: 'The file could not be read; it may be damaged or encrypted.',
      processing: processing({ state: 'failed', error: { code: 'file_corrupt', message: 'x' }, facts: { pages: 'many' } }),
    });
    expect(fileStatus(failed)).toEqual({ key: 'failed', label: 'Failed', percent: null });
    expect(fileStatus({ ...failed, processing: processing({ state: 'failed', error: { code: 'unsupported_file', message: 'x' } }) }).label).toBe('Unsupported');
    // A fact whose value has the wrong shape is not drawn at all.
    expect(fileFacts(failed)).toEqual([]);
    expect(fileFacts(fileOf({ processing: processing({ facts: { format: 'PDF', language: 'en (internal-asr v2)' } }) }))).toEqual([
      { key: 'format', label: 'Format', value: 'PDF' },
    ]);
    expect(fileStatus(fileOf({ status: 'uploaded', processing: processing({ state: 'processing', stage: 'assemble', step: 0, total_steps: 0, percent: 45 }) }))).toEqual({
      key: 'assembling',
      label: 'Assembling',
      percent: 45,
    });
  });
});

// ---------------------------------------------------------------------------
// Responsive: the same arithmetic the console suite pins for every table
// ---------------------------------------------------------------------------

const PHONE_TABLE_WIDTH = 400 - 32;
const READABLE_IDENTITY = 160;
const consoleColumnAt = (viewport: number) => Math.min(viewport - 240 - 16, 1180) - 64;

function geometry(table: HTMLTableElement, viewport: number | 'phone') {
  const wrapper = table.closest('[data-testid="console-table"]') as HTMLElement;
  const classes = wrapper.className.split(/\s+/);
  const cols = Array.from(table.querySelectorAll('col'));
  const labels = Array.from(table.querySelectorAll('thead th')).map((th) => th.textContent ?? '');
  const lifted = classes.includes('max-xl:[&_table]:!min-w-0');
  if (viewport === 'phone') {
    const visible = cols.filter((c) => !c.className.split(/\s+/).includes('hidden'));
    const fixed = visible.reduce((sum, col) => sum + (parseFloat(col.style.width) || 0), 0);
    return { lifted, identity: PHONE_TABLE_WIDTH - fixed, clipped: false, hiddenLabels: [] as string[] };
  }
  const has = (tier: 'xl' | '2xl', n: number) =>
    ['col', 'th', 'td'].every((el) => classes.includes(`max-${tier}:[&_${el}:nth-child(${n})]:!hidden`));
  const hidden = (i: number) => (viewport < 1280 && has('xl', i + 1)) || (viewport < 1536 && has('2xl', i + 1));
  const fixed = cols.filter((_, i) => !hidden(i)).reduce((sum, col) => sum + (parseFloat(col.style.width) || 0), 0);
  const floor = parseFloat(table.style.getPropertyValue('--admin-table-min'));
  const column = consoleColumnAt(viewport);
  const width = lifted && viewport < 1280 ? column : Math.max(column, floor);
  return { lifted, identity: width - fixed, clipped: width > column, hiddenLabels: labels.filter((_, i) => hidden(i)) };
}

describe('the files table at every width', () => {
  it('keeps the row menu reachable on a 400px phone and fits the column from 1024px to 1920px, folding Created and Expires until xl', async () => {
    route(baseRoutes({ [`GET ${filesPaths.files(P)}`]: () => json(listOf([fileOf()])) }));
    render(<FilesPanel me={ADMIN} deps={FOLLOW_QUIETLY} />);
    const menu = await screen.findByRole('button', { name: 'Actions for quarterly-report.pdf' });
    const table = menu.closest('table') as HTMLTableElement;

    const phone = geometry(table, 'phone');
    expect(phone.lifted).toBe(true);
    expect(phone.identity).toBeGreaterThanOrEqual(READABLE_IDENTITY);
    expect((menu.closest('td') as HTMLElement).className.split(/\s+/)).not.toContain('hidden');

    for (const viewport of [1024, 1100, 1279, 1280, 1366, 1440, 1536, 1920]) {
      const g = geometry(table, viewport);
      expect({ viewport, clipped: g.clipped, readable: g.identity >= READABLE_IDENTITY }).toEqual({
        viewport,
        clipped: false,
        readable: true,
      });
    }
    expect(geometry(table, 1024).hiddenLabels).toEqual(['Created', 'Expires']);
    expect(geometry(table, 1280).hiddenLabels).toEqual([]);
  });
});

// ---------------------------------------------------------------------------
// The paths and the BFF
// ---------------------------------------------------------------------------

describe('the console paths the tab calls', () => {
  const USED: [string, string][] = [
    [filesPaths.files(P), 'GET'],
    [filesPaths.file(P, FILE_ID), 'GET'],
    [filesPaths.file(P, FILE_ID), 'DELETE'],
    [filesPaths.fileEvents(P, FILE_ID), 'GET'],
    [filesPaths.fileDerived(P, FILE_ID), 'GET'],
    [filesPaths.storage(P), 'GET'],
    [filesPaths.uploads(P), 'POST'],
    [filesPaths.upload(P, UPLOAD_ID), 'GET'],
    [filesPaths.uploadPart(P, UPLOAD_ID, 9999), 'PUT'],
    [filesPaths.completeUpload(P, UPLOAD_ID), 'POST'],
    [filesPaths.cancelUpload(P, UPLOAD_ID), 'POST'],
  ];

  it('matches one declared operation each, and every declared operation has a caller', () => {
    for (const [path, method] of USED) {
      expect(filesOperationFor(path, method), `${method} ${path}`).not.toBeNull();
    }
    // The caller half counts what the uploader, the follower and the tab
    // ACTUALLY sent in this file's tests, not a list written beside the table.
    // It needs the file run whole, and says so rather than passing on nothing.
    const sent = EXERCISED.filter(([path, method]) => filesOperationFor(path, method) !== null);
    expect(sent.length, 'no Files call was sent before this test: run tests/devplatform-files.test.tsx whole').toBeGreaterThan(0);
    for (const op of FILES_CONSOLE_OPERATIONS) {
      for (const method of op.methods) {
        const called = sent.some(([path, m]) => m === method && filesOperationFor(path, m) === op);
        expect(called, `${method} ${op.pattern.join('/')}`).toBe(true);
      }
    }
    // Nothing the tab sent falls outside the table.
    for (const [path, method] of EXERCISED) {
      if (path.startsWith('projects/') && /\/(files|uploads|storage)(\/|$)/.test(path)) {
        expect(filesOperationFor(path, method), `${method} ${path}`).not.toBeNull();
      }
    }
    // Only the part is a raw body, and only the events are a stream.
    expect(filesOperationFor(filesPaths.uploadPart(P, UPLOAD_ID, 0), 'PUT')?.relay).toBe('part');
    expect(filesOperationFor(filesPaths.fileEvents(P, FILE_ID), 'GET')?.relay).toBe('events');
    // No byte route: the original's content and a derived file's bytes are not console operations.
    expect(filesOperationFor(`projects/${P}/files/${FILE_ID}/content`, 'GET')).toBeNull();
    expect(filesOperationFor(`projects/${P}/files/${FILE_ID}/derived/text.txt`, 'GET')).toBeNull();
  });

  it('is carried by the BFF in full once the BFF carries any of it', async () => {
    const { consoleRouteAllowed, consoleOperations } = await import('@/app/api/devplatform/[...path]/route');
    const integrated = consoleOperations().some((op) => op.path === 'projects/:id/files');
    // Before integration the BFF answers 404 for every Files path and the tab
    // shows that sentence with Retry; after it, a half-wired BFF must fail here.
    for (const [path, method] of USED) {
      expect(consoleRouteAllowed(path.split('/'), method), `${method} ${path}`).toBe(integrated);
    }
  });
});
