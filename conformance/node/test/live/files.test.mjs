// Files, chunked Uploads, resume after a dropped connection, and files as model
// input — through openai-node 7.15.0. Written 2026-09-13 against the Files API as
// BUILT (orchestrator/app/publicapi/files/wire.py and routes.py, and the
// model-input facade apifiles/service.py), not only the design, and kept at
// parity with conformance/python/tests/test_files_uploads.py:
//
// * ids `file-<24 hex>`, `upload_<24 hex>`, `part_<24 hex>`; the File object adds
//   `sha256`, `mime_type` and `processing`, and keeps `status` in
//   {uploaded, processed, error} so `waitForProcessing` works unchanged;
// * `POST /uploads/{id}/parts` takes optional `part_number` (0-based) and `sha256`
//   form fields; `PUT /uploads/{id}/parts/{n}` takes a raw body with
//   `X-Part-SHA256` or `Content-Digest`; `GET /uploads/{id}` lists held parts;
// * `complete` always answers 200 with a nested `file` and copies no bytes, so a
//   wrong md5 or sha256 shows later as that file ending in `error` /
//   `checksum_mismatch`;
// * another project's id answers exactly like an id that never existed, on every
//   file and upload route and as model input on both dialects;
// * scopes: each route needs exactly its one scope, checked BEFORE any lookup, so
//   a real id and a random id get the same 403; a `file_id` in a prompt needs
//   `files.read` on top of `responses.write`, inline `file_data` needs none;
// * model input: `input_file` / `input_image` / `input_video` with a `file_id`,
//   inline `file_data` and `file_context` on /v1/responses; the `file` part and
//   `input_audio` on /v1/chat/completions (the built Responses dialect has no
//   `input_audio`; an uploaded recording reaches /v1/responses as `input_file` or
//   `input_video`). A failed file, or one of the wrong kind for its part, is a 400
//   naming that part's `file_id`.
//
// HOW THE AUDIO TESTS KNOW THE RECORDING REACHED THE MODEL (2026-09-13, review).
// The fixture is a tone, so no test depends on transcript words. An uploaded
// recording: the model must answer its length (TONE_SECONDS), which only the
// recording's context block carries, and the bare question must not get it — a
// prompt-token comparison is not enough there, because the system addendum and a
// "(The file … is attached above.)" placeholder reach the prompt even when the
// block is dropped. `input_audio`: the clip adds only its transcript block, so the
// prompt count must grow by at least MIN_MEDIA_BLOCK_TOKENS over the bare question.
//
// Every test is an `itPlanned` of `files-api`, `uploads-chunked` or `file-input`
// (lib/feature-list.mjs): XFAIL until the feature is promoted, then it guards it.
// Extra keys, each optional (a test that needs a missing one SKIPs with the reason):
// TECHSARA_API_KEY_OTHER_PROJECT (a key of a second test project),
// TECHSARA_API_KEY_NARROW (same project, models.read only) and
// TECHSARA_API_KEY_RESPONSES_ONLY (same project, responses.* and no files.*).
// CONFORMANCE_FILES_EDGE_REFUSALS=edge|origin pins the form of the 411 and 413
// refusals given before a body is read (gateway edge form, or the orchestrator's).
//
// Proven by conformance/python/selftest/files_selftest.py against
// conformance/python/selftest/files_local_target.py — the real handlers under
// uvicorn with stub auth and stub engines (its docstring lists what is stubbed):
// all PASS with CONFORMANCE_BUILT_FEATURES=files-api,uploads-chunked,file-input,
// all XPASS(strict) without it, and each deliberate defect fails exactly the
// tests its EXPECTED entry lists. selftest/design-stub.mjs does NOT implement
// this wire, so selftest/run.mjs must leave this file to that self-test.
import { describe } from 'node:test';
import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import http from 'node:http';
import https from 'node:https';
import { toFile } from 'openai';
import { env, liveSkipReason } from '../../lib/env.mjs';
import { makeClient, raw } from '../../lib/client.mjs';
import { itPlanned, SkipTest } from '../../lib/features.mjs';
import { assertEnvelope, rejection } from '../../lib/assertions.mjs';
import { solidPng, toneWav } from '../../lib/media.mjs';

const KIB = 1024;
/** Small parts on purpose: the suite may run against a shared stack. */
const PART = 64 * KIB;
const POLL_MS = Number(process.env.CONFORMANCE_FILES_POLL_MS || 2000);
const PROCESSING_TIMEOUT_MS = Number(process.env.CONFORMANCE_FILES_PROCESSING_TIMEOUT_MS || 900_000);
const OTHER_KEY = process.env.TECHSARA_API_KEY_OTHER_PROJECT || '';
const RESPONSES_ONLY_KEY = process.env.TECHSARA_API_KEY_RESPONSES_ONLY || '';
/** The least an input_audio transcript block adds to a prompt (header comment). */
const MIN_MEDIA_BLOCK_TOKENS = 8;
/** Seven, not two: a model guessing a short clip's length should not pass by luck. */
const TONE_SECONDS = 7;
/** "7", "7 seconds", "0:07" — not "17" or "70". */
const TONE_SECONDS_RE = /(?<!\d)0?7(?!\d)/;
const CHECKSUM_SENTENCE = 'The assembled bytes did not match the checksum you supplied.';
/**
 * Which form a refusal made before any body byte is read (411, 413) must take on
 * this run: `edge` (through the v1-gateway: param null, request_id null, no
 * X-Request-Id), `origin` (the orchestrator directly), or unset — either, and the
 * failure names the one seen. Pin it whenever the path is known (review finding).
 */
const EDGE_REFUSALS = (process.env.CONFORMANCE_FILES_EDGE_REFUSALS || '').trim().toLowerCase();

const FILE_ID = /^file-[0-9a-f]{24}$/;
const UPLOAD_ID = /^upload_[0-9a-f]{24}$/;
const PART_ID = /^part_[0-9a-f]{24}$/;
const FILE_STATUSES = new Set(['uploaded', 'processed', 'error']);
const PROCESSING_STATES = new Set(['queued', 'processing', 'processed', 'failed']);

const sha256 = (buf) => crypto.createHash('sha256').update(buf).digest('hex');
const sha256b64 = (buf) => crypto.createHash('sha256').update(buf).digest('base64');
const md5 = (buf) => crypto.createHash('md5').update(buf).digest('hex');
const tag = () => crypto.randomBytes(6).toString('hex');
const randomFileId = () => `file-${crypto.randomBytes(12).toString('hex')}`;
const randomUploadId = () => `upload_${crypto.randomBytes(12).toString('hex')}`;
const client0 = (apiKey) => makeClient({ maxRetries: 0, ...(apiKey ? { apiKey } : {}) }).client;
const dataUrl = (buf, mime) => `data:${mime};base64,${buf.toString('base64')}`;
const octet = (extra = {}) => ({ 'Content-Type': 'application/octet-stream', ...extra });

/** Exactly `size` bytes of text: random bytes sniff as unsupported and end in `error`. */
function textPayload(size, label) {
  const lines = [];
  let length = 0;
  for (let n = 0; length < size; n++) {
    const line = `conformance ${label} line ${String(n).padStart(8, '0')}: the quick brown fox jumps over the lazy dog.\n`;
    lines.push(line);
    length += line.length;
  }
  return Buffer.from(lines.join('')).subarray(0, size);
}

function chunks(buf, size = PART) {
  const out = [];
  for (let o = 0; o < buf.length; o += size) out.push(buf.subarray(o, Math.min(o + size, buf.length)));
  return out;
}

function requireOtherKey() {
  if (!OTHER_KEY) {
    throw new SkipTest('no key of another project: set TECHSARA_API_KEY_OTHER_PROJECT (a second test project, default scopes)');
  }
  return OTHER_KEY;
}

