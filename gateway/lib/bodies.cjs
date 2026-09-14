'use strict';
/**
 * Paths, per-path body caps, and the request body itself (2026-09-13).
 *
 * THREE BODY MODES, each for a measured reason:
 *
 *  - BUFFERED (the JSON routes): read whole, kept in memory up to 1 MiB and
 *    spilled past that to a 0600 file in the 0700 spool directory. Buffered
 *    because the gateway must be able to send the SAME bytes again: the
 *    pre-commit connect retry (≤110 s) and the post-commit re-attach both
 *    re-POST with the same X-TechSara-Attempt, and the orchestrator attaches
 *    only when body_sha256 matches. Spilled because a 20 MiB image request
 *    held in memory by every waiting client is the fd-and-RSS flood the
 *    design's physical-safety section closes.
 *  - STREAMED (audio, POST /v1/files, upload parts): piped to the orchestrator
 *    with backpressure. Files design §12.2: buffering costs 310 MiB RSS per
 *    64 MiB body; streaming 8 x 64 MiB plateaued at 632 MiB.
 *  - NONE (GET, OPTIONS).
 *
 * MEMORY IS BOUNDED TOO (2026-09-14, review finding: 400 waiting 1 MiB
 * bodies held +914 MiB of RSS). The bytes kept in memory are reserved
 * against one process-wide MemoryBudget (lib/guards.cjs); a body that
 * cannot get memory spills to the spool at once, so the spool's quota and
 * floor below are what refuse it.
 *
 * THE SPOOL IS BOUNDED (2026-09-13, review finding, reproduced: six
 * credential-less connections trickling one byte per idle window held six
 * 19 MiB spool files, 114 MiB, with the orchestrator never called). Every
 * spilled byte is reserved first against a total quota and a free-space
 * floor (lib/guards.cjs SpoolBudget): a declared body whole, before a byte
 * is read; a chunked body chunk by chunk as it grows.
 *
 * THE CAPS read the orchestrator's own variables at call time, exactly as
 * route.ts `publicApiBodyBytesFor` does, so the edge and the server refuse at
 * the same byte. The three file routes use the Files design §8 values, which
 * route.ts gains in the same wave.
 */

const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const { Readable } = require('node:stream');

const MiB = 1024 * 1024;

const DEFAULT_PUBLIC_API_BODY_BYTES = 1 * MiB;
const DEFAULT_PUBLIC_API_MEDIA_BODY_BYTES = 20 * MiB;
const DEFAULT_PUBLIC_API_AUDIO_BODY_BYTES = 90 * MiB;
// no-timeout design: 2,048 inputs / 1,000 documents are request shape (route.ts).
const DEFAULT_PUBLIC_API_POOLING_BODY_BYTES = 8 * MiB;
// Files design §8: 64 MiB part + 1 MiB multipart framing; raw PUT part 64 MiB.
const DEFAULT_PUBLIC_API_FILES_MAX_BODY_BYTES = 68_157_440;
const DEFAULT_PUBLIC_API_FILES_PART_MAX_BYTES = 67_108_864;

function envBytes(env, name, fallback) {
  const raw = Number(env[name] ?? '');
  return Number.isFinite(raw) && raw > 0 ? Math.floor(raw) : fallback;
}

/* ----------------------------------------------------------- the path -- */

/**
 * The upstream path for a raw request target, or null for anything that is
 * not /v1 or under it.
 *
 * WHY THE WHATWG PARSE FIRST. Next only ever sees a normalised URL, so
 * `/v1/../api/admin` can never reach its /v1 handler. cloudflared's path rule
 * `^/v1(/|$)` matches the RAW path, so this process does receive it — and a
 * naive join would put `/v1/../api/admin` on the wire to a server that
 * resolves the dot segments. `new URL` resolves `..`, `.` and their %2e
 * spellings; what survives must still start with /v1; and a segment that
 * DECODES to a dot segment (%252e%252e) is refused as well, because
 * encodeURIComponent would put it back on the wire as a literal `..`.
 */
