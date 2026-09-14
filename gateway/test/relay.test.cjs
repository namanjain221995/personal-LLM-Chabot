'use strict';
/**
 * The relay against a stub orchestrator (2026-09-13). Timers run at the
 * design's DEFAULT values unless a test says it scales one, and says why.
 */

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const net = require('node:net');
const { performance } = require('node:perf_hooks');

const h = require('../testkit/harness.cjs');

const JSON_HEADERS = { 'content-type': 'application/json', authorization: 'Bearer sk-test' };

function chatBody(model, extra = {}) {
  return JSON.stringify({ model, messages: [{ role: 'user', content: 'hi' }], ...extra });
}

/** Cleanup that runs even when an assertion failed (a live child keeps the runner waiting). */
async function cleanup(gw, stub = null, later = () => null) {
  if (gw) await gw.close();
  if (stub) await stub.kill();
  const other = later();
  if (other) await other.kill();
}

function chunkFrames(text) {
  return h
    .sseDataPayloads(text)
    .filter((d) => d !== '[DONE]')
    .map((d) => JSON.parse(d));
}

test.describe('relaying an answer', { concurrency: true, timeout: 120_000 }, () => {
  let stub;
  let gw;
  test.before(async () => {
    stub = await h.startStub();
    gw = await h.startGateway({ orchestratorPort: stub.port });
  });
  test.after(async () => {
    await gw.close();
    await stub.kill();
  });

  test('a JSON answer keeps its status, its allowlisted headers and its exact body', async () => {
    const r = await h.request(gw.port, {
      method: 'POST',
      path: '/v1/chat/completions',
      headers: JSON_HEADERS,
      body: chatBody('stub/tokens=3/interval=5/hdrs'),
    });
    assert.equal(r.status, 200);
    assert.equal(JSON.parse(r.text).choices[0].message.content, 'w1 w2 w3 ');
    assert.equal(r.headers['x-request-id'], 'req_stub');
    assert.equal(r.headers['x-ratelimit-remaining-requests'], '99');
    assert.equal(r.headers['x-content-type-options'], 'nosniff');
    for (const name of ['set-cookie', 'access-control-allow-credentials', 'x-techsara-debug', 'x-techsara-run', 'x-powered-by', 'server-timing']) {
      assert.equal(r.headers[name], undefined, `${name} must not be relayed`);
    }
  });

  test('a refusal keeps its status and its Retry-After', async () => {
    const r = await h.request(gw.port, {
      method: 'POST',
      path: '/v1/chat/completions',
      headers: JSON_HEADERS,
      body: chatBody('stub/status=429'),
    });
    assert.equal(r.status, 429);
    assert.equal(r.headers['retry-after'], '7');
    assert.equal(JSON.parse(r.text).error.code, 'stub_status');
  });

  test('a stream reaches the caller one frame at a time while the generation is still running', async () => {
    const r = await h.request(gw.port, {
      method: 'POST',
      path: '/v1/chat/completions',
      headers: JSON_HEADERS,
      body: chatBody('stub/tokens=20/interval=100', { stream: true }),
    });
    assert.equal(r.status, 200);
    assert.equal(r.headers['content-type'], 'text/event-stream; charset=utf-8');
    assert.equal(r.headers['x-accel-buffering'], 'no');
    assert.equal(h.chatText(r.text), h.expectedText(20));
    // The first token is due at 100 ms and the last at 2,000 ms: a buffering
    // relay would deliver everything at once near the end.
    const firstToken = r.chunks.find((c) => c.buf.toString().includes('w1 '));
    assert.ok(firstToken.t - (r.chunks[0].t - r.firstByteMs) < 1000, 'first token arrived early');
    assert.ok(r.chunks.length >= 10, `arrived in ${r.chunks.length} chunks`);
  });

  test('no ts-seq comment and no x-techsara header ever reaches the caller', async () => {
    const r = await h.request(gw.port, {
      method: 'POST',
      path: '/v1/responses',
      headers: { ...JSON_HEADERS, 'x-techsara-attempt': 'client-chosen', 'x-techsara-resume-after': '3' },
      body: JSON.stringify({ model: 'stub/tokens=5/interval=10', input: 'hi', stream: true }),
    });
    assert.equal(r.status, 200);
    assert.ok(!r.text.includes('ts-seq'), r.text);
    assert.ok(!Object.keys(r.headers).some((k) => k.startsWith('x-techsara-')));
    const seqs = h.sseDataPayloads(r.text).map((d) => JSON.parse(d).sequence_number);
    // The client's own resume-after was dropped: the stream starts at 1.
    assert.deepEqual(seqs, Array.from({ length: seqs.length }, (_, i) => i + 1));
    const launch = stub.events().find((e) => e.route === 'responses' && e.kind === 'launch');
    assert.notEqual(launch.attempt, 'client-chosen');
    assert.match(launch.attempt, /^[0-9a-f-]{36}$/);
    assert.equal(launch.resume_after, null);
  });

  test('a redirect is refused rather than relayed, and names nothing inside', async () => {
    const r = await h.request(gw.port, { path: '/v1/redirect', headers: JSON_HEADERS });
    assert.equal(r.status, 500);
    assert.equal(r.headers.location, undefined);
    assert.deepEqual(JSON.parse(r.text), {
      error: { message: 'Something went wrong on our side.', type: 'server_error', code: 'internal_error', param: null, request_id: null },
    });
  });

  test('a 204 and a 304 are answered without a body', async () => {
    const a = await h.request(gw.port, { path: '/v1/nocontent' });
    assert.equal(a.status, 204);
    assert.equal(a.text, '');
    const b = await h.request(gw.port, { path: '/v1/notmodified' });
    assert.equal(b.status, 304);
    assert.equal(b.text, '');
  });

  test('a byte download keeps its Content-Length and its download headers (Files design §12.5)', async () => {
    const r = await h.request(gw.port, { path: '/v1/files/file-abc/content' });
    assert.equal(r.status, 200);
    assert.equal(r.headers['content-length'], '1000');
    assert.equal(r.buffer.length, 1000);
    assert.equal(r.headers.etag, '"sha"');
    assert.equal(r.headers['content-disposition'], 'attachment; filename="a.bin"');
  });

  test('paths outside /v1 are 404, including dot segments that resolve outside it', async () => {
    for (const p of ['/api/admin', '/v1/../api/admin', '/v1/%2e%2e/api/admin', '/v1/./../x', '/v1x', '/']) {
      const r = await h.request(gw.port, { path: p });
      assert.equal(r.status, 404, p);
    }
    assert.ok(!stub.events().some((e) => e.kind === 'request' && !e.url.startsWith('/v1')));
  });

  test('a doubly encoded dot segment stays a literal segment upstream, as it does through Next', async () => {
    const r = await h.request(gw.port, { path: '/v1/echo/%252e%252e/x' });
    assert.equal(r.status, 200);
    assert.equal(JSON.parse(r.text).url, '/v1/echo/%252e%252e/x');
  });

  test('a segment is re-encoded so nothing can smuggle a separator upstream', async () => {
    const r = await h.request(gw.port, { path: '/v1/echo/a%2Fb/c%3Fd?x=1&y=2' });
    assert.equal(r.status, 200);
    assert.equal(JSON.parse(r.text).url, '/v1/echo/a%2Fb/c%3Fd?x=1&y=2');
  });

  test('a declared oversize body is refused with 413 before the orchestrator is called', async () => {
    const r = await h.request(gw.port, {
      method: 'POST',
      path: '/v1/echo?probe=declared-oversize',
      headers: { 'content-type': 'application/json', 'content-length': String(2 * 1024 * 1024) },
      body: (req) => {
        req.write(Buffer.alloc(1024));
      },
    });
    assert.equal(r.status, 413);
    assert.deepEqual(JSON.parse(r.text).error, {
      message: 'The request body is larger than the 1048576 byte limit.',
      type: 'invalid_request_error',
      code: 'request_too_large',
      param: null,
      request_id: null,
    });
    assert.ok(!stub.events().some((e) => e.kind === 'request' && e.url.includes('declared-oversize')));
  });

  test('a chunked body that runs over the cap is refused with 413', async () => {
    const r = await h.request(gw.port, {
      method: 'POST',
      path: '/v1/echo',
      headers: { 'content-type': 'application/json', 'transfer-encoding': 'chunked' },
      body: (req) => {
        for (let i = 0; i < 12; i += 1) req.write(Buffer.alloc(128 * 1024, 32));
        req.end();
      },
    });
    assert.equal(r.status, 413);
  });

  test('a JSON body above 1 MiB is spilled to the spool and sent upstream byte for byte', async () => {
    const big = Buffer.alloc(19 * 1024 * 1024, 97);
    const body = Buffer.concat([Buffer.from('{"model":"stub/tokens=1/interval=5","input":"'), big, Buffer.from('"}')]);
    const r = await h.request(gw.port, {
      method: 'POST',
      path: '/v1/responses',
      headers: JSON_HEADERS,
      body,
    });
    assert.equal(r.status, 200);
    assert.equal(JSON.parse(r.text).status, 'completed');
    const launch = stub.events().filter((e) => e.route === 'responses').pop();
    assert.equal(launch.kind, 'launch');
    // The spool file is gone once the relay ends.
    await h.sleep(50);
    assert.deepEqual(fs.readdirSync(gw.spool), []);
  });

  test('a 64 MiB raw upload part streams through at the Files design cap', async () => {
    const part = Buffer.alloc(64 * 1024 * 1024, 5);
    const r = await h.request(gw.port, {
      method: 'PUT',
      path: '/v1/uploads/upload_x/parts/0',
      headers: { 'content-type': 'application/octet-stream', 'content-length': String(part.length) },
      body: part,
    });
    assert.equal(r.status, 200);
    const echo = JSON.parse(r.text);
    assert.equal(echo.bytes, part.length);
    assert.equal(echo.sha256, require('node:crypto').createHash('sha256').update(part).digest('hex'));
    assert.equal(echo.headers['content-length'], String(part.length));
  });

  test('a raw upload part reaches the orchestrator while the client is still sending it', async () => {
    const chunk = Buffer.alloc(1024 * 1024, 4);
    let lastWriteAt = null;
    const r = await h.request(gw.port, {
      method: 'PUT',
      path: '/v1/uploads/upload_x/parts/2',
      headers: { 'content-type': 'application/octet-stream', 'content-length': String(8 * chunk.length) },
      body: (req) => {
        let sent = 0;
        const tick = setInterval(() => {
          req.write(chunk);
          sent += 1;
          if (sent === 8) {
            clearInterval(tick);
            lastWriteAt = Date.now();
            req.end();
          }
        }, 150);
      },
    });
    assert.equal(r.status, 200);
    const echo = JSON.parse(r.text);
    assert.equal(echo.bytes, 8 * chunk.length);
    assert.ok(echo.first_byte_at < lastWriteAt - 500, `first byte upstream ${lastWriteAt - echo.first_byte_at} ms before the client's last write`);
  });

  test('a raw upload part without Content-Length is refused with 411', async () => {
    const r = await h.request(gw.port, {
      method: 'PUT',
      path: '/v1/uploads/upload_x/parts/1',
      headers: { 'content-type': 'application/octet-stream', 'transfer-encoding': 'chunked' },
      body: (req) => req.end(Buffer.alloc(10)),
    });
    assert.equal(r.status, 411);
    assert.equal(JSON.parse(r.text).error.type, 'invalid_request_error');
  });

  test('an early 401 on a streamed part is relayed after draining the body, not as a reset', async () => {
    const part = Buffer.alloc(8 * 1024 * 1024, 1);
    const r = await h.request(gw.port, {
      method: 'POST',
      path: '/v1/uploads/upload_x/parts?stub=early401',
      headers: { 'content-type': 'multipart/form-data; boundary=x', 'content-length': String(part.length) },
      body: part,
    });
    assert.equal(r.error, null);
    assert.equal(r.status, 401);
    assert.equal(JSON.parse(r.text).error.code, 'invalid_api_key');
    assert.equal(r.headers['www-authenticate'], 'Bearer error="invalid_token"');
  });

  test('a slow orchestrator applies backpressure instead of buffering the part', async () => {
    const part = Buffer.alloc(24 * 1024 * 1024, 9);
    const r = await h.request(gw.port, {
      method: 'POST',
      path: '/v1/files?stub=slow',
      headers: { 'content-type': 'multipart/form-data; boundary=x', 'content-length': String(part.length) },
      body: part,
    });
    assert.equal(r.status, 200);
    assert.equal(JSON.parse(r.text).bytes, part.length);
  });
});

