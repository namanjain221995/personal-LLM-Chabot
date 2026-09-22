#!/usr/bin/env node
'use strict';
/**
 * The CI stub engine: the OpenAI-compatible surface the orchestrator dials,
 * served by ~400 lines of Node with no model behind it.
 *
 * WHY IT EXISTS
 * The `e2e-hosted` stage runs the real orchestrator and frontend images on a
 * hosted runner. Those images dial six model engines at boot and during a
 * request. There is no path from a hosted runner to this deployment's engines
 * that does not mean opening them to the internet, so CI gets a stub that
 * speaks the wire format and nothing else.
 *
 * WHAT IT SPEAKS (every route the orchestrator is known to dial):
 *   GET  /health                 vLLM serves this at the ROOT, not under /v1
 *   GET  /v1/models              app/health.py:715 reads max_model_len here
 *   POST /v1/chat/completions    streaming (SSE) and not; JSON mode honoured
 *   POST /v1/completions         the plain text-completion form
 *   POST /v1/embeddings          app/engines/* and the sidecar facade
 *   POST /score                  app/rerank.py score_url(): root, not /v1
 *   POST /tokenize               app/context.py:400 reads count + max_model_len
 *   GET  /metrics                vllm:generation_tokens_total, which `verify`
 *                                and the engine-state code read as liveness
 * Anything else answers 404 with an OpenAI-shaped error AND logs the method
 * and path loudly, so the first real Actions run tells us what this file is
 * still missing instead of leaving a timeout to be guessed at.
 *
 * WHAT IT CANNOT PROVE, and the job summary repeats each one:
 *   - nothing about a model: no weights, no sampling, no real tokens;
 *   - nothing about token timing or the 15 s SSE heartbeat: a stream is
 *     written in one pass, as fast as the socket takes it;
 *   - nothing about CUDA, aarch64, or engine failure modes (a wedged engine
 *     answering /health green is exactly what this CANNOT reproduce).
 *
 * SECURITY NOTES, because this is a server:
 *   - IT NEVER LOGS A REQUEST BODY. Bodies carry prompts, documents and
 *     whatever a check uploaded; the log line is method, path and status.
 *   - It answers on a private container network and publishes no host port
 *     (e2e/ci/stack.sh runs it with no `-p`). The default bind is 0.0.0.0
 *     because a container's peers reach it by container name; on a developer
 *     machine set STUB_ENGINE_HOST=127.0.0.1.
 *   - It holds no credential and requires none: any Authorization header is
 *     accepted and never echoed, so a key cannot leak back through an error.
 *
 * Environment (all optional, all non-secret; defaults match e2e/ci/ci.env):
 *   STUB_ENGINE_PORT           8000
 *   STUB_ENGINE_HOST           0.0.0.0
 *   STUB_ENGINE_MODELS         comma-separated model ids to publish
 *   STUB_ENGINE_MAX_MODEL_LEN  32768
 *   STUB_ENGINE_EMBED_DIM      1024   (Qwen3-Embedding-0.6B's width)
 *   STUB_ENGINE_QUIET          1 to silence the per-request log line
 */

const http = require('http');

const PORT = Number(process.env.STUB_ENGINE_PORT || 8000);
const HOST = String(process.env.STUB_ENGINE_HOST || '0.0.0.0');
const MAX_MODEL_LEN = Number(process.env.STUB_ENGINE_MAX_MODEL_LEN || 32768);
const EMBED_DIM = Number(process.env.STUB_ENGINE_EMBED_DIM || 1024);
const QUIET = process.env.STUB_ENGINE_QUIET === '1';
const MODELS = String(process.env.STUB_ENGINE_MODELS || 'stub-main,stub-router,stub-embed,stub-rerank,stub-ocr')
  .split(',')
  .map((s) => s.trim())
  .filter(Boolean);

/** Bodies above this are refused rather than buffered: a stub is not a store. */
const MAX_BODY_BYTES = 64 * 1024 * 1024;

const ANSWER = 'This answer came from the CI stub engine. No model ran.';

/** Counters the /metrics text exposes, so a caller can see the stub was used. */
const counters = { completionTokens: 0, promptTokens: 0, requests: 0 };

// ---------------------------------------------------------------- utilities

