'use strict';
/**
 * The chat itself: send and stream, then the conversation's life in the
 * sidebar — listed, renamed, shared, deleted — each checked in the browser AND
 * against the history API, because a sidebar that updates optimistically can
 * show a rename the server never stored.
 */

const assert = require('assert/strict');
const crypto = require('crypto');
const { clickByText, pageText, sleep, waitForText } = require('../lib/browser');
const { truncate } = require('../lib/http');
const { chatStubStream } = require('../lib/stubs');

const CHAT_PATH = '/api/chat';
const TITLE_PATH = /^\/api\/history\/conversations\/[^/]+\/title$/;

/**
 * Answer the engine-bound calls in the browser (stub mode). Everything else —
 * history sync, sharing, uploads — goes to the real stack untouched.
 */
async function stubEngineCalls(page, answer, seen) {
  await page.setRequestInterception(true);
  page.on('request', (req) => {
    if (req.isInterceptResolutionHandled()) return;
    const url = new URL(req.url());
    if (req.method() === 'POST' && url.pathname === CHAT_PATH) {
      const raw = req.postData() || '';
      let sent = {};
      try {
        sent = JSON.parse(raw);
      } catch {
        sent = {};
      }
      // The generation id is minted here, as the orchestrator mints it, and
      // the intent id is echoed from the request, as the orchestrator echoes it.
      const generationId = crypto.randomUUID();
      const intentId = typeof sent.intent_id === 'string' ? sent.intent_id : null;
      seen.push({ path: url.pathname, body: raw, generationId, intentId });
      req.respond({
        status: 200,
        headers: { 'content-type': 'text/event-stream; charset=utf-8', 'cache-control': 'no-store' },
        body: chatStubStream(answer, { generationId, intentId, sessionId: typeof sent.session_id === 'string' ? sent.session_id : null }),
      });
      return;
    }
    if (req.method() === 'POST' && TITLE_PATH.test(url.pathname)) {
      // Titling asks the router model for a name; the stub keeps the title.
      req.respond({ status: 200, contentType: 'application/json', body: JSON.stringify({ title: null, generated: false }) });
      return;
    }
    req.continue();
  });
}

async function pollConversation(client, id, predicate, timeoutMs = 45_000) {
  const deadline = Date.now() + timeoutMs;
  let last = null;
  while (Date.now() < deadline) {
    last = await client.get(`/api/history/conversations/${encodeURIComponent(id)}`);
    if (last.status === 200 && predicate(last.json)) return last.json;
    await sleep(1000);
  }
  throw new Error(
    `conversation ${id} never reached the expected state within ${timeoutMs / 1000} s; last answer ${last && last.status}: ${truncate(last && last.text, 600)}`,
  );
}

async function openConversation(ctx, id) {
  const { page, client } = await ctx.page({ role: 'member' });
  await page.goto(`${ctx.cfg.base}/?c=${encodeURIComponent(id)}`, { waitUntil: 'domcontentloaded' });
  await page.waitForSelector('textarea[aria-label="Message"]', { timeout: 45_000 });
  return { page, client };
}

/**
 * Activate one item of the "⋯" menu on the ACTIVE sidebar row.
 *
 * The menu closes on any scroll or resize anywhere in the window (it is
 * position:fixed and would otherwise float away from its row), and the
 * sidebar settles — loads, scrolls the active row into view — just after a
 * navigation. So the menu is (re)opened with a real click until the item can
 * be clicked, rather than assumed to still be open.
 */
async function rowMenuItem(page, label, { reopen = true } = {}) {
  await page.waitForSelector('[aria-label="Conversations"] [aria-current="true"]', { timeout: 30_000 });
  for (let attempt = 1; attempt <= 4; attempt += 1) {
    const menuOpen = await page.$('[role="menu"][aria-label^="Conversation options"]');
    if (!menuOpen && reopen) {
      const trigger = await page.evaluateHandle(() => {
        const active = document.querySelector('[aria-label="Conversations"] [aria-current="true"]');
        const li = active && active.closest('li');
        return li ? li.querySelector('button[aria-label^="Options for conversation"]') : null;
      });
      const el = trigger.asElement();
      assert.ok(el, 'the active conversation row has no options button');
      await el.click();
    }
    const menu = await page
      .waitForSelector('[role="menu"][aria-label^="Conversation options"]', { visible: true, timeout: 5_000 })
      .catch(() => null);
    if (menu) {
      const items = await menu.$$('[role="menuitem"]');
      for (const item of items) {
        const text = (await item.evaluate((n) => n.innerText.trim())) || '';
        if (label.test(text)) {
          await item.click();
          return true;
        }
      }
    }
    await sleep(750);
  }
  return false;
}

