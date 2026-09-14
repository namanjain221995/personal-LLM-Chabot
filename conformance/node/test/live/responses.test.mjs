// POST /v1/responses — synchronous, streamed and background — through openai-node
// (CONTRACT-3 §8.1, §9, §10, §14). Every generating call asks for a handful of
// tokens: the stacks this runs against share their engine with people.
import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { setTimeout as sleep } from 'node:timers/promises';
import { env, liveSkipReason } from '../../lib/env.mjs';
import { makeClient, raw } from '../../lib/client.mjs';
import { itPlanned, SkipTest } from '../../lib/features.mjs';
import { assertEnvelope, assertNoInternalNames, assertUsage, rejection } from '../../lib/assertions.mjs';
import { pngDataUrl } from '../../lib/media.mjs';

const SHORT = 'Reply with the single word: ok';
const TERMINAL = new Set(['response.completed', 'response.failed', 'error']);

/**
 * The frames openai-node's `responses.stream()` accumulator needs around the
 * text. CONTRACT-3 §10 does not list them; openai-node 7.15.0 throws "missing
 * output at index 0" without them (test/offline/streaming.test.mjs).
 */
const ITEM_FRAMES = new Set([
  'response.output_item.added',
  'response.content_part.added',
  'response.content_part.done',
  'response.output_item.done',
]);

/** Every non-terminal event this suite accepts on a Responses stream. */
const KNOWN_NON_TERMINAL = new Set([
  'response.created',
  'response.queued',
  'response.in_progress',
  'response.output_text.delta',
  'response.output_text.done',
  ...ITEM_FRAMES,
]);

async function collectStream(client, body) {
  const stream = await client.responses.create({ ...body, stream: true });
  const events = [];
  for await (const ev of stream) events.push(ev);
  return { events, types: events.map((e) => e.type) };
}

async function pollUntilTerminal(client, id, { timeoutMs = 120_000 } = {}) {
  const deadline = Date.now() + timeoutMs;
  let r;
  do {
    r = await client.responses.retrieve(id);
    if (['completed', 'failed', 'cancelled'].includes(r.status)) return r;
    await sleep(500);
  } while (Date.now() < deadline);
  assert.fail(`response ${id} still ${r?.status} after ${timeoutMs} ms`);
}

