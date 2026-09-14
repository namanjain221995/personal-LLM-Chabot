'use strict';
/**
 * One /v1 request, client to orchestrator and back (2026-09-13, no-timeout
 * design revision 2: edge_100s §3-§6, deploy_survival "INTERNAL ATTACH
 * PROTOCOL", timers table "v1-gateway upstream").
 *
 * THE STATES, in the order a request can pass through them:
 *
 *  1. BODY. Refuse a declared over-cap body before reading it; read a JSON
 *     body whole (bodies.cjs); leave a streamed body in the socket.
 *  2. PRE-COMMIT. Nothing has reached the client, so its status line is
 *     still ours to choose. A failure before the request was fully written
 *     (connect refused, reset mid-body) is retried with the SAME
 *     X-TechSara-Attempt every 2 s for up to 110 s — the ≤97 s window in
 *     which a restarting uvicorn refuses connections — and only then
 *     answered 503 model_unavailable, Retry-After 30. A failure AFTER the
 *     orchestrator may have the whole request is retried only where sending
 *     it again cannot do the work twice (retryAfterAccepted); anywhere else
 *     the client gets 503 with x-should-retry: false. WHY 110: Cloudflare
 *     returns 524 at 125 s without a first byte, and the scaled measurement
 *     (nt-advrev ra_stub) showed a default SDK with 2 retries gives up after
 *     about 50 s of 503s, so the gateway absorbs the window itself.
 *  3. SELF-COMMIT. The orchestrator accepted the request but has written
 *     nothing for 15 s (it must, by CONTRACT §10 — so it is blocked, or its
 *     socket is half-open). For the routes whose response shape the request
 *     itself determines, the gateway commits: 200 and `: ping` for a stream,
 *     200 and one space for JSON (what keepalive.CommittedJSONResponse
 *     writes), then a byte every 15 s. Without this, a silent orchestrator is
 *     a Cloudflare 524 at 125 s and an undici headers timeout at 300 s in an
 *     openai-node process.
 *  4. RELAY. Headers through the allowlist, SSE frame by frame with the
 *     internal `: ts-seq=N` comments removed and N remembered, JSON and bytes
 *     as they come. A 300 s upstream silence is a failure (with the 15 s
 *     invariant it can only mean a dead or blocked orchestrator).
 *  5. RE-ATTACH. A committed relay whose upstream failed keeps the client
 *     alive (`: ping`, or a space while the JSON body is still whitespace)
 *     and re-POSTs with the same attempt id and X-TechSara-Resume-After: N,
 *     1 → 10 s backoff, for up to 1,800 s of continuous absence; then it
 *     cuts the client connection so the SDK sees an incomplete read.
 */

const http = require('node:http');
const { randomUUID } = require('node:crypto');
const { performance } = require('node:perf_hooks');

const H = require('./headers.cjs');
const B = require('./bodies.cjs');
const R = require('./reattach.cjs');
const { SseParser, comment, errorFrame } = require('./sse.cjs');

const now = () => performance.now();

const UNAVAILABLE = 'The service is temporarily unavailable. Please retry.';
const GENERATION_ROUTES = new Set(['responses', 'chat/completions']);
// Routes whose orchestrator side keys the work by X-TechSara-Attempt (T3):
// the generations, and the audio job (X-TechSara-Run job:<key>).
const ATTEMPT_KEYED_ROUTES = new Set(['responses', 'chat/completions', 'audio/transcriptions']);
// Files design §2: "Parts are idempotent by part_number, complete and cancel
// are idempotent by state". Creates (POST /v1/files, /v1/uploads) and the
// multipart POST .../parts are not: a second send makes a second object.
const UPLOAD_PART_PUT = /^uploads\/[^/]+\/parts\/[^/]+$/;
const UPLOAD_STATE_CHANGE = /^uploads\/[^/]+\/(complete|cancel)$/;

function errorCode(err) {
  let cur = err;
  for (let depth = 0; cur && depth < 5; depth += 1) {
    if (typeof cur.code === 'string') return cur.code;
    cur = cur.cause;
  }
  return (err && err.name) || 'Error';
}

function tagged(message, code) {
  const err = new Error(message);
  err.code = code;
  return err;
}

function hasJsonNonWhitespace(chunk) {
  for (let i = 0; i < chunk.length; i += 1) {
    const b = chunk[i];
    if (b !== 0x20 && b !== 0x09 && b !== 0x0a && b !== 0x0d) return true;
  }
  return false;
}

/** Read a small upstream body whole; null when it is larger or fails. */
function readSmall(res, maxBytes, silenceMs) {
  return new Promise((resolve) => {
    if ((res.destroyed || res.closed) && !res.complete) {
      resolve(null);
      return;
    }
    const chunks = [];
    let size = 0;
    let timer = setTimeout(() => res.destroy(tagged('upstream silent', 'UPSTREAM_SILENT')), silenceMs);
    const done = (value) => {
      clearTimeout(timer);
      timer = null;
      resolve(value);
    };
    res.on('data', (chunk) => {
      size += chunk.length;
      if (size > maxBytes) {
        res.destroy();
        done(null);
        return;
      }
      chunks.push(chunk);
    });
    res.on('end', () => done(Buffer.concat(chunks, size)));
    res.on('close', () => {
      if (!res.complete) done(null);
    });
    res.on('error', () => undefined);
  });
}

/* ------------------------------------------------------------ attempt -- */