function resolveTarget(rawUrl) {
  let url;
  try {
    url = new URL(String(rawUrl ?? ''), 'http://gateway.invalid');
  } catch {
    return null;
  }
  const pathname = url.pathname;
  if (pathname !== '/v1' && !pathname.startsWith('/v1/')) return null;
  const parts = [];
  for (const segment of pathname.slice(3).split('/')) {
    if (segment === '') continue;
    let decoded;
    try {
      decoded = decodeURIComponent(segment);
    } catch {
      return null;
    }
    if (decoded === '.' || decoded === '..') return null;
    parts.push(decoded);
  }
  // route.ts `upstreamPathFor`: the same path, each segment re-encoded.
  const tail = parts.map(encodeURIComponent).join('/');
  return {
    parts,
    route: parts.join('/'),
    search: url.search,
    searchParams: url.searchParams,
    upstreamPath: `/v1${tail ? `/${tail}` : ''}${url.search}`,
  };
}

/* ----------------------------------------------------------- the caps -- */

const UPLOAD_PARTS = /^uploads\/[^/]+\/parts$/;
const UPLOAD_PART_PUT = /^uploads\/[^/]+\/parts\/[^/]+$/;

/**
 * { cap, mode, requireLength } for one request. Exact paths, never prefixes
 * (route.ts: a prefix match hands every future route under a generous path
 * the generous cap without anybody deciding it should have one).
 */
function bodyRuleFor(method, route, env = process.env) {
  if (method === 'GET' || method === 'HEAD' || method === 'OPTIONS') {
    return { cap: 0, mode: 'none', requireLength: false };
  }
  if (route === 'audio/transcriptions') {
    return {
      cap: envBytes(env, 'PUBLIC_API_MAX_AUDIO_BODY_BYTES', DEFAULT_PUBLIC_API_AUDIO_BODY_BYTES),
      mode: 'stream',
      requireLength: false,
    };
  }
  if (route === 'responses' || route === 'chat/completions') {
    return {
      cap: envBytes(env, 'PUBLIC_API_MAX_MEDIA_BODY_BYTES', DEFAULT_PUBLIC_API_MEDIA_BODY_BYTES),
      mode: 'buffer',
      requireLength: false,
    };
  }
  if (route === 'embeddings' || route === 'rerank') {
    return {
      cap: envBytes(env, 'PUBLIC_API_MAX_POOLING_BODY_BYTES', DEFAULT_PUBLIC_API_POOLING_BODY_BYTES),
      mode: 'buffer',
      requireLength: false,
    };
  }
  if (method === 'POST' && (route === 'files' || UPLOAD_PARTS.test(route))) {
    return {
      cap: envBytes(env, 'PUBLIC_API_FILES_MAX_BODY_BYTES', DEFAULT_PUBLIC_API_FILES_MAX_BODY_BYTES),
      mode: 'stream',
      requireLength: false,
    };
  }
  if (method === 'PUT' && UPLOAD_PART_PUT.test(route)) {
    return {
      cap: envBytes(env, 'PUBLIC_API_FILES_PART_MAX_BYTES', DEFAULT_PUBLIC_API_FILES_PART_MAX_BYTES),
      mode: 'stream',
      requireLength: true,
    };
  }
  return {
    cap: envBytes(env, 'PUBLIC_API_MAX_BODY_BYTES', DEFAULT_PUBLIC_API_BODY_BYTES),
    mode: 'buffer',
    requireLength: false,
  };
}

/** Byte downloads whose Content-Length the edge re-sets (Files §12.5). */
const BYTE_DOWNLOAD = /^files\/[^/]+\/(content|derived\/[^/]+)$/;
function isByteDownload(method, route) {
  return method === 'GET' && BYTE_DOWNLOAD.test(route);
}

/**
 * The response shape the orchestrator WILL give, when it can be known from
 * the request alone: 'sse', 'json', or null. Only these routes may be
 * committed by the gateway while the orchestrator is silent, because only
 * for these is a heartbeat byte provably harmless (a `: ping` comment, or
 * JSON whitespace keepalive.CommittedJSONResponse itself writes).
 */
const DETERMINISTIC_JSON_ROUTES = new Set(['embeddings', 'rerank']);
const RESPONSE_BY_ID = /^responses\/[^/]+$/;

function inferShape(method, target, bodySummary) {
  const { route, searchParams } = target;
  if (method === 'POST' && (route === 'responses' || route === 'chat/completions')) {
    // bodySummary: StreamFlagScanner.summary(), null unless the body is one
    // JSON object.
    if (!bodySummary) return null;
    return bodySummary.stream === true ? 'sse' : 'json';
  }
  if (method === 'POST' && DETERMINISTIC_JSON_ROUTES.has(route)) return 'json';
  if (method === 'GET' && RESPONSE_BY_ID.test(route)) {
    return searchParams.get('stream') === 'true' ? 'sse' : 'json';
  }
  return null;
}

