'use strict';
/**
 * Re-attach policy and its 1,800 s budget (2026-09-13). The budget itself is
 * proven twice: exactly, at the design's numbers, on a virtual clock; and
 * end to end through real sockets with the budget SCALED to 6 s.
 */

const test = require('node:test');
const assert = require('node:assert/strict');

const R = require('../lib/reattach.cjs');
const h = require('../testkit/harness.cjs');
const { performance } = require('node:perf_hooks');

const JSON_HEADERS = { 'content-type': 'application/json', authorization: 'Bearer sk-test' };

function base(overrides = {}) {
  return {
    shape: 'sse',
    run: { kind: 'response', id: 'resp_1' },
    terminal: false,
    jsonStarted: false,
    seqReliable: true,
    lastSeq: 7,
    replayable: true,
    deterministic: false,
    attach: 'auto',
    evidence: true,
    ...overrides,
  };
}

test.describe('the policy', () => {
  test('backoff runs 1, 2, 4, 8 and then holds at 10 seconds', () => {
    assert.deepEqual([0, 1, 2, 3, 4, 5, 9].map((i) => R.backoffMs(i)), [1000, 2000, 4000, 8000, 10000, 10000, 10000]);
  });

  test('X-TechSara-Run is read as a response, an audio job, none, or absent', () => {
    assert.deepEqual(R.parseRun('resp_abc'), { kind: 'response', id: 'resp_abc' });
    assert.deepEqual(R.parseRun('job:k1'), { kind: 'job', key: 'k1' });
    assert.deepEqual(R.parseRun('none'), { kind: 'none' });
    assert.deepEqual(R.parseRun(undefined), { kind: 'absent' });
    assert.deepEqual(R.parseRun('  '), { kind: 'absent' });
  });

  test('a stream resumes after the last confirmed sequence number', () => {
    assert.deepEqual(R.planReattach(base()), { ok: true, headers: { 'x-techsara-resume-after': '7' }, emptyBody: false });
  });

  test('a stream that already delivered its terminal event is not re-attached', () => {
    assert.equal(R.planReattach(base({ terminal: true })).reason, 'terminal');
  });

  test('a JSON body that has begun is not re-attached, because the client holds half an object', () => {
    assert.equal(R.planReattach(base({ shape: 'json', jsonStarted: true })).reason, 'partial_json');
  });

  test('a run that cannot be replayed is not re-attached, except a deterministic pooling call', () => {
    assert.equal(R.planReattach(base({ run: { kind: 'none' } })).reason, 'not_replayable');
    assert.equal(R.planReattach(base({ shape: 'json', run: { kind: 'none' }, deterministic: true })).ok, true);
  });

  test('an audio job re-attaches with its key and an empty body', () => {
    assert.deepEqual(R.planReattach(base({ shape: 'json', run: { kind: 'job', key: 'abc' } })), {
      ok: true,
      headers: { 'x-techsara-attach-job': 'abc' },
      emptyBody: true,
    });
  });

  test('an orchestrator not known to speak the protocol is never re-POSTed a generation', () => {
    assert.equal(R.planReattach(base({ run: { kind: 'absent' }, evidence: false })).reason, 'protocol_unknown');
    assert.equal(R.planReattach(base({ run: { kind: 'absent' }, evidence: false, attach: 'on' })).ok, true);
    assert.equal(R.planReattach(base({ attach: 'off' })).reason, 'attach_off');
  });

  test('a stream with an unconfirmed frame is not re-attached, because the replay would duplicate it', () => {
    assert.equal(R.planReattach(base({ seqReliable: false })).reason, 'sequence_unconfirmed');
    assert.equal(R.planReattach(base({ seqReliable: false, run: { kind: 'job', key: 'k' } })).reason, 'sequence_unconfirmed');
    assert.equal(R.planReattach(base({ seqReliable: false, shape: 'json', run: { kind: 'job', key: 'k' } })).ok, true);
  });

  test('an opaque byte body is never re-attached', () => {
    assert.equal(R.planReattach(base({ shape: 'opaque' })).reason, 'opaque_body');
  });

  test('the orchestrator answer to a re-attach is relayed, retried or final', () => {
    assert.equal(R.classifyAttachResponse(200, 'text/event-stream', 'sse'), 'relay');
    assert.equal(R.classifyAttachResponse(200, 'application/json', 'json'), 'relay');
    assert.equal(R.classifyAttachResponse(200, 'application/json', 'sse'), 'abort');
    assert.equal(R.classifyAttachResponse(404, 'application/json', 'sse'), 'abort');
    assert.equal(R.classifyAttachResponse(409, 'application/json', 'json'), 'abort');
    for (const s of [502, 503, 504]) assert.equal(R.classifyAttachResponse(s, 'application/json', 'sse'), 'retry');
  });

  test('a re-attach answer is relayed only when it names the run the client has been reading', () => {
    const m = (expected, header, shape = 'sse', idempotent = false) => R.attachAnswerMatches({ expected, header, shape, idempotent });
    const resp = { kind: 'response', id: 'resp_A' };
    assert.equal(m(resp, 'resp_A'), true);
    assert.equal(m(resp, 'resp_B'), false, 'a different run');
    assert.equal(m(resp, undefined), false, 'a build that ignores the protocol launched afresh');
    assert.equal(m(resp, 'none'), false);
    assert.equal(m({ kind: 'job', key: 'k1' }, 'job:k1'), true);
    assert.equal(m({ kind: 'job', key: 'k1' }, 'job:k2'), false);
    assert.equal(m({ kind: 'job', key: 'k1' }, 'k1'), false);
    // Committed while silent: no run seen, so the attach must name one.
    assert.equal(m({ kind: 'absent' }, 'resp_X', 'json'), true);
    assert.equal(m({ kind: 'absent' }, undefined, 'json'), false);
    assert.equal(m({ kind: 'absent' }, 'none', 'json'), false);
    // The same JSON whoever computes it: a GET, embeddings, rerank.
    assert.equal(m({ kind: 'none' }, undefined, 'json', true), true);
    assert.equal(m({ kind: 'absent' }, undefined, 'json', true), true);
    // ...but never an idempotent SSE replay, whose frames would repeat.
    assert.equal(m(resp, undefined, 'sse', true), false);
  });
});

