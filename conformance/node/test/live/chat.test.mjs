// POST /v1/chat/completions through openai-node (CONTRACT-3 §8.2).
import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { env, liveSkipReason } from '../../lib/env.mjs';
import { makeClient } from '../../lib/client.mjs';
import { itPlanned } from '../../lib/features.mjs';
import { assertEnvelope, assertNoInternalNames, assertUsage, rejection } from '../../lib/assertions.mjs';
import { pngDataUrl } from '../../lib/media.mjs';

const messages = [{ role: 'user', content: 'Reply with the single word: ok' }];

describe('Chat Completions', { skip: liveSkipReason }, () => {
  it('chat.completions.create with max_tokens returns one assistant choice, a finish_reason and usage', async () => {
    const { client } = makeClient();
    const c = await client.chat.completions.create({ model: env.chatModel, messages, max_tokens: 16 });
    assert.equal(c.object, 'chat.completion');
    assert.equal(c.model, env.chatModel);
    assert.equal(c.choices.length, 1);
    assert.equal(c.choices[0].index, 0);
    assert.equal(c.choices[0].message.role, 'assistant');
    assert.ok(c.choices[0].message.content.length > 0);
    assert.ok(['stop', 'length'].includes(c.choices[0].finish_reason), `finish_reason ${c.choices[0].finish_reason}`);
    assertUsage(c.usage, { inputKey: 'prompt_tokens', outputKey: 'completion_tokens' });
    assertNoInternalNames(c, 'chat.completion');
  });

  it('a completion cut by max_tokens reports finish_reason length', async () => {
    const { client } = makeClient();
    const c = await client.chat.completions.create({
      model: env.chatModel,
      messages: [{ role: 'user', content: 'List the numbers from 1 to 200, separated by commas.' }],
      max_tokens: 4,
    });
    assert.equal(c.choices[0].finish_reason, 'length');
    if (c.usage) assert.ok(c.usage.completion_tokens <= 4, `completion_tokens ${c.usage.completion_tokens}`);
  });

  it('a stream delivers content deltas, exactly one finish_reason, and with include_usage a final usage chunk', async () => {
    const { client } = makeClient();
    const stream = await client.chat.completions.create({
      model: env.chatModel,
      messages,
      max_tokens: 16,
      stream: true,
      stream_options: { include_usage: true },
    });
    const chunks = [];
    for await (const chunk of stream) chunks.push(chunk);
    assert.ok(chunks.length >= 2, `chunks ${chunks.length}`);
    for (const ch of chunks) assert.equal(ch.object, 'chat.completion.chunk');
    const ids = new Set(chunks.map((c) => c.id));
    assert.equal(ids.size, 1, 'one completion id across chunks');
    const finishes = chunks.flatMap((c) => c.choices.map((ch) => ch.finish_reason)).filter(Boolean);
    assert.equal(finishes.length, 1, `finish reasons ${JSON.stringify(finishes)}`);
    const text = chunks.flatMap((c) => c.choices.map((ch) => ch.delta?.content ?? '')).join('');
    assert.ok(text.length > 0);
    const last = chunks.at(-1);
    assert.deepEqual(last.choices, [], 'the usage chunk has no choices');
    assertUsage(last.usage, { inputKey: 'prompt_tokens', outputKey: 'completion_tokens' });
    assert.notEqual(last.usage, null);
  });

  it('a stream without include_usage carries no usage chunk', async () => {
    const { client } = makeClient();
    const stream = await client.chat.completions.create({ model: env.chatModel, messages, max_tokens: 16, stream: true });
    const chunks = [];
    for await (const chunk of stream) chunks.push(chunk);
    assert.ok(chunks.every((c) => !c.usage), 'no chunk carries usage');
  });

  it('a Chat Completions field the platform does not support (n) is rejected with 400 naming it', async () => {
    const { client } = makeClient();
    const err = await rejection(client.chat.completions.create({ model: env.chatModel, messages, max_tokens: 16, n: 2 }));
    assertEnvelope(err, { status: 400, type: 'invalid_request_error', param: 'n' });
  });

  itPlanned('max-completion-tokens', 'max_completion_tokens works as the alias of max_tokens, and sending both is 400', async () => {
    const { client } = makeClient();
    const c = await client.chat.completions.create({ model: env.chatModel, messages, max_completion_tokens: 16 });
    assert.ok(c.choices[0].message.content.length > 0);
    const err = await rejection(
      client.chat.completions.create({ model: env.chatModel, messages, max_tokens: 16, max_completion_tokens: 16 }),
    );
    assertEnvelope(err, { status: 400, type: 'invalid_request_error' });
  });

  itPlanned('max-completion-tokens', 'a stream with max_completion_tokens ends with finish_reason length when cut', async () => {
    const { client } = makeClient();
    const stream = await client.chat.completions.create({
      model: env.chatModel,
      messages: [{ role: 'user', content: 'List the numbers from 1 to 200, separated by commas.' }],
      max_completion_tokens: 4,
      stream: true,
    });
    const finishes = [];
    for await (const chunk of stream) for (const ch of chunk.choices) if (ch.finish_reason) finishes.push(ch.finish_reason);
    assert.deepEqual(finishes, ['length']);
  });

  itPlanned('one-million-output', 'chat.completion carries the applied max_output_tokens extension', async () => {
    const { client } = makeClient();
    const c = await client.chat.completions.create({ model: env.chatModel, messages, max_tokens: 16 });
    assert.ok(Number.isInteger(c.max_output_tokens) && c.max_output_tokens <= 16, `max_output_tokens ${c.max_output_tokens}`);
  });

  itPlanned('image-input', 'an image_url data URL part on a user message is read by techsara-35b', async () => {
    const { client } = makeClient();
    const c = await client.chat.completions.create({
      model: 'techsara-35b',
      messages: [
        {
          role: 'user',
          content: [
            { type: 'text', text: 'What single colour fills this image? One word.' },
            { type: 'image_url', image_url: { url: pngDataUrl(64, [30, 30, 220]), detail: 'auto' } },
          ],
        },
      ],
      max_tokens: 16,
    });
    assert.match(c.choices[0].message.content.toLowerCase(), /blue/);
  });
});
