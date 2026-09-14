// A stub of the DESIGNED planned surface (CONTRACT-3 §7-§10, §15 and the Files
// API design, 2026-09-13), used only to self-test the suite's own test bodies.
//
// WHY (2026-09-13): on a stack where a feature is not built, its test body stops
// at the first 404, so every line after the first call has never executed. A
// typo there would surface only on release day, as a FAIL blamed on the new
// endpoint. Running the bodies against this stub with the features promoted
// proves the tests themselves, not TechSara.
//
// WHY the shapes are copied from the contract, not remembered (2026-09-13,
// review): the first stub minted `rerank_` ids because the test expected them,
// and the selftest passed 11/11 on a shared mistake — CONTRACT-3 §8.5/§16 and
// publicapi/endpoints.py mint `rrk_<24 hex>`. Every shape below cites its source.
//
// `streamMode` shapes a /v1/responses stream:
//   'contract' — exactly the CONTRACT-3 §10 event list (what the e2e stack sends)
//   'helper'   — §10 plus the output_item / content_part frames openai-node needs
//   'queued'   — 'helper' with the §10 `response.queued` of a recovering engine
//
//   node selftest/run.mjs
import { createServer } from 'node:http';
import { randomBytes } from 'node:crypto';
import { inflateSync } from 'node:zlib';

const hex24 = () => randomBytes(12).toString('hex');
const id = (p) => `${p}${hex24()}`;
const files = new Map();
const uploads = new Map();

// CONTRACT-3 §12.2 ceilings, as registry.to_wire renders them (§15).
const CHAT_ENDPOINTS = ['/v1/responses', '/v1/chat/completions'];
const MODELS = {
  'techsara-35b': { kind: 'chat', endpoints: CHAT_ENDPOINTS, context_window: 1_000_000, max_input_tokens: 999_232, max_output_tokens: 1_000_000, default_max_output_tokens: 8192, limits: { max_images_per_request: 16 } },
  'techsara-8b-vision': { kind: 'chat', endpoints: CHAT_ENDPOINTS, context_window: 24_576, max_input_tokens: 24_320, max_output_tokens: 24_576, default_max_output_tokens: 8192, limits: { max_images_per_request: 8 } },
  'techsara-ocr': { kind: 'chat', endpoints: CHAT_ENDPOINTS, context_window: 8192, max_input_tokens: 7936, max_output_tokens: 8192, default_max_output_tokens: 8192, limits: { max_images_per_request: 1 } },
  'techsara-embed': { kind: 'embedding', endpoints: ['/v1/embeddings'], context_window: 4096, max_input_tokens: 4096, max_output_tokens: null, default_max_output_tokens: null, limits: { max_inputs: 256 } },
  'techsara-rerank': { kind: 'rerank', endpoints: ['/v1/rerank'], context_window: 4096, max_input_tokens: 4096, max_output_tokens: null, default_max_output_tokens: null, limits: { max_documents: 100 } },
  'techsara-whisper': { kind: 'transcription', endpoints: ['/v1/audio/transcriptions'], context_window: null, max_input_tokens: null, max_output_tokens: null, default_max_output_tokens: null, limits: {} },
};
const modelWire = (mid) => ({ id: mid, object: 'model', owned_by: 'techsara', status: 'available', ...MODELS[mid] });

/** The colour of the first pixel of a PNG data URL (the suite's solid-colour images). */
function pngColour(dataUrl) {
  const m = /^data:image\/png;base64,(.+)$/.exec(dataUrl || '');
  if (!m) return undefined;
  const png = Buffer.from(m[1], 'base64');
  const idat = [];
  for (let off = 8; off < png.length; ) {
    const len = png.readUInt32BE(off);
    if (png.toString('ascii', off + 4, off + 8) === 'IDAT') idat.push(png.subarray(off + 8, off + 8 + len));
    off += 12 + len;
  }
  const [r, g, b] = inflateSync(Buffer.concat(idat)).subarray(1, 4);
  if (r > 200 && g > 200 && b > 200) return 'white';
  if (r > g && r > b) return 'red';
  if (b > r && b > g) return 'blue';
  return 'green';
}