/** Whether the shape needs the body parsed to be inferred. */
function shapeNeedsBody(method, route) {
  return method === 'POST' && (route === 'responses' || route === 'chat/completions');
}

/* ---------------------------------------------------- the stream flag -- */

const JSON_WS = new Uint8Array(256);
JSON_WS[0x20] = 1;
JSON_WS[0x09] = 1;
JSON_WS[0x0a] = 1;
JSON_WS[0x0d] = 1;
// "stream" with every character \u-escaped is 36 bytes; a longer key is not it.
const KEY_BYTES_MAX = 64;
const LITERAL_CHARS_MAX = 16;

/**
 * Whether a JSON request body is one object whose LAST top-level "stream"
 * member is the literal true, read incrementally as the body arrives.
 *
 * WHY NOT JSON.parse (2026-09-13, review finding, reproduced with
 * scratchpad p2_heap.cjs): shape inference did readFileSync + JSON.parse of
 * the whole spilled body on the event loop and kept the parsed object for the
 * life of the relay. Eight concurrent POST /v1/responses with a 15 MiB image,
 * held open by the orchestrator, grew heapUsed by 136.2 MiB and RSS by
 * 184.7 MiB; ~200 queued 20 MiB requests would exhaust the 4,144 MiB heap of
 * the one process that must never restart. This keeps a few bytes of state
 * and costs one pass over bytes the gateway is reading anyway; string
 * contents (the base64 image) are skipped with native indexOf.
 *
 * It agrees with JSON.parse on every valid body (duplicate keys: the last
 * wins, as in JSON.parse and Python's json; keys compare after unescaping, so
 * "stream" counts). It does not validate: an invalid body can be
 * summarised, and the orchestrator refuses it in its first write, long before
 * the 15 s self-commit could matter (and a late refusal is handled anyway).
 */
class StreamFlagScanner {
  constructor() {
    this.phase = 'start'; // start | open | done | invalid
    this.depth = 0;
    this.inString = false;
    this.escape = false;
    this.keyString = false;
    this.key = [];
    this.keyLength = 0;
    this.expect = 'key'; // at depth 1: key | colon | value | literal | comma
    this.lastKey = null;
    this.literal = '';
    this.streamTrue = false;
  }

  push(buf) {
    if (this.phase === 'invalid') return;
    const n = buf.length;
    let backslashAt = -2; // -2: not searched yet in this chunk; -1: none left
    let i = 0;
    while (i < n) {
      if (this.inString) {
        if (this.escape) {
          this.escape = false;
          if (this.keyString) this.keyBytes(buf, i, i + 1);
          i += 1;
          continue;
        }
        if (backslashAt !== -1 && backslashAt < i) backslashAt = buf.indexOf(0x5c, i);
        const quoteAt = buf.indexOf(0x22, i);
        let stop = quoteAt === -1 ? n : quoteAt;
        if (backslashAt !== -1 && backslashAt < stop) stop = backslashAt;
        if (this.keyString) this.keyBytes(buf, i, stop);
        if (stop === n) return;
        if (buf[stop] === 0x5c) {
          if (this.keyString) this.keyBytes(buf, stop, stop + 1);
          this.escape = true;
          i = stop + 1;
          continue;
        }
        this.inString = false;
        i = stop + 1;
        if (this.keyString) this.endKey();
        continue;
      }

      const b = buf[i];
      i += 1;
      if (this.phase === 'done') {
        if (!JSON_WS[b]) {
          this.phase = 'invalid';
          return;
        }
        continue;
      }
      if (this.phase === 'start') {
        if (JSON_WS[b]) continue;
        if (b !== 0x7b) {
          this.phase = 'invalid';
          return;
        }
        this.phase = 'open';
        this.depth = 1;
        this.expect = 'key';
        continue;
      }
      if (this.expect === 'literal') {
        if (!JSON_WS[b] && b !== 0x2c && b !== 0x7d && b !== 0x5d) {
          if (this.literal.length < LITERAL_CHARS_MAX) this.literal += String.fromCharCode(b);
          continue;
        }
        this.setValue(this.literal === 'true');
      }
      if (JSON_WS[b]) continue;
      if (b === 0x22) {
        this.inString = true;
        this.keyString = this.depth === 1 && this.expect === 'key';
        if (this.keyString) {
          this.key = [];
          this.keyLength = 0;
        } else if (this.depth === 1 && this.expect === 'value') {
          this.setValue(false);
        }
        continue;
      }
      if (b === 0x7b || b === 0x5b) {
        if (this.depth === 1 && this.expect === 'value') this.setValue(false);
        this.depth += 1;
        continue;
      }
      if (b === 0x7d || b === 0x5d) {
        this.depth -= 1;
        if (this.depth <= 0) this.phase = 'done';
        else if (this.depth === 1) this.expect = 'comma';
        continue;
      }
      if (this.depth !== 1) continue;
      if (b === 0x3a) {
        if (this.expect === 'colon') this.expect = 'value';
        continue;
      }
      if (b === 0x2c) {
        this.expect = 'key';
        continue;
      }
      if (this.expect === 'value') {
        this.expect = 'literal';
        this.literal = String.fromCharCode(b);
      }
    }
  }

