'use strict';
/**
 * The stub engine's proof: every route is checked against the shape the
 * ORCHESTRATOR reads, with the source line that reads it named in the test.
 * A stub whose wire format is wrong fails the e2e stage in a way that looks
 * like a product defect, so these run first in the job, before anything is
 * built.
 */

const assert = require('node:assert');
const { test, before, after } = require('node:test');

const engine = require('../engine');

let server;
let base;

before(async () => {
  server = engine.createServer();
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  base = `http://127.0.0.1:${server.address().port}`;
});

after(async () => {
  await new Promise((resolve) => server.close(resolve));
});

async function post(path, body) {
  return fetch(`${base}${path}`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body),
  });
}

test('GET /health answers at the ROOT, where app/health.py:46 probes it', async () => {
  const res = await fetch(`${base}/health`);
  assert.equal(res.status, 200);
  assert.equal((await res.json()).status, 'ok');
});

test('GET /v1/models publishes max_model_len, which app/health.py:715 reads', async () => {
  const res = await fetch(`${base}/v1/models`);
  assert.equal(res.status, 200);
  const payload = await res.json();
  assert.equal(payload.object, 'list');
  assert.ok(payload.data.length >= 1);
  for (const model of payload.data) {
    assert.equal(model.object, 'model');
    assert.equal(typeof model.id, 'string');
    assert.equal(model.max_model_len, engine.MAX_MODEL_LEN);
  }
});

test('POST /v1/chat/completions (not streaming) is a complete chat.completion', async () => {
  const res = await post('/v1/chat/completions', { model: 'stub-main', messages: [{ role: 'user', content: 'hello' }] });
  assert.equal(res.status, 200);
  const payload = await res.json();
  assert.equal(payload.object, 'chat.completion');
  assert.equal(payload.choices[0].message.role, 'assistant');
  assert.ok(payload.choices[0].message.content.length > 0);
  assert.equal(payload.choices[0].finish_reason, 'stop');
  assert.ok(payload.usage.prompt_tokens > 0);
  assert.ok(payload.usage.completion_tokens > 0);
  assert.equal(payload.usage.total_tokens, payload.usage.prompt_tokens + payload.usage.completion_tokens);
});

test('a streaming chat completion is a well-formed SSE sequence', async () => {
  const res = await post('/v1/chat/completions', {
    model: 'stub-main',
    stream: true,
    stream_options: { include_usage: true },
    messages: [{ role: 'user', content: 'stream please' }],
  });
  assert.equal(res.status, 200);
  assert.match(res.headers.get('content-type'), /text\/event-stream/);
  const body = await res.text();

  // Every frame is `data: <json>` separated by a blank line, and the last one
  // is the literal [DONE] sentinel an OpenAI client stops on.
  const frames = body.split('\n\n').filter((f) => f.trim());
  assert.ok(frames.length >= 3, body);
  for (const frame of frames) assert.match(frame, /^data: /);
  assert.equal(frames[frames.length - 1], 'data: [DONE]');

  const payloads = frames.slice(0, -1).map((f) => JSON.parse(f.slice('data: '.length)));
  assert.equal(payloads[0].object, 'chat.completion.chunk');
  assert.equal(payloads[0].choices[0].delta.role, 'assistant');
  const finished = payloads.filter((p) => p.choices.length && p.choices[0].finish_reason === 'stop');
  assert.equal(finished.length, 1, 'exactly one chunk carries finish_reason stop');
  const text = payloads.map((p) => (p.choices[0] && p.choices[0].delta.content) || '').join('');
  assert.ok(text.includes('stub engine'), text);
  const usage = payloads.filter((p) => p.usage);
  assert.equal(usage.length, 1, 'include_usage asks for exactly one usage frame');
  assert.ok(usage[0].usage.completion_tokens > 0);
  // Every chunk of one response shares one id, as a real client assumes.
  assert.equal(new Set(payloads.map((p) => p.id)).size, 1);
});

