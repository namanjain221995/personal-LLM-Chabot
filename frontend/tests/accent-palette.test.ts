/**
 * COLOR-01…COLOR-12 — the product accent is BLUE, and only the product accent
 * moved (owner request 2026-09-03).
 *
 * This is deliberately a TOKEN test, not a class-name test. Every green pixel
 * in the product reached the screen through `--ts-accent` / `--ts-accent-rgb`
 * / `--ts-accent-strong` / `--ts-accent-soft`: the "+" beside New chat, the
 * account avatar, selected menu rows, file hover borders, the composer source
 * chips, the drag overlay, agent checkmarks, every focus ring. Asserting the
 * class names would pin markup that did not change and would say nothing
 * about the colour; asserting the tokens pins the one thing that did.
 *
 * The second half is the harder claim: that NOTHING ELSE moved. It reads every
 * colour literal in globals.css, so a future edit that quietly turns the
 * danger red or the Vision violet blue fails here, and it holds the remaining
 * greens to an explicit allowlist with a reason attached to each — a new green
 * accent cannot appear unnoticed, and a listed one cannot vanish silently.
 */

import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';

const CSS = readFileSync(
  fileURLToPath(new URL('../app/globals.css', import.meta.url)),
  'utf8',
);

const SOURCE_GLOBS = ['../components', '../lib', '../app'] as const;

// --------------------------------------------------------------- colour math

type RGB = [number, number, number];

function hexToRgb(hex: string): RGB {
  const h = hex.replace('#', '');
  const full =
    h.length === 3
      ? h
          .split('')
          .map((c) => c + c)
          .join('')
      : h;
  return [0, 2, 4].map((i) => parseInt(full.slice(i, i + 2), 16)) as RGB;
}

/** Hue in degrees (0 = red, 120 = green, 210-240 = blue) and saturation 0-1. */
function hueSat([r, g, b]: RGB): { hue: number; sat: number } {
  const [rn, gn, bn] = [r / 255, g / 255, b / 255];
  const max = Math.max(rn, gn, bn);
  const min = Math.min(rn, gn, bn);
  const d = max - min;
  if (d === 0) return { hue: 0, sat: 0 };
  let hue: number;
  if (max === rn) hue = ((gn - bn) / d) % 6;
  else if (max === gn) hue = (bn - rn) / d + 2;
  else hue = (rn - gn) / d + 4;
  hue = (hue * 60 + 360) % 360;
  const l = (max + min) / 2;
  return { hue, sat: d / (1 - Math.abs(2 * l - 1)) };
}

function luminance([r, g, b]: RGB): number {
  const f = (c: number) => {
    const s = c / 255;
    return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
  };
  return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b);
}

function contrast(a: string, b: string): number {
  const [la, lb] = [luminance(hexToRgb(a)), luminance(hexToRgb(b))];
  const [hi, lo] = la > lb ? [la, lb] : [lb, la];
  return (hi + 0.05) / (lo + 0.05);
}

/**
 * A colour reads as GREEN when its hue sits in the green/teal arc AND it is
 * saturated enough for anyone to call it green. The band is wide on purpose:
 * it has to catch the teal the accent used to be (hue 177) as readily as a
 * literal `green`, because the owner's complaint was about the teal.
 */
function isGreenish(hex: string): boolean {
  const { hue, sat } = hueSat(hexToRgb(hex));
  return hue >= 80 && hue <= 190 && sat > 0.15;
}

function isBlueish(hex: string): boolean {
  const { hue, sat } = hueSat(hexToRgb(hex));
  return hue >= 195 && hue <= 260 && sat > 0.25;
}

// ------------------------------------------------------------- token reading

/**
 * The declarations of ONE top-level rule, found by its exact selector text.
 *
 * A substring search is not good enough here: `.auth-light` opens a rule of
 * its own AND appears inside `html.light,\n.auth-light`, and picking the wrong
 * one would silently read the light theme's tokens while claiming to read the
 * sign-in override. Scanning rules at brace depth 0 and comparing the whole
 * selector makes that ambiguity impossible.
 */