function virtualLoop(backAt) {
  // A virtual clock: sleep advances time; an attempt succeeds iff the
  // orchestrator is back by the time it is made.
  let t = 0;
  const attempts = [];
  return R.reattachLoop({
    deadline: 1_800_000,
    now: () => t,
    sleep: async (ms) => {
      t += ms;
      return true;
    },
    backoff: (i) => R.backoffMs(i),
    tryOnce: async () => {
      attempts.push(t);
      return t >= backAt ? { outcome: 'relay' } : { outcome: 'retry' };
    },
  }).then((result) => ({ ...result, attempts, endedAt: t }));
}

test.describe('the 1,800 s budget at the design numbers (virtual clock)', () => {
  test('an orchestrator absent for 1,799 s is found', async () => {
    const r = await virtualLoop(1_799_000);
    assert.equal(r.outcome, 'relay');
    assert.ok(r.endedAt <= 1_800_000);
  });

  test('an orchestrator absent for 1,801 s is given up on, with one last attempt at exactly 1,800 s', async () => {
    const r = await virtualLoop(1_801_000);
    assert.equal(r.outcome, 'exhausted');
    assert.equal(r.attempts.at(-1), 1_800_000);
    assert.deepEqual(r.attempts.slice(0, 5), [1000, 3000, 7000, 15000, 25000]);
  });
});

test('a budget already spent by an earlier cycle ends the loop without another attempt', async () => {
  let tried = 0;
  const r = await R.reattachLoop({
    deadline: 100,
    now: () => 101,
    sleep: async () => true,
    backoff: () => 1000,
    tryOnce: async () => {
      tried += 1;
      return { outcome: 'relay' };
    },
  });
  assert.equal(r.outcome, 'exhausted');
  assert.equal(tried, 0);
});

