// The CONTRACT-3 §9 error envelope and retry semantics, live. Everything here is
// refused before an engine is called, except the idempotency pair, which
// generates two short answers.
import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { randomUUID } from 'node:crypto';
import OpenAI from 'openai';
import { env, liveSkipReason } from '../../lib/env.mjs';
import { makeClient, raw } from '../../lib/client.mjs';
import { assertEnvelope, rejection } from '../../lib/assertions.mjs';

const SHORT = 'Reply with the single word: ok';

describe('Errors and retries', { skip: liveSkipReason }, () => {
  it('a malformed key is 401 AuthenticationError invalid_api_key, and the SDK does not retry it', async () => {
    const { client, requests } = makeClient({ apiKey: 'tsk_test_not-a-real-key' });
    const err = await rejection(client.models.list());
    assert.ok(err instanceof OpenAI.AuthenticationError, err.constructor.name);
    assertEnvelope(err, { status: 401, code: 'invalid_api_key', type: 'authentication_error' });
    assert.equal(requests.length, 1);
  });

  it('a well-formed key with a wrong checksum is the same generic 401', async () => {
    const fake = `tsk_test_${'0'.repeat(16)}_${'A'.repeat(43)}ZZZZZZ`;
    const { client } = makeClient({ apiKey: fake });
    const err = await rejection(client.responses.create({ model: env.chatModel, input: SHORT, max_output_tokens: 16 }));
    assertEnvelope(err, { status: 401, code: 'invalid_api_key' });
  });

  it('a request with no Authorization header is 401 with the envelope', async () => {
    const res = await raw('GET', '/models', { apiKey: '' });
    assert.equal(res.status, 401);
    assert.equal(res.json?.error?.code, 'invalid_api_key');
    assert.equal(res.headers.get('x-request-id'), res.json.error.request_id);
  });

  it('a key without responses.write is 403 PermissionDeniedError insufficient_scope, and the SDK does not retry it', {
    skip: env.narrowKey ? undefined : 'TECHSARA_API_KEY_NARROW is not set',
  }, async () => {
    const { client, requests } = makeClient({ apiKey: env.narrowKey });
    const err = await rejection(client.responses.create({ model: env.chatModel, input: SHORT, max_output_tokens: 16 }));
    assert.ok(err instanceof OpenAI.PermissionDeniedError, err.constructor.name);
    assertEnvelope(err, { status: 403, code: 'insufficient_scope' });
    assert.equal(requests.length, 1);
  });

  it('the same narrow key can still list models, because scopes are exact and not implied', {
    skip: env.narrowKey ? undefined : 'TECHSARA_API_KEY_NARROW is not set',
  }, async () => {
    const { client } = makeClient({ apiKey: env.narrowKey });
    const page = await client.models.list();
    assert.ok(page.data.length > 0);
  });

  it('an unknown model on responses.create is 404 NotFoundError model_not_found with param model', async () => {
    const { client } = makeClient();
    const err = await rejection(client.responses.create({ model: 'techsara-nope', input: SHORT, max_output_tokens: 16 }));
    assert.ok(err instanceof OpenAI.NotFoundError, err.constructor.name);
    assertEnvelope(err, { status: 404, code: 'model_not_found' });
  });

  it('an unknown /v1 path is a 404 with the envelope, not an HTML page', async () => {
    const res = await raw('GET', '/definitely-not-an-endpoint');
    assert.equal(res.status, 404);
    assert.equal(typeof res.json?.error?.message, 'string');
  });

  it('a body over the 1 MiB text limit is 413 request_too_large', async () => {
    const { client } = makeClient({ maxRetries: 0 });
    const err = await rejection(
      client.responses.create({ model: env.chatModel, input: 'a'.repeat(1024 * 1024 + 64), max_output_tokens: 16 }),
    );
    assertEnvelope(err, { status: 413, code: 'request_too_large' });
  });

  it('the same Idempotency-Key with the same body replays the first response id, status and usage', async () => {
    const { client } = makeClient();
    const key = `conformance-node-${randomUUID()}`;
    const body = { model: env.chatModel, input: SHORT, max_output_tokens: 16 };
    // WHY `headers` and not the SDK's `idempotencyKey` option: openai-node drops
    // that option (test/offline/retries.test.mjs pins it).
    const first = await client.responses.create(body, { headers: { 'Idempotency-Key': key } });
    const second = await client.responses.create(body, { headers: { 'Idempotency-Key': key } });
    assert.equal(second.id, first.id);
    assert.equal(second.status, first.status);
    // WHAT THIS DOES NOT PROVE (2026-09-13, review): equal usage is consistent
    // with a replay, but a second generation of "ok" measures the same 19/2/21.
    // The id is the evidence that the stored response came back; whether the
    // engine ran again is not observable from a /v1 caller.
    assert.deepEqual(second.usage, first.usage, 'the replay carries the first response usage');
  });

  it('a replayed response carries the original output text, as CONTRACT-3 §13 promises ("the original response is returned")', async () => {
    // WHY this is its own test (2026-09-13): the replay above keeps id, status and
    // usage, but CONTRACT-3 §16 says output is not stored, and the implementation
    // replays `output: []`. To an SDK caller retrying after a dropped connection
    // that is a completed response whose answer is the empty string —
    // indistinguishable from a model that said nothing. Either §13 or the replay
    // has to change; until one does this test fails on purpose.
    const { client } = makeClient();
    const key = `conformance-node-${randomUUID()}`;
    const body = { model: env.chatModel, input: SHORT, max_output_tokens: 16 };
    const first = await client.responses.create(body, { headers: { 'Idempotency-Key': key } });
    const second = await client.responses.create(body, { headers: { 'Idempotency-Key': key } });
    assert.ok(first.output_text.length > 0, 'the first call answered');
    assert.equal(second.output_text, first.output_text, 'replayed output_text');
  });

  it('the same Idempotency-Key with a different body is 409 ConflictError idempotency_conflict', async () => {
    // maxRetries 0: openai-node retries every 409 by default (offline test), which
    // would spend two more requests on a conflict that cannot clear.
    const { client, requests } = makeClient({ maxRetries: 0 });
    const key = `conformance-node-${randomUUID()}`;
    await client.responses.create({ model: env.chatModel, input: SHORT, max_output_tokens: 16 }, { headers: { 'Idempotency-Key': key } });
    const err = await rejection(
      client.responses.create(
        { model: env.chatModel, input: 'Reply with the single word: different', max_output_tokens: 16 },
        { headers: { 'Idempotency-Key': key } },
      ),
    );
    assert.ok(err instanceof OpenAI.ConflictError, err.constructor.name);
    assertEnvelope(err, { status: 409, code: 'idempotency_conflict' });
    assert.equal(requests.length, 2);
  });

  it('a 503 model_unavailable at capacity carries Retry-After that the SDK obeys', {
    skip:
      'not provoked live: an at-capacity 503 needs a saturated engine, which this suite must not create on a shared stack. ' +
      'The SDK side is proven offline in test/offline/retries.test.mjs; the schema side by "the published OpenAPI document declares Retry-After".',
  }, () => {});
});