function register(t, cfg) {
  t.add(
    'chat',
    'chat.send-stream',
    `A member sends a message and the streamed answer renders and is stored in history (${cfg.chatMode} engine)`,
    async (ctx) => {
      const nonce = `e2e-${Date.now().toString(36)}`;
      const question = `Regression check ${nonce}: reply with the single word ok.`;
      const answer = `Stubbed answer for ${nonce}. The stream rendered.`;
      const { page, client } = await ctx.page({ role: 'member' });
      const seen = [];
      if (ctx.cfg.chatMode === 'stub') await stubEngineCalls(page, answer, seen);
      await page.goto(`${ctx.cfg.base}/`, { waitUntil: 'domcontentloaded' });
      await page.waitForSelector('textarea[aria-label="Message"]', { timeout: 45_000 });
      await page.type('textarea[aria-label="Message"]', question);
      await page.waitForSelector('button[aria-label="Send message"]:not([disabled])', { timeout: 10_000 });
      if (ctx.cfg.chatMode === 'live') {
        await page.evaluate(() => {
          window.__e2eSawStop = false;
          const seen = () => {
            if (document.querySelector('button[aria-label="Stop generating"]')) window.__e2eSawStop = true;
          };
          new MutationObserver(seen).observe(document.body, { childList: true, subtree: true, attributes: true });
        });
      }
      await page.click('button[aria-label="Send message"]');

      await page.waitForFunction(() => new URLSearchParams(location.search).has('c'), { timeout: 30_000 });
      const id = new URL(page.url()).searchParams.get('c');
      ctx.state.chat = { id, nonce, question, answer, title: null };
      ctx.addCleanup(`delete conversation ${id}`, async (httpFor) => {
        const c = await httpFor('member');
        const res = await c.delete(`/api/history/conversations/${encodeURIComponent(id)}`);
        // 404: chat.delete already removed it, which is the normal path.
        if (![200, 204, 404].includes(res.status)) throw new Error(`DELETE conversation → ${res.status}: ${truncate(res.text, 200)}`);
      });

      if (ctx.cfg.chatMode === 'stub') {
        await waitForText(page, answer, 30_000);
        assert.equal(seen.length, 1, `expected exactly one POST /api/chat, saw ${seen.length}`);
        const sent = JSON.parse(seen[0].body || '{}');
        assert.equal(sent.conversation_id, id, 'the chat request named a different conversation than the URL');
        assert.ok(
          (sent.messages || []).some((m) => m.role === 'user' && String(m.content).includes(nonce)),
          `the chat request did not carry the question: ${truncate(seen[0].body, 400)}`,
        );
        const { generationId, intentId } = seen[0];
        assert.ok(intentId, `the chat request carried no intent_id, so the stream handshake could not be exercised: ${truncate(seen[0].body, 400)}`);
        const stored = await pollConversation(client, id, (conv) => {
          const msgs = conv.messages || [];
          return msgs.some((m) => m.role === 'user' && m.content.includes(nonce)) && msgs.some((m) => m.role === 'assistant' && m.content.includes(answer));
        });
        const answers = stored.messages.filter((m) => m.role === 'assistant');
        assert.equal(
          answers.length,
          1,
          `expected exactly one stored assistant message, found ${answers.length}: ${truncate(JSON.stringify(answers.map((m) => ({ content: m.content, generation_id: m.meta && m.meta.generation_id }))), 600)}`,
        );
        const storedGen = answers[0].meta && answers[0].meta.generation_id;
        assert.equal(storedGen, generationId, `the stored answer is keyed by generation_id ${storedGen}, the stream named ${generationId}`);
        const question = stored.messages.find((m) => m.role === 'user' && m.content.includes(nonce));
        const intent = question.meta && question.meta.intent;
        assert.ok(intent && intent.id === intentId, `the stored question does not carry the send intent ${intentId}: ${truncate(JSON.stringify(question.meta), 400)}`);
        assert.equal(intent.generation_id, generationId, `the stored intent names generation ${intent.generation_id}, the stream named ${generationId}`);
        assert.equal(intent.state, 'completed', `the stored intent is "${intent.state}", expected "completed"`);

        // A reload reconciles the local copy with the server's by
        // generation_id: the answer must be on screen exactly once.
        await page.reload({ waitUntil: 'domcontentloaded' });
        await page.waitForSelector('textarea[aria-label="Message"]', { timeout: 45_000 });
        await waitForText(page, answer, 30_000);
        await sleep(2500); // let the history sync and reconcile finish
        const shown = await page.evaluate((a) => document.body.innerText.split(a).length - 1, answer);
        assert.equal(shown, 1, `after a reload the answer is on screen ${shown} times`);
        const again = await client.get(`/api/history/conversations/${encodeURIComponent(id)}`);
        const answersAfter = ((again.json && again.json.messages) || []).filter((m) => m.role === 'assistant');
        assert.equal(answersAfter.length, 1, `after a reload the server holds ${answersAfter.length} assistant messages`);
        ctx.note(`conversation ${id} stored with ${stored.messages.length} messages; answer keyed by generation ${generationId}, intent ${intentId} completed; one answer after reload`);
      } else {
        // Live: the engine may be healthy (tokens arrive) or unavailable, in
        // which case the page must say so rather than hang — both are passes
        // for THIS check; a silent spinner is not. "Answered" means a Stop
        // button was seen and is gone again (the observer was armed before
        // the send), not merely that text follows the nonce: the question's
        // own tail already did (review, 2026-09-13).
        const outcome = await page.waitForFunction(
          () => {
            if (document.querySelector('[data-testid="chat-error-status"]')) return 'error-page';
            const stop = document.querySelector('button[aria-label="Stop generating"]');
            if (window.__e2eSawStop && !stop) return 'stream-finished';
            return false;
          },
          { timeout: 170_000, polling: 500 },
        );
        ctx.note(`live outcome: ${await outcome.jsonValue()}`);
        const stored = await pollConversation(client, id, (conv) => (conv.messages || []).some((m) => m.role === 'assistant'), 60_000);
        const reply = stored.messages.filter((m) => m.role === 'assistant').pop();
        ctx.note(`assistant stored ${reply.content.length} chars: ${truncate(reply.content, 120)}`);
      }
    },
    { timeoutMs: 240_000 },
  );

  t.add(
    'chat',
    'chat.conversation-list',
    'The new conversation is listed in the sidebar and by the history API',
    async (ctx) => {
      const { id } = ctx.state.chat;
      const { page, client } = await openConversation(ctx, id);
      const list = await client.get('/api/history/conversations');
      assert.equal(list.status, 200, `GET /api/history/conversations → ${list.status}: ${truncate(list.text, 300)}`);
      const row = (list.json || []).find((c) => c.id === id);
      assert.ok(row, `conversation ${id} is not in the API list of ${(list.json || []).length}`);
      ctx.state.chat.title = row.title;
      await page.waitForSelector('[aria-label="Conversations"] [aria-current="true"]', { timeout: 30_000 });
      const sidebarTitle = await page.$eval('[aria-label="Conversations"] [aria-current="true"]', (el) => el.innerText.trim());
      assert.ok(sidebarTitle.length > 0, 'the active sidebar row has no title');
      ctx.note(`API title "${row.title}", sidebar "${sidebarTitle}"`);
    },
    { needs: ['chat.send-stream'] },
  );

  t.add(
    'chat',
    'chat.rename',
    'Renaming from the sidebar menu changes the title on screen and on the server',
    async (ctx) => {
      const { id, nonce } = ctx.state.chat;
      const { page, client } = await openConversation(ctx, id);
      await sleep(1500); // let the sidebar settle; see rowMenuItem
      assert.ok(await rowMenuItem(page, /^rename$/i), 'no clickable Rename item in the conversation menu');
      await page.waitForSelector('input[aria-label="Rename conversation"]', { timeout: 10_000 });
      const title = `Renamed ${nonce}`;
      await page.$eval('input[aria-label="Rename conversation"]', (el) => el.select());
      await page.keyboard.press('Backspace');
      await page.type('input[aria-label="Rename conversation"]', title);
      await page.keyboard.press('Enter');
      await waitForText(page, title, 15_000);
      const deadline = Date.now() + 30_000;
      let got = null;
      while (Date.now() < deadline) {
        const res = await client.get(`/api/history/conversations/${encodeURIComponent(id)}`);
        got = res.json && res.json.title;
        if (got === title) break;
        await sleep(1000);
      }
      assert.equal(got, title, `the server title is "${got}" after renaming to "${title}"`);
      ctx.state.chat.title = title;
    },
    { needs: ['chat.send-stream'] },
  );

  t.add(
    'chat',
    'chat.share-link',
    'A share link created from the Share dialog opens the conversation for a signed-out visitor',
    async (ctx) => {
      const { id, nonce } = ctx.state.chat;
      const { page, client } = await openConversation(ctx, id);
      await page.waitForSelector('button[aria-label="Share conversation"]', { timeout: 30_000 });
      await page.click('button[aria-label="Share conversation"]');
      await page.waitForSelector('[role="dialog"]', { timeout: 10_000 });
      // The Create button is disabled until the policy has loaded.
      await page.waitForFunction(
        () => [...document.querySelectorAll('[role="dialog"] button')].some((b) => /^create link$/i.test(b.innerText.trim()) && !b.disabled),
        { timeout: 20_000 },
      );
      const visibility = await page.evaluate(() => {
        const select = document.querySelector('[role="dialog"] select');
        return select ? select.value : null;
      });
      assert.ok(await clickByText(page, '[role="dialog"] button', /^create link$/i), 'no Create link button');
      await page.waitForFunction(() => {
        const input = document.querySelector('input[aria-label="Share link"]');
        return input && /\/share\/[^/]+$/.test(input.value);
      }, { timeout: 20_000 });
      const link = await page.$eval('input[aria-label="Share link"]', (el) => el.value);
      const token = new URL(link).pathname.split('/').pop();
      ctx.state.chat.shareToken = token;
      ctx.addCleanup(`revoke share link of ${id}`, async (httpFor) => {
        const c = await httpFor('member');
        const res = await c.delete(`/api/conversations/${encodeURIComponent(id)}/share`);
        if (![200, 204, 404].includes(res.status)) throw new Error(`DELETE share → ${res.status}: ${truncate(res.text, 200)}`);
        const anon = new (require('../lib/http').HttpClient)(ctx.cfg.base);
        const open = await anon.get(`/api/public/shares/${encodeURIComponent(token)}`);
        if (open.status === 200) throw new Error('the share link still opens signed out after the cleanup');
      });
      ctx.note(`link created with visibility "${visibility}" (token ${token.length} chars)`);

      const anon = await ctx.http(null);
      const snapshot = await anon.get(`/api/public/shares/${encodeURIComponent(token)}`);
      if (visibility === 'public') {
        assert.equal(snapshot.status, 200, `signed-out GET of the public share → ${snapshot.status}: ${truncate(snapshot.text, 300)}`);
        assert.ok(snapshot.text.includes(nonce), 'the public snapshot does not contain the conversation');
        const { page: visitor } = await ctx.page();
        await visitor.goto(`${ctx.cfg.base}/share/${encodeURIComponent(token)}`, { waitUntil: 'networkidle2' });
        assert.equal(new URL(visitor.url()).pathname, `/share/${token}`, `the share page redirected to ${visitor.url()}`);
        await waitForText(visitor, nonce, 20_000);
      } else {
        // A workspace link: signed-out must NOT see it; the owner must.
        assert.notEqual(snapshot.status, 200, 'a workspace-only share was readable signed out');
        const own = await client.get(`/api/public/shares/${encodeURIComponent(token)}`);
        assert.equal(own.status, 200, `the owner could not read the workspace share: ${own.status}`);
        ctx.note('public sharing is not allowed by policy here; checked the workspace link instead');
      }
      const status = await client.get(`/api/conversations/${encodeURIComponent(id)}/share`);
      assert.equal(status.status, 200, `GET share status → ${status.status}`);
      const text = await pageText(page);
      assert.ok(!/something went wrong/i.test(text), 'the Share dialog shows an error');
    },
    { needs: ['chat.send-stream'] },
  );

  t.add(
    'chat',
    'chat.delete',
    'Deleting from the sidebar menu removes the conversation from the list and the server, and its share link stops opening',
    async (ctx) => {
      const { id, title } = ctx.state.chat;
      const { page, client } = await openConversation(ctx, id);
      await sleep(1500); // let the sidebar settle; see rowMenuItem
      assert.ok(await rowMenuItem(page, /^delete$/i), 'no clickable Delete item in the conversation menu');
      // The menu turns into an inline "Delete this chat? Delete / Cancel".
      await page.waitForFunction(
        () => [...document.querySelectorAll('[role="menu"] [role="menuitem"]')].some((b) => /^cancel$/i.test(b.innerText.trim())),
        { timeout: 10_000 },
      );
      assert.ok(await rowMenuItem(page, /^delete$/i, { reopen: false }), 'no confirming Delete item');
      await waitForText(page, /Conversation deleted/i, 15_000).catch(() => ctx.note('no "Conversation deleted." toast seen'));
      const deadline = Date.now() + 20_000;
      let res;
      while (Date.now() < deadline) {
        res = await client.get(`/api/history/conversations/${encodeURIComponent(id)}`);
        if (res.status === 404) break;
        await sleep(1000);
      }
      assert.equal(res.status, 404, `GET of the deleted conversation → ${res.status}`);
      const list = await client.get('/api/history/conversations');
      assert.ok(!(list.json || []).some((c) => c.id === id), 'the deleted conversation is still listed by the API');
      const sidebar = await page.$$eval('[aria-label="Conversations"] li', (lis) => lis.map((li) => li.innerText.trim()));
      assert.ok(!title || !sidebar.includes(title), `the sidebar still lists "${title}"`);
      // A deleted conversation's public link must stop opening with it.
      if (ctx.state.chat.shareToken) {
        const anon = await ctx.http(null);
        const shared = await anon.get(`/api/public/shares/${encodeURIComponent(ctx.state.chat.shareToken)}`);
        assert.notEqual(shared.status, 200, 'the share link of the deleted conversation still opens signed out');
        ctx.note(`its share link now answers ${shared.status}`);
      }
    },
    { needs: ['chat.send-stream'] },
  );
}

module.exports = { register, stubEngineCalls };
