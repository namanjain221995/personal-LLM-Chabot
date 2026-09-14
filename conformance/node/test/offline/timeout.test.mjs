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
