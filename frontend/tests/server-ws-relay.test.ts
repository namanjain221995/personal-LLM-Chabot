/**
 * frontend/server-ws-relay.cjs and the upgrade half of server-preload.cjs
 * (2026-09-29, real-time dictation build spec §4).
 *
 * The wire tests run a REAL child process — `node --require
 * server-preload.cjs <server>` — as tests/server-preload.test.ts does,
 * because what is under test is sockets, 'upgrade' events and signal
 * handling, none of which a mock can show. The child server stands in for
 * Next's standalone start-server.js in the three ways that matter here: it
 * builds its server with `http.createServer(listener)`; it registers its OWN
 * 'upgrade' listener afterwards, which `socket.end()`s a WebSocket on any
 * /api/ path (Next does that to any path matching a route, and
 * /api/audio/sessions/[id] is one), answers one path itself and leaves every
 * other upgrade open with no answer; and on SIGTERM it calls
 * `server.close()` and exits 143 once that resolves.
 *
 * The orchestrator is a small upstream inside this process. What it does
 * with a handshake depends on the session id it is asked for: `echo-*`
 * answers 101 and echoes every byte, `greet-*` sends bytes in the same write
 * as its 101, `frames-*` speaks real WebSocket frames, `refuse-*` and `big-*`
 * answer 403 and 500, `noaccept-*` answers 101 with no accept key, and
 * `silent-*` never answers.
 *
 * What is pinned:
 *  1. an owned path is relayed, Next's own listener never sees it, and the
 *     answer carries only the handshake's own headers;
 *  2. the refusals, each before any upstream connection exists;
 *  3. exactly the named header set crosses, forwarding headers as lib/proxy.ts
 *     would send them and never as the client wrote them — and the CJS copy
 *     of lib/proxy.ts's rules agrees with the original on every input;
 *  4. the handshake deadline, non-101 answers, the idle limit and the cap;
 *  5. an upgrade nobody answers is destroyed, one somebody answered is not;
 *  6. on SIGTERM, relays and stray upgrades go at +2 s and the process exits;
 *  7. nothing about ordinary HTTP changes, the preload still serves without
 *     the relay file, and no log line carries a cookie, a query or an id.
 */
