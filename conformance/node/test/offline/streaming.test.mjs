// How openai-node surfaces the CONTRACT-3 §10 terminal events.
import { describe, it, after } from 'node:test';
import assert from 'node:assert/strict';
import OpenAI from 'openai';
import { startStub } from '../../lib/stub.mjs';

const stubs = [];
after(async () => Promise.all(stubs.map((s) => s.close())));

async function sse(frames) {
  const s = await startStub(async (req, res) => {
    res.writeHead(200, { 'content-type': 'text/event-stream', 'x-request-id': 'req_stub' });
    for (const f of frames) res.write(f);
    res.end();
  });
  stubs.push(s);
  return new OpenAI({ baseURL: s.baseURL, apiKey: 'x', maxRetries: 0 });
}

const frame = (event, data) => `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;

describe('Terminal stream events through openai-node (offline)', () => {
  it('an `error` event is thrown as an APIError carrying the envelope code, after the events before it were delivered', async () => {
    const c = await sse([
      frame('response.created', { type: 'response.created', sequence_number: 1, response: { id: 'resp_x', status: 'queued' } }),
      frame('error', {
        type: 'error',
        sequence_number: 2,
        error: { message: 'The model is at capacity.', type: 'server_error', code: 'model_unavailable', param: null, request_id: 'req_x' },
      }),
    ]);
    const stream = await c.responses.create({ model: 'techsara-35b', input: 'x', stream: true });
    const seen = [];
    const err = await (async () => {
      for await (const ev of stream) seen.push(ev.type);
    })().then(() => null, (e) => e);
    assert.deepEqual(seen, ['response.created']);
    assert.ok(err instanceof OpenAI.APIError, `got ${err?.constructor?.name}`);
    assert.equal(err.code, 'model_unavailable');
    // WHY pinned: the SDK builds this error with no HTTP status (the stream was a 200).
    assert.equal(err.status, undefined);
  });

  it('a response.failed event is delivered as an ordinary event, not thrown, so a caller must check the terminal type', async () => {
    const c = await sse([
      frame('response.created', { type: 'response.created', sequence_number: 1, response: { id: 'resp_x', status: 'queued' } }),
      frame('response.failed', {
        type: 'response.failed',
        sequence_number: 2,
        response: { id: 'resp_x', status: 'failed', error: { code: 'timeout', message: 'The generation exceeded its wall clock.' }, usage: null },
      }),
    ]);
    const stream = await c.responses.create({ model: 'techsara-35b', input: 'x', stream: true });
    const seen = [];
    for await (const ev of stream) seen.push(ev);
    assert.deepEqual(seen.map((e) => e.type), ['response.created', 'response.failed']);
    assert.equal(seen[1].response.error.code, 'timeout');
  });

  it('a Chat Completions error chunk followed by [DONE] is thrown as an APIError', async () => {
    const c = await sse([
      `data: ${JSON.stringify({ id: 'chatcmpl_x', object: 'chat.completion.chunk', created: 1, model: 'techsara-35b', choices: [{ index: 0, delta: { role: 'assistant', content: 'par' }, finish_reason: null }] })}\n\n`,
      `data: ${JSON.stringify({ error: { message: 'The generation failed.', type: 'server_error', code: 'model_unavailable', param: null } })}\n\n`,
      'data: [DONE]\n\n',
    ]);
    const stream = await c.chat.completions.create({ model: 'techsara-35b', messages: [{ role: 'user', content: 'x' }], stream: true });
    const text = [];
    const err = await (async () => {
      for await (const chunk of stream) text.push(chunk.choices[0]?.delta?.content ?? '');
    })().then(() => null, (e) => e);
    assert.equal(text.join(''), 'par');
    assert.equal(err?.code, 'model_unavailable');
  });
});

describe('The responses.stream() helper and the event set it needs (offline)', () => {
  const resp = (status, output = [], usage = null) => ({
    id: 'resp_x',
    object: 'response',
    created_at: 1789200000,
    status,
    model: 'techsara-35b',
    output,
    usage,
  });
  const usage = { input_tokens: 3, output_tokens: 1, total_tokens: 4 };

  it('fails with "missing output at index 0" on the CONTRACT-3 §10 event set (no output_item / content_part events)', async () => {
    // The exact frames the e2e stack sent on 2026-09-13 (ids shortened).
    const c = await sse([
      frame('response.created', { type: 'response.created', sequence_number: 1, response: resp('queued') }),
      frame('response.in_progress', { type: 'response.in_progress', sequence_number: 2, response: resp('in_progress') }),
      frame('response.output_text.delta', { type: 'response.output_text.delta', sequence_number: 3, item_id: 'msg_x', output_index: 0, content_index: 0, delta: 'ok' }),
      frame('response.output_text.done', { type: 'response.output_text.done', sequence_number: 4, item_id: 'msg_x', output_index: 0, content_index: 0, text: 'ok' }),
      frame('response.completed', {
        type: 'response.completed',
        sequence_number: 5,
        response: resp('completed', [{ type: 'message', role: 'assistant', content: [{ type: 'output_text', text: 'ok' }] }], usage),
      }),
    ]);
    await assert.rejects(
      c.responses.stream({ model: 'techsara-35b', input: 'x' }).finalResponse(),
      /missing output at index 0/,
    );
  });

  it('succeeds once output_item.added, content_part.added, content_part.done and output_item.done frame the text', async () => {
    const item = { id: 'msg_x', type: 'message', role: 'assistant', status: 'in_progress', content: [] };
    const part = { type: 'output_text', text: '', annotations: [] };
    const doneItem = { ...item, status: 'completed', content: [{ type: 'output_text', text: 'ok', annotations: [] }] };
    const c = await sse([
      frame('response.created', { type: 'response.created', sequence_number: 1, response: resp('queued') }),
      frame('response.in_progress', { type: 'response.in_progress', sequence_number: 2, response: resp('in_progress') }),
      frame('response.output_item.added', { type: 'response.output_item.added', sequence_number: 3, output_index: 0, item }),
      frame('response.content_part.added', { type: 'response.content_part.added', sequence_number: 4, item_id: 'msg_x', output_index: 0, content_index: 0, part }),
      frame('response.output_text.delta', { type: 'response.output_text.delta', sequence_number: 5, item_id: 'msg_x', output_index: 0, content_index: 0, delta: 'ok' }),
      frame('response.output_text.done', { type: 'response.output_text.done', sequence_number: 6, item_id: 'msg_x', output_index: 0, content_index: 0, text: 'ok' }),
      frame('response.content_part.done', {
        type: 'response.content_part.done',
        sequence_number: 7,
        item_id: 'msg_x',
        output_index: 0,
        content_index: 0,
        part: { type: 'output_text', text: 'ok', annotations: [] },
      }),
      frame('response.output_item.done', { type: 'response.output_item.done', sequence_number: 8, output_index: 0, item: doneItem }),
      frame('response.completed', { type: 'response.completed', sequence_number: 9, response: resp('completed', [doneItem], usage) }),
    ]);
    const final = await c.responses.stream({ model: 'techsara-35b', input: 'x' }).finalResponse();
    assert.equal(final.status, 'completed');
    assert.equal(final.output_text, 'ok');
  });
});
