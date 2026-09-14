'use strict';
/**
 * Physical limits and client-side timers (2026-09-13, timers table
 * "v1-gateway client side", design F "Physical safety").
 */

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const net = require('node:net');
const path = require('node:path');
const { spawn } = require('node:child_process');
const { performance } = require('node:perf_hooks');

const h = require('../testkit/harness.cjs');
const B = require('../lib/bodies.cjs');
const { FdGuard, parseNofile } = require('../lib/guards.cjs');

function spawnGatewayUnder(t, shellPrefix, orchestratorPort, env = {}) {
  return h.freePort().then(async (port) => {
    const proc = spawn('bash', ['-c', `${shellPrefix} exec "${process.execPath}" "${path.join(h.GATEWAY_DIR, 'server.cjs')}"`], {
      env: {
        ...process.env,
        V1_GATEWAY_PORT: String(port),
        V1_GATEWAY_HOST: '127.0.0.1',
        ORCHESTRATOR_URL: `http://127.0.0.1:${orchestratorPort}`,
        V1_GATEWAY_SPOOL_DIR: h.tmpdir('gw-limits-spool-'),
        ...env,
      },
      stdio: ['ignore', 'pipe', 'inherit'],
    });
    t.after(() => {
      if (proc.exitCode === null && proc.signalCode === null) proc.kill('SIGKILL');
    });
    const m = await h.waitForLine(proc, /(\{[^\n]*"event":"listening"[^\n]*\})/);
    return { port, proc, listening: JSON.parse(m[1]) };
  });
}

function rssBytes(pid) {
  const status = fs.readFileSync(`/proc/${pid}/status`, 'utf8');
  return Number(/VmRSS:\s+(\d+) kB/.exec(status)[1]) * 1024;
}

test.describe('file descriptors', { concurrency: true }, () => {
  test('the soft RLIMIT_NOFILE equals the hard limit even when the gateway is started under a soft 1024', { skip: !fs.existsSync('/proc/self/limits') }, async (t) => {
    const g = await spawnGatewayUnder(t, 'ulimit -Sn 1024;', 1);
    const limits = parseNofile(fs.readFileSync(`/proc/${g.proc.pid}/limits`, 'utf8'));
    t.diagnostic(`child nofile soft=${limits.soft} hard=${limits.hard}`);
    assert.equal(limits.soft, limits.hard);
    assert.ok(limits.soft > 1024);
    assert.equal(g.listening.nofile_soft, limits.soft);
  });

  test('parseNofile reads the Max open files row', () => {
    const text = 'Limit                     Soft Limit           Hard Limit           Units     \nMax open files            1024                 524288               files     \n';
    assert.deepEqual(parseNofile(text), { soft: 1024, hard: 524288 });
    assert.equal(parseNofile('nothing'), null);
  });

  test('the fd guard refuses at 70% of the soft limit and samples at most once a second', () => {
    let t = 0;
    let open = 699;
    let reads = 0;
    const guard = new FdGuard({ now: () => t, limits: () => ({ soft: 1000, hard: 1000 }), count: () => (reads += 1, open) });
    assert.equal(guard.overPressure(), false);
    open = 700;
    t = 500;
    assert.equal(guard.overPressure(), false, 'still the cached sample');
    t = 1000;
    assert.equal(guard.overPressure(), true);
    assert.equal(reads, 2);
  });

  test('under fd pressure /v1 is refused 503 Retry-After 30 before the orchestrator is called, and /healthz still answers', async (t) => {
    const stub = await h.startStub();
    const gw = await h.startGateway({ orchestratorPort: stub.port, env: { V1_GATEWAY_FD_PRESSURE_RATIO: '0.000001' } });
    t.after(async () => {
      await gw.close();
      await stub.kill();
    });
    const r = await h.request(gw.port, { method: 'POST', path: '/v1/echo?probe=fd', headers: { 'content-type': 'application/json' }, body: '{}' });
    assert.equal(r.status, 503);
    assert.equal(r.headers['retry-after'], '30');
    assert.equal(JSON.parse(r.text).error.code, 'model_unavailable');
    assert.ok(!stub.events().some((e) => e.kind === 'request' && e.url.includes('probe=fd')));
    const health = await h.request(gw.port, { path: '/healthz' });
    assert.equal(health.status, 200);
  });
});

