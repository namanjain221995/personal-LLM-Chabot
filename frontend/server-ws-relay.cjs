'use strict';
/**
 * server-ws-relay.cjs — carries a live dictation's WebSocket from the browser
 * to the orchestrator (2026-09-29, real-time dictation build spec §4).
 * server-preload.cjs installs it on Next's http.Server; it has no effect of
 * its own when required.
 *
 * WHY A RELAY, AND WHY HERE. Words should appear while a person is still
 * talking, so the browser streams PCM over a WebSocket and the orchestrator
 * streams the transcript back. Browsers only ever reach this process — the
 * tunnel maps one hostname to frontend:3000 and the orchestrator is published
 * to nobody — and nothing inside Next can carry the socket:
 *
 *  · a route handler never sees an upgrade at all;
 *  · Next's own upgrade handler (next/dist/server/lib/router-server.js)
 *    `socket.end()`s a WebSocket whose path matches any route or file, even
 *    after another listener has answered 101, and /api/audio/sessions/[id]
 *    is a route (measured against a 16.3.3 standalone build, 2026-09-29);
 *  · a rewrite is frozen into the build and forwards every header the client
 *    sent — Origin, Cookie, a forged cf-connecting-ip — which is the
 *    laundering lib/proxy.ts was hardened against.
 *
 * The preload holds the server before Next registers anything on it, so that
 * is where this is installed.
 *
 * WHAT IT OWNS: OWNED_PATH and nothing else, the query string ignored. Its
 * 'upgrade' listener is prepended, and every 'upgrade' listener registered on
 * the server after it — Next's — is wrapped so that it returns without ever
 * seeing an owned path. Every other upgrade is Next's business; the preload
 * destroys the ones nobody answers.
 *
 * WHAT IT CHECKS, before any connection to the orchestrator exists. A refusal
 * is a bare HTTP status with no body, because a browser never shows a
 * handshake's status to script; the reason goes to the log instead.
 *  · GET, `Upgrade: websocket`, a well-formed key, version 13.
 *  · `Sec-Fetch-Site`, when present, is same-origin or none. A sibling
 *    subdomain says same-site, and SameSite=Lax does not keep siblings out
 *    (the session proxy's rule, app/api/audio/sessions/_forward.ts).
 *  · An Origin that is the Host's own origin (hostname and effective port;
 *    the scheme cannot be known here, because TLS ends at the tunnel) or is
 *    listed in FRONTEND_WS_ALLOWED_ORIGINS. A WebSocket answer is readable
 *    cross-origin, so this is what stops a page elsewhere from opening a
 *    signed-in person's dictation socket and reading their transcript.
 *  · A ts_session cookie. Whose session it is, is the orchestrator's question.
 *  · Fewer than FRONTEND_WS_MAX_RELAYS relays already open. 0 refuses every
 *    live connection, which makes it the kill switch for this path.
 * None of this is the security boundary. The orchestrator is reachable on the
 * LAN and repeats every check itself (spec §5); this keeps junk off it.
 *
 * WHAT GOES UPSTREAM is a named header set, built by naming what goes out
 * rather than by deleting what must not (lib/proxy.ts's rule, for the same
 * reason): the handshake's own headers, cookie, origin, user-agent, the
 * original Host as x-forwarded-host, and x-forwarded-for / x-forwarded-proto
 * only as lib/proxy.ts would send them. sec-websocket-extensions is not on the
 * list — permessage-deflate on PCM is CPU spent to save nothing — and neither
 * is any forwarding header a client wrote.
 *
 * LIFETIME. The orchestrator has FRONTEND_WS_HANDSHAKE_S (15 s) to answer. A
 * non-101 answer is relayed (status and a small body) and the socket closed.
 * After a 101 the bytes are piped both ways untouched. A relay with no byte in
 * either direction for FRONTEND_WS_IDLE_S (150 s) is destroyed; uvicorn pings
 * every 20 s, so a live socket is never that quiet. On SIGTERM the preload
 * stops new relays at once and destroys the open ones at +FRONTEND_DRAIN_WS_S
 * (2 s), so they never hold Next's server.close(); the browser reconnects to
 * the new container and resumes from its own buffer.
 *
 * LOGS: one JSON line per event, like the preload's — ws_refused (reason; the
 * Origin value only when the Origin was the reason), ws_open, ws_close
 * (reason, duration_ms, bytes_up, bytes_down, upstream_status). Never a
 * cookie, a query string or a session id.
 *
 * `next dev` does not load the preload; to try the live path there, start it
 * with NODE_OPTIONS='--require ./server-preload.cjs'.
 */