import { spawn, type ChildProcess } from 'node:child_process';
import { createHash } from 'node:crypto';
import { copyFileSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import http from 'node:http';
import { createRequire } from 'node:module';
import net from 'node:net';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from 'vitest';

import { SESSION_COOKIE } from '../lib/auth';
import {
  isIpLiteral as proxyIsIpLiteral,
  trustedClientIp as proxyTrustedClientIp,
  trustedForwardedProto as proxyTrustedForwardedProto,
} from '../lib/proxy';

const FRONTEND = join(dirname(fileURLToPath(import.meta.url)), '..');
const PRELOAD = join(FRONTEND, 'server-preload.cjs');

type Env = Record<string, string | undefined>;
type Fields = Record<string, unknown>;

interface RelayHandle {
  readonly size: number;
  beginDrain(): void;
  destroyAll(reason?: string): number;
}

interface RelayModule {
  SESSION_COOKIE: string;
  ownedSessionId(url: string | undefined): string | null;
  isIpLiteral(value: unknown): boolean;
  trustedClientIp(headers: Record<string, string | undefined>, headerName: string): string | null;
  trustedForwardedProto(value: string | undefined): string | null;
  requestOrigin(value: unknown): URL | null;
  hostMatchesOrigin(host: string | undefined, origin: URL): boolean;
  upstreamTarget(url: string): { hostname: string; port: number; host: string; basePath: string } | null;
  install(server: http.Server, env?: Env, options?: { log?: (event: string, fields: Fields) => void }): RelayHandle;
}

const relayModule = createRequire(import.meta.url)('../server-ws-relay.cjs') as RelayModule;

/** A well-formed Sec-WebSocket-Key: sixteen zero bytes. */
const KEY = 'AAAAAAAAAAAAAAAAAAAAAA==';
/** The fixed GUID of RFC 6455 §1.3, a public protocol constant, not a secret. */
const GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11';
const acceptFor = (key: string) => createHash('sha1').update(key + GUID).digest('base64');

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

async function waitFor<T>(probe: () => T | null | undefined | false, what: string, timeoutMs = 5_000): Promise<T> {
  const until = Date.now() + timeoutMs;
  for (;;) {
    const value = probe();
    if (value) return value;
    if (Date.now() > until) throw new Error(`timed out waiting for ${what}`);
    await sleep(20);
  }
}

/* ------------------------------------------------ the Next stand-in -- */

const SERVER_SOURCE = String.raw`
const http = require('node:http');
const server = http.createServer((req, res) => {
  const path = req.url.split('?')[0];
  if (path === '/hello') {
    res.writeHead(200, { 'content-type': 'application/json' });
    res.end(JSON.stringify({ ok: true }));
    return;
  }
  res.writeHead(404, { 'content-type': 'application/json' });
  res.end(JSON.stringify({ path, upgrade: req.headers.upgrade ?? null }));
});
// Registered after createServer, as Next's start-server.js does.
server.on('upgrade', (req, socket) => {
  const path = req.url.split('?')[0];
  process.stdout.write('APP_UPGRADE ' + path + '\n');
  if (path.startsWith('/api/')) { socket.end(); return; }
  if (path === '/app-ws') {
    socket.on('error', () => {});
    socket.write('HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n');
    socket.on('data', (d) => socket.write(d));
  }
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
  workdir = mkdtempSync(join(tmpdir(), 'server-ws-relay-'));
  serverFile = join(workdir, 'server.cjs');
  writeFileSync(serverFile, SERVER_SOURCE);
});

afterAll(() => {
  for (const child of children.splice(0)) if (child.exitCode === null) child.kill('SIGKILL');
  rmSync(workdir, { recursive: true, force: true });
});

interface Started {
  child: ChildProcess;
  port: number;
  output: () => string;
  exited: Promise<{ code: number | null; at: number }>;
}

/** The environment every child starts from: nothing inherited decides a test. */
const BASE_ENV: Env = {
  MOCK_MODE: 'false',
  TRUSTED_CLIENT_IP_HEADER: '',
  TRUSTED_FORWARDED_PROTO: '',
  FRONTEND_WS_ALLOWED_ORIGINS: '',
  FRONTEND_WS_MAX_RELAYS: '',
  FRONTEND_WS_IDLE_S: '',
  FRONTEND_WS_HANDSHAKE_S: '',
  FRONTEND_WS_STRAY_S: '',
  FRONTEND_DRAIN_WS_S: '',
};

async function start(env: Env = {}, preload = PRELOAD): Promise<Started> {
  const child = spawn(process.execPath, ['--require', preload, serverFile], {
    env: { ...process.env, ...BASE_ENV, ...env },
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
  const match = await waitFor(() => /LISTENING (\d+)/.exec(out), `the server to listen: ${out}`);
  return { child, port: Number(match[1]), output: () => out, exited };
}

/** The server-ws-relay (or server-preload) JSON lines a child has printed. */
function events(output: string, component = 'server-ws-relay'): Fields[] {
  const found: Fields[] = [];
  for (const line of output.split('\n')) {
    if (!line.startsWith('{')) continue;
    try {
      const parsed = JSON.parse(line) as Fields;
      if (parsed.component === component) found.push(parsed);
    } catch {
      /* not a log line */
    }
  }
  return found;
}

/* ------------------------------------------- the orchestrator stand-in -- */

interface Seen {
  url: string;
  headers: http.IncomingHttpHeaders;
  /** Every byte received after the 101. */
  received: Buffer;
  closed: boolean;
}

interface Upstream {
  port: number;
  seen: Seen[];
  close: () => Promise<void>;
}

function encodeFrame(opcode: number, payload: Buffer, masked: boolean): Buffer {
  // Every payload in this file is shorter than 126 bytes.
  const header = Buffer.from([0x80 | opcode, (masked ? 0x80 : 0) | payload.length]);
  if (!masked) return Buffer.concat([header, payload]);
  const mask = Buffer.from([0x11, 0x22, 0x33, 0x44]);
  return Buffer.concat([header, mask, Buffer.from(payload.map((b, i) => b ^ mask[i % 4]))]);
}

function readFrame(buffer: Buffer): { opcode: number; payload: Buffer; size: number } | null {
  if (buffer.length < 2) return null;
  const masked = (buffer[1] & 0x80) !== 0;
  const offset = 2 + (masked ? 4 : 0);
  const size = offset + (buffer[1] & 0x7f);
  if (buffer.length < size) return null;
  let payload = Buffer.from(buffer.subarray(offset, size));
  if (masked) {
    const mask = buffer.subarray(2, 6);
    payload = Buffer.from(payload.map((b, i) => b ^ mask[i % 4]));
  }
  return { opcode: buffer[0] & 0x0f, payload, size };
}

async function startUpstream(): Promise<Upstream> {
  const seen: Seen[] = [];
  const sockets = new Set<net.Socket>();
  const server = http.createServer((_req, res) => {
    res.writeHead(404);
    res.end();
  });
  server.on('upgrade', (req: http.IncomingMessage, socket: net.Socket) => {
    const record: Seen = { url: req.url ?? '', headers: req.headers, received: Buffer.alloc(0), closed: false };
    seen.push(record);
    sockets.add(socket);
    socket.on('error', () => undefined);
    socket.on('close', () => {
      record.closed = true;
      sockets.delete(socket);
    });
    const id = /^\/audio\/sessions\/([^/]+)\/live$/.exec(record.url)?.[1] ?? '';
    const mode = id.split('-')[0];
    const protocol = String(req.headers['sec-websocket-protocol'] ?? '').split(',')[0].trim();
    const switching = `${[
      'HTTP/1.1 101 Switching Protocols',
      'Upgrade: websocket',
      'Connection: Upgrade',
      ...(mode === 'noaccept' ? [] : [`Sec-WebSocket-Accept: ${acceptFor(String(req.headers['sec-websocket-key']))}`]),
      ...(protocol ? [`Sec-WebSocket-Protocol: ${protocol}`] : []),
      // What the relay must keep on this side.
      'Server: test-upstream',
      'X-Upstream-Internal: engine-1',
      'Sec-WebSocket-Extensions: permessage-deflate',
    ].join('\r\n')}\r\n\r\n`;
    const collect = (chunk: Buffer) => {
      record.received = Buffer.concat([record.received, chunk]);
    };

    if (mode === 'refuse') {
      const body = JSON.stringify({ detail: 'refused by the test upstream' });
      socket.end(`HTTP/1.1 403 Forbidden\r\nContent-Type: application/json\r\nContent-Length: ${body.length}\r\nConnection: close\r\n\r\n${body}`);
      return;
    }
    if (mode === 'big') {
      const body = 'x'.repeat(5_000);
      socket.end(`HTTP/1.1 500 Internal Server Error\r\nContent-Type: text/plain\r\nContent-Length: ${body.length}\r\nConnection: close\r\n\r\n${body}`);
      return;
    }
    if (mode === 'silent') {
      // Reads (so a dropped connection is noticed) and never answers.
      socket.resume();
      socket.on('end', () => socket.end());
      return;
    }
    if (mode === 'frames') {
      socket.write(switching);
      let buffer = Buffer.alloc(0);
      socket.on('data', (chunk: Buffer) => {
        collect(chunk);
        buffer = Buffer.concat([buffer, chunk]);
        for (let frame = readFrame(buffer); frame; frame = readFrame(buffer)) {
          buffer = buffer.subarray(frame.size);
          if (frame.opcode === 0x8) {
            // The closing handshake: answer with the same code, then close the
            // TCP connection, which is the server's job (RFC 6455 §7.1.1).
            socket.end(encodeFrame(0x8, frame.payload, false));
            return;
          }
          socket.write(encodeFrame(frame.opcode, frame.payload, false));
        }
      });
      return;
    }
    // One write, so the greeting arrives in the same packet as the 101.
    socket.write(mode === 'greet' ? `${switching}HELLO` : switching);
    socket.on('data', (chunk: Buffer) => {
      collect(chunk);
      socket.write(chunk);
    });
    // Like uvicorn: a client that goes away takes the connection with it.
    socket.on('end', () => socket.end());
  });
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
  const port = (server.address() as net.AddressInfo).port;
  return {
    port,
    seen,
    close: async () => {
      for (const socket of sockets) socket.destroy();
      await new Promise<void>((resolve) => server.close(() => resolve()));
    },
  };
}

/* --------------------------------------------------- a raw WS client -- */

interface Answer {
  /** null: the connection closed before any status line arrived. */
  status: number | null;
  headers: Record<string, string>;
}

interface Wire {
  socket: net.Socket;
  answer: Promise<Answer>;
  /** When the connection closed. */
  closed: Promise<number>;
  /** Every byte after the answer's header block. */
  body: () => Buffer;
  isClosed: () => boolean;
}

/** A WebSocket handshake written by hand, so every header is this test's choice. */
function handshake(
  port: number,
  path: string,
  headers: Record<string, string | undefined>,
  opts: { method?: string; head?: Buffer } = {},
): Wire {
  const socket = net.connect(port, '127.0.0.1');
  let received = Buffer.alloc(0);
  let headerEnd = -1;
  let closedAt = 0;
  let resolveAnswer!: (a: Answer) => void;
  const answer = new Promise<Answer>((resolve) => (resolveAnswer = resolve));
  const closed = new Promise<number>((resolve) =>
    socket.on('close', () => {
      closedAt = Date.now();
      if (headerEnd < 0) resolveAnswer({ status: null, headers: {} });
      resolve(closedAt);
    }),
  );
  socket.on('error', () => undefined);
  socket.on('data', (chunk: Buffer) => {
    received = Buffer.concat([received, chunk]);
    if (headerEnd >= 0) return;
    const end = received.indexOf('\r\n\r\n');
    if (end < 0) return;
    headerEnd = end + 4;
    const [statusLine, ...lines] = received.subarray(0, end).toString('latin1').split('\r\n');
    const parsed: Record<string, string> = {};
    for (const line of lines) {
      const colon = line.indexOf(':');
      parsed[line.slice(0, colon).trim().toLowerCase()] = line.slice(colon + 1).trim();
    }
    resolveAnswer({ status: Number(statusLine.split(' ')[1]), headers: parsed });
  });
  socket.on('connect', () => {
    const lines = [`${opts.method ?? 'GET'} ${path} HTTP/1.1`];
    for (const [name, value] of Object.entries(headers)) if (value !== undefined) lines.push(`${name}: ${value}`);
    socket.write(Buffer.concat([Buffer.from(`${lines.join('\r\n')}\r\n\r\n`, 'latin1'), opts.head ?? Buffer.alloc(0)]));
  });
  return {
    socket,
    answer,
    closed,
    body: () => (headerEnd < 0 ? Buffer.alloc(0) : received.subarray(headerEnd)),
    isClosed: () => closedAt !== 0,
  };
}

/** What a browser on the page's own origin sends. */
function browser(port: number, overrides: Record<string, string | undefined> = {}): Record<string, string | undefined> {
  return {
    Host: `127.0.0.1:${port}`,
    Upgrade: 'websocket',
    Connection: 'Upgrade',
    'Sec-WebSocket-Key': KEY,
    'Sec-WebSocket-Version': '13',
    'Sec-WebSocket-Protocol': 'techsara.voice.v1',
    Origin: `http://127.0.0.1:${port}`,
    'Sec-Fetch-Site': 'same-origin',
    Cookie: 'theme=dark; ts_session=cookie-value-1',
    'User-Agent': 'relay-test/1',
    ...overrides,
  };
}

