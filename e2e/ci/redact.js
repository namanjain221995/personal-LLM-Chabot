#!/usr/bin/env node
'use strict';
/**
 * The redactor every e2e artifact passes through before it is uploaded.
 *
 * WHY THIS FILE EXISTS
 * e2e/platform keeps failures VERBATIM, which is the right call for a report
 * a human reads on the box and the wrong one for a file uploaded from a
 * PUBLIC repository's CI run:
 *
 *   - lib/harness.js:164 writes each failed check's `r.error` verbatim under
 *     "## Failures, verbatim", and :175 writes every entry of
 *     `r.consoleErrors` verbatim after it;
 *   - run.js:150 serialises `{meta, results}` into results.json, so the same
 *     strings are in the JSON as well;
 *   - suites/console.js interpolates `truncate(res.text, 200..300)` of real
 *     API responses into its assertion messages, and it creates a real API
 *     key against a real `/v1` on every run.
 *
 * The suite's ONLY redaction is for SCREENSHOTS (lib/browser.js
 * SECRET_SELECTORS, blanked before the capture). Its own comment gives the
 * reason the text report needs an equivalent: "A failure while the one-time
 * key secret is on screen must not write it into a PNG." The same sentence is
 * true of results.md, and nothing enforced it until this file.
 *
 * `::add-mask::` IS NOT A SUBSTITUTE. A mask registered with the Actions
 * runner replaces the value in LOG OUTPUT ONLY. It does not touch the bytes
 * that actions/upload-artifact uploads, and an artifact on a public
 * repository is readable by anyone with the run URL. Do not delete this step
 * believing masking covers it.
 *
 * WHAT IT REMOVES
 *   1. API keys              tsk_test_… / tsk_live_… (app/apiplatform/keys.py)
 *   2. Session cookies       ts_session=<value>, in a header, a log or JSON
 *   3. Bearer tokens         authorization: Bearer <value>, any casing
 *   4. Database URLs         postgres:// and postgresql:// with credentials
 *   5. Password fields       "password": "…" and password=… in a request body
 *   6. LITERAL SECRETS       the per-run generated passwords, passed in by
 *                            value. THIS IS THE ONE THAT MATTERS MOST: the
 *                            auth suite signs in with them, so a failing
 *                            sign-in check or a browser console error that
 *                            carries a request body writes the plaintext
 *                            password into the report. A pattern cannot catch
 *                            a random hex string; only the value can.
 *
 * Usage:
 *   node e2e/ci/redact.js --secrets-file <path> [--secrets-file <path>] <file-or-dir>...
 *   node e2e/ci/redact.js --self-test            (no I/O; prints a summary)
 *
 * Exit 0 when every path was processed, 2 on a usage error, 1 when a file
 * could not be read or written.
 */

const fs = require('fs');
const path = require('path');

/** A literal shorter than this is not redacted: replacing a 3-character
 *  "password" would shred the report and hide the failure it was written to
 *  explain. CI generates 48 hex characters (`openssl rand -hex 24`). */
const MIN_SECRET_LENGTH = 8;

/** Binary artifacts are copied untouched — screenshots are already blanked by
 *  lib/browser.js, and a byte-wise rewrite would corrupt them. */
const BINARY_EXTENSIONS = new Set([
  '.png', '.jpg', '.jpeg', '.gif', '.webp', '.pdf', '.zip', '.gz', '.tgz',
  '.bz2', '.xz', '.mp4', '.webm', '.mp3', '.wav', '.woff', '.woff2', '.ico',
]);

const PLACEHOLDER = '<redacted>';

/**
 * Pattern rules, applied in order. Each keeps enough of the surrounding text
 * for the report to stay readable: a redacted key still shows that a key was
 * there, which is usually the whole point of the failing line.
 */