describe('Responses: synchronous', { skip: liveSkipReason }, () => {
  it('responses.create returns a completed response with output text, output_text and consistent usage', async () => {
    const { client } = makeClient();
    const r = await client.responses.create({ model: env.chatModel, input: SHORT, max_output_tokens: 16 });
    assert.match(r.id, /^resp_/);
    assert.equal(r.object, 'response');
    assert.equal(r.status, 'completed');
    assert.equal(r.model, env.chatModel);
    assert.ok(Number.isInteger(r.created_at));
    assert.equal(r.output[0].type, 'message');
    assert.equal(r.output[0].role, 'assistant');
    assert.equal(r.output[0].content[0].type, 'output_text');
    assert.ok(r.output_text.length > 0, 'the SDK output_text helper sees the text');
    assert.match(String(r._request_id), /^req_/);
    assertUsage(r.usage);
    assertNoInternalNames(r, 'response');
  });

  it('a message-list input with a system turn and instructions is accepted', async () => {
    const { client } = makeClient();
    const r = await client.responses.create({
      model: env.chatModel,
      instructions: 'Answer in one word.',
      input: [
        { role: 'system', content: 'You are terse.' },
        { role: 'user', content: SHORT },
      ],
      max_output_tokens: 16,
      temperature: 0,
      metadata: { suite: 'conformance-node' },
    });
    assert.equal(r.status, 'completed');
    assert.ok(r.output_text.length > 0);
  });

  it('a parameter the platform cannot honour (top_p) is rejected with 400 naming it, never ignored', async () => {
    const { client } = makeClient();
    const err = await rejection(client.responses.create({ model: env.chatModel, input: SHORT, top_p: 0.5, max_output_tokens: 16 }));
    assertEnvelope(err, { status: 400, type: 'invalid_request_error' });
    assert.match(`${err.param} ${err.message}`, /top_p/);
  });

  // CONTRACT-3 §8.1 / §14 (2026-09-14): refused until a background job had an
  // event log to follow; with the durable runtime the connection follows it.
  itPlanned('resumable-streams', 'stream and background together stream the job to one terminal event', async () => {
    const { client } = makeClient();
    const stream = await client.responses.create({ model: env.chatModel, input: SHORT, stream: true, background: true, max_output_tokens: 16 });
    const events = [];
    for await (const event of stream) events.push(event);
    assert.equal(events[0]?.type, 'response.created');
    assert.equal(events.filter((e) => TERMINAL.has(e.type)).length, 1);
  });

  it('max_output_tokens above the model ceiling advertised by /v1/models is 400, before any generation', async () => {
    const { client } = makeClient();
    const model = await client.models.retrieve(env.chatModel);
    assert.ok(Number.isInteger(model.max_output_tokens), `model.max_output_tokens ${model.max_output_tokens}`);
    const err = await rejection(
      client.responses.create({ model: env.chatModel, input: SHORT, max_output_tokens: model.max_output_tokens + 1 }),
    );
    assertEnvelope(err, { status: 400, param: 'max_output_tokens' });
  });

  it('retrieving a response id that does not exist is 404 response_not_found', async () => {
    const { client } = makeClient();
    const err = await rejection(client.responses.retrieve('resp_000000000000000000000000'));
    assertEnvelope(err, { status: 404, code: 'response_not_found' });
  });

  itPlanned('one-million-output', 'the response object carries max_output_tokens and incomplete_details', async () => {
    const res = await raw('POST', '/responses', { body: { model: env.chatModel, input: SHORT, max_output_tokens: 16 } });
    assert.equal(res.status, 200, res.text);
    assert.ok('max_output_tokens' in res.json, `keys: ${Object.keys(res.json).join(', ')}`);
    assert.ok('incomplete_details' in res.json, `keys: ${Object.keys(res.json).join(', ')}`);
    assert.ok(Number.isInteger(res.json.max_output_tokens) && res.json.max_output_tokens <= 16, `max_output_tokens ${res.json.max_output_tokens}`);
    // null when the model stopped by itself; the §9 object when 16 tokens ran out first.
    const d = res.json.incomplete_details;
    assert.ok(d === null || (d && d.reason === 'max_output_tokens'), `incomplete_details ${JSON.stringify(d)}`);
  });

  itPlanned('one-million-output', 'a response stopped by its ceiling reports incomplete_details.reason max_output_tokens', async () => {
    const { client } = makeClient();
    const r = await client.responses.create({
      model: env.chatModel,
      input: 'List the numbers from 1 to 200, separated by commas.',
      max_output_tokens: 4,
    });
    assert.equal(r.status, 'completed');
    assert.deepEqual(r.incomplete_details, { reason: 'max_output_tokens' });
    assert.equal(r.max_output_tokens, 4);
  });

  itPlanned(
    'one-million-output',
    'max_output_tokens of 1,000,000 on techsara-35b is accepted and clamped, not refused',
    {
      // WHY opt-in (2026-09-13, review): input + planned output > 131,072 takes
      // the fleet-wide `main.long` gate (public concurrency 1, CONTRACT-3 §12.3).
      skip: env.allowLongGate
        ? undefined
        : 'opt-in: set CONFORMANCE_ALLOW_LONG_GATE=1 — this request holds the fleet-wide main.long capacity slot (concurrency 1) that customer long jobs share',
    },
    async () => {
      const { client } = makeClient();
      let r;
      try {
        r = await client.responses.create({ model: 'techsara-35b', input: SHORT, max_output_tokens: 1_000_000 }, { maxRetries: 0 });
      } catch (err) {
        if (err.status === 503 && err.code === 'model_unavailable') {
          throw new SkipTest(`long gate busy: 503 model_unavailable, Retry-After ${err.headers?.get?.('retry-after')} — another long request holds main.long`);
        }
        throw err;
      }
      assert.equal(r.status, 'completed');
      assert.ok(r.max_output_tokens > 0 && r.max_output_tokens <= 1_000_000, `applied ${r.max_output_tokens}`);
    },
  );

  itPlanned('image-input', 'an input_image data URL on a user message is read by techsara-35b', async () => {
    const { client } = makeClient();
    const r = await client.responses.create({
      model: 'techsara-35b',
      input: [
        {
          role: 'user',
          content: [
            { type: 'input_text', text: 'What single colour fills this image? One word.' },
            { type: 'input_image', image_url: pngDataUrl(64, [220, 30, 30]), detail: 'auto' },
          ],
        },
      ],
      max_output_tokens: 16,
    });
    assert.equal(r.status, 'completed');
    assert.match(r.output_text.toLowerCase(), /red/);
  });

  const httpImageBody = {
    model: 'techsara-35b',
    input: [
      {
        role: 'user',
        content: [
          { type: 'input_text', text: 'Describe.' },
          { type: 'input_image', image_url: 'http://127.0.0.1:1/probe.png', detail: 'auto' },
        ],
      },
    ],
    max_output_tokens: 16,
  };

  it('a user message carrying an http image URL part is refused with 400 invalid_request_error', async () => {
    // WHAT THIS DOES NOT PROVE (2026-09-13, review): on a stack without image
    // input the 400 is "content must be a string", so the SSRF rule itself is
    // not exercised here, and "no engine was called" cannot be observed from
    // outside. The planned test below asserts the SSRF-specific refusal.
    const { client } = makeClient();
    const err = await rejection(client.responses.create(httpImageBody));
    assertEnvelope(err, { status: 400, type: 'invalid_request_error' });
  });

  itPlanned('image-input', 'an http image URL is refused because only data: URLs are accepted (the §8.1 SSRF rule)', async () => {
    const { client } = makeClient();
    const err = await rejection(client.responses.create(httpImageBody));
    assertEnvelope(err, { status: 400, type: 'invalid_request_error' });
    assert.match(err.error.message, /data: URL/, `message: ${err.error.message}`);
  });

  itPlanned('six-models', 'techsara-8b-vision answers on /v1/responses', async () => {
    const { client } = makeClient();
    const r = await client.responses.create({ model: 'techsara-8b-vision', input: SHORT, max_output_tokens: 16 });
    assert.equal(r.status, 'completed');
    assert.equal(r.model, 'techsara-8b-vision');
    assert.ok(r.output_text.length > 0);
  });

  itPlanned('six-models', 'techsara-ocr transcribes the text part of exactly one image', async () => {
    const { client } = makeClient();
    const r = await client.responses.create({
      model: 'techsara-ocr',
      input: [{ role: 'user', content: [{ type: 'input_image', image_url: pngDataUrl(64, [255, 255, 255]), detail: 'auto' }] }],
      max_output_tokens: 64,
    });
    assert.equal(r.status, 'completed');
    assert.equal(r.model, 'techsara-ocr');
    assert.equal(typeof r.output_text, 'string');
  });

  itPlanned('six-models', 'a permitted model on an endpoint its kind does not serve is 400 with param model', async () => {
    const { client } = makeClient();
    const err = await rejection(client.responses.create({ model: 'techsara-embed', input: SHORT, max_output_tokens: 16 }));
    assertEnvelope(err, { status: 400, type: 'invalid_request_error', param: 'model' });
  });
});