function getText(port: number, path: string): Promise<{ status: number; text: string }> {
  return new Promise((resolve, reject) => {
    http
      .get({ host: '127.0.0.1', port, path, agent: false }, (res) => {
        let text = '';
        res.on('data', (c) => (text += c));
        res.on('end', () => resolve({ status: res.statusCode ?? 0, text }));
      })
      .on('error', reject);
  });
}

/* ================================================================ tests == */

describe('which upgrades the relay owns', () => {
  it.each<[string, string | null]>([
    ['/api/audio/sessions/0123456789abcdef0123456789abcdef/live', '0123456789abcdef0123456789abcdef'],
    ['/api/audio/sessions/echo-1/live?note=x', 'echo-1'],
    ['/api/audio/sessions/A_b-9/live', 'A_b-9'],
    [`/api/audio/sessions/${'a'.repeat(64)}/live`, 'a'.repeat(64)],
    [`/api/audio/sessions/${'a'.repeat(65)}/live`, null],
    ['/api/audio/sessions/a.b/live', null],
    ['/api/audio/sessions/a%2Fb/live', null],
    ['/api/audio/sessions//live', null],
    ['/api/audio/sessions/abc/live/', null],
    ['/api/audio/sessions/abc', null],
    ['//api/audio/sessions/abc/live', null],
    ['http://evil.example/api/audio/sessions/abc/live', null],
    ['/api/chat', null],
  ])('%s -> %s', (url, id) => {
    expect(relayModule.ownedSessionId(url)).toBe(id);
  });
});

describe('the CJS rules match lib/proxy.ts and lib/auth.ts', () => {
  afterEach(() => {
    vi.unstubAllEnvs();
  });

  const ADDRESSES = [
    '203.0.113.9',
    '0.0.0.0',
    '255.255.255.255',
    '256.0.0.1',
    '203.0.113',
    '203.0.113.9:8080',
    '2001:db8::42',
    '::1',
    '::ffff:203.0.113.9',
    '2001:db8:0:0:0:0:2:1',
    '2001:db8::42:8080:1:2:3:4:5',
    '1:2:3:4:5:6:7:8:9',
    ':::1',
    'fe80::1',
    'localhost',
    'evil.example',
    '203.0.113.9 ',
    '203.0.113.9,10.0.0.1',
    '1.2.3.4 5.6.7.8',
    'x'.repeat(46),
    '',
  ];

  it('isIpLiteral agrees with lib/proxy.ts on every shape either documents', () => {
    for (const value of ADDRESSES) expect(relayModule.isIpLiteral(value), value).toBe(proxyIsIpLiteral(value));
  });

  it('trustedClientIp agrees with lib/proxy.ts, header by header and value by value', () => {
    const values = [...ADDRESSES, '203.0.113.9, 10.0.0.1', '  198.51.100.7  ', '2001:db8::42, 10.0.0.1'];
    for (const name of ['', 'cf-connecting-ip', 'x-forwarded-for']) {
      vi.stubEnv('TRUSTED_CLIENT_IP_HEADER', name);
      for (const value of values) {
        const headers = new Headers();
        if (value) headers.set('cf-connecting-ip', value);
        headers.set('x-forwarded-for', '198.51.100.8');
        const req = new Request('http://localhost:3000/api/audio/sessions/x/live', { headers });
        // The relay reads node's parsed headers: the same trimmed values.
        const nodeHeaders: Record<string, string | undefined> = {
          'cf-connecting-ip': headers.get('cf-connecting-ip') ?? undefined,
          'x-forwarded-for': headers.get('x-forwarded-for') ?? undefined,
        };
        expect(relayModule.trustedClientIp(nodeHeaders, name), `${name}: ${value}`).toBe(proxyTrustedClientIp(req));
      }
    }
  });

  it('trustedForwardedProto agrees with lib/proxy.ts', () => {
    for (const value of ['', 'https', 'http', 'HTTPS', ' https ', 'ftp', 'https, http']) {
      vi.stubEnv('TRUSTED_FORWARDED_PROTO', value);
      expect(relayModule.trustedForwardedProto(value), value).toBe(proxyTrustedForwardedProto());
    }
  });

  it('checks for the cookie lib/auth.ts names', () => {
    expect(relayModule.SESSION_COOKIE).toBe(SESSION_COOKIE);
  });
});

