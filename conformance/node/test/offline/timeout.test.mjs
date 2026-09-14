// "Timeout disabled" with openai-node, measured. Two timers can end a long
// TechSara request from the client side, and turning off one leaves the other:
//
//   1. the SDK's own timer (`timeout`, default 600,000 ms), a setTimeout;
//   2. undici's headersTimeout / bodyTimeout inside Node's fetch, 300 s each by
//      default. Measured once on 2026-09-13 and not re-measured per run (it takes
//      five idle minutes): Node v22.23.2 (bundled undici 6.28.0), openai-node
//      7.15.0 with timeout 2**31-1 against a server that never answers ->
//      APIConnectionTimeoutError after 300.7 s, cause UND_ERR_HEADERS_TIMEOUT.
//
// Scaled-down timers against a local stub show the mechanism in seconds.
import { describe, it, after } from 'node:test';
import assert from 'node:assert/strict';
import OpenAI from 'openai';
import { Agent, fetch as undiciFetch } from 'undici';
import { setTimeout as sleep } from 'node:timers/promises';
import { startStub, sendJSON, MODEL_LIST } from '../../lib/stub.mjs';
import { MAX_TIMER_MS, noTimeoutOptions } from '../../lib/client.mjs';

const stubs = [];
after(async () => Promise.all(stubs.map((s) => s.close())));

async function slowStub(delayMs) {
  const s = await startStub(async (req, res) => {
    await sleep(delayMs);
    sendJSON(res, MODEL_LIST);
  });
  stubs.push(s);
  return s;
}

describe('Disabling the client timeout in openai-node (offline)', () => {
  it('a client-level timeout of Infinity aborts the request at once, because Node coerces an over-large timer to 1 ms', async () => {
    const s = await slowStub(400);
    const warnings = [];
    const onWarning = (w) => warnings.push(w.name);
    process.on('warning', onWarning);
    const c = new OpenAI({ baseURL: s.baseURL, apiKey: 'x', timeout: Infinity, maxRetries: 0 });
    const started = Date.now();
    const err = await c.models.list().then(() => null, (e) => e);
    await sleep(10);
    process.off('warning', onWarning);
    assert.ok(err instanceof OpenAI.APIConnectionTimeoutError, `got ${err?.constructor?.name}: ${err?.message}`);
    assert.ok(Date.now() - started < 300, 'aborted before the stub answered');
    assert.ok(warnings.includes('TimeoutOverflowWarning'), `warnings: ${warnings.join(',')}`);
  });

  it('a per-request timeout of Infinity is refused by the SDK before any request is sent', async () => {
    const s = await slowStub(0);
    const c = new OpenAI({ baseURL: s.baseURL, apiKey: 'x', maxRetries: 0 });
    await assert.rejects(c.models.list({ timeout: Infinity }), /timeout must be an integer/);
    assert.equal(s.requests.length, 0);
  });

  it('timeout: 2**31 - 1 milliseconds (24.8 days) is the largest value that works, and it does not abort', async () => {
    const s = await slowStub(300);
    const c = new OpenAI({ baseURL: s.baseURL, apiKey: 'x', timeout: MAX_TIMER_MS, maxRetries: 0 });
    const page = await c.models.list();
    assert.equal(page.data[0].id, 'techsara-35b');
    const c2 = new OpenAI({ baseURL: s.baseURL, apiKey: 'x', timeout: MAX_TIMER_MS + 1, maxRetries: 0 });
    await assert.rejects(c2.models.list(), OpenAI.APIConnectionTimeoutError);
  });

  it("undici's headersTimeout ends a slow synchronous response even when the SDK timer is at its maximum", async () => {
    const s = await slowStub(1500);
    const c = new OpenAI({
      baseURL: s.baseURL,
      apiKey: 'x',
      timeout: MAX_TIMER_MS,
      maxRetries: 0,
      fetch: undiciFetch,
      fetchOptions: { dispatcher: new Agent({ headersTimeout: 500 }) },
    });
    const err = await c.models.list().then(() => null, (e) => e);
    assert.ok(err instanceof OpenAI.APIConnectionTimeoutError, `got ${err?.constructor?.name}: ${err?.message}`);
    assert.match(err.message, /timed out waiting for response headers/);
  });

  it('the recommended no-timeout client (SDK timer at maximum, undici headersTimeout and bodyTimeout 0) survives the same slow response', async () => {
    const s = await slowStub(1500);
    const c = new OpenAI({ baseURL: s.baseURL, apiKey: 'x', maxRetries: 0, ...noTimeoutOptions() });
    const page = await c.models.list();
    assert.equal(page.data[0].id, 'techsara-35b');
  });
});

