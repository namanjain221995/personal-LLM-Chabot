'use strict';
/**
 * The redactor's proof. Every case here is a string shape that the e2e suite
 * really does write into results.md or results.json on a failure.
 *
 * The LITERAL-SECRET cases are the fix this file exists for: `::add-mask::`
 * hides a value in the run LOG, and does nothing at all to the bytes
 * actions/upload-artifact uploads. Without them a failing `auth.login` check
 * uploads the run's plaintext password to a public repository's artifact.
 */

const assert = require('node:assert');
const { test } = require('node:test');

const { redact, redactWithCounts, MIN_SECRET_LENGTH } = require('../redact');

/** Two values of exactly the shape the job generates: `openssl rand -hex 24`. */
const ADMIN_PASSWORD = '6f1c0a9d4b2e8f37c5a10d9e4b7f2c83a6d1e0b9c4f37a52';
const MEMBER_PASSWORD = 'b83d2f61a0c94e7d5b1f83a06d2e9c47f105b6a3d8e2c910';
const SECRETS = [ADMIN_PASSWORD, MEMBER_PASSWORD];

test('a created API key secret never survives, test or live', () => {
  const report = [
    '### `console.key-create`',
    'the shown secret does not look like a key: tsk_test_01JABCDEF_9f8e7d6c5b4a32109876543210abcdef',
    'and the live one: tsk_live_01JABCDEF_0123456789abcdefFEDCBA9876543210',
  ].join('\n');
  const out = redact(report, { secrets: SECRETS });
  assert.ok(!out.includes('9f8e7d6c5b4a32109876543210abcdef'), out);
  assert.ok(!out.includes('0123456789abcdefFEDCBA9876543210'), out);
  assert.match(out, /tsk_test_<redacted>/);
  assert.match(out, /tsk_live_<redacted>/);
});

test('a session cookie value never survives, in a header or in JSON', () => {
  const report = 'cookie: ts_session=Zm9vYmFyLTEyMzQ1Njc4OTA; other=1\n{"cookie":"ts_session=Zm9vYmFyLTEyMzQ1Njc4OTA"}';
  const out = redact(report, { secrets: SECRETS });
  assert.ok(!out.includes('Zm9vYmFyLTEyMzQ1Njc4OTA'), out);
  assert.equal((out.match(/ts_session=<redacted>/g) || []).length, 2);
  assert.match(out, /other=1/); // the diagnostic context is kept
});

test('an Authorization: Bearer header never survives, in any casing', () => {
  const report = [
    'authorization: Bearer tsk_live_01JABC_secretsecretsecret',
    'Authorization: bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9',
    '"Authorization":"Bearer 0123456789abcdef0123456789abcdef"',
  ].join('\n');
  const out = redact(report, { secrets: SECRETS });
  assert.ok(!out.includes('secretsecretsecret'), out);
  assert.ok(!out.includes('eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9'), out);
  assert.ok(!out.includes('0123456789abcdef0123456789abcdef'), out);
  assert.equal((out.match(/Bearer <redacted>/g) || []).length, 3);
});

test('a postgres DSN keeps its host and database and loses its credentials', () => {
  const report = 'connect failed: postgresql://techsara_e2e:hunter2hunter2@127.0.0.1:5432/techsara_e2e_ci_test';
  const out = redact(report, { secrets: SECRETS });
  assert.ok(!out.includes('hunter2hunter2'), out);
  assert.ok(!out.includes('techsara_e2e:'), out);
  assert.match(out, /postgresql:\/\/<redacted>:<redacted>@127\.0\.0\.1:5432\/techsara_e2e_ci_test/);
});

test('a postgres:// DSN (the short scheme) is covered too', () => {
  const out = redact('postgres://u:p4ssw0rd-long-enough@db:5432/x', { secrets: SECRETS });
  assert.ok(!out.includes('p4ssw0rd-long-enough'), out);
});

test('THE B5 FIX: the per-run admin password is removed by literal value', () => {
  // Exactly what lib/harness.js writes when auth.login fails: the check's
  // error, verbatim, with the request body it was rejected for.
  const report = [
    '## Failures, verbatim',
    '### `auth.login`',
    '```text',
    `sign-in returned 401 for {"username":"e2e-admin@ci.invalid","password":"${ADMIN_PASSWORD}"}`,
    '```',
  ].join('\n');
  const out = redact(report, { secrets: SECRETS });
  assert.ok(!out.includes(ADMIN_PASSWORD), 'the admin password survived redaction');
  assert.match(out, /<redacted/);
});

