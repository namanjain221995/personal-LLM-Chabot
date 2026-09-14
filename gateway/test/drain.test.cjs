'use strict';
/**
 * SIGTERM on the real entry point, `node server.cjs`, as a child process
 * (2026-09-13, timers table "v1-gateway container stop": close the listener,
 * destroy every relay at +2 s so clients see an incomplete read and resume).
 */

const test = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');
const net = require('node:net');
const path = require('node:path');
const { spawn } = require('node:child_process');
const { performance } = require('node:perf_hooks');

const h = require('../testkit/harness.cjs');

async function spawnGateway(t, orchestratorPort, env = {}) {
  const port = await h.freePort();
  const proc = spawn(process.execPath, [path.join(h.GATEWAY_DIR, 'server.cjs')], {
    env: {
      ...process.env,
      V1_GATEWAY_PORT: String(port),
      V1_GATEWAY_HOST: '127.0.0.1',
      ORCHESTRATOR_URL: `http://127.0.0.1:${orchestratorPort}`,
      V1_GATEWAY_SPOOL_DIR: h.tmpdir('gw-drain-spool-'),
      ...env,
    },
    stdio: ['ignore', 'pipe', 'inherit'],
  });
  const lines = [];
  proc.stdout.on('data', (d) => {
    for (const l of d.toString().split('\n').filter(Boolean)) {
      try {
        lines.push({ t: performance.now(), ...JSON.parse(l) });
      } catch {
        /* partial line */
      }
    }
  });
  const exit = new Promise((resolve) => proc.once('exit', (code, signal) => resolve({ code, signal, t: performance.now() })));
  t.after(() => {
    if (proc.exitCode === null && proc.signalCode === null) proc.kill('SIGKILL');
  });
  await h.waitForLine(proc, /"event":"listening"/);
  return { port, proc, exit, lines };
}

function connects(port) {
  return new Promise((resolve) => {
    const s = net.connect(port, '127.0.0.1');
    s.once('connect', () => {
      s.destroy();
      resolve(true);
    });
    s.once('error', () => resolve(false));
  });
}

test.describe('draining on SIGTERM', { concurrency: true, timeout: 30_000 }, () => {
  let stub;
  test.before(async () => {
    stub = await h.startStub();
  });
  test.after(async () => {
    await stub.kill();
  });

  test('a running relay is cut by 2.5 s, new connections are refused, and the process exits 0', async (t) => {
    const g = await spawnGateway(t, stub.port);
    let sentAt = null;
    const pending = h.request(g.port, {
      method: 'POST',
      path: '/v1/chat/completions',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ model: 'stub/tokens=200/interval=100', stream: true }),
      onChunk: (c) => {
        if (sentAt === null && c.buf.toString().includes('w3 ')) {
          sentAt = performance.now();
          g.proc.kill('SIGTERM');
          setTimeout(async () => {
            refusedSoon = !(await connects(g.port));
          }, 100);
        }
      },
    });
    let refusedSoon = null;
    const r = await pending;
    const cutAfter = r.chunks.at(-1).t - sentAt;
    assert.ok(r.error, 'the client saw an incomplete read');
    const ended = performance.now() - sentAt;
    assert.ok(ended >= 1_900 && ended <= 2_500, `relay cut ${Math.round(ended)} ms after SIGTERM`);
    assert.ok(cutAfter <= 2_500, `last byte ${Math.round(cutAfter)} ms after SIGTERM`);
    const exit = await g.exit;
    assert.equal(exit.code, 0);
    assert.ok(exit.t - sentAt <= 3_000, `exited ${Math.round(exit.t - sentAt)} ms after SIGTERM`);
    assert.equal(refusedSoon, true, 'the listener was closed at once');
    assert.ok(g.lines.some((l) => l.event === 'drain_aborted' && l.aborted === 1));
  });

  test('a relay that ends inside the 2 s finishes whole, and the process exits right after it', async (t) => {
    const g = await spawnGateway(t, stub.port);
    let sentAt = null;
    const r = await h.request(g.port, {
      method: 'POST',
      path: '/v1/chat/completions',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ model: 'stub/tokens=12/interval=100', stream: true }),
      onChunk: (c) => {
        if (sentAt === null) {
          sentAt = performance.now();
          g.proc.kill('SIGTERM');
        }
      },
    });
    assert.equal(r.error, null);
    assert.equal(h.chatText(r.text), h.expectedText(12));
    const exit = await g.exit;
    assert.equal(exit.code, 0);
    assert.ok(exit.t - sentAt < 2_000, `exited ${Math.round(exit.t - sentAt)} ms after SIGTERM`);
    assert.ok(g.lines.some((l) => l.event === 'drain_exit' && l.reason === 'idle'));
  });

  test('an idle gateway holding a keep-alive connection exits at once', async (t) => {
    const g = await spawnGateway(t, stub.port);
    const agent = new http.Agent({ keepAlive: true });
    await new Promise((resolve) => {
      http.get({ host: '127.0.0.1', port: g.port, path: '/v1/models', agent }, (res) => {
        res.resume();
        res.on('end', resolve);
      });
    });
    const sent = performance.now();
    g.proc.kill('SIGTERM');
    const exit = await g.exit;
    agent.destroy();
    assert.equal(exit.code, 0);
    assert.ok(exit.t - sent < 500, `exited ${Math.round(exit.t - sent)} ms after SIGTERM`);
  });
});
