'use strict';
/**
 * A stand-in for Cloudflare's proxy read timeout, for tests only
 * (2026-09-13). The design assumes the stricter documented 100 s: no byte
 * from the origin for 100 s before the response starts → 524; no byte for
 * 100 s after it started → the connection is cut. It is not Cloudflare; it
 * exists so a "silent for 400 s" test fails without the gateway's byte
 * invariant (the control case proves that) instead of passing by accident
 * because a local SDK's own read timeout is 600 s.
 */

const http = require('node:http');

const HOP = new Set(['connection', 'keep-alive', 'transfer-encoding', 'host', 'upgrade', 'proxy-connection']);

function startEdge({ targetPort, readTimeoutMs = 100_000 }) {
  const server = http.createServer({ requestTimeout: 0 }, (req, res) => {
    const headers = {};
    for (const [k, v] of Object.entries(req.headers)) if (!HOP.has(k)) headers[k] = v;
    const up = http.request({ host: '127.0.0.1', port: targetPort, method: req.method, path: req.url, headers, agent: false });
    let timer = setTimeout(() => {
      up.destroy();
      if (!res.headersSent) {
        res.writeHead(524, { 'content-type': 'text/plain' });
        res.end('error code: 524');
      } else res.destroy();
    }, readTimeoutMs);
    up.on('response', (ur) => {
      const out = {};
      for (const [k, v] of Object.entries(ur.headers)) if (!HOP.has(k)) out[k] = v;
      res.writeHead(ur.statusCode, out);
      const arm = () => {
        clearTimeout(timer);
        timer = setTimeout(() => {
          up.destroy();
          res.destroy();
        }, readTimeoutMs);
      };
      arm();
      ur.on('data', (c) => {
        arm();
        res.write(c);
      });
      ur.on('end', () => {
        clearTimeout(timer);
        res.end();
      });
      ur.on('close', () => {
        if (!ur.complete) {
          clearTimeout(timer);
          res.destroy();
        }
      });
    });
    up.on('error', () => {
      clearTimeout(timer);
      if (!res.headersSent) {
        res.writeHead(502, { 'content-type': 'text/plain' });
        res.end('error code: 502');
      } else res.destroy();
    });
    res.on('close', () => {
      if (!res.writableFinished) up.destroy();
    });
    req.pipe(up);
  });
  return new Promise((resolve) => {
    server.listen(0, '127.0.0.1', () => resolve({ port: server.address().port, server, close: () => { server.closeAllConnections(); server.close(); } }));
  });
}

module.exports = { startEdge };
