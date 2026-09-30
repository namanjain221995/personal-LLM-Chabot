'use strict';
/**
 * server-preload.cjs — loaded before Next's standalone server
 * (`node --require ./server-preload.cjs server.js`, frontend/Dockerfile).
 *
 * WHY A PRELOAD (2026-09-13, no-timeout design revision 2, timers "Next
 * standalone http.Server" and "Frontend container stop"). Next's standalone
 * server.js builds its own `http.createServer` and exposes none of the three
 * timers below, and its SIGTERM handler (next/dist/server/lib/start-server.js)
 * calls `server.close()` and waits for EVERY in-flight response. Both were
 * measured to hurt:
 *
 *  · Node's defaults are requestTimeout 300 s (a slow upload got 408 at 325 s,
 *    through the edge) and keepAliveTimeout 5 s (below cloudflared's 90 s
 *    idle pool, so a reused tunnel connection can land on a socket this
 *    process is closing).
 *  · One long /v1 relay held the whole site down for the full 5-minute
 *    stop_grace_period of a deploy, because the replacement container cannot
 *    start while this one still runs.
 *
 * A preload is the one place that runs before Next without forking it.
 * Nothing here changes a response, a header or a route.
 *
 * WHAT IT DOES
 *
 * 1. Timers, on every server this process creates (there is one):
 *      requestTimeout   0        — no whole-request ceiling. A body is watched
 *                                  for silence instead (point 2).
 *      headersTimeout   100000   — a client still gets 100 s to send headers,
 *                                  which bounds a slowloris on the header phase.
 *      keepAliveTimeout 95000    — above cloudflared's 90 s, so the tunnel
 *                                  always closes an idle connection first.
 *
 * 2. A body-idle guard, FRONTEND_BODY_IDLE_S (60): a request whose body is
 *    still expected, whose socket has received nothing for that long, while
 *    the handler has consumed everything that did arrive, is destroyed. It
 *    replaces the 300 s requestTimeout for its only legitimate job — freeing a
 *    socket a client stopped feeding — without cutting an upload that is slow
 *    but moving (a byte every few seconds resets it) or a handler that is
 *    merely not reading yet (then bytes wait in the socket's buffer, and
 *    `readableLength` is not zero). Cloudflare's own write timeout is 30 s, so
 *    a real client through the tunnel never gets near 60.
 *
 * 3. A SIGTERM drain policy, run next to Next's own handler (which still does
 *    the `server.close()` and the exit):
 *      · idle keep-alive connections close at once, and every response still
 *        to start carries `Connection: close`;
 *      · /v1 relays are destroyed at +FRONTEND_DRAIN_V1_S (2 s). A caller sees
 *        an incomplete read and resumes; the generation itself belongs to the
 *        orchestrator and keeps running. Normally /v1 does not come through
 *        here at all — the tunnel sends it to the v1-gateway;
 *      · chat streams (/api/chat, /api/chat/attach/…, and any other
 *        text/event-stream answer) are destroyed at +FRONTEND_DRAIN_SSE_S
 *        (15 s): the browser re-attaches to the answer the orchestrator is
 *        still producing;
 *      · everything else — uploads above all — is left to finish, up to
 *        compose's 5-minute stop_grace_period, because an upload cut here is
 *        work the user has to redo;
 *      · each connection is closed as soon as its response ends, so Next's
 *        `server.close()` resolves, and the process exits, the moment the last
 *        upload is done.
 *
 * DECLINED (design, "declined sub-fixes"): `server.maxConnections`. A fixed
 * cap would refuse chat under load; the body-idle guard is what addresses a
 * slow-body attack.
 */

const http = require('node:http');

function envSeconds(name, fallback) {
  const raw = String(process.env[name] ?? '').trim();
  if (raw === '') return fallback;
  const value = Number(raw);
  return Number.isFinite(value) && value >= 0 ? value : fallback;
}

const SERVER_TIMERS = Object.freeze({
  requestTimeout: 0,
  headersTimeout: 100_000,
  keepAliveTimeout: 95_000,
});

function settings() {
  return {
    bodyIdleMs: envSeconds('FRONTEND_BODY_IDLE_S', 60) * 1000,
    v1AbortMs: envSeconds('FRONTEND_DRAIN_V1_S', 2) * 1000,
    sseAbortMs: envSeconds('FRONTEND_DRAIN_SSE_S', 15) * 1000,
  };
}

function log(event, fields = {}) {
  // One JSON line, like the gateway's, so both edges grep the same way.
  process.stdout.write(`${JSON.stringify({ component: 'server-preload', event, ...fields })}\n`);
}

/** `v1`, `sse` or `other`, from the path alone (the response can upgrade it). */
function classify(url) {
  const path = String(url ?? '').split('?')[0];
  if (path === '/v1' || path.startsWith('/v1/')) return 'v1';
  if (path === '/api/chat' || path.startsWith('/api/chat/attach/')) return 'sse';
  return 'other';
}

