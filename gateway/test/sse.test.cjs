'use strict';
/** The SSE frame parser (2026-09-13): what it keeps, strips and recognises. */

const test = require('node:test');
const assert = require('node:assert/strict');

const { SseParser, errorFrame } = require('../lib/sse.cjs');

function all(chunks) {
  const p = new SseParser();
  const frames = [];
  for (const c of chunks) frames.push(...p.push(Buffer.isBuffer(c) ? c : Buffer.from(c)));
  return { frames, partial: p.partial };
}

test('a data frame followed by its ts-seq comment yields the frame and a marker carrying N', () => {
  const { frames } = all(['data: {"a":1}\n\n: ts-seq=7\n\n']);
  assert.deepEqual(frames.map((f) => [f.kind, f.text, f.seq]), [
    ['event', 'data: {"a":1}\n\n', null],
    ['marker', '', 7],
  ]);
});

test('a ts-seq line inside the frame is stripped from the relayed text and still confirms it', () => {
  const { frames } = all(['event: response.output_text.delta\ndata: {"x":1}\n: ts-seq=12\n\n']);
  assert.equal(frames.length, 1);
  assert.equal(frames[0].text, 'event: response.output_text.delta\ndata: {"x":1}\n\n');
  assert.equal(frames[0].seq, 12);
});

test('frames split anywhere, including inside CRLF and inside a multi-byte character, come out whole', () => {
  const text = 'data: {"t":"ગુજરાતી"}\r\n\r\n: ping\r\n\r\ndata: [DONE]\r\n\r\n';
  const bytes = Buffer.from(text);
  for (let cut = 1; cut < bytes.length; cut += 1) {
    const { frames } = all([bytes.subarray(0, cut), bytes.subarray(cut)]);
    assert.deepEqual(
      frames.map((f) => f.text),
      ['data: {"t":"ગુજરાતી"}\n\n', ': ping\n\n', 'data: [DONE]\n\n'],
      `cut at ${cut}`,
    );
  }
});

test('comments other than ts-seq are relayed as comments', () => {
  const { frames } = all([': ping\n\n: queued\n\n']);
  assert.deepEqual(frames.map((f) => [f.kind, f.text]), [
    ['comment', ': ping\n\n'],
    ['comment', ': queued\n\n'],
  ]);
});

test('the terminal events are recognised in both dialects and for transcription', () => {
  const terminal = (s) => all([s]).frames[0].terminal;
  assert.equal(terminal('data: [DONE]\n\n'), true);
  assert.equal(terminal('event: response.completed\ndata: {}\n\n'), true);
  assert.equal(terminal('event: response.failed\ndata: {}\n\n'), true);
  assert.equal(terminal('event: response.incomplete\ndata: {}\n\n'), true);
  assert.equal(terminal('event: error\ndata: {}\n\n'), true);
  assert.equal(terminal('event: transcript.text.done\ndata: {}\n\n'), true);
  assert.equal(terminal('event: response.output_text.delta\ndata: {}\n\n'), false);
  assert.equal(terminal('data: {"choices":[]}\n\n'), false);
});

test('an unfinished frame is reported as partial and never emitted', () => {
  const { frames, partial } = all(['data: {"a":1}\n', 'data: {"b"']);
  assert.deepEqual(frames, []);
  assert.equal(partial, true);
});

test('the late-refusal error frame carries the orchestrator envelope, or a model_unavailable one', () => {
  const env = { error: { message: 'm', type: 'invalid_request_error', code: 'c', param: null, request_id: 'r' } };
  assert.equal(errorFrame(env), `event: error\ndata: ${JSON.stringify(env)}\n\n`);
  assert.match(errorFrame(null), /"code":"model_unavailable"/);
  assert.match(errorFrame({ detail: 'not an envelope' }), /"code":"model_unavailable"/);
});

test('one 16 MiB event fed in 64 KiB chunks is parsed in linear time, not by rescanning the unfinished buffer', () => {
  // Review finding 2026-09-13 (scratchpad p8_sse_quad.cjs): the rescanning
  // parser took 4,540 ms for this, the worst single push 39.5 ms.
  const p = new SseParser();
  const payload = Buffer.alloc(16 * 1024 * 1024, 0x61);
  const frames = [];
  let worst = 0;
  const started = process.hrtime.bigint();
  frames.push(...p.push(Buffer.from('event: response.completed\ndata: "')));
  for (let at = 0; at < payload.length; at += 64 * 1024) {
    const t0 = process.hrtime.bigint();
    frames.push(...p.push(payload.subarray(at, at + 64 * 1024)));
    worst = Math.max(worst, Number(process.hrtime.bigint() - t0) / 1e6);
  }
  frames.push(...p.push(Buffer.from('"\n\n')));
  const total = Number(process.hrtime.bigint() - started) / 1e6;
  assert.equal(frames.length, 1);
  assert.equal(frames[0].text.length, 'event: response.completed\ndata: ""\n\n'.length + payload.length);
  assert.equal(frames[0].terminal, true);
  assert.ok(total < 1500, `${total.toFixed(0)} ms in total`);
});

test('an unfinished frame past the size limit throws SSE_FRAME_TOO_LARGE instead of growing without bound', () => {
  const p = new SseParser({ maxFrameChars: 1000 });
  assert.deepEqual(p.push(Buffer.from(`data: ${'x'.repeat(500)}\n`)).length, 0);
  assert.throws(() => p.push(Buffer.from('y'.repeat(600))), { code: 'SSE_FRAME_TOO_LARGE' });
  const q = new SseParser({ maxFrameChars: 1000 });
  // Whole frames of any count stay fine: the limit is per frame.
  for (let i = 0; i < 50; i += 1) assert.equal(q.push(Buffer.from(`data: ${'z'.repeat(400)}\n\n`)).length, 1);
});

test('a CR ending one chunk and its LF starting the next are one line ending', () => {
  const { frames } = all(['data: a\r', '\ndata: b\r', '\n\r', '\n']);
  assert.deepEqual(frames.map((f) => f.text), ['data: a\ndata: b\n\n']);
  const lone = all(['data: a\r', '\r', 'data: b\n\n']);
  assert.deepEqual(lone.frames.map((f) => f.text), ['data: a\n\n', 'data: b\n\n']);
});
