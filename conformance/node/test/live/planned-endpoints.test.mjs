// Endpoints that are designed but not on every stack yet. Every test here is an
// `itPlanned` (lib/features.mjs): it RUNS, and reports XFAIL until the feature is
// promoted to built, then guards it. Shapes come from CONTRACT-3 §8.4-§8.6 and
// the Files API design of 2026-09-13; where the design is not final (the upload
// resume read) the assertions are deliberately few and say so.
import { describe } from 'node:test';
import assert from 'node:assert/strict';
import { toFile } from 'openai';
import { liveSkipReason, env } from '../../lib/env.mjs';
import { makeClient, raw } from '../../lib/client.mjs';
import { itPlanned } from '../../lib/features.mjs';
import { assertEnvelope, assertNoInternalNames, rejection } from '../../lib/assertions.mjs';
import { toneWav } from '../../lib/media.mjs';

describe('Planned: embeddings', { skip: liveSkipReason }, () => {
  itPlanned('embeddings', 'embeddings.create returns one 1,024-dimension float vector per input, in input order', async () => {
    const { client } = makeClient();
    const out = await client.embeddings.create({
      model: 'techsara-embed',
      input: ['the first text', 'the second text'],
      encoding_format: 'float',
    });
    assert.equal(out.object, 'list');
    assert.equal(out.model, 'techsara-embed');
    assert.deepEqual(out.data.map((d) => d.index), [0, 1]);
    for (const d of out.data) {
      assert.equal(d.object, 'embedding');
      assert.equal(d.embedding.length, 1024);
      assert.ok(d.embedding.every(Number.isFinite));
    }
    if (out.usage !== null) assert.ok(out.usage.prompt_tokens > 0 && out.usage.total_tokens === out.usage.prompt_tokens);
  });

  itPlanned('embeddings', 'embeddings.create with the SDK default encoding (base64 on the wire) decodes to the same vector', async () => {
    const { client } = makeClient();
    const asFloat = await client.embeddings.create({ model: 'techsara-embed', input: 'same text', encoding_format: 'float' });
    const asDefault = await client.embeddings.create({ model: 'techsara-embed', input: 'same text' });
    assert.equal(asDefault.data[0].embedding.length, 1024);
    const a = asFloat.data[0].embedding;
    const b = Array.from(asDefault.data[0].embedding);
    assert.ok(a.every((v, i) => Math.abs(v - b[i]) < 1e-5), 'float and base64 vectors agree');
  });

  itPlanned('embeddings', 'a dimensions parameter is rejected with 400 naming it', async () => {
    const { client } = makeClient();
    const err = await rejection(client.embeddings.create({ model: 'techsara-embed', input: 'x', dimensions: 256, encoding_format: 'float' }));
    assertEnvelope(err, { status: 400, param: 'dimensions' });
  });
});

describe('Planned: rerank (raw HTTP; openai-node has no rerank method)', { skip: liveSkipReason }, () => {
  itPlanned('rerank', 'POST /v1/rerank returns results sorted by relevance_score, cut to top_n, with documents when asked', async () => {
    const res = await raw('POST', '/rerank', {
      body: {
        model: 'techsara-rerank',
        query: 'How do I rotate an API key?',
        documents: ['Webhooks are signed with HMAC.', { text: 'Keys are rotated from the developer console.' }, 'The sky is blue.'],
        top_n: 2,
        return_documents: true,
      },
    });
    assert.equal(res.status, 200, res.text);
    // CONTRACT-3 §8.5 and §16: the ledger generation id, `rrk_<24 hex>`.
    assert.match(res.json.id, /^rrk_[0-9a-f]{24}$/);
    assert.equal(res.json.object, 'rerank');
    assert.equal(res.json.model, 'techsara-rerank');
    assert.equal(res.json.results.length, 2);
    const scores = res.json.results.map((r) => r.relevance_score);
    assert.ok(scores[0] >= scores[1], `sorted: ${scores}`);
    assert.equal(res.json.results[0].index, 1, 'the key-rotation document ranks first');
    assert.equal(res.json.results[0].document.text, 'Keys are rotated from the developer console.');
    assertNoInternalNames(res.json, 'rerank');
  });
});

describe('Planned: audio transcriptions', { skip: liveSkipReason }, () => {
  itPlanned('audio-transcriptions', 'audio.transcriptions.create with a one-second WAV returns json text and duration usage', async () => {
    const { client } = makeClient();
    const out = await client.audio.transcriptions.create({
      model: 'techsara-whisper',
      file: await toFile(toneWav(1), 'tone.wav', { type: 'audio/wav' }),
    });
    assert.equal(typeof out.text, 'string');
    // CONTRACT-3 §8.6 / endpoint_models.transcription_body: usage is null —
    // never zero — when the engine reports no duration.
    if (out.usage !== null) {
      assert.equal(out.usage?.type, 'duration', `usage ${JSON.stringify(out.usage)}`);
      assert.ok(Number.isInteger(out.usage.seconds) && out.usage.seconds >= 1, `usage.seconds ${out.usage.seconds}`);
    }
  });

  itPlanned('audio-transcriptions', 'response_format srt is refused with 400', async () => {
    const { client } = makeClient();
    const err = await rejection(
      client.audio.transcriptions.create({
        model: 'techsara-whisper',
        file: await toFile(toneWav(1), 'tone.wav', { type: 'audio/wav' }),
        response_format: 'srt',
      }),
    );
    assertEnvelope(err, { status: 400 });
  });
});