test('an orchestrator that answers a head and dies on every re-attach still runs out the budget (scaled to 5 s)', { timeout: 30_000 }, async (t) => {
  // SCALED: budget 1800 → 5 s, heartbeat 15 → 1 s.
  const stub = await h.startStub({ env: { STUB_HEARTBEAT_MS: '500' } });
  const gw = await h.startGateway({ orchestratorPort: stub.port, env: { PUBLIC_API_GATEWAY_REATTACH_MAX_S: '5', V1_GATEWAY_HEARTBEAT_S: '1' } });
  let replacement = null;
  t.after(async () => {
    await gw.close();
    await stub.kill();
    if (replacement) await replacement.kill();
  });
  let killedAt = null;
  const pending = h.request(gw.port, {
    method: 'POST',
    path: '/v1/chat/completions',
    headers: JSON_HEADERS,
    body: JSON.stringify({ model: 'stub/tokens=40/interval=50', stream: true, messages: [] }),
    onChunk: (c) => {
      if (killedAt === null && c.buf.toString().includes('w5 ')) {
        killedAt = c.t;
        stub.kill('SIGKILL').then(async () => {
          // The replacement answers every attach with a head and then drops it.
          replacement = await h.startStub({ port: stub.port, stateDir: stub.stateDir, env: { STUB_ATTACH_HEADDIE: '1' } });
        });
      }
    },
  });
  const r = await pending;
  assert.ok(r.error, 'the client was cut');
  const cutAfter = r.chunks.at(-1).t - killedAt;
  const attaches = replacement.events().filter((e) => e.kind === 'attach').length;
  t.diagnostic(`cut ${Math.round(r.elapsedMs)} ms into the request, ${attaches} attaches answered with a dead head`);
  assert.ok(attaches >= 2 && attaches <= 8, `${attaches} attaches: backoff restarted per cycle, never a tight loop`);
  assert.equal(gw.logs.filter((l) => l.event === 'reattach_abort').at(-1).outcome, 'exhausted');
  assert.ok(cutAfter <= 7_000, `last byte ${Math.round(cutAfter)} ms after the kill`);
});