  keyBytes(buf, from, to) {
    if (to <= from) return;
    this.keyLength += to - from;
    if (this.keyLength <= KEY_BYTES_MAX) this.key.push(Buffer.from(buf.subarray(from, to)));
  }

  endKey() {
    this.keyString = false;
    this.expect = 'colon';
    const tooLong = this.keyLength > KEY_BYTES_MAX;
    const raw = tooLong ? '' : Buffer.concat(this.key).toString('utf8');
    this.key = [];
    if (tooLong) {
      this.lastKey = null;
    } else if (raw.indexOf('\\') === -1) {
      this.lastKey = raw;
    } else {
      try {
        this.lastKey = JSON.parse(`"${raw}"`);
      } catch {
        this.lastKey = null;
      }
    }
  }

  setValue(isTrue) {
    if (this.lastKey === 'stream') this.streamTrue = isTrue;
    this.expect = 'comma';
    this.literal = '';
  }

  /** { stream } for a body that is one complete JSON object; null otherwise. */
  summary() {
    return this.phase === 'done' ? { stream: this.streamTrue } : null;
  }
}

/* ----------------------------------------------------- the idle guard -- */

/**
 * The 60 s body-idle guard (timers table, "v1-gateway client side").
 *
 * requestTimeout is 0, so without this a client that sends headers and then
 * one byte a minute holds a socket forever. The guard destroys the socket
 * after `ms` with no body data — but only while THIS process wants data: a
 * body paused by backpressure (the orchestrator writing a 64 MiB part to
 * disk) or by a connect retry is the gateway's wait, not the client's.
 */
class BodyIdleGuard {
  /**
   * Deliberately NOT a 'data' listener: attaching one would switch the client
   * body into flowing mode before the upstream socket exists, and the bytes
   * would be emitted to nobody. The readers call touch() on every chunk.
   */
  constructor(req, ms, onIdle) {
    this.req = req;
    this.ms = ms;
    this.onIdle = onIdle;
    this.timer = null;
    this.held = false;
    this.done = req.complete;
    this.onEnd = () => this.stop();
    req.once('end', this.onEnd);
  }

  touch() {
    if (this.done || this.held) return;
    clearTimeout(this.timer);
    this.timer = setTimeout(() => {
      this.timer = null;
      if (!this.done && !this.held && !this.req.complete) this.onIdle();
    }, this.ms);
  }

  arm() {
    this.touch();
  }

  /** The gateway stopped reading on purpose. */
  hold() {
    this.held = true;
    clearTimeout(this.timer);
    this.timer = null;
  }

  release() {
    this.held = false;
    this.touch();
  }

  stop() {
    this.done = true;
    clearTimeout(this.timer);
    this.timer = null;
    this.req.off('end', this.onEnd);
  }
}

/* --------------------------------------------------- the buffered body -- */

class BufferedBody {
  constructor({ budget = null, memory = null, scan = false } = {}) {
    this.size = 0;
    this.chunks = [];
    this.file = null;
    this.fd = null;
    this.budget = budget;
    this.reserved = 0;
    // Bytes of `chunks` held against the MemoryBudget.
    this.memory = memory;
    this.memoryReserved = 0;
    this.scanner = scan ? new StreamFlagScanner() : null;
  }

  get spilled() {
    return this.file !== null;
  }

  /**
   * A fresh readable over the same bytes, for every send. The kept chunks
   * themselves, never a Buffer.concat copy: every pre-commit retry (one per
   * 2 s for up to 110 s) and re-attach makes a new stream, and a copy each
   * time is body-sized garbage per attempt outside any budget.
   */
  stream() {
    if (this.file) return fs.createReadStream(this.file);
    return Readable.from(this.chunks.slice());
  }