const http = require('node:http');

/** The one path this relay answers. */
const OWNED_PATH = /^\/api\/audio\/sessions\/([A-Za-z0-9_-]{1,64})\/live$/;

/** lib/auth.ts SESSION_COOKIE; tests/server-ws-relay.test.ts pins the two together. */
const SESSION_COOKIE = 'ts_session';

const DEFAULTS = Object.freeze({
  orchestratorUrl: 'http://localhost:8080',
  maxRelays: 512,
  idleS: 150,
  handshakeS: 15,
});

/** The largest body of a non-101 answer that is relayed; a bigger one goes without. */
const MAX_REFUSAL_BODY_BYTES = 4096;

/**
 * Once one side of an open relay has closed, how long the other gets to flush
 * what it still holds (typically the closing frame) and close on its own.
 */
const CLOSE_GRACE_MS = 2000;

const DEFAULT_PORT = Object.freeze({ 'http:': 80, 'https:': 443 });

/** base64 of 16 bytes, as RFC 6455 §4.1 requires of Sec-WebSocket-Key. */
const WS_KEY = /^[A-Za-z0-9+/]{22}==$/;
/** base64 of a SHA-1: the only shape Sec-WebSocket-Accept can take. */
const WS_ACCEPT = /^[A-Za-z0-9+/]{27}=$/;
/** One HTTP token: the subprotocol the orchestrator selected. */
const TOKEN = /^[!#$%&'*+.^_`|~0-9A-Za-z-]+$/;
/** A header value this file will write raw onto a socket. */
const PRINTABLE = /^[\x20-\x7e]{1,256}$/;

function log(event, fields = {}) {
  // One JSON line, like the preload's and the gateway's, so every edge greps the same way.
  process.stdout.write(`${JSON.stringify({ component: 'server-ws-relay', event, ...fields })}\n`);
}

/** The session id when `url` is an owned path, else null. */
function ownedSessionId(url) {
  const match = OWNED_PATH.exec(String(url ?? '').split('?')[0]);
  return match ? match[1] : null;
}

/* -------------------------------------------- lib/proxy.ts, restated -- */
//
// PARITY. lib/proxy.ts is TypeScript inside the Next bundle, and this file runs
// outside it, before Next has loaded, so its forwarding rules are restated here
// exactly as gateway/lib/headers.cjs restates them for the v1-gateway. They
// must never drift apart: tests/server-ws-relay.test.ts feeds both copies the
// same table of values on every run and fails the moment they disagree.

const IPV4 =
  /^(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)(\.(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)){3}$/;

/** lib/proxy.ts `isIpv6`: the same group counting. */
function isIpv6(value) {
  const halves = value.split('::');
  if (halves.length > 2) return false;
  const groups = [];
  for (const half of halves) {
    if (half !== '') groups.push(...half.split(':'));
  }
  let count = 0;
  for (let i = 0; i < groups.length; i += 1) {
    const group = groups[i];
    if (group.includes('.')) {
      if (i !== groups.length - 1 || !IPV4.test(group)) return false;
      count += 2;
      continue;
    }
    if (!/^[0-9a-f]{1,4}$/i.test(group)) return false;
    count += 1;
  }
  return halves.length === 2 ? count <= 7 : count === 8;
}

/** lib/proxy.ts `isIpLiteral`. */
function isIpLiteral(value) {
  if (typeof value !== 'string' || value.length === 0 || value.length > 45) return false;
  return IPV4.test(value) || isIpv6(value);
}

/**
 * lib/proxy.ts `trustedClientIp`: only the header the deployment NAMED in
 * TRUSTED_CLIENT_IP_HEADER, only its first entry, only as one IP literal.
 * `headers` is node's lower-cased IncomingMessage.headers.
 */
function trustedClientIp(headers, headerName) {
  if (!headerName) return null;
  const raw = headers[headerName];
  if (typeof raw !== 'string' || !raw) return null;
  const first = raw.split(',')[0].trim();
  return isIpLiteral(first) ? first : null;
}

/** lib/proxy.ts `trustedForwardedProto`: stated by configuration, never read off the caller. */
function trustedForwardedProto(value) {
  const text = String(value ?? '').trim().toLowerCase();
  return text === 'https' || text === 'http' ? text : null;
}

/* ------------------------------------------------------- settings -- */

function envNumber(env, name, fallback, accept) {
  const raw = String(env[name] ?? '').trim();
  if (raw === '') return fallback;
  const value = Number(raw);
  return Number.isFinite(value) && accept(value) ? value : fallback;
}

/** FRONTEND_WS_ALLOWED_ORIGINS: a comma list, each entry reduced to its origin. */
function parseAllowedOrigins(raw) {
  const origins = new Set();
  let rejected = 0;
  for (const part of String(raw ?? '').split(',')) {
    const text = part.trim();
    if (!text) continue;
    try {
      const url = new URL(text);
      if (url.protocol === 'http:' || url.protocol === 'https:') {
        origins.add(url.origin);
        continue;
      }
    } catch {
      /* counted below */
    }
    rejected += 1;
  }
  return { origins, rejected };
}

/**
 * Where relays go: ORCHESTRATOR_URL, which must be http:// (the orchestrator
 * speaks plain HTTP on the application network). null when it is anything
 * else; every owned upgrade is then refused with 502, and the ready line says so.
 */
function upstreamTarget(orchestratorUrl) {
  let url;
  try {
    url = new URL(orchestratorUrl);
  } catch {
    return null;
  }
  if (url.protocol !== 'http:' || !url.hostname) return null;
  return {
    // http.request wants an IPv6 literal without its brackets.
    hostname: url.hostname.replace(/^\[(.*)\]$/, '$1'),
    port: Number(url.port || 80),
    host: url.host,
    basePath: url.pathname.replace(/\/+$/, ''),
  };
}

function settings(env = process.env) {
  const { origins, rejected } = parseAllowedOrigins(env.FRONTEND_WS_ALLOWED_ORIGINS);
  return {
    orchestratorUrl: String(env.ORCHESTRATOR_URL ?? '').trim() || DEFAULTS.orchestratorUrl,
    allowedOrigins: origins,
    rejectedOrigins: rejected,
    maxRelays: envNumber(env, 'FRONTEND_WS_MAX_RELAYS', DEFAULTS.maxRelays, (v) => Number.isInteger(v) && v >= 0),
    idleMs: envNumber(env, 'FRONTEND_WS_IDLE_S', DEFAULTS.idleS, (v) => v > 0) * 1000,
    handshakeMs: envNumber(env, 'FRONTEND_WS_HANDSHAKE_S', DEFAULTS.handshakeS, (v) => v > 0) * 1000,
    trustedClientIpHeader: String(env.TRUSTED_CLIENT_IP_HEADER ?? '').trim().toLowerCase(),
    trustedForwardedProto: trustedForwardedProto(env.TRUSTED_FORWARDED_PROTO),
    mockMode: env.MOCK_MODE === 'true',
  };
}

/* ------------------------------------------------ the handshake rules -- */

/**
 * The Origin header as a browser writes it — a serialized origin and nothing
 * more — as a URL, or null. Anything spelled otherwise (`null`, a path, a
 * trailing slash, upper case) is not what a browser sends and is refused.
 */
function requestOrigin(value) {
  if (typeof value !== 'string' || value === '' || value.length > 2048) return null;
  let url;
  try {
    url = new URL(value);
  } catch {
    return null;
  }
  if (url.protocol !== 'http:' && url.protocol !== 'https:') return null;
  return url.origin === value ? url : null;
}

/** A Host header split into hostname and port (null when it names none), or null. */
function parseHost(value) {
  if (typeof value !== 'string') return null;
  const match = /^(\[[0-9a-f:.]+\]|[a-z0-9._-]+)(?::([0-9]{1,5}))?$/.exec(value.trim().toLowerCase());
  if (!match) return null;
  const port = match[2] === undefined ? null : Number(match[2]);
  if (port !== null && port > 65535) return null;
  return { hostname: match[1], port };
}

/**
 * Is `origin` the origin of the Host this request was sent to? Hostnames are
 * compared after the URL parser has normalised both, ports numerically, and a
 * Host without a port stands for the default port of the Origin's scheme.
 */
function hostMatchesOrigin(hostHeader, origin) {
  const host = parseHost(hostHeader);
  if (!host) return false;
  let hostname;
  try {
    hostname = new URL(`http://${host.hostname}`).hostname;
  } catch {
    return false;
  }
  if (hostname !== origin.hostname) return false;
  const defaultPort = DEFAULT_PORT[origin.protocol];
  const originPort = origin.port === '' ? defaultPort : Number(origin.port);
  return (host.port ?? defaultPort) === originPort;
}

function hasSessionCookie(cookieHeader) {
  if (typeof cookieHeader !== 'string') return false;
  for (const pair of cookieHeader.split(';')) {
    const eq = pair.indexOf('=');
    if (eq > 0 && pair.slice(0, eq).trim() === SESSION_COOKIE && pair.slice(eq + 1).trim() !== '') return true;
  }
  return false;
}

/**
 * Why this handshake is refused before anything else happens, or null. Only
 * reads `req.method`, the HTTP version and node's lower-cased headers, so each
 * rule is testable without a socket.
 */
function refusalFor(req, config) {
  const headers = req.headers ?? {};
  if (req.method !== 'GET') return { status: 405, reason: 'method', headers: { Allow: 'GET' } };
  if (String(headers.upgrade ?? '').trim().toLowerCase() !== 'websocket') {
    return { status: 400, reason: 'not_websocket' };
  }
  const http11 = req.httpVersionMajor > 1 || (req.httpVersionMajor === 1 && req.httpVersionMinor >= 1);
  if (!http11 || !WS_KEY.test(String(headers['sec-websocket-key'] ?? '').trim())) {
    return { status: 400, reason: 'bad_handshake' };
  }
  if (String(headers['sec-websocket-version'] ?? '').trim() !== '13') {
    return { status: 426, reason: 'version', headers: { 'Sec-WebSocket-Version': '13' } };
  }
  if (headers['sec-fetch-site'] !== undefined) {
    const site = String(headers['sec-fetch-site']).trim().toLowerCase();
    if (site !== 'same-origin' && site !== 'none') return { status: 403, reason: 'cross_site' };
  }
  const rawOrigin = headers.origin;
  if (rawOrigin === undefined || rawOrigin === '') return { status: 403, reason: 'origin_missing' };
  const origin = requestOrigin(rawOrigin);
  if (!origin || !(config.allowedOrigins.has(origin.origin) || hostMatchesOrigin(headers.host, origin))) {
    return { status: 403, reason: 'origin_mismatch', origin: String(rawOrigin).slice(0, 200) };
  }
  if (!hasSessionCookie(headers.cookie)) return { status: 401, reason: 'no_session' };
  return null;
}

/** Client headers that go upstream exactly as the browser sent them. */
const FORWARDED_AS_IS = Object.freeze(['sec-websocket-protocol', 'cookie', 'origin', 'user-agent']);

/**
 * Everything that goes upstream, named. `clientHeaders` is node's lower-cased
 * IncomingMessage.headers.
 */
function upstreamHeaders(clientHeaders, config, target) {
  const out = {
    host: target.host,
    connection: 'Upgrade',
    upgrade: 'websocket',
    'sec-websocket-key': String(clientHeaders['sec-websocket-key']).trim(),
    'sec-websocket-version': '13',
  };
  for (const name of FORWARDED_AS_IS) {
    const value = clientHeaders[name];
    if (typeof value === 'string' && value !== '') out[name] = value;
  }
  // The Host the browser addressed, for the orchestrator's own Origin check
  // (it believes this only from a proxy it trusts). A malformed one is not
  // passed on; the orchestrator then judges the Origin by its allowlist.
  if (parseHost(clientHeaders.host)) out['x-forwarded-host'] = String(clientHeaders.host).trim();
  const ip = trustedClientIp(clientHeaders, config.trustedClientIpHeader);
  if (ip) out['x-forwarded-for'] = ip;
  if (config.trustedForwardedProto) out['x-forwarded-proto'] = config.trustedForwardedProto;
  return out;
}

/* ------------------------------------------------------ the socket side -- */

/**
 * Answer a handshake with a plain HTTP status and close. As the `ws` library
 * does it: the socket is destroyed the moment the answer has been handed to
 * the kernel; the timer covers a peer that never lets it drain.
 */
function answer(socket, status, { headers = {}, body = null, contentType = null } = {}) {
  if (socket.destroyed) return;
  const lines = [`HTTP/1.1 ${status} ${http.STATUS_CODES[status] ?? 'Unknown'}`];
  for (const [name, value] of Object.entries(headers)) lines.push(`${name}: ${value}`);
  const bytes = body && body.length ? body : null;
  if (bytes && typeof contentType === 'string' && PRINTABLE.test(contentType)) lines.push(`Content-Type: ${contentType}`);
  lines.push(`Content-Length: ${bytes ? bytes.length : 0}`, 'Cache-Control: no-store', 'Connection: close');
  const head = Buffer.from(`${lines.join('\r\n')}\r\n\r\n`, 'latin1');
  const timer = setTimeout(() => socket.destroy(), CLOSE_GRACE_MS);
  timer.unref();
  socket.once('close', () => clearTimeout(timer));
  socket.once('finish', () => socket.destroy());
  try {
    socket.end(bytes ? Buffer.concat([head, bytes]) : head);
  } catch {
    socket.destroy();
  }
}

/** An upstream status worth passing on as the answer to a handshake. */
function relayableStatus(status) {
  return Number.isInteger(status) && status >= 200 && status <= 599 ? status : 502;
}

const INSTALLED = Symbol('techsara.serverWsRelay');

/**
 * Wrap every 'upgrade' listener registered from now on so that an owned path
 * never reaches it. Registration goes through on, addListener or
 * prependListener (once and prependOnceListener call those), so those three
 * are covered. The wrapper carries `.listener`, which is how removeListener
 * finds a once() wrapper too, so removing the original still works.
 */
function guardLaterUpgradeListeners(server) {
  for (const method of ['on', 'addListener', 'prependListener']) {
    const register = server[method];
    server[method] = function registerWithoutOwnedPaths(event, listener) {
      if (event !== 'upgrade' || typeof listener !== 'function') return register.call(this, event, listener);
      function skipOwnedPaths(req, socket, head) {
        if (ownedSessionId(req && req.url) !== null) return undefined;
        return listener.call(this, req, socket, head);
      }
      skipOwnedPaths.listener = listener;
      return register.call(this, event, skipOwnedPaths);
    };
  }
}

/**
 * Install the relay on `server` (once; a second call returns the first
 * handle). `env` is read once, here. The handle is what the preload's SIGTERM
 * drain uses: `beginDrain()` refuses every new relay, `destroyAll()` ends the
 * open ones and says how many there were.
 */
function install(server, env = process.env, options = {}) {
  if (server[INSTALLED]) return server[INSTALLED];
  const emit = typeof options.log === 'function' ? options.log : log;
  const config = settings(env);
  const target = upstreamTarget(config.orchestratorUrl);
  const relays = new Set();
  let draining = false;
  let sequence = 0;

  function refuse(socket, status, reason, extra = {}) {
    const { headers, origin } = extra;
    emit('ws_refused', origin === undefined ? { reason, status } : { reason, status, origin });
    answer(socket, status, { headers });
  }

  function relay(req, socket, head, id) {
    const conn = (sequence += 1);
    const startedAt = Date.now();
    let phase = 'handshake';
    let upstreamReq = null;
    let upstream = null;
    let upstreamStatus = null;
    let bytesUp = 0;
    let bytesDown = 0;
    let firstClosed = null;
    let graceTimer = null;

    const record = { close: (reason) => finish(reason) };
    relays.add(record);

    const deadline = setTimeout(() => {
      // A status that arrived without its whole body is still the answer.
      finish('handshake_timeout', { status: upstreamStatus === null ? 504 : relayableStatus(upstreamStatus) });
    }, config.handshakeMs);
    deadline.unref();

    function finish(reason, reply) {
      if (phase === 'closed') return;
      phase = 'closed';
      clearTimeout(deadline);
      clearTimeout(graceTimer);
      relays.delete(record);
      if (reply) answer(socket, reply.status, reply);
      else socket.destroy();
      if (upstream) upstream.destroy();
      if (upstreamReq) upstreamReq.destroy();
      emit('ws_close', {
        conn,
        reason,
        duration_ms: Date.now() - startedAt,
        bytes_up: bytesUp,
        bytes_down: bytesDown,
        upstream_status: upstreamStatus,
      });
    }

    // A side that closed cleanly has already ended the other through pipe();
    // the other gets a moment to flush its last bytes (the closing frame) and
    // follow. One that closed on an error has already finished the relay.
    // Who closed is who stopped sending first ('end'): the client socket is
    // half-open by design, so its 'close' can come after the upstream's even
    // when the browser hung up.
    function sideClosed(side) {
      if (phase !== 'open') return;
      firstClosed ??= side;
      const reason = firstClosed === 'client' ? 'client_closed' : 'upstream_closed';
      if (socket.destroyed && upstream.destroyed) {
        finish(reason);
        return;
      }
      const other = side === 'client' ? upstream : socket;
      if (!other.destroyed && !other.writableEnded) other.end();
      if (!graceTimer) {
        graceTimer = setTimeout(() => finish(reason), CLOSE_GRACE_MS);
        graceTimer.unref();
      }
    }

    socket.on('error', () => finish('client_error'));
    socket.on('close', () => {
      if (phase === 'handshake') finish('client_gone');
      else sideClosed('client');
    });

    try {
      upstreamReq = http.request({
        hostname: target.hostname,
        port: target.port,
        method: 'GET',
        path: `${target.basePath}/audio/sessions/${id}/live`,
        headers: upstreamHeaders(req.headers, config, target),
        // A socket of its own: an upgraded socket never goes back to a pool.
        agent: false,
      });
    } catch {
      // Only a header node refuses to write can land here.
      finish('bad_header', { status: 400 });
      return;
    }

    upstreamReq.on('error', () => finish('upstream_unreachable', { status: 502 }));

    upstreamReq.on('response', (res) => {
      upstreamStatus = res.statusCode ?? null;
      const chunks = [];
      let size = 0;
      const done = (body) =>
        finish('upstream_refused', {
          status: relayableStatus(res.statusCode),
          body,
          contentType: res.headers['content-type'],
        });
      res.on('data', (chunk) => {
        if (phase !== 'handshake') return;
        size += chunk.length;
        if (size > MAX_REFUSAL_BODY_BYTES) done(null);
        else chunks.push(chunk);
      });
      res.on('end', () => done(Buffer.concat(chunks)));
      res.on('error', () => done(null));
      res.on('close', () => done(null));
    });

    upstreamReq.on('upgrade', (res, upstreamSocket, upstreamHead) => {
      upstream = upstreamSocket;
      upstreamStatus = res.statusCode ?? 101;
      // Node took its own listener off when it handed the socket over.
      upstream.on('error', () => finish('upstream_error'));
      upstream.on('close', () => sideClosed('upstream'));
      if (phase !== 'handshake') {
        upstream.destroy();
        return;
      }
      try {
        const accept = res.headers['sec-websocket-accept'];
        if (typeof accept !== 'string' || !WS_ACCEPT.test(accept)) {
          finish('upstream_protocol', { status: 502 });
          return;
        }
        clearTimeout(deadline);
        const lines = [
          'HTTP/1.1 101 Switching Protocols',
          'Upgrade: websocket',
          'Connection: Upgrade',
          `Sec-WebSocket-Accept: ${accept}`,
        ];
        // The orchestrator's server name and anything else it answers with
        // stay on this side; the subprotocol has to cross, or a browser that
        // offered one fails the connection.
        const protocol = res.headers['sec-websocket-protocol'];
        if (typeof protocol === 'string' && TOKEN.test(protocol)) lines.push(`Sec-WebSocket-Protocol: ${protocol}`);
        phase = 'open';
        // 40 ms PCM frames: Nagle would hold them back for the ACK of the last.
        socket.setNoDelay(true);
        upstream.setNoDelay(true);
        socket.setTimeout(config.idleMs);
        socket.on('timeout', () => finish('idle'));
        socket.write(`${lines.join('\r\n')}\r\n\r\n`);
        if (upstreamHead && upstreamHead.length) {
          bytesDown += upstreamHead.length;
          socket.write(upstreamHead);
        }
        if (head && head.length) {
          bytesUp += head.length;
          upstream.write(head);
        }
        socket.pipe(upstream);
        upstream.pipe(socket);
        socket.on('data', (chunk) => {
          bytesUp += chunk.length;
        });
        upstream.on('data', (chunk) => {
          bytesDown += chunk.length;
        });
        socket.on('end', () => {
          firstClosed ??= 'client';
        });
        upstream.on('end', () => {
          firstClosed ??= 'upstream';
        });
        emit('ws_open', { conn, relays: relays.size, handshake_ms: Date.now() - startedAt });
      } catch {
        finish('internal');
      }
    });

    upstreamReq.end();
  }

  function onUpgrade(req, socket, head) {
    const id = ownedSessionId(req.url);
    if (id === null) return;
    // Node took its own 'error' listener off this socket when it emitted
    // 'upgrade'; one error without a listener would be an uncaught exception
    // in the process that serves everyone. relay() adds its own on top.
    socket.on('error', () => socket.destroy());
    try {
      const refusal = refusalFor(req, config);
      if (refusal) {
        refuse(socket, refusal.status, refusal.reason, refusal);
        return;
      }
      if (config.mockMode) {
        refuse(socket, 404, 'mock_mode');
        return;
      }
      if (!target) {
        refuse(socket, 502, 'upstream_config');
        return;
      }
      if (draining) {
        refuse(socket, 503, 'draining');
        return;
      }
      if (relays.size >= config.maxRelays) {
        refuse(socket, 503, 'capacity');
        return;
      }
      relay(req, socket, head, id);
    } catch (err) {
      emit('ws_error', { error: String((err && (err.code || err.name)) || 'error') });
      socket.destroy();
    }
  }

  server.prependListener('upgrade', onUpgrade);
  guardLaterUpgradeListeners(server);

  const handle = Object.freeze({
    get size() {
      return relays.size;
    },
    beginDrain() {
      draining = true;
    },
    destroyAll(reason = 'drain') {
      const open = [...relays];
      for (const record of open) record.close(reason);
      return open.length;
    },
  });
  server[INSTALLED] = handle;

  emit('ws_relay_ready', {
    upstream: target ? 'ok' : 'invalid',
    max_relays: config.maxRelays,
    allowed_origins: config.allowedOrigins.size,
    allowed_origins_rejected: config.rejectedOrigins,
    idle_s: config.idleMs / 1000,
    handshake_s: config.handshakeMs / 1000,
  });
  return handle;
}

module.exports = {
  OWNED_PATH,
  SESSION_COOKIE,
  MAX_REFUSAL_BODY_BYTES,
  ownedSessionId,
  isIpLiteral,
  trustedClientIp,
  trustedForwardedProto,
  settings,
  upstreamTarget,
  requestOrigin,
  parseHost,
  hostMatchesOrigin,
  refusalFor,
  upstreamHeaders,
  install,
};
