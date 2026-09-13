'use strict';
/**
 * Sign-in, sign-out, and what the session APIs say to someone who has neither.
 */

const assert = require('assert/strict');
const { waitForPath, clickByText, sleep } = require('../lib/browser');
const { SESSION_COOKIE, truncate } = require('../lib/http');

async function signInThroughForm(page, base, email, password) {
  await page.goto(`${base}/login`, { waitUntil: 'domcontentloaded' });
  await page.waitForSelector('input[autocomplete="email"]');
  await page.type('input[autocomplete="email"]', email);
  await page.type('input[autocomplete="current-password"]', password);
  await page.click('button[type="submit"]');
}

function register(t) {
  t.add(
    'auth',
    'auth.anonymous-apis-401',
    'Signed-out calls to the session APIs answer 401 and carry no data',
    async (ctx) => {
      const anon = await ctx.http(null);
      for (const p of ['/api/auth/me', '/api/history/conversations', '/api/devplatform/projects', '/api/admin/members']) {
        const res = await anon.get(p);
        assert.equal(res.status, 401, `GET ${p} answered ${res.status}, expected 401; body: ${truncate(res.text, 200)}`);
      }
    },
  );

  t.add(
    'auth',
    'auth.login-form',
    'A member signs in through the login form, lands on the chat home and holds a session cookie',
    async (ctx) => {
      const password = ctx.cfg.member.password();
      if (!password) ctx.skip('no member password configured');
      const { page, context } = await ctx.page();
      await signInThroughForm(page, ctx.cfg.base, ctx.cfg.member.email, password);
      await waitForPath(page, '/', 45_000);
      await page.waitForSelector('textarea[aria-label="Message"]', { timeout: 45_000 });
      const cookies = await context.cookies(ctx.cfg.base);
      assert.ok(cookies.some((c) => c.name === SESSION_COOKIE && c.value), `no ${SESSION_COOKIE} cookie after sign-in`);
      const me = await page.evaluate(async () => {
        const r = await fetch('/api/auth/me', { cache: 'no-store' });
        return { status: r.status, body: await r.json().catch(() => null) };
      });
      assert.equal(me.status, 200, `/api/auth/me after sign-in answered ${me.status}`);
      assert.equal(me.body && me.body.user && me.body.user.email, ctx.cfg.member.email, 'signed in as someone else');
    },
  );

  t.add(
    'auth',
    'auth.login-wrong-password',
    'A wrong password keeps the person on /login with the incorrect-credentials alert and no session cookie',
    async (ctx) => {
      // WHY THE STATUS IS READ (2026-09-13): every failure here counts toward
      // the login lockout (orchestrator app/authn/api.py: keyed by email AND by
      // client address; AUTH_LOGIN_MAX_FAILS=8 in AUTH_LOGIN_WINDOW_SECONDS=900
      // locks for 300 s, and a success clears only the email key). A locked
      // stack answers 429 "Too many attempts", which used to pass this check
      // and then fail every later sign-in as if the product were broken.
      const { page, context } = await ctx.page();
      const answers = [];
      page.on('response', (res) => {
        if (res.request().method() === 'POST' && new URL(res.url()).pathname === '/api/auth/login') answers.push(res.status());
      });
      await signInThroughForm(page, ctx.cfg.base, ctx.cfg.member.email, `wrong-${Date.now()}`);
      await page.waitForSelector('[role="alert"]', { timeout: 20_000 });
      const alert = await page.$eval('[role="alert"]', (el) => el.innerText.trim());
      ctx.note(`login answered ${answers.join(', ') || '(no response seen)'}; alert text: ${alert}`);
      if (answers.includes(429) || /too many attempts/i.test(alert)) {
        throw new Error(
          `login throttled (429): "${alert}". The login lockout is active for this account or client address, so every sign-in on this stack fails for the next few minutes; wait for it to clear before rerunning, and do not rerun this check in a loop`,
        );
      }
      assert.equal(answers[0], 401, `the wrong-password sign-in answered ${answers[0]}, expected 401`);
      assert.match(alert, /incorrect email or password/i, `the alert is not the incorrect-credentials message: "${alert}"`);
      await sleep(500);
      assert.equal(new URL(page.url()).pathname, '/login', `navigated to ${page.url()} after a wrong password`);
      const cookies = await context.cookies(ctx.cfg.base);
      assert.ok(!cookies.some((c) => c.name === SESSION_COOKIE), 'a session cookie was set for a wrong password');
    },
  );

  t.add(
    'auth',
    'auth.logout',
    'Log out from the account menu revokes the session on the server and returns to /login',
    async (ctx) => {
      const password = ctx.cfg.member.password();
      if (!password) ctx.skip('no member password configured');
      const { page, context } = await ctx.page();
      await signInThroughForm(page, ctx.cfg.base, ctx.cfg.member.email, password);
      await waitForPath(page, '/', 45_000);
      await page.waitForSelector('textarea[aria-label="Message"]', { timeout: 45_000 });
      const before = (await context.cookies(ctx.cfg.base)).find((c) => c.name === SESSION_COOKIE);
      assert.ok(before, 'no session cookie before logout');

      await page.waitForSelector('button[aria-haspopup="menu"][title="Account"]', { timeout: 20_000 });
      await page.click('button[aria-haspopup="menu"][title="Account"]');
      await page.waitForSelector('[role="menu"][aria-label="Account"]', { timeout: 10_000 });
      const clicked = await clickByText(page, '[role="menu"][aria-label="Account"] [role="menuitem"], [role="menu"][aria-label="Account"] button', /^log out$/i);
      assert.ok(clicked, 'no "Log out" item in the account menu');
      await waitForPath(page, '/login', 30_000);

      // The OLD cookie must now be dead server-side, not merely deleted locally.
      const probe = await ctx.http(null);
      probe.cookies[SESSION_COOKIE] = before.value;
      const me = await probe.get('/api/auth/me');
      assert.equal(me.status, 401, `the logged-out session still answers /api/auth/me with ${me.status}`);
      ctx.note(`post-logout /api/auth/me: 401 ${truncate(me.text, 160)}`);
    },
  );
}

module.exports = { register, signInThroughForm };
