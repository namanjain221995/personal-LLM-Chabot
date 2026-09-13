'use strict';
/**
 * Uploads: a document attached in the composer, and the resumable chunked
 * rail surviving a dropped connection — once at the HTTP level (the server
 * must record NOTHING for a part cut short) and once in the browser (the
 * client must resume, not start again from byte 0).
 */

const assert = require('assert/strict');
const fs = require('fs');
const path = require('path');

const { sleep, waitForText } = require('../lib/browser');
const { SESSION_COOKIE, truncate } = require('../lib/http');
const { sha256, textBytes, writeTextFile, putWithDroppedConnection } = require('../lib/upload');
const { stubEngineCalls } = require('./chat');

const MiB = 1024 * 1024;

function cleanupConversation(ctx, id) {
  ctx.addCleanup(`delete conversation ${id} and its uploads`, async (httpFor) => {
    const c = await httpFor('member');
    const res = await c.delete(`/api/history/conversations/${encodeURIComponent(id)}`);
    if (![200, 204, 404].includes(res.status)) throw new Error(`DELETE conversation → ${res.status}: ${truncate(res.text, 200)}`);
  });
}

async function openComposer(ctx) {
  const { page, client } = await ctx.page({ role: 'member' });
  await page.goto(`${ctx.cfg.base}/`, { waitUntil: 'domcontentloaded' });
  await page.waitForSelector('textarea[aria-label="Message"]', { timeout: 45_000 });
  return { page, client };
}