test.describe('client-side timers', { concurrency: true }, () => {
  let stub;
  test.before(async () => {
    stub = await h.startStub();
  });
  test.after(async () => {
    await stub.kill();
  });

  test('the server runs with requestTimeout 0, headersTimeout 100 s and keepAliveTimeout 95 s', async (t) => {
    const gw = await h.startGateway({ orchestratorPort: stub.port });
    t.after(() => gw.close());
    assert.equal(gw.gateway.server.requestTimeout, 0);
    assert.equal(gw.gateway.server.headersTimeout, 100_000);
    assert.equal(gw.gateway.server.keepAliveTimeout, 95_000);
    assert.equal(gw.gateway.server.timeout, 0);
  });

  for (const [label, method, p] of [
    ['a buffered JSON body', 'POST', '/v1/echo'],
    ['a streamed upload part', 'PUT', '/v1/uploads/upload_x/parts/3'],
  ]) {
    test(`${label} idle for 60 s is destroyed at 60 s (default timer, real time)`, { timeout: 90_000 }, async (t) => {
      const gw = await h.startGateway({ orchestratorPort: stub.port });
      t.after(() => gw.close());
      const started = performance.now();
      const closedAt = await new Promise((resolve) => {
        const sock = net.connect(gw.port, '127.0.0.1', () => {
          sock.write(`${method} ${p} HTTP/1.1\r\nHost: x\r\nContent-Type: application/octet-stream\r\nContent-Length: 100\r\n\r\n0123456789`);
        });
        sock.on('data', () => undefined);
        sock.on('close', () => resolve(performance.now()));
        sock.on('error', () => undefined);
      });
      const idle = closedAt - started;
      assert.ok(idle >= 59_900 && idle <= 61_500, `destroyed after ${Math.round(idle)} ms`);
      assert.ok(gw.logs.some((l) => l.event === 'body_idle'));
    });
  }

  test('a slow body of one byte every 300 ms for 4 s is relayed with 200', async (t) => {
    const gw = await h.startGateway({ orchestratorPort: stub.port });
    t.after(() => gw.close());
    const body = Buffer.from('{"slow":"0123456789ab"}');
    const r = await h.request(gw.port, {
      method: 'POST',
      path: '/v1/echo',
      headers: { 'content-type': 'application/json', 'content-length': String(body.length) },
      body: (req) => {
        let i = 0;
        const tick = setInterval(() => {
          req.write(body.subarray(i, i + 1));
          i += 1;
          if (i === body.length) {
            clearInterval(tick);
            req.end();
          }
        }, 300);
      },
    });
    assert.equal(r.status, 200);
    assert.ok(r.elapsedMs > 6_000);
    assert.equal(JSON.parse(r.text).bytes, body.length);
  });
});

