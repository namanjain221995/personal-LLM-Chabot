'use strict';
/**
 * A fake orchestrator for the gateway tests (2026-09-13). Engines are stubs;
 * nothing here touches a real service.
 *
 * It speaks the orchestrator side of the internal attach protocol the way the
 * no-timeout design specifies it (T3): X-TechSara-Attempt keys a run, the run
 * is named in X-TechSara-Run, SSE data frames are followed by `: ts-seq=N`,
 * and a re-POST with the same attempt and X-TechSara-Resume-After: N replays
 * from N+1 and tails. Run state is a file per attempt in STUB_STATE_DIR, so a
 * killed and relaunched stub resumes the same deterministic generation —
 * which is what a durable orchestrator restart looks like from outside.
 *
 * Behaviour is chosen per request by the `model` string, e.g.
 * `stub/tokens=40/interval=100/silent=400000`:
 *   tokens=N interval=MS   the generation: token k is "w<k> ", due at k*MS
 *   silent=MS              no byte at all (no headers) until MS after launch
 *   silentonce             `silent` applies to the launch only, not to attaches
 *   stall=K                SSE: after frame K go silent (no ping) until attached
 *   partial                JSON: commit, write `{"id": ` and go silent
 *   status=NNN             answer NNN with the contract's envelope at once
 *   latestatus=NNN         after `silent`, answer NNN instead of the result
 *   attach404              an attach is answered 404 (run not replayable)
 *   norun                  no X-TechSara-Run header (a pre-protocol build)
 *   none                   X-TechSara-Run: none (store:false)
 *   nomarker               no `: ts-seq` comments
 *   hdrs                   add headers the edge must never relay
 *   headdie                send a 200 head, then drop the connection
 *   crashbeforemarker=K    write frame K, then SIGKILL itself before its ts-seq
 *   dropafter=MS           a launch reads the whole body, then drops the
 *                          connection MS later without a byte (attaches answer)
 * STUB_PRE_PROTOCOL=1 makes the whole process a build that ignores every
 * X-TechSara-* header (a rollback): no attach, no run header, no ts-seq.
 * `?stub=drop` on any other route reads the body, then drops the connection.
 * Every launch and attach is appended to STUB_STATE_DIR/events.jsonl.
 */

const crypto = require('node:crypto');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');

const PORT = Number(process.env.STUB_PORT || 0);
const STATE = process.env.STUB_STATE_DIR || fs.mkdtempSync(require('node:os').tmpdir() + '/stub-orch-');
const HEARTBEAT_MS = Number(process.env.STUB_HEARTBEAT_MS || 15000);
const COMMIT_MS = Number(process.env.STUB_COMMIT_MS || 15000);
const PRE_PROTOCOL = process.env.STUB_PRE_PROTOCOL === '1';
fs.mkdirSync(STATE, { recursive: true });

function record(event) {
  fs.appendFileSync(path.join(STATE, 'events.jsonl'), `${JSON.stringify({ t: Date.now(), pid: process.pid, ...event })}\n`);
}

function parseModel(model) {
  const opts = { tokens: 5, interval: 50, silent: 0 };
  for (const part of String(model || '').split('/').slice(1)) {
    const [k, v] = part.split('=');
    opts[k] = v === undefined ? true : Number.isNaN(Number(v)) ? v : Number(v);
  }
  return opts;
}

function envelope(status, code) {
  return JSON.stringify({
    error: { message: `stub refusal ${status}`, type: 'invalid_request_error', code, param: null, request_id: 'req_stub' },
  });
}

function readBody(req) {
  return new Promise((resolve) => {
    const hash = crypto.createHash('sha256');
    let bytes = 0;
    const chunks = [];
    let firstByteAt = null;
    req.on('data', (c) => {
      if (firstByteAt === null) firstByteAt = Date.now();
      bytes += c.length;
      hash.update(c);
      if (bytes <= 32 * 1024 * 1024) chunks.push(c);
    });
    req.on('end', () => resolve({ bytes, sha256: hash.digest('hex'), buffer: Buffer.concat(chunks), firstByteAt }));
    req.on('error', () => resolve({ bytes, sha256: hash.digest('hex'), buffer: Buffer.alloc(0) }));
  });
}

const sleep = (ms) => new Promise((r) => setTimeout(r, Math.max(0, ms)));

function stateFile(attempt) {
  return path.join(STATE, `run-${crypto.createHash('sha256').update(attempt).digest('hex').slice(0, 32)}.json`);
}