/**
 * One HTTP request to the orchestrator. `done` resolves { res } when its
 * response head arrives, or { error } when the request fails first.
 */
class Attempt {
  constructor(relay, headers, source) {
    this.relay = relay;
    this.source = source; // 'none' | 'empty' | 'buffer' | 'client'
    this.bodyBytes = 0;
    this.clientEnded = false;
    this.settled = false;
    this.responded = false;
    this.accepted = false;
    this.watchdog = null;
    this.unpump = () => undefined;
    this.done = new Promise((resolve) => {
      this.resolveDone = resolve;
    });

    const s = relay.s;
    const gw = relay.gw;
    // A FRESH connection for a streamed body: a pooled keep-alive socket the
    // orchestrator closed during a restart fails on first write, after body
    // bytes have left the client and can no longer be sent again. And for a
    // request that may not be sent twice once accepted: a pooled socket that
    // uvicorn's 5 s keep-alive closes as the request is written fails AFTER
    // the write (measured 2026-09-13 against real uvicorn, scratchpad
    // p10_ka.cjs: two ECONNRESETs within 60 requests spaced ~5 s), which that
    // request could only answer with a 503. A new connection cannot race an
    // idle close; it costs one handshake on the container network.
    const agent = source === 'client' || relay.needsFreshConnection() ? gw.freshAgent : gw.agent;
    const req = http.request({
      host: s.orchestrator.hostname,
      port: s.orchestrator.port,
      method: relay.method,
      path: relay.target.upstreamPath,
      headers,
      agent,
    });
    this.req = req;
    this.kick();

    req.on('socket', (socket) => {
      // TCP keepalive on the orchestrator socket, so a half-open peer
      // surfaces as an error instead of waiting out the silence watchdog.
      socket.setKeepAlive(true, 60_000);
      const connected = () => {
        this.kick();
        if (this.source === 'client') this.pumpClient();
      };
      if (socket.connecting) socket.once('connect', connected);
      else connected();
    });
    req.on('finish', () => {
      this.accepted = true;
      this.kick();
      relay.onAttemptAccepted(this);
    });
    req.on('response', (res) => {
      this.responded = true;
      this.clearWatchdog();
      this.settle({ res });
    });
    req.on('error', (error) => {
      this.clearWatchdog();
      this.unpump();
      this.settle({ error });
    });

    if (source === 'buffer') {
      const stream = relay.body.stream();
      stream.on('error', (err) => req.destroy(err));
      // pipe() never destroys its source. Without this, every attempt on a
      // spilled body whose connection died left its fs.ReadStream paused and
      // its descriptor open for the life of the process (measured 2026-09-13,
      // scratchpad gwfix/pipeleak.cjs: +20 fds after 20 aborted sends of a
      // 32 MiB file; 0 with the destroy).
      req.once('close', () => stream.destroy());
      stream.pipe(req);
    } else if (source === 'client') {
      // pumped once connected
    } else {
      req.end();
    }
  }

  kick() {
    if (this.responded) return;
    clearTimeout(this.watchdog);
    this.watchdog = setTimeout(() => {
      this.watchdog = null;
      this.req.destroy(tagged('upstream silent', 'UPSTREAM_SILENT'));
    }, this.relay.s.upstreamSilenceMs);
  }

  clearWatchdog() {
    clearTimeout(this.watchdog);
    this.watchdog = null;
  }

  settle(result) {
    if (this.settled) return;
    this.settled = true;
    this.resolveDone(result);
  }

  /** Client body → orchestrator, with backpressure both ways. */
  pumpClient() {
    const relay = this.relay;
    const client = relay.req;
    const up = this.req;
    if (relay.clientBodyDone) {
      this.clientEnded = true;
      up.end();
      return;
    }
    let draining = false;
    const onData = (chunk) => {
      if (relay.idle) relay.idle.touch();
      this.bodyBytes += chunk.length;
      relay.clientBodyRead += chunk.length;
      if (relay.clientBodyRead > relay.rule.cap) {
        relay.overCap = true;
        cleanup();
        client.pause();
        up.destroy(tagged('client body over cap', 'BODY_OVER_CAP'));
        return;
      }
      this.kick();
      if (!up.write(chunk) && !draining) {
        draining = true;
        client.pause();
        if (relay.idle) relay.idle.hold();
        up.once('drain', () => {
          draining = false;
          if (relay.idle) relay.idle.release();
          client.resume();
        });
      }
    };
    const onEnd = () => {
      cleanup();
      relay.clientBodyDone = true;
      this.clientEnded = true;
      up.end();
    };
    const cleanup = () => {
      client.off('data', onData);
      client.off('end', onEnd);
    };
    this.unpump = () => {
      cleanup();
      client.pause();
    };
    client.on('data', onData);
    client.once('end', onEnd);
    if (relay.idle) relay.idle.release();
    client.resume();
  }

  abort() {
    this.clearWatchdog();
    this.unpump();
    if (!this.req.destroyed) this.req.destroy();
  }
}

/* -------------------------------------------------------------- relay -- */

