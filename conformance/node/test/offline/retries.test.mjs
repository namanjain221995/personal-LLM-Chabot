// openai-node's retry and error semantics, measured against a local stub that
// answers with the CONTRACT-3 §9 envelope. These pin what a TechSara caller using
// the SDK actually gets when the API says 503 + Retry-After, 409, 401 or 403 —
// facts the live suite cannot provoke safely (a real 503 "at capacity" needs a
// saturated engine).
import { describe, it, after } from 'node:test';
import assert from 'node:assert/strict';
import OpenAI from 'openai';
import { startStub, sendError, sendJSON, MODEL_LIST, responseObject } from '../../lib/stub.mjs';

const stubs = [];
after(async () => Promise.all(stubs.map((s) => s.close())));

async function stub(handler) {
  const s = await startStub(handler);
  stubs.push(s);
  return s;
}

function client(baseURL, extra = {}) {
  return new OpenAI({ baseURL, apiKey: 'tsk_test_stub', ...extra });
}

describe('Retry-After on a 503 (offline, openai-node)', () => {
  it('a 503 model_unavailable with Retry-After: 2 is retried after about two seconds, and the envelope reaches err.code', async () => {
    const s = await stub((req, res) => sendError(res, 503, 'model_unavailable', 'server_error', { retryAfter: 2 }));
    const c = client(s.baseURL, { maxRetries: 1 });
    const err = await c.models.list().then(
      () => assert.fail('expected an error'),
      (e) => e,
    );
    assert.ok(err instanceof OpenAI.InternalServerError, `got ${err?.constructor?.name}`);
    assert.equal(err.status, 503);
    assert.equal(err.code, 'model_unavailable');
    assert.equal(err.type, 'server_error');
    assert.equal(err.error.request_id, 'req_stub0000000000000000000000');
    assert.equal(err.requestID, 'req_stub0000000000000000000000');
    assert.equal(err.headers.get('retry-after'), '2');
    assert.equal(s.requests.length, 2);
    const gap = s.requests[1].at - s.requests[0].at;
    assert.ok(gap >= 1900 && gap < 4000, `retry gap ${gap} ms`);
  });

  it('a 503 that clears within the retry budget returns the answer and surfaces no error', async () => {
    const s = await stub((req, res, n) =>
      n === 0 ? sendError(res, 503, 'model_recovering', 'server_error', { retryAfter: 1 }) : sendJSON(res, MODEL_LIST),
    );
    const page = await client(s.baseURL).models.list();
    assert.equal(page.data[0].id, 'techsara-35b');
    assert.equal(s.requests.length, 2);
  });

  it('a Retry-After above 60 seconds is ignored and the SDK falls back to its own 0.5 to 8 second backoff', async () => {
    // WHY this matters (2026-09-13): a planned-restart recovery measured 172-188 s.
    // A server that answered `Retry-After: 180` would NOT be obeyed by openai-node;
    // the SDK retries within a second and gives up after maxRetries (default 2).
    const s = await stub((req, res) => sendError(res, 503, 'model_recovering', 'server_error', { retryAfter: 90 }));
    const c = client(s.baseURL, { maxRetries: 1 });
    await assert.rejects(c.models.list(), (e) => e.status === 503);
    assert.equal(s.requests.length, 2);
    const gap = s.requests[1].at - s.requests[0].at;
    assert.ok(gap < 1500, `retry gap ${gap} ms should be the SDK backoff, not 90 s`);
  });

  it('the default client gives up on a persistent 503 after two retries', async () => {
    const s = await stub((req, res) => sendError(res, 503, 'model_unavailable', 'server_error', { retryAfter: 1 }));
    await assert.rejects(client(s.baseURL).models.list(), (e) => e.status === 503);
    assert.equal(s.requests.length, 3);
  });
});