describe('Why a stream must heartbeat (offline)', () => {
  async function sseStub({ heartbeatEveryMs, silentForMs }) {
    const s = await startStub(async (req, res) => {
      res.writeHead(200, { 'content-type': 'text/event-stream', 'x-request-id': 'req_stub' });
      const send = (event, data) => res.write(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`);
      send('response.created', { type: 'response.created', sequence_number: 1, response: { id: 'resp_stub', status: 'queued' } });
      const until = Date.now() + silentForMs;
      while (Date.now() < until) {
        if (heartbeatEveryMs) {
          await sleep(heartbeatEveryMs);
          res.write(': ping\n\n');
        } else {
          await sleep(silentForMs);
        }
      }
      send('response.completed', {
        type: 'response.completed',
        sequence_number: 2,
        response: { id: 'resp_stub', status: 'completed', usage: { input_tokens: 1, output_tokens: 1, total_tokens: 2 } },
      });
      res.end();
    });
    stubs.push(s);
    return s;
  }

  function streamClient(baseURL, bodyTimeout) {
    return new OpenAI({
      baseURL,
      apiKey: 'x',
      maxRetries: 0,
      timeout: MAX_TIMER_MS,
      fetch: undiciFetch,
      fetchOptions: { dispatcher: new Agent({ bodyTimeout }) },
    });
  }

  it('a heartbeat comment more often than the body timeout keeps a quiet stream alive, and the SDK never surfaces the comment', async () => {
    const s = await sseStub({ heartbeatEveryMs: 200, silentForMs: 1500 });
    const stream = await streamClient(s.baseURL, 600).responses.create({ model: 'techsara-35b', input: 'x', stream: true });
    const types = [];
    for await (const ev of stream) types.push(ev.type);
    assert.deepEqual(types, ['response.created', 'response.completed']);
  });

  it('the same quiet stream without heartbeats is cut by the body timeout', async () => {
    const s = await sseStub({ heartbeatEveryMs: 0, silentForMs: 1500 });
    const stream = await streamClient(s.baseURL, 600).responses.create({ model: 'techsara-35b', input: 'x', stream: true });
    const err = await (async () => {
      for await (const _ of stream) {
        /* drain */
      }
    })().then(() => null, (e) => e);
    assert.ok(err, 'expected the stream to fail');
    assert.match(String(err?.cause?.code ?? err?.code ?? err), /UND_ERR_BODY_TIMEOUT|terminated|timeout/i);
  });
});

// ---------------------------------------------------------------------------
// The no-timeout release, as openai-node sees it (2026-09-13, design revision 2).
// Each test is a stub speaking what CONTRACT-3 §10 and §13 promise, so what the
// documentation tells a caller to do is proven against the SDK itself.

function committedJsonStub({ spacesEveryMs, spaces, body, status = 200 }) {
  return startStub(async (req, res) => {
    res.writeHead(status, { 'content-type': 'application/json', 'cache-control': 'no-store, no-transform', 'x-request-id': 'req_stub' });
    res.write(' ');
    for (let i = 1; i < spaces; i += 1) {
      await sleep(spacesEveryMs);
      res.write(' ');
    }
    res.end(JSON.stringify(body));
  });
}

describe('The no-timeout release, through openai-node (offline)', () => {
  it('the documented client parses a committed synchronous body that starts with spaces', async () => {
    const s = await committedJsonStub({ spacesEveryMs: 100, spaces: 8, body: responseLike('completed', 'hello') });
    stubs.push(s);
    const c = new OpenAI({ baseURL: s.baseURL, apiKey: 'x', timeout: MAX_TIMER_MS, maxRetries: 5 });
    const r = await c.responses.create({ model: 'techsara-35b', input: 'x' });
    assert.equal(r.status, 'completed');
    assert.equal(r.output_text, 'hello');
  });

  it('a 200 whose body is a failed response does not throw: the caller must read status, as the docs say', async () => {
    const failed = { ...responseLike('failed', 'partial'), error: { code: 'model_unavailable', message: 'The model is not available at the moment.' } };
    const s = await committedJsonStub({ spacesEveryMs: 50, spaces: 3, body: failed });
    stubs.push(s);
    const c = new OpenAI({ baseURL: s.baseURL, apiKey: 'x', timeout: MAX_TIMER_MS, maxRetries: 0 });
    const r = await c.responses.create({ model: 'techsara-35b', input: 'x' });
    assert.equal(r.status, 'failed');
    assert.equal(r.error.code, 'model_unavailable');
  });

  it('a timeout covers the whole non-streamed call, so bytes arriving every 100 ms do not save a 1 s timeout — 2147483647 does', async () => {
    // WHY the docs say "keep the timeout at the maximum": from 7.5 the SDK's
    // timer is a deadline for the call, not a limit on silence.
    const s = await committedJsonStub({ spacesEveryMs: 100, spaces: 20, body: responseLike('completed', 'late') });
    stubs.push(s);
    const short = new OpenAI({ baseURL: s.baseURL, apiKey: 'x', timeout: 1000, maxRetries: 0 });
    const started = Date.now();
    await assert.rejects(short.responses.create({ model: 'techsara-35b', input: 'x' }), OpenAI.APIConnectionTimeoutError);
    assert.ok(Date.now() - started < 1800, 'cut at its deadline, although bytes kept arriving');
    const long = new OpenAI({ baseURL: s.baseURL, apiKey: 'x', timeout: MAX_TIMER_MS, maxRetries: 0 });
    const r = await long.responses.create({ model: 'techsara-35b', input: 'x' });
    assert.equal(r.output_text, 'late');
  });

  it('the SDK marks its retry with x-stainless-retry-count 1 and sends the identical body, which is what implicit attach matches', async () => {
    const s = await startStub((req, res, n) => {
      if (n === 0) {
        req.socket.destroy();
        return;
      }
      sendJSON(res, responseLike('completed', 'attached'));
    });
    stubs.push(s);
    const c = new OpenAI({ baseURL: s.baseURL, apiKey: 'x', timeout: MAX_TIMER_MS, maxRetries: 5 });
    const r = await c.responses.create({ model: 'techsara-35b', input: 'the same body' });
    assert.equal(r.output_text, 'attached');
    assert.equal(s.requests.length, 2);
    assert.equal(s.requests[0].headers['x-stainless-retry-count'], '0');
    assert.equal(s.requests[1].headers['x-stainless-retry-count'], '1');
    assert.equal(s.requests[1].body, s.requests[0].body);
  });

  it('x-should-retry: false on a 503 stops the SDK retrying, however many retries it has', async () => {
    const s = await startStub((req, res) => {
      res.writeHead(503, { 'content-type': 'application/json', 'retry-after': '1', 'x-should-retry': 'false' });
      res.end(JSON.stringify({ error: { message: 'x', type: 'service_unavailable_error', code: 'model_unavailable', param: null, request_id: null } }));
    });
    stubs.push(s);
    const c = new OpenAI({ baseURL: s.baseURL, apiKey: 'x', timeout: MAX_TIMER_MS, maxRetries: 5 });
    await assert.rejects(c.responses.create({ model: 'techsara-35b', input: 'x' }), (e) => e.status === 503);
    assert.equal(s.requests.length, 1);
  });

  it('responses.retrieve with stream and starting_after sends both query parameters and iterates the replayed events', async () => {
    const s = await startStub((req, res) => {
      res.writeHead(200, { 'content-type': 'text/event-stream' });
      const send = (type, extra) => res.write(`event: ${type}\ndata: ${JSON.stringify({ type, ...extra })}\n\n`);
      res.write(': ping\n\n');
      send('response.output_text.delta', { sequence_number: 4, item_id: 'msg_1', output_index: 0, content_index: 0, delta: 'lo' });
      send('response.completed', { sequence_number: 5, response: responseLike('completed', 'hello') });
      res.end();
    });
    stubs.push(s);
    const c = new OpenAI({ baseURL: s.baseURL, apiKey: 'x', timeout: MAX_TIMER_MS, maxRetries: 0 });
    const stream = await c.responses.retrieve('resp_stub', { stream: true, starting_after: 3 });
    const seen = [];
    for await (const event of stream) seen.push(event.sequence_number);
    assert.deepEqual(seen, [4, 5]);
    const url = new URL(s.requests[0].url, 'http://x');
    assert.equal(url.pathname, '/v1/responses/resp_stub');
    assert.equal(url.searchParams.get('stream'), 'true');
    assert.equal(url.searchParams.get('starting_after'), '3');
  });
});

function responseLike(status, text) {
  return {
    id: 'resp_stub',
    object: 'response',
    created_at: 1789200000,
    status,
    model: 'techsara-35b',
    output: [{ type: 'message', id: 'msg_1', role: 'assistant', status: 'completed', content: [{ type: 'output_text', text, annotations: [] }] }],
    max_output_tokens: 8192,
    incomplete_details: null,
    usage: status === 'completed' ? { input_tokens: 3, output_tokens: 1, total_tokens: 4 } : null,
  };
}