class Relay {
  constructor(gw, req, res, target) {
    this.gw = gw;
    this.s = gw.settings;
    this.req = req;
    this.res = res;
    this.target = target;
    this.method = req.method;
    this.rule = B.bodyRuleFor(req.method, target.route, this.s.env);
    this.attemptId = randomUUID();

    this.body = null;
    this.streamed = this.rule.mode === 'stream';
    this.clientBodyRead = 0;
    this.clientBodyDone = this.rule.mode === 'none';
    this.overCap = false;
    this.idle = null;

    this.current = null;
    this.precommitDeadline = null;
    this.commitTimer = null;
    this.forceFresh = false; // a pooled socket failed: every later attempt connects anew
    this.shapeCache = undefined;

    this.committed = false; // a byte (or a whole answer) reached the client
    this.selfCommitted = false;
    this.adopted = false; // the orchestrator's answer replaced a self-commit
    this.pendingHead = null;
    this.keepLength = false;
    this.shape = null; // 'sse' | 'json' | 'opaque'
    this.runRef = { kind: 'absent' };
    this.lastSeq = 0;
    this.seqReliable = true;
    this.terminal = false;
    this.jsonStarted = false;
    this.held = null;
    this.holdTimer = null;

    this.lastClientWrite = now();
    this.heartbeatTimer = null;
    this.absentSince = null;
    this.sleepers = new Set();
    this.clientGone = false;
    this.aborted = false;
    this.finished = false;
    this.startedAt = now();

    gw.registry.add(this);
    res.on('close', () => {
      if (!res.writableFinished) this.onClientGone();
    });
  }

  log(event, fields = {}) {
    this.gw.log(event, { attempt: this.attemptId, method: this.method, route: this.target.route, ...fields });
  }

  /* ------------------------------------------------------------- run -- */

  async run() {
    try {
      if (this.rule.mode === 'none' && this.declaresBody()) {
        // WHY (2026-09-13, review finding, reproduced with scratchpad
        // p4_getbody.cjs): a GET, HEAD or OPTIONS body is never read, so
        // neither the cap nor the body-idle guard ever ran on it. `GET
        // /v1/models` declaring 1 GiB and sending nothing was answered and
        // its socket still held 10 s later with no timer; the same POST was
        // destroyed by the idle guard. Their cap is 0 bytes.
        this.refuseTooLarge();
        return;
      }
      if (this.rule.mode !== 'none' && !(await this.readRequestBody())) return;
      let step = await this.precommit();
      while (step && !this.clientGone && !this.aborted) {
        let failed = Boolean(step.failed);
        if (step.res) {
          const out = await this.consume(step.res, step.reattached === true);
          if (out === 'precommit-failed') {
            // A head arrived: the orchestrator had the whole request.
            if (!this.retryAfterAccepted()) {
              await this.refuseUnknownOutcome('EHEADDIED');
              break;
            }
            step = await this.precommit({ afterFailure: 'EHEADDIED' });
            continue;
          }
          failed = out === 'failed';
        }
        if (!failed) break;
        const plan = R.planReattach(this.reattachState());
        if (!plan.ok) {
          this.log('relay_abort', { reason: plan.reason });
          this.abortClient();
          break;
        }
        step = await this.reattach(plan);
      }
    } catch (err) {
      this.log('relay_error', { error: errorCode(err) });
      if (!this.res.headersSent && !this.clientGone) {
        this.sendEdge(H.edgeError('internal_error', 'Something went wrong on our side.'));
      } else {
        this.abortClient();
      }
    } finally {
      this.finish();
    }
  }

  declaresBody() {
    const length = this.req.headers['content-length'];
    if (length !== undefined && Number(length) !== 0) return true;
    return this.req.headers['transfer-encoding'] !== undefined;
  }

  /** A request whose answer is the same however many times it is computed. */
  idempotentRequest() {
    if (this.method === 'GET' || this.method === 'HEAD' || this.method === 'OPTIONS') return true;
    return this.method === 'POST' && B.DETERMINISTIC_JSON_ROUTES.has(this.target.route);
  }

  /**
   * May a request the orchestrator may already hold whole be sent again?
   *
   * WHY (2026-09-13, review finding, reproduced with scratchpad
   * p6_precommit_dup.cjs): every pre-commit failure was retried, including
   * one after the orchestrator had read the whole body. An orchestrator that
   * read POST /v1/responses and dropped the socket 1 s later was sent it
   * again with attachEvidence false: two launches of one generation. The
   * design retries CONNECT-phase failures; after the write, a second send is
   * safe only where it cannot do the work twice:
   *  - GET, HEAD, OPTIONS, and the deterministic embeddings and rerank;
   *  - the attempt-keyed routes, once this orchestrator has shown it
   *    attaches (the same evidence rule as re-attach), or V1_GATEWAY_ATTACH=on;
   *  - the Files design's idempotent upload calls (a part PUT, complete, cancel).
   * A create or a DELETE sent twice is a second object or a 404 for a delete
   * that succeeded, so those are answered instead (refuseUnknownOutcome).
   */
  retryAfterAccepted() {
    const route = this.target.route;
    if (this.idempotentRequest()) return true;
    if (this.method === 'POST' && ATTEMPT_KEYED_ROUTES.has(route)) {
      return this.s.attach === 'on' || (this.s.attach !== 'off' && this.gw.attachEvidence === true);
    }
    if (this.method === 'PUT' && UPLOAD_PART_PUT.test(route)) return true;
    return this.method === 'POST' && UPLOAD_STATE_CHANGE.test(route);
  }

  needsFreshConnection() {
    return this.forceFresh || !this.retryAfterAccepted();
  }