/** FNV-1a over a string: deterministic, dependency-free, and not a hash we
 *  rely on for anything but "give me a stable number for this text". */
function hash32(text) {
  let h = 0x811c9dc5;
  for (let i = 0; i < text.length; i += 1) {
    h ^= text.charCodeAt(i) & 0xff;
    h = Math.imul(h, 0x01000193) >>> 0;
  }
  return h >>> 0;
}

/** A stable number in [0, 1) for a string. */
function unit(text) {
  return hash32(text) / 0x100000000;
}

const STOPWORDS = new Set([
  'a', 'an', 'and', 'are', 'as', 'at', 'be', 'by', 'does', 'do', 'for', 'from', 'how', 'in',
  'is', 'it', 'of', 'on', 'or', 'that', 'the', 'this', 'to', 'was', 'what', 'when', 'where',
  'which', 'who', 'why', 'with',
]);

function contentTokens(text) {
  return new Set(
    String(text || '')
      .toLowerCase()
      .split(/[^a-z0-9]+/)
      .filter((w) => w.length > 1 && !STOPWORDS.has(w)),
  );
}

/**
 * A relevance score in [0, 1] from lexical coverage, shaped to satisfy the
 * two contracts app/rerank.py enforces on a real reranker:
 *
 *   * THE CANARY (rerank.py:159): the fixed triple must score positive >= 0.9,
 *     negative <= 0.1 and a margin >= 0.7, or the breaker trips and the
 *     reranker is declared broken. A flat 0.5 for everything would fail it.
 *   * NOT DEGENERATE (rerank.py:277): with six or more documents, a spread
 *     under DEGENERATE_BAND (0.02) is read as "the model is not judging" and
 *     the scores are thrown away. Hence the per-document jitter below, which
 *     is deterministic and always leaves the band above 0.02.
 *
 * This is honest: it really does rank a document that shares the question's
 * content words above one that does not. It is not a cross-encoder, and the
 * job summary says so.
 */
function relevance(query, document) {
  const q = contentTokens(query);
  const d = contentTokens(document);
  let hit = 0;
  for (const token of q) if (d.has(token)) hit += 1;
  const coverage = q.size ? hit / q.size : 0;
  const jitter = unit(`${query}\u0000${document}`) * 0.06;
  if (coverage >= 0.5) return Math.min(0.99, 0.93 + jitter);
  if (coverage <= 0.15) return Math.min(0.09, 0.02 + jitter);
  // Between the two: a straight line from 0.12 to 0.88, plus the same jitter.
  const t = (coverage - 0.15) / 0.35;
  return 0.12 + t * 0.76 + jitter - 0.03;
}

/** A deterministic unit vector of EMBED_DIM floats for a piece of text. */
function embedding(text) {
  const out = new Array(EMBED_DIM);
  let seed = hash32(text) || 1;
  let norm = 0;
  for (let i = 0; i < EMBED_DIM; i += 1) {
    // xorshift32: reproducible, uniform enough, no dependency.
    seed ^= seed << 13; seed >>>= 0;
    seed ^= seed >>> 17;
    seed ^= seed << 5; seed >>>= 0;
    const v = seed / 0x100000000 - 0.5;
    out[i] = v;
    norm += v * v;
  }
  norm = Math.sqrt(norm) || 1;
  for (let i = 0; i < EMBED_DIM; i += 1) out[i] = Number((out[i] / norm).toFixed(6));
  return out;
}

/** vLLM's own rule of thumb, and all a stub needs: ~4 characters per token. */
function estimateTokens(text) {
  return Math.max(1, Math.ceil(String(text || '').length / 4));
}

function textOf(value) {
  if (typeof value === 'string') return value;
  if (Array.isArray(value)) return value.map((part) => textOf(part && part.text !== undefined ? part.text : part)).join(' ');
  if (value && typeof value === 'object') return textOf(value.text !== undefined ? value.text : '');
  return '';
}

function promptTextOf(body) {
  if (Array.isArray(body.messages)) return body.messages.map((m) => textOf(m && m.content)).join('\n');
  if (body.prompt !== undefined) return textOf(body.prompt);
  return '';
}

/** JSON mode must return PARSEABLE JSON: app/*.json_completion parses it, and
 *  a prose answer there is a crash loop, not a failed assertion. */
