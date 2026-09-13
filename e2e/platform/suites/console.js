'use strict';
/**
 * The developer console at /api, driven the way an admin uses it: every tab
 * renders; a project is created; a key is minted, shown once, works against
 * /v1, and stops working the moment it is revoked; the models list; the
 * playground.
 */

const assert = require('assert/strict');
const { clickByText, sleep, waitForText } = require('../lib/browser');
const { truncate } = require('../lib/http');
const { playgroundStubStream } = require('../lib/stubs');

const TABS = [
  ['overview', /developer platform|overview/i],
  ['projects', /^projects$/i],
  ['keys', /api keys/i],
  ['models', /^models$/i],
  ['playground', /^playground$/i],
  ['usage', /^usage$/i],
  ['logs', /request logs|logs/i],
  ['webhooks', /^webhooks$/i],
  ['settings', /^settings$/i],
];

async function consolePage(ctx, tab) {
  const { page, client } = await ctx.page({ role: 'admin' });
  await page.goto(`${ctx.cfg.base}${tab === 'overview' ? '/api' : `/api?tab=${tab}`}`, { waitUntil: 'domcontentloaded' });
  await page.waitForSelector('h1', { timeout: 30_000 });
  return { page, client };
}

async function h1Texts(page) {
  return page.$$eval('h1', (hs) => hs.map((h) => h.innerText.trim()));
}

async function bearer(ctx, secret, path) {
  const res = await fetch(`${ctx.cfg.base}${path}`, { headers: { authorization: `Bearer ${secret}` } });
  const text = await res.text();
  let json = null;
  try {
    json = JSON.parse(text);
  } catch {
    json = null;
  }
  return { status: res.status, text, json };
}

