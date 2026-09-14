'use strict';
/**
 * Real OpenAI SDKs, at their DEFAULT settings, through the gateway at ITS
 * default settings (2026-09-13).
 *
 * The SDKs are not dependencies of gateway/ (it has none). Point the tests
 * at installs outside the repository:
 *   GATEWAY_SDK_PYTHON=/path/to/python          (with openai installed)
 *   GATEWAY_SDK_NODE_DIRS=/dir/a,/dir/b         (each with node_modules/openai)
 * The 400 s run is behind GATEWAY_LONG_TESTS=1.
 */

const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const { spawn } = require('node:child_process');

const h = require('../testkit/harness.cjs');
const { startEdge } = require('../testkit/edge-emulator.cjs');

const PYTHON = process.env.GATEWAY_SDK_PYTHON || '';
const NODE_DIRS = (process.env.GATEWAY_SDK_NODE_DIRS || '').split(',').filter(Boolean);
const LONG = process.env.GATEWAY_LONG_TESTS === '1';
const noSdk = !PYTHON && NODE_DIRS.length === 0 ? 'set GATEWAY_SDK_PYTHON and/or GATEWAY_SDK_NODE_DIRS' : false;

function runClient(kind, baseUrl, mode, model) {
  const [cmd, args] =
    kind === 'python'
      ? [PYTHON, [path.join(__dirname, '..', 'testkit', 'sdk_client.py'), baseUrl, mode, model]]
      : [process.execPath, [path.join(__dirname, '..', 'testkit', 'sdk-client.cjs'), kind, baseUrl, mode, model]];
  return new Promise((resolve) => {
    const proc = spawn(cmd, args, { stdio: ['ignore', 'pipe', 'pipe'] });
    let out = '';
    let err = '';
    proc.stdout.on('data', (d) => (out += d));
    proc.stderr.on('data', (d) => (err += d));
    proc.on('exit', (code) => {
      try {
        resolve({ ...JSON.parse(out.trim().split('\n').pop()), exit: code });
      } catch {
        resolve({ ok: false, error: `no result (exit ${code}): ${err.slice(-500)}`, exit: code });
      }
    });
  });
}

function clients() {
  const list = [];
  if (PYTHON) list.push('python');
  for (const dir of NODE_DIRS) list.push(dir);
  return list;
}

test('default SDK clients finish a chat stream, a Responses stream and a sync call across an orchestrator SIGKILL', { skip: noSdk, timeout: 180_000 }, async (t) => {
  const stub = await h.startStub();
  const gw = await h.startGateway({ orchestratorPort: stub.port });
  let replacement = null;
  t.after(async () => {
    await gw.close();
    await stub.kill();
    if (replacement) await replacement.kill();
  });
  const base = `${gw.url}/v1`;
  const calls = [];
  for (const kind of clients()) {
    calls.push({ kind, mode: 'chat-stream', model: 'stub/tokens=300/interval=100', tokens: 300 });
    calls.push({ kind, mode: 'responses-stream', model: 'stub/tokens=300/interval=100', tokens: 300 });
    calls.push({ kind, mode: 'chat-sync', model: 'stub/tokens=40/interval=1000', tokens: 40 });
  }
  const running = calls.map((c) => runClient(c.kind, base, c.mode, c.model).then((r) => ({ ...c, r })));
  await h.sleep(20_000);
  await stub.kill('SIGKILL');
  await h.sleep(5_000);
  replacement = await h.startStub({ port: stub.port, stateDir: stub.stateDir });
  const results = await Promise.all(running);
  for (const { kind, mode, tokens, r } of results) {
    const label = `${r.sdk ?? kind} ${mode}`;
    t.diagnostic(`${label}: ok=${r.ok} elapsed=${r.elapsed_s}s${r.error ? ` error=${r.error}` : ''}`);
    assert.equal(r.ok, true, `${label}: ${r.error}`);
    assert.equal(r.text, h.expectedText(tokens), label);
    if (mode === 'responses-stream') assert.equal(r.seqs_contiguous, true, label);
  }
  const events = replacement.events();
  const launches = events.filter((e) => e.kind === 'launch');
  assert.equal(launches.length, calls.length, 'each call generated exactly once');
  // Measured 2026-09-13: openai-python 3.13.0, openai-node 6.49.0 and 7.15.0
  // all send x-stainless-retry-count: 0 on a first try, so "0" everywhere
  // means no SDK retried — the gateway absorbed the restart.
  assert.ok(launches.every((e) => e.retry_count === '0'), 'no SDK retried');
  assert.equal(events.filter((e) => e.kind === 'attach').length, calls.length, 'every call re-attached once');
  assert.equal(gw.logs.filter((l) => l.event === 'reattach_ok').length, calls.length);
});

test('a 400 s silent orchestrator reaches default SDK clients through a 100 s edge (real time)', { skip: noSdk || (!LONG && 'set GATEWAY_LONG_TESTS=1'), timeout: 900_000 }, async (t) => {
  const stub = await h.startStub();
  const gw = await h.startGateway({ orchestratorPort: stub.port });
  const edge = await startEdge({ targetPort: gw.port });
  const control = await startEdge({ targetPort: stub.port });
  t.after(async () => {
    edge.close();
    control.close();
    await gw.close();
    await stub.kill();
  });
  // Any answer naming X-TechSara-Run tells the gateway this orchestrator
  // attaches, so its 300 s silence watchdog may re-POST (production always
  // has such an answer before a 400 s call).
  const warm = await h.request(gw.port, { path: '/v1/models' });
  assert.equal(warm.status, 200);

  const base = `http://127.0.0.1:${edge.port}/v1`;
  const model = 'stub/tokens=4/interval=10/silent=400000';
  const calls = [];
  for (const kind of clients()) for (const mode of ['chat-sync', 'chat-stream', 'responses-stream']) calls.push({ kind, mode });
  const running = calls.map((c) => runClient(c.kind, base, c.mode, model).then((r) => ({ ...c, r })));

  // CONTROL: the same silence straight to the orchestrator through the same
  // edge, no gateway. It must be cut, or this test proves nothing.
  const direct = await h.request(control.port, {
    method: 'POST',
    path: '/v1/chat/completions',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ model, messages: [] }),
  });
  t.diagnostic(`control without the gateway: status ${direct.status} after ${Math.round(direct.elapsedMs)} ms`);
  assert.equal(direct.status, 524);
  assert.ok(direct.elapsedMs >= 99_000 && direct.elapsedMs < 102_000);

  const results = await Promise.all(running);
  for (const { kind, mode, r } of results) {
    const label = `${r.sdk ?? kind} ${mode}`;
    t.diagnostic(`${label}: ok=${r.ok} elapsed=${r.elapsed_s}s${r.error ? ` error=${r.error}` : ''}`);
    assert.equal(r.ok, true, `${label}: ${r.error}`);
    assert.equal(r.text, h.expectedText(4), label);
    assert.ok(r.elapsed_s >= 400, `${label} took ${r.elapsed_s}s`);
  }
  const launches = stub.events().filter((e) => e.kind === 'launch' && e.attempt);
  assert.equal(launches.length, calls.length, 'each call generated exactly once');
  assert.ok(launches.every((e) => e.retry_count === '0'), 'no SDK retried');
  assert.equal(gw.logs.filter((l) => l.event === 'self_commit').length, calls.length, 'the gateway committed every call at 15 s');
  assert.equal(gw.logs.filter((l) => l.event === 'reattach_ok').length, calls.length, 'the 300 s watchdog re-attached every call');
});