function register(t) {
  t.add(
    'uploads',
    'uploads.document',
    'A text document attached to a first message uploads on send, reaches the chat request intact, and reads back byte for byte',
    async (ctx) => {
      const nonce = `e2e-doc-${Date.now().toString(36)}`;
      const fixture = path.join(ctx.outDir, 'fixtures', `${nonce}.txt`);
      fs.mkdirSync(path.dirname(fixture), { recursive: true });
      const bytes = textBytes(48 * 1024, nonce);
      fs.writeFileSync(fixture, bytes);
      try {
        const { page, client } = await openComposer(ctx);
        const chats = [];
        const answer = `Stubbed answer about ${nonce}.`;
        // A blank chat has no conversation yet, so the document uploads when
        // the message is sent; the chat call itself is stubbed (see lib/stubs).
        if (ctx.cfg.chatMode === 'stub') await stubEngineCalls(page, answer, chats);
        const uploads = [];
        page.on('response', async (res) => {
          const u = new URL(res.url());
          if (res.request().method() === 'POST' && u.pathname === '/api/upload') {
            uploads.push({ status: res.status(), body: await res.text().catch(() => '') });
          }
        });
        const input = await page.$('input[type="file"]');
        assert.ok(input, 'the composer has no file input');
        await input.uploadFile(fixture);
        await waitForText(page, path.basename(fixture), 15_000);
        await page.type('textarea[aria-label="Message"]', `Summarise ${nonce} in one line.`);
        await page.waitForSelector('button[aria-label="Send message"]:not([disabled])', { timeout: 10_000 });
        await page.click('button[aria-label="Send message"]');
        await page.waitForFunction(() => new URLSearchParams(location.search).has('c'), { timeout: 60_000 });
        const conv = new URL(page.url()).searchParams.get('c');
        cleanupConversation(ctx, conv);

        const deadline = Date.now() + 60_000;
        while (uploads.length === 0 && Date.now() < deadline) await sleep(500);
        assert.equal(uploads.length, 1, `expected one POST /api/upload, saw ${uploads.length}`);
        const [up] = uploads;
        assert.equal(up.status, 200, `POST /api/upload → ${up.status}: ${truncate(up.body, 300)}`);
        const uploadId = JSON.parse(up.body).upload_id;

        if (ctx.cfg.chatMode === 'stub') {
          await waitForText(page, answer, 60_000);
          const body = JSON.parse(chats[0].body || '{}');
          // Two legitimate shapes: a small document rides inline as base64
          // (`pdf`) while its stored copy is uploaded alongside; a large one is
          // referenced by upload id. Either way the bytes must be the file's.
          if (typeof body.pdf === 'string' && body.pdf) {
            const inline = Buffer.from(body.pdf, 'base64');
            assert.equal(sha256(inline), sha256(bytes), `the inline document in the chat request differs from the file (${inline.length} vs ${bytes.length} bytes)`);
            ctx.note('the chat request carried the document inline; the stored copy is checked below');
          } else {
            const refs = JSON.stringify(body.pdf_uploads || body);
            assert.ok(refs.includes(uploadId), `the chat request neither inlines the document nor references upload ${uploadId}: ${truncate(chats[0].body, 400)}`);
            ctx.note('the chat request referenced the upload by id');
          }
        }

        const list = await client.get(`/api/uploads/${encodeURIComponent(conv)}`);
        assert.equal(list.status, 200, `GET /api/uploads/<conversation> → ${list.status}: ${truncate(list.text, 300)}`);
        const row = (list.json.uploads || []).find((u) => u.id === uploadId || u.upload_id === uploadId);
        assert.ok(row, `upload ${uploadId} is not listed: ${truncate(list.text, 300)}`);
        const file = await fetch(`${ctx.cfg.base}/api/uploads/${encodeURIComponent(conv)}/${uploadId}/file`, {
          headers: { cookie: `${SESSION_COOKIE}=${client.cookies[SESSION_COOKIE]}` },
        });
        assert.equal(file.status, 200, `downloading the stored document → ${file.status}`);
        const back = Buffer.from(await file.arrayBuffer());
        assert.equal(sha256(back), sha256(bytes), `the stored bytes differ (${back.length} vs ${bytes.length} bytes)`);
      } finally {
        fs.rmSync(fixture, { force: true });
      }
    },
  );

  t.add(
    'uploads',
    'uploads.chunked-dropped-connection-http',
    'A chunked part whose connection drops mid-body is not recorded, the session reports only whole parts, and resending completes an identical file',
    async (ctx) => {
      const client = await ctx.http('member');
      const conv = `e2e-chunk-${Date.now().toString(36)}`;
      const created = await client.post('/api/history/conversations', { json: { id: conv, title: 'e2e chunked upload' } });
      assert.equal(created.status, 200, `creating the conversation → ${created.status}: ${truncate(created.text, 300)}`);
      cleanupConversation(ctx, conv);

      const partSize = 256 * 1024;
      const size = partSize * 2 + 100 * 1024;
      const file = textBytes(size, conv);
      const parts = [file.subarray(0, partSize), file.subarray(partSize, 2 * partSize), file.subarray(2 * partSize)];

      const form = new FormData();
      form.append('conversation_id', conv);
      form.append('filename', `${conv}.txt`);
      form.append('purpose', 'document');
      form.append('size', String(size));
      form.append('parts', '3');
      form.append('part_size', String(partSize));
      const init = await client.post('/api/upload/chunked/init', { body: form });
      assert.equal(init.status, 200, `chunked init → ${init.status}: ${truncate(init.text, 300)}`);
      const uploadId = init.json.upload_id;
      const base = `/api/upload/chunked/${encodeURIComponent(conv)}/${uploadId}`;
      const put = (i) =>
        client.put(`${base}/part/${i}`, { body: parts[i], headers: { 'content-type': 'application/octet-stream', 'x-part-sha256': sha256(parts[i]) } });

      const p0 = await put(0);
      assert.equal(p0.status, 200, `part 0 → ${p0.status}: ${truncate(p0.text, 300)}`);

      const dropped = await putWithDroppedConnection(
        `${ctx.cfg.base}${base}/part/1`,
        {
          cookie: `${SESSION_COOKIE}=${client.cookies[SESSION_COOKIE]}`,
          origin: ctx.cfg.base,
          'content-type': 'application/octet-stream',
          'x-part-sha256': sha256(parts[1]),
        },
        parts[1],
        Math.floor(parts[1].length / 2),
      );
      ctx.note(`dropped PUT of part 1 with its digest: ${JSON.stringify(dropped)}`);
      // And once WITHOUT X-Part-SHA256: with a digest, a truncated body is
      // refused on hash mismatch even if the server mishandled the cut
      // connection itself, so only this one tests the cut (review, 2026-09-13).
      const droppedBare = await putWithDroppedConnection(
        `${ctx.cfg.base}${base}/part/1`,
        {
          cookie: `${SESSION_COOKIE}=${client.cookies[SESSION_COOKIE]}`,
          origin: ctx.cfg.base,
          'content-type': 'application/octet-stream',
        },
        parts[1],
        Math.floor(parts[1].length / 2),
      );
      ctx.note(`dropped PUT of part 1 without a digest: ${JSON.stringify(droppedBare)}`);

      // Give the proxy and the orchestrator time to notice, then ask twice:
      // a part cut short must never appear, not even late — neither cut.
      for (const wait of [1500, 3000]) {
        await sleep(wait);
        const st = await client.get(base);
        assert.equal(st.status, 200, `session status → ${st.status}: ${truncate(st.text, 300)}`);
        assert.deepEqual(st.json.accepted_parts, [0], `after the dropped part the session reports ${JSON.stringify(st.json.accepted_parts)}: ${truncate(st.text, 300)}`);
        assert.equal(st.json.bytes_received, partSize, `bytes_received is ${st.json.bytes_received}, expected ${partSize}`);
      }

      const early = await client.post(`${base}/complete`);
      assert.equal(early.status, 409, `complete with parts missing → ${early.status}: ${truncate(early.text, 300)}`);
      assert.deepEqual(early.json.missing_parts, [1, 2], `missing_parts: ${truncate(early.text, 300)}`);

      for (const i of [1, 2]) {
        const r = await put(i);
        assert.equal(r.status, 200, `resending part ${i} → ${r.status}: ${truncate(r.text, 300)}`);
      }
      const done = await client.post(`${base}/complete`);
      assert.equal(done.status, 200, `complete → ${done.status}: ${truncate(done.text, 300)}`);
      assert.equal(done.json.bytes, size, `assembled ${done.json.bytes} bytes, expected ${size}`);
      const again = await client.post(`${base}/complete`);
      assert.equal(again.status, 200, `a repeated complete → ${again.status} (it must be idempotent)`);

      const back = await fetch(`${ctx.cfg.base}/api/uploads/${encodeURIComponent(conv)}/${done.json.upload_id}/file`, {
        headers: { cookie: `${SESSION_COOKIE}=${client.cookies[SESSION_COOKIE]}` },
      });
      assert.equal(back.status, 200, `downloading the assembled file → ${back.status}`);
      const bytes = Buffer.from(await back.arrayBuffer());
      assert.equal(sha256(bytes), sha256(file), `the assembled file differs from the original (${bytes.length} vs ${file.length} bytes)`);
    },
  );

  t.add(
    'uploads',
    'uploads.chunked-resume-browser',
    'In the browser, a 92 MiB document whose second part keeps losing its connection resumes from the server\'s part list without resending the first part',
    async (ctx) => {
      const nonce = `e2e-big-${Date.now().toString(36)}`;
      const fixture = path.join(ctx.outDir, 'fixtures', `${nonce}.txt`);
      fs.mkdirSync(path.dirname(fixture), { recursive: true });
      const size = 92 * MiB; // above the 90 MiB chunking threshold: 64 MiB + 28 MiB
      writeTextFile(fixture, size, nonce);
      try {
        // An OPEN conversation uploads on attach (a blank chat waits for the
        // send), so the conversation is made first and opened by id.
        const member = await ctx.http('member');
        const conv0 = `e2e-big-${Date.now().toString(36)}`;
        const made = await member.post('/api/history/conversations', { json: { id: conv0, title: 'e2e resumable upload' } });
        assert.equal(made.status, 200, `creating the conversation → ${made.status}: ${truncate(made.text, 300)}`);
        cleanupConversation(ctx, conv0);
        const { page, client } = await ctx.page({ role: 'member' });
        await page.goto(`${ctx.cfg.base}/?c=${conv0}`, { waitUntil: 'domcontentloaded' });
        await page.waitForSelector('textarea[aria-label="Message"]', { timeout: 45_000 });
        const log = [];
        let aborted = 0;
        let statusChecks = 0;
        let conv = null;
        await page.setRequestInterception(true);
        page.on('request', (req) => {
          if (req.isInterceptResolutionHandled()) return;
          const u = new URL(req.url());
          const m = /^\/api\/upload\/chunked\/([^/]+)\/([^/]+)(?:\/(part)\/(\d+)|\/(complete))?$/.exec(u.pathname);
          if (!m) return req.continue();
          conv = decodeURIComponent(m[1]);
          if (m[3] === 'part') {
            const index = Number(m[4]);
            // Keep cutting part 1 until the client has gone back to the
            // server to ask what it holds — that request IS the resume.
            if (index === 1 && statusChecks === 0 && aborted < 12) {
              aborted += 1;
              log.push(`PUT part 1 → aborted (${aborted})`);
              return req.abort('connectionreset');
            }
            log.push(`PUT part ${index}`);
          } else if (m[5]) {
            log.push('POST complete');
          } else if (req.method() === 'GET') {
            statusChecks += 1;
            log.push('GET session status');
          }
          return req.continue();
        });
        const responses = [];
        page.on('response', (res) => {
          const u = new URL(res.url());
          if (u.pathname.startsWith('/api/upload/chunked/')) responses.push(`${res.request().method()} ${u.pathname.replace(/\/[0-9a-f]{32}/, '/<upload>')} → ${res.status()}`);
        });

        // The composer re-mounts once the opened conversation has loaded; an
        // input handle taken before that is detached and silently ignores the
        // file. Wait for the load, then take the handle.
        await page.waitForFunction(() => !document.querySelector('[data-testid="conversation-loading"]'), { timeout: 30_000 });
        await sleep(1500);
        const input = await page.$('input[type="file"]');
        assert.ok(input, 'the composer has no file input');
        await input.uploadFile(fixture);
        await waitForText(page, path.basename(fixture), 20_000).catch(async () => {
          throw new Error(`the ${size}-byte file never appeared as a chip in the composer; page text: ${truncate(await page.evaluate(() => document.body.innerText), 400)}`);
        });
        await page.waitForFunction(() => /\bUploaded\b/.test(document.body.innerText), { timeout: 150_000 }).catch(async (err) => {
          const text = await page.evaluate(() => document.body.innerText);
          const at = text.indexOf('e2e-big-');
          throw new Error(
            `the chip never said "Uploaded" (${err.message}); requests: ${log.join(' | ')}; responses: ${responses.join(' | ')}; chip: ${truncate(text.slice(Math.max(0, at), at + 300), 300)}`,
          );
        });
        ctx.note(`request log: ${log.join(' | ')}`);
        assert.equal(conv, conv0, `the upload went to conversation ${conv}, not the open one`);
        const part0 = log.filter((l) => l === 'PUT part 0').length;
        assert.equal(part0, 1, `part 0 was sent ${part0} times; a resume must not resend a part the server holds. Log: ${log.join(' | ')}`);
        assert.ok(aborted >= 1, 'no attempt of part 1 was cut');
        assert.ok(statusChecks >= 1, `the client never asked the server for its part list. Log: ${log.join(' | ')}`);
        const afterStatus = log.slice(log.indexOf('GET session status'));
        assert.ok(afterStatus.includes('PUT part 1'), `part 1 was not resent after the status check. Log: ${log.join(' | ')}`);
        assert.ok(responses.some((r) => /\/complete → 200$/.test(r)), `complete did not answer 200: ${responses.join(' | ')}`);

        const list = await client.get(`/api/uploads/${encodeURIComponent(conv)}`);
        assert.equal(list.status, 200, `GET /api/uploads/<conversation> → ${list.status}: ${truncate(list.text, 300)}`);
        const row = (list.json.uploads || []).find((u) => Number(u.bytes ?? u.size) === size);
        assert.ok(row, `no ${size}-byte upload is listed for the conversation: ${truncate(list.text, 400)}`);
      } finally {
        fs.rmSync(fixture, { force: true });
      }
    },
    { timeoutMs: 240_000 },
  );
}

module.exports = { register };
