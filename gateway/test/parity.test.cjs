'use strict';
/**
 * The gateway and the Next /v1 route must be the same edge (2026-09-13).
 * This file READS frontend/app/v1/[[...path]]/route.ts and
 * frontend/next.config.mjs on every run, so a change to either that is not
 * made here too fails the build instead of changing what callers see when
 * the cloudflared path rule moves /v1 to the gateway.
 */

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const H = require('../lib/headers.cjs');
const B = require('../lib/bodies.cjs');
const { METHODS } = require('../server.cjs');
const h = require('../testkit/harness.cjs');

const REPO = path.resolve(__dirname, '..', '..');
const ROUTE_TS = path.join(REPO, 'frontend', 'app', 'v1', '[[...path]]', 'route.ts');
const NEXT_CONFIG = path.join(REPO, 'frontend', 'next.config.mjs');
const haveFrontend = fs.existsSync(ROUTE_TS) && fs.existsSync(NEXT_CONFIG);
const skip = haveFrontend ? false : 'frontend/ is not beside gateway/ (image build context)';

function source(file) {
  // Line comments removed so an apostrophe or a quoted name in prose is never
  // mistaken for a list member.
  return fs
    .readFileSync(file, 'utf8')
    .split('\n')
    .map((line) => line.replace(/^\s*\/\/.*$/, '').replace(/\s\/\/\s.*$/, ''))
    .join('\n');
}

function stringArray(src, name) {
  const m = new RegExp(`const ${name}\\s*=\\s*\\[([\\s\\S]*?)\\]\\s*as const`).exec(src);
  assert.ok(m, `${name} not found in route.ts`);
  return [...m[1].matchAll(/'([^']*)'/g)].map((x) => x[1]);
}

function numberSet(src, name) {
  const m = new RegExp(`const ${name}\\s*=\\s*new Set\\(\\[([^\\]]*)\\]\\)`).exec(src);
  assert.ok(m, `${name} not found`);
  return m[1].split(',').map((x) => Number(x.trim()));
}

function product(expr) {
  assert.match(expr, /^[\d\s*_]+$/, `not a constant product: ${expr}`);
  return expr.split('*').reduce((acc, x) => acc * Number(x.trim().replace(/_/g, '')), 1);
}