  /**
   * The orchestrator may or may not have done the work, and doing it again
   * is not safe: say so. x-should-retry: false stops openai-python and
   * openai-node retrying a 503 on their own (both read the header first), so
   * the caller decides, instead of an SDK creating a second object blind.
   */
  async refuseUnknownOutcome(code) {
    this.log('precommit_outcome_unknown', { error: code });
    const answer = H.edgeError('model_unavailable', UNAVAILABLE, { retryAfter: 30 });
    answer.headers['x-should-retry'] = 'false';
    await this.drainAndAnswer(answer);
  }

  reattachState() {
    return {
      shape: this.shape,
      run: this.runRef,
      terminal: this.terminal,
      jsonStarted: this.jsonStarted,
      seqReliable: this.seqReliable,
      lastSeq: this.lastSeq,
      replayable: this.rule.mode === 'none' || this.body !== null,
      deterministic: this.method === 'POST' && B.DETERMINISTIC_JSON_ROUTES.has(this.target.route),
      attach: this.s.attach,
      evidence: this.gw.attachEvidence,
    };
  }

  /* ------------------------------------------------------------ body -- */

  async readRequestBody() {
    const declaredRaw = this.req.headers['content-length'];
    const cap = this.rule.cap;
    if (declaredRaw !== undefined) {
      const declared = Number(declaredRaw);
      if (!Number.isFinite(declared) || declared > cap) {
        this.refuseTooLarge();
        return false;
      }
    }
    if (this.rule.requireLength && declaredRaw === undefined) {
      this.sendEdge(
        H.edgeError('invalid_request_error', 'This request needs a Content-Length header.'),
        { close: true },
      );
      return false;
    }
    this.idle = new B.BodyIdleGuard(this.req, this.s.bodyIdleMs, () => this.onBodyIdle());
    if (this.rule.mode === 'stream') {
      // Not reading yet: the wait for the orchestrator's socket is ours.
      this.idle.hold();
      return true;
    }
    return this.bufferBody();
  }

  async bufferBody() {
    if (this.idle) this.idle.release();
    const declaredRaw = this.req.headers['content-length'];
    const result = await B.readBufferedBody(this.req, {
      cap: this.rule.cap,
      memoryBytes: this.s.memoryBodyBytes,
      spoolDir: this.s.spoolDir,
      idle: this.idle,
      budget: this.gw.spoolBudget || null,
      memory: this.gw.memoryBudget || null,
      declared: declaredRaw === undefined ? null : Number(declaredRaw),
      scan: B.shapeNeedsBody(this.method, this.target.route),
    });
    if (result.over) {
      this.refuseTooLarge();
      return false;
    }
    if (result.gone) return false;
    if (result.spoolRefused) {
      // Closed, not drained: reading the rest is what a flood wants.
      this.log('spool_refused', { reason: result.spoolRefused });
      this.sendEdge(H.edgeError('model_unavailable', UNAVAILABLE, { retryAfter: 30 }), { close: true });
      return false;
    }
    if (result.storage) {
      this.log('spool_failed');
      this.sendEdge(H.edgeError('model_unavailable', UNAVAILABLE, { retryAfter: 60 }), { close: true });
      return false;
    }
    this.body = result.body;
    this.clientBodyDone = true;
    this.clientBodyRead = result.body.size;
    return true;
  }

  onBodyIdle() {
    this.log('body_idle', { idle_ms: this.s.bodyIdleMs });
    this.aborted = true;
    if (this.current) this.current.abort();
    this.req.socket?.destroy();
  }

  refuseTooLarge() {
    this.sendEdge(
      H.edgeError('request_too_large', `The request body is larger than the ${this.rule.cap} byte limit.`),
      { close: true },
    );
  }

  /* ------------------------------------------------------ pre-commit -- */

  async precommit({ afterFailure = null } = {}) {
    if (this.precommitDeadline === null) this.precommitDeadline = now() + this.s.precommitRetryMs;
    if (afterFailure !== null && !(await this.precommitPause(afterFailure, 0))) return null;
    for (let tries = 1; ; tries += 1) {
      if (this.clientGone || this.aborted) return null;
      if (this.streamed && this.body === null && this.clientBodyRead > 0) {
        // An earlier attempt consumed part of a streamed body and its socket
        // then failed after the response head: those bytes cannot be re-sent.
        this.log('precommit_body_lost', { body_bytes: this.clientBodyRead });
        await this.drainAndAnswer(H.edgeError('model_unavailable', UNAVAILABLE, { retryAfter: 30 }));
        return null;
      }
      const attempt = this.openAttempt({});
      const result = await attempt.done;
      this.clearCommitTimer();
      if (this.clientGone || this.aborted) return null;
      if (result.res) {
        if (tries > 1) this.log('precommit_recovered', { tries, waited_ms: Math.round(now() - this.startedAt) });
        return { res: result.res };
      }
      const code = errorCode(result.error);
      if (this.overCap) {
        await this.drainAndAnswer(H.edgeError('request_too_large', `The request body is larger than the ${this.rule.cap} byte limit.`));
        return null;
      }
      if (this.committed) return { failed: true, code };
      if (this.streamed && this.body === null) {
        if (attempt.bodyBytes > 0) {
          // Part of the body went to a socket that is gone; it cannot be sent
          // again. The SDK gets a retryable answer it can see (Files §12.3).
          this.log('precommit_body_lost', { error: code, body_bytes: attempt.bodyBytes });
          await this.drainAndAnswer(H.edgeError('model_unavailable', UNAVAILABLE, { retryAfter: 30 }));
          return null;
        }
        // Nothing consumed: keep the client writing (Cloudflare's 30 s write
        // timeout) into the spool, so every retry can send the whole body.
        if (!(await this.bufferBody())) return null;
        this.streamed = false;
      }
      if (attempt.accepted && !this.retryAfterAccepted()) {
        await this.refuseUnknownOutcome(code);
        return null;
      }
      if (attempt.req.reusedSocket && !this.forceFresh) {
        // A pooled socket the orchestrator had closed while idle: try again
        // at once on a new connection, not after the 2 s restart interval
        // (review finding 2026-09-13: +2,006 ms on each of the two such
        // requests seen against real uvicorn).
        this.forceFresh = true;
        this.log('precommit_stale_socket', { error: code });
        continue;
      }
      if (!(await this.precommitPause(code, tries))) return null;
    }
  }