function chatFrames(runId, opts) {
  const frames = [];
  const created = 1789300000;
  for (let k = 1; k <= opts.tokens; k += 1) {
    frames.push({
      due: k * opts.interval,
      text: `data: ${JSON.stringify({ id: runId, object: 'chat.completion.chunk', created, model: 'stub', choices: [{ index: 0, delta: k === 1 ? { role: 'assistant', content: `w${k} ` } : { content: `w${k} ` }, finish_reason: null }] })}\n\n`,
    });
  }
  const end = (opts.tokens + 1) * opts.interval;
  frames.push({ due: end, text: `data: ${JSON.stringify({ id: runId, object: 'chat.completion.chunk', created, model: 'stub', choices: [{ index: 0, delta: {}, finish_reason: 'stop' }] })}\n\n` });
  frames.push({ due: end, text: 'data: [DONE]\n\n' });
  return frames;
}

function responseObject(runId, status, text) {
  return {
    id: runId,
    object: 'response',
    created_at: 1789300000,
    status,
    model: 'stub',
    output: text === null ? [] : [{ id: `msg_${runId}`, type: 'message', role: 'assistant', status: 'completed', content: [{ type: 'output_text', text, annotations: [] }] }],
    usage: null,
    error: null,
    incomplete_details: null,
  };
}

function responsesFrames(runId, opts) {
  const frames = [];
  let seq = 0;
  const ev = (due, type, data) => {
    seq += 1;
    frames.push({ due, text: `event: ${type}\ndata: ${JSON.stringify({ type, sequence_number: seq, ...data })}\n\n` });
  };
  const item = `msg_${runId}`;
  ev(0, 'response.created', { response: responseObject(runId, 'in_progress', null) });
  ev(0, 'response.output_item.added', { output_index: 0, item: { id: item, type: 'message', role: 'assistant', status: 'in_progress', content: [] } });
  ev(0, 'response.content_part.added', { item_id: item, output_index: 0, content_index: 0, part: { type: 'output_text', text: '', annotations: [] } });
  let text = '';
  for (let k = 1; k <= opts.tokens; k += 1) {
    text += `w${k} `;
    ev(k * opts.interval, 'response.output_text.delta', { item_id: item, output_index: 0, content_index: 0, delta: `w${k} ` });
  }
  const end = (opts.tokens + 1) * opts.interval;
  ev(end, 'response.output_text.done', { item_id: item, output_index: 0, content_index: 0, text });
  ev(end, 'response.content_part.done', { item_id: item, output_index: 0, content_index: 0, part: { type: 'output_text', text, annotations: [] } });
  ev(end, 'response.output_item.done', { output_index: 0, item: { id: item, type: 'message', role: 'assistant', status: 'completed', content: [{ type: 'output_text', text, annotations: [] }] } });
  ev(end, 'response.completed', { response: responseObject(runId, 'completed', text) });
  return frames;
}

function syncBody(route, runId, opts) {
  let text = '';
  for (let k = 1; k <= opts.tokens; k += 1) text += `w${k} `;
  if (route === 'responses') return JSON.stringify(responseObject(runId, 'completed', text));
  if (route === 'embeddings') return JSON.stringify({ object: 'list', data: [{ object: 'embedding', index: 0, embedding: [0.1, 0.2] }], model: 'stub', usage: { prompt_tokens: 1, total_tokens: 1 } });
  return JSON.stringify({
    id: runId,
    object: 'chat.completion',
    created: 1789300000,
    model: 'stub',
    choices: [{ index: 0, message: { role: 'assistant', content: text }, finish_reason: 'stop' }],
    usage: { prompt_tokens: 1, completion_tokens: opts.tokens, total_tokens: opts.tokens + 1 },
  });
}