test.describe('memory', () => {
  // WHY A WARM-UP PART. The first 64 MiB through ANY node:http relay grows
  // RSS by ~30-36 MiB of allocator high-water that is then reused: measured
  // 2026-09-13 on Node 20.20.2 with a four-line `req.pipe(http.request())`
  // proxy, deltas 35.7 / 1.1 / 3.6 MiB for three consecutive parts, and this
  // gateway 30.1 / 5.9 / 0.0 MiB. A relay that buffered the part would add
  // the whole 64 MiB on every part, so the steady-state part is the one that
  // tells streaming from buffering.
  test('a 64 MiB part streams through with the gateway RSS growing by less than 32 MiB once warm', { skip: !fs.existsSync('/proc/self/status') }, async (t) => {
    const stub = await h.startStub();
    t.after(() => stub.kill());
    const g = await spawnGatewayUnder(t, '', stub.port);
    const size = 64 * 1024 * 1024;
    const chunk = Buffer.alloc(1024 * 1024, 3);
    const sendPart = (n) =>
      h.request(g.port, {
        method: 'PUT',
        // A slow-reading orchestrator (~50 MiB/s), so a relay without
        // backpressure would have to hold the difference.
        path: `/v1/uploads/u/parts/${n}?stub=slow`,
        headers: { 'content-type': 'application/octet-stream', 'content-length': String(size) },
        body: (req) => {
          let sent = 0;
          const pump = () => {
            while (sent < size) {
              sent += chunk.length;
              const more = req.write(chunk);
              if (sent >= size) {
                req.end();
                return;
              }
              if (!more) {
                req.once('drain', pump);
                return;
              }
            }
          };
          pump();
        },
      });
    const warm = await sendPart(0);
    assert.equal(warm.status, 200);
    await h.sleep(300);
    const baseline = rssBytes(g.proc.pid);
    let peak = baseline;
    const sampler = setInterval(() => {
      peak = Math.max(peak, rssBytes(g.proc.pid));
    }, 10);
    const r = await sendPart(1);
    clearInterval(sampler);
    assert.equal(r.status, 200);
    assert.equal(JSON.parse(r.text).bytes, size);
    const delta = peak - baseline;
    t.diagnostic(`gateway RSS baseline ${(baseline / 1048576).toFixed(1)} MiB, peak ${(peak / 1048576).toFixed(1)} MiB, delta ${(delta / 1048576).toFixed(1)} MiB`);
    assert.ok(delta < 32 * 1024 * 1024, `RSS grew ${(delta / 1048576).toFixed(1)} MiB`);
  });

  // WHY TIMING AND NOT ONLY RSS: a relay that ignored backpressure would
  // still pass the warm RSS test above, because its first part inflates the
  // baseline and later parts reuse that memory (measured 2026-09-13 on a
  // mutated copy: baseline 107.8 MiB against 77 MiB, delta 5.4 MiB). With
  // backpressure, the client cannot finish sending much before the
  // orchestrator finishes reading: the gap is bounded by kernel socket
  // buffers. Without it the client finishes at once and waits the whole read.
  test('a slow orchestrator slows the client’s upload instead of the gateway holding the part', async (t) => {
    const stub = await h.startStub();
    const gw = await h.startGateway({ orchestratorPort: stub.port });
    t.after(async () => {
      await gw.close();
      await stub.kill();
    });
    const size = 64 * 1024 * 1024;
    const chunk = Buffer.alloc(1024 * 1024, 6);
    let finishedAt = null;
    const r = await h.request(gw.port, {
      method: 'PUT',
      // ~12.5 MiB/s: 64 KiB every 5 ms.
      path: '/v1/uploads/u/parts/9?stub=slow&kib=64',
      headers: { 'content-type': 'application/octet-stream', 'content-length': String(size) },
      body: (req) => {
        let sent = 0;
        req.on('finish', () => {
          finishedAt = performance.now();
        });
        const pump = () => {
          while (sent < size) {
            sent += chunk.length;
            const more = req.write(chunk);
            if (sent >= size) {
              req.end();
              return;
            }
            if (!more) {
              req.once('drain', pump);
              return;
            }
          }
        };
        pump();
      },
    });
    const answeredAt = r.chunks.length ? r.chunks[0].t : performance.now();
    assert.equal(r.status, 200);
    assert.equal(JSON.parse(r.text).bytes, size);
    const gap = answeredAt - finishedAt;
    t.diagnostic(`client finished sending ${Math.round(gap)} ms before the orchestrator finished reading; whole upload ${Math.round(r.elapsedMs)} ms`);
    assert.ok(r.elapsedMs > 3_500, 'the orchestrator really was slow');
    assert.ok(gap < 3_000, `the gateway let the client run ${Math.round(gap)} ms ahead`);
  });

  test('a declared 90 MiB part is refused with 413 before a byte is read', async (t) => {
    const stub = await h.startStub();
    const gw = await h.startGateway({ orchestratorPort: stub.port });
    t.after(async () => {
      await gw.close();
      await stub.kill();
    });
    const r = await h.request(gw.port, {
      method: 'PUT',
      path: '/v1/uploads/u/parts/2',
      headers: { 'content-type': 'application/octet-stream', 'content-length': String(90 * 1024 * 1024) },
      body: (req) => req.write(Buffer.alloc(1024)),
    });
    assert.equal(r.status, 413);
    assert.match(JSON.parse(r.text).error.message, /67108864 byte limit/);
  });
});