  /**
   * Wait out one retry interval inside the 110 s budget; false when the
   * budget is spent (the 503 has been sent) or the client is gone. Every
   * pre-commit failure passes through here — including an answer whose
   * connection died after its head — so no failure mode can retry in a
   * tight loop.
   */
  async precommitPause(code, tries) {
    const left = this.precommitDeadline - now();
    if (left <= 0) {
      this.log('precommit_exhausted', { tries, error: code, waited_ms: Math.round(now() - this.startedAt) });
      this.sendEdge(H.edgeError('model_unavailable', UNAVAILABLE, { retryAfter: 30 }));
      return false;
    }
    if (tries <= 1) this.log('precommit_retrying', { error: code });
    return this.sleep(Math.min(this.s.precommitRetryIntervalMs, left));
  }

  openAttempt({ extraHeaders = {}, emptyBody = false }) {
    const headers = H.upstreamHeaders(this.req.headers, this.s);
    headers['x-techsara-attempt'] = this.attemptId;
    Object.assign(headers, extraHeaders);
    let source = 'none';
    if (emptyBody) {
      headers['content-length'] = '0';
      source = 'empty';
    } else if (this.body) {
      headers['content-length'] = String(this.body.size);
      source = 'buffer';
    } else if (this.streamed) {
      const declared = this.req.headers['content-length'];
      if (declared !== undefined) headers['content-length'] = declared;
      // Keep-alive on the wire even though the socket is never reused: a
      // `Connection: close` request lets the orchestrator close right after
      // an early 401/413, and the part still being written then turns that
      // answer into EPIPE (measured 2026-09-13: 8 MiB part, early 401 lost
      // as EPIPE after 290,617 bytes with close; relayed with keep-alive).
      headers.connection = 'keep-alive';
      source = 'client';
    }
    const attempt = new Attempt(this, headers, source);
    this.current = attempt;
    return attempt;
  }

  onAttemptAccepted(attempt) {
    if (this.committed || attempt !== this.current || this.precommitDeadline === null) return;
    const shape = this.inferShape();
    if (!shape) return;
    // Commit at 15 s of silence, or at the 110 s pre-commit deadline if that
    // comes first — but never sooner than 1 s after THIS attempt was sent.
    // Without the floor, an attempt made at the deadline committed on a 0 ms
    // timer, before a healthy orchestrator's head could arrive, and a refusal
    // that should have been a status became an in-band failure (measured
    // 2026-09-13: the dead-head test timed out in 3 of 6 runs; 1 s + 110 s
    // is still inside Cloudflare's 125 s first-byte wall).
    const floor = now() + Math.min(1000, this.s.heartbeatMs);
    const at = Math.min(now() + this.s.heartbeatMs, Math.max(this.precommitDeadline, floor));
    this.clearCommitTimer();
    this.commitTimer = setTimeout(() => {
      this.commitTimer = null;
      if (this.committed || this.current !== attempt || attempt.settled || this.clientGone) return;
      this.selfCommit(shape);
    }, Math.max(0, at - now()));
  }

  inferShape() {
    if (this.shapeCache !== undefined) return this.shapeCache;
    const needs = B.shapeNeedsBody(this.method, this.target.route);
    if (needs && !this.body) return null;
    // The body was scanned as it was read (bodies.cjs StreamFlagScanner):
    // no parse, no read of the spool file, nothing retained.
    this.shapeCache = B.inferShape(this.method, this.target, needs ? this.body.summary() : null);
    return this.shapeCache;
  }

  clearCommitTimer() {
    clearTimeout(this.commitTimer);
    this.commitTimer = null;
  }

  selfCommit(shape) {
    this.selfCommitted = true;
    this.shape = shape;
    const headers =
      shape === 'sse'
        ? { ...H.NEXT_STATIC_HEADERS, ...H.SSE_HEADERS }
        : { ...H.NEXT_STATIC_HEADERS, ...H.COMMITTED_JSON_HEADERS };
    this.res.writeHead(200, headers);
    this.committed = true;
    this.writeClient(shape === 'sse' ? comment('ping') : ' ');
    this.startHeartbeat();
    this.log('self_commit', { shape, waited_ms: Math.round(now() - this.startedAt) });
  }

  /* ---------------------------------------------------------- consume -- */