function requireNarrowKey() {
  if (!env.narrowKey) {
    throw new SkipTest('no narrow key: set TECHSARA_API_KEY_NARROW to a key of the same project holding only models.read');
  }
  return env.narrowKey;
}

function requireResponsesOnlyKey() {
  if (!RESPONSES_ONLY_KEY) {
    throw new SkipTest('no responses-only key: set TECHSARA_API_KEY_RESPONSES_ONLY to a key of the same project holding responses.write and no files.* scope');
  }
  return RESPONSES_ONLY_KEY;
}

function assertFile(f, { bytes, filename, purpose, sha } = {}) {
  assert.match(f.id, FILE_ID);
  assert.equal(f.object, 'file');
  assert.ok(Number.isInteger(f.bytes) && Number.isInteger(f.created_at), JSON.stringify(f));
  assert.ok(FILE_STATUSES.has(f.status), `status ${f.status} is outside the SDK literal set`);
  assert.ok('status_details' in f && 'expires_at' in f, `missing parity keys: ${Object.keys(f)}`);
  assert.ok(f.processing && PROCESSING_STATES.has(f.processing.state), `processing ${JSON.stringify(f.processing)}`);
  assert.ok(Array.isArray(f.processing.stages) && f.processing.stages.length > 0, JSON.stringify(f.processing));
  if (f.status === 'processed') assert.equal(f.processing.state, 'processed');
  if (f.status === 'error') assert.ok(f.processing.state === 'failed' && typeof f.status_details === 'string', JSON.stringify(f));
  if (bytes !== undefined) assert.equal(f.bytes, bytes);
  if (filename !== undefined) assert.equal(f.filename, filename);
  if (purpose !== undefined) assert.equal(f.purpose, purpose);
  if (sha !== undefined) assert.equal(f.sha256, sha);
  return f;
}

function assertUpload(u, { status, bytes } = {}) {
  assert.match(u.id, UPLOAD_ID);
  assert.equal(u.object, 'upload');
  assert.ok('file' in u, `no file key: ${Object.keys(u)}`);
  if (status !== undefined) assert.equal(u.status, status, JSON.stringify(u));
  if (bytes !== undefined) assert.equal(u.bytes, bytes);
  return u;
}

function assertPart(p, { uploadId, partNumber, bytes, sha } = {}) {
  assert.match(p.id, PART_ID);
  assert.equal(p.object, 'upload.part');
  assert.equal(p.upload_id, uploadId);
  assert.ok(Number.isInteger(p.part_number) && p.part_number >= 0, JSON.stringify(p));
  if (partNumber !== undefined) assert.equal(p.part_number, partNumber);
  if (bytes !== undefined) assert.equal(p.bytes, bytes);
  if (sha !== undefined) assert.equal(p.sha256, sha);
  return p;
}

/** The §9 envelope for a Files code, plus x-should-retry when it matters. Returns it minus request_id. */
function assertFilesError(err, { status, code, param, shouldRetry }) {
  assertEnvelope(err, { status, code, ...(param !== undefined ? { param } : {}) });
  if (shouldRetry !== undefined) {
    assert.equal(
      err.headers?.get('x-should-retry'),
      shouldRetry ? 'true' : 'false',
      `x-should-retry on ${code}: openai-node retries 408/409/503 by default and only this header stops it`,
    );
  }
  const { request_id: _ignored, ...rest } = err.error;
  return rest;
}

/** A 411/413 given before the body is read, in the edge or the origin form (EDGE_REFUSALS). Returns the form. */
function assertPreBodyRefusal(res, { status, code, originParam, mentions }) {
  assert.ok(['', 'edge', 'origin'].includes(EDGE_REFUSALS), `CONFORMANCE_FILES_EDGE_REFUSALS=${EDGE_REFUSALS}: use edge, origin or leave it unset`);
  assert.equal(res.status, status, res.text);
  const error = res.json?.error;
  assert.ok(error && typeof error === 'object', `not the §9 envelope: ${res.text}`);
  assert.deepEqual(Object.keys(error).sort(), ['code', 'message', 'param', 'request_id', 'type'], res.text);
  assert.deepEqual([error.code, error.type], [code, 'invalid_request_error'], res.text);
  if (mentions) assert.ok(error.message.includes(mentions), error.message);
  const form = error.request_id === null ? 'edge' : 'origin';
  if (EDGE_REFUSALS) assert.equal(form, EDGE_REFUSALS, `the ${status} came in the ${form} form: ${res.text}`);
  if (form === 'edge') {
    assert.equal(error.param, null, res.text);
    assert.ok(!res.headers['x-request-id'], 'an edge refusal carries no X-Request-Id');
  } else {
    assert.equal(error.param, originParam, res.text);
    assert.match(String(error.request_id), /^req_/);
    assert.equal(error.request_id, res.headers['x-request-id']);
  }
  return form;
}

/** 403 insufficient_scope whose sentence names the ONE scope the route needs. */
function assertInsufficientScope(err, scope) {
  const body = assertFilesError(err, { status: 403, code: 'insufficient_scope' });
  assert.ok(body.message.includes(`\`${scope}\``), `the refusal should name \`${scope}\`: ${body.message}`);
  return body;
}

const waitProcessed = (client, id) => client.files.waitForProcessing(id, { pollInterval: POLL_MS, maxWait: PROCESSING_TIMEOUT_MS });
const contentOf = async (client, id) => Buffer.from(await (await client.files.content(id)).arrayBuffer());
const deleteQuietly = (client, id) => (id ? client.files.delete(id).catch(() => undefined) : undefined);
/** Leave no pending upload behind a failed test (a completed one answers 409 here, which is fine). */
const cancelQuietly = (client, id) => (id ? client.uploads.cancel(id).catch(() => undefined) : undefined);
/** A completed upload's file is deleted; an open one is cancelled. */
const finish = async (client, uploadId, fileId) => (fileId ? deleteQuietly(client, fileId) : cancelQuietly(client, uploadId));
const putPart = (client, uploadId, n, body, headers) => client.put(`/uploads/${uploadId}/parts/${n}`, { body, headers: octet(headers) });

/** Every route that names an upload id, as `caller`. */
const uploadProbes = (caller) => [
  ['retrieve', (uid) => caller.get(`/uploads/${uid}`)],
  ['parts.create', async (uid) => caller.uploads.parts.create(uid, { data: await toFile(Buffer.from('intruder'), 'p') })],
  ['put part', (uid) => putPart(caller, uid, 0, Buffer.from('intruder'))],
  ['complete', (uid) => caller.uploads.complete(uid, { part_ids: [] })],
  ['cancel', (uid) => caller.uploads.cancel(uid)],
];

/**
 * A fetch that cuts ONE part upload off mid-body: the `cut`-th POST …/parts
 * attempt streams half its multipart bytes and then errors, so undici aborts the
 * connection mid-request — what a network drop looks like from the client.
 */
function dropsOnePart({ cut = 0 } = {}) {
  const state = { attempts: 0, cuts: 0 };
  const fetchImpl = async (url, init = {}) => {
    const target = new URL(String(url));
    if ((init.method || 'GET').toUpperCase() === 'POST' && target.pathname.endsWith('/parts')) {
      const attempt = state.attempts++;
      if (attempt === cut && state.cuts === 0) {
        state.cuts++;
        const probe = new Request(target, { method: 'POST', headers: init.headers, body: init.body });
        const bytes = Buffer.from(await probe.arrayBuffer());
        const headers = new Headers(probe.headers);
        headers.delete('content-length');
        let sent = false;
        const body = new ReadableStream({
          pull(controller) {
            if (!sent) {
              sent = true;
              controller.enqueue(bytes.subarray(0, bytes.length >> 1));
            } else {
              controller.error(new Error('conformance: the network dropped mid-part'));
            }
          },
        });
        return fetch(target, { ...init, headers, body, duplex: 'half' });
      }
    }
    return fetch(url, init);
  };
  return { fetchImpl, state };
}