function wantsJson(body) {
  const fmt = body && body.response_format;
  const type = fmt && typeof fmt === 'object' ? String(fmt.type || '') : '';
  return type === 'json_object' || type === 'json_schema';
}

function answerFor(body) {
  if (wantsJson(body)) return JSON.stringify({ stub: true, note: 'CI stub engine; no model ran.' });
  return ANSWER;
}

function send(res, status, payload, headers = {}) {
  const text = typeof payload === 'string' ? payload : JSON.stringify(payload);
  res.writeHead(status, {
    'content-type': typeof payload === 'string' ? 'text/plain; charset=utf-8' : 'application/json',
    'content-length': Buffer.byteLength(text),
    ...headers,
  });
  res.end(text);
}

function sendError(res, status, message, type = 'invalid_request_error') {
  send(res, status, { error: { message, type, param: null, code: null } });
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    let size = 0;
    req.on('data', (chunk) => {
      size += chunk.length;
      if (size > MAX_BODY_BYTES) {
        reject(new Error('request body too large'));
        req.destroy();
        return;
      }
      chunks.push(chunk);
    });
    req.on('end', () => {
      const raw = Buffer.concat(chunks).toString('utf8');
      if (!raw.trim()) return resolve({});
      try {
        resolve(JSON.parse(raw));
      } catch (err) {
        reject(new Error(`body is not JSON: ${err.message}`));
      }
    });
    req.on('error', reject);
  });
}

// ------------------------------------------------------------------ routes

function modelsPayload() {
  const created = 1700000000;
  return {
    object: 'list',
    data: MODELS.map((id) => ({
      id,
      object: 'model',
      created,
      owned_by: 'techsara-ci-stub',
      root: id,
      parent: null,
      max_model_len: MAX_MODEL_LEN,
      permission: [],
    })),
  };
}

function chatCompletion(body, text) {
  const promptTokens = estimateTokens(promptTextOf(body));
  const completionTokens = estimateTokens(text);
  counters.promptTokens += promptTokens;
  counters.completionTokens += completionTokens;
  return {
    id: `chatcmpl-stub-${hash32(text + String(counters.requests)).toString(16)}`,
    object: 'chat.completion',
    created: Math.floor(Date.now() / 1000),
    model: String(body.model || MODELS[0] || 'stub-main'),
    choices: [
      {
        index: 0,
        message: { role: 'assistant', content: text, tool_calls: [] },
        logprobs: null,
        finish_reason: 'stop',
      },
    ],
    usage: {
      prompt_tokens: promptTokens,
      completion_tokens: completionTokens,
      total_tokens: promptTokens + completionTokens,
    },
  };
}

/** The SSE frames vLLM writes for `stream: true`, in the same order. */
function chatChunks(body, text) {
  const id = `chatcmpl-stub-${hash32(text).toString(16)}`;
  const model = String(body.model || MODELS[0] || 'stub-main');
  const created = Math.floor(Date.now() / 1000);
  const base = { id, object: 'chat.completion.chunk', created, model };
  const frames = [];
  frames.push({ ...base, choices: [{ index: 0, delta: { role: 'assistant', content: '' }, logprobs: null, finish_reason: null }] });
  // Word-sized deltas: enough pieces for a consumer's accumulation to be
  // exercised, few enough that the whole stream is one write.
  for (const piece of text.match(/\S+\s*/g) || [text]) {
    frames.push({ ...base, choices: [{ index: 0, delta: { content: piece }, logprobs: null, finish_reason: null }] });
  }
  frames.push({ ...base, choices: [{ index: 0, delta: {}, logprobs: null, finish_reason: 'stop' }] });
  const promptTokens = estimateTokens(promptTextOf(body));
  const completionTokens = estimateTokens(text);
  counters.promptTokens += promptTokens;
  counters.completionTokens += completionTokens;
  if (body.stream_options && body.stream_options.include_usage) {
    frames.push({
      ...base,
      choices: [],
      usage: { prompt_tokens: promptTokens, completion_tokens: completionTokens, total_tokens: promptTokens + completionTokens },
    });
  }
  return frames;
}