test('JSON mode returns PARSEABLE JSON, because json_completion parses it', async () => {
  const res = await post('/v1/chat/completions', {
    model: 'stub-main',
    response_format: { type: 'json_object' },
    messages: [{ role: 'user', content: 'give me json' }],
  });
  const payload = await res.json();
  assert.doesNotThrow(() => JSON.parse(payload.choices[0].message.content));
});

test('POST /v1/embeddings returns one unit vector of the configured width per input', async () => {
  const res = await post('/v1/embeddings', { model: 'stub-embed', input: ['alpha', 'beta', 'gamma'] });
  assert.equal(res.status, 200);
  const payload = await res.json();
  assert.equal(payload.object, 'list');
  assert.equal(payload.data.length, 3);
  payload.data.forEach((item, i) => {
    assert.equal(item.object, 'embedding');
    assert.equal(item.index, i);
    assert.equal(item.embedding.length, engine.EMBED_DIM);
    const norm = Math.sqrt(item.embedding.reduce((a, v) => a + v * v, 0));
    assert.ok(Math.abs(norm - 1) < 1e-3, `vector ${i} is not a unit vector (${norm})`);
  });
  // Different inputs must not collapse to one vector, and the same input must
  // give the same vector: a retrieval check asserts on both.
  assert.notDeepEqual(payload.data[0].embedding, payload.data[1].embedding);
  const again = await (await post('/v1/embeddings', { input: 'alpha' })).json();
  assert.deepEqual(again.data[0].embedding, payload.data[0].embedding);
});

test('a bare string input is accepted, not only a list', async () => {
  const payload = await (await post('/v1/embeddings', { input: 'one string' })).json();
  assert.equal(payload.data.length, 1);
});

test('POST /score answers the reranker contract app/rerank.py:248 parses', async () => {
  const documents = ['red apples', 'blue whales', 'green fields'];
  const res = await post('/score', { text_1: 'what colour are apples', text_2: documents });
  assert.equal(res.status, 200);
  const payload = await res.json();
  assert.ok(Array.isArray(payload.data));
  assert.equal(payload.data.length, documents.length);
  // parse_scores refuses incomplete indices and non-numeric scores.
  assert.deepEqual(payload.data.map((d) => d.index).sort((a, b) => a - b), [0, 1, 2]);
  for (const item of payload.data) {
    assert.equal(typeof item.score, 'number');
    assert.ok(item.score >= 0 && item.score <= 1, String(item.score));
  }
});

test('the reranker CANARY passes: positive >= 0.9, negative <= 0.1, margin >= 0.7', async () => {
  // The fixed triple from app/rerank.py:81-86. A stub that fails this trips
  // the breaker and the orchestrator declares its reranker broken.
  const query = 'At what temperature does water boil at sea level?';
  const positive = 'At sea level, pure water boils at 100 degrees Celsius (212 degrees Fahrenheit).';
  const negative = 'The museum opens at nine in the morning and closes at five on weekdays.';
  const payload = await (await post('/score', { text_1: query, text_2: [positive, negative] })).json();
  const [pos, neg] = payload.data.map((d) => d.score);
  assert.ok(pos >= 0.9, `positive scored ${pos}`);
  assert.ok(neg <= 0.1, `negative scored ${neg}`);
  assert.ok(pos - neg >= 0.7, `margin ${pos - neg}`);
});

test('six documents are never DEGENERATE (band > 0.02), so scores are not thrown away', async () => {
  // app/rerank.py:277 discards a whole scoring pass when six or more scores
  // sit inside DEGENERATE_BAND. Both a uniformly irrelevant set and a mixed
  // set have to clear it.
  const irrelevant = ['aaa', 'bbb', 'ccc', 'ddd', 'eee', 'fff'];
  const flat = await (await post('/score', { text_1: 'nothing in common here', text_2: irrelevant })).json();
  const flatScores = flat.data.map((d) => d.score);
  assert.ok(Math.max(...flatScores) - Math.min(...flatScores) > 0.02, JSON.stringify(flatScores));

  const mixed = ['boiling water at sea level', 'bbb', 'water temperature', 'ddd', 'eee', 'fff'];
  const spread = await (await post('/score', { text_1: 'water boiling temperature at sea level', text_2: mixed })).json();
  const scores = spread.data.map((d) => d.score);
  assert.ok(Math.max(...scores) - Math.min(...scores) > 0.5, JSON.stringify(scores));
  assert.ok(scores[0] > scores[1], 'the relevant document must outrank the irrelevant one');
});