/**
 * One raw HTTP exchange with node:http, for what fetch will not send. `waitMs`
 * bounds the wait for a response: a server that waits for a body the client will
 * never send must FAIL the test, not hang the run.
 */
function rawExchange(method, path, { headers = {}, write, waitMs = 30_000 } = {}) {
  const target = new URL(`${env.baseURL}${path}`);
  const lib = target.protocol === 'https:' ? https : http;
  return new Promise((resolve, reject) => {
    const req = lib.request(target, { method, headers: { authorization: `Bearer ${env.apiKey}`, ...headers } });
    req.setTimeout(waitMs, () => {
      req.destroy(new Error(`no response in ${waitMs} ms to ${method} ${path} (headers ${JSON.stringify(headers)})`));
    });
    req.on('response', (res) => {
      const parts = [];
      res.on('data', (c) => parts.push(c));
      res.on('end', () => {
        const text = Buffer.concat(parts).toString('utf8');
        let json;
        try {
          json = JSON.parse(text);
        } catch {
          json = undefined;
        }
        resolve({ status: res.statusCode, headers: res.headers, text, json, sentHeaders: req.getHeaders() });
        req.destroy();
      });
    });
    req.on('error', reject);
    write(req);
  });
}

/** Send half of a raw PUT part, then destroy the socket (no SDK involved). */
function putHalfThenDrop(path, chunk) {
  const target = new URL(`${env.baseURL}${path}`);
  const lib = target.protocol === 'https:' ? https : http;
  return new Promise((resolve) => {
    const req = lib.request(target, {
      method: 'PUT',
      headers: {
        authorization: `Bearer ${env.apiKey}`,
        'content-type': 'application/octet-stream',
        'content-length': String(chunk.length),
        'x-part-sha256': sha256(chunk),
      },
    });
    req.on('response', (res) => {
      res.resume();
      resolve(`answered ${res.statusCode} before the body ended`);
    });
    req.on('error', () => resolve('dropped'));
    req.write(chunk.subarray(0, chunk.length >> 1), () => {
      setTimeout(() => {
        req.destroy();
        resolve('dropped');
      }, 250);
    });
  });
}