test.describe('the byte invariant for a silent orchestrator', { concurrency: true, timeout: 120_000 }, () => {
  let stub;
  let gw;
  test.before(async () => {
    stub = await h.startStub();
    gw = await h.startGateway({ orchestratorPort: stub.port });
  });
  test.after(async () => {
    await gw.close();
    await stub.kill();
  });

  test('a sync call to an orchestrator silent for 40 s gets a byte by 15 s and one every 15 s after (default timers)', async () => {
    const r = await h.request(gw.port, {
      method: 'POST',
      path: '/v1/chat/completions',
      headers: JSON_HEADERS,
      body: chatBody('stub/tokens=3/interval=10/silent=40000'),
    });
    assert.equal(r.status, 200);
    assert.ok(r.firstByteMs >= 14_500 && r.firstByteMs <= 15_600, `first byte at ${r.firstByteMs}`);
    assert.ok(r.maxGapMs <= 15_600, `max gap ${r.maxGapMs}`);
    assert.match(r.text, /^ +\{/);
    assert.equal(JSON.parse(r.text).choices[0].message.content, 'w1 w2 w3 ');
    assert.equal(r.headers['cache-control'], 'no-store, no-transform');
  });

  test('a stream to an orchestrator silent for 20 s is committed with a ping by 15 s', async () => {
    const r = await h.request(gw.port, {
      method: 'POST',
      path: '/v1/chat/completions',
      headers: JSON_HEADERS,
      body: chatBody('stub/tokens=4/interval=10/silent=20000', { stream: true }),
    });
    assert.equal(r.status, 200);
    assert.ok(r.firstByteMs >= 14_500 && r.firstByteMs <= 15_600, `first byte at ${r.firstByteMs}`);
    assert.ok(r.text.startsWith(': ping\n\n'));
    assert.equal(h.chatText(r.text), h.expectedText(4));
  });

  test('a stream the gateway committed turns a late refusal into an error event every SDK raises', async () => {
    const r = await h.request(gw.port, {
      method: 'POST',
      path: '/v1/chat/completions',
      headers: JSON_HEADERS,
      body: chatBody('stub/silent=16000/latestatus=400', { stream: true }),
    });
    assert.equal(r.status, 200);
    assert.match(r.text, /event: error\ndata: \{"error":\{"message":"stub refusal 400"/);
    assert.equal(r.error, null);
  });

  test('a sync call the gateway committed is cut on a late refusal, so the SDK retries and sees the real status', async () => {
    const r = await h.request(gw.port, {
      method: 'POST',
      path: '/v1/chat/completions',
      headers: JSON_HEADERS,
      body: chatBody('stub/silent=16000/latestatus=400'),
    });
    assert.equal(r.status, 200);
    assert.ok(r.error, 'the connection was cut');
    assert.match(r.text, /^ +$/);
  });
});

test.describe('surviving an orchestrator restart', { concurrency: true, timeout: 180_000 }, () => {
  test('a chat stream crosses a SIGKILL and relaunch with no duplicated or missing frame', async (t) => {
    const stub = await h.startStub();
    const gw = await h.startGateway({ orchestratorPort: stub.port });
    let replacement = null;
    t.after(() => cleanup(gw, stub, () => replacement));
    let killedAfter = null;
    const r = await h.request(gw.port, {
      method: 'POST',
      path: '/v1/chat/completions',
      headers: JSON_HEADERS,
      body: chatBody('stub/tokens=60/interval=100', { stream: true }),
      onChunk: (c) => {
        if (killedAfter === null && c.buf.toString().includes('"w15 "')) {
          killedAfter = 15;
          stub.kill('SIGKILL').then(async () => {
            await h.sleep(5000);
            replacement = await h.startStub({ port: stub.port, stateDir: stub.stateDir });
          });
        }
      },
    });
    assert.equal(r.status, 200);
    assert.equal(r.error, null);
    const frames = chunkFrames(r.text);
    const contents = frames.map((f) => f.choices[0].delta.content).filter((c) => c !== undefined);
    assert.deepEqual(contents, Array.from({ length: 60 }, (_, i) => `w${i + 1} `));
    assert.equal(h.sseDataPayloads(r.text).filter((d) => d === '[DONE]').length, 1);
    assert.equal(frames.filter((f) => f.choices[0].finish_reason === 'stop').length, 1);
    const attach = replacement.events().find((e) => e.kind === 'attach');
    assert.ok(attach.resume_after >= 15, `resumed after ${attach.resume_after}`);
    assert.equal(replacement.events().filter((e) => e.kind === 'launch').length, 1, 'generated once');
    assert.ok(gw.logs.some((l) => l.event === 'reattach_ok'));
  });

  test('a frame whose ts-seq was lost in a crash reaches the client exactly once', async (t) => {
    const stub = await h.startStub();
    const gw = await h.startGateway({ orchestratorPort: stub.port });
    let replacement = null;
    t.after(() => cleanup(gw, stub, () => replacement));
    const exited = new Promise((resolve) => stub.proc.once('exit', resolve));
    const pending = h.request(gw.port, {
      method: 'POST',
      path: '/v1/chat/completions',
      headers: JSON_HEADERS,
      body: chatBody('stub/tokens=20/interval=50/crashbeforemarker=8', { stream: true }),
    });
    await exited;
    await h.sleep(1500);
    replacement = await h.startStub({ port: stub.port, stateDir: stub.stateDir });
    const r = await pending;
    assert.equal(r.error, null);
    const contents = chunkFrames(r.text).map((f) => f.choices[0].delta.content).filter((c) => c !== undefined);
    assert.deepEqual(contents, Array.from({ length: 20 }, (_, i) => `w${i + 1} `));
    assert.equal(replacement.events().find((e) => e.kind === 'attach').resume_after, 7);
  });

  test('a Responses stream crosses a SIGTERM with contiguous sequence numbers and one response.created', async (t) => {
    const stub = await h.startStub();
    const gw = await h.startGateway({ orchestratorPort: stub.port });
    let replacement = null;
    t.after(() => cleanup(gw, stub, () => replacement));
    let killed = false;
    const r = await h.request(gw.port, {
      method: 'POST',
      path: '/v1/responses',
      headers: JSON_HEADERS,
      body: JSON.stringify({ model: 'stub/tokens=40/interval=100', input: 'hi', stream: true }),
      onChunk: (c) => {
        if (!killed && c.buf.toString().includes('"delta":"w10 "')) {
          killed = true;
          stub.kill('SIGTERM').then(async () => {
            await h.sleep(2500);
            replacement = await h.startStub({ port: stub.port, stateDir: stub.stateDir });
          });
        }
      },
    });
    assert.equal(r.status, 200);
    assert.equal(r.error, null);
    const events = h.sseDataPayloads(r.text).map((d) => JSON.parse(d));
    assert.deepEqual(events.map((e) => e.sequence_number), Array.from({ length: events.length }, (_, i) => i + 1));
    assert.equal(events.filter((e) => e.type === 'response.created').length, 1);
    assert.equal(events.at(-1).type, 'response.completed');
    const text = events.filter((e) => e.type === 'response.output_text.delta').map((e) => e.delta).join('');
    assert.equal(text, h.expectedText(40));
  });

  test('a sync caller gets the whole body when the orchestrator dies after 40 s of whitespace', async (t) => {
    const stub = await h.startStub();
    const gw = await h.startGateway({ orchestratorPort: stub.port });
    let replacement = null;
    t.after(() => cleanup(gw, stub, () => replacement));
    const pending = h.request(gw.port, {
      method: 'POST',
      path: '/v1/chat/completions',
      headers: JSON_HEADERS,
      body: chatBody('stub/tokens=59/interval=1000'),
    });
    await h.sleep(41_000);
    await stub.kill('SIGKILL');
    await h.sleep(3000);
    replacement = await h.startStub({ port: stub.port, stateDir: stub.stateDir });
    const r = await pending;
    assert.equal(r.status, 200);
    assert.equal(r.error, null);
    assert.match(r.text, /^ +\{/);
    assert.equal(JSON.parse(r.text).choices[0].message.content, h.expectedText(59));
    assert.ok(r.maxGapMs <= 15_600, `max gap ${r.maxGapMs}`);
    assert.equal(replacement.events().filter((e) => e.kind === 'attach').length, 1);
  });

  test('301 s of upstream silence is a failure that re-attaches (silence scaled to 3 s)', async (t) => {
    // SCALED: V1_GATEWAY_UPSTREAM_SILENCE_S 300 → 3, so the test takes
    // seconds; the rule under test is "silence past the watchdog re-attaches".
    const stub = await h.startStub();
    const gw = await h.startGateway({ orchestratorPort: stub.port, env: { V1_GATEWAY_UPSTREAM_SILENCE_S: '3' } });
    t.after(() => cleanup(gw, stub));
    const r = await h.request(gw.port, {
      method: 'POST',
      path: '/v1/chat/completions',
      headers: JSON_HEADERS,
      body: chatBody('stub/tokens=12/interval=20/stall=5', { stream: true }),
    });
    assert.equal(r.status, 200);
    assert.equal(h.chatText(r.text), h.expectedText(12));
    const failed = gw.logs.find((l) => l.event === 'upstream_failed');
    assert.ok(failed, 'the silence was treated as a failure');
    const attach = stub.events().find((e) => e.kind === 'attach');
    assert.equal(attach.resume_after, 5);
  });
});

test('an answer whose connection dies after its head is retried, and its status never leaks into the final 503 (timers scaled)', { timeout: 30_000 }, async (t) => {
  // SCALED: heartbeat 15 → 1 s, retry interval 2 → 1.5 s, budget 110 → 4 s,
  // so a heartbeat would fall inside a retry sleep if the dead head lingered.
  const stub = await h.startStub();
  const gw = await h.startGateway({
    orchestratorPort: stub.port,
    env: { V1_GATEWAY_HEARTBEAT_S: '1', V1_GATEWAY_PRECOMMIT_RETRY_INTERVAL_S: '1.5', PUBLIC_API_GATEWAY_PRECOMMIT_RETRY_S: '4' },
  });
  t.after(async () => {
    await gw.close();
    await stub.kill();
  });
  const r = await h.request(gw.port, {
    method: 'POST',
    path: '/v1/chat/completions',
    headers: JSON_HEADERS,
    body: chatBody('stub/headdie'),
  });
  assert.equal(r.status, 503);
  assert.equal(r.headers['retry-after'], '30');
  assert.equal(JSON.parse(r.text).error.code, 'model_unavailable');
  assert.ok(stub.events().filter((e) => e.kind === 'launch' || e.kind === 'attach').length >= 3, 'retried with the same attempt');
});

test.describe('pre-commit connect retry (real time, default 110 s budget)', { concurrency: true, timeout: 180_000 }, () => {
  test('an orchestrator refusing connections for 95 s is waited out and the client gets 200 with no retry of its own', async (t) => {
    const port = await h.freePort();
    const gw = await h.startGateway({ orchestratorPort: port });
    let stub = null;
    t.after(() => cleanup(gw, null, () => stub));
    const later = h.sleep(95_000).then(async () => {
      stub = await h.startStub({ port });
    });
    const r = await h.request(gw.port, {
      method: 'POST',
      path: '/v1/chat/completions',
      headers: JSON_HEADERS,
      body: chatBody('stub/tokens=2/interval=5'),
    });
    await later;
    assert.equal(r.status, 200);
    assert.ok(r.elapsedMs >= 95_000 && r.elapsedMs < 99_000, `answered at ${r.elapsedMs}`);
    assert.equal(JSON.parse(r.text).choices[0].message.content, 'w1 w2 ');
    assert.equal(stub.events().filter((e) => e.kind === 'launch').length, 1);
    assert.ok(gw.logs.some((l) => l.event === 'precommit_recovered'));
  });

  test('an orchestrator unreachable for 115 s gets the client a 503 Retry-After 30 by 111 s', async (t) => {
    const port = await h.freePort();
    const gw = await h.startGateway({ orchestratorPort: port });
    t.after(() => cleanup(gw));
    const r = await h.request(gw.port, {
      method: 'POST',
      path: '/v1/chat/completions',
      headers: JSON_HEADERS,
      body: chatBody('stub/tokens=2/interval=5'),
    });
    assert.equal(r.status, 503);
    assert.equal(r.headers['retry-after'], '30');
    assert.ok(r.elapsedMs >= 109_000 && r.elapsedMs <= 111_000, `answered at ${r.elapsedMs}`);
    assert.deepEqual(JSON.parse(r.text).error, {
      message: 'The service is temporarily unavailable. Please retry.',
      type: 'service_unavailable_error',
      code: 'model_unavailable',
      param: null,
      request_id: null,
    });
  });
});

/** A raw request: the status line, and when the gateway closed the socket. */
function rawRequest(port, text, { waitMs = 3000 } = {}) {
  return new Promise((resolve) => {
    const started = performance.now();
    let got = '';
    let closedAt = null;
    const sock = net.connect(port, '127.0.0.1', () => sock.write(text));
    sock.on('data', (d) => {
      got += d;
    });
    sock.on('error', () => undefined);
    sock.on('close', () => {
      closedAt = performance.now() - started;
    });
    setTimeout(() => {
      resolve({ statusLine: got.split('\r\n')[0], text: got, closedAt });
      sock.destroy();
    }, waitMs);
  });
}

test.describe('a failure after the orchestrator may hold the whole request', { concurrency: true, timeout: 60_000 }, () => {
  test('a generation read whole and then dropped is not sent again to an orchestrator not yet seen to attach: 503 with x-should-retry false', async (t) => {
    // Review finding 2026-09-13 (scratchpad p6_precommit_dup.cjs): it was
    // re-POSTed, and a build that ignores the attempt id launched it twice.
    const stub = await h.startStub();
    const gw = await h.startGateway({ orchestratorPort: stub.port });
    t.after(() => cleanup(gw, stub));
    assert.equal(gw.gateway.gw.attachEvidence, false);
    const r = await h.request(gw.port, {
      method: 'POST',
      path: '/v1/chat/completions',
      headers: JSON_HEADERS,
      body: chatBody('stub/tokens=2/interval=5/dropafter=300'),
    });
    assert.equal(r.status, 503);
    assert.equal(r.headers['x-should-retry'], 'false');
    assert.equal(r.headers['retry-after'], '30');
    assert.equal(JSON.parse(r.text).error.code, 'model_unavailable');
    await h.sleep(300);
    const runs = stub.events().filter((e) => e.kind === 'launch' || e.kind === 'attach');
    assert.deepEqual(runs.map((e) => e.kind), ['launch']);
    assert.ok(gw.logs.some((l) => l.event === 'precommit_outcome_unknown'));
  });

  test('the same drop is sent again with the same attempt once the orchestrator has named a run, and the caller gets the answer', async (t) => {
    const stub = await h.startStub();
    const gw = await h.startGateway({ orchestratorPort: stub.port });
    t.after(() => cleanup(gw, stub));
    const first = await h.request(gw.port, { method: 'POST', path: '/v1/chat/completions', headers: JSON_HEADERS, body: chatBody('stub/tokens=1/interval=5') });
    assert.equal(first.status, 200);
    assert.equal(gw.gateway.gw.attachEvidence, true, 'the first answer named its run');
    const r = await h.request(gw.port, {
      method: 'POST',
      path: '/v1/chat/completions',
      headers: JSON_HEADERS,
      body: chatBody('stub/tokens=2/interval=5/dropafter=300'),
    });
    assert.equal(r.status, 200);
    assert.equal(JSON.parse(r.text).choices[0].message.content, 'w1 w2 ');
    const runs = stub.events().filter((e) => (e.kind === 'launch' || e.kind === 'attach') && e.route === 'chat/completions');
    assert.deepEqual(runs.map((e) => e.kind), ['launch', 'launch', 'attach']);
    assert.equal(runs[1].attempt, runs[2].attempt, 'the same attempt, so the orchestrator attached');
  });

  test('a head without X-TechSara-Run that dies is not sent again: the orchestrator has shown it does not attach', async (t) => {
    const stub = await h.startStub();
    const gw = await h.startGateway({ orchestratorPort: stub.port });
    t.after(() => cleanup(gw, stub));
    const r = await h.request(gw.port, { method: 'POST', path: '/v1/chat/completions', headers: JSON_HEADERS, body: chatBody('stub/headdie/norun') });
    assert.equal(r.status, 503);
    assert.equal(r.headers['x-should-retry'], 'false');
    assert.equal(stub.events().filter((e) => e.kind === 'launch' || e.kind === 'attach').length, 1);
  });

  test('a DELETE read and dropped is answered 503 with x-should-retry false, never sent twice', async (t) => {
    const stub = await h.startStub();
    const gw = await h.startGateway({ orchestratorPort: stub.port });
    t.after(() => cleanup(gw, stub));
    const r = await h.request(gw.port, { method: 'DELETE', path: '/v1/files/file-abc?stub=drop', headers: JSON_HEADERS });
    assert.equal(r.status, 503);
    assert.equal(r.headers['x-should-retry'], 'false');
    await h.sleep(2500);
    assert.equal(stub.events().filter((e) => e.kind === 'drop').length, 1);
  });

  test('an idempotent upload complete read and dropped is sent again with the same attempt until the budget ends (timers scaled)', async (t) => {
    // SCALED: retry interval 2 → 0.5 s, budget 110 → 3 s.
    const stub = await h.startStub();
    const gw = await h.startGateway({
      orchestratorPort: stub.port,
      env: { V1_GATEWAY_PRECOMMIT_RETRY_INTERVAL_S: '0.5', PUBLIC_API_GATEWAY_PRECOMMIT_RETRY_S: '3' },
    });
    t.after(() => cleanup(gw, stub));
    const r = await h.request(gw.port, { method: 'POST', path: '/v1/uploads/upload_x/complete?stub=drop', headers: JSON_HEADERS, body: '{}' });
    assert.equal(r.status, 503);
    assert.equal(r.headers['x-should-retry'], undefined, 'an ordinary retryable 503');
    const drops = stub.events().filter((e) => e.kind === 'drop');
    assert.ok(drops.length >= 4, `${drops.length} sends`);
    assert.equal(new Set(drops.map((e) => e.attempt)).size, 1);
  });

  test('a connection refused before the request was written is still waited out on a route that may not be sent twice', async (t) => {
    const port = await h.freePort();
    const gw = await h.startGateway({ orchestratorPort: port });
    let stub = null;
    t.after(() => cleanup(gw, null, () => stub));
    const later = h.sleep(3000).then(async () => {
      stub = await h.startStub({ port });
    });
    const r = await h.request(gw.port, { method: 'DELETE', path: '/v1/files/file-abc', headers: JSON_HEADERS });
    await later;
    assert.equal(r.status, 200);
    assert.equal(JSON.parse(r.text).method, 'DELETE');
    assert.ok(r.elapsedMs >= 3000, `answered at ${Math.round(r.elapsedMs)} ms`);
  });
});

test.describe('pooled and fresh orchestrator connections', { concurrency: true, timeout: 30_000 }, () => {
  async function orchestrator(t) {
    const seen = new WeakSet();
    const stats = { sockets: 0, stale: 0 };
    const server = http.createServer({ keepAliveTimeout: 60_000 }, (req, res) => {
      if (seen.has(req.socket)) {
        if (req.url.includes('stale')) {
          // An idle keep-alive socket closed under the request, as uvicorn's 5 s timer does.
          stats.stale += 1;
          req.socket.destroy();
          return;
        }
      } else {
        seen.add(req.socket);
        stats.sockets += 1;
      }
      req.resume();
      req.on('end', () => {
        res.writeHead(200, { 'content-type': 'application/json', 'x-techsara-run': 'none' });
        res.end('{"object":"list","data":[]}');
      });
    });
    await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
    const gw = await h.startGateway({ orchestratorPort: server.address().port });
    t.after(async () => {
      await gw.close();
      server.closeAllConnections();
      server.close();
    });
    return { gw, stats };
  }

  test('a pooled connection the orchestrator closed while idle is retried at once on a new connection, not after the 2 s interval', async (t) => {
    const { gw, stats } = await orchestrator(t);
    const first = await h.request(gw.port, { method: 'POST', path: '/v1/embeddings', headers: JSON_HEADERS, body: '{"input":"a"}' });
    assert.equal(first.status, 200);
    await h.sleep(50);
    const r = await h.request(gw.port, { method: 'POST', path: '/v1/embeddings?stale=1', headers: JSON_HEADERS, body: '{"input":"a"}' });
    assert.equal(r.status, 200);
    assert.equal(stats.stale, 1, 'the second request really met a closed pooled socket');
    assert.ok(r.elapsedMs < 1000, `answered in ${Math.round(r.elapsedMs)} ms`);
    assert.ok(gw.logs.some((l) => l.event === 'precommit_stale_socket'));
    assert.ok(!gw.logs.some((l) => l.event === 'precommit_retrying'));
  });

  test('a request that may not be sent twice always gets its own connection, so it cannot meet a closing pooled one', async (t) => {
    const { gw, stats } = await orchestrator(t);
    for (let i = 0; i < 3; i += 1) {
      const r = await h.request(gw.port, { method: 'DELETE', path: '/v1/files/file-1?stale=1', headers: JSON_HEADERS });
      assert.equal(r.status, 200);
    }
    assert.equal(stats.sockets, 3);
    assert.equal(stats.stale, 0);
  });
});

test('a GET that declares a body is refused 413 and its socket closed at once, instead of held with no timer', async (t) => {
  // Review finding 2026-09-13 (scratchpad p4_getbody.cjs): answered, and the
  // socket still open 10 s later with the idle guard never armed.
  const stub = await h.startStub();
  const gw = await h.startGateway({ orchestratorPort: stub.port });
  t.after(() => cleanup(gw, stub));
  const r = await rawRequest(gw.port, 'GET /v1/models?probe=getbody HTTP/1.1\r\nHost: x\r\nContent-Length: 1073741824\r\n\r\n', { waitMs: 1500 });
  assert.equal(r.statusLine, 'HTTP/1.1 413 Payload Too Large');
  assert.ok(r.closedAt !== null && r.closedAt < 1000, `closed at ${r.closedAt}`);
  const chunked = await rawRequest(gw.port, 'OPTIONS /v1/models?probe=getbody HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n', { waitMs: 1500 });
  assert.equal(chunked.statusLine, 'HTTP/1.1 413 Payload Too Large');
  const empty = await h.request(gw.port, { path: '/v1/models', headers: { ...JSON_HEADERS, 'content-length': '0' } });
  assert.equal(empty.status, 200);
  assert.ok(!stub.events().some((e) => e.kind === 'request' && e.url.includes('probe=getbody')));
});