  async consume(upRes, reattached) {
    const attempt = this.current;
    const status = upRes.statusCode;
    const contentType = upRes.headers['content-type'];
    this.noteRun(upRes);

    if (H.REDIRECT_STATUSES.has(status)) {
      upRes.resume();
      attempt.abort();
      this.log('upstream_redirect', { status });
      if (!this.committed) this.sendEdge(H.edgeError('internal_error', 'Something went wrong on our side.'));
      else this.abortClient();
      return 'done';
    }

    if (attempt.source === 'client' && !attempt.clientEnded) return this.earlyResponse(upRes, attempt);

    const sse = H.isEventStream(contentType);
    if (this.selfCommitted && !this.adopted && !reattached) {
      const matches = status === 200 && (this.shape === 'sse') === sse;
      if (!matches) {
        if (status === 502 || status === 503 || status === 504) {
          upRes.resume();
          attempt.abort();
          return 'failed';
        }
        this.log('late_refusal', { status });
        if (this.shape === 'sse') {
          const bytes = await readSmall(upRes, this.s.earlyResponseMaxBytes, this.s.upstreamSilenceMs);
          let envelope = null;
          try {
            envelope = bytes ? JSON.parse(bytes.toString('utf8')) : null;
          } catch {
            envelope = null;
          }
          this.writeClient(errorFrame(envelope));
          this.endClient();
        } else {
          // A 200 is on the wire; a refusal written as its body would parse
          // as a successful object in both SDKs. Cutting the connection makes
          // the SDK retry, and the retry is answered with the real status.
          upRes.resume();
          attempt.abort();
          this.abortClient();
        }
        return 'done';
      }
    }
    if (this.selfCommitted || reattached) this.adopted = true;

    if (!this.committed) {
      if (H.NULL_BODY_STATUSES.has(status) || this.method === 'HEAD') {
        upRes.resume();
        this.res.writeHead(status, H.clientHeaders(upRes.headers, {}));
        this.committed = true;
        this.res.end();
        return 'done';
      }
      // A text transcript committed by the orchestrator carries leading
      // spaces by contract (no-timeout design edge_100s §7), so it is as
      // whitespace-safe as JSON.
      const textTranscript = this.target.route === 'audio/transcriptions' && H.mediaType(contentType) === 'text/plain';
      this.shape = sse ? 'sse' : H.isJson(contentType) || textTranscript ? 'json' : 'opaque';
      const keepLength = !sse && B.isByteDownload(this.method, this.target.route);
      this.pendingHead = { status, headers: H.clientHeaders(upRes.headers, { sse, keepLength }) };
      this.keepLength = keepLength;
    }
    return this.pipeBody(upRes, attempt);
  }

  noteRun(upRes) {
    const header = upRes.headers['x-techsara-run'];
    this.runRef = R.parseRun(header);
    if (typeof header === 'string') {
      this.gw.attachEvidence = true;
    } else if (
      this.method === 'POST' &&
      GENERATION_ROUTES.has(this.target.route) &&
      upRes.statusCode >= 200 &&
      upRes.statusCode < 300
    ) {
      // An orchestrator that launched a generation without naming its run
      // does not speak the protocol (a rollback, or a build before T3).
      this.gw.attachEvidence = false;
    }
  }

  pipeBody(upRes, attempt) {
    return new Promise((resolve) => {
      const parser = this.shape === 'sse' ? new SseParser() : null;
      let watchdog = null;
      let paused = false;
      let settled = false;
      const kick = () => {
        clearTimeout(watchdog);
        if (paused) return;
        watchdog = setTimeout(
          () => upRes.destroy(tagged('upstream silent', 'UPSTREAM_SILENT')),
          this.s.upstreamSilenceMs,
        );
      };
      const done = (out) => {
        if (settled) return;
        settled = true;
        clearTimeout(watchdog);
        this.pipeCancel = null;
        resolve(out);
      };
      this.pipeCancel = () => {
        upRes.destroy();
        done('done');
      };
      // A head not yet relayed gets a full heartbeat interval from its own
      // arrival before a keepalive byte may commit it.
      if (!this.committed) this.lastClientWrite = now();
      this.startHeartbeat();
      kick();
      // Listener bodies run outside run()'s try: a throw here would be an
      // uncaught exception and take every relay in the process down with it.
      const guard = (fn) => (...args) => {
        try {
          fn(...args);
        } catch (err) {
          this.log('relay_error', { error: errorCode(err) });
          upRes.destroy();
          this.abortClient();
          done('done');
        }
      };
      upRes.on('data', guard((chunk) => {
        kick();
        // A body byte is proof the orchestrator is present: only now does a
        // re-attach's absence clock stop. A head alone is not — an
        // orchestrator that answers a head and dies, again and again, must
        // still run out the 1,800 s budget.
        this.absentSince = null;
        if (parser) {
          for (const frame of parser.push(chunk)) this.onFrame(frame);
        } else {
          this.relayRaw(chunk);
        }
        if (this.res.writableNeedDrain && !paused) {
          paused = true;
          clearTimeout(watchdog);
          upRes.pause();
          this.res.once('drain', () => {
            paused = false;
            upRes.resume();
            kick();
          });
        }
      }));
      upRes.on('end', guard(() => {
        this.releaseHeld(true);
        this.endClient();
        done('done');
      }));
      const onClose = () => {
        if (settled || upRes.complete) return;
        this.dropHeld();
        if (!this.committed && this.pendingHead) {
          // Nothing reached the client: forget this answer's head and its
          // heartbeat, or a heartbeat during the retry sleep would commit a
          // status the next attempt may contradict.
          this.pendingHead = null;
          this.keepLength = false;
          this.stopHeartbeat();
          done('precommit-failed');
          return;
        }
        this.log('upstream_failed', { committed: this.committed, shape: this.shape, last_seq: this.lastSeq });
        attempt.abort();
        done('failed');
      };
      upRes.on('close', onClose);
      upRes.on('error', () => undefined);
      // Defensive, not observed: a head and the connection's death can be
      // parsed in one read, and node emits 'close' on process.nextTick, which
      // runs before this promise continuation. Waiting for a 'close' that
      // already fired would hang the relay forever.
      if (upRes.destroyed || upRes.closed) onClose();
    });
  }