describe('Planned: Files and Uploads', { skip: liveSkipReason }, () => {
  const text = `conformance-node file body ${new Date().toISOString()}\n`;

  itPlanned('files-api', 'files.create, retrieve, content, list and delete round-trip a small text file', async () => {
    const { client } = makeClient();
    const created = await client.files.create({ file: await toFile(Buffer.from(text), 'note.txt', { type: 'text/plain' }), purpose: 'user_data' });
    try {
      assert.match(created.id, /^file-/);
      assert.equal(created.object, 'file');
      assert.equal(created.bytes, Buffer.byteLength(text));
      assert.equal(created.filename, 'note.txt');
      assert.equal(created.purpose, 'user_data');
      const got = await client.files.retrieve(created.id);
      assert.equal(got.id, created.id);
      const content = await client.files.content(created.id);
      assert.equal(await content.text(), text);
      const ids = [];
      for await (const f of client.files.list({ purpose: 'user_data', limit: 100 })) ids.push(f.id);
      assert.ok(ids.includes(created.id), 'the new file is listed');
    } finally {
      const deleted = await client.files.delete(created.id);
      assert.deepEqual({ id: deleted.id, deleted: deleted.deleted }, { id: created.id, deleted: true });
    }
  });

  itPlanned('files-api', 'a purpose the platform has no product for (fine-tune) is 400 with param purpose', async () => {
    const { client } = makeClient();
    const err = await rejection(client.files.create({ file: await toFile(Buffer.from(text), 'x.jsonl'), purpose: 'fine-tune' }));
    assertEnvelope(err, { status: 400, param: 'purpose' });
  });

  itPlanned('uploads-chunked', 'uploads.create, two parts and complete yield a file; a fresh client reads the pending upload back to resume', async () => {
    const first = makeClient().client;
    const partA = Buffer.from('A'.repeat(1024));
    const partB = Buffer.from('B'.repeat(512));
    const upload = await first.uploads.create({
      bytes: partA.length + partB.length,
      filename: 'chunked.txt',
      mime_type: 'text/plain',
      purpose: 'user_data',
    });
    assert.match(upload.id, /^upload_/);
    assert.equal(upload.status, 'pending');
    const p1 = await first.uploads.parts.create(upload.id, { data: await toFile(partA, 'part-1') });
    assert.equal(p1.upload_id, upload.id);

    // "The client restarted": a new client instance, knowing only the upload id.
    // GET /v1/uploads/{id} is a TechSara extension the SDK can reach generically;
    // its part listing is not final in the design, so only identity and state
    // are asserted.
    const second = makeClient().client;
    const resumed = await second.get(`/uploads/${upload.id}`);
    assert.equal(resumed.id, upload.id);
    assert.equal(resumed.status, 'pending');

    const p2 = await second.uploads.parts.create(upload.id, { data: await toFile(partB, 'part-2') });
    const done = await second.uploads.complete(upload.id, { part_ids: [p1.id, p2.id] });
    assert.equal(done.status, 'completed');
    assert.match(done.file.id, /^file-/);
    const content = await second.files.content(done.file.id);
    assert.equal(await content.text(), partA.toString() + partB.toString());
    await second.files.delete(done.file.id);
  });

  itPlanned('uploads-chunked', 'completing with a byte count that does not match the declared bytes is 400', async () => {
    const { client } = makeClient();
    const upload = await client.uploads.create({ bytes: 4096, filename: 'short.txt', mime_type: 'text/plain', purpose: 'user_data' });
    const p = await client.uploads.parts.create(upload.id, { data: await toFile(Buffer.from('too short'), 'p') });
    const err = await rejection(client.uploads.complete(upload.id, { part_ids: [p.id] }));
    assertEnvelope(err, { status: 400 });
    await client.uploads.cancel(upload.id).catch(() => undefined);
  });

  itPlanned('file-input', 'an uploaded text file is read by the model as input_file on /v1/responses', async () => {
    const { client } = makeClient();
    const secret = `pelican-${Math.floor(Math.random() * 1e6)}`;
    const f = await client.files.create({
      file: await toFile(Buffer.from(`The code word is ${secret}.\n`), 'codeword.txt', { type: 'text/plain' }),
      purpose: 'user_data',
    });
    try {
      const r = await client.responses.create({
        model: env.chatModel,
        input: [
          {
            role: 'user',
            content: [
              { type: 'input_file', file_id: f.id },
              { type: 'input_text', text: 'What is the code word in the file? Reply with the code word only.' },
            ],
          },
        ],
        max_output_tokens: 32,
      });
      assert.match(r.output_text, new RegExp(secret));
    } finally {
      await client.files.delete(f.id).catch(() => undefined);
    }
  });
});
