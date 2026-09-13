'use strict';
/**
 * Upload helpers: deterministic fixture bytes, hashing, and a PUT whose
 * connection is deliberately cut halfway through the body.
 */

const crypto = require('crypto');
const fs = require('fs');
const http = require('http');
const https = require('https');

const sha256 = (buf) => crypto.createHash('sha256').update(buf).digest('hex');

/** Printable, line-structured bytes: a real text document, not noise. */
function textBytes(size, seed = 'e2e') {
  const line = Buffer.from(`${seed} regression fixture line — the quick brown fox jumps over the lazy dog 0123456789\n`);
  const out = Buffer.allocUnsafe(size);
  for (let off = 0; off < size; off += line.length) line.copy(out, off, 0, Math.min(line.length, size - off));
  return out;
}

/** Write a large text fixture in 8 MiB slices so memory stays flat. */
function writeTextFile(file, size, seed) {
  const fd = fs.openSync(file, 'w');
  try {
    const slice = 8 * 1024 * 1024;
    const block = textBytes(slice, seed);
    for (let written = 0; written < size; written += slice) {
      fs.writeSync(fd, block, 0, Math.min(slice, size - written));
    }
  } finally {
    fs.closeSync(fd);
  }
}

/**
 * Send `headers` declaring the whole `body`, write only `sendBytes` of it,
 * then destroy the socket — what a laptop lid closing mid-part looks like to
 * the server. Resolves with whatever happened on the client side; the
 * interesting verdict is what the server recorded, which the caller asks for.
 */
function putWithDroppedConnection(url, headers, body, sendBytes, { holdMs = 400 } = {}) {
  return new Promise((resolve) => {
    const u = new URL(url);
    const lib = u.protocol === 'https:' ? https : http;
    const req = lib.request(
      {
        method: 'PUT',
        hostname: u.hostname,
        port: u.port,
        path: `${u.pathname}${u.search}`,
        headers: { ...headers, 'content-length': String(body.length) },
      },
      (res) => {
        res.resume();
        resolve({ outcome: 'response', status: res.statusCode });
      },
    );
    req.on('error', (err) => resolve({ outcome: 'client-error', error: err.code || err.message }));
    req.write(body.subarray(0, sendBytes), () => {
      setTimeout(() => {
        req.destroy(new Error('e2e: connection dropped on purpose'));
      }, holdMs);
    });
  });
}

module.exports = { sha256, textBytes, writeTextFile, putWithDroppedConnection };
