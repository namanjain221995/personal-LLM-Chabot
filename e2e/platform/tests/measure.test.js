'use strict';
// The overflow and console measurements against pages with KNOWN defects, in
// the real Chrome the suite uses. A regression suite whose detector silently
// reads zero is worse than none. Skipped when Chrome is not installed.
//
// WHY THE SHELL CASES (2026-09-13): the review showed the old document-only
// meter reading 0 on the app's `h-dvh overflow-hidden` shells with a 1200px
// element in the main pane. Every layout below is one the app really uses.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');

const { launchBrowser, measureOverflow, viewportFor, trackConsole } = require('../lib/browser');

const chrome = process.env.CHROME || '/usr/bin/google-chrome';
const haveChrome = fs.existsSync(chrome);

const page = (body, bodyStyle = 'margin:0') =>
  `data:text/html,${encodeURIComponent(`<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1"><body style="${bodyStyle}">${body}</body>`)}`;

const longWord = 'x'.repeat(400);
const srOnly = 'position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;white-space:nowrap;border-width:0';

// [name, body, bodyStyle, expected "how" fragment]
const DEFECTS = [
  ['a wide block in a plain document', (w) => `<div style="width:${w + 140}px;height:10px">wide</div><div style="height:3000px"></div>`, 'margin:0', 'past the right edge'],
  [
    'a block 840px wider than the phone (1200px at 360) in the main pane of an h-dvh overflow-hidden shell',
    (w) => `<div style="display:flex;height:100dvh;overflow:hidden"><nav style="width:40px;flex:none"></nav><main style="flex:1;min-width:0;overflow:auto"><h1>Members</h1><div style="width:${w + 840}px;height:20px">wide</div><div style="height:2000px"></div></main></div>`,
    'margin:0',
    'scrolls sideways inside main',
  ],
  [
    'a long unbroken string in a pre that does not scroll, in a full-height pane',
    () => `<main style="height:100dvh;overflow:auto"><pre style="margin:0">${longWord}</pre></main>`,
    'margin:0;overflow:hidden',
    'scrolls sideways inside main',
  ],
  [
    'a short pane that fits its content but still scrolls sideways past a heading',
    (w) => `<div style="display:flex;height:100dvh;overflow:hidden"><main style="flex:1;min-width:0;overflow-y:auto"><h1>Keys</h1><div style="width:${w + 540}px">row</div></main></div>`,
    'margin:0',
    'scrolls sideways inside main',
  ],
  [
    'absolutely positioned text that is NOT clipped to nothing, cut off by its shell (the sr-only exemption stays narrow)',
    (w) => `<div style="height:100dvh;overflow:hidden"><div style="display:flex"><div style="flex:none;width:${w - 300}px"></div><div style="position:relative;width:300px"><span style="position:absolute;white-space:nowrap">${longWord}</span></div></div></div>`,
    'margin:0',
    'cut off at the screen edge by overflow-x:hidden on div',
  ],
  ['content clipped by overflow-x hidden on body', (w) => `<div style="width:${w + 240}px;height:20px">clipped</div>`, 'margin:0;overflow-x:hidden', 'cut off at the screen edge by overflow-x:hidden on body'],
  [
    'content clipped by an overflow-hidden shell',
    (w) => `<div style="height:100dvh;overflow:hidden"><div style="display:flex;gap:8px"><div style="flex:none;width:${w}px">a</div><div style="flex:none;width:200px">b</div></div></div>`,
    'margin:0',
    'cut off at the screen edge by overflow-x:hidden on div',
  ],
];