  onFrame(frame) {
    if (frame.kind === 'marker') {
      this.confirm(frame.seq);
      return;
    }
    if (frame.kind === 'comment') {
      this.writeFrame(frame.text);
      if (frame.seq !== null) this.confirm(frame.seq);
      return;
    }
    if (this.held) this.releaseHeld(false);
    if (frame.seq !== null) {
      if (frame.seq <= this.lastSeq) return; // replayed twice; already delivered
      this.writeEvent(frame);
      this.lastSeq = frame.seq;
      return;
    }
    if (this.runRef.kind === 'response' || this.runRef.kind === 'job') {
      // Hold the event until its `: ts-seq=N` confirms it, so a crash between
      // the two can never leave the client holding a frame the re-attach
      // will send again. Both run kinds re-attach with Resume-After (review
      // finding 2026-09-13, scratchpad p7_job_dup.cjs: an audio job's frame
      // written just before a crash reached the client twice, d1 d2 d2 d3).
      this.held = frame;
      this.holdTimer = setTimeout(() => this.releaseHeld(false), this.s.seqHoldMs);
      return;
    }
    this.writeEvent(frame);
  }

  confirm(seq) {
    if (seq === null) return;
    if (this.held) {
      const frame = this.held;
      this.held = null;
      clearTimeout(this.holdTimer);
      this.holdTimer = null;
      if (seq > this.lastSeq) this.writeEvent(frame);
    }
    if (seq > this.lastSeq) this.lastSeq = seq;
  }

  releaseHeld(ending) {
    if (!this.held) return;
    const frame = this.held;
    this.held = null;
    clearTimeout(this.holdTimer);
    this.holdTimer = null;
    if (!ending) this.seqReliable = false;
    this.writeEvent(frame);
  }

  dropHeld() {
    this.held = null;
    clearTimeout(this.holdTimer);
    this.holdTimer = null;
  }

  writeEvent(frame) {
    this.writeFrame(frame.text);
    if (frame.terminal) this.terminal = true;
  }

  writeFrame(text) {
    this.ensureHead();
    this.writeClient(text);
  }

  relayRaw(chunk) {
    this.ensureHead();
    if (this.shape === 'json' && !this.jsonStarted && hasJsonNonWhitespace(chunk)) this.jsonStarted = true;
    this.writeClient(chunk);
  }

  ensureHead() {
    if (!this.pendingHead) return;
    const { status, headers } = this.pendingHead;
    this.pendingHead = null;
    if (!this.res.headersSent) this.res.writeHead(status, headers);
    this.committed = true;
  }

  writeClient(data) {
    if (this.clientGone || this.res.destroyed) return false;
    this.lastClientWrite = now();
    return this.res.write(data);
  }

  endClient() {
    if (this.clientGone || this.res.destroyed) return;
    this.ensureHead();
    if (!this.res.headersSent) this.res.writeHead(200, H.clientHeaders({}, {}));
    this.committed = true;
    this.res.end();
  }

  /* -------------------------------------------------------- heartbeat -- */

  startHeartbeat() {
    if (this.heartbeatTimer || this.finished) return;
    const schedule = (delay) => {
      this.heartbeatTimer = setTimeout(tick, Math.max(5, delay));
      this.heartbeatTimer.unref?.();
    };
    const tick = () => {
      this.heartbeatTimer = null;
      if (this.finished || this.clientGone || this.res.writableEnded) return;
      const due = this.lastClientWrite + this.s.heartbeatMs;
      if (now() + 5 >= due) {
        if (!this.heartbeat()) this.lastClientWrite = now();
      }
      schedule(this.lastClientWrite + this.s.heartbeatMs - now());
    };
    schedule(this.lastClientWrite + this.s.heartbeatMs - now());
  }

  stopHeartbeat() {
    clearTimeout(this.heartbeatTimer);
    this.heartbeatTimer = null;
  }

  /** One keepalive byte where it cannot change the body; false if none. */
  heartbeat() {
    if (this.res.writableNeedDrain || this.keepLength) return false;
    if (this.shape === 'sse') {
      this.ensureHead();
      this.writeClient(comment('ping'));
      return true;
    }
    if (this.shape === 'json' && !this.jsonStarted) {
      this.ensureHead();
      this.writeClient(' ');
      return true;
    }
    return false;
  }

  /* --------------------------------------------------------- early answer -- */

  async earlyResponse(upRes, attempt) {
    attempt.unpump();
    const bytes = await readSmall(upRes, this.s.earlyResponseMaxBytes, this.s.upstreamSilenceMs);
    attempt.abort();
    const envelope =
      bytes === null
        ? H.edgeError('model_unavailable', UNAVAILABLE, { retryAfter: 30 })
        : { status: upRes.statusCode, headers: H.clientHeaders(upRes.headers, {}), body: bytes };
    this.log('early_response', { status: envelope.status });
    await this.drainAndAnswer(envelope);
    return 'done';
  }