test.describe('re-attach through real sockets (budget scaled to 6 s, heartbeat to 1 s)', { concurrency: true, timeout: 60_000 }, () => {
  // SCALED: PUBLIC_API_GATEWAY_REATTACH_MAX_S 1800 → 6 and
  // V1_GATEWAY_HEARTBEAT_S 15 → 1, so a 30-minute rule runs in seconds.
  const env = { PUBLIC_API_GATEWAY_REATTACH_MAX_S: '6', V1_GATEWAY_HEARTBEAT_S: '1' };

  async function killMidStream(t, { model, downMs, route = 'chat/completions', body, replacementEnv = {} }) {
    const stub = await h.startStub({ env: { STUB_HEARTBEAT_MS: '500' } });
    const gw = await h.startGateway({ orchestratorPort: stub.port, env });
    let replacement = null;
    // Cleanup runs even when an assertion fails; a live child process would
    // otherwise keep the test runner waiting forever.
    t.after(async () => {
      await gw.close();
      await stub.kill();
      await h.sleep(Math.max(0, downMs + 1500 - (performance.now() - started)));
      if (replacement) await replacement.kill();
    });
    const started = performance.now();
    let killedAt = null;
    const payload = body ?? JSON.stringify({ model, stream: true, messages: [] });
    const r = await h.request(gw.port, {
      method: 'POST',
      path: `/v1/${route}`,
      headers: JSON_HEADERS,
      body: payload,
      onChunk: (c) => {
        if (killedAt === null && c.buf.toString().includes('w5 ')) {
          killedAt = c.t;
          stub.kill('SIGKILL').then(async () => {
            await h.sleep(downMs);
            replacement = await h.startStub({ port: stub.port, stateDir: stub.stateDir, env: replacementEnv });
          });
        }
      },
    });
    return { r, gw, stub, killedAt, replacement: () => replacement };
  }

  test('an orchestrator back inside the budget is re-attached, with a heartbeat at least every second meanwhile', async (t) => {
    const { r, gw, killedAt, replacement } = await killMidStream(t, { model: 'stub/tokens=30/interval=50', downMs: 4500 });
    assert.equal(r.error, null);
    assert.equal(h.chatText(r.text), h.expectedText(30));
    const during = r.chunks.filter((c) => c.t > killedAt && c.buf.toString() === ': ping\n\n');
    assert.ok(during.length >= 3, `${during.length} pings while absent`);
    const gaps = r.chunks.map((c, i) => (i ? c.t - r.chunks[i - 1].t : 0));
    assert.ok(Math.max(...gaps) <= 1_300, `max gap ${Math.max(...gaps)}`);
    const ok = gw.logs.find((l) => l.event === 'reattach_ok');
    assert.ok(ok.absent_ms >= 4_000 && ok.absent_ms <= 6_100, `absent ${ok.absent_ms}`);
  });

  test('an orchestrator absent past the budget gets the client cut, at the budget', async (t) => {
    const { r, gw, killedAt, replacement } = await killMidStream(t, { model: 'stub/tokens=30/interval=50', downMs: 9000 });
    assert.ok(r.error, 'the client connection was cut');
    const cutAfter = r.elapsedMs - (killedAt - (r.chunks[0].t - r.firstByteMs));
    assert.ok(cutAfter >= 5_800 && cutAfter <= 7_000, `cut ${cutAfter} ms after the kill`);
    const abort = gw.logs.find((l) => l.event === 'reattach_abort');
    assert.equal(abort.outcome, 'exhausted');
  });

  test('a 404 on re-attach cuts the client at once', async (t) => {
    const { r, gw, killedAt, replacement } = await killMidStream(t, { model: 'stub/tokens=30/interval=50/attach404', downMs: 500 });
    assert.ok(r.error);
    const abort = gw.logs.find((l) => l.event === 'reattach_abort');
    assert.equal(abort.outcome, 'abort');
    assert.equal(abort.status, 404);
    assert.ok(r.elapsedMs - (killedAt - (r.chunks[0].t - r.firstByteMs)) < 4_000);
  });

  test('a store:false run (X-TechSara-Run: none) is cut, never re-POSTed', async (t) => {
    const { r, gw, replacement } = await killMidStream(t, { model: 'stub/tokens=30/interval=50/none', downMs: 500 });
    assert.ok(r.error);
    assert.equal(gw.logs.find((l) => l.event === 'relay_abort').reason, 'not_replayable');
    await h.sleep(1500);
    const runs = replacement().events().filter((e) => e.kind === 'attach' || e.kind === 'launch');
    assert.deepEqual(runs.map((e) => e.kind), ['launch'], 'launched once, by the first stub; never re-POSTed');
  });

  test('an orchestrator that names no run is cut, never sent the generation twice', async (t) => {
    const { r, gw, replacement } = await killMidStream(t, { model: 'stub/tokens=30/interval=50/norun', downMs: 500 });
    assert.ok(r.error);
    assert.equal(gw.logs.find((l) => l.event === 'relay_abort').reason, 'protocol_unknown');
    await h.sleep(1500);
    const runs = replacement().events().filter((e) => e.kind === 'attach' || e.kind === 'launch');
    assert.deepEqual(runs.map((e) => e.kind), ['launch'], 'launched once, by the first stub; never re-POSTed');
  });

  test('a re-attach answered by a rolled-back build that ignores the protocol is cut, never spliced into the stream', async (t) => {
    // Review finding 2026-09-13 (scratchpad p1_rollback.cjs): the fresh
    // generation was relayed after the frames the client held, as one 200.
    const { r, gw, replacement } = await killMidStream(t, {
      model: 'stub/tokens=30/interval=50',
      downMs: 500,
      replacementEnv: { STUB_PRE_PROTOCOL: '1' },
    });
    assert.ok(r.error, 'the client was cut, so the SDK sees an incomplete read');
    const text = h.chatText(r.text);
    assert.equal((text.match(/\bw1 /g) || []).length, 1, `no second generation spliced in: ${text}`);
    assert.ok(h.expectedText(30).startsWith(text), text);
    const abort = gw.logs.find((l) => l.event === 'reattach_abort');
    assert.equal(abort.reason, 'run_mismatch');
    assert.equal(abort.status, 200);
    assert.equal(gw.gateway.gw.attachEvidence, false, 'later relays stop trusting this orchestrator to attach');
    assert.ok(!gw.logs.some((l) => l.event === 'reattach_ok'));
  });

  test('a partially relayed JSON object is cut, never re-POSTed', async (t) => {
    const stub = await h.startStub({ env: { STUB_COMMIT_MS: '300', STUB_HEARTBEAT_MS: '200' } });
    const gw = await h.startGateway({ orchestratorPort: stub.port, env: { ...env, V1_GATEWAY_UPSTREAM_SILENCE_S: '2' } });
    t.after(async () => {
      await gw.close();
      await stub.kill();
    });
    const r = await h.request(gw.port, {
      method: 'POST',
      path: '/v1/chat/completions',
      headers: JSON_HEADERS,
      body: JSON.stringify({ model: 'stub/tokens=3/interval=2000/partial', messages: [] }),
    });
    assert.ok(r.error);
    assert.match(r.text, /\{"id": $/);
    assert.equal(gw.logs.find((l) => l.event === 'relay_abort').reason, 'partial_json');
    assert.equal(stub.events().filter((e) => e.kind === 'attach').length, 0);
  });

  test('a whitespace-only JSON body is re-attached and completed', async (t) => {
    const stub = await h.startStub({ env: { STUB_COMMIT_MS: '300', STUB_HEARTBEAT_MS: '200' } });
    const gw = await h.startGateway({ orchestratorPort: stub.port, env });
    let replacement = null;
    t.after(async () => {
      await gw.close();
      await stub.kill();
      if (replacement) await replacement.kill();
    });
    const pending = h.request(gw.port, {
      method: 'POST',
      path: '/v1/responses',
      headers: JSON_HEADERS,
      body: JSON.stringify({ model: 'stub/tokens=4/interval=1000', input: 'hi' }),
    });
    await h.sleep(1500);
    await stub.kill('SIGKILL');
    await h.sleep(2000);
    replacement = await h.startStub({ port: stub.port, stateDir: stub.stateDir, env: { STUB_COMMIT_MS: '300', STUB_HEARTBEAT_MS: '200' } });
    const r = await pending;
    assert.equal(r.error, null);
    assert.equal(JSON.parse(r.text).output[0].content[0].text, h.expectedText(4));
    assert.equal(replacement.events().filter((e) => e.kind === 'attach').length, 1);
  });
});

test('an audio job frame whose ts-seq was lost in a crash reaches the client exactly once', { timeout: 30_000 }, async (t) => {
  // Review finding 2026-09-13 (scratchpad p7_job_dup.cjs): frames were held
  // for their marker only on response runs, so a job run delivered d2 twice.
  const http = require('node:http');
  const frame = (k) => `event: transcript.text.delta\ndata: ${JSON.stringify({ type: 'transcript.text.delta', delta: `d${k} ` })}\n\n`;
  const seen = [];
  const server = http.createServer((req, res) => {
    req.resume();
    req.on('end', () => {
      seen.push({ attachJob: req.headers['x-techsara-attach-job'] ?? null, resumeAfter: req.headers['x-techsara-resume-after'] ?? null });
      res.writeHead(200, { 'content-type': 'text/event-stream', 'x-techsara-run': 'job:k1' });
      if (seen.length === 1) {
        res.write(`${frame(1)}: ts-seq=1\n\n`);
        setTimeout(() => {
          res.write(frame(2)); // its marker never follows
          setTimeout(() => res.socket.destroy(), 50);
        }, 50);
        return;
      }
      let out = '';
      for (let k = Number(req.headers['x-techsara-resume-after'] || 0) + 1; k <= 3; k += 1) out += `${frame(k)}: ts-seq=${k}\n\n`;
      res.end(`${out}event: transcript.text.done\ndata: {"type":"transcript.text.done","text":"d1 d2 d3 "}\n\n`);
    });
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  const gw = await h.startGateway({ orchestratorPort: server.address().port, env: { V1_GATEWAY_HEARTBEAT_S: '1' } });
  t.after(async () => {
    await gw.close();
    server.closeAllConnections();
    server.close();
  });
  const body = Buffer.alloc(1000, 0x78);
  const r = await h.request(gw.port, {
    method: 'POST',
    path: '/v1/audio/transcriptions',
    headers: { 'content-type': 'multipart/form-data; boundary=b', 'content-length': String(body.length), authorization: 'Bearer sk-test' },
    body,
  });
  const deltas = h.sseDataPayloads(r.text).map((d) => JSON.parse(d)).filter((o) => o.type === 'transcript.text.delta').map((o) => o.delta);
  assert.deepEqual(deltas, ['d1 ', 'd2 ', 'd3 ']);
  assert.deepEqual(seen, [{ attachJob: null, resumeAfter: null }, { attachJob: 'k1', resumeAfter: '1' }]);
});