const CLEAN = [
  ['a fitting page', () => '<div style="max-width:100%;height:10px">fits</div><div style="height:3000px"></div>', 'margin:0'],
  ['a code block that scrolls sideways by design', () => `<main style="height:100dvh;overflow:auto"><h1>Docs</h1><pre style="overflow-x:auto;margin:0">${longWord}</pre><div style="height:2000px"></div></main>`, 'margin:0;overflow:hidden'],
  [
    'a table in a horizontal scroll wrapper inside the main pane',
    () => `<main style="height:100dvh;overflow:auto"><h1>Members</h1><div style="overflow-x:auto"><table style="width:1100px"><tr><td>a</td><td>b</td></tr></table></div><div style="height:2000px"></div></main>`,
    'margin:0;overflow:hidden',
  ],
  ['a chip row that scrolls sideways', () => `<div style="display:flex;gap:6px;overflow-x:auto;height:40px">${'<span style="flex:none;width:120px">chip</span>'.repeat(10)}</div>`, 'margin:0'],
  ['a closed drawer translated off-screen', () => '<aside style="position:fixed;top:0;right:0;width:320px;height:100dvh;transform:translateX(100%)">drawer</aside>', 'margin:0;overflow:hidden'],
  ['a closed drawer in an overflow-hidden shell', () => '<div style="position:relative;height:100dvh;overflow:hidden"><aside style="position:absolute;top:0;left:100%;width:320px;height:100%">drawer</aside></div>', 'margin:0'],
  ['text truncated with an ellipsis', () => `<div style="display:flex"><span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0"><b>${longWord}</b></span></div>`, 'margin:0'],
  // The console's key table (2026-09-14): a scope list for screen readers in
  // a cell near the right edge, far wider than its 1x1 box.
  [
    'screen-reader-only text (clip: rect(0,0,0,0)) in a cell near the right edge',
    (w) => `<div style="height:100dvh;overflow:hidden"><table style="width:100%"><tr><td style="width:${w - 300}px">name</td><td>models.read <span style="${srOnly};clip:rect(0,0,0,0)">${longWord}</span></td></tr></table></div>`,
    'margin:0',
  ],
  [
    'screen-reader-only text in the clip-path form',
    (w) => `<div style="height:100dvh;overflow:hidden"><div style="display:flex"><div style="flex:none;width:${w - 200}px"></div><div style="position:relative;width:200px">scopes <span style="${srOnly};clip-path:inset(50%)">${longWord}</span></div></div></div>`,
    'margin:0',
  ],
  ['a decorative wide element under aria-hidden, clipped by its card', (w) => `<div style="position:relative;overflow:hidden;height:50px"><div aria-hidden="true" style="width:${w + 300}px;height:5px"></div></div>`, 'margin:0'],
];

test('overflow is found in the document, in scrolling panes and behind overflow:hidden, at phone and desktop widths', { skip: !haveChrome && 'no Chrome' }, async () => {
  const browser = await launchBrowser({ chrome, headful: false });
  try {
    const p = await browser.newPage();
    for (const width of [360, 1440]) {
      await p.setViewport(viewportFor(width));
      for (const [name, body, style, how] of DEFECTS) {
        await p.goto(page(body(width), style));
        const m = await measureOverflow(p, width);
        assert.ok(m.overflowPx > 0 && m.offenders.length > 0, `at ${width}px, "${name}" read as clean: ${JSON.stringify(m)}`);
        assert.ok(
          m.offenders.some((o) => `${o.how} ${o.container || ''}`.includes(how)),
          `at ${width}px, "${name}" was not reported as "${how}": ${JSON.stringify(m.offenders)}`,
        );
      }
    }
  } finally {
    await browser.close();
  }
});

test('layouts that scroll or hide content on purpose read as clean', { skip: !haveChrome && 'no Chrome' }, async () => {
  const browser = await launchBrowser({ chrome, headful: false });
  try {
    const p = await browser.newPage();
    for (const width of [360, 1440]) {
      await p.setViewport(viewportFor(width));
      for (const [name, body, style] of CLEAN) {
        await p.goto(page(body(width), style));
        const m = await measureOverflow(p, width);
        assert.equal(m.overflowPx, 0, `at ${width}px, "${name}" read as ${m.overflowPx}px overflow: ${JSON.stringify(m.offenders)}`);
        assert.equal(m.offenders.length, 0, `at ${width}px, "${name}" named offenders: ${JSON.stringify(m.offenders)}`);
      }
    }
  } finally {
    await browser.close();
  }
});

test('the suite console collector receives console errors and uncaught exceptions', { skip: !haveChrome && 'no Chrome' }, async () => {
  const browser = await launchBrowser({ chrome, headful: false });
  try {
    const p = await browser.newPage();
    const errors = [];
    trackConsole(p, errors);
    await p.goto(page('<script>console.warn("amber"); console.error("red one"); setTimeout(() => { throw new Error("red two") }, 0)</script>'));
    await new Promise((r) => setTimeout(r, 300));
    assert.ok(errors.some((e) => e.startsWith('console.error: red one')), JSON.stringify(errors));
    assert.ok(errors.some((e) => e.startsWith('pageerror: ') && e.includes('red two')), JSON.stringify(errors));
    assert.ok(!errors.some((e) => e.includes('amber')), `a warning was collected as an error: ${JSON.stringify(errors)}`);
  } finally {
    await browser.close();
  }
});
