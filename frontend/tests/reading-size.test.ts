/**
 * Reading size and sidebar width (owner request 2026-09-13, compared with
 * ChatGPT: a wider sidebar, bigger text in the chat and the sidebar; then
 * "a little wider than 288px on a desktop": 300px from 1280, 320px from 1536).
 *
 * A source test on purpose: jsdom does not lay out or resolve media queries,
 * so the numbers were measured in Chrome (390/768/1280/1440/1920 px) and this
 * pins the declarations those measurements came from.
 */
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';

const read = (p: string) => readFileSync(fileURLToPath(new URL(p, import.meta.url)), 'utf8');
const CSS = read('../app/globals.css').replace(/\/\*[\s\S]*?\*\//g, '');
const TAILWIND = read('../tailwind.config.ts');

/** The body of the first rule whose selector is exactly `selector`. */
function rule(selector: string): string {
  // Anchored to the end of the previous rule, so `.md table` cannot match the
  // tail of `.chat-answer .md table`.
  const re = new RegExp(`(^|\\})\\s*${selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}\\s*\\{([^}]*)\\}`);
  const m = CSS.match(re);
  expect(m, `rule not found: ${selector}`).toBeTruthy();
  return (m as RegExpMatchArray)[2];
}

/** The `:root` body inside `@media (min-width: <px>px)`, or '' when absent. */
function rootAt(px: number): string {
  const m = CSS.match(new RegExp(`@media \\(min-width: ${px}px\\)\\s*\\{\\s*:root\\s*\\{([^}]*)\\}`));
  return m ? m[1] : '';
}

/** The sidebar width in px each desktop media step declares. */
function sidebarSteps(): Array<[number, number]> {
  const steps: Array<[number, number]> = [];
  for (const m of CSS.matchAll(/@media \(min-width: (\d+)px\)\s*\{\s*:root\s*\{([^}]*)\}/g)) {
    const w = m[2].match(/--ts-sidebar-w:\s*(\d+)px;/);
    if (w) steps.push([Number(m[1]), Number(w[1])]);
  }
  return steps.sort((a, b) => a[0] - b[0]);
}

function sidebarAt(viewport: number): number {
  let width = NaN;
  for (const [from, px] of sidebarSteps()) if (viewport >= from) width = px;
  return width;
}

describe('the sidebar', () => {
  it('reads its Tailwind width from the --ts-sidebar-w token instead of a fixed 260px', () => {
    expect(TAILWIND).toContain("sidebar: 'var(--ts-sidebar-w)'");
    expect(TAILWIND).not.toContain("sidebar: '260px'");
  });

  it('is a phone drawer of at most 320px that always leaves 56px of the thread to tap out on', () => {
    expect(CSS).toContain('--ts-sidebar-w: min(320px, calc(100vw - 56px));');
  });

  it('is 288px on a tablet, 300px from 1280px and 320px from 1536px', () => {
    expect(rootAt(768)).toContain('--ts-sidebar-w: 288px;');
    expect(rootAt(1280)).toContain('--ts-sidebar-w: 300px;');
    expect(rootAt(1536)).toContain('--ts-sidebar-w: 320px;');
    expect([1024, 1279, 1280, 1440, 1535, 1536, 1920].map(sidebarAt)).toEqual([
      288, 288, 300, 300, 300, 320, 320,
    ]);
  });

  it('never grows so wide that an open file panel squeezes the thread under 360px', () => {
    // A model of the flex row to the right of the sidebar, mirroring the two
    // CSS strings ChatApp sets on the panel column (pinned verbatim in
    // artifact-panel-layout.test.tsx): the panel is its 45 % or 55 % basis,
    // clamped between min-width and max-width (min wins a conflict, as in
    // CSS), and the thread takes what is left after the 6px divider.
    const thread = (workspace: number, pct: number) => {
      const minW = Math.max(0.45 * workspace, Math.min(520, 0.62 * workspace, workspace - 366));
      const maxW = Math.min(960, Math.max(0.45 * workspace, workspace - 366));
      const panel = Math.max(minW, Math.min(maxW, (pct / 100) * workspace));
      return workspace - panel - 6;
    };
    const squeezed: string[] = [];
    for (let vw = 768; vw <= 2560; vw++) {
      for (const pct of [45, 55]) {
        // From 1024 up the sidebar may stay open beside the panel (below 1280
        // only if reopened by hand); from 768 to 1279 opening a file closes it.
        // `!(x >= 360)` rather than `x < 360`, so a width the CSS no longer
        // declares (NaN) counts as squeezed instead of passing vacuously.
        if (vw >= 1024 && !(thread(vw - sidebarAt(vw), pct) >= 360)) squeezed.push(`${vw}px open ${pct}%`);
        if (vw < 1280 && !(thread(vw, pct) >= 360)) squeezed.push(`${vw}px closed ${pct}%`);
      }
    }
    expect(squeezed).toEqual([]);
  });
});

describe('the chat reading size', () => {
  it('is 16px on a phone and 17px from 768px up, with a 1.7 line height', () => {
    expect(CSS).toContain('--ts-fs-chat: 16px;');
    expect(CSS).toContain('--ts-lh-chat: 1.7;');
    expect(rootAt(768)).toContain('--ts-fs-chat: 17px;');
    expect(rule('.chat-answer')).toContain('font-size: var(--ts-fs-chat)');
    expect(rule('.chat-answer .md')).toContain('line-height: var(--ts-lh-chat)');
  });

  it('scales the headings with the body, so an h3 is never smaller than its paragraph', () => {
    expect(rule('.chat-answer .md h1')).toContain('font-size: 1.25em');
    expect(rule('.chat-answer .md h2')).toContain('font-size: 1.125em');
    expect(rule('.chat-answer .md h3,\n.chat-answer .md h4')).toContain('font-size: 1em');
  });

  it('steps tables and code down from the same size, so they grow on a desktop and stay compact on a phone', () => {
    expect(rule('.chat-answer .md table')).toContain('font-size: calc(var(--ts-fs-chat) - 2px)');
    expect(rule('.chat-answer .md thead th')).toContain('font-size: calc(var(--ts-fs-chat) - 3px)');
    expect(rule('.chat-answer .code-block pre')).toContain('font-size: calc(var(--ts-fs-chat) - 3px)');
  });

  it('does not move the docs site or any other .md surface', () => {
    // The unscoped rules keep their fixed sizes; only .chat-answer overrides.
    expect(rule('.md table')).toContain('font-size: var(--ts-fs-sm)');
    expect(rule('.md h3,\n.md h4')).toContain('font-size: var(--ts-fs-base)');
  });
});