function json(res, status, body) {
  res.writeHead(status, { 'content-type': 'application/json', 'x-request-id': 'req_selftest' });
  res.end(JSON.stringify(body));
}
function error(res, status, code, param = null, message = `selftest ${code}`) {
  json(res, status, { error: { message, type: 'invalid_request_error', code, param, request_id: 'req_selftest' } });
}
function sse(res) {
  // CONTRACT-3 §10 headers.
  res.writeHead(200, { 'content-type': 'text/event-stream', 'cache-control': 'no-store, no-cache, no-transform', 'x-request-id': 'req_selftest' });
}
async function form(req, body) {
  return new Response(body, { headers: { 'content-type': req.headers['content-type'] } }).formData();
}
const vector = (seed) => Array.from({ length: 1024 }, (_, i) => Math.sin(seed + i) / 10);

export function startDesignStub({ streamMode = 'helper' } = {}) {
  const server = createServer(async (req, res) => {
    const chunks = [];
    for await (const c of req) chunks.push(c);
    const body = Buffer.concat(chunks);
    const url = new URL(req.url, 'http://stub');
    const p = url.pathname;
    const m = req.method;
    let match;
    try {
      if (m === 'POST' && p === '/v1/embeddings') {
        const b = JSON.parse(body);
        const extra = Object.keys(b).find((k) => !['model', 'input', 'encoding_format'].includes(k));
        if (extra) return error(res, 400, 'invalid_request_error', extra);
        const inputs = Array.isArray(b.input) ? b.input : [b.input];
        const data = inputs.map((text, index) => {
          const v = vector(text.length);
          const embedding = b.encoding_format === 'base64' ? Buffer.from(new Float32Array(v).buffer).toString('base64') : Array.from(new Float32Array(v));
          return { object: 'embedding', index, embedding };
        });
        return json(res, 200, { object: 'list', model: b.model, data, usage: { prompt_tokens: 5, total_tokens: 5 } });
      }
      if (m === 'POST' && p === '/v1/rerank') {
        const b = JSON.parse(body);
        const docs = b.documents.map((d) => (typeof d === 'string' ? d : d.text));
        // Crude stem overlap (first four letters) — enough to rank "rotate a key"
        // against "Keys are rotated"; the real model is not what is under test.
        const stems = (t) => (t.toLowerCase().match(/[a-z]{3,}/g) || []).map((w) => w.slice(0, 4));
        const words = new Set(stems(b.query));
        const results = docs
          .map((text, index) => ({ index, relevance_score: stems(text).filter((w) => words.has(w)).length / 10, ...(b.return_documents ? { document: { text } } : {}) }))
          .sort((a, c) => c.relevance_score - a.relevance_score || a.index - c.index)
          .slice(0, b.top_n ?? docs.length);
        // CONTRACT-3 §8.5: `rrk_<24 hex>`, the ledger generation id (§16).
        return json(res, 200, { id: id('rrk_'), object: 'rerank', model: b.model, results, usage: { input_tokens: 10, total_tokens: 10 } });
      }
      if (m === 'POST' && p === '/v1/audio/transcriptions') {
        const f = await form(req, body);
        if (['srt', 'vtt'].includes(f.get('response_format'))) return error(res, 400, 'invalid_request_error', 'response_format');
        return json(res, 200, { text: '', usage: { type: 'duration', seconds: 1 } });
      }
      if (m === 'POST' && p === '/v1/files') {
        const f = await form(req, body);
        if (f.get('purpose') !== 'user_data') return error(res, 400, 'invalid_request_error', 'purpose');
        const file = f.get('file');
        const bytes = Buffer.from(await file.arrayBuffer());
        const obj = { id: id('file-'), object: 'file', bytes: bytes.length, created_at: 1789200000, filename: file.name, purpose: 'user_data', status: 'processed' };
        files.set(obj.id, { obj, bytes });
        return json(res, 200, obj);
      }
      if (m === 'GET' && p === '/v1/files') {
        return json(res, 200, { object: 'list', data: [...files.values()].map((f) => f.obj), has_more: false });
      }
      if ((match = /^\/v1\/files\/([^/]+)(\/content)?$/.exec(p))) {
        const f = files.get(match[1]);
        if (!f) return error(res, 404, 'file_not_found');
        if (m === 'GET' && match[2]) {
          res.writeHead(200, { 'content-type': 'application/octet-stream' });
          return res.end(f.bytes);
        }
        if (m === 'GET') return json(res, 200, f.obj);
        if (m === 'DELETE') {
          files.delete(match[1]);
          return json(res, 200, { id: match[1], object: 'file', deleted: true });
        }
      }
      if (m === 'POST' && p === '/v1/uploads') {
        const b = JSON.parse(body);
        const u = { id: id('upload_'), object: 'upload', bytes: b.bytes, created_at: 1789200000, expires_at: 1789286400, filename: b.filename, purpose: b.purpose, status: 'pending', file: null };
        uploads.set(u.id, { u, parts: new Map() });
        return json(res, 200, u);
      }
      if ((match = /^\/v1\/uploads\/([^/]+)(?:\/(parts|complete|cancel))?$/.exec(p))) {
        const up = uploads.get(match[1]);
        if (!up) return error(res, 404, 'upload_not_found');
        if (m === 'GET' && !match[2]) return json(res, 200, { ...up.u, parts: [...up.parts.keys()] });
        if (m === 'POST' && match[2] === 'parts') {
          const f = await form(req, body);
          const partId = id('part_');
          up.parts.set(partId, Buffer.from(await f.get('data').arrayBuffer()));
          return json(res, 200, { id: partId, object: 'upload.part', created_at: 1789200000, upload_id: up.u.id });
        }
        if (m === 'POST' && match[2] === 'complete') {
          const b = JSON.parse(body);
          const bytes = Buffer.concat(b.part_ids.map((pid) => up.parts.get(pid)));
          if (bytes.length !== up.u.bytes) return error(res, 400, 'invalid_request_error', 'part_ids');
          const obj = { id: id('file-'), object: 'file', bytes: bytes.length, created_at: 1789200000, filename: up.u.filename, purpose: up.u.purpose, status: 'processed' };
          files.set(obj.id, { obj, bytes });
          up.u = { ...up.u, status: 'completed', file: obj };
          return json(res, 200, up.u);
        }
        if (m === 'POST' && match[2] === 'cancel') {
          up.u = { ...up.u, status: 'cancelled' };
          return json(res, 200, up.u);
        }
      }
      if (m === 'GET' && p === '/v1/models') {
        return json(res, 200, { object: 'list', data: Object.keys(MODELS).map(modelWire) });
      }
      if (m === 'GET' && (match = /^\/v1\/models\/([^/]+)$/.exec(p))) {
        if (!MODELS[match[1]]) return error(res, 404, 'model_not_found', 'model');
        return json(res, 200, modelWire(match[1]));
      }
      if (m === 'POST' && (p === '/v1/responses' || p === '/v1/chat/completions')) {
        const b = JSON.parse(body);
        const chat = p === '/v1/chat/completions';
        const spec = MODELS[b.model];
        if (!spec) return error(res, 404, 'model_not_found', 'model');
        // §7: a permitted model on an endpoint its kind does not serve.
        if (spec.kind !== 'chat') return error(res, 400, 'invalid_request_error', 'model', `The model \`${b.model}\` does not support ${p}.`);
        // §8.2: max_completion_tokens is an alias; both together is 400.
        if (chat && b.max_tokens !== undefined && b.max_completion_tokens !== undefined) return error(res, 400, 'invalid_request_error', 'max_completion_tokens');
        const requested = (chat ? (b.max_tokens ?? b.max_completion_tokens) : b.max_output_tokens) ?? spec.default_max_output_tokens;
        if (requested > spec.max_output_tokens) return error(res, 400, 'invalid_request_error', 'max_output_tokens');
        const msgs = chat ? b.messages : Array.isArray(b.input) ? b.input : [{ role: 'user', content: b.input }];
        const parts = msgs.flatMap((msg) => (Array.isArray(msg.content) ? msg.content : [{ type: 'input_text', text: msg.content }]));
        let reply = 'ok';
        for (const part of parts) {
          const url = part.type === 'input_image' ? part.image_url : part.type === 'image_url' ? part.image_url?.url : undefined;
          if (url === undefined) continue;
          // §8.1: nothing but a data: URL (models.validate_image_data_url's sentence).
          if (!String(url).startsWith('data:')) return error(res, 400, 'invalid_request_error', 'input', 'image_url must be a data: URL; remote image URLs are not fetched');
          reply = pngColour(url) ?? 'unknown';
        }
        const fileText = parts.filter((c) => c.type === 'input_file').map((c) => files.get(c.file_id)?.bytes.toString() ?? '').join('');
        reply = /code word is ([\w-]+)/.exec(fileText)?.[1] ?? reply;
        const text = parts.map((c) => c.text ?? '').join(' ');
        let incomplete = null;
        let finish = 'stop';
        if (/numbers from 1 to 200/.test(text)) {
          reply = Array.from({ length: requested }, (_, i) => `${i + 1},`).join(' ');
          incomplete = { reason: 'max_output_tokens' };
          finish = 'length';
        }
        const applied = requested; // §8.3: min(requested, window − input − reserve); the stub's inputs are tiny
        const usage = { input_tokens: 5, output_tokens: 1, total_tokens: 6 };
        const created = 1789200000;
        if (chat) {
          const cid = `chatcmpl-${hex24()}`;
          if (!b.stream) {
            return json(res, 200, {
              id: cid, object: 'chat.completion', created, model: b.model, max_output_tokens: applied,
              choices: [{ index: 0, message: { role: 'assistant', content: reply }, finish_reason: finish }],
              usage: { prompt_tokens: 5, completion_tokens: 1, total_tokens: 6 },
            });
          }
          sse(res);
          const chunk = (choices, extra = {}) => res.write(`data: ${JSON.stringify({ id: cid, object: 'chat.completion.chunk', created, model: b.model, choices, ...extra })}\n\n`);
          chunk([{ index: 0, delta: { role: 'assistant', content: reply }, finish_reason: null }]);
          chunk([{ index: 0, delta: {}, finish_reason: finish }], { max_output_tokens: applied });
          if (b.stream_options?.include_usage) chunk([], { usage: { prompt_tokens: 5, completion_tokens: 1, total_tokens: 6 } });
          res.write('data: [DONE]\n\n');
          return res.end();
        }
        const rid = id('resp_');
        const msgId = `msg_${hex24()}`;
        const doneItem = { id: msgId, type: 'message', role: 'assistant', status: 'completed', content: [{ type: 'output_text', text: reply, annotations: [] }] };
        const snapshot = (status, output = [], u = null) => ({
          id: rid, object: 'response', created_at: created, status, model: b.model, output,
          max_output_tokens: applied, incomplete_details: status === 'completed' ? incomplete : null, usage: u,
        });
        if (!b.stream) return json(res, 200, snapshot('completed', [doneItem], usage));
        sse(res);
        let n = 0;
        const ev = (type, data) => res.write(`event: ${type}\ndata: ${JSON.stringify({ type, sequence_number: ++n, ...data })}\n\n`);
        const frames = streamMode !== 'contract';
        const at = { item_id: msgId, output_index: 0, content_index: 0 };
        ev('response.created', { response: snapshot('queued') });
        if (streamMode === 'queued') ev('response.queued', { response: snapshot('queued') });
        ev('response.in_progress', { response: snapshot('in_progress') });
        if (frames) {
          ev('response.output_item.added', { output_index: 0, item: { ...doneItem, status: 'in_progress', content: [] } });
          ev('response.content_part.added', { ...at, part: { type: 'output_text', text: '', annotations: [] } });
        }
        for (const piece of reply.match(/.{1,3}/g)) ev('response.output_text.delta', { ...at, delta: piece });
        ev('response.output_text.done', { ...at, text: reply });
        if (frames) {
          ev('response.content_part.done', { ...at, part: doneItem.content[0] });
          ev('response.output_item.done', { output_index: 0, item: doneItem });
        }
        ev('response.completed', { response: snapshot('completed', [doneItem], usage) });
        return res.end();
      }
      return error(res, 404, 'invalid_request_error');
    } catch (err) {
      json(res, 500, { error: { message: String(err), type: 'server_error', code: 'internal_error', param: null } });
    }
  });
  return new Promise((resolve) => server.listen(0, '127.0.0.1', () => resolve(server)));
}
