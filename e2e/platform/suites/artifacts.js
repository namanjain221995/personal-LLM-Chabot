'use strict';
/**
 * The artifacts panel, when this account has anything to open in it.
 *
 * The suite does not MAKE an artifact: that is a model generation, which the
 * run does not put on the engines. It opens one an earlier run left behind
 * (scripts/artifact_smoke2.py on the e2e stack) and skips, saying so, when
 * the account has none.
 */

const assert = require('assert/strict');
const { sleep } = require('../lib/browser');
const { truncate } = require('../lib/http');

async function findConversationWithArtifacts(client, note) {
  const list = await client.get('/api/history/conversations');
  assert.equal(list.status, 200, `GET /api/history/conversations → ${list.status}: ${truncate(list.text, 200)}`);
  const convs = list.json || [];
  // Prefer the artifact smoke runs by name, then anything else, newest first.
  const ordered = [...convs.filter((c) => /^smoke2-/.test(c.id)), ...convs.filter((c) => !/^smoke2-/.test(c.id))].slice(0, 15);
  for (const c of ordered) {
    const conv = await client.get(`/api/history/conversations/${encodeURIComponent(c.id)}`);
    if (conv.status === 200 && /"artifacts?"\s*:|"artifact_id"|"artifact_card"/.test(conv.text)) return c.id;
  }
  note(`looked at ${ordered.length} of ${convs.length} conversations`);
  return null;
}

function register(t) {
  t.add(
    'artifacts',
    'artifacts.panel',
    'A generated file card opens the artifacts panel with its title and a download',
    async (ctx) => {
      const client = await ctx.http('member');
      const id = await findConversationWithArtifacts(client, ctx.note);
      if (!id) ctx.skip('this account has no conversation with artifacts, so the panel is not reachable without a model generation');
      ctx.note(`conversation ${id}`);
      const { page } = await ctx.page({ role: 'member' });
      await page.goto(`${ctx.cfg.base}/?c=${encodeURIComponent(id)}`, { waitUntil: 'domcontentloaded' });
      await page.waitForSelector('[data-testid="file-card"]', { timeout: 60_000 });
      await sleep(1000);
      const opener = await page.evaluateHandle(() => {
        const cards = document.querySelectorAll('[data-testid="artifact-card"]');
        const last = cards[cards.length - 1] || document;
        return last.querySelector('[data-testid="file-card"] [role="button"], [data-testid="file-card"] button');
      });
      const el = opener.asElement();
      assert.ok(el, 'the file card has nothing to click');
      await el.evaluate((n) => n.scrollIntoView({ block: 'center' }));
      await el.click();
      const panel = await page.waitForSelector('[data-testid="artifact-panel"]', { visible: true, timeout: 30_000 });
      const title = await panel.evaluate((n) => (n.querySelector('h2, h3, [id$="title"]') || n).innerText.split('\n')[0].trim());
      assert.ok(title.length > 0, 'the panel has no title');
      const download = await page.$('[data-testid="artifact-panel-download"], [data-testid="artifact-panel-download-fallback"], [data-testid="artifact-download"]');
      assert.ok(download, 'the panel offers no download');
      ctx.note(`panel title: ${truncate(title, 80)}`);
    },
  );
}

module.exports = { register };