describe('Files', { skip: liveSkipReason }, () => {
  itPlanned('files-api', 'files.create, retrieve, list, content and delete round-trip a text file, and a deleted id is 404 file_not_found', async () => {
    const client = client0();
    const data = Buffer.from(`conformance-node ${tag()}\n`.repeat(64));
    const created = assertFile(
      await client.files.create({ file: await toFile(data, 'note.txt', { type: 'text/plain' }), purpose: 'user_data' }),
      { bytes: data.length, filename: 'note.txt', purpose: 'user_data' },
    );
    try {
      assert.equal(created.expires_at, null);
      assertFile(await client.files.retrieve(created.id), { bytes: data.length, sha: sha256(data) });
      const ids = [];
      for await (const f of client.files.list({ purpose: 'user_data', limit: 100 })) ids.push(f.id);
      assert.ok(ids.includes(created.id), 'the new file is listed');
      assert.ok((await contentOf(client, created.id)).equals(data), 'content bytes');
    } finally {
      const deleted = await client.files.delete(created.id);
      assert.deepEqual({ id: deleted.id, object: deleted.object, deleted: deleted.deleted }, { id: created.id, object: 'file', deleted: true });
    }
    assertFilesError(await rejection(client.files.retrieve(created.id)), { status: 404, code: 'file_not_found' });
    assertFilesError(await rejection(client.files.delete(created.id)), { status: 404, code: 'file_not_found' });
  });

  itPlanned('files-api', 'waitForProcessing returns a text file processed, with kind text, every stage done and mime_type sniffed', async () => {
    const client = client0();
    const data = textPayload(3 * KIB, tag());
    const created = await client.files.create({ file: await toFile(data, 'processed.txt', { type: 'text/plain' }), purpose: 'user_data' });
    try {
      const done = assertFile(await waitProcessed(client, created.id), { bytes: data.length, sha: sha256(data) });
      assert.equal(done.status, 'processed');
      assert.equal(done.status_details, null);
      assert.equal(done.processing.kind, 'text');
      assert.equal(done.processing.percent, 100);
      assert.ok(done.processing.stages.every((s) => ['done', 'skipped'].includes(s.status)), JSON.stringify(done.processing.stages));
      assert.equal(done.mime_type, 'text/plain');
    } finally {
      await deleteQuietly(client, created.id);
    }
  });

  itPlanned('files-api', 'expires_after sets expires_at to created_at + seconds, and seconds below 3600 is 400 naming it', async () => {
    const client = client0();
    const created = await client.files.create({
      file: await toFile(Buffer.from('an hour\n'), 'expiring.txt', { type: 'text/plain' }),
      purpose: 'user_data',
      expires_after: { anchor: 'created_at', seconds: 3600 },
    });
    try {
      assert.equal(created.expires_at - created.created_at, 3600);
      const err = await rejection(client.files.create({
        file: await toFile(Buffer.from('x'), 'x.txt', { type: 'text/plain' }),
        purpose: 'user_data',
        expires_after: { anchor: 'created_at', seconds: 60 },
      }));
      assertFilesError(err, { status: 400, code: 'invalid_request_error', param: 'expires_after.seconds' });
    } finally {
      await deleteQuietly(client, created.id);
    }
  });

  itPlanned('files-api', 'a purpose the platform has no product for (fine-tune) is 400 with param purpose', async () => {
    const err = await rejection(client0().files.create({ file: await toFile(Buffer.from('{}\n'), 'x.jsonl'), purpose: 'fine-tune' }));
    assertFilesError(err, { status: 400, code: 'invalid_request_error', param: 'purpose' });
  });

  itPlanned('files-api', 'files.list pages newest first with limit and after, and has_more ends the paging', async () => {
    // Only this test's three files are asserted on: the project may hold other
    // callers' files, created before, between or after ours (review finding).
    const client = client0();
    const label = tag();
    const made = [];
    try {
      for (let n = 0; n < 3; n++) {
        made.push((await client.files.create({ file: await toFile(Buffer.from(`${label} ${n}\n`), `page-${n}.txt`, { type: 'text/plain' }), purpose: 'user_data' })).id);
      }
      const seen = [];
      let newestPageHasMore;
      let after;
      for (let guard = 0; guard < 200; guard++) {
        const page = await client.files.list({ limit: 2, order: 'desc', ...(after ? { after } : {}) });
        const ids = page.data.map((f) => f.id);
        assert.ok(ids.length <= 2, `limit=2 answered ${ids.length} files`);
        if (ids.includes(made[2])) newestPageHasMore = page.has_more;
        seen.push(...ids.filter((id) => made.includes(id)));
        if (seen.length === 3 || !page.has_more || ids.length === 0) break;
        after = ids[ids.length - 1];
      }
      assert.deepEqual(seen, [made[2], made[1], made[0]], 'newest first');
      assert.equal(newestPageHasMore, true, 'the page holding the newest file said there was nothing after it');
      const older = (await client.files.list({ limit: 2, order: 'desc', after: made[1] })).data.map((f) => f.id);
      assert.ok(older.includes(made[0]) && !older.includes(made[1]) && !older.includes(made[2]), JSON.stringify(older));
      const newer = (await client.files.list({ limit: 100, order: 'asc', after: made[0] })).data.map((f) => f.id);
      assert.ok(newer.includes(made[1]) && newer.includes(made[2]) && newer.indexOf(made[1]) < newer.indexOf(made[2]), JSON.stringify(newer));
      assert.ok(!newer.includes(made[0]), JSON.stringify(newer));
      assert.equal((await client.files.list({ limit: 10000, order: 'asc', after: made[2] })).has_more, false);
      assertFilesError(await rejection(client.files.list({ limit: 0 })), { status: 400, code: 'invalid_request_error', param: 'limit' });
    } finally {
      for (const id of made) await deleteQuietly(client, id);
    }
  });

  itPlanned('files-api', 'content honours one Range with 206, its own ETag with 304, a range past the end with 416, and the download headers', async () => {
    const client = client0();
    const data = textPayload(10 * KIB, tag());
    const { id } = await client.files.create({ file: await toFile(data, 'ranged.txt', { type: 'text/plain' }), purpose: 'user_data' });
    try {
      const whole = await raw('GET', `/files/${id}/content`);
      assert.equal(whole.status, 200);
      assert.equal(whole.text, data.toString());
      assert.equal(whole.headers.get('etag'), `"${sha256(data)}"`);
      assert.equal(whole.headers.get('accept-ranges'), 'bytes');
      assert.equal(whole.headers.get('x-content-type-options'), 'nosniff');
      assert.match(whole.headers.get('content-disposition') || '', /^attachment;/);
      assert.match(whole.headers.get('content-security-policy') || '', /sandbox/);
      assert.match(whole.headers.get('cache-control') || '', /no-store/);
      const part = await raw('GET', `/files/${id}/content`, { headers: { range: 'bytes=100-1123' } });
      assert.equal(part.status, 206);
      assert.equal(part.text, data.subarray(100, 1124).toString());
      assert.equal(part.headers.get('content-range'), `bytes 100-1123/${data.length}`);
      const cached = await raw('GET', `/files/${id}/content`, { headers: { 'if-none-match': `"${sha256(data)}"` } });
      assert.equal(cached.status, 304);
      assert.equal(cached.text, '');
      const past = await raw('GET', `/files/${id}/content`, { headers: { range: `bytes=${data.length}-` } });
      assert.equal(past.status, 416);
      assert.equal(past.headers.get('content-range'), `bytes */${data.length}`);
      assert.equal(past.json?.error?.code, 'invalid_request_error');
      assert.equal(past.json?.error?.param, 'Range');
      assert.equal(past.json?.error?.request_id, past.headers.get('x-request-id'));
    } finally {
      await deleteQuietly(client, id);
    }
  });

  itPlanned('files-api', 'a deleted, a random and a malformed file id all get one 404 file_not_found body that never echoes the id', async () => {
    const client = client0();
    const { id } = await client.files.create({ file: await toFile(Buffer.from('soon gone\n'), 'gone.txt', { type: 'text/plain' }), purpose: 'user_data' });
    await client.files.delete(id);
    const bodies = [];
    for (const probe of [id, randomFileId(), 'file-not-a-real-id']) {
      const err = await rejection(client.files.retrieve(probe));
      const body = assertFilesError(err, { status: 404, code: 'file_not_found' });
      assert.ok(!JSON.stringify(err.error).includes(probe), 'the id is echoed');
      bodies.push(body);
    }
    assert.deepEqual(bodies[1], bodies[0]);
    assert.deepEqual(bodies[2], bodies[0]);
  });

  itPlanned('files-api', 'an Idempotency-Key header on a file route is refused with 400 naming it', async () => {
    const err = await rejection(client0().files.create(
      { file: await toFile(Buffer.from('k\n'), 'k.txt', { type: 'text/plain' }), purpose: 'user_data' },
      { headers: { 'Idempotency-Key': `conformance-${tag()}` } },
    ));
    assertFilesError(err, { status: 400, code: 'invalid_request_error', param: 'Idempotency-Key' });
  });

  itPlanned('files-api', "another project's file id answers retrieve, content and delete exactly like an id that never existed", async () => {
    const other = client0(requireOtherKey()); // before anything is created: a SKIP leaves nothing
    const client = client0();
    const data = Buffer.from(`private ${tag()}\n`);
    const { id } = await client.files.create({ file: await toFile(data, 'private.txt', { type: 'text/plain' }), purpose: 'user_data' });
    try {
      const never = randomFileId();
      for (const [name, call] of [
        ['retrieve', (fid) => other.files.retrieve(fid)],
        ['content', (fid) => other.files.content(fid)],
        ['delete', (fid) => other.files.delete(fid)],
      ]) {
        const foreign = assertFilesError(await rejection(call(id)), { status: 404, code: 'file_not_found' });
        const unknown = assertFilesError(await rejection(call(never)), { status: 404, code: 'file_not_found' });
        assert.deepEqual(foreign, unknown, name);
      }
      const listed = await other.files.list({ limit: 10000 });
      assert.ok(!listed.data.some((f) => f.id === id), 'a foreign file is listed');
      assert.ok((await contentOf(client, id)).equals(data), 'the owner lost the file to a foreign DELETE');
    } finally {
      await deleteQuietly(client, id);
    }
  });

  itPlanned('files-api', 'a key without the files scopes gets the same 403 for a real and a random file id on every file route', async () => {
    const limited = client0(requireNarrowKey()); // before anything is created
    const client = client0();
    const data = Buffer.from(`scoped ${tag()}\n`);
    const { id } = await client.files.create({ file: await toFile(data, 'scoped.txt', { type: 'text/plain' }), purpose: 'user_data' });
    try {
      assertInsufficientScope(await rejection(limited.files.create({
        file: await toFile(Buffer.from('x\n'), 'x.txt', { type: 'text/plain' }), purpose: 'user_data',
      })), 'files.write');
      assertInsufficientScope(await rejection(limited.files.list()), 'files.read');
      const never = randomFileId();
      for (const [name, scope, call] of [
        ['retrieve', 'files.read', (fid) => limited.files.retrieve(fid)],
        ['content', 'files.read', (fid) => limited.files.content(fid)],
        ['delete', 'files.write', (fid) => limited.files.delete(fid)],
      ]) {
        const real = assertInsufficientScope(await rejection(call(id)), scope);
        const unknown = assertInsufficientScope(await rejection(call(never)), scope);
        assert.deepEqual(real, unknown, `${name}: a real id got another 403`);
      }
      assert.ok((await contentOf(client, id)).equals(data), 'a refused DELETE removed the file');
    } finally {
      await deleteQuietly(client, id);
    }
  });
});

