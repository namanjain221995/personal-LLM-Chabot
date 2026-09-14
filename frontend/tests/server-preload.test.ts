/**
 * frontend/server-preload.cjs (2026-09-13, no-timeout design revision 2).
 *
 * Every test here runs a REAL child process — `node --require
 * server-preload.cjs <server>` — because what is under test is the Node http
 * server's own timers, sockets and signal handling, none of which a mock can
 * show. The child server imitates exactly the two things Next's standalone
 * start-server.js does that matter: it builds its server with
 * `http.createServer(listener)`, and on SIGTERM it calls `server.close()` and
 * exits 143 once that resolves, without closing connections itself.
 *
 * What is pinned:
 *  1. the three timers the design names;
 *  2. a slow body that keeps moving is never cut, and a body that stops is
 *     cut after the idle window — but not a handler that is merely slow to
 *     read;
 *  3. on SIGTERM, a /v1 relay is cut at +2 s, a chat stream at +15 s, an
 *     upload is left to finish, and the process exits right after it does.
 */
import { spawn, type ChildProcess } from 'node:child_process';
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import http from 'node:http';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

import { afterAll, afterEach, beforeAll, describe, expect, it } from 'vitest';

const FRONTEND = join(dirname(fileURLToPath(import.meta.url)), '..');
const PRELOAD = join(FRONTEND, 'server-preload.cjs');

/** The stand-in for Next's standalone server. */
const SERVER_SOURCE = String.raw`
const http = require('node:http');
const server = http.createServer((req, res) => {
  const path = req.url.split('?')[0];
  if (path === '/settings') {
    res.writeHead(200, { 'content-type': 'application/json' });
    res.end(JSON.stringify({ requestTimeout: server.requestTimeout, headersTimeout: server.headersTimeout, keepAliveTimeout: server.keepAliveTimeout }));
    return;
  }
  if (path === '/body') {
    let n = 0;
    req.on('data', (c) => { n += c.length; });
    req.on('end', () => { res.writeHead(200, { 'content-type': 'application/json' }); res.end(JSON.stringify({ bytes: n })); });
    return;
  }
  if (path === '/late-reader') {
    // A handler that does not start reading for 3 s.
    setTimeout(() => {
      let n = 0;
      req.on('data', (c) => { n += c.length; });
      req.on('end', () => { res.end(JSON.stringify({ bytes: n })); });
    }, 3000);
    return;
  }
  if (path.startsWith('/v1/') || path === '/api/chat' || path === '/api/devplatform/playground') {
    res.writeHead(200, { 'content-type': 'text/event-stream' });
    res.write(': ping\n\n');
    const t = setInterval(() => res.write(': ping\n\n'), 500);
    res.on('close', () => clearInterval(t));
    return;
  }
  if (path === '/api/report') {
    // A response whose head (and some body) went out BEFORE the SIGTERM, and
    // which ends a few seconds after it — on a keep-alive connection.
    res.writeHead(200, { 'content-type': 'application/octet-stream' });
    res.write('start');
    const wait = Number(new URL(req.url, 'http://x').searchParams.get('ms') || 3000);
    setTimeout(() => res.end('end'), wait);
    return;
  }
  if (path.startsWith('/api/upload/')) {
    let n = 0;
    req.on('data', (c) => { n += c.length; });
    req.on('end', () => { res.writeHead(200, { 'content-type': 'application/json' }); res.end(JSON.stringify({ bytes: n })); });
    return;
  }
  res.writeHead(404);
  res.end();
});
server.listen(0, '127.0.0.1', () => { process.stdout.write('LISTENING ' + server.address().port + '\n'); });
let cleaning = false;
process.on('SIGTERM', () => {
  if (cleaning) return;
  cleaning = true;
  const started = Date.now();
  server.close(() => { process.stdout.write('EXITING ' + (Date.now() - started) + '\n'); process.exit(143); });
});
`;

let workdir = '';
let serverFile = '';
const children: ChildProcess[] = [];

beforeAll(() => {
  workdir = mkdtempSync(join(tmpdir(), 'server-preload-'));
  serverFile = join(workdir, 'server.cjs');
  writeFileSync(serverFile, SERVER_SOURCE);
});

afterAll(() => {
  rmSync(workdir, { recursive: true, force: true });
});

afterEach(() => {
  for (const child of children.splice(0)) if (child.exitCode === null) child.kill('SIGKILL');
});

interface Started {
  child: ChildProcess;
  port: number;
  output: () => string;
  exited: Promise<{ code: number | null; at: number }>;
}