function writeStream(res, frames) {
  res.writeHead(200, {
    'content-type': 'text/event-stream; charset=utf-8',
    'cache-control': 'no-cache',
    connection: 'keep-alive',
  });
  for (const frame of frames) res.write(`data: ${JSON.stringify(frame)}\n\n`);
  res.write('data: [DONE]\n\n');
  res.end();
}

function embeddingsPayload(body) {
  const input = body.input === undefined ? [] : Array.isArray(body.input) ? body.input : [body.input];
  const items = input.length ? input : [''];
  let promptTokens = 0;
  const data = items.map((item, index) => {
    const text = textOf(item);
    promptTokens += estimateTokens(text);
    return { object: 'embedding', index, embedding: embedding(text) };
  });
  counters.promptTokens += promptTokens;
  return {
    object: 'list',
    data,
    model: String(body.model || 'stub-embed'),
    usage: { prompt_tokens: promptTokens, total_tokens: promptTokens },
  };
}

/** vLLM's /score: `{data: [{index, score}, ...]}` in DOCUMENT order.
 *  app/rerank.py:248 parse_scores refuses a response whose indices are
 *  incomplete or whose count differs, so both are produced exactly. */
function scorePayload(body) {
  const query = textOf(body.text_1 !== undefined ? body.text_1 : body.query);
  const rawDocs = body.text_2 !== undefined ? body.text_2 : body.documents;
  const docs = Array.isArray(rawDocs) ? rawDocs : rawDocs === undefined ? [] : [rawDocs];
  return {
    id: `score-stub-${hash32(query).toString(16)}`,
    object: 'list',
    model: String(body.model || 'stub-rerank'),
    data: docs.map((doc, index) => ({ index, object: 'score', score: Number(relevance(query, textOf(doc)).toFixed(6)) })),
    usage: { prompt_tokens: estimateTokens(query), total_tokens: estimateTokens(query) },
  };
}

function tokenizePayload(body) {
  const text = promptTextOf(body);
  const count = estimateTokens(text);
  return {
    count,
    max_model_len: MAX_MODEL_LEN,
    // A token-id list of the right LENGTH. app/context.py reads `count`; the
    // list is here because vLLM sends one and a client may assert on it.
    tokens: Array.from({ length: Math.min(count, 4096) }, (_, i) => (hash32(`${text}:${i}`) % 50000) + 1),
  };
}

function metricsText() {
  return [
    '# HELP vllm:generation_tokens_total Number of generation tokens processed.',
    '# TYPE vllm:generation_tokens_total counter',
    `vllm:generation_tokens_total{model_name="${MODELS[0] || 'stub-main'}"} ${counters.completionTokens}`,
    '# HELP vllm:prompt_tokens_total Number of prefill tokens processed.',
    '# TYPE vllm:prompt_tokens_total counter',
    `vllm:prompt_tokens_total{model_name="${MODELS[0] || 'stub-main'}"} ${counters.promptTokens}`,
    '# HELP vllm:num_requests_running Number of requests currently running.',
    '# TYPE vllm:num_requests_running gauge',
    `vllm:num_requests_running{model_name="${MODELS[0] || 'stub-main'}"} 0`,
    '',
  ].join('\n');
}

/**
 * One request. Exported so the tests can drive it through a real server.
 */