describe('Uploads', { skip: liveSkipReason }, () => {
  itPlanned('uploads-chunked', 'parts carrying their sha256 complete in part_ids order with md5, return a nested file, and a wrong sha256 is 400 checksum_mismatch', async () => {
    const client = client0();
    const data = textPayload(2 * PART + 10 * KIB, tag());
    const pieces = chunks(data);
    const upload = assertUpload(
      await client.uploads.create({ bytes: data.length, filename: 'parts.txt', mime_type: 'text/plain', purpose: 'user_data' }),
      { status: 'pending', bytes: data.length },
    );
    let fileId;
    try {
      assert.equal(upload.file, null);
      const ids = [];
      for (const index of [1, 2, 0]) {
        const part = await client.uploads.parts.create(upload.id, { data: await toFile(pieces[index], 'part'), sha256: sha256(pieces[index]) });
        ids[index] = assertPart(part, { uploadId: upload.id, bytes: pieces[index].length, sha: sha256(pieces[index]) }).id;
      }
      const mismatch = await rejection(client.uploads.parts.create(upload.id, { data: await toFile(pieces[0], 'part'), sha256: '0'.repeat(64) }));
      assertFilesError(mismatch, { status: 400, code: 'checksum_mismatch', param: 'sha256', shouldRetry: false });
      const done = assertUpload(await client.uploads.complete(upload.id, { part_ids: ids, md5: md5(data) }), { status: 'completed' });
      const nested = assertFile(done.file, { bytes: data.length, filename: 'parts.txt', purpose: 'user_data' });
      fileId = nested.id;
      const early = await raw('GET', `/files/${nested.id}/content`);
      if (early.status !== 200) {
        assert.equal(early.status, 409, early.text);
        assert.equal(early.json?.error?.code, 'file_not_ready');
        assert.ok(early.headers.get('retry-after'), 'file_not_ready without Retry-After');
        assert.equal(early.headers.get('x-should-retry'), 'true');
      }
      assert.equal(assertFile(await waitProcessed(client, nested.id), { bytes: data.length, sha: sha256(data) }).status, 'processed');
      assert.ok((await contentOf(client, nested.id)).equals(data), 'assembled bytes');
    } finally {
      await finish(client, upload.id, fileId);
    }
  });

  itPlanned('uploads-chunked', 'complete with parts that do not add up to the declared bytes is 400 naming part_ids, and the upload stays open', async () => {
    const client = client0();
    const data = textPayload(PART, tag());
    const upload = await client.uploads.create({ bytes: data.length + 1, filename: 'short.txt', mime_type: 'text/plain', purpose: 'user_data' });
    let fileId;
    try {
      const first = await client.uploads.parts.create(upload.id, { data: await toFile(data, 'p') });
      assertFilesError(await rejection(client.uploads.complete(upload.id, { part_ids: [first.id] })), { status: 400, code: 'invalid_request_error', param: 'part_ids' });
      const last = await client.uploads.parts.create(upload.id, { data: await toFile(Buffer.from('\n'), 'p') });
      const done = assertUpload(await client.uploads.complete(upload.id, { part_ids: [first.id, last.id] }), { status: 'completed' });
      fileId = done.file.id;
      assert.equal(done.file.bytes, data.length + 1);
    } finally {
      await finish(client, upload.id, fileId);
    }
  });

  itPlanned('uploads-chunked', 'complete with a wrong md5 or a wrong sha256 returns the file, which then ends in error with checksum_mismatch', async () => {
    const client = client0();
    for (const [field, wrong] of [['md5', '0'.repeat(32)], ['sha256', '0'.repeat(64)]]) {
      const data = textPayload(PART, tag());
      const upload = await client.uploads.create({ bytes: data.length, filename: `${field}.txt`, mime_type: 'text/plain', purpose: 'user_data' });
      let fileId;
      try {
        const part = await client.uploads.parts.create(upload.id, { data: await toFile(data, 'p') });
        const done = assertUpload(await client.uploads.complete(upload.id, { part_ids: [part.id], [field]: wrong }), { status: 'completed' });
        fileId = assertFile(done.file, { bytes: data.length }).id;
        const ended = assertFile(await waitProcessed(client, fileId));
        assert.equal(ended.status, 'error', `a wrong ${field} must fail the file`);
        assert.equal(ended.processing.error?.code, 'checksum_mismatch', `${field}: ${JSON.stringify(ended.processing)}`);
        assert.match(ended.status_details || '', /checksum/);
      } finally {
        await finish(client, upload.id, fileId);
      }
    }
  });

  itPlanned('uploads-chunked', 'a replayed complete returns the same upload and file, and a late part is 409 upload_state_conflict with x-should-retry false', async () => {
    const client = client0();
    const data = textPayload(PART >> 1, tag());
    const upload = await client.uploads.create({ bytes: data.length, filename: 'replay.txt', mime_type: 'text/plain', purpose: 'user_data' });
    let fileId;
    try {
      const part = await client.uploads.parts.create(upload.id, { data: await toFile(data, 'p') });
      const first = assertUpload(await client.uploads.complete(upload.id, { part_ids: [part.id] }), { status: 'completed' });
      fileId = first.file.id;
      const again = assertUpload(await client.uploads.complete(upload.id, { part_ids: [part.id] }), { status: 'completed' });
      assert.deepEqual([again.id, again.file.id], [first.id, first.file.id]);
      const late = await rejection(client.uploads.parts.create(upload.id, { data: await toFile(Buffer.from('late'), 'p') }));
      assertFilesError(late, { status: 409, code: 'upload_state_conflict', shouldRetry: false });
      assertFilesError(await rejection(client.uploads.cancel(upload.id)), { status: 409, code: 'upload_state_conflict' });
    } finally {
      await finish(client, upload.id, fileId);
    }
  });

  itPlanned('uploads-chunked', 'cancel is idempotent and a cancelled upload refuses parts and complete with 409', async () => {
    const client = client0();
    const upload = await client.uploads.create({ bytes: PART, filename: 'cancel.txt', mime_type: 'text/plain', purpose: 'user_data' });
    try {
      const part = await client.uploads.parts.create(upload.id, { data: await toFile(Buffer.from('half\n'), 'p') });
      assert.equal(assertUpload(await client.uploads.cancel(upload.id), { status: 'cancelled' }).file, null);
      assertUpload(await client.uploads.cancel(upload.id), { status: 'cancelled' });
      assertFilesError(await rejection(client.uploads.parts.create(upload.id, { data: await toFile(Buffer.from('more'), 'p') })), { status: 409, code: 'upload_state_conflict', shouldRetry: false });
      assertFilesError(await rejection(client.uploads.complete(upload.id, { part_ids: [part.id] })), { status: 409, code: 'upload_state_conflict', shouldRetry: false });
    } finally {
      await cancelQuietly(client, upload.id);
    }
  });

  itPlanned('uploads-chunked', "another project's upload id answers retrieve, parts, put, complete and cancel exactly like an id that never existed", async () => {
    const other = client0(requireOtherKey()); // before anything is created: a SKIP leaves nothing
    const client = client0();
    const data = textPayload(KIB, tag());
    const upload = await client.uploads.create({ bytes: data.length, filename: 'mine.txt', mime_type: 'text/plain', purpose: 'user_data' });
    let fileId;
    try {
      // The owner's part goes in FIRST, so a foreign write has something to clobber.
      const part = await client.uploads.parts.create(upload.id, { data: await toFile(data, 'p') });
      const never = randomUploadId();
      for (const [name, call] of uploadProbes(other)) {
        const foreign = assertFilesError(await rejection(call(upload.id)), { status: 404, code: 'upload_not_found' });
        const unknown = assertFilesError(await rejection(call(never)), { status: 404, code: 'upload_not_found' });
        assert.deepEqual(foreign, unknown, name);
      }
      const done = assertUpload(await client.uploads.complete(upload.id, { part_ids: [part.id], md5: md5(data) }), { status: 'completed' });
      fileId = done.file.id;
      assert.equal(done.file.bytes, data.length, "the foreign calls changed the owner's upload");
      assert.equal((await waitProcessed(client, fileId)).status, 'processed');
      assert.ok((await contentOf(client, fileId)).equals(data), "a foreign part write reached the owner's bytes");
    } finally {
      await finish(client, upload.id, fileId);
    }
  });

  itPlanned('uploads-chunked', 'a key without files.write gets the same 403 for a real and a random upload id on every upload route', async () => {
    const limited = client0(requireNarrowKey()); // before anything is created
    const client = client0();
    const data = textPayload(KIB, tag());
    const upload = await client.uploads.create({ bytes: data.length, filename: 'scoped.txt', mime_type: 'text/plain', purpose: 'user_data' });
    let fileId;
    try {
      const part = await client.uploads.parts.create(upload.id, { data: await toFile(data, 'p') });
      assertInsufficientScope(await rejection(limited.uploads.create({ bytes: 1, filename: 'x.txt', mime_type: 'text/plain', purpose: 'user_data' })), 'files.write');
      const never = randomUploadId();
      for (const [name, call] of uploadProbes(limited)) {
        const real = assertInsufficientScope(await rejection(call(upload.id)), 'files.write');
        const unknown = assertInsufficientScope(await rejection(call(never)), 'files.write');
        assert.deepEqual(real, unknown, `${name}: a real id got another 403`);
      }
      const done = assertUpload(await client.uploads.complete(upload.id, { part_ids: [part.id], md5: md5(data) }), { status: 'completed' });
      fileId = done.file.id;
      assert.equal((await waitProcessed(client, fileId)).status, 'processed');
      assert.ok((await contentOf(client, fileId)).equals(data), "a refused call changed the owner's upload");
    } finally {
      await finish(client, upload.id, fileId);
    }
  });
});