describe('same-origin, as the relay judges it', () => {
  const origin = (value: string) => {
    const url = relayModule.requestOrigin(value);
    if (!url) throw new Error(`not an origin: ${value}`);
    return url;
  };

  it('takes only an Origin spelled the way a browser serializes one', () => {
    for (const ok of ['https://ai.example', 'http://127.0.0.1:3000', 'http://[::1]:3000']) {
      expect(relayModule.requestOrigin(ok)?.origin).toBe(ok);
    }
    for (const bad of ['null', 'https://ai.example/', 'https://ai.example/x', 'HTTPS://AI.EXAMPLE', 'https://ai.example:443', 'ws://ai.example', 'file://x', '']) {
      expect(relayModule.requestOrigin(bad), bad).toBeNull();
    }
  });

  it.each<[string, string, boolean]>([
    ['ai.example', 'https://ai.example', true],
    ['ai.example', 'http://ai.example', true],
    ['ai.example:443', 'https://ai.example', true],
    ['AI.Example', 'https://ai.example', true],
    ['ai.example:8443', 'https://ai.example:8443', true],
    ['192.168.9.54:3000', 'http://192.168.9.54:3000', true],
    ['[::1]:3000', 'http://[::1]:3000', true],
    ['ai.example:80', 'https://ai.example', false],
    ['ai.example', 'http://ai.example:8080', false],
    ['ai.example:3000', 'http://ai.example:3001', false],
    ['evil.example', 'https://ai.example', false],
    ['ai.example.evil.example', 'https://ai.example', false],
    ['ai.example:99999', 'https://ai.example', false],
    ['user@ai.example', 'https://ai.example', false],
    ['ai.example/x', 'https://ai.example', false],
  ])('Host %s and Origin %s: %s', (host, value, expected) => {
    expect(relayModule.hostMatchesOrigin(host, origin(value))).toBe(expected);
  });

  it('builds the upstream target from ORCHESTRATOR_URL, http only', () => {
    expect(relayModule.upstreamTarget('http://orchestrator:8080')).toEqual({
      hostname: 'orchestrator',
      port: 8080,
      host: 'orchestrator:8080',
      basePath: '',
    });
    expect(relayModule.upstreamTarget('http://[::1]:8080/base/')).toEqual({
      hostname: '::1',
      port: 8080,
      host: '[::1]:8080',
      basePath: '/base',
    });
    expect(relayModule.upstreamTarget('http://orchestrator')?.port).toBe(80);
    expect(relayModule.upstreamTarget('https://orchestrator:8080')).toBeNull();
    expect(relayModule.upstreamTarget('not a url')).toBeNull();
  });
});

describe('an owned path is relayed, and Next never sees it', () => {
  let upstream: Upstream;
  let app: Started;
  beforeAll(async () => {
    upstream = await startUpstream();
    app = await start({ ORCHESTRATOR_URL: `http://127.0.0.1:${upstream.port}` });
  });
  afterAll(async () => {
    app.child.kill('SIGKILL');
    await upstream.close();
  });

  it('answers 101 with the orchestrator’s accept key and subprotocol, and nothing else of its', async () => {
    const before = upstream.seen.length;
    const ws = handshake(app.port, '/api/audio/sessions/echo-1/live?note=query-value-1', browser(app.port));
    const answer = await ws.answer;
    expect(answer.status).toBe(101);
    expect(answer.headers['sec-websocket-accept']).toBe(acceptFor(KEY));
    expect(answer.headers['sec-websocket-protocol']).toBe('techsara.voice.v1');
    expect(answer.headers.upgrade?.toLowerCase()).toBe('websocket');
    expect(answer.headers.connection?.toLowerCase()).toBe('upgrade');
    expect(Object.keys(answer.headers).sort()).toEqual(
      ['connection', 'sec-websocket-accept', 'sec-websocket-protocol', 'upgrade'].sort(),
    );
    // The query string never reaches the orchestrator; the path is its own.
    expect(upstream.seen[before].url).toBe('/audio/sessions/echo-1/live');

    ws.socket.write('frame-one');
    await waitFor(() => ws.body().toString().includes('frame-one'), 'the first echo');
    // Next's own listener would have ended this socket within a millisecond.
    await sleep(300);
    ws.socket.write('frame-two');
    await waitFor(() => ws.body().toString().includes('frame-two'), 'the second echo');
    expect(ws.isClosed()).toBe(false);
    expect(app.output()).not.toContain('APP_UPGRADE /api/audio/sessions/');

    const opened = events(app.output()).find((e) => e.event === 'ws_open');
    expect(opened).toMatchObject({ relays: 1 });
    ws.socket.end();
    const closed = await waitFor(() => events(app.output()).find((e) => e.event === 'ws_close'), 'ws_close');
    expect(closed).toMatchObject({
      reason: 'client_closed',
      upstream_status: 101,
      bytes_up: 'frame-oneframe-two'.length,
      bytes_down: 'frame-oneframe-two'.length,
    });
    await ws.closed;
  });

  it('carries the bytes that came with each side’s handshake: client ones up after the 101, orchestrator ones down right behind it', async () => {
    const before = upstream.seen.length;
    const ws = handshake(app.port, '/api/audio/sessions/greet-1/live', browser(app.port), { head: Buffer.from('EARLY') });
    expect((await ws.answer).status).toBe(101);
    await waitFor(() => ws.body().toString().startsWith('HELLO'), 'the greeting');
    // The echo proves the client's early bytes went up, and first.
    await waitFor(() => upstream.seen[before]?.received.toString() === 'EARLY', 'the early bytes upstream');
    await waitFor(() => ws.body().toString() === 'HELLOEARLY', 'the early bytes echoed');
    ws.socket.destroy();
  });

  it('relays real WebSocket frames and a closing handshake end to end', async () => {
    const ws = handshake(app.port, '/api/audio/sessions/frames-1/live', browser(app.port));
    expect((await ws.answer).status).toBe(101);
    ws.socket.write(encodeFrame(0x1, Buffer.from('hello relay'), true));
    await waitFor(() => readFrame(ws.body()), 'the echoed frame');
    expect(readFrame(ws.body())).toMatchObject({ opcode: 0x1, payload: Buffer.from('hello relay') });

    const code = Buffer.from([0x03, 0xe8]); // 1000
    const marker = ws.body().length;
    ws.socket.write(encodeFrame(0x8, code, true));
    const closedAt = await ws.closed;
    const closing = readFrame(ws.body().subarray(marker));
    expect(closing).toMatchObject({ opcode: 0x8, payload: code });
    expect(closedAt).toBeGreaterThan(0);
    const closed = await waitFor(
      () => events(app.output()).find((e) => e.event === 'ws_close' && e.reason === 'upstream_closed'),
      'ws_close after the closing handshake',
    );
    expect(closed.upstream_status).toBe(101);
  });

  it('sends the orchestrator exactly the named headers, and none a client wrote about where it came from', async () => {
    const before = upstream.seen.length;
    const ws = handshake(
      app.port,
      '/api/audio/sessions/echo-2/live',
      browser(app.port, {
        'X-Forwarded-For': '198.51.100.8',
        'X-Forwarded-Proto': 'http',
        'X-Forwarded-Host': 'evil.example',
        Forwarded: 'for=198.51.100.12',
        'CF-Connecting-IP': '198.51.100.7',
        'X-Real-IP': '198.51.100.9',
        'True-Client-IP': '198.51.100.10',
        'X-Client-IP': '198.51.100.11',
        'X-TechSara-Attempt': 'client-attempt',
        'Sec-WebSocket-Extensions': 'permessage-deflate; client_max_window_bits',
        Authorization: 'Bearer test-key-1',
        'Proxy-Authorization': 'Bearer test-key-1',
        Referer: `http://127.0.0.1:${app.port}/chat`,
        'Accept-Language': 'gu',
      }),
    );
    expect((await ws.answer).status).toBe(101);
    const seen = upstream.seen[before].headers;
    expect(Object.keys(seen).sort()).toEqual(
      [
        'connection',
        'cookie',
        'host',
        'origin',
        'sec-websocket-key',
        'sec-websocket-protocol',
        'sec-websocket-version',
        'upgrade',
        'user-agent',
        'x-forwarded-host',
      ].sort(),
    );
    expect(seen).toMatchObject({
      host: `127.0.0.1:${upstream.port}`,
      connection: 'Upgrade',
      upgrade: 'websocket',
      'sec-websocket-key': KEY,
      'sec-websocket-version': '13',
      'sec-websocket-protocol': 'techsara.voice.v1',
      cookie: 'theme=dark; ts_session=cookie-value-1',
      origin: `http://127.0.0.1:${app.port}`,
      'user-agent': 'relay-test/1',
      // The Host the browser addressed, not the one the client claimed.
      'x-forwarded-host': `127.0.0.1:${app.port}`,
    });
    ws.socket.destroy();
  });

  it('relays a refusal from the orchestrator — status, type and a small body — and closes', async () => {
    const ws = handshake(app.port, '/api/audio/sessions/refuse-1/live', browser(app.port));
    const answer = await ws.answer;
    expect(answer.status).toBe(403);
    expect(answer.headers['content-type']).toBe('application/json');
    await ws.closed;
    expect(JSON.parse(ws.body().toString())).toEqual({ detail: 'refused by the test upstream' });
    const closed = await waitFor(
      () => events(app.output()).find((e) => e.event === 'ws_close' && e.upstream_status === 403),
      'ws_close for the refusal',
    );
    expect(closed.reason).toBe('upstream_refused');
  });

  it('relays the status of an answer whose body is too big to pass on, without the body', async () => {
    const ws = handshake(app.port, '/api/audio/sessions/big-1/live', browser(app.port));
    const answer = await ws.answer;
    expect(answer.status).toBe(500);
    expect(answer.headers['content-length']).toBe('0');
    await ws.closed;
    expect(ws.body().length).toBe(0);
  });

  it('answers 502 when the orchestrator says 101 without the accept key', async () => {
    const ws = handshake(app.port, '/api/audio/sessions/noaccept-1/live', browser(app.port));
    expect((await ws.answer).status).toBe(502);
    await ws.closed;
    await waitFor(
      () => events(app.output()).find((e) => e.event === 'ws_close' && e.reason === 'upstream_protocol'),
      'ws_close upstream_protocol',
    );
  });

  it('leaves ordinary requests alone, the owned path included when it is not an upgrade', async () => {
    const before = upstream.seen.length;
    expect(await getText(app.port, '/hello')).toEqual({ status: 200, text: JSON.stringify({ ok: true }) });
    const plain = await getText(app.port, '/api/audio/sessions/echo-3/live');
    expect(plain.status).toBe(404);
    expect(JSON.parse(plain.text)).toEqual({ path: '/api/audio/sessions/echo-3/live', upgrade: null });
    expect(upstream.seen.length).toBe(before);
  });

  it.each([
    '/api/audio/sessions/echo-4/live/',
    '/api/audio/sessions/echo.4/live',
    '/api/audio/sessions/echo-4',
    '/api/chat',
  ])('hands a look-alike path, %s, to Next untouched', async (path) => {
    const before = upstream.seen.length;
    const ws = handshake(app.port, path, browser(app.port));
    // The stand-in, like Next, ends a WebSocket on a route path at once.
    expect((await ws.answer).status).toBeNull();
    await waitFor(() => app.output().includes(`APP_UPGRADE ${path}\n`), `the app to see ${path}`);
    expect(upstream.seen.length).toBe(before);
  });

  it('never logs the cookie, the query string or the session id', () => {
    const out = app.output();
    expect(out).toContain('"event":"ws_close"');
    for (const secret of ['cookie-value-1', 'query-value-1', 'echo-1', 'greet-1', 'frames-1', 'ts_session']) {
      expect(out).not.toContain(secret);
    }
  });
});