function register(t, cfg) {
  t.add(
    'console',
    'console.tabs-render',
    'Every API console tab renders its heading without an error panel',
    async (ctx) => {
      const { page } = await consolePage(ctx, 'overview');
      const problems = [];
      for (const [tab, heading] of TABS) {
        await page.goto(`${ctx.cfg.base}${tab === 'overview' ? '/api' : `/api?tab=${tab}`}`, { waitUntil: 'domcontentloaded' });
        const ok = await page
          .waitForFunction((src, fl) => [...document.querySelectorAll('h1')].some((h) => new RegExp(src, fl).test(h.innerText.trim())), { timeout: 20_000 }, heading.source, heading.flags)
          .then(() => true)
          .catch(() => false);
        if (!ok) {
          problems.push(`${tab}: no heading matching ${heading} (headings: ${JSON.stringify(await h1Texts(page))})`);
          continue;
        }
        await sleep(1200);
        const alerts = await page.$$eval('[role="alert"]', (els) => els.map((e) => e.innerText.trim()).filter(Boolean));
        if (alerts.length) problems.push(`${tab}: error shown — ${alerts.join(' | ')}`);
      }
      assert.equal(problems.length, 0, `console tabs with problems:\n${problems.join('\n')}`);
    },
  );

  t.add(
    'console',
    'console.project-create',
    'An admin creates a project from the Projects tab and the console API lists it',
    async (ctx) => {
      const name = `e2e project ${Date.now().toString(36)}`;
      const { page, client } = await consolePage(ctx, 'projects');
      await waitForText(page, /new project/i, 20_000);
      assert.ok(await clickByText(page, 'button', /^new project$/i), 'no New project button');
      await page.waitForSelector('[role="dialog"] input', { visible: true, timeout: 10_000 });
      await page.type('[role="dialog"] input', name);
      assert.ok(await clickByText(page, '[role="dialog"] button', /^create project$/i), 'no Create project button');
      await page.waitForFunction(() => !document.querySelector('[role="dialog"]'), { timeout: 20_000 }).catch(async () => {
        const alert = await page.$eval('[role="dialog"] [role="alert"]', (e) => e.innerText).catch(() => '(no alert)');
        throw new Error(`the New project dialog did not close; it says: ${alert}`);
      });
      await waitForText(page, name, 20_000);
      const list = await client.get('/api/devplatform/projects');
      assert.equal(list.status, 200, `GET /api/devplatform/projects → ${list.status}: ${truncate(list.text, 300)}`);
      const project = (list.json.projects || []).find((p) => p.name === name);
      assert.ok(project, `the console API does not list "${name}"`);
      ctx.state.project = { id: project.id, name };
      ctx.addCleanup(`disable API project ${project.id} (projects cannot be deleted)`, async (httpFor) => {
        const c = await httpFor('admin');
        const res = await c.patch(`/api/devplatform/projects/${encodeURIComponent(project.id)}`, { json: { status: 'disabled' } });
        if (![200].includes(res.status)) throw new Error(`PATCH project status disabled → ${res.status}: ${truncate(res.text, 200)}`);
      });
      ctx.note(`project ${project.id} (${project.environment})`);
    },
  );

  t.add(
    'console',
    'console.key-create',
    'Creating a key shows its secret exactly once, and the secret authenticates against /v1',
    async (ctx) => {
      const { id: projectId, name: projectName } = ctx.state.project;
      const keyName = `e2e key ${Date.now().toString(36)}`;
      const { page } = await consolePage(ctx, 'keys');
      await page.waitForSelector('[data-testid="project-select"] select', { timeout: 20_000 });
      await page.select('[data-testid="project-select"] select', projectId);
      await waitForText(page, /no keys in this project|create key/i, 20_000);
      assert.ok(await clickByText(page, 'button', /^create key$/i), 'no Create key button');
      await page.waitForSelector('[role="dialog"] input', { visible: true, timeout: 10_000 });
      await page.type('[role="dialog"] input', keyName);
      await page.waitForFunction(
        () => [...document.querySelectorAll('[role="dialog"] button[type="submit"]')].some((b) => !b.disabled),
        { timeout: 10_000 },
      );
      await page.click('[role="dialog"] button[type="submit"]');
      const secretEl = await page.waitForSelector('[data-testid="created-key-secret"]', { visible: true, timeout: 20_000 }).catch(async () => {
        const alert = await page.$eval('[role="dialog"] [role="alert"]', (e) => e.innerText).catch(() => '(no alert)');
        throw new Error(`no secret was shown after Create key; the dialog says: ${alert}`);
      });
      const secret = (await secretEl.evaluate((n) => n.innerText || n.value || '')).trim();
      assert.match(secret, /^tsk_(test|live)_/, `the shown secret does not look like a key (${secret.slice(0, 9)}…)`);
      ctx.state.key = { secret, name: keyName, projectId };
      // Registered the moment a live secret exists, so no failure below can
      // leave a working key behind (review, 2026-09-13). Idempotent: a key
      // console.key-revoke already revoked is revoked again, harmlessly.
      ctx.addCleanup(`revoke API key "${keyName}"`, async (httpFor) => {
        const c = await httpFor('admin');
        const list = await c.get(`/api/devplatform/projects/${encodeURIComponent(projectId)}/keys`);
        if (![200].includes(list.status)) throw new Error(`GET project keys → ${list.status}: ${truncate(list.text, 200)}`);
        const key = ((list.json && list.json.keys) || []).find((k) => k.name === keyName);
        if (!key) throw new Error(`key "${keyName}" is not listed in project ${projectId}`);
        const res = await c.post(`/api/devplatform/projects/${encodeURIComponent(projectId)}/keys/${encodeURIComponent(key.id)}/revoke`);
        if (![200].includes(res.status)) throw new Error(`POST key revoke → ${res.status}: ${truncate(res.text, 200)}`);
        const probe = await bearer(ctx, secret, '/v1/models');
        if (probe.status !== 401) throw new Error(`the key still answers /v1/models with ${probe.status} after revoking`);
      });

      assert.ok(await clickByText(page, '[role="dialog"] button', /^done$/i), 'no Done button on the secret dialog');
      await page.waitForFunction(() => !document.querySelector('[data-testid="created-key-secret"]'), { timeout: 10_000 });
      await waitForText(page, keyName, 20_000);
      // Shown once: nothing on the page may still carry the secret.
      const html = await page.content();
      assert.ok(!html.includes(secret), 'the secret is still present in the page after the dialog closed');

      const models = await bearer(ctx, secret, '/v1/models');
      assert.equal(models.status, 200, `GET /v1/models with the new key → ${models.status}: ${truncate(models.text, 300)}`);
      ctx.note(`key "${keyName}" in project "${projectName}"; /v1/models lists ${(models.json.data || []).length} models`);
    },
    { needs: ['console.project-create'] },
  );

  t.add(
    'console',
    'v1.models-published',
    `/v1/models lists every published model the owner decided on (${cfg.expectedModels.join(', ')}) and each one resolves`,
    async (ctx) => {
      const { secret } = ctx.state.key;
      const models = await bearer(ctx, secret, '/v1/models');
      assert.equal(models.status, 200, `GET /v1/models → ${models.status}: ${truncate(models.text, 300)}`);
      const ids = (models.json.data || []).map((m) => m.id);
      ctx.note(`listed: ${ids.join(', ') || '(none)'}`);
      const missing = cfg.expectedModels.filter((id) => !ids.includes(id));
      const unresolved = [];
      for (const id of ids) {
        const one = await bearer(ctx, secret, `/v1/models/${encodeURIComponent(id)}`);
        if (one.status !== 200) unresolved.push(`${id} → ${one.status}`);
      }
      assert.equal(missing.length + unresolved.length, 0, `missing from /v1/models: ${missing.join(', ') || 'none'}; listed but not resolvable: ${unresolved.join(', ') || 'none'}`);
    },
    { needs: ['console.key-create'] },
  );

  t.add(
    'console',
    'console.models-tab',
    'The Models tab shows a card for every model the console API publishes',
    async (ctx) => {
      const { page, client } = await consolePage(ctx, 'models');
      const api = await client.get('/api/devplatform/models');
      assert.equal(api.status, 200, `GET /api/devplatform/models → ${api.status}: ${truncate(api.text, 300)}`);
      const ids = (api.json.models || api.json.data || []).map((m) => m.id);
      assert.ok(ids.length > 0, `the console API publishes no models: ${truncate(api.text, 300)}`);
      // Cards (data-testid="model-card-<id>") on current builds; older builds
      // drew a table. Either way each id must be its OWN element — a card, or
      // a cell/code element whose whole text is the id — not a substring
      // somewhere in the page (a snippet naming one model would satisfy that
      // for all of them; review, 2026-09-13).
      await page
        .waitForFunction((first) => document.body.innerText.includes(first), { timeout: 20_000 }, ids[0])
        .catch(() => {});
      const shown = await page.evaluate((wanted) => {
        const cards = [...document.querySelectorAll('[data-testid^="model-card-"]')].map((e) => e.getAttribute('data-testid').replace('model-card-', ''));
        const exact = new Set();
        for (const el of document.querySelectorAll('td, th, tr div, tr span, code, h2, h3, h4, [data-model-id]')) {
          const text = (el.getAttribute('data-model-id') || el.innerText || '').trim();
          if (wanted.includes(text)) exact.add(text);
        }
        return { cards, exact: [...exact] };
      }, ids);
      const cards = shown.cards;
      const missing = ids.filter((id) => (cards.length ? !cards.includes(id) : !shown.exact.includes(id)));
      assert.equal(
        missing.length,
        0,
        `published models without their own card/row on the Models tab: ${missing.join(', ')} (cards: ${cards.join(', ') || 'none'}; exact cells: ${shown.exact.join(', ') || 'none'})`,
      );
      ctx.note(`${ids.length} published: ${ids.join(', ')}; ${cards.length ? `${cards.length} cards` : 'no model-card elements (table layout)'}`);
    },
  );

  t.add(
    'console',
    'console.playground',
    `The playground runs a request and renders the streamed output and a code snippet (${cfg.chatMode} engine)`,
    async (ctx) => {
      const { page } = await consolePage(ctx, 'playground');
      const answer = `Playground stub ${Date.now().toString(36)} rendered.`;
      const calls = [];
      if (ctx.cfg.chatMode === 'stub') {
        await page.setRequestInterception(true);
        page.on('request', (req) => {
          if (req.isInterceptResolutionHandled()) return;
          const u = new URL(req.url());
          if (req.method() === 'POST' && u.pathname === '/api/devplatform/playground/execute') {
            calls.push(req.postData() || '');
            req.respond({ status: 200, headers: { 'content-type': 'text/event-stream; charset=utf-8' }, body: playgroundStubStream(answer) });
            return;
          }
          req.continue();
        });
      }
      await page.waitForSelector('textarea', { visible: true, timeout: 20_000 });
      const inputs = await page.$$('textarea');
      await inputs[inputs.length - 1].type('Say hello in five words.');
      assert.ok(await clickByText(page, 'button', /^run$/i), 'no Run button');
      await page.waitForFunction(
        () => {
          const out = document.querySelector('[data-testid="playground-output"]');
          const alert = document.querySelector('section[aria-label="Response"] [role="alert"]');
          return (out && out.innerText.trim().length > 0 && !document.querySelector('button[aria-label="Stop"], button.stop')) || alert;
        },
        { timeout: ctx.cfg.chatMode === 'stub' ? 20_000 : 170_000, polling: 500 },
      );
      const output = await page.$eval('[data-testid="playground-output"]', (e) => e.innerText.trim()).catch(() => '');
      const alert = await page.$eval('section[aria-label="Response"] [role="alert"]', (e) => e.innerText.trim()).catch(() => '');
      if (ctx.cfg.chatMode === 'stub') {
        assert.equal(calls.length, 1, `expected one playground execute call, saw ${calls.length}`);
        const body = JSON.parse(calls[0]);
        assert.ok(body.model, `the playground request names no model: ${truncate(calls[0], 300)}`);
        assert.equal(alert, '', `the playground shows an error for a clean stream: ${alert}`);
        assert.ok(output.includes(answer), `the output is "${truncate(output, 200)}", expected the streamed text`);
      } else {
        ctx.note(alert ? `live run refused/failed with: ${alert}` : `live output: ${truncate(output, 120)}`);
      }
      const snippet = await page.$eval('[data-testid="playground-snippet"]', (e) => e.innerText).catch(() => '');
      assert.ok(/\/v1\/(responses|chat\/completions)/.test(snippet), `the code snippet does not call /v1: ${truncate(snippet, 200)}`);
    },
    { timeoutMs: 240_000 },
  );

  t.add(
    'console',
    'console.key-revoke',
    'Revoking the key from its row menu marks it revoked, and /v1 refuses it at once',
    async (ctx) => {
      const { secret, name, projectId } = ctx.state.key;
      const { page } = await consolePage(ctx, 'keys');
      await page.waitForSelector('[data-testid="project-select"] select', { timeout: 20_000 });
      await page.select('[data-testid="project-select"] select', projectId);
      await waitForText(page, name, 20_000);
      const menuButton = await page.$(`button[aria-label="Actions for ${name}"]`);
      assert.ok(menuButton, `no actions menu for ${name}`);
      await menuButton.click();
      await page.waitForSelector('[role="menu"] [role="menuitem"]', { visible: true, timeout: 10_000 });
      assert.ok(await clickByText(page, '[role="menu"] [role="menuitem"]', /^revoke$/i), 'no Revoke item');
      await page.waitForSelector('[role="dialog"], [role="alertdialog"]', { visible: true, timeout: 10_000 });
      assert.ok(await clickByText(page, '[role="dialog"] button, [role="alertdialog"] button', /^revoke$/i), 'no confirming Revoke button');
      await waitForText(page, /revoked/i, 20_000);
      const refused = await bearer(ctx, secret, '/v1/models');
      assert.equal(refused.status, 401, `GET /v1/models with the revoked key → ${refused.status}: ${truncate(refused.text, 300)}`);
      ctx.note(`revoked key answer: ${truncate(refused.text, 160)}`);
    },
    { needs: ['console.key-create'] },
  );
}

module.exports = { register };