describe('Uploads: resume', { skip: liveSkipReason }, () => {
  itPlanned('uploads-chunked', 'a part cut off by a dropped connection is retried by the SDK and takes no part number of its own', async () => {
    const client = client0();
    const { fetchImpl, state } = dropsOnePart({ cut: 1 });
    const retrying = makeClient({ maxRetries: 2, clientOptions: { fetch: fetchImpl } }).client;
    const data = textPayload(2 * PART + 999, tag());
    const pieces = chunks(data);
    const upload = await retrying.uploads.create({ bytes: data.length, filename: 'dropped.txt', mime_type: 'text/plain', purpose: 'user_data' });
    let fileId;
    try {
      const ids = [];
      for (const piece of pieces) ids.push((await retrying.uploads.parts.create(upload.id, { data: await toFile(piece, 'part') })).id);
      assert.deepEqual([state.cuts, state.attempts], [1, pieces.length + 1], 'the drop did not happen as arranged');
      const held = await client.get(`/uploads/${upload.id}`);
      // Had the cut attempt been recorded, its retry would hold number 2.
      assert.deepEqual(held.parts.map((p) => p.part_number), pieces.map((_, n) => n));
      const done = await client.uploads.complete(upload.id, { part_ids: ids, md5: md5(data) });
      fileId = done.file.id;
      assert.equal((await waitProcessed(client, fileId)).status, 'processed');
      assert.ok((await contentOf(client, fileId)).equals(data));
    } finally {
      await finish(client, upload.id, fileId);
    }
  });

  itPlanned('uploads-chunked', 'an upload cut by a dropped connection resumes in a fresh client from GET /uploads/{id}, which lists only whole parts', async () => {
    const data = textPayload(3 * PART + 4321, tag());
    const pieces = chunks(data);
    const first = client0();
    const upload = await first.uploads.create({ bytes: data.length, filename: 'resumed.txt', mime_type: 'text/plain', purpose: 'user_data' });
    let fileId;
    try {
      await putPart(first, upload.id, 0, pieces[0], { 'X-Part-SHA256': sha256(pieces[0]) });
      assert.equal(await putHalfThenDrop(`/uploads/${upload.id}/parts/1`, pieces[1]), 'dropped');

      // "The client restarted": a new client that knows only the upload id.
      const fresh = client0();
      const state = assertUpload(await fresh.get(`/uploads/${upload.id}`), { status: 'pending', bytes: data.length });
      assert.deepEqual(state.parts.map((p) => p.part_number), [0], `a cut part must record nothing: ${JSON.stringify(state.parts)}`);
      assertPart(state.parts[0], { uploadId: upload.id, partNumber: 0, bytes: pieces[0].length, sha: sha256(pieces[0]) });
      assert.equal(state.bytes_received, pieces[0].length);
      const have = new Set(state.parts.map((p) => p.part_number));
      for (let n = 0; n < pieces.length; n++) {
        if (have.has(n)) continue;
        await putPart(fresh, upload.id, n, pieces[n], { 'X-Part-SHA256': sha256(pieces[n]) });
      }
      const done = assertUpload(await fresh.uploads.complete(upload.id, { md5: md5(data) }), { status: 'completed' });
      fileId = done.file.id;
      assert.equal((await waitProcessed(fresh, fileId)).status, 'processed');
      assert.ok((await contentOf(fresh, fileId)).equals(data));
    } finally {
      await finish(first, upload.id, fileId);
    }
  });

  itPlanned('uploads-chunked', 'resending a part_number replaces that part under the same part id, and an unnumbered part then is 400 naming part_number', async () => {
    const client = client0();
    const data = textPayload(PART, tag());
    const upload = await client.uploads.create({ bytes: data.length, filename: 'retry.txt', mime_type: 'text/plain', purpose: 'user_data' });
    let fileId;
    try {
      const firstPart = assertPart(
        await client.uploads.parts.create(upload.id, { data: await toFile(textPayload(PART, 'stale'), 'p'), part_number: 0 }),
        { uploadId: upload.id, partNumber: 0 },
      );
      const again = assertPart(
        await client.uploads.parts.create(upload.id, { data: await toFile(data, 'p'), part_number: 0 }),
        { uploadId: upload.id, partNumber: 0, sha: sha256(data) },
      );
      assert.equal(again.id, firstPart.id, 'a lost-acknowledgement retry must not leave an orphan part');
      const state = await client.get(`/uploads/${upload.id}`);
      assert.deepEqual(state.parts.map((p) => [p.id, p.sha256]), [[firstPart.id, sha256(data)]]);
      const mixed = await rejection(client.uploads.parts.create(upload.id, { data: await toFile(Buffer.from('unnumbered'), 'p') }));
      assertFilesError(mixed, { status: 400, code: 'invalid_request_error', param: 'part_number' });
      const done = assertUpload(await client.uploads.complete(upload.id, { part_ids: [again.id], md5: md5(data) }), { status: 'completed' });
      fileId = done.file.id;
      assert.equal((await waitProcessed(client, fileId)).status, 'processed');
      assert.ok((await contentOf(client, fileId)).equals(data));
    } finally {
      await finish(client, upload.id, fileId);
    }
  });

  itPlanned('uploads-chunked', 'raw PUT parts with X-Part-SHA256 or Content-Digest complete without part_ids in part_number order, and a wrong digest records nothing', async () => {
    const client = client0();
    const data = textPayload(2 * PART + 77, tag());
    const pieces = chunks(data);
    const upload = await client.uploads.create({ bytes: data.length, filename: 'raw.txt', mime_type: 'text/plain', purpose: 'user_data' });
    let fileId;
    try {
      for (const n of [2, 0]) {
        assertPart(await putPart(client, upload.id, n, pieces[n], { 'X-Part-SHA256': sha256(pieces[n]) }),
          { uploadId: upload.id, partNumber: n, bytes: pieces[n].length, sha: sha256(pieces[n]) });
      }
      assertFilesError(await rejection(putPart(client, upload.id, 1, pieces[1], { 'X-Part-SHA256': 'f'.repeat(64) })),
        { status: 400, code: 'checksum_mismatch', param: 'sha256', shouldRetry: false });
      const wrongDigest = sha256b64(Buffer.from('not these bytes'));
      assertFilesError(await rejection(putPart(client, upload.id, 1, pieces[1], { 'Content-Digest': `sha-256=:${wrongDigest}:` })),
        { status: 400, code: 'checksum_mismatch', param: 'sha256', shouldRetry: false });
      assert.deepEqual((await client.get(`/uploads/${upload.id}`)).parts.map((p) => p.part_number), [0, 2], 'a refused part was recorded');
      assertFilesError(await rejection(client.post(`/uploads/${upload.id}/complete`, { body: {} })),
        { status: 400, code: 'invalid_request_error', param: 'part_ids' });
      assertPart(await putPart(client, upload.id, 1, pieces[1], { 'Content-Digest': `sha-256=:${sha256b64(pieces[1])}:` }),
        { uploadId: upload.id, partNumber: 1, sha: sha256(pieces[1]) });
      const done = assertUpload(await client.post(`/uploads/${upload.id}/complete`, { body: { sha256: sha256(data) } }), { status: 'completed' });
      fileId = done.file.id;
      const ready = assertFile(await waitProcessed(client, fileId), { bytes: data.length, sha: sha256(data) });
      assert.equal(ready.status, 'processed');
      assert.ok((await contentOf(client, fileId)).equals(data));
    } finally {
      await finish(client, upload.id, fileId);
    }
  });

  itPlanned('uploads-chunked', 'a raw part sent without Content-Length is 411 and records nothing', async () => {
    const client = client0();
    const upload = await client.uploads.create({ bytes: KIB, filename: 'nolength.txt', mime_type: 'text/plain', purpose: 'user_data' });
    try {
      const res = await rawExchange('PUT', `/uploads/${upload.id}/parts/0`, {
        headers: { 'content-type': 'application/octet-stream', 'transfer-encoding': 'chunked' },
        write: (req) => req.end(Buffer.alloc(KIB, 'x')),
      });
      assert.ok(!('content-length' in res.sentHeaders), 'node sent a length, so this proves nothing');
      assertPreBodyRefusal(res, { status: 411, code: 'invalid_request_error', originParam: 'Content-Length', mentions: 'Content-Length' });
      assert.deepEqual((await client.get(`/uploads/${upload.id}`)).parts, []);
    } finally {
      await cancelQuietly(client, upload.id);
    }
  });

  itPlanned('uploads-chunked', 'a raw part declaring more bytes than a part may hold is 413 before any byte is sent, and records nothing', async () => {
    const client = client0();
    const upload = await client.uploads.create({ bytes: KIB, filename: 'huge.txt', mime_type: 'text/plain', purpose: 'user_data' });
    try {
      // Headers only: 1 TiB declared, not one body byte. A handler that reads
      // before checking the declared length never answers: that is a failure
      // after 30 s, not a hang.
      const res = await rawExchange('PUT', `/uploads/${upload.id}/parts/0`, {
        headers: { 'content-type': 'application/octet-stream', 'content-length': String(2 ** 40) },
        write: (req) => req.flushHeaders(),
      }).catch((err) => assert.fail(`the server did not refuse a part declaring 1 TiB before its body: ${err.message}`));
      assertPreBodyRefusal(res, { status: 413, code: 'request_too_large', originParam: null });
      assert.deepEqual((await client.get(`/uploads/${upload.id}`)).parts, []);
    } finally {
      await cancelQuietly(client, upload.id);
    }
  });
});