  /** Files design §12.3: drain the client body, then answer; destroy only over the cap. */
  async drainAndAnswer(answer) {
    if (this.idle) this.idle.release();
    const drained = await B.drainClientBody(this.req, this.rule.cap, this.clientBodyRead, this.idle);
    if (drained.gone || this.clientGone) return;
    this.sendEdge(answer, { close: drained.over });
  }

  /* ---------------------------------------------------------- re-attach -- */

  async reattach(plan) {
    this.startHeartbeat();
    if (this.absentSince === null) this.absentSince = now();
    const started = this.absentSince;
    const deadline = started + this.s.reattachMaxMs;
    this.gw.stats.reattachStarted += 1;
    this.log('reattach_start', { shape: this.shape, last_seq: this.lastSeq, run: this.runRef.kind });
    const result = await R.reattachLoop({
      deadline,
      now,
      backoff: (i) => R.backoffMs(i, this.s.reattachBackoffMinMs, this.s.reattachBackoffMaxMs),
      sleep: (ms) => this.sleep(ms),
      tryOnce: async () => {
        if (this.clientGone || this.aborted) return { outcome: 'cancelled' };
        const headers = { ...plan.headers };
        if (this.shape === 'sse') headers['x-techsara-resume-after'] = String(this.lastSeq);
        const attempt = this.openAttempt({ extraHeaders: headers, emptyBody: plan.emptyBody });
        const r = await attempt.done;
        if (this.clientGone || this.aborted) {
          attempt.abort();
          return { outcome: 'cancelled' };
        }
        if (r.error) return { outcome: 'retry', error: errorCode(r.error) };
        const verdict = R.classifyAttachResponse(r.res.statusCode, r.res.headers['content-type'], this.shape);
        if (verdict === 'relay') {
          const header = r.res.headers['x-techsara-run'];
          const same = R.attachAnswerMatches({
            expected: this.runRef,
            header,
            shape: this.shape,
            idempotent: this.idempotentRequest(),
          });
          if (same) return { outcome: 'relay', res: r.res };
          // Not the run the client holds: splicing it would hand the SDK two
          // generations as one. Cut the client; its retry starts clean.
          r.res.resume();
          attempt.abort();
          if (typeof header !== 'string') this.gw.attachEvidence = false;
          return { outcome: 'abort', status: r.res.statusCode, reason: 'run_mismatch' };
        }
        r.res.resume();
        attempt.abort();
        return { outcome: verdict, status: r.res.statusCode };
      },
    });
    const absentMs = Math.round(now() - started);
    if (result.outcome === 'relay') {
      this.gw.stats.reattachSucceeded += 1;
      this.log('reattach_ok', {
        tries: result.tries,
        absent_ms: absentMs,
        last_seq: this.lastSeq,
        reattach_total: this.gw.stats.reattachSucceeded,
      });
      return { res: result.res, reattached: true };
    }
    this.gw.stats.reattachFailed += 1;
    this.log('reattach_abort', {
      outcome: result.outcome,
      tries: result.tries,
      absent_ms: absentMs,
      status: result.status ?? null,
      reason: result.reason ?? null,
    });
    if (result.outcome !== 'cancelled') this.abortClient();
    return null;
  }

  /* ------------------------------------------------------------ endings -- */

  sendEdge(answer, { close = false } = {}) {
    if (this.res.headersSent || this.clientGone || this.res.destroyed) return;
    const headers = { ...answer.headers };
    if (close) headers.connection = 'close';
    this.res.writeHead(answer.status, headers);
    this.committed = true;
    this.res.end(answer.body, () => {
      if (close) this.req.socket?.destroy();
    });
  }

  abortClient() {
    this.aborted = true;
    this.dropHeld();
    if (!this.res.destroyed) this.res.destroy();
    this.req.socket?.destroy();
  }

  onClientGone() {
    if (this.clientGone) return;
    this.clientGone = true;
    if (this.current) this.current.abort();
    if (this.pipeCancel) this.pipeCancel();
    for (const wake of this.sleepers) wake(false);
    this.sleepers.clear();
  }

  /** Drain: the gateway is stopping. Clients see an incomplete read and resume. */
  abortForDrain() {
    this.log('drain_abort', { committed: this.committed });
    this.aborted = true;
    if (this.current) this.current.abort();
    if (this.pipeCancel) this.pipeCancel();
    if (!this.res.destroyed) this.res.destroy();
    this.req.socket?.destroy();
    for (const wake of this.sleepers) wake(false);
    this.sleepers.clear();
  }

  sleep(ms) {
    if (this.clientGone || this.aborted) return Promise.resolve(false);
    return new Promise((resolve) => {
      const wake = (alive) => {
        clearTimeout(timer);
        this.sleepers.delete(wake);
        resolve(alive);
      };
      const timer = setTimeout(() => wake(!this.clientGone && !this.aborted), ms);
      this.sleepers.add(wake);
    });
  }

  finish() {
    if (this.finished) return;
    this.finished = true;
    this.clearCommitTimer();
    this.stopHeartbeat();
    this.dropHeld();
    if (this.current && !this.current.settled) this.current.abort();
    if (this.body) this.body.dispose();
    if (this.idle) this.idle.stop();
    this.gw.registry.delete(this);
  }
}

module.exports = { Relay, Attempt, readSmall, hasJsonNonWhitespace };