describe('Responses: streaming', { skip: liveSkipReason }, () => {
  it('a stream follows the §10 order: created, [queued], in_progress, deltas, output_text.done, one terminal last, sequence numbers 1, 2, 3 …', async () => {
    // WHY only the §10 invariants (2026-09-13, review): the first version pinned
    // "nothing but deltas between in_progress and output_text.done", which
    // forbade the output_item / content_part frames openai-node's stream helper
    // needs, and failed on the contract-legal response.queued a recovering engine
    // announces. No server could pass it and the helper test at the same time.
    const { client } = makeClient();
    const { events, types } = await collectStream(client, { model: env.chatModel, input: SHORT, max_output_tokens: 16 });
    const path = types.join(' → ');

    assert.equal(types[0], 'response.created', path);
    const progressAt = types[1] === 'response.queued' ? 2 : 1;
    assert.equal(types[progressAt], 'response.in_progress', `in_progress after created (and an optional queued): ${path}`);
    assert.equal(types.filter((t) => t === 'response.queued').length <= 1, true, `at most one queued: ${path}`);
    assert.equal(types.at(-1), 'response.completed', path);
    assert.equal(types.filter((t) => TERMINAL.has(t)).length, 1, `exactly one terminal event: ${path}`);
    for (const t of types.slice(0, -1)) assert.ok(KNOWN_NON_TERMINAL.has(t), `unexpected event ${t}: ${path}`);

    const deltaIdx = types.flatMap((t, i) => (t === 'response.output_text.delta' ? [i] : []));
    const doneAt = types.indexOf('response.output_text.done');
    assert.ok(deltaIdx.length > 0, `at least one delta: ${path}`);
    assert.equal(types.filter((t) => t === 'response.output_text.done').length, 1, `one output_text.done: ${path}`);
    assert.ok(deltaIdx[0] > progressAt, `deltas after in_progress: ${path}`);
    assert.ok(doneAt > deltaIdx.at(-1), `output_text.done after the last delta: ${path}`);

    events.forEach((e, i) => assert.equal(e.sequence_number, i + 1, `sequence_number of event ${i} (${e.type})`));

    const deltas = deltaIdx.map((i) => events[i].delta).join('');
    const completed = events.at(-1).response;
    assert.equal(events[doneAt].text, deltas, 'done.text is the concatenated deltas');
    const message = completed.output.find((o) => o.type === 'message');
    assert.equal(message?.content?.[0]?.text, deltas, 'the terminal snapshot carries the same text');
    assert.equal(completed.status, 'completed');

    for (const e of events.slice(0, -1)) if (e.response) assert.equal(e.response.usage, null, `${e.type} usage is null`);
    assertUsage(completed.usage);
    assert.notEqual(completed.usage, null, 'the terminal event carries measured usage on this stack');
  });

  it('the text is framed by output_item.added and content_part.added before the first delta, and content_part.done and output_item.done after output_text.done, with one item id throughout', async () => {
    // WHY (2026-09-13): openai-node's responses.stream() accumulator needs these
    // four frames; without them it throws "missing output at index 0". This test
    // names the missing frames, the helper test below shows the SDK consequence.
    const { client } = makeClient();
    const { events, types } = await collectStream(client, { model: env.chatModel, input: SHORT, max_output_tokens: 16 });
    const path = types.join(' → ');
    const at = (t) => types.indexOf(t);
    const firstDelta = at('response.output_text.delta');
    const textDone = at('response.output_text.done');
    const terminal = types.length - 1;
    for (const t of ITEM_FRAMES) assert.equal(types.filter((x) => x === t).length, 1, `exactly one ${t}: ${path}`);
    assert.ok(at('response.output_item.added') < at('response.content_part.added'), `item before part: ${path}`);
    assert.ok(at('response.content_part.added') < firstDelta, `content_part.added before the first delta: ${path}`);
    assert.ok(textDone < at('response.content_part.done'), `content_part.done after output_text.done: ${path}`);
    assert.ok(at('response.content_part.done') < at('response.output_item.done'), `part done before item done: ${path}`);
    assert.ok(at('response.output_item.done') < terminal, `output_item.done before the terminal: ${path}`);

    const added = events[at('response.output_item.added')];
    const itemId = added.item?.id;
    assert.ok(typeof itemId === 'string' && itemId.length > 0, `output_item.added.item.id ${JSON.stringify(added.item)}`);
    for (const e of events) if ('item_id' in e) assert.equal(e.item_id, itemId, `${e.type}.item_id`);
    assert.equal(events[at('response.output_item.done')].item.id, itemId, 'output_item.done.item.id');
    const message = events.at(-1).response.output.find((o) => o.type === 'message');
    assert.equal(message?.id, itemId, 'the terminal snapshot message id equals the delta item_id');
  });

  it('the stream is served as text/event-stream with no-store caching and a request id', async () => {
    const { client } = makeClient();
    const { data: stream, response } = await client.responses
      .create({ model: env.chatModel, input: SHORT, stream: true, max_output_tokens: 16 })
      .withResponse();
    assert.match(response.headers.get('content-type') ?? '', /^text\/event-stream/);
    assert.match(response.headers.get('cache-control') ?? '', /no-store/);
    assert.match(response.headers.get('x-request-id') ?? '', /^req_/);
    for await (const _ of stream) {
      /* drain so the generation is not abandoned */
    }
  });

  it('the responses.stream() helper resolves finalResponse() to the completed response', async () => {
    const { client } = makeClient();
    const runner = client.responses.stream({ model: env.chatModel, input: SHORT, max_output_tokens: 16 });
    let text = '';
    runner.on('response.output_text.delta', (e) => {
      text += e.delta;
    });
    const final = await runner.finalResponse();
    assert.equal(final.status, 'completed');
    assert.equal(final.output_text, text);
  });

  itPlanned('one-million-output', 'every stream snapshot carries max_output_tokens and incomplete_details, the terminal one the applied value', async () => {
    const { client } = makeClient();
    const stream = await client.responses.create({ model: env.chatModel, input: SHORT, stream: true, max_output_tokens: 16 });
    const snapshots = [];
    for await (const ev of stream) if (ev.response) snapshots.push(ev.response);
    for (const s of snapshots) {
      assert.ok('max_output_tokens' in s && 'incomplete_details' in s, `snapshot keys ${Object.keys(s).join(', ')}`);
    }
    assert.ok(snapshots.at(-1).max_output_tokens <= 16);
  });
});