describe('refused before any upstream connection exists', () => {
  let upstream: Upstream;
  let app: Started;
  beforeAll(async () => {
    upstream = await startUpstream();
    app = await start({
      ORCHESTRATOR_URL: `http://127.0.0.1:${upstream.port}`,
      FRONTEND_WS_ALLOWED_ORIGINS: 'https://allowed.example, https://Other.Example:8443/ , not a url',
    });
  });
  afterAll(async () => {
    app.child.kill('SIGKILL');
    await upstream.close();
  });

  const PATH = '/api/audio/sessions/echo-9/live';
  type Case = [string, (port: number) => Record<string, string | undefined>, { method?: string }, number, string];
  const cases: Case[] = [
    ['a POST', (p) => browser(p), { method: 'POST' }, 405, 'method'],
    ['an upgrade to something other than websocket', (p) => browser(p, { Upgrade: 'h2c' }), {}, 400, 'not_websocket'],
    ['no Sec-WebSocket-Key', (p) => browser(p, { 'Sec-WebSocket-Key': undefined }), {}, 400, 'bad_handshake'],
    ['a malformed Sec-WebSocket-Key', (p) => browser(p, { 'Sec-WebSocket-Key': 'short' }), {}, 400, 'bad_handshake'],
    ['WebSocket version 8', (p) => browser(p, { 'Sec-WebSocket-Version': '8' }), {}, 426, 'version'],
    ['Sec-Fetch-Site: cross-site', (p) => browser(p, { 'Sec-Fetch-Site': 'cross-site' }), {}, 403, 'cross_site'],
    ['Sec-Fetch-Site: same-site (a sibling subdomain)', (p) => browser(p, { 'Sec-Fetch-Site': 'same-site' }), {}, 403, 'cross_site'],
    ['no Origin', (p) => browser(p, { Origin: undefined }), {}, 403, 'origin_missing'],
    ['a foreign Origin', (p) => browser(p, { Origin: 'https://evil.example' }), {}, 403, 'origin_mismatch'],
    ['the right host on another port', (p) => browser(p, { Origin: `http://127.0.0.1:${p + 1}` }), {}, 403, 'origin_mismatch'],
    ['Origin: null', (p) => browser(p, { Origin: 'null' }), {}, 403, 'origin_mismatch'],
    ['an Origin with a path', (p) => browser(p, { Origin: `http://127.0.0.1:${p}/` }), {}, 403, 'origin_mismatch'],
    ['a Host on the scheme’s other default port', () => browser(0, { Host: 'app.test:80', Origin: 'https://app.test' }), {}, 403, 'origin_mismatch'],
    ['a Host that is not the Origin’s', () => browser(0, { Host: 'evil.test', Origin: 'https://app.test' }), {}, 403, 'origin_mismatch'],
    ['no cookie at all', (p) => browser(p, { Cookie: undefined }), {}, 401, 'no_session'],
    ['cookies, but no session cookie', (p) => browser(p, { Cookie: 'theme=dark; ts_session_old=x' }), {}, 401, 'no_session'],
    ['an empty session cookie', (p) => browser(p, { Cookie: 'ts_session=; theme=dark' }), {}, 401, 'no_session'],
  ];

  it.each(cases)('refuses %s', async (_what, headersFor, opts, status, reason) => {
    const before = upstream.seen.length;
    const refusedBefore = events(app.output()).filter((e) => e.event === 'ws_refused').length;
    const ws = handshake(app.port, PATH, headersFor(app.port), opts);
    const answer = await ws.answer;
    expect(answer.status).toBe(status);
    expect(answer.headers['content-length']).toBe('0');
    if (status === 405) expect(answer.headers.allow).toBe('GET');
    if (status === 426) expect(answer.headers['sec-websocket-version']).toBe('13');
    await ws.closed;
    const refused = await waitFor(
      () => events(app.output()).filter((e) => e.event === 'ws_refused')[refusedBefore],
      'the ws_refused line',
    );
    expect(refused).toMatchObject({ reason, status });
    // Only an Origin that was the reason is written down.
    if (reason === 'origin_mismatch') expect(refused.origin).toBe(headersFor(app.port).Origin);
    else expect(refused.origin).toBeUndefined();
    expect(upstream.seen.length).toBe(before);
    // And Next never saw it either.
    expect(app.output()).not.toContain('APP_UPGRADE /api/audio/sessions/');
  });

  it.each<[string, (port: number) => Record<string, string | undefined>]>([
    ['Sec-Fetch-Site: none', (p: number) => browser(p, { 'Sec-Fetch-Site': 'none' })],
    ['no Sec-Fetch-Site at all', (p: number) => browser(p, { 'Sec-Fetch-Site': undefined })],
    ['a Host with no port and an https Origin', () => browser(0, { Host: 'app.test', Origin: 'https://app.test' })],
    ['a Host with no port and an http Origin', () => browser(0, { Host: 'app.test', Origin: 'http://app.test' })],
    ['a Host and Origin on port 8443', () => browser(0, { Host: 'app.test:8443', Origin: 'https://app.test:8443' })],
    ['an upper-case Host', () => browser(0, { Host: 'APP.test', Origin: 'http://app.test' })],
    ['an Origin on FRONTEND_WS_ALLOWED_ORIGINS', (p: number) => browser(p, { Origin: 'https://allowed.example' })],
    ['a listed Origin written loosely in the setting', (p: number) => browser(p, { Origin: 'https://other.example:8443' })],
  ])('lets through %s', async (_what, headersFor) => {
    const before = upstream.seen.length;
    const ws = handshake(app.port, PATH, headersFor(app.port));
    expect((await ws.answer).status).toBe(101);
    expect(upstream.seen.length).toBe(before + 1);
    ws.socket.destroy();
  });

  it('counts the settings it could not read, and names none of them', () => {
    const ready = events(app.output()).find((e) => e.event === 'ws_relay_ready');
    expect(ready).toMatchObject({ upstream: 'ok', allowed_origins: 2, allowed_origins_rejected: 1, max_relays: 512 });
    expect(ready).toMatchObject({ idle_s: 150, handshake_s: 15 });
  });
});

