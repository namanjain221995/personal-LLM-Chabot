'use strict';
/** Shared helpers for the gateway tests: ports, the stub, the gateway, a timing client. */

const { spawn } = require('node:child_process');
const fs = require('node:fs');
const http = require('node:http');
const net = require('node:net');
const os = require('node:os');
const path = require('node:path');
const { performance } = require('node:perf_hooks');

const GATEWAY_DIR = path.resolve(__dirname, '..');
const STUB = path.join(__dirname, 'stub-orchestrator.cjs');

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function freePort() {
  return new Promise((resolve, reject) => {
    const srv = net.createServer();
    srv.unref();
    srv.on('error', reject);
    srv.listen(0, '127.0.0.1', () => {
      const { port } = srv.address();
      srv.close(() => resolve(port));
    });
  });
}

// Every temporary directory of one test process lives under one root that is
// removed when the process exits, so a run leaves nothing behind in /tmp.
let tmpRoot = null;
function tmpdir(prefix) {
  if (tmpRoot === null) {
    tmpRoot = fs.mkdtempSync(path.join(os.tmpdir(), `gw-test-${process.pid}-`));
    process.once('exit', () => fs.rmSync(tmpRoot, { recursive: true, force: true }));
  }
  return fs.mkdtempSync(path.join(tmpRoot, prefix));
}

function waitForLine(proc, pattern, timeoutMs = 10_000) {
  return new Promise((resolve, reject) => {
    let buf = '';
    const timer = setTimeout(() => reject(new Error(`timed out waiting for ${pattern}; got ${buf}`)), timeoutMs);
    const onData = (d) => {
      buf += d.toString();
      const m = pattern.exec(buf);
      if (m) {
        clearTimeout(timer);
        proc.stdout.off('data', onData);
        resolve(m);
      }
    };
    proc.stdout.on('data', onData);
    proc.once('exit', (code) => {
      clearTimeout(timer);
      reject(new Error(`process exited ${code} before ${pattern}; got ${buf}`));
    });
  });
}

/** A stub orchestrator child process; restartable on the same port and state. */
async function startStub({ port, stateDir, env = {} } = {}) {
  const p = port ?? (await freePort());
  const dir = stateDir ?? tmpdir('gw-stub-');
  const proc = spawn(process.execPath, [STUB], {
    env: { ...process.env, STUB_PORT: String(p), STUB_STATE_DIR: dir, ...env },
    stdio: ['ignore', 'pipe', 'inherit'],
  });
  await waitForLine(proc, /READY (\d+)/);
  const exited = new Promise((resolve) => proc.once('exit', resolve));
  return {
    port: p,
    stateDir: dir,
    proc,
    async kill(signal = 'SIGKILL') {
      if (proc.exitCode === null && proc.signalCode === null) proc.kill(signal);
      await exited;
    },
    events() {
      const file = path.join(dir, 'events.jsonl');
      if (!fs.existsSync(file)) return [];
      return fs
        .readFileSync(file, 'utf8')
        .split('\n')
        .filter(Boolean)
        .map((l) => JSON.parse(l));
    },
  };
}

/** The gateway in this process, with its log lines captured. */
async function startGateway({ orchestratorPort, env = {} } = {}) {
  // Loaded lazily so a test can set process.env before requiring.
  const { createGateway } = require(path.join(GATEWAY_DIR, 'server.cjs'));
  const logs = [];
  const spool = tmpdir('gw-spool-');
  const gateway = createGateway({
    env: {
      V1_GATEWAY_PORT: '0',
      V1_GATEWAY_HOST: '127.0.0.1',
      ORCHESTRATOR_URL: `http://127.0.0.1:${orchestratorPort}`,
      V1_GATEWAY_SPOOL_DIR: spool,
      ...env,
    },
    log: (event, fields) => logs.push({ t: performance.now(), event, ...fields }),
  });
  const port = await gateway.listen();
  return {
    port,
    url: `http://127.0.0.1:${port}`,
    logs,
    spool,
    gateway,
    close() {
      return new Promise((resolve) => {
        gateway.server.closeAllConnections();
        gateway.server.close(() => resolve());
        gateway.gw.agent.destroy();
        gateway.gw.freshAgent.destroy();
      });
    },
  };
}

/**
 * One HTTP request with per-chunk arrival times.
 * Resolves { status, headers, text, chunks, firstByteMs, maxGapMs, complete, error, elapsedMs }.
 */
function request(port, { method = 'GET', path: p = '/', headers = {}, body, host = '127.0.0.1', onChunk } = {}) {
  return new Promise((resolve) => {
    const started = performance.now();
    const chunks = [];
    let status = null;
    let resHeaders = null;
    let settled = false;
    let headersAt = null;
    let res0 = null;
    const done = (error) => {
      if (settled) return;
      settled = true;
      const times = chunks.map((c) => c.t);
      let maxGapMs = 0;
      let prev = headersAt ?? started;
      for (const t of times) {
        maxGapMs = Math.max(maxGapMs, t - prev);
        prev = t;
      }
      resolve({
        status,
        headers: resHeaders,
        text: Buffer.concat(chunks.map((c) => c.buf)).toString('utf8'),
        buffer: Buffer.concat(chunks.map((c) => c.buf)),
        chunks,
        firstByteMs: chunks.length ? chunks[0].t - started : null,
        headersMs: headersAt === null ? null : headersAt - started,
        maxGapMs,
        complete: res0 ? res0.complete : false,
        error: error || null,
        elapsedMs: performance.now() - started,
      });
    };
    const req = http.request({ host, port, method, path: p, headers, agent: false }, (res) => {
      res0 = res;
      status = res.statusCode;
      resHeaders = res.headers;
      headersAt = performance.now();
      res.on('data', (buf) => {
        const chunk = { t: performance.now(), buf };
        chunks.push(chunk);
        if (onChunk) onChunk(chunk, req);
      });
      res.on('end', () => done(null));
      res.on('close', () => done(res.complete ? null : new Error('incomplete')));
      res.on('error', () => undefined);
    });
    req.on('error', (err) => done(err));
    if (typeof body === 'string' || Buffer.isBuffer(body)) req.end(body);
    else if (typeof body === 'function') body(req);
    else req.end();
  });
}

function sseDataPayloads(text) {
  const out = [];
  for (const block of text.split('\n\n')) {
    const data = block
      .split('\n')
      .filter((l) => l.startsWith('data:'))
      .map((l) => l.slice(5).trimStart())
      .join('\n');
    if (data) out.push(data);
  }
  return out;
}

function chatText(text) {
  let s = '';
  for (const data of sseDataPayloads(text)) {
    if (data === '[DONE]') continue;
    const obj = JSON.parse(data);
    s += obj.choices?.[0]?.delta?.content ?? '';
  }
  return s;
}

function expectedText(tokens) {
  let s = '';
  for (let k = 1; k <= tokens; k += 1) s += `w${k} `;
  return s;
}

module.exports = {
  GATEWAY_DIR,
  sleep,
  freePort,
  tmpdir,
  waitForLine,
  startStub,
  startGateway,
  request,
  sseDataPayloads,
  chatText,
  expectedText,
};
