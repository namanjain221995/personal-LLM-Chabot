'use strict';
/**
 * The buffered body: the stream flag read as bytes arrive, the spool budget,
 * and the spool descriptor's lifetime (2026-09-13, review fixes).
 */

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { spawnSync } = require('node:child_process');

const B = require('../lib/bodies.cjs');
const { SpoolBudget, MemoryBudget } = require('../lib/guards.cjs');
const h = require('../testkit/harness.cjs');

/* ------------------------------------------------------ the stream flag -- */

function scan(text, splits = []) {
  const bytes = Buffer.from(text, 'utf8');
  const scanner = new B.StreamFlagScanner();
  let from = 0;
  for (const at of [...splits, bytes.length]) {
    scanner.push(bytes.subarray(from, at));
    from = at;
  }
  return scanner.summary();
}

/** What inferShape needs, computed the old way. */
function reference(text) {
  let value;
  try {
    value = JSON.parse(text);
  } catch {
    return 'invalid';
  }
  return value && typeof value === 'object' && !Array.isArray(value) ? { stream: value.stream === true } : null;
}

function mulberry32(seed) {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

/** A random JSON text: random whitespace, random \u escapes, "stream" everywhere. */
function randomJson(rand) {
  const pick = (xs) => xs[Math.floor(rand() * xs.length)];
  const ws = () => pick(['', '', '', ' ', '\n', '\t', '\r\n  ']);
  const str = (s) => {
    let out = '"';
    for (const ch of s) {
      const code = ch.codePointAt(0);
      if (ch === '"') out += '\\"';
      else if (ch === '\\') out += '\\\\';
      else if (code < 0x20) out += `\\u${code.toString(16).padStart(4, '0')}`;
      else if (code < 0x10000 && rand() < 0.15) out += `\\u${code.toString(16).padStart(4, '0')}`;
      else out += ch;
    }
    return `${out}"`;
  };
  const words = ['stream', 'stream', 'Stream', 'streams', 'model', 'input', 'true', '{"stream":true}', 'a"b', 'back\\slash', 'é😀', ''];
  const value = (depth) => {
    const r = rand();
    if (depth > 3 || r < 0.35) {
      return pick(['true', 'false', 'null', '0', '-1.5e3', '42', str(pick(words))]);
    }
    if (r < 0.6) {
      const n = Math.floor(rand() * 4);
      return `[${ws()}${Array.from({ length: n }, () => value(depth + 1)).join(`${ws()},${ws()}`)}${ws()}]`;
    }
    return object(depth + 1);
  };
  const object = (depth) => {
    const n = Math.floor(rand() * 5);
    const member = () => {
      // A third of members are "stream" with a literal, so the flag is often really true.
      const key = rand() < 0.33 ? 'stream' : pick(words);
      const v = key === 'stream' && rand() < 0.6 ? pick(['true', 'true', 'false', 'null', '1']) : value(depth);
      return `${ws()}${str(key)}${ws()}:${ws()}${v}${ws()}`;
    };
    const members = Array.from({ length: n }, member);
    return `{${members.join(',')}${n === 0 ? ws() : ''}}`;
  };
  const top = rand() < 0.9 ? object(0) : value(0);
  return `${ws()}${top}${ws()}`;
}

test.describe('the stream flag', () => {
  test('it agrees with JSON.parse on 1,000 random bodies, whole, byte by byte and split at random', () => {
    const rand = mulberry32(20260913);
    let compared = 0;
    let streamTrue = 0;
    for (let n = 0; n < 1000; n += 1) {
      const text = randomJson(rand);
      const want = reference(text);
      if (want === 'invalid') continue;
      const bytes = Buffer.byteLength(text);
      assert.deepEqual(scan(text), want, text);
      assert.deepEqual(scan(text, Array.from({ length: bytes - 1 }, (_, i) => i + 1)), want, `byte by byte: ${text}`);
      const cuts = Array.from({ length: 3 }, () => Math.floor(rand() * bytes)).sort((a, b) => a - b);
      assert.deepEqual(scan(text, cuts), want, `split at ${cuts}: ${text}`);
      compared += 1;
      if (want && want.stream) streamTrue += 1;
    }
    assert.ok(compared >= 900, `${compared} valid bodies compared`);
    assert.ok(streamTrue >= 80, `${streamTrue} of them asked for a stream`);
  });

  for (const [label, text, want] of [
    ['a top-level stream: true', '{"model":"m","stream":true}', { stream: true }],
    ['the last duplicate wins, as in JSON.parse', '{"stream":true,"stream":false}', { stream: false }],
    ['the last duplicate wins the other way too', '{"stream":false , "stream" : true }', { stream: true }],
    ['a nested stream key is not the request’s', '{"input":{"stream":true},"tools":[{"stream":true}]}', { stream: false }],
    ['the string "true" is not true', '{"stream":"true"}', { stream: false }],
    ['an escaped key is compared unescaped', '{"str\\u0065am":true}', { stream: true }],
    ['a key that only contains stream is not it', '{"streams":true,"stream_options":{"include_usage":true}}', { stream: false }],
    ['a quoted "stream": true inside a message is text', '{"messages":[{"content":"\\"stream\\": true"}],"stream":false}', { stream: false }],
    ['whitespace around every token', ' \r\n{ "stream"\t:\n true\r\n}\n ', { stream: true }],
    ['a key far longer than any spelling of stream', `{"${'s'.repeat(5000)}":true}`, { stream: false }],
    ['a literal longer than true', '{"stream":truetruetruetruetrue}', 'invalid'],
  ]) {
    test(`${label}`, () => {
      assert.deepEqual(reference(text), want);
      if (want !== 'invalid') assert.deepEqual(scan(text), want);
    });
  }

  test('a body that is not one complete JSON object has no shape, as before', () => {
    for (const text of ['[{"stream":true}]', '"stream"', '\ufeff{"stream":true}', '{"stream":true', '{"stream":true}x', '', '   ']) {
      assert.equal(scan(text), null, JSON.stringify(text));
      assert.ok(reference(text) === null || reference(text) === 'invalid', JSON.stringify(text));
    }
  });

  test('a 20 MiB image body is scanned without keeping it', () => {
    const scanner = new B.StreamFlagScanner();
    scanner.push(Buffer.from('{"model":"m","input":[{"type":"input_image","image_url":"data:image/png;base64,'));
    const block = Buffer.alloc(1024 * 1024, 0x41);
    const started = process.hrtime.bigint();
    for (let i = 0; i < 20; i += 1) scanner.push(block);
    scanner.push(Buffer.from('"}],"stream":true}'));
    const ms = Number(process.hrtime.bigint() - started) / 1e6;
    assert.deepEqual(scanner.summary(), { stream: true });
    assert.equal(scanner.key.length, 0);
    assert.ok(ms < 500, `${ms.toFixed(1)} ms for 20 MiB`);
  });
});

/* ------------------------------------------------------ the spool budget -- */

test.describe('the spool budget', () => {
  test('a reservation past the total quota is refused, and a release makes room again', () => {
    const budget = new SpoolBudget({ dir: '/x', maxBytes: 100, minFreeBytes: 0, statfs: () => ({ bavail: 1e6, bsize: 4096 }) });
    assert.deepEqual(budget.reserve(60), { ok: true });
    assert.deepEqual(budget.reserve(41), { ok: false, reason: 'quota' });
    assert.deepEqual(budget.reserve(40), { ok: true });
    budget.release(60);
    assert.equal(budget.reserved, 40);
    assert.deepEqual(budget.reserve(60), { ok: true });
  });

  test('a reservation that would take free space under the floor is refused, counting reservations since the last statfs sample', () => {
    let t = 0;
    let calls = 0;
    const budget = new SpoolBudget({
      dir: '/x',
      maxBytes: 1e12,
      minFreeBytes: 1000,
      now: () => t,
      statfs: () => {
        calls += 1;
        return { bavail: 3, bsize: 1000 }; // 3,000 bytes free
      },
    });
    assert.deepEqual(budget.reserve(1500), { ok: true });
    // Same second: the sample still says 3,000 free, but 1,500 are promised.
    assert.deepEqual(budget.reserve(600), { ok: false, reason: 'disk' });
    assert.deepEqual(budget.reserve(500), { ok: true });
    assert.equal(calls, 1, 'sampled once a second');
    t = 1000;
    assert.deepEqual(budget.reserve(2001), { ok: false, reason: 'disk' });
    assert.equal(calls, 2);
  });

  test('an unreadable filesystem leaves the quota as the only limit', () => {
    const budget = new SpoolBudget({ dir: '/x', maxBytes: 10, minFreeBytes: 1e15, statfs: () => { throw new Error('ENOENT'); } });
    assert.deepEqual(budget.reserve(10), { ok: true });
    assert.deepEqual(budget.reserve(1), { ok: false, reason: 'quota' });
  });

  test('the settings default to a 2 GiB quota and the orchestrator’s 20 GiB floor, and a floor of 0 is honoured', () => {
    const { readSettings } = require('../lib/settings.cjs');
    const d = readSettings({});
    assert.equal(d.spoolMaxBytes, 2 * 1024 ** 3);
    assert.equal(d.spoolMinFreeBytes, 21_474_836_480);
    assert.equal(readSettings({ PUBLIC_API_MIN_FREE_DISK_BYTES: '0' }).spoolMinFreeBytes, 0);
    assert.equal(readSettings({ PUBLIC_API_MIN_FREE_DISK_BYTES: 'nonsense' }).spoolMinFreeBytes, 21_474_836_480);
    assert.equal(readSettings({ V1_GATEWAY_SPOOL_MAX_BYTES: '4096' }).spoolMaxBytes, 4096);
  });
});

/* ----------------------------------------------------- the memory budget -- */

/** A request stand-in the tests drive chunk by chunk. */
function fakeRequest() {
  const { EventEmitter } = require('node:events');
  const req = new EventEmitter();
  req.complete = false;
  req.pause = () => undefined;
  req.resume = () => undefined;
  req.send = (...chunks) => {
    for (const chunk of chunks) req.emit('data', Buffer.from(chunk));
  };
  req.finish = () => {
    req.complete = true;
    req.emit('end');
  };
  return req;
}

function readAll(stream) {
  return new Promise((resolve, reject) => {
    const parts = [];
    stream.on('data', (c) => parts.push(Buffer.from(c)));
    stream.on('end', () => resolve(Buffer.concat(parts).toString('utf8')));
    stream.on('error', reject);
  });
}

test.describe('the memory budget', () => {
  // Review finding 2026-09-14 (scratchpad memflood.cjs): 400 waiting 1 MiB
  // bodies held +914 MiB of RSS, because only spooled bytes had a budget.
  test('a reservation past the total is refused whole, and a release makes room again', () => {
    const budget = new MemoryBudget({ maxBytes: 100 });
    assert.equal(budget.reserve(60), true);
    assert.equal(budget.reserve(41), false);
    assert.equal(budget.used, 60, 'a refused reservation holds nothing');
    assert.equal(budget.reserve(40), true);
    budget.release(60);
    assert.equal(budget.used, 40);
    budget.release(1000);
    assert.equal(budget.used, 0, 'never below zero');
  });

  test('bodies stay in memory while the budget lasts, the next one spills at once, and every byte is still sent', async () => {
    const dir = h.tmpdir('gw-membudget-');
    const memory = new MemoryBudget({ maxBytes: 1000 });
    const spool = new SpoolBudget({ dir, maxBytes: 1 << 20, minFreeBytes: 0 });
    const opts = { cap: 1 << 20, memoryBytes: 1 << 20, spoolDir: dir, idle: null, budget: spool, memory };

    const reqA = fakeRequest();
    const pA = B.readBufferedBody(reqA, opts);
    reqA.send('a'.repeat(600));
    reqA.finish();
    const a = (await pA).body;
    assert.equal(a.spilled, false);
    assert.equal(memory.used, 600);

    const reqB = fakeRequest();
    const pB = B.readBufferedBody(reqB, opts);
    reqB.send('b'.repeat(300));
    assert.equal(memory.used, 900);
    reqB.send('B'.repeat(300)); // 1,200 > 1,000: B goes to disk, with the 300 it had in memory
    reqB.finish();
    const b = (await pB).body;
    assert.equal(b.spilled, true, 'the body that could not get memory spilled');
    assert.equal(memory.used, 600, 'the spilled body gave its memory back');
    assert.equal(spool.reserved, 600, 'and was charged to the spool instead');
    assert.equal(fs.readFileSync(b.file, 'utf8'), 'b'.repeat(300) + 'B'.repeat(300));

    // The same bytes on every send, from memory and from the file.
    assert.equal(await readAll(a.stream()), 'a'.repeat(600));
    assert.equal(await readAll(a.stream()), 'a'.repeat(600), 'a second send of an in-memory body');
    assert.equal(await readAll(b.stream()), 'b'.repeat(300) + 'B'.repeat(300));

    a.dispose();
    b.dispose();
    assert.equal(memory.used, 0, 'released when the relay ends');
    assert.equal(spool.reserved, 0);
  });

  test('a body neither the memory budget nor the spool can hold is refused, holding nothing', async () => {
    const dir = h.tmpdir('gw-membudget-full-');
    const memory = new MemoryBudget({ maxBytes: 100 });
    const spool = new SpoolBudget({ dir, maxBytes: 150, minFreeBytes: 0 });
    const req = fakeRequest();
    const p = B.readBufferedBody(req, { cap: 1 << 20, memoryBytes: 1 << 20, spoolDir: dir, idle: null, budget: spool, memory });
    req.send('x'.repeat(80), 'y'.repeat(80));
    assert.deepEqual(await p, { spoolRefused: 'quota' });
    await h.sleep(20);
    assert.equal(memory.used, 0, 'the refused body let go of its memory');
    assert.equal(spool.reserved, 0);
    assert.deepEqual(fs.readdirSync(dir), []);
  });

  test('a body that gives up mid-way gives its memory back', async () => {
    const memory = new MemoryBudget({ maxBytes: 1000 });
    const req = fakeRequest();
    const p = B.readBufferedBody(req, { cap: 1 << 20, memoryBytes: 1 << 20, spoolDir: h.tmpdir('gw-membudget-gone-'), idle: null, memory });
    req.send('z'.repeat(500));
    assert.equal(memory.used, 500);
    req.emit('close');
    assert.deepEqual(await p, { gone: true });
    await h.sleep(20);
    assert.equal(memory.used, 0);
  });

  test('the budget defaults to 256 MiB and follows V1_GATEWAY_MEMORY_BUDGET_BYTES', () => {
    const { readSettings } = require('../lib/settings.cjs');
    assert.equal(readSettings({}).memoryBudgetBytes, 256 * 1024 * 1024);
    assert.equal(readSettings({ V1_GATEWAY_MEMORY_BUDGET_BYTES: '8388608' }).memoryBudgetBytes, 8388608);
    assert.equal(readSettings({ V1_GATEWAY_MEMORY_BUDGET_BYTES: '0' }).memoryBudgetBytes, 256 * 1024 * 1024, 'blank, zero or nonsense is the default');
  });
});

/* -------------------------------------------- the spool descriptor -- */

test('a spool write still queued when its client leaves never lands in the next request’s file', { skip: process.platform !== 'linux' }, () => {
  // The review's recipe (scratchpad p5_fdrace.cjs), in a child so the
  // threadpool can be ONE thread: a pbkdf2 holds it, client A's second chunk
  // queues an fs.write behind it, A disconnects, and client B's spool file is
  // opened at once and gets the lowest free descriptor number.
  const dir = h.tmpdir('gw-fdrace-');
  const script = `
    const { EventEmitter } = require('node:events');
    const crypto = require('node:crypto');
    const fs = require('node:fs');
    const path = require('node:path');
    const B = require(${JSON.stringify(path.join(h.GATEWAY_DIR, 'lib', 'bodies.cjs'))});
    const dir = ${JSON.stringify(dir)};
    const req = new EventEmitter();
    req.complete = false; req.pause = () => {}; req.resume = () => {};
    const p = B.readBufferedBody(req, { cap: 1 << 26, memoryBytes: 1024, spoolDir: dir, idle: null });
    req.emit('data', Buffer.from('X'.repeat(2048)));
    crypto.pbkdf2('k', 's', 400000, 32, 'sha256', () => {});
    req.emit('data', Buffer.from('CLIENT-A-SECRET-PROMPT'));
    setImmediate(async () => {
      req.emit('close');
      const victim = path.join(dir, 'client-B');
      const fdB = fs.openSync(victim, 'wx', 0o600);
      fs.writeSync(fdB, 'client B bytes|');
      const a = await p;
      setTimeout(() => {
        fs.closeSync(fdB);
        process.stdout.write(JSON.stringify({ a: Object.keys(a), victim: fs.readFileSync(victim, 'utf8'), left: fs.readdirSync(dir).sort() }));
      }, 2500);
    });
  `;
  const run = spawnSync(process.execPath, ['-e', script], { env: { ...process.env, UV_THREADPOOL_SIZE: '1' }, encoding: 'utf8', timeout: 30_000 });
  assert.equal(run.status, 0, run.stderr);
  const out = JSON.parse(run.stdout);
  assert.deepEqual(out.a, ['gone']);
  assert.equal(out.victim, 'client B bytes|', 'no byte of client A in client B’s file');
  assert.deepEqual(out.left, ['client-B'], 'A’s spool file was removed once its write settled');
});