describe('the caller’s address and scheme go upstream only as lib/proxy.ts sends them', () => {
  let upstream: Upstream;
  let trusting: Started;
  let plain: Started;
  beforeAll(async () => {
    upstream = await startUpstream();
    [trusting, plain] = await Promise.all([
      start({
        ORCHESTRATOR_URL: `http://127.0.0.1:${upstream.port}`,
        TRUSTED_CLIENT_IP_HEADER: 'CF-Connecting-IP',
        TRUSTED_FORWARDED_PROTO: 'https',
      }),
      start({ ORCHESTRATOR_URL: `http://127.0.0.1:${upstream.port}` }),
    ]);
  });
  afterAll(async () => {
    trusting.child.kill('SIGKILL');
    plain.child.kill('SIGKILL');
    await upstream.close();
  });

  it.each<[string | undefined, string | undefined]>([
    ['203.0.113.9', '203.0.113.9'],
    ['203.0.113.9, 10.0.0.1', '203.0.113.9'],
    ['2001:db8::1', '2001:db8::1'],
    ['::ffff:203.0.113.9', '::ffff:203.0.113.9'],
    ['203.0.113.9:8080', undefined],
    ['evil.example', undefined],
    ['1.2.3.4 5.6.7.8', undefined],
    [undefined, undefined],
  ])('trusted header %s -> x-forwarded-for %s', async (value, expected) => {
    const before = upstream.seen.length;
    const ws = handshake(
      trusting.port,
      '/api/audio/sessions/echo-5/live',
      browser(trusting.port, { 'CF-Connecting-IP': value, 'X-Forwarded-For': '198.51.100.8' }),
    );
    expect((await ws.answer).status).toBe(101);
    const seen = upstream.seen[before].headers;
    expect(seen['x-forwarded-for']).toBe(expected);
    expect(seen['x-forwarded-proto']).toBe('https');
    expect(seen['cf-connecting-ip']).toBeUndefined();
    ws.socket.destroy();
  });

  it('forwards neither when the deployment names no header and no scheme', async () => {
    const before = upstream.seen.length;
    const ws = handshake(
      plain.port,
      '/api/audio/sessions/echo-6/live',
      browser(plain.port, { 'CF-Connecting-IP': '203.0.113.9', 'X-Forwarded-Proto': 'https' }),
    );
    expect((await ws.answer).status).toBe(101);
    const seen = upstream.seen[before].headers;
    expect(seen['x-forwarded-for']).toBeUndefined();
    expect(seen['x-forwarded-proto']).toBeUndefined();
    ws.socket.destroy();
  });
});