describe('Files as model input', { skip: liveSkipReason }, () => {
  const question = (text, part) => [{ role: 'user', content: [{ type: 'input_text', text }, part] }];
  const chatQuestion = (text, part) => [{ role: 'user', content: [{ type: 'text', text }, part] }];

  itPlanned('file-input', 'an uploaded text file is read by the model as input_file on /v1/responses, in auto and full file_context, and an unknown mode is 400', async () => {
    const client = client0();
    const codeword = `pelican-${crypto.randomBytes(4).toString('hex')}`;
    const f = await client.files.create({ file: await toFile(Buffer.from(`The code word is ${codeword}.\n`), 'codeword.txt', { type: 'text/plain' }), purpose: 'user_data' });
    try {
      assert.equal((await waitProcessed(client, f.id)).status, 'processed');
      const ask = 'What is the code word in the file? Reply with the code word only.';
      const part = { type: 'input_file', file_id: f.id };
      const r = await client.responses.create({ model: env.chatModel, input: question(ask, part), max_output_tokens: 32, temperature: 0 });
      assert.equal(r.status, 'completed');
      assert.match(r.output_text, new RegExp(codeword));
      const full = await client.responses.create({ model: env.chatModel, input: question(ask, part), max_output_tokens: 32, temperature: 0, file_context: { mode: 'full' } });
      assert.match(full.output_text, new RegExp(codeword), 'file_context full');
      const bad = await rejection(client.responses.create({ model: env.chatModel, input: question(ask, part), max_output_tokens: 32, file_context: { mode: 'everything' } }));
      assertFilesError(bad, { status: 400, code: 'invalid_request_error', param: 'file_context.mode' });
    } finally {
      await deleteQuietly(client, f.id);
    }
  });

  itPlanned('file-input', 'an uploaded text file is read by the model as a file part on /v1/chat/completions', async () => {
    const client = client0();
    const codeword = `heron-${crypto.randomBytes(4).toString('hex')}`;
    const f = await client.files.create({ file: await toFile(Buffer.from(`The meeting codeword is ${codeword}.\n`), 'minutes.txt', { type: 'text/plain' }), purpose: 'user_data' });
    try {
      assert.equal((await waitProcessed(client, f.id)).status, 'processed');
      const completion = await client.chat.completions.create({
        model: env.chatModel, max_tokens: 32, temperature: 0,
        messages: chatQuestion('What is the meeting codeword in the file? Reply with the codeword only.', { type: 'file', file: { file_id: f.id } }),
      });
      assert.match(completion.choices[0].message.content || '', new RegExp(codeword));
    } finally {
      await deleteQuietly(client, f.id);
    }
  });

  itPlanned('file-input', 'inline file_data text is read by the model on both routes without an upload', async () => {
    const client = client0();
    const codeword = `otter-${crypto.randomBytes(4).toString('hex')}`;
    const url = dataUrl(Buffer.from(`The inline codeword is ${codeword}.\n`), 'text/plain');
    const ask = 'What is the inline codeword in the file? Reply with the codeword only.';
    const r = await client.responses.create({
      model: env.chatModel, max_output_tokens: 32, temperature: 0,
      input: question(ask, { type: 'input_file', filename: 'inline.txt', file_data: url }),
    });
    assert.equal(r.status, 'completed');
    assert.match(r.output_text, new RegExp(codeword));
    const completion = await client.chat.completions.create({
      model: env.chatModel, max_tokens: 32, temperature: 0,
      messages: chatQuestion(ask, { type: 'file', file: { filename: 'inline.txt', file_data: url } }),
    });
    assert.match(completion.choices[0].message.content || '', new RegExp(codeword));
  });

  itPlanned('file-input', 'an uploaded PNG is seen by the model as input_image with its file_id', async () => {
    const client = client0();
    const f = await client.files.create({ file: await toFile(solidPng(64, [220, 20, 20]), 'red.png', { type: 'image/png' }), purpose: 'vision' });
    try {
      const done = await waitProcessed(client, f.id);
      assert.equal(done.status, 'processed');
      assert.equal(done.processing.kind, 'image');
      const r = await client.responses.create({
        model: env.chatModel,
        input: question('What single colour fills this image? Answer with one word.', { type: 'input_image', file_id: f.id, detail: 'auto' }),
        max_output_tokens: 16,
        temperature: 0,
      });
      assert.match(r.output_text.toLowerCase(), /red/);
    } finally {
      await deleteQuietly(client, f.id);
    }
  });

  itPlanned('file-input', 'an uploaded WAV is processed as audio and the model reads its length from it as input_file and input_video', async () => {
    const client = client0();
    const f = await client.files.create({ file: await toFile(toneWav(TONE_SECONDS), 'tone.wav', { type: 'audio/wav' }), purpose: 'user_data' });
    try {
      const done = await waitProcessed(client, f.id);
      assert.equal(done.status, 'processed', done.status_details);
      assert.equal(done.processing.kind, 'audio');
      assert.ok(Math.abs(Number(done.processing.facts?.duration_s) - TONE_SECONDS) < 0.2, JSON.stringify(done.processing.facts));
      const ask = 'How many seconds long is the recording in the attached file? Reply with the number only.';
      const bare = await client.responses.create({ model: env.chatModel, max_output_tokens: 32, temperature: 0, input: [{ role: 'user', content: [{ type: 'input_text', text: ask }] }] });
      assert.ok(!TONE_SECONDS_RE.test(bare.output_text), `without the file the model already answered ${JSON.stringify(bare.output_text)}: the check below would prove nothing`);
      for (const type of ['input_file', 'input_video']) {
        const r = await client.responses.create({ model: env.chatModel, max_output_tokens: 32, temperature: 0, input: question(ask, { type, file_id: f.id }) });
        assert.equal(r.status, 'completed');
        assert.ok(
          TONE_SECONDS_RE.test(r.output_text),
          `${type}: asked the length of a ${TONE_SECONDS} s recording, the model answered ${JSON.stringify(r.output_text)}; the recording's block did not reach it`,
        );
      }
    } finally {
      await deleteQuietly(client, f.id);
    }
  });

  itPlanned('file-input', "input_audio on /v1/chat/completions refuses an unknown format by name and puts a WAV clip's transcript into the prompt", async () => {
    const client = client0();
    const clip = toneWav(2).toString('base64');
    const bad = await rejection(client.chat.completions.create({
      model: env.chatModel,
      max_tokens: 16,
      messages: [{ role: 'user', content: [{ type: 'input_audio', input_audio: { data: clip, format: 'ogg' } }] }],
    }));
    assertFilesError(bad, { status: 400, code: 'invalid_request_error', param: 'messages.0.content.0.input_audio.format' });
    const ask = 'Describe what can be heard in this recording in one sentence.';
    const bare = await client.chat.completions.create({ model: env.chatModel, max_tokens: 16, messages: [{ role: 'user', content: [{ type: 'text', text: ask }] }] });
    const completion = await client.chat.completions.create({
      model: env.chatModel,
      max_tokens: 16,
      messages: chatQuestion(ask, { type: 'input_audio', input_audio: { data: clip, format: 'wav' } }),
    });
    assert.ok((completion.choices[0].message.content || '').trim().length > 0, JSON.stringify(completion));
    const tokens = (usage) => {
      assert.ok(usage && Number.isInteger(usage.prompt_tokens) && usage.prompt_tokens > 0, `usage ${JSON.stringify(usage)}: this test needs the prompt count`);
      return usage.prompt_tokens;
    };
    const grew = tokens(completion.usage) - tokens(bare.usage);
    assert.ok(grew >= MIN_MEDIA_BLOCK_TOKENS, `the prompt grew by ${grew} tokens over the bare question; the clip's transcript did not reach the model`);
  });

  itPlanned('file-input', 'a failed file and a file of the wrong kind are refused as model input with 400 naming the file_id', async () => {
    const client = client0();
    const data = textPayload(KIB, tag());
    const upload = await client.uploads.create({ bytes: data.length, filename: 'broken.txt', mime_type: 'text/plain', purpose: 'user_data' });
    let failedId;
    let textId;
    try {
      const part = await client.uploads.parts.create(upload.id, { data: await toFile(data, 'p') });
      failedId = (await client.uploads.complete(upload.id, { part_ids: [part.id], md5: '0'.repeat(32) })).file.id;
      const ended = await waitProcessed(client, failedId);
      assert.ok(ended.status === 'error' && ended.processing.error?.code === 'checksum_mismatch', JSON.stringify(ended.processing));
      textId = (await client.files.create({ file: await toFile(Buffer.from('just some text\n'), 'plain.txt', { type: 'text/plain' }), purpose: 'user_data' })).id;
      assert.equal((await waitProcessed(client, textId)).status, 'processed');

      for (const [name, param, call] of [
        ['responses input_file', 'input.0.content.1.file_id', () => client.responses.create({
          model: env.chatModel, max_output_tokens: 16, input: question('Summarise.', { type: 'input_file', file_id: failedId }) })],
        ['chat file', 'messages.0.content.1.file.file_id', () => client.chat.completions.create({
          model: env.chatModel, max_tokens: 16, messages: chatQuestion('Summarise.', { type: 'file', file: { file_id: failedId } }) })],
      ]) {
        const body = assertFilesError(await rejection(call()), { status: 400, code: 'invalid_request_error', param });
        assert.equal(body.message, CHECKSUM_SENTENCE, name);
      }
      for (const type of ['input_image', 'input_video']) {
        const err = await rejection(client.responses.create({
          model: env.chatModel, max_output_tokens: 16, input: question('Describe it.', { type, file_id: textId }) }));
        const body = assertFilesError(err, { status: 400, code: 'invalid_request_error', param: 'input.0.content.1.file_id' });
        assert.ok(body.message.includes(type), body.message);
      }
    } finally {
      await deleteQuietly(client, textId);
      await finish(client, upload.id, failedId);
    }
  });

  itPlanned('file-input', "another project's file id as model input is the same 404 file_not_found as an id that never existed, on both routes", async () => {
    const other = client0(requireOtherKey()); // before anything is created: a SKIP leaves nothing
    const client = client0();
    const f = await client.files.create({ file: await toFile(Buffer.from(`secret-${tag()}\n`), 'secret.txt', { type: 'text/plain' }), purpose: 'user_data' });
    try {
      assert.equal((await waitProcessed(client, f.id)).status, 'processed');
      for (const [dialect, param, call] of [
        ['responses', 'input.0.content.1.file_id', (fid) => other.responses.create({
          model: env.chatModel, max_output_tokens: 16, input: question('Repeat the file.', { type: 'input_file', file_id: fid }) })],
        ['chat.completions', 'messages.0.content.1.file.file_id', (fid) => other.chat.completions.create({
          model: env.chatModel, max_tokens: 16, messages: chatQuestion('Repeat the file.', { type: 'file', file: { file_id: fid } }) })],
      ]) {
        const bodies = [];
        for (const fileId of [f.id, randomFileId()]) {
          bodies.push(assertFilesError(await rejection(call(fileId)), { status: 404, code: 'file_not_found', param }));
        }
        assert.deepEqual(bodies[0], bodies[1], dialect);
      }
    } finally {
      await deleteQuietly(client, f.id);
    }
  });

  itPlanned('file-input', 'a key without files.read gets the same 403 for a real and a random file id in a prompt, but may send file_data', async () => {
    const narrow = client0(requireResponsesOnlyKey()); // before anything is created
    const client = client0();
    const f = await client.files.create({ file: await toFile(Buffer.from(`kestrel-${tag()}\n`), 'scoped-input.txt', { type: 'text/plain' }), purpose: 'user_data' });
    try {
      assert.equal((await waitProcessed(client, f.id)).status, 'processed');
      for (const [dialect, call] of [
        ['responses', (fid) => narrow.responses.create({
          model: env.chatModel, max_output_tokens: 16, input: question('Repeat the file.', { type: 'input_file', file_id: fid }) })],
        ['chat.completions', (fid) => narrow.chat.completions.create({
          model: env.chatModel, max_tokens: 16, messages: chatQuestion('Repeat the file.', { type: 'file', file: { file_id: fid } }) })],
      ]) {
        const real = assertInsufficientScope(await rejection(call(f.id)), 'files.read');
        const unknown = assertInsufficientScope(await rejection(call(randomFileId())), 'files.read');
        assert.deepEqual(real, unknown, `${dialect}: a real id got another 403`);
      }
      // Inline bytes are the caller's own: no file scope (service.required_scope).
      const inline = await narrow.responses.create({
        model: env.chatModel, max_output_tokens: 16,
        input: question('Summarise.', { type: 'input_file', filename: 'own.txt', file_data: dataUrl(Buffer.from('my own words\n'), 'text/plain') }),
      });
      assert.equal(inline.status, 'completed');
    } finally {
      await deleteQuietly(client, f.id);
    }
  });

  itPlanned('file-input', 'input_file with a file_url is refused with 400 naming file_url, and nothing is fetched', async () => {
    const err = await rejection(client0().responses.create({
      model: env.chatModel,
      max_output_tokens: 16,
      input: question('Summarise.', { type: 'input_file', file_url: 'http://127.0.0.1:9/private.pdf' }),
    }));
    assertFilesError(err, { status: 400, code: 'invalid_request_error', param: 'input.0.content.1.file_url' });
  });
});