  /** Give the memory budget back what `chunks` held (they went to disk, or away). */
  releaseMemory() {
    if (this.memory && this.memoryReserved > 0) this.memory.release(this.memoryReserved);
    this.memoryReserved = 0;
  }

  /** StreamFlagScanner.summary() of the whole body; null when not scanned. */
  summary() {
    return this.scanner ? this.scanner.summary() : null;
  }

  /**
   * Free the memory, the descriptor, the file and the spool reservation.
   * Never call it while an fs.write on `fd` may still be queued (see
   * readBufferedBody): the descriptor number would be reused under it.
   */
  dispose() {
    this.chunks = [];
    this.releaseMemory();
    if (this.budget && this.reserved > 0) {
      this.budget.release(this.reserved);
      this.reserved = 0;
    }
    if (this.fd !== null) {
      try {
        fs.closeSync(this.fd);
      } catch {
        /* already closed */
      }
      this.fd = null;
    }
    if (this.file) {
      fs.rm(this.file, { force: true }, () => undefined);
      this.file = null;
    }
  }
}

/**
 * Read a client body into a BufferedBody. Resolves one of:
 *   { body }                 the whole body, within the cap
 *   { over: true }           more than `cap` bytes arrived (reading stopped)
 *   { gone: true }           the client went away mid-body
 *   { storage: true }        the spool could not be written (ENOSPC, EACCES)
 *   { spoolRefused: reason } the spool budget said no ('quota' | 'disk');
 *                            for a declared body, before a byte was read
 *
 * `budget` (a SpoolBudget) is charged for every byte that goes to disk;
 * `memory` (a MemoryBudget) for every byte kept in memory -- when it says
 * no, the body spills early and the spool budget decides;
 * `declared` is the Content-Length, reserved whole when it would spill;
 * `scan` feeds every chunk to a StreamFlagScanner (body.summary()).
 */
function readBufferedBody(req, { cap, memoryBytes, spoolDir, idle, budget = null, memory = null, declared = null, scan = false }) {
  return new Promise((resolve) => {
    const body = new BufferedBody({ budget, memory, scan });
    let settled = false;
    let writing = Promise.resolve();

    if (budget && Number.isFinite(declared) && declared > memoryBytes) {
      const verdict = budget.reserve(declared);
      if (!verdict.ok) {
        resolve({ spoolRefused: verdict.reason });
        return;
      }
      body.reserved = declared;
    }

    const finish = (result) => {
      if (settled) return;
      settled = true;
      req.off('data', onData);
      req.off('end', onEnd);
      req.off('close', onClose);
      if (!result.body) {
        // WHY AFTER `writing` SETTLES (2026-09-13, review finding, reproduced
        // with scratchpad p5_fdrace.cjs under UV_THREADPOOL_SIZE=1): an
        // fs.write still queued on the libuv threadpool names body.fd. When a
        // client left mid-body the descriptor was closed at once, the next
        // open() reused its number, and the queued write then put client A's
        // bytes into client B's spool file ("client B bytes|CLIENT-A-SECRET-
        // PROMPT"); it could as well have been a socket. The threadpool is
        // shared with dns.lookup, every spilled re-send's read stream and
        // fs.rm, so a queued write is ordinary under load.
        const dispose = () => body.dispose();
        writing.then(dispose, dispose);
      }
      resolve(result);
    };

    const spill = () => {
      fs.mkdirSync(spoolDir, { recursive: true, mode: 0o700 });
      const file = path.join(spoolDir, `body-${crypto.randomBytes(12).toString('hex')}`);
      // 'wx' so a pre-existing path (a symlink planted in the spool) is refused.
      body.fd = fs.openSync(file, 'wx', 0o600);
      body.file = file;
      for (const chunk of body.chunks) fs.writeSync(body.fd, chunk);
      body.chunks = [];
      body.releaseMemory();
    };

    /** Reserve spool bytes up to `body.size`; false (and finished) when refused. */
    const reserveForSize = () => {
      if (!budget || body.size <= body.reserved) return true;
      const verdict = budget.reserve(body.size - body.reserved);
      if (!verdict.ok) {
        req.pause();
        finish({ spoolRefused: verdict.reason });
        return false;
      }
      body.reserved = body.size;
      return true;
    };

    const onData = (chunk) => {
      if (settled) return;
      if (idle) idle.touch();
      body.size += chunk.length;
      if (body.size > cap) {
        req.pause();
        finish({ over: true });
        return;
      }
      if (body.scanner) body.scanner.push(chunk);
      if (!body.file && body.size <= memoryBytes && (!memory || memory.reserve(chunk.length))) {
        body.memoryReserved += memory ? chunk.length : 0;
        body.chunks.push(chunk);
        return;
      }
      if (!reserveForSize()) return;
      try {
        if (!body.file) {
          body.chunks.push(chunk);
          spill();
        } else {
          // Serialised appends with backpressure on the client socket.
          req.pause();
          if (idle) idle.hold();
          writing = writing.then(
            () =>
              new Promise((done, fail) => {
                if (body.fd === null) {
                  fail(new Error('spool descriptor closed'));
                  return;
                }
                fs.write(body.fd, chunk, 0, chunk.length, null, (err) => (err ? fail(err) : done()));
              }),
          );
          writing.then(
            () => {
              if (settled) return;
              if (idle) idle.release();
              req.resume();
            },
            () => finish({ storage: true }),
          );
        }
      } catch {
        finish({ storage: true });
      }
    };

    const onEnd = () => {
      writing.then(
        () => {
          if (settled) return;
          if (body.fd !== null) {
            try {
              fs.closeSync(body.fd);
            } catch {
              /* closed */
            }
            body.fd = null;
          }
          finish({ body });
        },
        () => finish({ storage: true }),
      );
    };

    const onClose = () => {
      if (!req.complete) finish({ gone: true });
    };

    req.on('data', onData);
    req.once('end', onEnd);
    req.once('close', onClose);
    if (idle) idle.arm();
  });
}