async function generation(req, res, route, body) {
  let json = {};
  try {
    json = JSON.parse(body.buffer.toString('utf8') || '{}');
  } catch {
    json = {};
  }
  const opts = parseModel(json.model);
  const attempt = PRE_PROTOCOL ? undefined : req.headers['x-techsara-attempt'];
  const resumeHeader = PRE_PROTOCOL ? undefined : req.headers['x-techsara-resume-after'];
  const resumeAfter = resumeHeader === undefined ? 0 : Number(resumeHeader);

  let state = null;
  let attaching = false;
  if (attempt) {
    const file = stateFile(attempt);
    if (fs.existsSync(file)) {
      state = JSON.parse(fs.readFileSync(file, 'utf8'));
      attaching = true;
      if (state.sha256 !== body.sha256) {
        record({ kind: 'attach_refused', attempt, reason: 'sha' });
        res.writeHead(409, { 'content-type': 'application/json' });
        res.end(envelope(409, 'conflict'));
        return;
      }
    } else {
      state = { launchedAt: Date.now(), sha256: body.sha256, attaches: 0 };
      fs.writeFileSync(file, JSON.stringify(state));
    }
  } else {
    state = { launchedAt: Date.now(), sha256: body.sha256, attaches: 0 };
  }
  if (attaching) {
    state.attaches += 1;
    fs.writeFileSync(stateFile(attempt), JSON.stringify(state));
  }
  record({ kind: attaching ? 'attach' : 'launch', attempt: attempt || null, route, resume_after: resumeHeader === undefined ? null : resumeAfter, retry_count: req.headers['x-stainless-retry-count'] ?? null, stream: json.stream === true });

  const runId = `resp_${crypto.createHash('sha256').update(attempt || String(state.launchedAt)).digest('hex').slice(0, 16)}`;
  const extra = {};
  if (attempt && !opts.norun) extra['x-techsara-run'] = opts.none ? 'none' : runId;
  extra['x-request-id'] = 'req_stub';
  extra['x-ratelimit-remaining-requests'] = '99';
  if (opts.hdrs) {
    extra['set-cookie'] = 'ts_session=stolen; Path=/';
    extra['access-control-allow-credentials'] = 'true';
    extra['x-techsara-debug'] = 'internal-host:8080';
    extra['x-powered-by'] = 'stub';
    extra['server-timing'] = 'db;dur=3';
  }

  if (opts.dropafter !== undefined && !attaching) {
    setTimeout(() => req.socket.destroy(), opts.dropafter);
    return;
  }
  if (opts.status) {
    res.writeHead(opts.status, { 'content-type': 'application/json', ...extra, 'retry-after': '7' });
    res.end(envelope(opts.status, 'stub_status'));
    return;
  }
  if (opts.headdie || (attaching && process.env.STUB_ATTACH_HEADDIE === '1')) {
    // A head, then the connection dies before any body byte.
    res.writeHead(200, { 'content-type': json.stream === true ? 'text/event-stream; charset=utf-8' : 'application/json', ...extra });
    res.flushHeaders();
    setTimeout(() => req.socket.destroy(), 30);
    return;
  }
  if (attaching && opts.attach404) {
    res.writeHead(404, { 'content-type': 'application/json', ...extra });
    res.end(envelope(404, 'not_found'));
    return;
  }

  const silent = opts.silentonce && attaching ? 0 : opts.silent || 0;
  const headersAt = state.launchedAt + silent;
  let closed = false;
  res.on('close', () => {
    closed = true;
  });
  await sleep(headersAt - Date.now());
  if (closed) return;
  if (opts.latestatus) {
    res.writeHead(opts.latestatus, { 'content-type': 'application/json', ...extra });
    res.end(envelope(opts.latestatus, 'late'));
    return;
  }

  const base = state.launchedAt + silent;
  if (json.stream === true) {
    const frames = route === 'responses' ? responsesFrames(runId, opts) : chatFrames(runId, opts);
    res.writeHead(200, { 'content-type': 'text/event-stream; charset=utf-8', 'cache-control': 'no-store', ...extra });
    if (route !== 'responses') res.write(': ping\n\n');
    let lastWrite = Date.now();
    const tag = Boolean(attempt) && !opts.norun && !opts.nomarker;
    for (let seq = 1; seq <= frames.length; seq += 1) {
      const frame = frames[seq - 1];
      if (seq <= resumeAfter) continue;
      if (opts.stall !== undefined && !attaching && seq > opts.stall) {
        await new Promise(() => undefined); // silent forever
      }
      for (;;) {
        if (closed) return;
        const wait = base + frame.due - Date.now();
        if (wait <= 0) break;
        const untilPing = lastWrite + HEARTBEAT_MS - Date.now();
        if (untilPing <= 0) {
          res.write(': ping\n\n');
          lastWrite = Date.now();
          continue;
        }
        await sleep(Math.min(wait, untilPing));
      }
      if (closed) return;
      if (opts.crashbeforemarker === seq && !attaching) {
        // The frame leaves, its ts-seq never does: the process dies between.
        res.write(frame.text, () => process.kill(process.pid, 'SIGKILL'));
        await new Promise(() => undefined);
      }
      res.write(tag ? `${frame.text}: ts-seq=${seq}\n\n` : frame.text);
      lastWrite = Date.now();
    }
    res.end();
    return;
  }

  const doneAt = base + (opts.tokens + 1) * opts.interval;
  const payload = syncBody(route, runId, opts);
  if (doneAt - Date.now() > COMMIT_MS || opts.partial) {
    await sleep(Math.min(COMMIT_MS, doneAt - Date.now()));
    if (closed) return;
    res.writeHead(200, { 'content-type': 'application/json', 'cache-control': 'no-store, no-transform', ...extra });
    res.write(' ');
    if (opts.partial) {
      res.write('{"id": ');
      await new Promise(() => undefined);
    }
    while (!closed && Date.now() < doneAt) {
      await sleep(Math.min(HEARTBEAT_MS, doneAt - Date.now()));
      if (!closed && Date.now() < doneAt) res.write(' ');
    }
    if (!closed) res.end(payload);
    return;
  }
  await sleep(doneAt - Date.now());
  if (closed) return;
  res.writeHead(200, { 'content-type': 'application/json', 'content-length': Buffer.byteLength(payload), ...extra });
  res.end(payload);
}

