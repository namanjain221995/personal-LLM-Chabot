// GET /v1/models and /v1/models/{model} through openai-node (CONTRACT-3 §7, §15).
import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { env, liveSkipReason } from '../../lib/env.mjs';
import { makeClient, raw } from '../../lib/client.mjs';
import { itPlanned } from '../../lib/features.mjs';
import { assertEnvelope, assertNoInternalNames, rejection } from '../../lib/assertions.mjs';

const SIX = ['techsara-35b', 'techsara-8b-vision', 'techsara-ocr', 'techsara-embed', 'techsara-rerank', 'techsara-whisper'];

describe('Models', { skip: liveSkipReason }, () => {
  it('models.list returns a list of model objects that includes the chat model', async () => {
    const { client } = makeClient();
    const page = await client.models.list();
    const ids = [];
    for await (const m of page) {
      assert.equal(m.object, 'model');
      assert.equal(typeof m.id, 'string');
      assert.equal(m.owned_by, 'techsara');
      ids.push(m.id);
    }
    assert.ok(ids.includes(env.chatModel), `ids: ${ids.join(', ')}`);
  });

  it('no listed model names an internal checkpoint, engine or URL', async () => {
    const res = await raw('GET', '/models');
    assert.equal(res.status, 200);
    assertNoInternalNames(res.text, 'GET /v1/models');
    for (const m of res.json.data) assert.match(m.id, /^techsara-[a-z0-9-]+$/);
  });

  it('models.retrieve returns the chat model by id', async () => {
    const { client } = makeClient();
    const m = await client.models.retrieve(env.chatModel);
    assert.equal(m.id, env.chatModel);
    assert.equal(m.object, 'model');
  });

  it('models.retrieve for an unknown model is a 404 NotFoundError with code model_not_found, not retried', async () => {
    const { client, requests } = makeClient();
    const err = await rejection(client.models.retrieve('techsara-does-not-exist'));
    assertEnvelope(err, { status: 404, code: 'model_not_found' });
    assert.equal(requests.length, 1);
  });

  it('a successful /v1 response carries X-Request-Id', async () => {
    const { client } = makeClient();
    const { response, request_id } = await client.models.list().withResponse();
    assert.match(String(request_id), /^req_/);
    assert.equal(response.headers.get('x-request-id'), request_id);
  });

  it('the published OpenAPI document declares Retry-After on the 503 of both generating operations', async () => {
    const res = await raw('GET', '/openapi.json', { apiKey: '' });
    assert.equal(res.status, 200);
    for (const path of ['/v1/responses', '/v1/chat/completions']) {
      const op = res.json.paths?.[path]?.post;
      assert.ok(op, `${path} is in the schema`);
      assert.ok(op.responses?.['503']?.headers?.['Retry-After'], `${path} 503 documents Retry-After`);
    }
    assertNoInternalNames(res.text, 'openapi.json');
  });

  itPlanned('six-models', 'the model list holds all six public models', async () => {
    const res = await raw('GET', '/models');
    assert.equal(res.status, 200);
    const ids = res.json.data.map((m) => m.id);
    for (const id of SIX) assert.ok(ids.includes(id), `${id} missing from ${ids.join(', ')}`);
  });

  itPlanned('six-models', 'each non-chat model advertises its kind and endpoint on models.retrieve', async () => {
    const { client } = makeClient();
    const expected = {
      'techsara-8b-vision': ['chat', '/v1/responses'],
      'techsara-ocr': ['chat', '/v1/responses'],
      'techsara-embed': ['embedding', '/v1/embeddings'],
      'techsara-rerank': ['rerank', '/v1/rerank'],
      'techsara-whisper': ['transcription', '/v1/audio/transcriptions'],
    };
    for (const [id, [kind, endpoint]] of Object.entries(expected)) {
      const m = await client.models.retrieve(id);
      assert.equal(m.kind, kind, `${id}.kind`);
      assert.ok(Array.isArray(m.endpoints) && m.endpoints.some((e) => e.includes(endpoint)), `${id}.endpoints ${JSON.stringify(m.endpoints)}`);
    }
  });

  itPlanned('one-million-output', 'techsara-35b advertises an output ceiling of min(1,000,000, its context window), and a default within it', async () => {
    // WHY no hard-coded window (2026-09-13, review): CONTRACT-3 §15 reads limits
    // at call time and narrows them by the engine's served max_model_len, and
    // §8.3 defines the ceiling as min(PUBLIC_API_MAX_OUTPUT_TOKENS, window). A
    // stack with a smaller MAIN_MODEL_MAX_LEN is conformant with a smaller
    // number; only the relation is the contract. CONFORMANCE_OUTPUT_CEILING
    // names a deployment whose PUBLIC_API_MAX_OUTPUT_TOKENS is not the default.
    const res = await raw('GET', '/models/techsara-35b');
    assert.equal(res.status, 200, res.text);
    const m = res.json;
    assert.ok(Number.isInteger(m.context_window) && m.context_window > 0, `context_window ${m.context_window}`);
    assert.equal(m.max_output_tokens, Math.min(env.outputCeiling, m.context_window), `max_output_tokens ${m.max_output_tokens} for window ${m.context_window}`);
    assert.ok(Number.isInteger(m.max_input_tokens) && m.max_input_tokens < m.context_window, `max_input_tokens ${m.max_input_tokens}`);
    assert.ok(
      Number.isInteger(m.default_max_output_tokens) && m.default_max_output_tokens > 0 && m.default_max_output_tokens <= m.max_output_tokens,
      `default_max_output_tokens ${m.default_max_output_tokens}`,
    );
  });

  itPlanned('no-usage-limits', 'an authenticated /v1 response carries no RateLimit or RateLimit-Policy header', async () => {
    const res = await raw('GET', '/models');
    assert.equal(res.status, 200);
    assert.equal(res.headers.get('ratelimit'), null, `RateLimit: ${res.headers.get('ratelimit')}`);
    assert.equal(res.headers.get('ratelimit-policy'), null, `RateLimit-Policy: ${res.headers.get('ratelimit-policy')}`);
  });
});