test.describe('the spool', () => {
  test('a spilled body is a 0600 file in a 0700 directory, and is removed when the relay ends', async (t) => {
    const spool = path.join(h.tmpdir('gw-spool-perm-'), 'spool');
    const seen = {};
    const server = http.createServer(async (req, res) => {
      const result = await B.readBufferedBody(req, { cap: 8 * 1024 * 1024, memoryBytes: 1024 * 1024, spoolDir: spool });
      seen.spilled = result.body.spilled;
      seen.fileMode = fs.statSync(result.body.file).mode & 0o777;
      seen.dirMode = fs.statSync(spool).mode & 0o777;
      seen.size = result.body.size;
      const file = result.body.file;
      result.body.dispose();
      await h.sleep(50);
      seen.removed = !fs.existsSync(file);
      res.end('ok');
    });
    await new Promise((r) => server.listen(0, '127.0.0.1', r));
    t.after(() => server.close());
    const body = Buffer.alloc(3 * 1024 * 1024, 1);
    const r = await h.request(server.address().port, { method: 'POST', path: '/', body, headers: { 'content-length': String(body.length) } });
    assert.equal(r.status, 200);
    assert.deepEqual(seen, { spilled: true, fileMode: 0o600, dirMode: 0o700, size: body.length, removed: true });
  });

  test('a declared body the spool quota cannot hold is refused 503 before a byte is read, and the reservation is freed when the relay ends', async (t) => {
    // Review finding 2026-09-13 (scratchpad p3_spool.cjs): six credential-less
    // trickling connections held 114 MiB of spool with no limit at all.
    // SCALED: memory part 1 MiB → 64 KiB, quota 2 GiB → 256 KiB.
    const stub = await h.startStub();
    const gw = await h.startGateway({
      orchestratorPort: stub.port,
      env: { V1_GATEWAY_MEMORY_BODY_BYTES: '65536', V1_GATEWAY_SPOOL_MAX_BYTES: '262144' },
    });
    t.after(async () => {
      await gw.close();
      await stub.kill();
    });
    const budget = gw.gateway.gw.spoolBudget;
    // A holds a 200 KiB reservation with half its body sent.
    let aText = '';
    const a = net.connect(gw.port, '127.0.0.1');
    a.on('data', (d) => {
      aText += d;
    });
    a.on('error', () => undefined);
    a.write('POST /v1/echo?probe=A HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\nContent-Length: 200000\r\n\r\n');
    a.write(Buffer.alloc(100_000, 0x20));
    t.after(() => a.destroy());
    await h.sleep(300);
    assert.equal(budget.reserved, 200_000);
    const b = await h.request(gw.port, {
      method: 'POST',
      path: '/v1/echo?probe=B',
      headers: { 'content-type': 'application/json', 'content-length': '200000' },
      body: (req) => req.write(Buffer.alloc(1000, 0x20)),
    });
    assert.equal(b.status, 503);
    assert.equal(b.headers['retry-after'], '30');
    assert.equal(b.headers.connection, 'close');
    assert.equal(JSON.parse(b.text).error.code, 'model_unavailable');
    assert.equal(gw.logs.find((l) => l.event === 'spool_refused').reason, 'quota');
    a.write(Buffer.alloc(100_000, 0x20));
    for (let i = 0; i < 50 && !aText.includes('"bytes":200000'); i += 1) await h.sleep(100);
    assert.match(aText, /^HTTP\/1\.1 200/);
    for (let i = 0; i < 50 && gw.gateway.gw.registry.size > 0; i += 1) await h.sleep(20);
    assert.equal(budget.reserved, 0, 'released when the relay ended');
    const c = await h.request(gw.port, { method: 'POST', path: '/v1/echo?probe=C', headers: { 'content-type': 'application/json' }, body: Buffer.alloc(200_000, 0x20) });
    assert.equal(c.status, 200);
    assert.ok(!stub.events().some((e) => e.kind === 'request' && e.url.includes('probe=B')), 'B never reached the orchestrator');
    // A chunked body is charged as it grows, and refused when it crosses the quota.
    const d = await h.request(gw.port, {
      method: 'POST',
      path: '/v1/echo?probe=D',
      headers: { 'content-type': 'application/json' },
      body: (req) => {
        let sent = 0;
        const tick = setInterval(() => {
          if (sent >= 400_000 || req.destroyed) {
            clearInterval(tick);
            if (!req.destroyed) req.end();
            return;
          }
          req.write(Buffer.alloc(20_000, 0x20));
          sent += 20_000;
        }, 5);
      },
    });
    assert.equal(d.status, 503);
    await h.sleep(200);
    assert.equal(budget.reserved, 0);
    assert.deepEqual(fs.readdirSync(gw.spool), [], 'the refused body left no file');
  });

  test('a body that would spill is refused 503 while the disk is under the free-space floor, and a body that fits in memory still passes', async (t) => {
    // SCALED: memory part 1 MiB → 64 KiB; the floor set above any disk.
    const stub = await h.startStub();
    const gw = await h.startGateway({
      orchestratorPort: stub.port,
      env: { V1_GATEWAY_MEMORY_BODY_BYTES: '65536', PUBLIC_API_MIN_FREE_DISK_BYTES: '9000000000000000' },
    });
    t.after(async () => {
      await gw.close();
      await stub.kill();
    });
    const big = await h.request(gw.port, { method: 'POST', path: '/v1/echo', headers: { 'content-type': 'application/json' }, body: Buffer.alloc(100_000, 0x20) });
    assert.equal(big.status, 503);
    assert.equal(gw.logs.find((l) => l.event === 'spool_refused').reason, 'disk');
    const small = await h.request(gw.port, { method: 'POST', path: '/v1/echo', headers: { 'content-type': 'application/json' }, body: Buffer.alloc(1000, 0x20) });
    assert.equal(small.status, 200);
    assert.equal(JSON.parse(small.text).bytes, 1000);
  });

  test('six spilled 12 MiB image requests held open by the orchestrator leave the gateway heap flat', async (t) => {
    // Review finding 2026-09-13 (scratchpad p2_heap.cjs): eight 15 MiB bodies
    // were each parsed and kept, +136.2 MiB of heap; after the fix the same
    // probe measured +16.0 MiB, all of it the probe's own request string.
    const v8 = require('node:v8');
    const vm = require('node:vm');
    v8.setFlagsFromString('--expose-gc');
    const gc = vm.runInNewContext('gc');
    let received = 0;
    const server = http.createServer((req, res) => {
      req.resume();
      req.on('end', () => {
        received += 1;
        res.writeHead(200, { 'content-type': 'text/event-stream', 'x-techsara-run': `resp_${received}` });
        res.write(': ping\n\n'); // and hold the stream open
      });
    });
    await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
    const gw = await h.startGateway({ orchestratorPort: server.address().port });
    const clients = [];
    t.after(async () => {
      for (const c of clients) c.destroy();
      await gw.close();
      server.closeAllConnections();
      server.close();
    });
    const N = 6;
    const body = Buffer.from(JSON.stringify({
      model: 'm',
      stream: true,
      input: [{ role: 'user', content: [{ type: 'input_image', image_url: `data:image/png;base64,${'A'.repeat(12 * 1024 * 1024)}` }] }],
    }));
    gc();
    await h.sleep(100);
    gc();
    const before = process.memoryUsage().heapUsed;
    for (let i = 0; i < N; i += 1) {
      const req = http.request({ host: '127.0.0.1', port: gw.port, method: 'POST', path: '/v1/responses', agent: false, headers: { 'content-type': 'application/json', 'content-length': String(body.length) } }, (res) => res.resume());
      req.on('error', () => undefined);
      req.end(body);
      clients.push(req);
    }
    for (let i = 0; i < 200 && received < N; i += 1) await h.sleep(50);
    assert.equal(received, N);
    await h.sleep(300);
    gc();
    await h.sleep(100);
    gc();
    const delta = process.memoryUsage().heapUsed - before;
    t.diagnostic(`heapUsed grew ${(delta / 1048576).toFixed(1)} MiB with ${gw.gateway.gw.registry.size} relays holding ${N} spilled ${(body.length / 1048576).toFixed(0)} MiB bodies`);
    assert.equal(gw.gateway.gw.registry.size, N);
    assert.ok(delta < 16 * 1024 * 1024, `heap grew ${(delta / 1048576).toFixed(1)} MiB`);
  });

  test('every aborted send of a spilled body closes its read stream, so retries leak no descriptors (timers scaled)', { skip: !fs.existsSync('/proc/self/fd') }, async (t) => {
    // Found while fixing the review (2026-09-13, scratchpad gwfix/pipeleak.cjs):
    // pipe() never destroys its source, +1 fd per aborted attempt.
    // SCALED: retry interval 2 → 0.05 s, budget 110 → 1.5 s, body cap raised.
    let connections = 0;
    const server = http.createServer((req) => {
      connections += 1;
      req.once('data', () => req.socket.destroy());
    });
    await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
    const gw = await h.startGateway({
      orchestratorPort: server.address().port,
      env: { V1_GATEWAY_PRECOMMIT_RETRY_INTERVAL_S: '0.05', PUBLIC_API_GATEWAY_PRECOMMIT_RETRY_S: '1.5', PUBLIC_API_MAX_BODY_BYTES: String(40 * 1024 * 1024) },
    });
    t.after(async () => {
      await gw.close();
      server.closeAllConnections();
      server.close();
    });
    const fds = () => fs.readdirSync('/proc/self/fd').length;
    const body = Buffer.alloc(24 * 1024 * 1024, 0x20);
    await h.sleep(100);
    const before = fds();
    const r = await h.request(gw.port, { method: 'POST', path: '/v1/echo', headers: { 'content-type': 'application/json' }, body });
    assert.equal(r.status, 503);
    await h.sleep(500);
    const delta = fds() - before;
    t.diagnostic(`${connections} aborted sends, fd delta ${delta}`);
    assert.ok(connections >= 8, `${connections} sends`);
    assert.ok(delta <= 2, `${delta} descriptors left open`);
  });

  test('with the orchestrator refusing connections, bodies past the memory budget spill instead of growing the gateway, and every one is answered and cleaned up', { skip: !fs.existsSync('/proc/self/status') }, async (t) => {
    // Review finding 2026-09-14 (scratchpad memflood.cjs, Node 20.20.2): the
    // orchestrator refusing connections as in every deploy, 400 junk-key
    // 1 MiB bodies took the gateway from 47 to 961 MiB of RSS, 0 responses.
    // SCALED: 96 bodies, a 4 MiB budget, the 110 s pre-commit window 6 s.
    const N = 96;
    const budget = 4 * 1024 * 1024;
    const spool = h.tmpdir('gw-membudget-flood-');
    const g = await spawnGatewayUnder(t, '', 1, {
      V1_GATEWAY_SPOOL_DIR: spool,
      V1_GATEWAY_MEMORY_BUDGET_BYTES: String(budget),
      PUBLIC_API_MIN_FREE_DISK_BYTES: '0',
      PUBLIC_API_GATEWAY_PRECOMMIT_RETRY_S: '6',
      V1_GATEWAY_PRECOMMIT_RETRY_INTERVAL_S: '0.5',
    });
    assert.equal(g.listening.memory_budget_bytes, budget);
    await h.sleep(300);
    const before = rssBytes(g.proc.pid);
    const body = JSON.stringify({ model: 'm', input: 'a'.repeat(1_000_000) });
    const statuses = [];
    const pending = [];
    for (let i = 0; i < N; i += 1) {
      pending.push(new Promise((resolve) => {
        const req = http.request({ host: '127.0.0.1', port: g.port, method: 'POST', path: '/v1/embeddings', agent: false, headers: { 'content-type': 'application/json', 'content-length': String(Buffer.byteLength(body)), authorization: 'Bearer junk' } }, (res) => {
          res.resume();
          res.on('end', () => resolve(statuses.push(res.statusCode)));
        });
        req.on('error', () => resolve(statuses.push('error')));
        req.end(body);
      }));
    }
    let peak = 0;
    let spilled = 0;
    for (let i = 0; i < 16 && statuses.length === 0; i += 1) {
      await h.sleep(250);
      peak = Math.max(peak, rssBytes(g.proc.pid) - before);
      spilled = Math.max(spilled, fs.readdirSync(spool).length);
    }
    t.diagnostic(`${N} waiting 1 MiB bodies: RSS +${(peak / 1048576).toFixed(1)} MiB, ${spilled} spilled`);
    // Measured +39.3 to +49.7 MiB over six runs, some on a loaded host. Unbudgeted,
    // the bodies alone are 96 MiB (the mutant measured +111.6 MiB, 0 spilled).
    assert.ok(peak < budget + 72 * 1024 * 1024, `RSS grew ${(peak / 1048576).toFixed(1)} MiB`);
    assert.ok(spilled >= N - Math.floor(budget / 1_000_000) - 1, `${spilled} of ${N} bodies spilled`);
    await Promise.all(pending);
    assert.deepEqual([...new Set(statuses)], [503], 'each waiting body got the pre-commit 503, none was dropped');
    for (let i = 0; i < 40 && fs.readdirSync(spool).length > 0; i += 1) await h.sleep(50);
    assert.deepEqual(fs.readdirSync(spool), [], 'every spilled body was removed');
  });

  test('a relayed body is byte-exact whether it stayed in memory or spilled because the budget was spent, and the budget comes back when relays end', async (t) => {
    // SCALED: budget 256 MiB → 250 KiB; three 100 KiB bodies held open together.
    let held = [];
    const server = http.createServer((req, res) => {
      const hash = require('node:crypto').createHash('sha256');
      let bytes = 0;
      req.on('data', (c) => {
        bytes += c.length;
        hash.update(c);
      });
      req.on('end', () => held.push(() => res.end(JSON.stringify({ bytes, sha256: hash.digest('hex') }))));
    });
    await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
    const gw = await h.startGateway({ orchestratorPort: server.address().port, env: { V1_GATEWAY_MEMORY_BUDGET_BYTES: String(250_000), PUBLIC_API_MIN_FREE_DISK_BYTES: '0' } });
    t.after(async () => {
      await gw.close();
      server.closeAllConnections();
      server.close();
    });
    const memory = gw.gateway.gw.memoryBudget;
    const bodies = [0x41, 0x42, 0x43].map((b, i) => Buffer.concat([Buffer.from(`{"n":${i},"x":"`), Buffer.alloc(100_000, b), Buffer.from('"}')]));
    const replies = [];
    for (const body of bodies) {
      replies.push(h.request(gw.port, { method: 'POST', path: '/v1/echo', headers: { 'content-type': 'application/json' }, body }));
      for (let i = 0; i < 100 && held.length < replies.length; i += 1) await h.sleep(20);
    }
    assert.equal(held.length, 3);
    assert.equal(memory.used, bodies[0].length + bodies[1].length, 'the first two bodies are in memory');
    assert.equal(fs.readdirSync(gw.spool).length, 1, 'the third spilled');
    for (const release of held) release();
    held = [];
    const answers = await Promise.all(replies);
    const sha = (b) => require('node:crypto').createHash('sha256').update(b).digest('hex');
    assert.deepEqual(answers.map((a) => JSON.parse(a.text)), bodies.map((b) => ({ bytes: b.length, sha256: sha(b) })));
    for (let i = 0; i < 50 && gw.gateway.gw.registry.size > 0; i += 1) await h.sleep(20);
    assert.equal(memory.used, 0);
    for (let i = 0; i < 50 && fs.readdirSync(gw.spool).length > 0; i += 1) await h.sleep(20);
    assert.deepEqual(fs.readdirSync(gw.spool), []);
  });

  test('a restarted gateway removes the bodies a previous process left and tightens the directory', () => {
    const spool = h.tmpdir('gw-spool-stale-');
    fs.chmodSync(spool, 0o755);
    fs.writeFileSync(path.join(spool, 'body-stale'), 'x');
    fs.writeFileSync(path.join(spool, 'unrelated'), 'y');
    assert.equal(B.prepareSpool(spool), 1);
    assert.deepEqual(fs.readdirSync(spool), ['unrelated']);
    assert.equal(fs.statSync(spool).mode & 0o777, 0o700);
  });
});