test('THE B5 FIX: the per-run member password is removed from a browser console error', () => {
  const report = [
    'Browser console errors seen during the check:',
    '```text',
    `Failed to load resource: POST /api/auth/login body={"password":"${MEMBER_PASSWORD}"} 429`,
    '```',
  ].join('\n');
  const out = redact(report, { secrets: SECRETS });
  assert.ok(!out.includes(MEMBER_PASSWORD), 'the member password survived redaction');
});

test('THE B5 FIX: a generated password survives neither a URL nor a form encoding', () => {
  const encoded = encodeURIComponent(ADMIN_PASSWORD);
  const report = `GET /login?user=a&password=${encoded} 302`;
  const out = redact(report, { secrets: SECRETS });
  assert.ok(!out.includes(ADMIN_PASSWORD), out);
  assert.ok(!out.includes(encoded), out);
});

test('THE B5 FIX: a generated password inside a DSN is gone even though the DSN rule also fires', () => {
  const report = `postgresql://app:${ADMIN_PASSWORD}@127.0.0.1:5432/db`;
  const out = redact(report, { secrets: SECRETS });
  assert.ok(!out.includes(ADMIN_PASSWORD), out);
});

test('THE B5 FIX: a password in PROSE, with no key name near it, is only caught by value', () => {
  // The shape lib/harness.js describeError() produces: the check's own
  // message, verbatim. No `password=`, no JSON key, no DSN — nothing a
  // pattern can key off. This case is the whole argument for passing the
  // generated values in by literal value.
  const report = `sign-in as e2e-ci-admin@test.local with ${ADMIN_PASSWORD} was refused (401)`;
  const out = redact(report, { secrets: SECRETS });
  assert.ok(!out.includes(ADMIN_PASSWORD), 'a bare password in prose survived redaction');
});

test('THE B5 FIX: a TRUNCATED body, where the closing quote is gone, is only caught by value', () => {
  // suites/console.js interpolates truncate(res.text, 200..300) of a real
  // response into its assertion messages. A cut mid-value leaves
  // `"password":"<secret>` with no closing quote, which the password-field
  // pattern cannot match — it needs the pair.
  const report = `unexpected 500; body was {"username":"e2e-ci-admin@test.local","password":"${MEMBER_PASSWORD}`;
  const out = redact(report, { secrets: SECRETS });
  assert.ok(!out.includes(MEMBER_PASSWORD), 'a truncated password survived redaction');
});

test('every generated secret is reported as a literal-secret hit, by name and count only', () => {
  const { hits } = redactWithCounts(`${ADMIN_PASSWORD} and ${MEMBER_PASSWORD}`, { secrets: SECRETS });
  assert.equal(hits['literal-secret'], 2);
});

test('a report with no secrets passes through BYTE-IDENTICAL', () => {
  const clean = [
    '# Platform regression run',
    '',
    '- Base URL: `http://127.0.0.1:3901`',
    '- Totals: **41 passed, 1 failed, 2 skipped** of 44',
    '',
    '| # | Suite | Check | Result |',
    '|---|---|---|---|',
    '| 1 | chat | `chat.send-stream` the answer streams and is stored once | PASS |',
    '',
    '## Failures, verbatim',
    '',
    '```text',
    'responsive.admin-invitations: at 360px div.h-10.mt-6.flex.w-fit ends 3px past the screen edge',
    '    at Object.fn (/repo/e2e/platform/suites/responsive.js:61:13)',
    '```',
    '', // a deliberate trailing blank line: renderMarkdown ends with one
  ].join('\n');
  const out = redact(clean, { secrets: SECRETS });
  assert.equal(out, clean);
  assert.deepEqual(Buffer.from(out), Buffer.from(clean));
  assert.equal(redactWithCounts(clean, { secrets: SECRETS }).total, 0);
});

test('a short "secret" is refused, so a careless value cannot shred the report', () => {
  const short = 'abc';
  assert.ok(short.length < MIN_SECRET_LENGTH);
  const clean = 'the abc of the matter is that abc appears everywhere';
  assert.equal(redact(clean, { secrets: [short] }), clean);
});

test('no rule leaves the secret inside its own replacement', () => {
  const report = [
    `tsk_live_01J_${ADMIN_PASSWORD}`,
    `ts_session=${MEMBER_PASSWORD}`,
    `authorization: Bearer ${ADMIN_PASSWORD}`,
  ].join('\n');
  const out = redact(report, { secrets: SECRETS });
  for (const secret of SECRETS) assert.ok(!out.includes(secret), `${secret.slice(0, 6)}… survived: ${out}`);
});

test('redact never throws on the odd shapes a report really contains', () => {
  for (const input of ['', '\n', 'ts_session=', 'password=', 'postgres://@/', null, undefined]) {
    assert.doesNotThrow(() => redact(input, { secrets: SECRETS }));
  }
});
