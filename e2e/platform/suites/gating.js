'use strict';
/**
 * Who may open what, signed out: the documentation at every depth, and
 * nothing else. Plus the one surface that must not care about cookies at all.
 */

const assert = require('assert/strict');
const { SESSION_COOKIE, truncate } = require('../lib/http');
const { pageText } = require('../lib/browser');

// Used only when the /docs index itself cannot be read (it redirects, or the
// candidate renders links differently): the pages every release has had.
const FALLBACK_DOC_SLUGS = ['quickstart', 'authentication', 'models', 'responses', 'chat-completions', 'streaming', 'errors', 'rate-limits', 'python', 'curl', 'changelog'];

const GATED_PAGES = ['/', '/api', '/api?tab=keys', '/api?tab=playground', '/admin', '/admin/members', '/admin/invitations', '/admin/access'];

function isLoginRedirect(res) {
  if (res.status < 300 || res.status >= 400 || !res.location) return false;
  try {
    return new URL(res.location, 'http://x').pathname === '/login';
  } catch {
    return false;
  }
}

function slugsFromIndex(html) {
  const found = new Set();
  const re = /href="\/docs\/([a-z0-9][a-z0-9-]*)(?:[#?][^"]*)?"/g;
  let m;
  while ((m = re.exec(html))) found.add(m[1]);
  return [...found];
}

function register(t) {
  t.add(
    'gating',
    'gating.docs-public-every-depth',
    'Signed out, /docs and every page under it at any depth is served, never redirected to /login',
    async (ctx) => {
      const anon = await ctx.http(null);
      const index = await anon.get('/docs');
      let slugs = index.status === 200 ? slugsFromIndex(index.text) : [];
      if (slugs.length) ctx.note(`discovered ${slugs.length} doc pages from /docs`);
      else {
        slugs = FALLBACK_DOC_SLUGS;
        ctx.note(`/docs answered ${index.status}${index.location ? ` → ${index.location}` : ''}; using the fallback list of ${slugs.length} slugs`);
      }
      const paths = ['/docs', '/docs/', ...slugs.map((s) => `/docs/${s}`), `/docs/${slugs[0]}/deeper`, '/docs/a/b/c', '/docs/does-not-exist'];
      const problems = [];
      for (const p of paths) {
        const res = await anon.get(p);
        if (isLoginRedirect(res)) problems.push(`${p} → ${res.status} redirect to ${res.location}`);
        else if (res.status >= 300 && res.status < 400 && p !== '/docs/') problems.push(`${p} → ${res.status} redirect to ${res.location}`);
        else if (res.status >= 500) problems.push(`${p} → ${res.status}`);
        else if (slugs.includes(p.replace('/docs/', '')) && res.status !== 200) problems.push(`${p} → ${res.status}, expected 200 for a published page`);
      }
      assert.equal(problems.length, 0, `signed-out docs requests that were not served (${problems.length} of ${paths.length}):\n${problems.join('\n')}`);
    },
  );

  t.add(
    'gating',
    'gating.signed-out-redirects',
    'Signed out, the chat, the API console and every admin page redirect to /login',
    async (ctx) => {
      const anon = await ctx.http(null);
      const problems = [];
      for (const p of [...GATED_PAGES, '/docsx', '/docs-private']) {
        const res = await anon.get(p);
        if (!isLoginRedirect(res)) problems.push(`${p} → ${res.status}${res.location ? ` ${res.location}` : ''}`);
      }
      assert.equal(problems.length, 0, `pages that did not redirect to /login:\n${problems.join('\n')}`);
    },
  );

  t.add(
    'gating',
    'gating.v1-cookie-blind',
    'The public /v1 API answers 401 JSON without a key, with or without a session cookie, and never redirects',
    async (ctx) => {
      const anon = await ctx.http(null);
      const bare = await anon.get('/v1/models');
      assert.equal(bare.status, 401, `/v1/models without a key answered ${bare.status}: ${truncate(bare.text, 200)}`);
      assert.ok(bare.json && typeof bare.json === 'object', `/v1/models 401 body is not JSON: ${truncate(bare.text, 200)}`);
      const member = await ctx.http('member');
      const withCookie = await member.get('/v1/models');
      assert.equal(withCookie.status, 401, `/v1/models with only a session cookie answered ${withCookie.status}: ${truncate(withCookie.text, 200)}`);
      // request_id is per request by design; everything else must be identical.
      const strip = (r) => JSON.stringify({ ...r.json, error: { ...(r.json && r.json.error), request_id: undefined } });
      assert.equal(strip(withCookie), strip(bare), 'the /v1 answer differs when a session cookie is present');
      const root = await anon.get('/v1');
      assert.ok(!(root.status >= 300 && root.status < 400), `/v1 redirected (${root.status} → ${root.location})`);
      ctx.note(`/v1 → ${root.status}; /v1/models → 401 both ways`);
      assert.ok(member.cookies[SESSION_COOKIE]);
    },
  );

  t.add(
    'gating',
    'gating.docs-browser-signed-out',
    'A signed-out browser opens /docs and a docs page and reads them without being sent to /login',
    async (ctx) => {
      const { page } = await ctx.page();
      for (const p of ['/docs', '/docs/quickstart']) {
        await page.goto(`${ctx.cfg.base}${p}`, { waitUntil: 'networkidle2' });
        const at = new URL(page.url()).pathname;
        assert.equal(at, p, `opening ${p} signed out ended at ${page.url()}`);
        await page.waitForSelector('h1', { timeout: 15_000 });
        const text = await pageText(page);
        assert.ok(text.length > 200, `${p} rendered only ${text.length} characters of text`);
      }
    },
  );
}

module.exports = { register, slugsFromIndex, isLoginRedirect };