describe('limits', () => {
  let upstream: Upstream;
  beforeAll(async () => {
    upstream = await startUpstream();
  });
  afterAll(async () => {
    await upstream.close();
  });

  it('refuses the relay past FRONTEND_WS_MAX_RELAYS with 503, and takes one again once a slot frees', async () => {
    const app = await start({ ORCHESTRATOR_URL: `http://127.0.0.1:${upstream.port}`, FRONTEND_WS_MAX_RELAYS: '1' });
    const first = handshake(app.port, '/api/audio/sessions/echo-7/live', browser(app.port));
    expect((await first.answer).status).toBe(101);
    const before = upstream.seen.length;
    const second = handshake(app.port, '/api/audio/sessions/echo-8/live', browser(app.port));
    expect((await second.answer).status).toBe(503);
    expect(upstream.seen.length).toBe(before);
    expect(events(app.output()).find((e) => e.event === 'ws_refused')).toMatchObject({ reason: 'capacity', status: 503 });

    first.socket.end();
    await waitFor(() => events(app.output()).find((e) => e.event === 'ws_close'), 'the first relay to close');
    const third = handshake(app.port, '/api/audio/sessions/echo-9/live', browser(app.port));
    expect((await third.answer).status).toBe(101);
    third.socket.destroy();
    app.child.kill('SIGKILL');
  });

  it('refuses every relay when FRONTEND_WS_MAX_RELAYS is 0', async () => {
    const app = await start({ ORCHESTRATOR_URL: `http://127.0.0.1:${upstream.port}`, FRONTEND_WS_MAX_RELAYS: '0' });
    const ws = handshake(app.port, '/api/audio/sessions/echo-10/live', browser(app.port));
    expect((await ws.answer).status).toBe(503);
    app.child.kill('SIGKILL');
  });

  it('answers 504 when the orchestrator says nothing for FRONTEND_WS_HANDSHAKE_S, and lets go of it', async () => {
    const app = await start({ ORCHESTRATOR_URL: `http://127.0.0.1:${upstream.port}`, FRONTEND_WS_HANDSHAKE_S: '1' });
    const before = upstream.seen.length;
    const sentAt = Date.now();
    const ws = handshake(app.port, '/api/audio/sessions/silent-1/live', browser(app.port));
    const answer = await ws.answer;
    const waited = Date.now() - sentAt;
    expect(answer.status).toBe(504);
    expect(waited).toBeGreaterThanOrEqual(900);
    expect(waited).toBeLessThan(2_500);
    await waitFor(() => upstream.seen[before]?.closed, 'the upstream request to be dropped');
    expect(events(app.output()).find((e) => e.event === 'ws_close')).toMatchObject({
      reason: 'handshake_timeout',
      upstream_status: null,
    });
    app.child.kill('SIGKILL');
  }, 10_000);

  it('answers 502 when the orchestrator cannot be reached', async () => {
    const closedPort = await new Promise<number>((resolve) => {
      const probe = net.createServer();
      probe.listen(0, '127.0.0.1', () => {
        const { port } = probe.address() as net.AddressInfo;
        probe.close(() => resolve(port));
      });
    });
    const app = await start({ ORCHESTRATOR_URL: `http://127.0.0.1:${closedPort}` });
    const ws = handshake(app.port, '/api/audio/sessions/echo-11/live', browser(app.port));
    expect((await ws.answer).status).toBe(502);
    await waitFor(
      () => events(app.output()).find((e) => e.event === 'ws_close' && e.reason === 'upstream_unreachable'),
      'ws_close upstream_unreachable',
    );
    app.child.kill('SIGKILL');
  });

  it('refuses with 502 when ORCHESTRATOR_URL is not http://, and says so when it starts', async () => {
    const app = await start({ ORCHESTRATOR_URL: `https://127.0.0.1:${upstream.port}` });
    expect(events(app.output()).find((e) => e.event === 'ws_relay_ready')).toMatchObject({ upstream: 'invalid' });
    const ws = handshake(app.port, '/api/audio/sessions/echo-12/live', browser(app.port));
    expect((await ws.answer).status).toBe(502);
    expect(events(app.output()).find((e) => e.event === 'ws_refused')).toMatchObject({ reason: 'upstream_config' });
    app.child.kill('SIGKILL');
  });

  it('refuses with 404 in mock mode, where there is no orchestrator', async () => {
    const app = await start({ ORCHESTRATOR_URL: `http://127.0.0.1:${upstream.port}`, MOCK_MODE: 'true' });
    const before = upstream.seen.length;
    const ws = handshake(app.port, '/api/audio/sessions/echo-13/live', browser(app.port));
    expect((await ws.answer).status).toBe(404);
    expect(upstream.seen.length).toBe(before);
    app.child.kill('SIGKILL');
  });

  it('destroys a relay with no byte either way for FRONTEND_WS_IDLE_S, and not one that keeps talking', async () => {
    const app = await start({ ORCHESTRATOR_URL: `http://127.0.0.1:${upstream.port}`, FRONTEND_WS_IDLE_S: '1' });
    const quiet = handshake(app.port, '/api/audio/sessions/echo-14/live', browser(app.port));
    const busy = handshake(app.port, '/api/audio/sessions/echo-15/live', browser(app.port));
    expect((await quiet.answer).status).toBe(101);
    expect((await busy.answer).status).toBe(101);
    const openedAt = Date.now();
    for (let i = 0; i < 9; i += 1) {
      busy.socket.write('.');
      await sleep(250);
    }
    const quietClosedAt = await quiet.closed;
    expect(quietClosedAt - openedAt).toBeGreaterThanOrEqual(900);
    expect(quietClosedAt - openedAt).toBeLessThan(2_000);
    // 2.25 s in, and the one that kept talking is still there.
    expect(busy.isClosed()).toBe(false);
    await waitFor(() => busy.body().toString() === '.........', 'the busy relay’s echoes');
    const idle = events(app.output()).filter((e) => e.event === 'ws_close' && e.reason === 'idle');
    expect(idle).toHaveLength(1);
    // Then it falls quiet too, and goes the same way.
    await busy.closed;
    app.child.kill('SIGKILL');
  }, 15_000);
});

describe('upgrades nobody answers', () => {
  let app: Started;
  beforeAll(async () => {
    app = await start({ ORCHESTRATOR_URL: 'http://127.0.0.1:9', FRONTEND_WS_STRAY_S: '1' });
  });
  afterAll(() => {
    app.child.kill('SIGKILL');
  });

  it('are destroyed after FRONTEND_WS_STRAY_S', async () => {
    const sentAt = Date.now();
    const ws = handshake(app.port, '/no-route?x=1', browser(app.port));
    const closedAt = await ws.closed;
    expect((await ws.answer).status).toBeNull();
    expect(closedAt - sentAt).toBeGreaterThanOrEqual(900);
    expect(closedAt - sentAt).toBeLessThan(2_500);
    const line = events(app.output(), 'server-preload').find((e) => e.event === 'upgrade_unanswered');
    expect(line).toMatchObject({ path: '/no-route' });
    expect(app.output()).toContain('APP_UPGRADE /no-route\n');
  }, 10_000);

  it('are still ended at once when Next ends them', async () => {
    const sentAt = Date.now();
    const ws = handshake(app.port, '/api/admin/x', browser(app.port));
    const closedAt = await ws.closed;
    expect(closedAt - sentAt).toBeLessThan(500);
  });

  it('leave alone an upgrade something answered', async () => {
    const ws = handshake(app.port, '/app-ws', browser(app.port));
    expect((await ws.answer).status).toBe(101);
    await sleep(1_600);
    expect(ws.isClosed()).toBe(false);
    ws.socket.write('still-here');
    await waitFor(() => ws.body().toString().includes('still-here'), 'the app’s own echo');
    ws.socket.destroy();
  }, 10_000);
});

