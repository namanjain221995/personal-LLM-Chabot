// Resuming a Responses stream (CONTRACT-3 §10.3), through openai-node.
//
// WHY (2026-09-13, no-timeout design revision 2): a tunnel drop cuts every open
// connection and no timer prevents it. `client.responses.retrieve(id, { stream:
// true, starting_after })` is the call the documentation teaches for that; these
// tests break a stream on purpose and prove the resumed events continue it
// exactly. On Chat Completions, a retry with the same Idempotency-Key replays.
import { describe } from 'node:test';
import assert from 'node:assert/strict';
import { randomUUID } from 'node:crypto';
import OpenAI from 'openai';
import { env, liveSkipReason } from '../../lib/env.mjs';
import { documentedClientOptions, makeClient, raw } from '../../lib/client.mjs';
import { itPlanned, SkipTest } from '../../lib/features.mjs';

const PROMPT = 'Write the integers from 1 to 120 in words, one per line, with no other text.';
const TERMINALS = new Set(['response.completed', 'response.failed']);

describe('Resuming a stream', { skip: liveSkipReason }, () => {
  itPlanned('resumable-streams', 'a stream broken after five deltas resumes with contiguous sequence numbers and the same text', async () => {
    const { client } = makeClient({ clientOptions: documentedClientOptions() });
    const stream = await client.responses.create({ model: env.chatModel, input: PROMPT, max_output_tokens: 400, temperature: 0, stream: true });
    const seen = [];
    for await (const event of stream) {
      seen.push(event);
      if (seen.filter((e) => e.type === 'response.output_text.delta').length >= 5) break;
    }
    const responseId = seen.find((e) => e.type === 'response.created').response.id;
    const lastSeq = seen.at(-1).sequence_number;

    const resumed = [];
    for await (const event of await client.responses.retrieve(responseId, { stream: true, starting_after: lastSeq })) resumed.push(event);
    const numbers = [...seen, ...resumed].map((e) => e.sequence_number);
    assert.deepEqual(numbers, numbers.map((_, i) => i + 1));
    assert.ok(TERMINALS.has(resumed.at(-1).type), resumed.at(-1).type);
    const text = [...seen, ...resumed].filter((e) => e.type === 'response.output_text.delta').map((e) => e.delta).join('');
    const done = resumed.find((e) => e.type === 'response.output_text.done');
    assert.equal(text, done.text);
  });

  itPlanned('resumable-streams', 'starting_after without stream=true is a 400, and an unknown response is a 404 first', async () => {
    const unknown = await raw('GET', '/responses/resp_000000000000000000000000?starting_after=1');
    assert.equal(unknown.status, 404, unknown.text);
    const { client } = makeClient({ clientOptions: documentedClientOptions() });
    const r = await client.responses.create({ model: env.chatModel, input: 'Reply ok.', max_output_tokens: 16 });
    const bad = await raw('GET', `/responses/${r.id}?starting_after=1`);
    assert.equal(bad.status, 400, bad.text);
  });

  itPlanned('resumable-streams', 'a store:false response cannot be replayed (400, param stream)', async () => {
    const { client } = makeClient({ clientOptions: documentedClientOptions() });
    const stream = await client.responses.create({ model: env.chatModel, input: 'Reply ok.', max_output_tokens: 16, stream: true, store: false });
    let id;
    for await (const event of stream) if (event.type === 'response.created') id = event.response.id;
    const replay = await raw('GET', `/responses/${id}?stream=true&starting_after=0`);
    assert.equal(replay.status, 400, replay.text);
    assert.equal(replay.json?.error?.param, 'stream');
  });

  itPlanned('no-timeouts', 'a Chat Completions retry with the same Idempotency-Key replays the stream with the same completion id', async () => {
    const { client } = makeClient({ clientOptions: documentedClientOptions() });
    const key = randomUUID();
    const body = { model: env.chatModel, messages: [{ role: 'user', content: 'Reply with the words one two three.' }], max_tokens: 16, stream: true };
    const collect = async () => {
      const chunks = [];
      for await (const chunk of await client.chat.completions.create(body, { headers: { 'Idempotency-Key': key } })) chunks.push(chunk);
      return chunks;
    };
    const first = await collect();
    const second = await collect();
    assert.equal(second[0].id, first[0].id, 'the replay is the same completion');
    const text = (chunks) => chunks.map((c) => c.choices[0]?.delta?.content ?? '').join('');
    assert.equal(text(second), text(first));
  });

  itPlanned('no-timeouts', 'a same-key retry from a key of another service account is a 409 marked x-should-retry: false', async () => {
    // Needs a second key that may call /v1/responses but belongs to another
    // service account; the narrow models.read key is refused for its scope
    // first, which says nothing about the attach rule.
    const other = process.env.TECHSARA_API_KEY_OTHER_ACCOUNT;
    if (!other) throw new SkipTest('needs TECHSARA_API_KEY_OTHER_ACCOUNT: a responses.write key of another service account in the same project');
    const key = randomUUID();
    const { client } = makeClient({ clientOptions: documentedClientOptions() });
    await client.responses.create({ model: env.chatModel, input: 'Reply ok.', max_output_tokens: 16 }, { headers: { 'Idempotency-Key': key } });
    const second = await raw('POST', '/responses', {
      apiKey: other,
      headers: { 'idempotency-key': key },
      body: { model: env.chatModel, input: 'Reply ok.', max_output_tokens: 16 },
    });
    assert.equal(second.status, 409, second.text);
    assert.equal(second.json?.error?.code, 'idempotency_conflict');
    assert.equal(second.headers.get('x-should-retry'), 'false');
  });
});

void OpenAI;