test.describe('the lists match route.ts', { skip }, () => {
  const src = haveFrontend ? source(ROUTE_TS) : '';

  test('the request allowlist is route.ts plus exactly the pending names', () => {
    const route = stringArray(src, 'REQUEST_HEADER_ALLOWLIST');
    const forwarded = new Set([...H.REQUEST_HEADER_ALLOWLIST, ...H.PENDING_REQUEST_HEADERS]);
    assert.deepEqual([...forwarded].sort(), [...new Set([...route, ...H.PENDING_REQUEST_HEADERS])].sort());
  });

  test('the response allowlist is route.ts plus exactly the pending names', () => {
    const route = stringArray(src, 'RESPONSE_HEADER_ALLOWLIST');
    const relayed = new Set([...H.RESPONSE_HEADER_ALLOWLIST, ...H.PENDING_RESPONSE_HEADERS]);
    assert.deepEqual([...relayed].sort(), [...new Set([...route, ...H.PENDING_RESPONSE_HEADERS])].sort());
    assert.deepEqual(stringArray(src, 'RESPONSE_HEADER_PREFIXES'), [...H.RESPONSE_HEADER_PREFIXES]);
  });

  test('pending names route.ts has since absorbed are reported for removal from the pending lists', (t) => {
    const reqAbsorbed = H.PENDING_REQUEST_HEADERS.filter((n) => stringArray(src, 'REQUEST_HEADER_ALLOWLIST').includes(n));
    const resAbsorbed = H.PENDING_RESPONSE_HEADERS.filter((n) => stringArray(src, 'RESPONSE_HEADER_ALLOWLIST').includes(n));
    if (reqAbsorbed.length || resAbsorbed.length) {
      t.diagnostic(`route.ts now carries ${[...reqAbsorbed, ...resAbsorbed].join(', ')}: move them from PENDING_* into the base lists`);
    }
    // No pending name may be something route.ts forbids by name.
    for (const forbidden of ['cookie', 'set-cookie', 'access-control-allow-credentials', 'location', 'content-encoding']) {
      assert.ok(!H.PENDING_REQUEST_HEADERS.includes(forbidden) && !H.PENDING_RESPONSE_HEADERS.includes(forbidden), forbidden);
    }
  });

  test('no allowlisted name is in the internal x-techsara- namespace', () => {
    for (const n of [...H.REQUEST_HEADER_ALLOWLIST, ...H.PENDING_REQUEST_HEADERS, ...H.RESPONSE_HEADER_ALLOWLIST, ...H.PENDING_RESPONSE_HEADERS]) {
      assert.ok(!n.startsWith(H.INTERNAL_HEADER_PREFIX), n);
    }
  });

  test('the streaming headers, null-body and redirect statuses are route.ts’s', () => {
    const m = /const SSE_HEADERS[^=]*=\s*\{([\s\S]*?)\};/.exec(src);
    const sse = Object.fromEntries([...m[1].matchAll(/'?([a-z-]+)'?:\s*'([^']*)'/g)].map((x) => [x[1], x[2]]));
    assert.deepEqual(sse, { ...H.SSE_HEADERS });
    assert.deepEqual(numberSet(src, 'NULL_BODY_STATUSES'), [...H.NULL_BODY_STATUSES]);
    assert.deepEqual(numberSet(src, 'REDIRECT_STATUSES'), [...H.REDIRECT_STATUSES]);
  });

  test('the edge errors have route.ts’s codes, statuses, types and sentences', () => {
    const m = /const EDGE_ERRORS\s*=\s*\{([\s\S]*?)\}\s*as const/.exec(src);
    const table = Object.fromEntries(
      [...m[1].matchAll(/([a-z_]+):\s*\{\s*status:\s*(\d+),\s*type:\s*'([^']+)'\s*\}/g)].map((x) => [x[1], { status: Number(x[2]), type: x[3] }]),
    );
    for (const [code, spec] of Object.entries(table)) assert.deepEqual(H.EDGE_ERRORS[code], spec, code);
    // The one addition is the Files design's 411 (§2.17).
    assert.deepEqual(Object.keys(H.EDGE_ERRORS).filter((c) => !(c in table)), ['invalid_request_error']);
    for (const sentence of ['The service is temporarily unavailable. Please retry.', 'Something went wrong on our side.', 'byte limit.']) {
      assert.ok(src.includes(sentence), sentence);
    }
    const e = H.edgeError('model_unavailable', 'x', { retryAfter: 0.2 });
    assert.equal(e.headers['retry-after'], '1');
    assert.deepEqual(Object.keys(JSON.parse(e.body).error), ['message', 'type', 'code', 'param', 'request_id']);
  });

  test('the body caps read the same variables with the same defaults', () => {
    const defaults = Object.fromEntries(
      [...src.matchAll(/export const (DEFAULT_PUBLIC_API_[A-Z_]+)\s*=\s*([\d\s*_]+);/g)].map((x) => [x[1], product(x[2])]),
    );
    for (const [name, value] of Object.entries(defaults)) assert.equal(B[name], value, name);
    const rules = [...src.matchAll(/if \(path === '([^']+)'(?: \|\| path === '([^']+)')?\) \{\s*return envBytes\(\s*'([A-Z_]+)',\s*(DEFAULT_[A-Z_]+)\s*\)/g)];
    assert.ok(rules.length >= 2, 'per-path rules parsed');
    for (const [, p1, p2, envName, defName] of rules) {
      for (const route of [p1, p2].filter(Boolean)) {
        assert.equal(B.bodyRuleFor('POST', route, {}).cap, defaults[defName], route);
        assert.equal(B.bodyRuleFor('POST', route, { [envName]: '12345' }).cap, 12345, `${route} reads ${envName}`);
        assert.equal(B.bodyRuleFor('POST', route, { [envName]: 'nonsense' }).cap, defaults[defName]);
      }
    }
    const fallback = /return envBytes\('([A-Z_]+)', (DEFAULT_[A-Z_]+)\);\s*\}\s*\n\s*\/\*\*/.exec(src) || /function publicApiBodyBytes\(\)[^{]*\{\s*return envBytes\('([A-Z_]+)', (DEFAULT_[A-Z_]+)\)/.exec(src);
    assert.ok(fallback, 'default cap parsed');
    assert.equal(B.bodyRuleFor('POST', 'embeddings', {}).cap, defaults[fallback[2]]);
    assert.equal(B.bodyRuleFor('POST', 'embeddings', { [fallback[1]]: '777' }).cap, 777);
  });

  test('the gateway answers every method route.ts exports, plus HEAD (Next derives it from GET) and the Files design’s PUT and DELETE', () => {
    const exported = [...src.matchAll(/export async function ([A-Z]+)\(/g)].map((x) => x[1]);
    for (const m of exported) assert.ok(METHODS.has(m), m);
    assert.deepEqual([...METHODS].filter((m) => !exported.includes(m)).sort(), ['DELETE', 'HEAD', 'PUT']);
  });

  test('route.ts still re-encodes each segment, as bodies.cjs does', () => {
    assert.ok(src.includes("parts.map(encodeURIComponent).join('/')"));
    assert.equal(B.resolveTarget('/v1/responses/resp_1/cancel?x=1').upstreamPath, '/v1/responses/resp_1/cancel?x=1');
    assert.equal(B.resolveTarget('/v1').upstreamPath, '/v1');
    assert.equal(B.resolveTarget('/v1/a%2Fb').upstreamPath, '/v1/a%2Fb');
  });
});

test.describe('the static headers match next.config.mjs', { skip }, () => {
  test('every /v1 answer carries the four headers Next adds to every path', () => {
    const src = source(NEXT_CONFIG);
    const block = /source:\s*'\/:path\*',\s*headers:\s*\[([\s\S]*?)\],\s*\}/.exec(src);
    assert.ok(block, "the '/:path*' block");
    const pairs = [...block[1].matchAll(/key:\s*'([^']+)',\s*value:\s*'([^']*)'/g)].map((x) => [x[1].toLowerCase(), x[2]]);
    assert.deepEqual(Object.fromEntries(pairs), { ...H.NEXT_STATIC_HEADERS });
  });
});

test.describe('on the wire', { concurrency: true }, () => {
  let stub;
  let gw;
  let trusting;
  test.before(async () => {
    stub = await h.startStub();
    gw = await h.startGateway({ orchestratorPort: stub.port });
    trusting = await h.startGateway({
      orchestratorPort: stub.port,
      env: { TRUSTED_CLIENT_IP_HEADER: 'CF-Connecting-IP', TRUSTED_FORWARDED_PROTO: 'https' },
    });
  });
  test.after(async () => {
    await gw.close();
    await trusting.close();
    await stub.kill();
  });

  const clientHeaders = {
    authorization: 'Bearer sk-live-abc',
    'idempotency-key': 'idem-1',
    'content-type': 'application/json',
    accept: 'application/json',
    'accept-language': 'gu',
    'user-agent': 'OpenAI/Python 3.13.0',
    origin: 'https://example.com',
    'access-control-request-method': 'POST',
    'access-control-request-headers': 'authorization',
    'x-stainless-retry-count': '1',
    cookie: 'ts_session=secret',
    'x-forwarded-for': '6.6.6.6',
    'x-forwarded-proto': 'http',
    'x-forwarded-host': 'evil',
    forwarded: 'for=6.6.6.6',
    'x-real-ip': '6.6.6.6',
    'true-client-ip': '6.6.6.6',
    'x-client-ip': '6.6.6.6',
    'cf-connecting-ip': '203.0.113.9',
    'x-techsara-attempt': 'client-attempt',
    'x-techsara-resume-after': '99',
    'x-techsara-attach-job': 'someone-elses-job',
    'x-techsara-run': 'resp_x',
    'proxy-authorization': 'Basic x',
    'x-api-key': 'k',
  };

  test('the credential and the named headers cross; the cookie, forwarding headers and x-techsara-* do not', async () => {
    const r = await h.request(gw.port, { method: 'POST', path: '/v1/echo', headers: clientHeaders, body: '{}' });
    const seen = JSON.parse(r.text).headers;
    for (const name of [...H.REQUEST_HEADER_ALLOWLIST, 'x-stainless-retry-count']) assert.equal(seen[name], clientHeaders[name], name);
    for (const name of ['cookie', 'x-forwarded-for', 'x-forwarded-proto', 'x-forwarded-host', 'forwarded', 'x-real-ip', 'true-client-ip', 'x-client-ip', 'cf-connecting-ip', 'proxy-authorization', 'x-api-key', 'x-techsara-resume-after', 'x-techsara-attach-job', 'x-techsara-run']) {
      assert.equal(seen[name], undefined, `${name} must not cross`);
    }
    assert.notEqual(seen['x-techsara-attempt'], 'client-attempt');
    assert.match(seen['x-techsara-attempt'], /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
  });

  test('the caller address comes only from the configured header, and only as one IP literal', async () => {
    const cases = [
      ['203.0.113.9', '203.0.113.9'],
      ['203.0.113.9, 10.0.0.1', '203.0.113.9'],
      ['2001:db8::1', '2001:db8::1'],
      ['::ffff:203.0.113.9', '::ffff:203.0.113.9'],
      ['203.0.113.9:8080', undefined],
      ['evil.example', undefined],
      ['1.2.3.4 5.6.7.8', undefined],
    ];
    for (const [value, expected] of cases) {
      const r = await h.request(trusting.port, { path: '/v1/echo', headers: { 'cf-connecting-ip': value, 'x-forwarded-for': '6.6.6.6' } });
      const seen = JSON.parse(r.text).headers;
      assert.equal(seen['x-forwarded-for'], expected, value);
      assert.equal(seen['x-forwarded-proto'], 'https');
    }
    const plain = await h.request(gw.port, { path: '/v1/echo', headers: { 'cf-connecting-ip': '203.0.113.9' } });
    assert.equal(JSON.parse(plain.text).headers['x-forwarded-for'], undefined);
  });

  test('a response header value node would refuse to write is dropped, not thrown', () => {
    const out = H.clientHeaders({ 'x-request-id': 'ok', 'retry-after': `7${String.fromCharCode(10)}x-evil: 1` });
    assert.equal(out['x-request-id'], 'ok');
    assert.equal(out['retry-after'], undefined);
  });

  test('isIpLiteral agrees with lib/proxy.ts on the shapes it documents', () => {
    for (const ok of ['1.2.3.4', '255.255.255.255', '::1', '2001:db8:0:0:0:0:2:1', 'fe80::1', '::ffff:1.2.3.4']) assert.ok(H.isIpLiteral(ok), ok);
    for (const bad of ['', '256.1.1.1', '1.2.3', '203.0.113.9:8080', 'a.b.c.d', '1:2:3:4:5:6:7:8:9', ':::1', 'x'.repeat(46)]) assert.ok(!H.isIpLiteral(bad), bad);
  });

  test('the answer never carries x-techsara-*, a cookie, or credentials permission, and keeps the rate-limit family', async () => {
    const r = await h.request(gw.port, {
      method: 'POST',
      path: '/v1/chat/completions',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ model: 'stub/tokens=2/interval=5/hdrs', stream: true }),
    });
    assert.equal(r.status, 200);
    for (const name of Object.keys(r.headers)) {
      assert.ok(!name.startsWith('x-techsara-'), name);
      assert.ok(!['set-cookie', 'access-control-allow-credentials', 'x-powered-by', 'server-timing'].includes(name), name);
    }
    assert.equal(r.headers['x-ratelimit-remaining-requests'], '99');
    assert.ok(!r.text.includes('ts-seq'));
    for (const [k, v] of Object.entries(H.NEXT_STATIC_HEADERS)) assert.equal(r.headers[k], v);
  });
});