test('POST /tokenize returns count and max_model_len, which app/context.py:403 reads', async () => {
  const res = await post('/tokenize', { model: 'stub-main', messages: [{ role: 'user', content: 'a'.repeat(400) }] });
  assert.equal(res.status, 200);
  const payload = await res.json();
  assert.equal(typeof payload.count, 'number');
  assert.ok(payload.count > 0);
  assert.equal(payload.max_model_len, engine.MAX_MODEL_LEN);
});

test('GET /metrics exposes vllm:generation_tokens_total and it ADVANCES', async () => {
  const read = async () => {
    const text = await (await fetch(`${base}/metrics`)).text();
    const m = /^vllm:generation_tokens_total\{[^}]*\}\s+(\d+)$/m.exec(text);
    assert.ok(m, text);
    return Number(m[1]);
  };
  const before0 = await read();
  await post('/v1/chat/completions', { messages: [{ role: 'user', content: 'count me' }] });
  assert.ok((await read()) > before0, 'the counter did not move after a completion');
});

test('an unimplemented route is a clear 404, not a hang', async () => {
  const res = await post('/v1/audio/transcriptions', {});
  assert.equal(res.status, 404);
  const payload = await res.json();
  assert.match(payload.error.message, /does not implement/);
});

test('a body that is not JSON is a 400 that names the problem', async () => {
  const res = await fetch(`${base}/v1/chat/completions`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: '{not json',
  });
  assert.equal(res.status, 400);
  assert.match((await res.json()).error.message, /not JSON/);
});

test('the stub never echoes an Authorization header back to the caller', async () => {
  const res = await fetch(`${base}/v1/models`, { headers: { authorization: 'Bearer tsk_live_01J_secret' } });
  const text = await res.text();
  assert.ok(!text.includes('tsk_live'), text);
});

test('GET /metrics carries a marker that says the `vllm:` series are synthetic', async () => {
  // WHY (2026-09-27): every other surface of this stub names itself —
  // /health returns engine "techsara-ci-stub", /version the same, the models
  // are `stub-*` and owned_by techsara-ci-stub, and a chat id is
  // `chatcmpl-stub-…`. /metrics was the one exception: it emitted
  // `vllm:generation_tokens_total` and `vllm:prompt_tokens_total` under exactly
  // the names a real engine uses, so a scrape, a pasted sample or a dashboard
  // could not tell the two apart. The marker is a series of its own, so
  // nothing that parses the `vllm:` names is affected.
  const text = await (await fetch(`${base}/metrics`)).text();
  assert.match(text, /^techsara_ci_stub_engine_info\{engine="techsara-ci-stub",real_model="none"\} 1$/m, text);
  assert.match(text, /# HELP techsara_ci_stub_engine_info .*No model/, text);
});

test('GET /state answers MONITORING_UNKNOWN, the one state that changes no decision', async () => {
  // ci.env points ENGINE_CONTROLLER_URL here so CI never resolves `vllm`, the
  // PRODUCTION container name app/config.py:2182 defaults to. The document
  // must parse under app/engine_state.py's strict rules — `state` one of the
  // nine names and `state_code` agreeing with it — and MONITORING_UNKNOWN is
  // in neither SERVING (engine_state.py:87) nor OPENS_BREAKER (:81), so the
  // orchestrator reads it as "cannot observe" and opens no breaker, queues no
  // generation and closes no admission lane on it.
  const res = await fetch(`${base}/state`);
  assert.equal(res.status, 200);
  const doc = await res.json();
  assert.equal(doc.state, 'MONITORING_UNKNOWN');
  assert.equal(doc.state_code, 0);
  assert.equal(doc.primary_ready, false);
  assert.ok(typeof doc.generated_at === 'number' && doc.generated_at > 1e9, `generated_at: ${doc.generated_at}`);
  assert.match(String(doc.reason), /stub/i);
});