function rules(): Map<string, string> {
  const out = new Map<string, string>();
  const src = CSS.replace(/\/\*[\s\S]*?\*\//g, '');
  let i = 0;
  while (i < src.length) {
    const open = src.indexOf('{', i);
    if (open === -1) break;
    // Statement at-rules (@tailwind …;) sit between rules and would otherwise
    // be swallowed into the next selector.
    const raw = src.slice(i, open);
    const selector = raw.slice(raw.lastIndexOf(';') + 1).trim();
    let depth = 1;
    let j = open + 1;
    while (j < src.length && depth > 0) {
      if (src[j] === '{') depth += 1;
      else if (src[j] === '}') depth -= 1;
      j += 1;
    }
    // At-rules (@media, @keyframes) nest their own rules; the token blocks
    // this test reads are all top level, so they are simply skipped.
    if (!selector.startsWith('@')) out.set(selector, src.slice(open + 1, j - 1));
    i = j;
  }
  return out;
}

const RULES = rules();

function block(selector: string): Record<string, string> {
  const body = RULES.get(selector);
  expect(body, `selector not found: ${selector}`).toBeTruthy();
  const out: Record<string, string> = {};
  for (const line of (body as string).split(';')) {
    const m = line.match(/(--[a-z0-9-]+)\s*:\s*(.+)/i);
    if (m) out[m[1]] = m[2].trim();
  }
  return out;
}

const DARK = block(':root');
const LIGHT = block('html.light,\n.auth-light');
const AUTH = block('.auth-light');

const THEMES = [
  { name: 'dark', tokens: DARK },
  { name: 'light', tokens: LIGHT },
] as const;

// ------------------------------------------------------ COLOR-01 … COLOR-06

describe('the product accent is blue', () => {
  it.each(THEMES)('COLOR-01/03 · $name accent is no longer green', ({ tokens }) => {
    expect(isGreenish(tokens['--ts-accent'])).toBe(false);
    expect(isBlueish(tokens['--ts-accent'])).toBe(true);
  });

  it.each(THEMES)('COLOR-02/05 · $name filled-button accent is blue', ({ tokens }) => {
    expect(isGreenish(tokens['--ts-accent-strong'])).toBe(false);
    expect(isBlueish(tokens['--ts-accent-strong'])).toBe(true);
  });

  it.each(THEMES)('COLOR-04 · the $name soft tint follows the accent', ({ tokens }) => {
    const m = tokens['--ts-accent-soft'].match(
      /rgba\(\s*(\d+),\s*(\d+),\s*(\d+),\s*([\d.]+)\s*\)/,
    );
    expect(m, 'accent-soft must stay an rgba() of the strong accent').toBeTruthy();
    const soft = `#${[1, 2, 3]
      .map((i) => Number(m![i]).toString(16).padStart(2, '0'))
      .join('')}`;
    expect(isGreenish(soft)).toBe(false);
    expect(soft).toBe(tokens['--ts-accent-strong']);
  });

  /**
   * The file carries a "keep in sync" contract because Tailwind's
   * `accent/NN` opacity modifiers compile off the channels, not the hex: a
   * drifting --ts-accent-rgb would leave every tinted pill and hover border
   * teal while the solid ones went blue.
   */
  it.each(THEMES)('$name --ts-accent-rgb matches --ts-accent', ({ tokens }) => {
    expect(tokens['--ts-accent-rgb'].split(/\s+/).map(Number)).toEqual(
      hexToRgb(tokens['--ts-accent']),
    );
  });

  it('COLOR-06 · the focus ring still paints from the accent token', () => {
    const rule = CSS.slice(CSS.indexOf(':focus-visible {'));
    expect(rule.slice(0, rule.indexOf('}'))).toContain(
      'outline: 2px solid var(--ts-accent)',
    );
  });
});

describe('the blue stays readable in both themes', () => {
  // The surfaces an accent-coloured icon or label actually sits on.
  const DARK_SURFACES = ['--ts-bg', '--ts-sidebar', '--ts-surface', '--ts-surface-2', '--ts-bubble'];

  it.each(DARK_SURFACES)('COLOR-07 · dark accent on %s clears AA', (surface) => {
    expect(contrast(DARK['--ts-accent'], DARK[surface])).toBeGreaterThanOrEqual(4.5);
  });

  it.each(DARK_SURFACES)('COLOR-08 · light accent on %s clears AA', (surface) => {
    expect(contrast(LIGHT['--ts-accent'], LIGHT[surface])).toBeGreaterThanOrEqual(4.5);
  });

  it('COLOR-09 · white on a primary button clears AA', () => {
    // The teal this replaced sat at 3.26:1 and failed. Guard the gain.
    expect(contrast(DARK['--ts-accent-strong'], '#ffffff')).toBeGreaterThanOrEqual(4.5);
    expect(contrast(LIGHT['--ts-accent-strong'], '#ffffff')).toBeGreaterThanOrEqual(4.5);
  });
});

// ------------------------------------------------------------------ COLOR-11

describe('COLOR-11 · nothing but the accent moved', () => {
  /**
   * Pinned by exact value, both themes. These are the colours the owner named
   * as must-not-change — red, amber, the Vision violet, the neutral surfaces —
   * plus the rest of the palette they sit in.
   */
  const PINNED_DARK: Record<string, string> = {
    '--ts-navy': '#0a1d37',
    '--ts-boardroom': '#143a66',
    '--ts-slate': '#5b6b7f',
    '--ts-paper': '#f6f8fb',
    '--ts-bg': '#000000',
    '--ts-sidebar': '#0a0a0a',
    '--ts-bubble': '#303030',
    '--ts-surface': '#1e1e1e',
    '--ts-surface-2': '#2a2a2a',
    '--ts-border': '#262626',
    '--ts-text': '#ffffff',
    '--ts-text-muted': '#b3b3b3',
    '--ts-text-faint': '#8a8a8a',
    '--ts-text-icon': '#cfcfcf',
    '--ts-danger': '#ef5a5f',
    '--ts-warn': '#f0a92e',
    '--ts-engine-sql': '#b7791f',
    '--ts-engine-vision': '#6d5ae6',
    '--ts-engine-vision-ink': '#a99cf3',
    '--ts-engine-report': '#2f6fb2',
    '--ts-engine-report-ink': '#82b0dd',
    '--ts-engine-chat': '#5b6b7f',
    '--ts-engine-agent': '#8b5cf6',
    '--ts-engine-agent-ink': '#b79df8',
    '--ts-chart-1': '#0e9d9a',
    '--ts-chart-2': '#2f6fb2',
    '--ts-chart-3': '#b7791f',
    '--ts-chart-4': '#6d5ae6',
    '--ts-chart-5': '#c0566b',
  };

  const PINNED_LIGHT: Record<string, string> = {
    '--ts-bg': '#ffffff',
    '--ts-sidebar': '#f9f9f9',
    '--ts-bubble': '#f0f0f0',
    '--ts-surface': '#f4f4f4',
    '--ts-surface-2': '#ececec',
    '--ts-border': '#e3e3e3',
    '--ts-text': '#0d0d0d',
    '--ts-text-muted': '#5d5d5d',
    '--ts-danger': '#c62a30',
    '--ts-warn': '#b26a00',
    '--ts-engine-vision-ink': '#5140bd',
    '--ts-engine-report-ink': '#26598f',
    '--ts-engine-agent-ink': '#6d28d9',
  };

  it.each(Object.entries(PINNED_DARK))('dark %s is still %s', (token, value) => {
    expect(DARK[token]).toBe(value);
  });

  it.each(Object.entries(PINNED_LIGHT))('light %s is still %s', (token, value) => {
    expect(LIGHT[token]).toBe(value);
  });

  it('the sign-in pages still wear the logo indigo, not the product accent', () => {
    // .auth-light overrides the accent for the white sign-in subtree; it was
    // never teal, so the swap must not have reached it.
    expect(AUTH['--ts-accent']).toBe('#1a2480');
    expect(AUTH['--ts-accent-strong']).toBe('#1a2480');
  });

  it('the Records badge stays hue-distinct from Report and from the accent', () => {
    // This IS the reason Records/Web teal was not recoloured. The engine badge
    // set encodes a route by hue; sending Records to the accent blue would put
    // it on top of Report/Page/Site, and the badge would stop meaning
    // anything. Pinning the separation makes that trade-off enforceable rather
    // than a note in a report.
    const gap = (a: string, b: string) => {
      const d = Math.abs(hueSat(hexToRgb(a)).hue - hueSat(hexToRgb(b)).hue);
      return Math.min(d, 360 - d);
    };
    expect(gap(DARK['--ts-engine-rag'], DARK['--ts-engine-report'])).toBeGreaterThan(25);
    expect(gap(DARK['--ts-engine-rag'], DARK['--ts-accent-strong'])).toBeGreaterThan(25);
    expect(gap(DARK['--ts-engine-rag'], DARK['--ts-accent'])).toBeGreaterThan(25);
  });
});

// ------------------------------------------------------------------ COLOR-12

describe('COLOR-13 · the status tokens carry their channels', () => {
  /**
   * Tailwind compiles `bg-ok/12` only when the colour is declared as
   * `rgb(var(--token-rgb) / <alpha-value>)`. As a bare `var()` the modifier
   * silently produces NOTHING, which is how the analytics console's
   * positive-change badge came to render green text on no background — the
   * same trap `accent` and `danger` were fixed for earlier.
   */
  const CONFIG = readFileSync(
    fileURLToPath(new URL('../tailwind.config.ts', import.meta.url)),
    'utf8',
  );

  it('ok is alpha-capable, like accent and danger', () => {
    expect(CONFIG).toContain("ok: 'rgb(var(--ts-ok-rgb) / <alpha-value>)'");
  });

  it('and its channels are defined for BOTH themes', () => {
    // A token defined only on dark turns the badge invisible on paper.
    expect(CSS.match(/--ts-ok-rgb:/g)?.length).toBe(2);
  });
});

describe('COLOR-12 · the remaining greens are the allowlisted ones', () => {
  /**
   * Every green left in the stylesheet, with the reason it stays. The test
   * fails BOTH ways: an unlisted green is a missed product accent, and a
   * listed green that disappeared means this list is now lying.
   */
  const ALLOWED: Record<string, string> = {
    '#0e9f9a': 'brand constant + Records engine identity (categorical hue)',
    '#33c7c0': 'Records engine ink (dark)',
    '#0b7c77': 'Records engine ink (light)',
    '#0e9d9a': 'chart series 1 — data semantics, not a UI accent',
    '#98c379': 'code string token (dark) — syntax highlighting',
    '#50a14f': 'code string token (light) — syntax highlighting',
    '#56b6c2': 'code builtin token (dark) — syntax highlighting',
    '#0184bc': 'code builtin token (light) — syntax highlighting',
    '#7fd6a3': 'SQL string token (dark) — syntax highlighting',
    '#197a4b': 'SQL string token (light) — syntax highlighting',
    '#3fb950': 'status OK dot (dark) — health, not the product accent',
    '#1a7f37': 'status OK dot (light) — health, not the product accent',
  };

  /** Colour literals only: hex plus the rgb()/rgba() triples. */
  function literals(css: string): string[] {
    const out: string[] = [];
    for (const m of css.matchAll(/#[0-9a-f]{6}\b|#[0-9a-f]{3}\b/gi)) out.push(m[0].toLowerCase());
    for (const m of css.matchAll(/rgba?\(\s*(\d+),\s*(\d+),\s*(\d+)/g)) {
      out.push(
        `#${[1, 2, 3].map((i) => Number(m[i]).toString(16).padStart(2, '0')).join('')}`,
      );
    }
    return out;
  }

  // Comments quote historical hexes (including the old teal); they paint
  // nothing, so they are not part of the audit.
  const PAINTED = CSS.replace(/\/\*[\s\S]*?\*\//g, '');

  it('no unexplained green survives in globals.css', () => {
    const greens = [...new Set(literals(PAINTED).filter(isGreenish))];
    expect(greens.filter((g) => !(g in ALLOWED))).toEqual([]);
  });

  it('every allowlisted green is still genuinely in use', () => {
    const present = new Set(literals(PAINTED));
    expect(Object.keys(ALLOWED).filter((g) => !present.has(g))).toEqual([]);
  });

  it('no component hard-codes a green of its own', async () => {
    const { readdirSync, statSync } = await import('node:fs');
    const { join } = await import('node:path');
    const offenders: string[] = [];
    const walk = (dir: string) => {
      for (const entry of readdirSync(dir)) {
        const full = join(dir, entry);
        if (statSync(full).isDirectory()) {
          walk(full);
          continue;
        }
        if (!/\.tsx?$/.test(entry)) continue;
        const src = readFileSync(full, 'utf8').replace(
          /\/\*[\s\S]*?\*\/|\/\/[^\n]*/g,
          '',
        );
        for (const hex of new Set(literals(src))) {
          // lib/chartTheme.ts mirrors the --ts-chart-* series, which is data.
          if (isGreenish(hex) && !full.endsWith('chartTheme.ts')) {
            offenders.push(`${full}: ${hex}`);
          }
        }
        // Tailwind's own green ramps were never used here; keep it that way.
        if (/\b(?:text|bg|border|ring|from|to|via|fill|stroke|outline|divide)-(?:green|emerald|teal|lime)-\d/.test(src)) {
          offenders.push(`${full}: tailwind green utility`);
        }
      }
    };
    for (const rel of SOURCE_GLOBS) {
      walk(fileURLToPath(new URL(rel, import.meta.url)));
    }
    expect(offenders).toEqual([]);
  });
});

/* ------------------------------------------------- SELECT-COLOR-01 … 04 */

describe('SELECT-COLOR · the text-selection highlight', () => {
  /** Parse an `rgba(r, g, b, a)` declaration. */
  function rgba(value: string): { rgb: RGB; alpha: number } {
    const m = value.match(/rgba?\(\s*(\d+),\s*(\d+),\s*(\d+)(?:,\s*([\d.]+))?\s*\)/);
    expect(m, `not an rgba(): ${value}`).toBeTruthy();
    return {
      rgb: [Number(m![1]), Number(m![2]), Number(m![3])] as RGB,
      alpha: m![4] === undefined ? 1 : Number(m![4]),
    };
  }

  /** What the eye actually sees: the tint composited over the page. */
  function over(tint: { rgb: RGB; alpha: number }, base: string): string {
    const b = hexToRgb(base);
    const mixed = tint.rgb.map((c, i) =>
      Math.round(c * tint.alpha + b[i] * (1 - tint.alpha)),
    ) as RGB;
    return `#${mixed.map((c) => c.toString(16).padStart(2, '0')).join('')}`;
  }

  /**
   * Where the highlight has been.
   *
   * `soft` is the accent tint ::selection borrowed until 2026-09-03; `weak` is
   * the first dedicated --ts-selection, which the owner still found too faint
   * to pick a run of text out of the page. Both are floors: the highlight may
   * only ever get stronger than either.
   */
  const PREVIOUS_ALPHA = { dark: 0.14, light: 0.12 };
  const WEAK_ALPHA = { dark: 0.26, light: 0.2 };

  it('SELECT-COLOR-01 · ::selection paints from --ts-selection, and it is blue', () => {
    const rule = CSS.slice(CSS.indexOf('::selection {'));
    expect(rule.slice(0, rule.indexOf('}'))).toContain('background: var(--ts-selection)');
    for (const { name, tokens } of THEMES) {
      const tint = rgba(tokens['--ts-selection']);
      const hex = `#${tint.rgb.map((c) => c.toString(16).padStart(2, '0')).join('')}`;
      expect(isGreenish(hex), `${name} selection must not be green`).toBe(false);
      expect(isBlueish(hex), `${name} selection must stay blue`).toBe(true);
    }
  });

  it('SELECT-COLOR-02 · it is stronger than the soft tint it replaced', () => {
    // The owner's complaint was legibility of the SELECTION, not of the
    // accent: a highlight at 12-14% over black is very nearly invisible.
    expect(rgba(DARK['--ts-selection']).alpha).toBeGreaterThan(PREVIOUS_ALPHA.dark);
    expect(rgba(LIGHT['--ts-selection']).alpha).toBeGreaterThan(PREVIOUS_ALPHA.light);
  });

  it('SELECTION-STYLE-02 · and stronger again than the first dedicated value', () => {
    // Raised a second time on 2026-09-03: .26 / .20 were still too quiet to
    // pick a selected run out of the page at a glance.
    expect(rgba(DARK['--ts-selection']).alpha).toBeGreaterThan(WEAK_ALPHA.dark);
    expect(rgba(LIGHT['--ts-selection']).alpha).toBeGreaterThan(WEAK_ALPHA.light);
  });

  it('SELECTION-STYLE-01 · the highlight has a token of its very own', () => {
    // The whole point of --ts-selection: strengthening the highlight must be
    // reachable WITHOUT touching a token any button, ring or chip reads.
    for (const { name, tokens } of THEMES) {
      expect(tokens['--ts-selection'], `${name} has no selection token`).toBeTruthy();
    }
    const rule = CSS.slice(CSS.indexOf('::selection {'));
    expect(rule.slice(0, rule.indexOf('}'))).toContain('var(--ts-selection)');
  });

  it('SELECT-COLOR-03 / SELECTION-STYLE-03 · the product accent tokens are untouched by it', () => {
    // A selection-specific token exists precisely so that making the
    // highlight stronger cannot darken buttons, chips, focus rings or the
    // meter. --ts-accent-soft keeps the exact values the blue migration set.
    expect(DARK['--ts-accent-soft']).toBe('rgba(37, 99, 235, 0.14)');
    expect(LIGHT['--ts-accent-soft']).toBe('rgba(37, 99, 235, 0.12)');
    expect(DARK['--ts-accent']).toBe('#60a5fa');
    expect(LIGHT['--ts-accent']).toBe('#1d4ed8');
    expect(DARK['--ts-accent-strong']).toBe('#2563eb');
    // …and --ts-selection is a DIFFERENT value, or none of the above matters.
    expect(DARK['--ts-selection']).not.toBe(DARK['--ts-accent-soft']);
  });

  it('SELECT-COLOR-04 · selected text stays readable in both themes', () => {
    // Dark: white body text on the highlighted run.
    const darkBand = over(rgba(DARK['--ts-selection']), DARK['--ts-bg']);
    expect(contrast(DARK['--ts-text'], darkBand)).toBeGreaterThanOrEqual(4.5);
    // Light: near-black body text on it.
    const lightBand = over(rgba(LIGHT['--ts-selection']), LIGHT['--ts-bg']);
    expect(contrast(LIGHT['--ts-text'], lightBand)).toBeGreaterThanOrEqual(4.5);
  });

  it('SELECT-COLOR-04 · and is actually visible against the page', () => {
    /**
     * The other half of "readable": a highlight nobody can see is the bug
     * being fixed.
     *
     * Measured as a CIE L* difference, NOT as a WCAG contrast ratio. The ratio
     * is a poor instrument near either end of the range — over pure black it
     * compresses every dark band into roughly the same number, and it scored
     * the old, genuinely invisible 14% tint at 1.13 against the new one's 1.23.
     * L* is perceptually uniform, so the difference it reports is the
     * difference a person sees: the old dark highlight measured ~4, which is
     * the complaint, and anything past ~8 is unmistakable.
     */
    const lstar = (hex: string) => {
      const [r, g, b] = hexToRgb(hex).map((c) => {
        const s = c / 255;
        return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
      });
      const y = 0.2126 * r + 0.7152 * g + 0.0722 * b;
      return y > 0.008856 ? 116 * y ** (1 / 3) - 16 : 903.3 * y;
    };
    const delta = (tint: string, base: string) =>
      Math.abs(lstar(over(rgba(tint), base)) - lstar(base));

    for (const { name, tokens } of THEMES) {
      const d = delta(tokens['--ts-selection'], tokens['--ts-bg']);
      expect(d, `${name} highlight is invisible`).toBeGreaterThan(8);
      // …and not so strong that the run reads as a painted block over its text.
      expect(d, `${name} highlight is a solid block`).toBeLessThan(25);
    }
  });

  it('the sign-in pages keep their own indigo highlight', () => {
    // .auth-light is not what the owner was looking at, and it wears the logo
    // indigo rather than the product blue. Restating the token there is what
    // stops the app's stronger highlight leaking onto it.
    expect(AUTH['--ts-selection']).toBe('rgba(26, 36, 128, 0.1)');
  });
});