/**
 * Read and discard the rest of a client body, up to `cap` bytes in total
 * (counting `already` bytes read before). Files design §12.3: when the
 * orchestrator answers early (401/404/413), the edge DRAINS rather than
 * aborts, because an aborted body makes openai-node report "Connection error"
 * and retry the part three times, hiding the envelope. Resolves
 * { over: boolean, gone: boolean }.
 */
function drainClientBody(req, cap, already = 0, idle = null) {
  return new Promise((resolve) => {
    if (already > cap) {
      resolve({ over: true, gone: false });
      return;
    }
    if (req.complete || req.readableEnded) {
      resolve({ over: false, gone: false });
      return;
    }
    let total = already;
    if (idle) idle.release();
    const onData = (chunk) => {
      if (idle) idle.touch();
      total += chunk.length;
      if (total > cap) done({ over: true, gone: false });
    };
    const onEnd = () => done({ over: false, gone: false });
    const onClose = () => done({ over: false, gone: !req.complete });
    function done(result) {
      req.off('data', onData);
      req.off('end', onEnd);
      req.off('close', onClose);
      if (result.over) req.pause();
      resolve(result);
    }
    req.on('data', onData);
    req.once('end', onEnd);
    req.once('close', onClose);
    req.resume();
  });
}

/** Remove what a previous process left in the spool (it owned every file). */
function prepareSpool(spoolDir) {
  fs.mkdirSync(spoolDir, { recursive: true, mode: 0o700 });
  try {
    fs.chmodSync(spoolDir, 0o700);
  } catch {
    /* read-only mount point owned by someone else: leave it */
  }
  let removed = 0;
  for (const name of fs.readdirSync(spoolDir)) {
    if (!name.startsWith('body-')) continue;
    fs.rmSync(path.join(spoolDir, name), { force: true });
    removed += 1;
  }
  return removed;
}

module.exports = {
  DEFAULT_PUBLIC_API_BODY_BYTES,
  DEFAULT_PUBLIC_API_MEDIA_BODY_BYTES,
  DEFAULT_PUBLIC_API_AUDIO_BODY_BYTES,
  DEFAULT_PUBLIC_API_POOLING_BODY_BYTES,
  DEFAULT_PUBLIC_API_FILES_MAX_BODY_BYTES,
  DEFAULT_PUBLIC_API_FILES_PART_MAX_BYTES,
  DETERMINISTIC_JSON_ROUTES,
  resolveTarget,
  bodyRuleFor,
  isByteDownload,
  inferShape,
  shapeNeedsBody,
  BodyIdleGuard,
  BufferedBody,
  StreamFlagScanner,
  readBufferedBody,
  drainClientBody,
  prepareSpool,
};
