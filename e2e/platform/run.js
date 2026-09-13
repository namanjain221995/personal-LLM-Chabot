#!/usr/bin/env node
'use strict';
/**
 * Release regression suite for the whole platform.
 *
 *   node run.js [--base http://127.0.0.1:3001] [--only chat,gating] [--skip responsive]
 *               [--widths 360,768,1440] [--chat-mode stub|live] [--out DIR] [--label TEXT]
 *               [--list] [--headful]
 *
 * Writes <out>/results.json, <out>/results.md and <out>/screenshots/*.png, prints
 * one line per check, and exits 1 when any check failed (2 on a usage error).
 * See README.md for pointing it at a candidate container.
 */

const fs = require('fs');
const path = require('path');

const { resolveConfig, baseRefusal } = require('./lib/config');
const { Registry, runAll, renderMarkdown } = require('./lib/harness');
const { launchBrowser, makeSessions } = require('./lib/browser');
const { HttpClient } = require('./lib/http');

const SUITES = ['auth', 'gating', 'chat', 'uploads', 'artifacts', 'admin', 'console', 'responsive'];

async function main() {
  let cfg;
  try {
    cfg = resolveConfig();
  } catch (err) {
    console.error(`usage error: ${err.message}`);
    process.exit(2);
  }

  const registry = new Registry();
  for (const name of SUITES) require(`./suites/${name}`).register(registry, cfg);

  const selected = registry.select(cfg.only, cfg.skip);
  if (cfg.list) {
    for (const t of selected) console.log(`${t.id.padEnd(40)} ${t.title}`);
    return;
  }
  let refusal;
  try {
    refusal = baseRefusal(cfg.base, { allowRemote: cfg.allowRemote, allowedPorts: cfg.allowedPorts });
  } catch (err) {
    refusal = err.message;
  }
  if (refusal) {
    console.error(`refusing to run against ${cfg.base}: this suite creates and deletes data, and ${refusal}.`);
    process.exit(2);
  }

  fs.mkdirSync(cfg.outDir, { recursive: true });
  const startedAt = new Date().toISOString();
  console.log(`platform e2e: ${selected.length} checks against ${cfg.base} (chat mode ${cfg.chatMode}); output ${cfg.outDir}`);

  let browser = null;
  let browserVersion = '';
  const browserRef = async () => {
    if (!browser) {
      browser = await launchBrowser(cfg);
      browserVersion = await browser.version();
    }
    return browser;
  };
  // Shared, run-scoped state: ids one check hands to the next.
  const state = {};
  const cleanups = [];

  const makeContext = (test, result) => {
    const sessions = makeSessions(browserRef, cfg, test, result);
    return {
      cfg,
      state,
      result,
      outDir: cfg.outDir,
      http: sessions.http,
      page: sessions.page,
      adopt: sessions.adopt,
      note: (msg) => result.notes.push(String(msg)),
      /**
       * Undo something at the end of the run, pass or fail. `fn(httpFor)`
       * gets signed-in clients that are NOT tied to this check (which is
       * disposed by then) and must throw when the undo did not happen; the
       * outcome is printed and written to results.md.
       */
      addCleanup: (label, fn) => cleanups.push({ label, from: test.id, fn }),
      skip: (reason) => {
        const { SkipError } = require('./lib/harness');
        throw new SkipError(reason);
      },
      _screenshotOpenPages: sessions.screenshotOpenPages,
      _dispose: sessions.dispose,
    };
  };

  let results;
  const cleanupResults = [];
  try {
    results = await runAll(selected, makeContext, { outDir: cfg.outDir });
  } finally {
    // Clean-ups a suite registered for the end of the run (keys, projects,
    // conversations), newest first, whatever happened in between. Drained
    // until empty, so one registered while an earlier one ran still runs.
    while (cleanups.length) {
      const item = cleanups.pop();
      const clients = [];
      const httpFor = async (role) => {
        const client = new HttpClient(cfg.base);
        const who = cfg[role];
        const password = who && who.password();
        if (!password) throw new Error(`no password for the ${role} account`);
        await client.login(who.email, password);
        clients.push(client);
        return client;
      };
      const outcome = { label: item.label, from: item.from, status: 'done', error: null };
      try {
        await item.fn(httpFor);
      } catch (err) {
        outcome.status = 'failed';
        outcome.error = err && err.message ? err.message : String(err);
        console.log(`CLEANUP FAILED ${item.label} (from ${item.from}): ${outcome.error}`);
      }
      for (const client of clients) await client.post('/api/auth/logout').catch(() => {});
      cleanupResults.push(outcome);
    }
    if (browser) await browser.close().catch(() => {});
  }

  const meta = {
    base: cfg.base,
    label: cfg.label,
    chatMode: cfg.chatMode,
    widths: cfg.widths,
    startedAt,
    finishedAt: new Date().toISOString(),
    browserVersion,
    node: process.version,
    // What was under test, as the operator states it: a tag says nothing
    // about uncommitted edits in the tree it was built from (review,
    // 2026-09-13), so the image id and the git state are recorded verbatim.
    target: {
      frontendImage: process.env.E2E_TARGET_FRONTEND_IMAGE || '',
      orchestratorImage: process.env.E2E_TARGET_ORCHESTRATOR_IMAGE || '',
      git: process.env.E2E_TARGET_GIT || '',
    },
    cleanups: cleanupResults,
  };
  fs.writeFileSync(path.join(cfg.outDir, 'results.json'), JSON.stringify({ meta, results }, null, 2));
  const md = renderMarkdown(results, meta);
  fs.writeFileSync(path.join(cfg.outDir, 'results.md'), md);

  const failed = results.filter((r) => r.status === 'fail').length;
  const skipped = results.filter((r) => r.status === 'skip').length;
  const cleanupFailed = cleanupResults.filter((c) => c.status === 'failed').length;
  console.log('');
  console.log(
    `${results.length - failed - skipped} passed, ${failed} failed, ${skipped} skipped; cleanups ${cleanupResults.length - cleanupFailed} done, ${cleanupFailed} failed — ${path.join(cfg.outDir, 'results.md')}`,
  );
  // A cleanup that did not happen leaves data (or a live key) behind: that
  // fails the run even when every check passed.
  process.exitCode = failed || cleanupFailed ? 1 : 0;
}

main().catch((err) => {
  console.error(err && err.stack ? err.stack : err);
  process.exit(2);
});
