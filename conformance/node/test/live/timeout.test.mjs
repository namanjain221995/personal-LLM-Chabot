// "Timeout disabled" against a real stack. The mechanism is proven offline
// (test/offline/timeout.test.mjs); this file proves the documented client works
// end to end, and — only when asked — holds requests open long enough to matter.
//
// 2026-09-13, no-timeout design revision 2 (CONTRACT-3 §10.1): the API writes a
// byte within 15 s and at least every 15 s after, so a long synchronous call now
// works through the public hostname too — Cloudflare's 125 s first-byte limit and
// undici's 300 s timers in this process are both reset by those bytes. What the
// SDK itself does with its own timer is the remaining question, and the opt-in
// eleven-minute test records it as documented behaviour.
import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import OpenAI from 'openai';
import { env, liveSkipReason } from '../../lib/env.mjs';
import { documentedClientOptions, makeClient, MAX_TIMER_MS } from '../../lib/client.mjs';
import { itPlanned, SkipTest } from '../../lib/features.mjs';

function countingPrompt(tokens) {
  return `Write every integer from 1 to ${Math.max(1000, tokens)} in ascending order, one per line, with no other text.`;
}

describe('No client timeout', { skip: liveSkipReason }, () => {
  it('the documented client (timeout 2147483647, five retries) completes a synchronous and a streamed response', async () => {
    const { client } = makeClient({ clientOptions: documentedClientOptions() });
    assert.equal(client.timeout, MAX_TIMER_MS);
    const r = await client.responses.create({ model: env.chatModel, input: 'Reply with the single word: ok', max_output_tokens: 16 });
    assert.equal(r.status, 'completed');
    const stream = await client.chat.completions.create({
      model: env.chatModel,
      messages: [{ role: 'user', content: 'Reply with the single word: ok' }],
      max_tokens: 16,
      stream: true,
    });
    let text = '';
    for await (const chunk of stream) text += chunk.choices[0]?.delta?.content ?? '';
    assert.ok(text.length > 0);
  });

  itPlanned(
    'no-timeouts',
    'a synchronous response held open longer than CONFORMANCE_LONG_REQUEST_SECONDS completes with the documented client',
    { timeout: Math.min(MAX_TIMER_MS, (env.longRequestSeconds + 3600) * 1000) },
    async () => {
      if (!(env.longRequestSeconds > 0)) {
        throw new SkipTest('opt-in: set CONFORMANCE_LONG_REQUEST_SECONDS (e.g. 330) — it makes the engine generate for that long');
      }
      const { client } = makeClient({ clientOptions: { ...documentedClientOptions(), maxRetries: 0 } });
      // ~100 tok/s single-stream decode measured 2026-09-11; ask for 1.5x.
      const tokens = Math.ceil(env.longRequestSeconds * 150);
      const started = Date.now();
      const r = await client.responses.create({ model: env.chatModel, input: countingPrompt(tokens), max_output_tokens: tokens, temperature: 0 });
      const elapsed = (Date.now() - started) / 1000;
      assert.equal(r.status, 'completed', `status ${r.status}`);
      assert.ok(elapsed >= env.longRequestSeconds, `ended after ${elapsed.toFixed(0)} s, before ${env.longRequestSeconds} s: it proved nothing`);
    },
  );

  itPlanned(
    'no-timeouts',
    'an eleven-minute synchronous call: the default 600 s client gives up after 3 attempts that all join ONE generation, 2147483647 finishes (documented behaviour)',
    { timeout: 45 * 60 * 1000 },
    async (t) => {
      if (process.env.CONFORMANCE_ELEVEN_MINUTES !== '1') {
        throw new SkipTest('opt-in: set CONFORMANCE_ELEVEN_MINUTES=1 — about 42 minutes of engine time');
      }
      const tokens = 11 * 60 * 150;
      const body = { model: env.chatModel, input: countingPrompt(tokens), max_output_tokens: tokens, temperature: 0 };

      const recorded = makeClient({ maxRetries: 2 });
      const started = Date.now();
      const err = await recorded.client.responses.create(body).then(() => null, (e) => e);
      const elapsed = (Date.now() - started) / 1000;
      assert.ok(err instanceof OpenAI.APIConnectionTimeoutError, `the 7.x default client should time out: ${err?.constructor?.name}`);
      assert.equal(recorded.requests.length, 3, 'three attempts');
      assert.deepEqual(recorded.requests.map((r) => r.headers['x-stainless-retry-count']), ['0', '1', '2']);
      t.diagnostic(`default client gave up after ${elapsed.toFixed(0)} s and ${recorded.requests.length} attempts (the SDK's own deadline)`);

      const { client } = makeClient({ clientOptions: { ...documentedClientOptions(), maxRetries: 0 } });
      const r = await client.responses.create(body);
      assert.equal(r.status, 'completed');
    },
  );
});