describe('Responses: background', { skip: liveSkipReason }, () => {
  it('a background response is accepted queued or in_progress and polls to completed through responses.retrieve', async () => {
    const { client } = makeClient();
    const created = await client.responses.create({ model: env.chatModel, input: SHORT, background: true, max_output_tokens: 16 });
    assert.match(created.id, /^resp_/);
    assert.ok(['queued', 'in_progress', 'completed'].includes(created.status), `created.status ${created.status}`);
    const done = await pollUntilTerminal(client, created.id);
    assert.equal(done.id, created.id);
    assert.equal(done.status, 'completed');
    assert.ok(done.output_text.length > 0);
    assertUsage(done.usage);
  });

  it('cancelling a background response ends it cancelled, and cancelling again is idempotent', async (t) => {
    const { client } = makeClient();
    const created = await client.responses.create({
      model: env.chatModel,
      input: 'Write the numbers from 1 to 400 in words, one per line.',
      background: true,
      max_output_tokens: 600,
    });
    const first = await client.responses.cancel(created.id);
    assert.equal(first.id, created.id);
    if (first.status === 'completed') {
      // WHY SKIP and not PASS (2026-09-13, review): the generation finished before
      // the cancel landed, so cancellation itself was never exercised.
      t.skip('cancel lost the race: the background generation completed before cancel arrived, so cancellation was not exercised');
      return;
    }
    const settled = await pollUntilTerminal(client, created.id);
    assert.equal(settled.status, 'cancelled', `settled status ${settled.status}`);
    const second = await client.responses.cancel(created.id);
    const now = await client.responses.retrieve(created.id);
    assert.equal(second.status, now.status, 'a second cancel returns the current state, no error');
    assert.ok(['cancelled', 'completed'].includes(now.status), `final ${now.status}`);
  });
});