async function handle(req, res) {
  const url = new URL(req.url, 'http://engine.invalid');
  const route = url.pathname.replace(/\/+$/, '') || '/';
  counters.requests += 1;
  const log = (status) => {
    // Method, path and status ONLY. Never the body: it carries prompts,
    // documents and uploads, and this log is printed into a public run.
    if (!QUIET) process.stdout.write(`stub-engine ${req.method} ${route} ${status}\n`);
  };

  if (req.method === 'GET' && (route === '/health' || route === '/v1/health')) {
    send(res, 200, { status: 'ok', engine: 'techsara-ci-stub' });
    return log(200);
  }
  if (req.method === 'GET' && (route === '/v1/models' || route === '/models')) {
    send(res, 200, modelsPayload());
    return log(200);
  }
  if (req.method === 'GET' && route === '/metrics') {
    send(res, 200, metricsText());
    return log(200);
  }
  if (req.method === 'GET' && route === '/version') {
    send(res, 200, { version: 'techsara-ci-stub' });
    return log(200);
  }

  if (req.method !== 'POST') {
    sendError(res, 404, `the CI stub engine does not serve ${req.method} ${route}`);
    return log(404);
  }

  let body;
  try {
    body = await readBody(req);
  } catch (err) {
    sendError(res, 400, err.message);
    return log(400);
  }

  if (route === '/v1/chat/completions' || route === '/v1/completions') {
    const text = answerFor(body);
    if (body.stream) {
      if (route === '/v1/completions') {
        const id = `cmpl-stub-${hash32(text).toString(16)}`;
        const frames = (text.match(/\S+\s*/g) || [text]).map((piece) => ({
          id, object: 'text_completion', created: Math.floor(Date.now() / 1000),
          model: String(body.model || MODELS[0] || 'stub-main'),
          choices: [{ index: 0, text: piece, logprobs: null, finish_reason: null }],
        }));
        frames.push({
          id, object: 'text_completion', created: Math.floor(Date.now() / 1000),
          model: String(body.model || MODELS[0] || 'stub-main'),
          choices: [{ index: 0, text: '', logprobs: null, finish_reason: 'stop' }],
        });
        counters.completionTokens += estimateTokens(text);
        writeStream(res, frames);
        return log(200);
      }
      writeStream(res, chatChunks(body, text));
      return log(200);
    }
    if (route === '/v1/completions') {
      const promptTokens = estimateTokens(promptTextOf(body));
      const completionTokens = estimateTokens(text);
      counters.promptTokens += promptTokens;
      counters.completionTokens += completionTokens;
      send(res, 200, {
        id: `cmpl-stub-${hash32(text).toString(16)}`,
        object: 'text_completion',
        created: Math.floor(Date.now() / 1000),
        model: String(body.model || MODELS[0] || 'stub-main'),
        choices: [{ index: 0, text, logprobs: null, finish_reason: 'stop' }],
        usage: { prompt_tokens: promptTokens, completion_tokens: completionTokens, total_tokens: promptTokens + completionTokens },
      });
      return log(200);
    }
    send(res, 200, chatCompletion(body, text));
    return log(200);
  }

  if (route === '/v1/embeddings' || route === '/embeddings' || route === '/pooling') {
    send(res, 200, embeddingsPayload(body));
    return log(200);
  }

  if (route === '/score' || route === '/v1/score' || route === '/rerank' || route === '/v1/rerank') {
    send(res, 200, scorePayload(body));
    return log(200);
  }

  if (route === '/tokenize' || route === '/v1/tokenize') {
    send(res, 200, tokenizePayload(body));
    return log(200);
  }

  // The loud 404. A path that reaches here is a route the orchestrator dials
  // and this file does not implement: the message says so in the run log, in
  // the words somebody grepping for it would use.
  process.stderr.write(`stub-engine UNIMPLEMENTED ${req.method} ${route} — add it to e2e/ci/engine.js\n`);
  sendError(res, 404, `the CI stub engine does not implement ${req.method} ${route}`, 'not_found_error');
  return log(404);
}

function createServer() {
  const server = http.createServer((req, res) => {
    handle(req, res).catch((err) => {
      try {
        sendError(res, 500, `stub engine failed: ${err.message}`, 'internal_error');
      } catch {
        res.destroy();
      }
    });
  });
  // A stub must never be the thing that hangs a job: no keep-alive parking.
  server.keepAliveTimeout = 5000;
  server.headersTimeout = 10000;
  return server;
}

if (require.main === module) {
  const server = createServer();
  server.listen(PORT, HOST, () => {
    process.stdout.write(`stub-engine listening on ${HOST}:${PORT}; models ${MODELS.join(', ')}; max_model_len ${MAX_MODEL_LEN}\n`);
  });
  for (const signal of ['SIGTERM', 'SIGINT']) {
    process.on(signal, () => server.close(() => process.exit(0)));
  }
}

module.exports = {
  createServer,
  handle,
  relevance,
  embedding,
  estimateTokens,
  modelsPayload,
  scorePayload,
  embeddingsPayload,
  tokenizePayload,
  chatChunks,
  chatCompletion,
  metricsText,
  counters,
  MODELS,
  EMBED_DIM,
  MAX_MODEL_LEN,
};