function isEventStream(value) {
  const text = Array.isArray(value) ? value.join(',') : String(value ?? '');
  return text.toLowerCase().split(';')[0].trim() === 'text/event-stream';
}

const servers = new Set();
let draining = false;

function track(server) {
  if (servers.has(server)) return;
  servers.add(server);
  Object.assign(server, SERVER_TIMERS);
  const { bodyIdleMs } = settings();
  const inflight = new Set();
  server.inflight = inflight;

  // prependListener: tracking must be in place before the application's own
  // listener runs, or a handler that writes its head synchronously would be
  // classified (and drained) as the wrong kind.
  server.prependListener('request', (req, res) => {
    const socket = req.socket;
    const entry = {
      req,
      res,
      socket,
      kind: classify(req.url),
      lastBytes: socket ? socket.bytesRead : 0,
      lastProgressAt: Date.now(),
    };
    inflight.add(entry);
    if (draining && !res.headersSent) res.setHeader('connection', 'close');

    // An SSE answer on any path joins the chat-stream class. writeHead is the
    // one place every response passes through before its head is sent.
    const writeHead = res.writeHead;
    res.writeHead = function patchedWriteHead(status, ...rest) {
      try {
        const headers = rest.find((x) => x && typeof x === 'object');
        const type = (headers && (headers['content-type'] ?? headers['Content-Type'])) ?? res.getHeader('content-type');
        if (entry.kind === 'other' && isEventStream(type)) entry.kind = 'sse';
        if (draining && !res.headersSent) res.setHeader('connection', 'close');
      } catch {
        // Classification is best effort; the response must never fail for it.
      }
      return writeHead.call(this, status, ...rest);
    };

    res.on('close', () => {
      inflight.delete(entry);
      if (draining) {
        // Let the connection go now that its answer is done, so server.close()
        // (Next's) can resolve as soon as the last response ends.
        setImmediate(() => server.closeIdleConnections());
      }
    });
  });

  if (bodyIdleMs > 0) {
    const every = Math.max(50, Math.min(5_000, Math.floor(bodyIdleMs / 4)));
    const timer = setInterval(() => {
      const now = Date.now();
      for (const entry of inflight) {
        const { req, socket } = entry;
        if (!socket || socket.destroyed || req.complete) continue;
        if (socket.bytesRead !== entry.lastBytes) {
          entry.lastBytes = socket.bytesRead;
          entry.lastProgressAt = now;
          continue;
        }
        // Bytes that arrived and have not been read yet mean the handler is
        // the slow side, not the client.
        if (req.readableLength > 0) {
          entry.lastProgressAt = now;
          continue;
        }
        if (now - entry.lastProgressAt >= bodyIdleMs) {
          log('body_idle', { path: String(req.url ?? '').split('?')[0], idle_ms: now - entry.lastProgressAt });
          socket.destroy();
        }
      }
    }, every);
    timer.unref();
    server.on('close', () => clearInterval(timer));
  }
}

const originalCreateServer = http.createServer;
http.createServer = function createServerWithPreload(...args) {
  const server = originalCreateServer.apply(this, args);
  track(server);
  return server;
};

function destroyKind(kind) {
  let count = 0;
  for (const server of servers) {
    for (const entry of server.inflight ?? []) {
      if (entry.kind !== kind || !entry.socket || entry.socket.destroyed) continue;
      entry.socket.destroy();
      count += 1;
    }
  }
  if (count) log('drain_abort', { kind, count });
}

function onTerminate(signal) {
  if (draining) return;
  draining = true;
  const { v1AbortMs, sseAbortMs } = settings();
  let inflight = 0;
  // Idle keep-alive connections are closed by Next's own server.close(), which
  // on Node 20 calls closeIdleConnections() (measured: tests/server-preload.test.ts).
  for (const server of servers) {
    for (const entry of server.inflight ?? []) {
      inflight += 1;
      if (!entry.res.headersSent) {
        try {
          entry.res.setHeader('connection', 'close');
        } catch {
          /* head already on its way */
        }
      }
    }
  }
  log('drain_start', { signal, inflight, v1_abort_ms: v1AbortMs, sse_abort_ms: sseAbortMs });
  setTimeout(() => destroyKind('v1'), v1AbortMs).unref();
  setTimeout(() => destroyKind('sse'), sseAbortMs).unref();
}

// Registered before Next's own handler (a preload runs first), and it never
// exits: Next's handler closes the server and exits once the drain lets it.
// SIGTERM only — docker stop's signal. Adding a SIGINT listener would take
// away Node's default Ctrl-C exit anywhere this file is preloaded without
// Next's handler.
process.on('SIGTERM', () => onTerminate('SIGTERM'));

module.exports = { SERVER_TIMERS, classify, settings };