describe('on SIGTERM', () => {
  let upstream: Upstream;
  beforeAll(async () => {
    upstream = await startUpstream();
  });
  afterAll(async () => {
    await upstream.close();
  });

  it('destroys relays and unanswered upgrades at +2 s, refuses nothing it already carried before then, and exits right after', async () => {
    const app = await start({ ORCHESTRATOR_URL: `http://127.0.0.1:${upstream.port}` });
    const relayed = handshake(app.port, '/api/audio/sessions/echo-20/live', browser(app.port));
    expect((await relayed.answer).status).toBe(101);
    const stray = handshake(app.port, '/no-route', browser(app.port));
    await waitFor(() => app.output().includes('APP_UPGRADE /no-route\n'), 'the stray to arrive');

    const termAt = Date.now();
    app.child.kill('SIGTERM');
    // Still carrying bytes during the grace period.
    await sleep(500);
    relayed.socket.write('mid-drain');
    await waitFor(() => relayed.body().toString().includes('mid-drain'), 'an echo during the drain');

    const [relayClosed, strayClosed, exit] = await Promise.all([relayed.closed, stray.closed, app.exited]);
    for (const at of [relayClosed, strayClosed]) {
      expect(at - termAt).toBeGreaterThanOrEqual(1_900);
      expect(at - termAt).toBeLessThanOrEqual(2_700);
    }
    expect(exit.code).toBe(143);
    expect(exit.at - Math.max(relayClosed, strayClosed)).toBeLessThan(1_000);

    const drain = events(app.output(), 'server-preload');
    expect(drain.find((e) => e.event === 'drain_start')).toMatchObject({ upgrades: 2, ws_abort_ms: 2_000 });
    expect(drain.find((e) => e.event === 'drain_abort')).toMatchObject({ kind: 'ws', count: 2 });
    expect(events(app.output()).find((e) => e.event === 'ws_close')).toMatchObject({ reason: 'drain' });
  }, 15_000);

  it('follows FRONTEND_DRAIN_WS_S', async () => {
    const app = await start({ ORCHESTRATOR_URL: `http://127.0.0.1:${upstream.port}`, FRONTEND_DRAIN_WS_S: '0.5' });
    const relayed = handshake(app.port, '/api/audio/sessions/echo-21/live', browser(app.port));
    expect((await relayed.answer).status).toBe(101);
    const termAt = Date.now();
    app.child.kill('SIGTERM');
    const closedAt = await relayed.closed;
    expect(closedAt - termAt).toBeGreaterThanOrEqual(400);
    expect(closedAt - termAt).toBeLessThan(1_200);
    expect((await app.exited).code).toBe(143);
  }, 10_000);
});

describe('install()', () => {
  let upstream: Upstream;
  beforeAll(async () => {
    upstream = await startUpstream();
  });
  afterAll(async () => {
    await upstream.close();
  });

  async function listening(env: Env) {
    const logged: Array<[string, Fields]> = [];
    const server = http.createServer((_req, res) => res.end());
    const handle = relayModule.install(server, { ORCHESTRATOR_URL: `http://127.0.0.1:${upstream.port}`, ...env }, {
      log: (event, fields) => logged.push([event, fields]),
    });
    await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
    const port = (server.address() as net.AddressInfo).port;
    const close = () =>
      new Promise<void>((resolve) => {
        server.closeAllConnections();
        server.close(() => resolve());
      });
    return { server, handle, port, logged, close };
  }

  it('keeps owned paths from every upgrade listener added after it, however it was added, and still lets one be removed', async () => {
    const { server, port, close } = await listening({});
    const heard: string[] = [];
    const listener = (name: string) => (req: http.IncomingMessage, socket: net.Socket) => {
      heard.push(`${name} ${req.url}`);
      socket.destroy();
    };
    const viaOn = listener('on');
    server.on('upgrade', viaOn);
    server.addListener('upgrade', listener('addListener'));
    server.prependListener('upgrade', listener('prependListener'));
    server.once('upgrade', listener('once'));

    // Refused for want of a cookie: answered by the relay, and by nobody else.
    const owned = handshake(port, '/api/audio/sessions/echo-30/live', browser(port, { Cookie: undefined }));
    expect((await owned.answer).status).toBe(401);
    await owned.closed;
    expect(heard).toEqual([]);

    const other = handshake(port, '/somewhere-else', browser(port));
    await other.closed;
    expect(heard.sort()).toEqual(
      ['addListener /somewhere-else', 'on /somewhere-else', 'once /somewhere-else', 'prependListener /somewhere-else'].sort(),
    );

    const count = server.listenerCount('upgrade');
    server.removeListener('upgrade', viaOn);
    expect(server.listenerCount('upgrade')).toBe(count - 1);
    await close();
  });

  it('is installed once, however often it is called', async () => {
    const { server, handle, close } = await listening({});
    const count = server.listenerCount('upgrade');
    expect(relayModule.install(server)).toBe(handle);
    expect(server.listenerCount('upgrade')).toBe(count);
    await close();
  });

  it('refuses new relays once draining, and destroyAll ends the open ones and counts them', async () => {
    const { handle, port, logged, close } = await listening({});
    const open = handshake(port, '/api/audio/sessions/echo-31/live', browser(port));
    expect((await open.answer).status).toBe(101);
    expect(handle.size).toBe(1);

    handle.beginDrain();
    const late = handshake(port, '/api/audio/sessions/echo-32/live', browser(port));
    expect((await late.answer).status).toBe(503);
    expect(logged.find(([event]) => event === 'ws_refused')?.[1]).toMatchObject({ reason: 'draining' });

    expect(handle.destroyAll('drain')).toBe(1);
    await open.closed;
    expect(handle.size).toBe(0);
    expect(logged.find(([event]) => event === 'ws_close')?.[1]).toMatchObject({ reason: 'drain' });
    await close();
  });
});

describe('the preload without the relay file', () => {
  it('logs that the relay is unavailable, and serves everything else as before', async () => {
    const alone = mkdtempSync(join(tmpdir(), 'server-preload-alone-'));
    try {
      const preload = join(alone, 'server-preload.cjs');
      copyFileSync(PRELOAD, preload);
      const app = await start({ FRONTEND_WS_STRAY_S: '1' }, preload);
      expect(events(app.output(), 'server-preload').find((e) => e.event === 'ws_relay_unavailable')).toMatchObject({
        error: 'MODULE_NOT_FOUND',
      });
      expect(await getText(app.port, '/hello')).toEqual({ status: 200, text: JSON.stringify({ ok: true }) });
      // With no relay, the path is Next's like any other.
      const ws = handshake(app.port, '/api/audio/sessions/echo-40/live', browser(app.port));
      await ws.closed;
      expect(app.output()).toContain('APP_UPGRADE /api/audio/sessions/echo-40/live\n');
      app.child.kill('SIGKILL');
    } finally {
      rmSync(alone, { recursive: true, force: true });
    }
  });
});
