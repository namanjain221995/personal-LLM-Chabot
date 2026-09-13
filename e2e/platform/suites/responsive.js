'use strict';
/**
 * Every page at phone, tablet and desktop width: no horizontal overflow — in
 * the document, in a pane that scrolls sideways, or cut off at the screen edge
 * by an overflow:hidden shell (see measureOverflow) — and nothing red in the
 * console. One check per page, one verdict per width.
 */

const fs = require('fs');
const path = require('path');

const { measureOverflow, formatOffenders, trackConsole, viewportFor, sleep } = require('../lib/browser');

// [id, path (or a function of run state), role, a selector that means "rendered"]
const PAGES = [
  ['login', '/login', null, 'input[autocomplete="email"]'],
  ['accept-invite', '/accept-invite', null, 'h1'],
  ['access-removed', '/access-removed', null, 'h1'],
  ['docs-index', '/docs', null, 'h1'],
  ['docs-page', '/docs/quickstart', null, 'h1'],
  ['chat-home', '/', 'member', 'textarea[aria-label="Message"]'],
  ['chat-conversation', 'conversation', 'member', 'textarea[aria-label="Message"]'],
  ['admin-members', '/admin/members', 'admin', 'h1'],
  ['admin-invitations', '/admin/invitations', 'admin', 'h1'],
  ['admin-access', '/admin/access', 'admin', 'h1'],
  ['api-overview', '/api', 'admin', 'h1'],
  ['api-keys', '/api?tab=keys', 'admin', 'h1'],
  ['api-models', '/api?tab=models', 'admin', 'h1'],
  ['api-playground', '/api?tab=playground', 'admin', 'h1'],
];

function register(t, cfg) {
  for (const [name, target, role, ready] of PAGES) {
    t.add(
      'responsive',
      `responsive.${name}`,
      `${typeof target === 'string' && target.startsWith('/') ? target : 'An open conversation'} has no horizontal overflow and no console errors at ${cfg.widths.join(', ')} px`,
      async (ctx) => {
        let pathname = target;
        const { page: first, context, client } = await ctx.page({ role, width: cfg.widths[0] });
        if (target === 'conversation') {
          const list = await client.get('/api/history/conversations');
          const conv = (list.json || [])[0];
          if (!conv) ctx.skip('the member account has no conversation to open');
          pathname = `/?c=${encodeURIComponent(conv.id)}`;
          ctx.note(`conversation ${conv.id}`);
        }
        await first.close();
        const problems = [];
        const clean = [];
        for (const width of cfg.widths) {
          const page = ctx.adopt(await context.newPage());
          const errors = [];
          // The same collector the self-test proves (tests/measure.test.js).
          trackConsole(page, errors);
          await page.setViewport(viewportFor(width));
          await page.goto(`${cfg.base}${pathname}`, { waitUntil: 'domcontentloaded', timeout: 60_000 });
          await page.waitForNetworkIdle({ idleTime: 800, timeout: 20_000 }).catch(() => {});
          const rendered = await page.waitForSelector(ready, { timeout: 30_000 }).then(() => true).catch(() => false);
          await sleep(800);
          const at = new URL(page.url());
          const wanted = new URL(`${cfg.base}${pathname}`);
          const found = [];
          if (at.pathname !== wanted.pathname) found.push(`ended at ${at.pathname}${at.search} instead of ${wanted.pathname}`);
          if (!rendered) found.push(`never rendered (${ready} absent after 30 s)`);
          const m = await measureOverflow(page, width);
          if (m.overflowPx > 0 || m.offenders.length) {
            found.push(
              `horizontal overflow ${m.overflowPx}px (document ${m.docOverflowPx}px: scrollWidth ${m.scrollWidth}, clientWidth ${m.clientWidth}, innerWidth ${m.innerWidth}; ${m.offenderCount} element(s) past the ${m.edge}px edge)` +
                (m.offenders.length ? `; ${formatOffenders(m)}` : ''),
            );
          }
          if (errors.length) found.push(`${errors.length} console error(s):\n        ${errors.join('\n        ')}`);
          if (found.length) {
            const dir = path.join(ctx.outDir, 'screenshots');
            fs.mkdirSync(dir, { recursive: true });
            const file = path.join(dir, `responsive.${name}-${width}.png`);
            await page.screenshot({ path: file }).catch(() => {});
            ctx.result.screenshots.push(path.relative(ctx.outDir, file));
            problems.push(`at ${width}px: ${found.join('\n      ')}`);
          } else {
            clean.push(width);
          }
          await page.close();
        }
        if (clean.length) ctx.note(`clean at ${clean.join(', ')} px`);
        if (problems.length) throw new Error(`${pathname}:\n  ${problems.join('\n  ')}`);
      },
      { timeoutMs: 300_000 },
    );
  }
}

module.exports = { register, PAGES };
