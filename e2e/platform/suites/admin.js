'use strict';
/**
 * Administration: the three people-and-access pages, the new Developer →
 * API platform link, and the refusal a member gets.
 */

const assert = require('assert/strict');
const { sleep, waitForPath, pageText } = require('../lib/browser');
const { truncate } = require('../lib/http');

async function adminPage(ctx, pathname, heading) {
  const { page, client } = await ctx.page({ role: 'admin' });
  await page.goto(`${ctx.cfg.base}${pathname}`, { waitUntil: 'domcontentloaded' });
  assert.equal(new URL(page.url()).pathname, pathname, `opening ${pathname} as an admin ended at ${page.url()}`);
  await page.waitForFunction((h) => [...document.querySelectorAll('h1')].some((n) => new RegExp(`^${h}$`, 'i').test(n.innerText.trim())), { timeout: 30_000 }, heading);
  // Give the page's data call time to land, then look for its error panel.
  await sleep(1500);
  const alerts = await page.$$eval('main [role="alert"], [role="alert"].border-danger\\/40', (els) => els.map((e) => e.innerText.trim()).filter(Boolean));
  assert.equal(alerts.length, 0, `${pathname} shows an error: ${alerts.join(' | ')}`);
  return { page, client };
}

function register(t, cfg) {
  t.add(
    'admin',
    'admin.members',
    'The Members page lists the workspace members, the member test account among them',
    async (ctx) => {
      const { page } = await adminPage(ctx, '/admin/members', 'Members');
      await page.waitForFunction((email) => document.body.innerText.includes(email), { timeout: 20_000 }, cfg.member.email).catch(() => {
        throw new Error(`${cfg.member.email} is not listed on /admin/members`);
      });
    },
  );

  t.add(
    'admin',
    'admin.invitations',
    'The Invitations page renders its list and the Invite member action',
    async (ctx) => {
      const { page } = await adminPage(ctx, '/admin/invitations', 'Invitations');
      const text = await pageText(page);
      assert.ok(/invite/i.test(text), `no invite action on /admin/invitations: ${truncate(text, 300)}`);
    },
  );

  t.add(
    'admin',
    'admin.access',
    'The Access page renders the per-tool access settings',
    async (ctx) => {
      const { page } = await adminPage(ctx, '/admin/access', 'Access');
      const switches = await page.$$('[role="switch"], input[type="checkbox"]');
      assert.ok(switches.length > 0, 'no access toggles on /admin/access');
      ctx.note(`${switches.length} toggles`);
    },
  );

  t.add(
    'admin',
    'admin.api-platform-link',
    'The admin rail shows Developer → API platform, and it opens the API console',
    async (ctx) => {
      const { page } = await adminPage(ctx, '/admin/members', 'Members');
      const link = await page.evaluateHandle(() =>
        [...document.querySelectorAll('a[href="/api"]')].find((a) => /api platform/i.test(a.innerText) && a.getBoundingClientRect().width > 0) || null,
      );
      const el = link.asElement();
      if (!el) {
        const rail = await page.$$eval('nav a', (as) => as.map((a) => `${a.innerText.trim()} → ${a.getAttribute('href')}`));
        throw new Error(`no visible "API platform" link to /api in the admin rail; rail links: ${rail.join(' | ')}`);
      }
      // The group heading is the nearest preceding text in the rail, e.g. "Developer".
      const group = await el.evaluate((a) => {
        for (let n = a.parentElement, i = 0; n && i < 5; n = n.parentElement, i += 1) {
          const first = (n.innerText || '').split('\n')[0].trim();
          if (first && !/api platform/i.test(first)) return first;
        }
        return '';
      });
      ctx.note(`"API platform" sits under "${group || '(no heading found)'}"`);
      assert.match(group, /developer/i, `the API platform link is not in a Developer group (found "${group}")`);
      await el.click();
      await waitForPath(page, '/api', 30_000);
      await page.waitForSelector('h1', { timeout: 30_000 });
    },
  );

  t.add(
    'admin',
    'admin.member-refused',
    'A member is kept out of administration: the pages send them away and the admin API refuses',
    async (ctx) => {
      const { page, client } = await ctx.page({ role: 'member' });
      await page.goto(`${ctx.cfg.base}/admin/members`, { waitUntil: 'domcontentloaded' });
      await page.waitForFunction(() => !location.pathname.startsWith('/admin') || /not found|404/i.test(document.body.innerText), { timeout: 30_000 }).catch(async () => {
        throw new Error(`a member stayed on ${page.url()} and it shows: ${truncate(await pageText(page), 300)}`);
      });
      ctx.note(`member ended at ${new URL(page.url()).pathname}`);
      for (const p of ['/api/admin/members', '/api/admin/invitations']) {
        const res = await client.get(p);
        assert.ok([403, 404].includes(res.status), `member GET ${p} → ${res.status}: ${truncate(res.text, 200)}`);
      }
      const consolePage = await client.get('/api');
      assert.ok(consolePage.status === 404 || (consolePage.status >= 300 && consolePage.status < 400), `member GET /api → ${consolePage.status}`);
    },
  );
}

module.exports = { register };