describe('Which statuses the SDK retries (offline, openai-node)', () => {
  it('a 409 idempotency_conflict is retried by default, so it costs three requests unless maxRetries is 0', async () => {
    const s = await stub((req, res) => sendError(res, 409, 'idempotency_conflict', 'invalid_request_error', { param: 'Idempotency-Key' }));
    await assert.rejects(
      client(s.baseURL).responses.create({ model: 'techsara-35b', input: 'x' }),
      (e) => e instanceof OpenAI.ConflictError && e.code === 'idempotency_conflict',
    );
    assert.equal(s.requests.length, 3);

    const s2 = await stub((req, res) => sendError(res, 409, 'idempotency_conflict', 'invalid_request_error'));
    await assert.rejects(client(s2.baseURL, { maxRetries: 0 }).responses.create({ model: 'techsara-35b', input: 'x' }));
    assert.equal(s2.requests.length, 1);
  });

  it('an x-should-retry: false header stops the SDK retrying a 409', async () => {
    const s = await stub((req, res) => {
      res.setHeader('x-should-retry', 'false');
      sendError(res, 409, 'idempotency_conflict', 'invalid_request_error');
    });
    await assert.rejects(client(s.baseURL).responses.create({ model: 'techsara-35b', input: 'x' }));
    assert.equal(s.requests.length, 1);
  });

  it('a 504 timeout on POST /v1/responses is retried by default, and an Idempotency-Key passed in headers goes out unchanged on every attempt', async () => {
    // WHY pinned (2026-09-13, review): CONTRACT-3 §8.3 answers a synchronous
    // generation cut by its wall clock with 504 `timeout`. openai-node retries
    // every status >= 500 on POST, so without an Idempotency-Key each retry
    // starts a NEW generation — hours long for a large max_output_tokens. Any
    // "raise maxRetries" advice must come with the header.
    const s = await stub((req, res) => sendError(res, 504, 'timeout', 'server_error'));
    await assert.rejects(
      client(s.baseURL).responses.create({ model: 'techsara-35b', input: 'x' }, { headers: { 'Idempotency-Key': 'same-key' } }),
      (e) => e.status === 504 && e.code === 'timeout',
    );
    assert.equal(s.requests.length, 3, 'one request and two retries');
    assert.ok(s.requests.every((r) => r.method === 'POST'));
    assert.deepEqual(
      s.requests.map((r) => r.headers['idempotency-key']),
      ['same-key', 'same-key', 'same-key'],
    );

    const bare = await stub((req, res) => sendError(res, 504, 'timeout', 'server_error'));
    await assert.rejects(client(bare.baseURL).responses.create({ model: 'techsara-35b', input: 'x' }));
    assert.equal(bare.requests.length, 3);
    assert.ok(bare.requests.every((r) => r.headers['idempotency-key'] === undefined), 'no key unless the caller passes one');
  });

  for (const [status, code, type, Klass] of [
    [401, 'invalid_api_key', 'authentication_error', OpenAI.AuthenticationError],
    [403, 'insufficient_scope', 'permission_error', OpenAI.PermissionDeniedError],
    [404, 'model_not_found', 'invalid_request_error', OpenAI.NotFoundError],
    [400, 'invalid_request_error', 'invalid_request_error', OpenAI.BadRequestError],
  ]) {
    it(`a ${status} ${code} is not retried and maps to ${Klass.name}`, async () => {
      const s = await stub((req, res) => sendError(res, status, code, type));
      const err = await client(s.baseURL)
        .models.list()
        .then(() => assert.fail('expected an error'), (e) => e);
      assert.ok(err instanceof Klass, `got ${err?.constructor?.name}`);
      assert.equal(err.code, code);
      assert.equal(s.requests.length, 1);
    });
  }
});

describe('Idempotency-Key from openai-node (offline)', () => {
  it('the SDK sends no Idempotency-Key by default, ignores the idempotencyKey request option, and sends one only through headers', async () => {
    const s = await stub((req, res) => sendJSON(res, responseObject('completed', 'ok')));
    const c = client(s.baseURL, { maxRetries: 0 });
    await c.responses.create({ model: 'techsara-35b', input: 'x' });
    await c.responses.create({ model: 'techsara-35b', input: 'x' }, { idempotencyKey: 'from-option' });
    await c.responses.create({ model: 'techsara-35b', input: 'x' }, { headers: { 'Idempotency-Key': 'from-headers' } });
    assert.equal(s.requests[0].headers['idempotency-key'], undefined);
    // WHY pinned (2026-09-13): `idempotencyKey` is a documented request option in
    // the SDK's types, but OpenAI's client never sets `idempotencyHeader`, so the
    // option is silently dropped. TechSara docs must show `headers`.
    assert.equal(s.requests[1].headers['idempotency-key'], undefined);
    assert.equal(s.requests[2].headers['idempotency-key'], 'from-headers');
  });

  it('an embeddings call carries no Idempotency-Key, so the sidecar rule "Idempotency-Key is 400" does not break the SDK', async () => {
    const s = await stub((req, res) =>
      sendJSON(res, { object: 'list', model: 'techsara-embed', data: [{ object: 'embedding', index: 0, embedding: [0.1] }], usage: null }),
    );
    await client(s.baseURL).embeddings.create({ model: 'techsara-embed', input: 'x', encoding_format: 'float' });
    assert.equal(s.requests[0].headers['idempotency-key'], undefined);
    assert.deepEqual(JSON.parse(s.requests[0].body), { model: 'techsara-embed', input: 'x', encoding_format: 'float' });
  });

  it('embeddings.create without encoding_format sends base64 on the wire, so the server must implement base64', async () => {
    const s = await stub((req, res) => {
      // 1.0 and -2.0 as little-endian float32, the §8.4 base64 encoding.
      const buf = Buffer.alloc(8);
      buf.writeFloatLE(1.0, 0);
      buf.writeFloatLE(-2.0, 4);
      sendJSON(res, {
        object: 'list',
        model: 'techsara-embed',
        data: [{ object: 'embedding', index: 0, embedding: buf.toString('base64') }],
        usage: { prompt_tokens: 1, total_tokens: 1 },
      });
    });
    const out = await client(s.baseURL).embeddings.create({ model: 'techsara-embed', input: 'x' });
    assert.equal(JSON.parse(s.requests[0].body).encoding_format, 'base64');
    assert.deepEqual(Array.from(out.data[0].embedding), [1, -2]);
  });
});