const server = http.createServer({ requestTimeout: 0, keepAliveTimeout: 5000 }, async (req, res) => {
  record({ kind: 'request', method: req.method, url: req.url });
  const url = new URL(req.url, 'http://stub');
  const route = url.pathname.replace(/^\/v1\/?/, '');
  const q = url.searchParams.get('stub');

  if (q === 'early401') {
    record({ kind: 'early401', route });
    res.writeHead(401, { 'content-type': 'application/json', 'www-authenticate': 'Bearer error="invalid_token"' });
    res.end(envelope(401, 'invalid_api_key'));
    return;
  }
  if (route === 'redirect') {
    res.writeHead(302, { location: 'http://orchestrator:8080/internal' });
    res.end();
    return;
  }
  if (route === 'nocontent') {
    res.writeHead(204, { 'x-request-id': 'req_204' });
    res.end();
    return;
  }
  if (route === 'notmodified') {
    res.writeHead(304, { etag: '"abc"' });
    res.end();
    return;
  }
  if (req.method === 'GET' && route === 'models') {
    res.writeHead(200, { 'content-type': 'application/json', 'x-techsara-run': 'none' });
    res.end(JSON.stringify({ object: 'list', data: [{ id: 'stub', object: 'model', created: 0, owned_by: 'techsara' }] }));
    return;
  }
  if (req.method === 'GET' && /^files\/[^/]+\/content$/.test(route)) {
    const bytes = Buffer.alloc(1000, 7);
    res.writeHead(200, {
      'content-type': 'application/octet-stream',
      'content-length': String(bytes.length),
      etag: '"sha"',
      'accept-ranges': 'bytes',
      'content-disposition': 'attachment; filename="a.bin"',
      'content-security-policy': "sandbox; default-src 'none'",
    });
    res.end(bytes);
    return;
  }

  const slow = q === 'slow';
  if (slow) {
    // Read slowly: exercise backpressure through the gateway.
    req.pause();
    const hash = crypto.createHash('sha256');
    let bytes = 0;
    const perTick = Number(url.searchParams.get('kib') || 256) * 1024;
    const tick = setInterval(() => {
      let c;
      while ((c = req.read(perTick)) !== null) {
        bytes += c.length;
        hash.update(c);
        break;
      }
    }, 5);
    req.on('end', () => {
      clearInterval(tick);
      const out = JSON.stringify({ bytes, sha256: hash.digest('hex') });
      res.writeHead(200, { 'content-type': 'application/json' });
      res.end(out);
    });
    req.on('readable', () => undefined);
    return;
  }

  const body = await readBody(req);
  if (q === 'drop') {
    record({ kind: 'drop', method: req.method, route, attempt: req.headers['x-techsara-attempt'] || null, bytes: body.bytes });
    req.socket.destroy();
    return;
  }
  if (req.method === 'POST' && (route === 'chat/completions' || route === 'responses' || route === 'embeddings')) {
    await generation(req, res, route, body);
    return;
  }
  // Everything else: echo what arrived, so the tests can see the wire.
  const out = JSON.stringify({ method: req.method, url: req.url, headers: req.headers, bytes: body.bytes, sha256: body.sha256, first_byte_at: body.firstByteAt });
  res.writeHead(200, { 'content-type': 'application/json', 'x-techsara-debug': 'never-relayed', 'set-cookie': 'a=b' });
  res.end(out);
});

server.listen(PORT, '127.0.0.1', () => {
  process.stdout.write(`READY ${server.address().port}\n`);
});
process.on('SIGTERM', () => {
  // uvicorn-like: readers see an incomplete chunked body, then exit.
  server.closeAllConnections();
  process.exit(0);
});