async function start(env: Record<string, string> = {}): Promise<Started> {
  const child = spawn(process.execPath, ['--require', PRELOAD, serverFile], {
    env: { ...process.env, ...env },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  children.push(child);
  let out = '';
  child.stdout!.on('data', (c: Buffer) => {
    out += c.toString();
  });
  child.stderr!.on('data', (c: Buffer) => {
    out += c.toString();
  });
  const exited = new Promise<{ code: number | null; at: number }>((resolve) =>
    child.on('exit', (code) => resolve({ code, at: Date.now() })),
  );
  for (let i = 0; i < 200; i += 1) {
    const match = /LISTENING (\d+)/.exec(out);
    if (match) return { child, port: Number(match[1]), output: () => out, exited };
    await new Promise((r) => setTimeout(r, 25));
  }
  throw new Error(`the server never listened: ${out}`);
}

function getJson(port: number, path: string): Promise<unknown> {
  return new Promise((resolve, reject) => {
    http
      .get({ host: '127.0.0.1', port, path }, (res) => {
        let text = '';
        res.on('data', (c) => (text += c));
        res.on('end', () => resolve(JSON.parse(text)));
      })
      .on('error', reject);
  });
}

interface Outcome {
  status: number | null;
  body: string;
  error: string | null;
  closedAt: number;
}

/** A request whose body this test writes by hand, chunk by chunk. */
function openRequest(port: number, path: string, method = 'POST', headers: Record<string, string> = {}) {
  let resolveOutcome!: (o: Outcome) => void;
  const outcome = new Promise<Outcome>((r) => (resolveOutcome = r));
  let status: number | null = null;
  let body = '';
  let settled = false;
  const settle = (error: string | null) => {
    if (settled) return;
    settled = true;
    resolveOutcome({ status, body, error, closedAt: Date.now() });
  };
  const req = http.request({ host: '127.0.0.1', port, path, method, headers, agent: false }, (res) => {
    status = res.statusCode ?? null;
    res.on('data', (c) => (body += c));
    res.on('end', () => settle(null));
    res.on('error', (e) => settle(e.message));
    res.on('aborted', () => settle('aborted'));
  });
  req.on('error', (e) => settle((e as NodeJS.ErrnoException).code ?? e.message));
  req.on('close', () => settle(status === null ? 'closed' : null));
  return { req, outcome };
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

describe('the server timers', () => {
  it('are requestTimeout 0, headersTimeout 100 s and keepAliveTimeout 95 s', async () => {
    const { port } = await start();
    expect(await getJson(port, '/settings')).toEqual({
      requestTimeout: 0,
      headersTimeout: 100_000,
      keepAliveTimeout: 95_000,
    });
  });
});

describe('the body-idle guard', () => {
  it('never cuts a slow body that keeps moving: one byte every 300 ms for 4 s gets its 200', async () => {
    const { port } = await start();
    const { req, outcome } = openRequest(port, '/body', 'POST', { 'transfer-encoding': 'chunked' });
    for (let i = 0; i < 14; i += 1) {
      req.write('x');
      await sleep(300);
    }
    req.end();
    const result = await outcome;
    expect(result.status).toBe(200);
    expect(JSON.parse(result.body)).toEqual({ bytes: 14 });
  }, 20_000);

  it('never cuts a moving body even when the window is shorter than the gap between two bytes would add up to', async () => {
    // 1 s window, a byte every 300 ms for 4 s: the clock resets on each byte.
    const { port } = await start({ FRONTEND_BODY_IDLE_S: '1' });
    const { req, outcome } = openRequest(port, '/body', 'POST', { 'transfer-encoding': 'chunked' });
    for (let i = 0; i < 14; i += 1) {
      req.write('x');
      await sleep(300);
    }
    req.end();
    expect((await outcome).status).toBe(200);
  }, 20_000);

  it('destroys a body that stops arriving, once the window has passed', async () => {
    const { port, output } = await start({ FRONTEND_BODY_IDLE_S: '1' });
    const { req, outcome } = openRequest(port, '/body', 'POST', { 'content-length': '100' });
    req.write('x'.repeat(10));
    const stoppedAt = Date.now();
    const result = await outcome;
    const waited = result.closedAt - stoppedAt;
    expect(result.status).toBeNull();
    expect(waited).toBeGreaterThanOrEqual(1_000);
    expect(waited).toBeLessThan(2_000);
    expect(output()).toContain('"event":"body_idle"');
  }, 10_000);

  it('does not cut a handler that is slow to read what the client already sent', async () => {
    const { port } = await start({ FRONTEND_BODY_IDLE_S: '1' });
    const payload = 'y'.repeat(4 * 1024 * 1024);
    const { req, outcome } = openRequest(port, '/late-reader', 'POST', { 'content-length': String(payload.length) });
    req.end(payload);
    const result = await outcome;
    expect(JSON.parse(result.body)).toEqual({ bytes: payload.length });
  }, 15_000);

  const long = process.env.PRELOAD_LONG_TESTS === '1' ? it : it.skip;

  long('destroys a body idle for 61 s with the default 60 s window, and not before 60 s', async () => {
    const { port } = await start();
    const { req, outcome } = openRequest(port, '/body', 'POST', { 'content-length': '100' });
    req.write('x');
    const stoppedAt = Date.now();
    const result = await outcome;
    const waited = result.closedAt - stoppedAt;
    expect(result.status).toBeNull();
    expect(waited).toBeGreaterThanOrEqual(60_000);
    expect(waited).toBeLessThanOrEqual(66_000);
  }, 90_000);
});

describe('on SIGTERM', () => {
  it('cuts the /v1 relay by 2.5 s and the chat stream by 15.5 s, lets the upload finish, and exits right after it', async () => {
    const { child, port, output, exited } = await start();

    const v1 = openRequest(port, '/v1/responses', 'POST', { 'content-length': '2' });
    v1.req.end('{}');
    const chat = openRequest(port, '/api/chat', 'POST', { 'content-length': '2' });
    chat.req.end('{}');
    // An SSE answer on a path the preload does not name joins the chat class.
    const playground = openRequest(port, '/api/devplatform/playground', 'POST', { 'content-length': '2' });
    playground.req.end('{}');
    const upload = openRequest(port, '/api/upload/chunked/part', 'PUT', { 'transfer-encoding': 'chunked' });
    upload.req.write('a');
    await sleep(500);

    const termAt = Date.now();
    child.kill('SIGTERM');
    // The upload keeps sending for 18 s, one chunk every 250 ms.
    let sent = 1;
    while (Date.now() - termAt < 18_000) {
      await sleep(250);
      upload.req.write('a');
      sent += 1;
    }
    upload.req.end();

    const [v1Result, chatResult, playgroundResult, uploadResult, exit] = await Promise.all([
      v1.outcome,
      chat.outcome,
      playground.outcome,
      upload.outcome,
      exited,
    ]);
    const at = (t: number) => t - termAt;

    expect(at(v1Result.closedAt)).toBeGreaterThanOrEqual(1_900);
    expect(at(v1Result.closedAt)).toBeLessThanOrEqual(2_500);
    expect(v1Result.error).not.toBeNull();

    for (const stream of [chatResult, playgroundResult]) {
      expect(at(stream.closedAt)).toBeGreaterThanOrEqual(14_900);
      expect(at(stream.closedAt)).toBeLessThanOrEqual(15_500);
      expect(stream.error).not.toBeNull();
    }

    // The upload was never cut, and its answer is whole.
    expect(uploadResult.status).toBe(200);
    expect(JSON.parse(uploadResult.body)).toEqual({ bytes: sent });
    // The process leaves the moment the last response is done.
    expect(exit.code).toBe(143);
    expect(exit.at - uploadResult.closedAt).toBeLessThan(1_000);
    expect(output()).toContain('"event":"drain_start"');
  }, 40_000);

  it('lets a response already under way finish, then exits at once instead of holding its keep-alive socket', async () => {
    const { child, port, exited } = await start();
    const agent = new http.Agent({ keepAlive: true });
    let endedAt = 0;
    const done = new Promise<void>((resolve) => {
      http.get({ host: '127.0.0.1', port, path: '/api/report?ms=3000', agent }, (res) => {
        res.resume();
        res.on('end', () => {
          endedAt = Date.now();
          resolve();
        });
      });
    });
    await sleep(400);
    const termAt = Date.now();
    child.kill('SIGTERM');
    await done;
    const exit = await exited;
    expect(endedAt - termAt).toBeGreaterThanOrEqual(2_500);
    expect(exit.code).toBe(143);
    // Not the 95 s keep-alive timeout: the socket is let go when the answer ends.
    expect(exit.at - endedAt).toBeLessThan(1_000);
    agent.destroy();
  }, 15_000);

  it('closes idle keep-alive connections at once, and exits at once with nothing in flight', async () => {
    const { child, port, exited } = await start();
    const agent = new http.Agent({ keepAlive: true });
    const idleClosed = new Promise<number>((resolve) => {
      const req = http.get({ host: '127.0.0.1', port, path: '/settings', agent }, (res) => res.resume());
      req.on('socket', (socket) => socket.on('close', () => resolve(Date.now())));
    });
    await sleep(300);
    const termAt = Date.now();
    child.kill('SIGTERM');
    const closedAt = await idleClosed;
    const exit = await exited;
    expect(closedAt - termAt).toBeLessThan(500);
    expect(exit.code).toBe(143);
    expect(exit.at - termAt).toBeLessThan(1_000);
    agent.destroy();
  }, 10_000);
});
