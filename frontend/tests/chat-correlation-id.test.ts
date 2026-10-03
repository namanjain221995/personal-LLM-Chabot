/**
 * B-03 (2026-10-03): one correlation id per chat request, end to end.
 *
 * The proxy forwards `X-Request-ID` to the orchestrator, writes the same id on
 * its log line and returns it to the browser. It adopts an id an upstream
 * proxy assigned only in the orchestrator's shape (`req_` + 32 hex) — the one
 * shape the orchestrator accepts and its trace contract pins — and mints a
 * fresh one for anything else, so client input never travels unvalidated.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { REQUEST_ID_HEADER, acceptRequestId, newRequestId } from '../lib/orchestrator';

const SHAPE = /^req_[0-9a-f]{32}$/;
const VALID = `req_${'0123456789abcdef'.repeat(2)}`;

type Sent = { url: string; headers: Record<string, string> };

let sent: Sent[] = [];
let errors: string[] = [];

function stubOrchestrator(response: () => Response) {
  vi.stubGlobal('fetch', async (url: string, init: RequestInit) => {
    sent.push({ url, headers: init.headers as Record<string, string> });
    return response();
  });
}

const sse = () =>
  new Response('event: done\ndata: {}\n\n', {
    status: 200,
    headers: { 'content-type': 'text/event-stream' },
  });

const post = (headers: Record<string, string> = {}) =>
  new Request('http://localhost:3001/api/chat', {
    method: 'POST',
    body: JSON.stringify({ messages: [{ role: 'user', content: 'hello' }] }),
    headers: { 'content-type': 'application/json', ...headers },
  });

async function POST(req: Request) {
  const mod = await import('../app/api/chat/route');
  return mod.POST(req);
}

beforeEach(() => {
  sent = [];
  errors = [];
  vi.stubEnv('MOCK_MODE', 'false');
  vi.stubEnv('ORCHESTRATOR_URL', 'http://orchestrator:8080');
  vi.spyOn(console, 'error').mockImplementation((...args: unknown[]) => {
    errors.push(args.map(String).join(' '));
  });
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
  vi.restoreAllMocks();
});

describe('the id helpers', () => {
  it('mints the orchestrator shape, a different id every time', () => {
    const ids = new Set(Array.from({ length: 50 }, () => newRequestId()));
    expect(ids.size).toBe(50);
    for (const id of ids) expect(id).toMatch(SHAPE);
  });

  it('accepts only that shape', () => {
    expect(acceptRequestId(VALID)).toBe(VALID);
    expect(acceptRequestId(`req_${'ABCDEF0123456789'.repeat(2)}`)).not.toBeNull();
    for (const bad of [
      undefined,
      null,
      42,
      '',
      'r1',
      'req-42',
      `req_${'a'.repeat(31)}`,
      `req_${'a'.repeat(33)}`,
      `req_${'g'.repeat(32)}`,
      ` ${VALID}`,
      `${VALID}\n`,
      '00000000-0000-4000-8000-000000000000',
      'x'.repeat(5000),
    ]) {
      expect(acceptRequestId(bad)).toBeNull();
    }
  });
});

describe('POST /api/chat — the correlation id', () => {
  it('mints one when none was sent: forwarded, and returned on the stream', async () => {
    stubOrchestrator(sse);
    const res = await POST(post());
    expect(res.status).toBe(200);
    const forwarded = sent[0].headers[REQUEST_ID_HEADER];
    expect(forwarded).toMatch(SHAPE);
    expect(res.headers.get('x-request-id')).toBe(forwarded);
    // The SSE contract is untouched.
    expect(res.headers.get('content-type')).toBe('text/event-stream; charset=utf-8');
    expect(res.headers.get('x-accel-buffering')).toBe('no');
    expect(await res.text()).toBe('event: done\ndata: {}\n\n');
  });

  it('two requests never share an id', async () => {
    stubOrchestrator(sse);
    await POST(post());
    await POST(post());
    expect(sent[0].headers[REQUEST_ID_HEADER]).not.toBe(sent[1].headers[REQUEST_ID_HEADER]);
  });

  it.each([['x-request-id'], ['x-correlation-id']])(
    'adopts a well-formed upstream id from %s',
    async (header) => {
      stubOrchestrator(sse);
      const res = await POST(post({ [header]: VALID }));
      expect(sent[0].headers[REQUEST_ID_HEADER]).toBe(VALID);
      expect(res.headers.get('x-request-id')).toBe(VALID);
    },
  );

  it.each([
    ['req-42'],
    [`req_${'z'.repeat(32)}`],
    ['00000000-0000-4000-8000-000000000000'],
    ['a'.repeat(300)],
    ['req_" onerror="x'],
  ])('replaces a malformed one (%s) instead of forwarding it', async (inbound) => {
    stubOrchestrator(sse);
    const res = await POST(post({ 'x-request-id': inbound }));
    const forwarded = sent[0].headers[REQUEST_ID_HEADER];
    expect(forwarded).toMatch(SHAPE);
    expect(forwarded).not.toBe(inbound);
    expect(res.headers.get('x-request-id')).toBe(forwarded);
  });

  it('still forwards the cookie and nothing else the browser sent', async () => {
    stubOrchestrator(sse);
    await POST(post({ cookie: 'ts_session=abc', 'x-request-id': VALID, 'x-forwarded-for': '198.51.100.7' }));
    expect(Object.keys(sent[0].headers).sort()).toEqual(
      ['Content-Type', REQUEST_ID_HEADER, 'cookie'].sort(),
    );
  });

  it('names the same id on a failure: response header and log line', async () => {
    stubOrchestrator(() => new Response('boom', { status: 500 }));
    const res = await POST(post());
    const forwarded = sent[0].headers[REQUEST_ID_HEADER];
    expect(res.status).toBe(500);
    expect(res.headers.get('x-request-id')).toBe(forwarded);
    expect(errors.join('\n')).toContain(`request_id="${forwarded}"`);
  });

  it('names an id on a refusal that never reached the orchestrator', async () => {
    stubOrchestrator(sse);
    const res = await POST(
      new Request('http://localhost:3001/api/chat', {
        method: 'POST',
        body: 'not json',
        headers: { 'content-type': 'application/json', 'x-request-id': VALID },
      }),
    );
    expect(res.status).toBe(400);
    expect(sent).toHaveLength(0);
    expect(res.headers.get('x-request-id')).toBe(VALID);
    expect(errors.join('\n')).toContain(`request_id="${VALID}"`);
  });
});
