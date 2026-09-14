'use strict';
/**
 * v1-gateway: the /v1 relay that routine deploys never restart
 * (2026-09-13, no-timeout design revision 2, "WHY THE GATEWAY").
 *
 * THE CHAIN: developer → Cloudflare → cloudflared → v1-gateway (this) →
 * orchestrator → engines. cloudflared sends `^/v1(/|$)` here; everything
 * else still goes to the Next frontend, whose /v1 route delegates here when
 * V1_GATEWAY_URL is set.
 *
 * WHY A SEPARATE PROCESS. No server change can save a default-SDK chat
 * stream, or an openai-node sync call, when the process holding the client's
 * socket restarts — and the orchestrator and frontend are both recreated by
 * routine deploys (22 of 22). This process holds that socket instead, and it
 * changes only when gateway/ changes: Node 20, node: built-ins only, no npm
 * dependency to update.
 *
 * WHAT IT IS NOT: an authentication boundary. It adds no credential, strips
 * the cookie, drops every client-sent x-techsara-* header and every
 * client-described forwarding header. The orchestrator resolves Bearer keys
 * and honours X-TechSara-* only from PUBLIC_API_TRUSTED_PROXIES peers.
 */

const http = require('node:http');

const { readSettings } = require('./lib/settings.cjs');
const H = require('./lib/headers.cjs');
const B = require('./lib/bodies.cjs');
const { Relay } = require('./lib/relay.cjs');
const { FdGuard, SpoolBudget, readNofile } = require('./lib/guards.cjs');
const { installDrain } = require('./lib/drain.cjs');

/**
 * route.ts exports GET, POST, OPTIONS; the Files design adds PUT, DELETE.
 * HEAD because Next's app router answers HEAD from a route's GET handler, so
 * a HEAD through Next reaches the orchestrator as HEAD today.
 */
const METHODS = new Set(['GET', 'HEAD', 'POST', 'OPTIONS', 'PUT', 'DELETE']);

function jsonLog(stream = process.stdout) {
  return (event, fields = {}) => {
    stream.write(`${JSON.stringify({ ts: new Date().toISOString(), svc: 'v1-gateway', event, ...fields })}\n`);
  };
}

function createGateway({ env = process.env, log = jsonLog() } = {}) {
  const settings = readSettings(env);
  const removed = B.prepareSpool(settings.spoolDir);
  const gw = {
    settings,
    log,
    registry: new Set(),
    agent: new http.Agent({ keepAlive: true, keepAliveMsecs: 30_000, maxSockets: Infinity, maxFreeSockets: 256 }),
    freshAgent: new http.Agent({ keepAlive: false, maxSockets: Infinity }),
    attachEvidence: false,
    stats: { reattachStarted: 0, reattachSucceeded: 0, reattachFailed: 0 },
    fdGuard: new FdGuard({ ratio: settings.fdPressureRatio }),
    spoolBudget: new SpoolBudget({
      dir: settings.spoolDir,
      maxBytes: settings.spoolMaxBytes,
      minFreeBytes: settings.spoolMinFreeBytes,
    }),
    draining: false,
  };

  const answer = (res, { status, headers, body }, close = false) => {
    const h = { ...headers };
    if (close) h.connection = 'close';
    res.writeHead(status, h);
    res.end(body);
  };

  const handle = (req, res) => {
    if (req.url === '/healthz' && (req.method === 'GET' || req.method === 'HEAD')) {
      const body = JSON.stringify({ status: gw.draining ? 'draining' : 'ok', relays: gw.registry.size });
      res.writeHead(gw.draining ? 503 : 200, { 'content-type': 'application/json', 'cache-control': 'no-store' });
      res.end(req.method === 'HEAD' ? undefined : body);
      return;
    }
    const target = B.resolveTarget(req.url);
    if (!target) {
      res.writeHead(404, { ...H.NEXT_STATIC_HEADERS, 'content-length': '0' });
      res.end();
      return;
    }
    if (!METHODS.has(req.method)) {
      res.writeHead(405, { ...H.NEXT_STATIC_HEADERS, allow: [...METHODS].join(', '), 'content-length': '0' });
      res.end();
      return;
    }
    if (gw.draining) {
      answer(res, H.edgeError('model_unavailable', 'The service is temporarily unavailable. Please retry.', { retryAfter: 2 }), true);
      return;
    }
    if (gw.fdGuard.overPressure()) {
      log('fd_pressure_refusal', { ratio: Number(gw.fdGuard.pressure().toFixed(3)) });
      answer(res, H.edgeError('model_unavailable', 'The service is temporarily unavailable. Please retry.', { retryAfter: 30 }), true);
      return;
    }
    const relay = new Relay(gw, req, res, target);
    relay.run();
  };

  const server = http.createServer(
    {
      requestTimeout: settings.requestTimeoutMs,
      headersTimeout: settings.headersTimeoutMs,
      keepAliveTimeout: settings.keepAliveTimeoutMs,
    },
    handle,
  );
  // Asserted again as properties: these are the values the tests read back.
  server.requestTimeout = settings.requestTimeoutMs;
  server.headersTimeout = settings.headersTimeoutMs;
  server.keepAliveTimeout = settings.keepAliveTimeoutMs;
  server.timeout = 0;

  // `Expect: 100-continue` (curl sends it above 1 MiB): refuse a declared
  // over-cap body BEFORE inviting the client to send it.
  server.on('checkContinue', (req, res) => {
    const target = B.resolveTarget(req.url);
    const declared = Number(req.headers['content-length']);
    if (target && METHODS.has(req.method)) {
      const rule = B.bodyRuleFor(req.method, target.route, settings.env);
      // A GET, HEAD or OPTIONS has a 0 byte cap (see Relay.run).
      if (Number.isFinite(declared) && declared > rule.cap) {
        answer(res, H.edgeError('request_too_large', `The request body is larger than the ${rule.cap} byte limit.`), true);
        return;
      }
    }
    res.writeContinue();
    handle(req, res);
  });

  const nofile = readNofile();
  return {
    gw,
    server,
    settings,
    listen() {
      return new Promise((resolve) => {
        server.listen(settings.port, settings.host, () => {
          const address = server.address();
          log('listening', {
            port: address.port,
            nofile_soft: nofile ? nofile.soft : null,
            nofile_hard: nofile ? nofile.hard : null,
            spool_removed: removed,
            attach: settings.attach,
          });
          resolve(address.port);
        });
      });
    },
    installDrain(opts = {}) {
      return installDrain({
        server,
        registry: gw.registry,
        settings,
        log,
        onStart: () => {
          gw.draining = true;
        },
        ...opts,
      });
    },
  };
}

module.exports = { createGateway, METHODS };

if (require.main === module) {
  const gateway = createGateway();
  gateway.installDrain();
  gateway.listen();
}
