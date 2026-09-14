// "Timeout disabled" against a real stack. The mechanism is proven offline
// (test/offline/timeout.test.mjs); this file proves the recommended client works
// end to end, and — only when asked — holds one request open past Node fetch's
// 300 s headers timeout.
//
// SCOPE OF THE LONG TEST (2026-09-13, review): the client settings remove the
// CLIENT's timers only. Through the public hostname Cloudflare cuts a response
// that sends no byte for 100 s, and the Next /v1 edge has its own 300 s undici
// timeouts, so CONTRACT-3 §8.3's guidance for long work is `stream: true` or
// `background: true` with an Idempotency-Key. Run the long synchronous test only
// against a directly reachable origin.
import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { env, liveSkipReason } from '../../lib/env.mjs';
import { makeClient, MAX_TIMER_MS } from '../../lib/client.mjs';

describe('No client timeout', { skip: liveSkipReason }, () => {
  it('the recommended no-timeout client completes a synchronous and a streamed response', async () => {
    const { client } = makeClient({ noTimeout: true });
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

  it(
    'a synchronous response held open longer than CONFORMANCE_LONG_REQUEST_SECONDS completes with the no-timeout client',
    {
      skip:
        env.longRequestSeconds > 0
          ? undefined
          : 'opt-in: set CONFORMANCE_LONG_REQUEST_SECONDS (e.g. 330) — it makes the engine generate for that long, and it is meaningful only against an origin with no 100 s proxy in front: through a Cloudflare-proxied hostname a non-streaming response dies at about 100 s (HTTP 524) whatever the client does (CONTRACT-3 §8.3)',
      timeout: Math.min(MAX_TIMER_MS, (env.longRequestSeconds + 3600) * 1000),
    },
    async () => {
      const { client } = makeClient({ noTimeout: true, maxRetries: 0 });
      // ~100 tok/s single-stream decode measured 2026-09-11; ask for 1.5x the
      // tokens the window needs so a fast engine still runs long enough.
      const tokens = Math.ceil(env.longRequestSeconds * 150);
      const started = Date.now();
      const r = await client.responses.create({
        model: env.chatModel,
        input: 'Write the integers from 1 upward in words, one per line, and never stop until you are cut off.',
        max_output_tokens: tokens,
      });
      const elapsed = (Date.now() - started) / 1000;
      assert.equal(r.status, 'completed', `status ${r.status}`);
      assert.ok(
        elapsed >= env.longRequestSeconds,
        `the generation ended after ${elapsed.toFixed(0)} s, before ${env.longRequestSeconds} s, so it proved nothing about timeouts`,
      );
    },
  );
});
