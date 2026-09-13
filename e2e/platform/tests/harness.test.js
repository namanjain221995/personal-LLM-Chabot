'use strict';
// Self-tests for the pure parts of the suite: run with `npm test`.
const test = require('node:test');
const assert = require('node:assert/strict');

const { isLoopbackUrl, baseRefusal, parseArgs, resolveConfig } = require('../lib/config');
const { Registry, runAll, renderMarkdown, describeError, escapeCell } = require('../lib/harness');
const { parseSetCookies } = require('../lib/http');
const { sseFrame, chatStubStream, playgroundStubStream } = require('../lib/stubs');
const { slugsFromIndex, isLoginRedirect } = require('../suites/gating');
const os = require('os');
const fs = require('fs');
const path = require('path');

test('only real loopback addresses count as loopback, not DNS names that start with 127', () => {
  assert.equal(isLoopbackUrl('http://127.0.0.1:3001'), true);
  assert.equal(isLoopbackUrl('http://127.1.2.3:3001'), true);
  assert.equal(isLoopbackUrl('http://localhost:3901/'), true);
  assert.equal(isLoopbackUrl('http://[::1]:3901'), true);
  assert.equal(isLoopbackUrl('http://[::ffff:127.0.0.1]:3901'), true);
  assert.equal(isLoopbackUrl('http://127.attacker.example:3001'), false);
  assert.equal(isLoopbackUrl('http://127.0.0.1.nip.io:3001'), false);
  assert.equal(isLoopbackUrl('http://0.0.0.0:3001'), false);
  assert.equal(isLoopbackUrl('https://example.com'), false);
  assert.equal(isLoopbackUrl('http://203.0.113.5:3001'), false);
  assert.equal(isLoopbackUrl('file:///etc/passwd'), false);
  assert.equal(isLoopbackUrl('not a url'), false);
});

test('the production ports are refused on every host, with or without E2E_ALLOW_REMOTE', () => {
  for (const base of ['http://127.0.0.1:3000', 'http://localhost:3000', 'http://[::1]:8080', 'http://127.0.0.1:8080']) {
    assert.match(baseRefusal(base) || '', /production port/, base);
    assert.match(baseRefusal(base, { allowRemote: true }) || '', /production port/, base);
  }
  assert.match(baseRefusal('http://203.0.113.5:3000', { allowRemote: true }) || '', /production port/);
});

test('a loopback base must use a test-stack port, and a remote one needs E2E_ALLOW_REMOTE', () => {
  assert.equal(baseRefusal('http://127.0.0.1:3001'), null);
  assert.equal(baseRefusal('http://127.0.0.1:3002'), null);
  assert.equal(baseRefusal('http://127.0.0.1:3901'), null);
  assert.match(baseRefusal('http://127.0.0.1') || '', /not a known test-stack port/);
  assert.match(baseRefusal('http://127.0.0.1:5173') || '', /not a known test-stack port/);
  assert.equal(baseRefusal('http://127.0.0.1:5173', { allowedPorts: '5173' }), null);
  assert.match(baseRefusal('http://127.attacker.example:3001') || '', /not a loopback address/);
  assert.match(baseRefusal('https://example.com') || '', /E2E_ALLOW_REMOTE/);
  assert.equal(baseRefusal('https://example.com', { allowRemote: true }), null);
  assert.throws(() => baseRefusal('http://127.0.0.1:3001', { allowedPorts: 'abc' }), /not a port or a range/);
});

test('flags parse in both --name value and --name=value spellings', () => {
  assert.deepEqual(parseArgs(['--base', 'http://x', '--only=chat,gating', '--list']), { _: [], base: 'http://x', only: 'chat,gating', list: true });
});

test('config reads passwords from env or a file and never has a default', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'e2e-cfg-'));
  const file = path.join(dir, 'pw');
  fs.writeFileSync(file, 'from-file\n');
  const cfg = resolveConfig([], { E2E_MEMBER_PASSWORD_FILE: file });
  assert.equal(cfg.member.password(), 'from-file');
  assert.equal(cfg.admin.password(), null);
  assert.deepEqual(cfg.widths, [360, 768, 1440]);
  assert.equal(cfg.chatMode, 'stub');
  assert.throws(() => resolveConfig(['--chat-mode', 'fast'], {}), /stub or live/);
});

test('set-cookie headers yield name=value pairs without attributes', () => {
  const headers = { getSetCookie: () => ['ts_session=abc.def; Path=/; HttpOnly; Secure; SameSite=Lax', 'other=1; Max-Age=0'] };
  assert.deepEqual(parseSetCookies(headers), { ts_session: 'abc.def', other: '1' });
});

test('the chat stub stream is valid SSE that ends with done and carries the whole answer', () => {
  const body = chatStubStream('Hello stub world', { pieces: 3 });
  const tokens = [...body.matchAll(/event: token\ndata: (.*)\n\n/g)].map((m) => JSON.parse(m[1]).text).join('');
  assert.equal(tokens, 'Hello stub world');
  assert.match(body, /event: done\ndata: \{\}\n\n$/);
  assert.equal(sseFrame('x', 'a\nb'), 'event: x\ndata: a\ndata: b\n\n');
});