const RULES = [
  {
    name: 'api-key',
    // tsk_live_<public_id>_<secret><checksum> (app/apiplatform/keys.py:5).
    // The prefix is kept so a reader can see WHICH environment it was.
    re: /\btsk_(test|live)_[A-Za-z0-9._-]+/g,
    replace: (_m, env) => `tsk_${env}_${PLACEHOLDER}`,
  },
  {
    name: 'session-cookie',
    // `ts_session=…` in a Cookie/Set-Cookie header, a curl line or JSON. The
    // value ends at a cookie separator, quote, or whitespace.
    re: /\bts_session=([^;,"'\s\\]+)/g,
    replace: () => `ts_session=${PLACEHOLDER}`,
  },
  {
    name: 'bearer',
    // `authorization: Bearer …`, `"Authorization":"Bearer …"`, any casing.
    re: /\b(authorization)(["']?\s*[:=]\s*["']?)bearer\s+([^\s"',}\\]+)/gi,
    replace: (_m, key, sep) => `${key}${sep}Bearer ${PLACEHOLDER}`,
  },
  {
    name: 'bearer-bare',
    // A Bearer token with no header name in front of it (a stack frame, a
    // truncated body). Keeps the word so the shape stays visible.
    re: /\bBearer\s+(tsk_[A-Za-z0-9._-]+|[A-Za-z0-9._-]{16,})/g,
    replace: () => `Bearer ${PLACEHOLDER}`,
  },
  {
    name: 'postgres-dsn',
    // postgres://user:password@host:port/db — the credentials go, the host
    // and database stay, because "which database" is diagnostic and the
    // password is not.
    re: /\b(postgres(?:ql)?:\/\/)([^\s:/@"']+)(?::([^\s@"']*))?@/g,
    replace: (_m, scheme) => `${scheme}${PLACEHOLDER}:${PLACEHOLDER}@`,
  },
  {
    name: 'password-field',
    // "password": "…" / 'password':'…' in a JSON body quoted into a message.
    re: /(["']?(?:password|passwd|pwd|secret|api_key|apiKey)["']?\s*[:=]\s*)(["'])((?:\\.|(?!\2)[^\\])*)\2/gi,
    replace: (_m, head, quote) => `${head}${quote}${PLACEHOLDER}${quote}`,
  },
  {
    name: 'password-form',
    // password=… in a form body or a query string (no quotes around it).
    re: /\b(password|passwd|pwd)=([^\s&"',;}\\]+)/gi,
    replace: (_m, key) => `${key}=${PLACEHOLDER}`,
  },
];

function escapeRegExp(value) {
  return String(value).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

/**
 * The literal-value rules for one run's generated secrets. Each secret is
 * removed in its plain form AND percent-encoded, because a value that reached
 * a URL or a form body arrives encoded.
 */
function literalRules(secrets) {
  const rules = [];
  const seen = new Set();
  for (const raw of secrets || []) {
    const secret = String(raw == null ? '' : raw).trim();
    if (secret.length < MIN_SECRET_LENGTH) continue;
    for (const form of [secret, encodeURIComponent(secret)]) {
      if (seen.has(form)) continue;
      seen.add(form);
      rules.push({
        name: 'literal-secret',
        re: new RegExp(escapeRegExp(form), 'g'),
        replace: () => '<redacted:secret>',
      });
    }
  }
  return rules;
}

/**
 * Redact `text`. Returns the text unchanged — byte for byte — when nothing
 * matched: a clean report must not be reformatted, retyped or re-encoded by
 * passing through here.
 *
 * @param {string} text
 * @param {{secrets?: string[]}} [opts]
 * @returns {string}
 */
function redact(text, opts = {}) {
  return redactWithCounts(text, opts).text;
}

/**
 * Redact and say what was hit, by rule name. The counts are safe to print:
 * they are names and numbers, never the matched value.
 *
 * @returns {{text: string, hits: Record<string, number>, total: number}}
 */
function redactWithCounts(text, opts = {}) {
  const input = String(text == null ? '' : text);
  const hits = {};
  let total = 0;
  let out = input;
  // The literal secrets run FIRST: a generated password inside a DSN or a
  // JSON field must be gone even if a later rule would have rewritten the
  // surrounding text and hidden it from the literal match.
  for (const rule of [...literalRules(opts.secrets), ...RULES]) {
    out = out.replace(rule.re, (...args) => {
      hits[rule.name] = (hits[rule.name] || 0) + 1;
      total += 1;
      return rule.replace(...args);
    });
  }
  return { text: out, hits, total };
}

// ------------------------------------------------------------------- the CLI

function readSecretsFile(file) {
  // One secret per line; blank lines and `#` comments ignored, so a file can
  // say what it holds without that comment becoming a "secret".
  const lines = fs.readFileSync(file, 'utf8').split('\n');
  return lines.map((l) => l.trim()).filter((l) => l && !l.startsWith('#'));
}

function* walk(target) {
  const stat = fs.statSync(target);
  if (stat.isFile()) {
    yield target;
    return;
  }
  if (!stat.isDirectory()) return;
  for (const entry of fs.readdirSync(target, { withFileTypes: true })) {
    const child = path.join(target, entry.name);
    if (entry.isSymbolicLink()) continue; // never follow a link out of the tree
    if (entry.isDirectory()) yield* walk(child);
    else if (entry.isFile()) yield child;
  }
}

function main(argv) {
  const targets = [];
  const secrets = [];
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (arg === '--secrets-file') {
      const file = argv[i + 1];
      i += 1;
      if (!file) {
        process.stderr.write('redact: --secrets-file needs a path\n');
        return 2;
      }
      try {
        secrets.push(...readSecretsFile(file));
      } catch (err) {
        process.stderr.write(`redact: cannot read ${file}: ${err.code || err.message}\n`);
        return 1;
      }
    } else if (arg === '--secret-env') {
      // The NAME of an environment variable holding one secret, so the value
      // never appears in this process's argv (and so `ps` cannot read it).
      const name = argv[i + 1];
      i += 1;
      if (!name) {
        process.stderr.write('redact: --secret-env needs a variable name\n');
        return 2;
      }
      if (process.env[name]) secrets.push(process.env[name]);
    } else if (arg === '--self-test') {
      const sample = 'key tsk_live_abc_def ts_session=zzz';
      const out = redact(sample, { secrets: [] });
      process.stdout.write(`${out}\n`);
      return out.includes('tsk_live_abc_def') || out.includes('zzz') ? 1 : 0;
    } else if (arg.startsWith('--')) {
      process.stderr.write(`redact: unknown option ${arg}\n`);
      return 2;
    } else {
      targets.push(arg);
    }
  }
  if (!targets.length) {
    process.stderr.write('usage: redact.js [--secrets-file F] [--secret-env NAME] <file-or-dir>...\n');
    return 2;
  }

  const totals = {};
  let files = 0;
  let changed = 0;
  let failed = 0;
  for (const target of targets) {
    let entries;
    try {
      entries = [...walk(target)];
    } catch (err) {
      process.stderr.write(`redact: cannot walk ${target}: ${err.code || err.message}\n`);
      failed += 1;
      continue;
    }
    for (const file of entries) {
      if (BINARY_EXTENSIONS.has(path.extname(file).toLowerCase())) continue;
      files += 1;
      try {
        const before = fs.readFileSync(file, 'utf8');
        const { text, hits, total } = redactWithCounts(before, { secrets });
        if (total > 0) {
          fs.writeFileSync(file, text, 'utf8');
          changed += 1;
          for (const [name, n] of Object.entries(hits)) totals[name] = (totals[name] || 0) + n;
        }
      } catch (err) {
        process.stderr.write(`redact: cannot process ${file}: ${err.code || err.message}\n`);
        failed += 1;
      }
    }
  }
  const summary = Object.entries(totals)
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([name, n]) => `${name}=${n}`)
    .join(' ');
  process.stdout.write(
    `redact: ${files} text file(s) scanned, ${changed} rewritten${summary ? `; ${summary}` : '; nothing matched'}\n`,
  );
  return failed ? 1 : 0;
}

if (require.main === module) {
  process.exitCode = main(process.argv.slice(2));
}

module.exports = { redact, redactWithCounts, literalRules, RULES, MIN_SECRET_LENGTH, PLACEHOLDER, main };