test('the chat stub opens with the generation handshake before any token and repeats the ids on the final meta', () => {
  const body = chatStubStream('Hello', { generationId: '0f1e2d3c-aaaa-bbbb-cccc-000000000001', intentId: 'intent-1', sessionId: 's-1' });
  const events = [...body.matchAll(/event: (\w+)\ndata: (.*)\n\n/g)].map((m) => ({ event: m[1], data: JSON.parse(m[2]) }));
  assert.equal(events[0].event, 'meta');
  assert.deepEqual(
    { generation_id: events[0].data.generation_id, intent_id: events[0].data.intent_id, attempt: events[0].data.attempt },
    { generation_id: '0f1e2d3c-aaaa-bbbb-cccc-000000000001', intent_id: 'intent-1', attempt: 1 },
  );
  assert.ok(events.findIndex((e) => e.event === 'token') > 0);
  const last = events.filter((e) => e.event === 'meta').pop();
  assert.equal(last.data.generation_id, '0f1e2d3c-aaaa-bbbb-cccc-000000000001');
  assert.equal(last.data.engine, 'e2e-stub');
  assert.deepEqual(events[events.length - 1], { event: 'done', data: { session_id: 's-1' } });
  // No intent sent → no leading meta, as the orchestrator does.
  const bare = chatStubStream('Hello', { generationId: 'g' });
  assert.match(bare, /^: stub stream from e2e\/platform\n\nevent: status/);
});

test('the playground stub numbers its events from 1 with exactly one terminal event', () => {
  const body = playgroundStubStream('abcdefghij');
  const seqs = [...body.matchAll(/"sequence_number":(\d+)/g)].map((m) => Number(m[1]));
  assert.deepEqual(seqs, seqs.map((_, i) => i + 1));
  assert.equal((body.match(/event: response\.completed/g) || []).length, 1);
});

test('doc slugs are discovered from index links, once each', () => {
  const html = '<a href="/docs/quickstart">Q</a><a href="/docs/errors#codes">E</a><a href="/docs/quickstart">again</a><a href="/docsx">no</a>';
  assert.deepEqual(slugsFromIndex(html), ['quickstart', 'errors']);
});

test('only a 3xx whose Location is /login counts as a login redirect', () => {
  assert.equal(isLoginRedirect({ status: 307, location: '/login' }), true);
  assert.equal(isLoginRedirect({ status: 307, location: 'http://127.0.0.1:3001/login?next=/api' }), true);
  assert.equal(isLoginRedirect({ status: 308, location: '/docs' }), false);
  assert.equal(isLoginRedirect({ status: 200, location: null }), false);
});

test('a failing check is reported verbatim, a dependent check is skipped, and the table escapes pipes', async () => {
  const reg = new Registry();
  reg.add('s', 's.one', 'fails', async () => {
    throw new Error('boom | with a pipe\nsecond line');
  });
  reg.add('s', 's.two', 'needs one', async () => {}, { needs: ['s.one'] });
  reg.add('s', 's.three', 'passes', async (ctx) => ctx.note('fine'));
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'e2e-run-'));
  const makeContext = (t, result) => ({
    note: (m) => result.notes.push(m),
    _screenshotOpenPages: async () => {},
    _dispose: async () => {},
  });
  const results = await runAll(reg.select([], []), makeContext, { outDir: dir, log: () => {} });
  assert.deepEqual(results.map((r) => r.status), ['fail', 'skip', 'pass']);
  assert.match(results[0].error, /^boom \| with a pipe\nsecond line/);
  const md = renderMarkdown(results, {
    base: 'http://127.0.0.1:1',
    chatMode: 'stub',
    widths: [360],
    startedAt: 'a',
    finishedAt: 'b',
    target: { frontendImage: 'sha256:abc', orchestratorImage: '', git: 'deadbee dirty' },
    cleanups: [
      { label: 'revoke API key "k"', from: 'console.key-create', status: 'failed', error: 'POST key revoke → 500' },
      { label: 'delete conversation c', from: 'chat.send-stream', status: 'done', error: null },
    ],
  });
  assert.match(md, /1 passed, 1 failed, 1 skipped/);
  assert.match(md, /Target: frontend image `sha256:abc`; orchestrator image `not stated`; git `deadbee dirty`/);
  assert.match(md, /## Cleanup: 1 done, 1 failed/);
  assert.match(md, /\*\*FAILED\*\*: revoke API key "k" \(registered by `console.key-create`\) — POST key revoke → 500/);
  // The table carries the first line; the Failures section carries all of it.
  assert.match(md, /\| FAIL \| 0\.0 s \| boom \\\| with a pipe \|/);
  assert.match(md, /```text\nboom \| with a pipe\nsecond line/);
});

test('a message line that starts with "at" is not mistaken for a stack frame', () => {
  const err = new Error('/docs:\n  at 360px: ended at /login');
  const text = describeError(err);
  assert.equal(text.split('at 360px').length - 1, 1);
  assert.equal(escapeCell('a|b\nc'), 'a\\|b c');
});

test('a timed-out check may not open sessions or send requests once the runner has moved on', async () => {
  const { makeSessions } = require('../lib/browser');
  const cfg = { base: 'http://127.0.0.1:9', member: { email: 'm@test.local', password: () => 'x' } };
  const result = { consoleErrors: [], screenshots: [], notes: [] };
  const sessions = makeSessions(async () => {
    throw new Error('no browser in this test');
  }, cfg, { id: 's.timeout' }, result);
  const anon = await sessions.http(null);
  await sessions.dispose();
  await assert.rejects(() => sessions.http(null), /s\.timeout is over/);
  await assert.rejects(() => sessions.page(), /s\.timeout is over/);
  await assert.rejects(() => anon.get('/api/auth/me'), /s\.timeout is over/);
});

test('selection matches suite names and id prefixes, and --skip wins', () => {
  const reg = new Registry();
  for (const id of ['chat.send', 'chat.rename', 'console.tabs', 'v1.models']) reg.add(id.split('.')[0], id, id, async () => {});
  assert.deepEqual(reg.select(['chat'], []).map((t) => t.id), ['chat.send', 'chat.rename']);
  assert.deepEqual(reg.select(['chat', 'v1'], ['chat.rename']).map((t) => t.id), ['chat.send', 'v1.models']);
});
