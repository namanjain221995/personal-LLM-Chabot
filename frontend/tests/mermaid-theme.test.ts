/**
 * DIAG-01…DIAG-24 — the chat diagram theme.
 *
 * Written in the shape of tests/accent-palette.test.ts: the colour claims are
 * COMPUTED from whatever the theme returns (contrast ratios, hue bands, CVD
 * separation), never asserted as hex strings. Pinning `#22303f` would pin a
 * value that says nothing about whether the box is visible; computing the
 * contrast pins the thing the owner complained about.
 *
 * These tests discriminate against the SHIPPED behaviour, not merely against
 * an empty file: run against a module that returns the old config
 * (`theme: 'dark'` + 26 themeVariables, light = `{ background: '#ffffff' }`,
 * no roles, no sanitiser, `useMaxWidth`-style sizing), DIAG-01, 02, 03, 05,
 * 06, 07, 08, 11-20 and 22-24 all fail.
 *
 * What these tests CANNOT see: colour. A pure function returning `#22303f`
 * proves nothing about what Chromium paints — the whole defect was that
 * mermaid's packaged `dark` theme silently discarded a declared `#2f2f2f` and
 * painted rgb(31,32,32). That half is proved by rendering the corpus in a real
 * browser and looking at it; these tests pin the values the browser was given.
 */

import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  CATEGORICAL_SLOTS,
  DIAGRAM_ROLES,
  LABEL_FLOOR_PX,
  SECURE_KEYS,
  acceptsClassDefs,
  categoricalInk,
  diagramHead,
  diagramRefusal,
  diagramScale,
  guardDiagramSource,
  mermaidTheme,
  prepareDiagramSource,
  resolveCategorical,
  resolveRolePaints,
  roleClassDefs,
  sanitizeDiagramSource,
  smallestLabelPx,
  withoutRoleApplications,
  type ThemeMode,
} from '../lib/mermaidTheme';
import { diagramFileName, looksRenderable } from '../lib/mermaid';

// --------------------------------------------------------------- colour math
// Same instruments as tests/accent-palette.test.ts, deliberately duplicated:
// a shared helper module would let one edit move both the palette and its
// judge at once.

type RGB = [number, number, number];

function hexToRgb(hex: string): RGB {
  const h = hex.replace('#', '');
  const full =
    h.length === 3 ? h.split('').map((c) => c + c).join('') : h;
  return [0, 2, 4].map((i) => parseInt(full.slice(i, i + 2), 16)) as RGB;
}

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

function toLinear(c: number): number {
  const s = c / 255;
  return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
}

function luminance([r, g, b]: RGB): number {
  return 0.2126 * toLinear(r) + 0.7152 * toLinear(g) + 0.0722 * toLinear(b);
}

function contrast(a: string, b: string): number {
  const [la, lb] = [luminance(hexToRgb(a)), luminance(hexToRgb(b))];
  const [hi, lo] = la > lb ? [la, lb] : [lb, la];
  return (hi + 0.05) / (lo + 0.05);
}

/** The green/teal band tests/accent-palette.test.ts polices, to the letter. */
function isGreenish(hex: string): boolean {
  const { hue, sat } = hueSat(hexToRgb(hex));
  return hue >= 80 && hue <= 190 && sat > 0.15;
}

/** Brettel-style simulation of the three dichromacies, in linear RGB. */
function simulate(hex: string, kind: 'protan' | 'deutan' | 'tritan'): RGB {
  const [r, g, b] = hexToRgb(hex).map(toLinear);
  let L = 0.31399 * r + 0.63951 * g + 0.04649 * b;
  let M = 0.15537 * r + 0.75789 * g + 0.0867 * b;
  let S = 0.01775 * r + 0.10944 * g + 0.87262 * b;
  if (kind === 'protan') L = 1.05118 * M - 0.05116 * S;
  else if (kind === 'deutan') M = 0.9513 * L + 0.04865 * S;
  else S = -0.86744 * L + 1.86727 * M;
  const lin = [
    5.47221 * L - 4.6419 * M + 0.16963 * S,
    -1.1252 * L + 2.29317 * M - 0.1678 * S,
    0.0298 * L - 0.19318 * M + 1.16364 * S,
  ];
  return lin.map((c) => {
    const v = Math.min(1, Math.max(0, c));
    return (v <= 0.0031308 ? 12.92 * v : 1.055 * v ** (1 / 2.4) - 0.055) * 255;
  }) as RGB;
}

function cvdDistance(a: string, b: string, kind: 'protan' | 'deutan' | 'tritan'): number {
  const [pa, pb] = [simulate(a, kind), simulate(b, kind)];
  return Math.hypot(pa[0] - pb[0], pa[1] - pb[1], pa[2] - pb[2]);
}

/** Every hex literal anywhere in a returned config, however deeply nested. */
function hexes(value: unknown): string[] {
  if (typeof value === 'string') {
    return [...value.matchAll(/#[0-9a-f]{6}\b|#[0-9a-f]{3}\b/gi)].map((m) =>
      m[0].toLowerCase(),
    );
  }
  if (Array.isArray(value)) return value.flatMap(hexes);
  if (value && typeof value === 'object') return Object.values(value).flatMap(hexes);
  return [];
}

const MODES: ThemeMode[] = ['dark', 'light'];
/** The real card surface a diagram sits on, per theme (globals.css). */
const SURFACE: Record<ThemeMode, string> = { dark: '#1e1e1e', light: '#f4f4f4' };

// ------------------------------------------------------- DIAG-01 … DIAG-04

describe('DIAG-01…04 · the declared theme is the theme that renders', () => {
  it.each(MODES)('DIAG-01 · %s asks for the base theme, not a packaged one', (mode) => {
    // The whole defect: mermaid's packaged `dark`/`default` themes RE-DERIVE
    // their node colours after applying overrides, so a declared
    // primaryColor #2f2f2f painted rgb(31,32,32) and a declared
    // primaryBorderColor #6b6b6b painted rgb(204,204,204). Only `base` makes
    // themeVariables authoritative.
    expect(mermaidTheme(mode).theme).toBe('base');
  });

  it.each(MODES)('DIAG-02 · %s carries a COMPLETE variable set', (mode) => {
    const vars = mermaidTheme(mode).themeVariables as Record<string, unknown>;
    // Light used to be the single key `{ background: '#ffffff' }`, i.e.
    // mermaid's stock theme on our grey card. Both modes now describe every
    // surface a diagram paints.
    expect(Object.keys(vars).length).toBeGreaterThanOrEqual(40);
    for (const key of [
      'background', 'primaryColor', 'primaryBorderColor', 'primaryTextColor',
      'mainBkg', 'nodeBorder', 'nodeTextColor', 'lineColor', 'textColor',
      'clusterBkg', 'clusterBorder', 'edgeLabelBackground',
      'actorBkg', 'actorBorder', 'signalColor',
      'transitionColor', 'stateBkg', 'altBackground',
      'attributeBackgroundColorOdd', 'attributeBackgroundColorEven',
      'noteBkgColor', 'noteTextColor',
    ]) {
      expect(vars[key], `${mode} is missing ${key}`).toBeTruthy();
    }
  });

  it.each(MODES)('DIAG-03 · %s paints its own background, not white', (mode) => {
    // `unknown`, not `string`: themeVariables carries mermaid's boolean
    // `darkMode` alongside the colours, and `next build` type-checks this
    // file even though `tsc -p .` excludes tests/.
    const vars = mermaidTheme(mode).themeVariables as Record<string, unknown>;
    expect(String(vars.background).toLowerCase()).toBe(SURFACE[mode]);
  });

  it('DIAG-04 · the two safety properties are untouched', () => {
    // securityLevel 'strict' keeps model-authored labels from carrying raw
    // HTML; htmlLabels:false draws labels as SVG <text> instead of
    // <foreignObject>, which is what keeps the canvas untainted so "Download
    // PNG" works. Neither is needed to fix the colour.
    for (const mode of MODES) {
      const cfg = mermaidTheme(mode);
      expect(cfg.securityLevel).toBe('strict');
      expect(cfg.htmlLabels).toBe(false);
      expect(cfg.flowchart.htmlLabels).toBe(false);
    }
  });
});

// ------------------------------------------------------- DIAG-05 … DIAG-10

describe('DIAG-05…10 · the role palette is legible and distinguishable', () => {
  it('DIAG-05 · the role list is exactly the shared four-name vocabulary', () => {
    // A closed vocabulary shared verbatim with the orchestrator. If a track
    // drifts and emits a fifth name, a schema-valid diagram would carry a
    // class this theme has no classDef for, and the node would silently lose
    // its colour. That drift fails HERE.
    expect([...DIAGRAM_ROLES]).toEqual(['service', 'store', 'model', 'external']);
  });

  it.each(MODES)('DIAG-06 · %s · every role label clears AA on its own fill', (mode) => {
    const paints = resolveRolePaints(mode);
    for (const role of DIAGRAM_ROLES) {
      const { ink, fill } = paints[role];
      expect(contrast(ink, fill), `${mode}/${role} ink on fill`).toBeGreaterThanOrEqual(4.5);
    }
  });

  it.each(MODES)('DIAG-07 · %s · every role outline clears 3:1 on the card', (mode) => {
    // The FILL is a tint; the STROKE is what makes the box a box. The shipped
    // dark node was 1.02:1 against the card — an invisible box with a thin
    // outline, which is the owner's "what is this inside the box".
    const paints = resolveRolePaints(mode);
    for (const role of DIAGRAM_ROLES) {
      expect(
        contrast(paints[role].stroke, SURFACE[mode]),
        `${mode}/${role} stroke on card`,
      ).toBeGreaterThanOrEqual(3);
    }
  });

  it.each(MODES)("DIAG-07b · %s · …and 3:1 on the role's OWN fill", (mode) => {
    /**
     * The other side of the same line.
     *
     * DIAG-07 above checks the stroke against the CARD, which is the boundary
     * a person sees from outside the box. A stroke is drawn BETWEEN two
     * colours, and the one on the inside is the role's own fill — a rule the
     * card check cannot see at all, because the fill is a tint of the stroke's
     * own hue and therefore the neighbour most likely to swallow it.
     *
     * Measured with the dataviz skill's validator, 2026-09-27: dark `service`
     * #2f6fb2 on its own #22303f fill was 2.58:1 and light `store` #b7791f on
     * #eae0d2 was 2.79:1 — outlines that passed DIAG-07 and then vanished
     * into the box. Both are moved; this test is what stops them coming back.
     *
     * Not a stricter floor than DIAG-07 on purpose: 3:1 is the non-text
     * contrast minimum, and the fill deliberately sits close to the card, so
     * a stroke that clears 3:1 on both sides is the most a tint-plus-outline
     * node can be asked for.
     */
    const paints = resolveRolePaints(mode);
    for (const role of DIAGRAM_ROLES) {
      expect(
        contrast(paints[role].stroke, paints[role].fill),
        `${mode}/${role} stroke on its own fill`,
      ).toBeGreaterThanOrEqual(3);
    }
  });

  it.each(MODES)('DIAG-08 · %s · the fill is visibly lighter than the card', (mode) => {
    // L*, not a WCAG ratio: the ratio compresses everything dark into roughly
    // the same number near black, which is exactly how a 1.02:1 fill survived
    // review. Measured, the shipped dark fill sat at ΔL* 0.88.
    const lstar = (hex: string) => {
      const y = luminance(hexToRgb(hex));
      return y > 0.008856 ? 116 * y ** (1 / 3) - 16 : 903.3 * y;
    };
    const paints = resolveRolePaints(mode);
    for (const role of DIAGRAM_ROLES) {
      expect(
        Math.abs(lstar(paints[role].fill) - lstar(SURFACE[mode])),
        `${mode}/${role} fill ΔL* from the card`,
      ).toBeGreaterThan(5);
    }
  });

  it.each(MODES)('DIAG-09 · %s · the four outlines stay apart under CVD', (mode) => {
    const paints = resolveRolePaints(mode);
    const strokes = DIAGRAM_ROLES.map((r) => paints[r].stroke);
    for (const kind of ['protan', 'deutan', 'tritan'] as const) {
      for (let i = 0; i < strokes.length; i += 1) {
        for (let j = i + 1; j < strokes.length; j += 1) {
          expect(
            cvdDistance(strokes[i], strokes[j], kind),
            `${mode} ${kind} ${DIAGRAM_ROLES[i]}/${DIAGRAM_ROLES[j]}`,
          ).toBeGreaterThan(40);
        }
      }
    }
  });

  it.each(MODES)('DIAG-10 · %s · the four roles are four distinct paints', (mode) => {
    const paints = resolveRolePaints(mode);
    expect(new Set(DIAGRAM_ROLES.map((r) => paints[r].fill)).size).toBe(4);
    expect(new Set(DIAGRAM_ROLES.map((r) => paints[r].stroke)).size).toBe(4);
  });
});

// ------------------------------------------------------------------ DIAG-11

describe('DIAG-11 · nothing the theme returns is green', () => {
  it.each(MODES)('%s · no hex falls in the green band', (mode) => {
    // tests/accent-palette.test.ts fails any hex with hue 80-190 and
    // saturation > 0.15 outside chartTheme.ts, and teal is already the
    // Records engine identity. A fifth role in teal would break both.
    const offenders = [
      ...hexes(mermaidTheme(mode)),
      ...hexes(resolveRolePaints(mode)),
      ...hexes(roleClassDefs(mode)),
    ].filter(isGreenish);
    expect([...new Set(offenders)]).toEqual([]);
  });
});

// ------------------------------------------------------- DIAG-12 … DIAG-19

/**
 * The heading below is a claim, and it was NOT true when it was first written.
 * `sanitizeDiagramSource` anchored its match to the start of a line, and
 * mermaid's flowchart grammar takes `;` as a statement separator, so
 * `C-->D; style A fill:#ff0000,stroke:#00ff00` rendered red-on-green in both
 * themes. DIAG-13e…13k and DIAG-16b/16c are the cases that were missing; nine
 * of them still fail against the version that made the claim.
 */
describe('DIAG-12…19 · the colour ban is enforced in code, not by asking', () => {
  it('DIAG-12 · a %%{init}%% directive cannot reach the theme', () => {
    // Measured: a model-emitted %%{init: {'theme':'default'}}%% painted
    // mermaid's light lavender (#ececff nodes) inside the dark chat. Two
    // defences, belt and braces — the directive is stripped from the source,
    // AND `theme`/`themeVariables` are in mermaid's `secure` list so a
    // directive we somehow miss still cannot move them.
    expect(SECURE_KEYS).toContain('theme');
    expect(SECURE_KEYS).toContain('themeVariables');
    for (const mode of MODES) {
      expect(mermaidTheme(mode).secure).toEqual(expect.arrayContaining(['theme', 'themeVariables']));
    }
    const out = sanitizeDiagramSource(
      "%%{init: {'theme':'default'}}%%\nflowchart LR\n  A[Start] --> B[End]",
    );
    expect(out).not.toContain('%%{');
    expect(out).toContain('A[Start] --> B[End]');
  });

  it('DIAG-13 · classDef, style, linkStyle and click are stripped', () => {
    const out = sanitizeDiagramSource(
      [
        'flowchart LR',
        '  A[Start] --> B[End]',
        '  style A fill:#ff0000,stroke:#00ff00',
        '  linkStyle 0 stroke:#ff00ff',
        '  classDef mine fill:#123456',
        '  click A "https://example.com"',
      ].join('\n'),
    );
    for (const banned of ['style ', 'linkStyle', 'classDef', 'click ', '#ff0000']) {
      expect(out, `${banned} survived`).not.toContain(banned);
    }
    expect(out).toContain('A[Start] --> B[End]');
  });

  it('DIAG-14 · a label that merely contains the word is left alone', () => {
    // The matches are anchored to the start of a line precisely so this
    // survives; a bare substring search would gut the diagram.
    const out = sanitizeDiagramSource('flowchart LR\n  A[style guide] --> B[click here]');
    expect(out).toContain('A[style guide]');
    expect(out).toContain('B[click here]');
  });

  it.each(DIAGRAM_ROLES)('DIAG-15 · the role name %s survives as :::role', (role) => {
    const out = sanitizeDiagramSource(`flowchart LR\n  A[One]:::${role} --> B[Two]`);
    expect(out).toContain(`:::${role}`);
  });

  it.each(DIAGRAM_ROLES)('DIAG-16 · …and as `class A,B %s`', (role) => {
    const out = sanitizeDiagramSource(`flowchart LR\n  A --> B\n  class A,B ${role}`);
    expect(out).toContain(`class A,B ${role}`);
  });

  it('DIAG-17 · a classDiagram keeps its own `class Foo { … }` blocks', () => {
    // `class` is NOT in the strip list: a classDiagram's own declarations
    // carry no colour, and removing them would empty the diagram.
    const src = 'classDiagram\n  class Order {\n    +String id\n  }\n  Order --> Item';
    expect(sanitizeDiagramSource(src)).toBe(src);
  });

  it.each(MODES)('DIAG-18 · %s appends exactly four role classDefs', (mode) => {
    const defs = roleClassDefs(mode);
    expect(defs).toHaveLength(4);
    for (const role of DIAGRAM_ROLES) {
      expect(defs.some((d) => d.startsWith(`classDef ${role} `))).toBe(true);
    }
    const prepared = prepareDiagramSource('flowchart LR\n  A:::service --> B:::store', mode);
    expect(prepared).toContain('A:::service --> B:::store');
    for (const def of defs) expect(prepared).toContain(def);
  });

  it('DIAG-19 · an unknown role is left in place and degrades to the default node', () => {
    // Mermaid renders a node whose class has no classDef with the default
    // node paint. The sanitiser must not "helpfully" rewrite or drop the
    // name — this track ships before the orchestrator has been taught the
    // vocabulary, so unknown names are the NORMAL case for a while.
    const src = 'flowchart LR\n  A[Gateway]:::service --> B[Queue]:::broker';
    expect(() => sanitizeDiagramSource(src)).not.toThrow();
    const prepared = prepareDiagramSource(src, 'dark');
    expect(prepared).toContain(':::broker');
    expect(prepared).not.toContain('classDef broker');
  });

  it('DIAG-19b · classDefs are only appended where the grammar takes them', () => {
    // Appending a classDef to a sequenceDiagram or an erDiagram is a PARSE
    // ERROR, which would replace a working diagram with our quiet error card.
    expect(acceptsClassDefs('flowchart LR\n A-->B')).toBe(true);
    expect(acceptsClassDefs('graph TD\n A-->B')).toBe(true);
    expect(acceptsClassDefs('stateDiagram-v2\n [*] --> A')).toBe(true);
    expect(acceptsClassDefs('sequenceDiagram\n A->>B: hi')).toBe(false);
    expect(acceptsClassDefs('erDiagram\n A ||--o{ B : has')).toBe(false);
    expect(prepareDiagramSource('sequenceDiagram\n  A->>B: hi', 'dark')).not.toContain('classDef');
    expect(prepareDiagramSource('erDiagram\n  A ||--o{ B : has', 'dark')).not.toContain('classDef');
  });

  it('DIAG-19d · the appended classDefs must not satisfy the streaming guard', () => {
    // Found by measurement, not by reading: `looksRenderable` asks only for a
    // known head plus ONE body line, and the four appended classDef lines are
    // four body lines. Feeding it the PREPARED source therefore reports a
    // still-streaming `flowchart LR` as a finished diagram, and the block
    // renders an empty one mid-answer. MermaidBlock gates on the raw `code`
    // for exactly this reason; this test pins the hazard so a future
    // refactor cannot quietly swap the argument back.
    const partial = 'flowchart LR';
    expect(looksRenderable(partial)).toBe(false);
    expect(looksRenderable(prepareDiagramSource(partial, 'dark'))).toBe(true);
  });

  it('DIAG-19c · the sanitiser never throws on junk', () => {
    for (const junk of ['', '   ', '%%{', 'style', 'classDef', '\n\n\n', '%%{init: {']) {
      expect(() => sanitizeDiagramSource(junk)).not.toThrow();
      expect(() => prepareDiagramSource(junk, 'dark')).not.toThrow();
    }
    expect(sanitizeDiagramSource('')).toBe('');
  });

  /**
   * DIAG-13e…13j — the semicolon hole.
   *
   * The first version of this sanitiser anchored its match to the start of a
   * LINE. mermaid's flowchart grammar takes `;` as a statement separator, so
   * `C-->D; style A fill:#ff0000,stroke:#00ff00` never started with `style`
   * and rode straight through: measured in Chromium, rgb(255,0,0) on a
   * rgb(0,255,0) outline, in BOTH themes. The ban is per STATEMENT now.
   */
  it('DIAG-13e · a colour directive cannot ride a semicolon', () => {
    const out = sanitizeDiagramSource(
      'graph TD\n  A[Alpha]-->B[Beta]\n  C[Gamma]-->D[Delta]; style A fill:#ff0000,stroke:#00ff00',
    );
    expect(out).not.toContain('style ');
    expect(out).not.toContain('#ff0000');
    expect(out).not.toContain('#00ff00');
    // …and the statements that SHARED the line are still there.
    expect(out).toContain('A[Alpha]-->B[Beta]');
    expect(out).toContain('C[Gamma]-->D[Delta]');
  });

  it.each([
    ['classDef', 'classDef mine fill:#00ff88,stroke:#ff00ff'],
    ['linkStyle', 'linkStyle 0 stroke:#ff0000,stroke-width:6px'],
    ['click', 'click A "https://example.com"'],
  ])('DIAG-13f · …and neither can %s', (_name, directive) => {
    const out = sanitizeDiagramSource(`graph TD\n  A-->B\n  C-->D; ${directive}`);
    expect(out).not.toContain(directive);
    expect(out).toContain('C-->D');
  });

  it('DIAG-13g · several directives on one line all go, survivors stay in order', () => {
    const out = sanitizeDiagramSource(
      'graph TD\n  A-->B; style A fill:#ff0000; C-->D; linkStyle 0 stroke:#0f0',
    );
    expect(out).not.toContain('style');
    expect(out).not.toContain('#ff0000');
    expect(out.indexOf('A-->B')).toBeLessThan(out.indexOf('C-->D'));
  });

  /**
   * The other half of the same change: splitting on `;` must not cut a LABEL
   * in half. A label is delimited by `"…"`, `[…]`, `(…)`, `{…}` or the `|…|`
   * of an edge label, and a `;` inside one is content, not a separator.
   */
  it('DIAG-13h · a semicolon inside a quoted label is content, not a separator', () => {
    const src =
      'flowchart TD\n  A["Alpha; then Beta"] --> B["Ends with a semicolon;"]\n  B --> C[Gamma]';
    expect(sanitizeDiagramSource(src)).toBe(src);
  });

  it('DIAG-13i · …even when a real directive follows the labelled statement', () => {
    const out = sanitizeDiagramSource(
      'flowchart TD\n  A["Alpha; then Beta"] --> B[Beta]; style A fill:#ff0000',
    );
    expect(out).toContain('A["Alpha; then Beta"] --> B[Beta]');
    expect(out).not.toContain('#ff0000');
  });

  it.each([
    ['an edge label', 'flowchart TD\n  A -->|yes; maybe| B; style A fill:#ff0000', 'A -->|yes; maybe| B'],
    ['an apostrophe', "flowchart TD\n  A[Don't panic] --> B; style A fill:#ff0000", "A[Don't panic] --> B"],
    ['a brace label', 'flowchart TD\n  A{Ready; set?} --> B; style A fill:#ff0000', 'A{Ready; set?} --> B'],
    ['a round label', 'flowchart TD\n  A(Start; go) --> B; style A fill:#ff0000', 'A(Start; go) --> B'],
  ])('DIAG-13i · …and with %s', (_what, src, kept) => {
    const out = sanitizeDiagramSource(src);
    expect(out).toContain(kept);
    expect(out).not.toContain('#ff0000');
  });

  it('DIAG-13j · an unbalanced delimiter does not re-open the hole', () => {
    // If `[` were trusted here the rest of the line would count as "inside a
    // label" and the `style` would survive — measured, it did. A delimiter
    // that does not balance on its line is not treated as a delimiter, which
    // costs nothing because such a source does not parse anyway.
    const out = sanitizeDiagramSource(
      'flowchart TD\n  A["unclosed --> B; style A fill:#ff0000',
    );
    expect(out).not.toContain('#ff0000');
  });

  it('DIAG-12b · a %%{init}%% block that spans several lines is stripped whole', () => {
    const out = sanitizeDiagramSource(
      [
        '%%{init: {',
        "  'theme': 'default',",
        "  'themeVariables': { 'primaryColor': '#ff0000' }",
        '}}%%',
        'flowchart TD',
        '  A[Alpha] --> B[Beta]',
      ].join('\n'),
    );
    expect(out).not.toContain('%%{');
    expect(out).not.toContain('themeVariables');
    expect(out).not.toContain('#ff0000');
    expect(out).toContain('A[Alpha] --> B[Beta]');
    // …and what is left still reads as a diagram, head first.
    expect(out.split('\n')[0].trim()).toBe('flowchart TD');
  });

  it('DIAG-12c · an init directive tucked behind a semicolon is stripped too', () => {
    const out = sanitizeDiagramSource(
      'flowchart TD\n  A[Alpha] --> B[Beta]; %%{init: {"theme":"default"}}%%',
    );
    expect(out).not.toContain('%%{');
    expect(out).toContain('A[Alpha] --> B[Beta]');
  });

  it('DIAG-16b · a role application survives a line it shares with a directive', () => {
    // The point of splitting rather than dropping the line: the ROLE is the
    // vocabulary we keep, and it must not become collateral damage.
    const out = sanitizeDiagramSource(
      'flowchart LR\n  A[One]:::service --> B[Two]; style A fill:#ff0000\n  class B store',
    );
    expect(out).toContain(':::service');
    expect(out).toContain('class B store');
    expect(out).not.toContain('#ff0000');
  });

  it('DIAG-16c · `class X mine` is left in place and paints nothing', () => {
    // Once its `classDef mine` is stripped, the application names a class
    // nobody defined. mermaid renders that node in the default paint — the
    // same graceful degradation DIAG-19 pins for `:::broker`, and the reason
    // `class` is not in the strip list at all (DIAG-16, DIAG-17).
    const out = sanitizeDiagramSource(
      'graph TD\n  A[Alpha]-->B[Beta]; classDef mine fill:#00ff88\n  class A mine',
    );
    expect(out).toContain('class A mine');
    expect(out).not.toContain('classDef mine');
    expect(out).not.toContain('#00ff88');
  });

  /**
   * DIAG-12d…12g — a preamble must not cost the diagram.
   *
   * Stripping a directive is only half the job: measured, a source opening
   * with a multi-line `%%{init: {` or with a `---\nconfig: …\n---` frontmatter
   * never rendered at all, in either theme. `looksRenderable` read
   * `'theme': 'default'` (or `---`) as the head, matched no known diagram
   * type, and the answer showed source where a diagram belonged. The
   * directive was inert; so was the diagram.
   */
  it('DIAG-12d · a multi-line init block does not hide the diagram head', () => {
    const src = [
      '%%{init: {',
      "  'theme': 'default'",
      '}}%%',
      'flowchart TD',
      '  A[Alpha] --> B[Beta]',
    ].join('\n');
    expect(looksRenderable(src)).toBe(true);
    expect(diagramHead(src)).toBe('flowchart');
    expect(acceptsClassDefs(src)).toBe(true);
  });

  it('DIAG-12e · …and neither does a YAML frontmatter block', () => {
    const src = [
      '---',
      'title: Alpha to Beta',
      'config:',
      '  theme: default',
      '---',
      'flowchart TD',
      '  A[Alpha] --> B[Beta]',
    ].join('\n');
    expect(looksRenderable(src)).toBe(true);
    expect(diagramHead(src)).toBe('flowchart');
    // …so the role classDefs still get appended, which they did not before.
    expect(prepareDiagramSource(src, 'dark')).toContain('classDef service ');
  });

  it('DIAG-12f · an UNCLOSED preamble still stops the streaming guard', () => {
    // Mid-stream the closing `}%%` has not arrived yet. Dropping an
    // unterminated block would let a half-written diagram render.
    expect(looksRenderable("%%{init: {\n  'theme': 'default'")).toBe(false);
    expect(looksRenderable('---\ntitle: x\nflowchart TD')).toBe(false);
    expect(looksRenderable('%%{init: {}}%%\nflowchart TD')).toBe(false);
  });

  it('DIAG-12g · the download name comes from the diagram, not the preamble', () => {
    expect(
      diagramFileName('---\ntitle: x\n---\nflowchart TD\n  A --> B', 'png'),
    ).toBe('flowchart-td.png');
  });

  it('DIAG-13k · a line with nothing to strip is returned byte-for-byte', () => {
    // Rewriting every line would churn the Code tab — and the Code tab is
    // what the copy button copies.
    for (const src of [
      'graph TD;\n  A-->B;\n  B-->C;',
      'flowchart LR\n  A[One] --> B[Two]',
      'sequenceDiagram\n  A->>B: hi; there',
      'mindmap\n  root((Core))\n    Leaf',
    ]) {
      expect(sanitizeDiagramSource(src)).toBe(src);
    }
  });
});

// ------------------------------------------------------- DIAG-20 … DIAG-24

describe('DIAG-20…24 · the zoom floor', () => {
  /**
   * The measured corpus: natural size and the SMALLEST label in the SVG's own
   * units, read out of Chromium 11.17 renders. The right-hand comment is what
   * the shipped policy put on screen at the 702 px column.
   */
  const CORPUS = [
    { name: 'architecture', naturalWidth: 1773, labelPx: 16 },          // was 6.0 px
    { name: 'architecture_subgraph', naturalWidth: 2486, labelPx: 16 }, // was 4.3 px
    { name: 'er', naturalWidth: 459, labelPx: 14 },                     // was 14.0 px
    { name: 'sequence', naturalWidth: 1098, labelPx: 16 },              // was 9.7 px
    { name: 'state', naturalWidth: 396, labelPx: 16 },                  // was 16.0 px
    { name: 'small', naturalWidth: 459, labelPx: 16 },                  // was 16.0 px
    { name: 'roles_inline', naturalWidth: 1136, labelPx: 16 },          // was 9.4 px
    { name: 'roles_class', naturalWidth: 513, labelPx: 16 },            // was 16.0 px
    { name: 'roles_unknown', naturalWidth: 476, labelPx: 16 },          // was 16.0 px
  ];

  // Content width inside the host: 702 - 2px border - 32px padding, and the
  // same for a 360 px phone viewport. Both measured, not assumed.
  const DESKTOP = 668;
  const PHONE = 260;

  it.each(CORPUS)('DIAG-20 · $name keeps 12 px labels on a 702 px column', (c) => {
    const scale = diagramScale({
      hostWidth: DESKTOP,
      naturalWidth: c.naturalWidth,
      smallestLabelPx: c.labelPx,
    });
    expect(c.labelPx * scale).toBeGreaterThanOrEqual(LABEL_FLOOR_PX);
  });

  it.each(CORPUS)('DIAG-21 · $name keeps 12 px labels on a 360 px phone', (c) => {
    const scale = diagramScale({
      hostWidth: PHONE,
      naturalWidth: c.naturalWidth,
      smallestLabelPx: c.labelPx,
    });
    expect(c.labelPx * scale).toBeGreaterThanOrEqual(LABEL_FLOOR_PX);
  });

  it('DIAG-22 · a diagram narrower than the column is never stretched UP', () => {
    // One answer carrying 8 px text in one diagram and 20 px in another was
    // the other half of "it looks too small": the sizes disagreed.
    expect(
      diagramScale({ hostWidth: DESKTOP, naturalWidth: 459, smallestLabelPx: 14 }),
    ).toBe(1);
    expect(
      diagramScale({ hostWidth: DESKTOP, naturalWidth: 120, smallestLabelPx: 16 }),
    ).toBe(1);
  });

  it('DIAG-23 · it fits the column whenever that stays above the floor', () => {
    // A diagram only slightly wider than the column is shown WHOLE, not
    // floored and scrolled: fit 0.9 with 16 px labels is 14.4 px, legible.
    const scale = diagramScale({
      hostWidth: DESKTOP,
      naturalWidth: Math.round(DESKTOP / 0.9),
      smallestLabelPx: 16,
    });
    expect(scale).toBeCloseTo(0.9, 2);
    expect(16 * scale).toBeGreaterThan(LABEL_FLOOR_PX);
  });

  it('DIAG-24 · the floor comes from the MEASURED label, not from a constant', () => {
    // Two diagrams of identical width but different label sizes must not get
    // the same scale: a 10 px label needs more room than a 16 px one. This is
    // why the floor is computed from the rendered SVG rather than assumed —
    // edge labels, cluster titles and ER attribute rows are each set
    // differently per diagram type.
    const wide = { hostWidth: DESKTOP, naturalWidth: 2000 };
    const big = diagramScale({ ...wide, smallestLabelPx: 16 });
    const small = diagramScale({ ...wide, smallestLabelPx: 10 });
    expect(small).toBeGreaterThan(big);
    expect(16 * big).toBeGreaterThanOrEqual(LABEL_FLOOR_PX);
  });

  it('DIAG-24a · the floor never scales a diagram UP to reach 12 px', () => {
    // The stated boundary of the policy, pinned so it cannot drift by
    // accident: the contract is "never SHRINK a label below 12 px", not
    // "never SHOW one below 12 px". A source whose own labels are smaller
    // than the floor renders at 1:1 and stays small, because scaling up is
    // what made one answer carry 8 px text in one diagram and 20 px in
    // another. It does not arise in the measured corpus — mermaid's own
    // smallest label is 14 px (erDiagram) and 16 px everywhere else.
    const scale = diagramScale({
      hostWidth: DESKTOP,
      naturalWidth: 300,
      smallestLabelPx: 8,
    });
    expect(scale).toBe(1);
    expect(8 * scale).toBeLessThan(LABEL_FLOOR_PX);
  });

  it('DIAG-24b · degenerate inputs fall back to 1 rather than to 0 or NaN', () => {
    for (const bad of [
      { hostWidth: 668, naturalWidth: 0, smallestLabelPx: 16 },
      { hostWidth: 0, naturalWidth: 500, smallestLabelPx: 16 },
      { hostWidth: 668, naturalWidth: Number.NaN, smallestLabelPx: 16 },
    ]) {
      expect(diagramScale(bad)).toBe(1);
    }
    // No labels to measure: fit the column, but never blow up.
    const s = diagramScale({ hostWidth: 668, naturalWidth: 2000, smallestLabelPx: 0 });
    expect(s).toBeCloseTo(0.334, 2);
  });

  it('DIAG-24c · smallestLabelPx survives a missing or empty SVG', () => {
    expect(smallestLabelPx(null)).toBe(0);
    expect(smallestLabelPx(undefined)).toBe(0);
  });
});

// ------------------------------------------------------------------ DIAG-25

describe('DIAG-25 · the tokens are the source of truth', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  /**
   * Stand in for a document root carrying custom properties.
   *
   * This file is a `.test.ts` and so runs in the node environment, per the
   * split vitest.config.ts describes. Stubbing `window.getComputedStyle` is
   * not a weaker test here — it is a stricter one: it pins the exact token
   * NAMES the theme asks the stylesheet for, so a typo in one of them fails
   * rather than falling back to the literal and passing.
   */
  function withTokens(tokens: Record<string, string>) {
    vi.stubGlobal('window', {
      getComputedStyle: () => ({
        getPropertyValue: (name: string) => tokens[name] ?? '',
      }),
    });
    vi.stubGlobal('document', { documentElement: {} });
  }

  it('a --ts-diagram-* token overrides the literal fallback', () => {
    // Same contract as lib/chartTheme.ts: globals.css wins, the literals are
    // the SSR/test safety net.
    withTokens({
      '--ts-diagram-service-fill': '#123456',
      '--ts-diagram-service-stroke': '#654321',
      '--ts-diagram-ink': '#abcdef',
    });
    const paints = resolveRolePaints('dark', {} as Element);
    expect(paints.service.fill).toBe('#123456');
    expect(paints.service.stroke).toBe('#654321');
    expect(paints.service.ink).toBe('#abcdef');
    // …and an unset token still falls back rather than emptying.
    expect(paints.store.fill).toMatch(/^#[0-9a-f]{6}$/i);
  });

  it('every role and every channel is reachable from a token', () => {
    // The whole point of the tokens: a designer changing globals.css must be
    // able to move ALL of the diagram palette without touching TypeScript.
    const tokens: Record<string, string> = { '--ts-diagram-ink': '#010101' };
    for (const role of DIAGRAM_ROLES) {
      tokens[`--ts-diagram-${role}-fill`] = '#020202';
      tokens[`--ts-diagram-${role}-stroke`] = '#030303';
    }
    withTokens(tokens);
    const paints = resolveRolePaints('light', {} as Element);
    for (const role of DIAGRAM_ROLES) {
      expect(paints[role].fill).toBe('#020202');
      expect(paints[role].stroke).toBe('#030303');
      expect(paints[role].ink).toBe('#010101');
    }
  });

  it('a junk token value is ignored in favour of the literal', () => {
    // `var(--x)` would resolve to nothing inside the SVG mermaid builds, and
    // an empty string would paint black-on-black.
    withTokens({
      '--ts-diagram-service-fill': 'var(--something-else)',
      '--ts-diagram-service-stroke': '   ',
    });
    const paints = resolveRolePaints('dark', {} as Element);
    expect(paints.service.fill).toMatch(/^#[0-9a-f]{6}$/i);
    expect(paints.service.stroke).toMatch(/^#[0-9a-f]{6}$/i);
  });

  it('DIAG-25b · the literals equal the tokens they mirror, value for value', () => {
    /**
     * The runtime reads globals.css; every test in this file reads the
     * literals. So a drift between them is invisible in BOTH directions: the
     * suite would keep proving contrast on values the browser never paints.
     *
     * That is not hypothetical — this file's three stroke moves had to be made
     * twice, once in each place, and the ONLY thing that would have caught a
     * half-done edit is this test. It parses the two token blocks out of the
     * stylesheet by the `--ts-diagram-ink` that opens each (dark first, then
     * the `html.light` override) rather than by line number.
     */
    const css = readFileSync(
      fileURLToPath(new URL('../app/globals.css', import.meta.url)),
      'utf8',
    );
    const blocks = css.split('--ts-diagram-ink:').slice(1);
    expect(blocks, 'one --ts-diagram-ink block per theme').toHaveLength(2);
    const modes: ThemeMode[] = ['dark', 'light'];
    modes.forEach((mode, i) => {
      const block = blocks[i];
      const ink = /^\s*(#[0-9a-f]{6})\s*;/i.exec(block)?.[1];
      const paints = resolveRolePaints(mode);
      expect(ink?.toLowerCase(), `${mode} --ts-diagram-ink`).toBe(
        paints.service.ink.toLowerCase(),
      );
      for (const role of DIAGRAM_ROLES) {
        for (const channel of ['fill', 'stroke'] as const) {
          const token = new RegExp(
            `--ts-diagram-${role}-${channel}:\\s*(#[0-9a-f]{6})\\s*;`,
            'i',
          ).exec(block)?.[1];
          expect(token?.toLowerCase(), `${mode} --ts-diagram-${role}-${channel}`).toBe(
            paints[role][channel].toLowerCase(),
          );
        }
      }
    });
  });

  it('resolves to the literals with no DOM at all', () => {
    const paints = resolveRolePaints('light', null);
    for (const role of DIAGRAM_ROLES) {
      expect(paints[role].fill).toMatch(/^#[0-9a-f]{6}$/i);
      expect(paints[role].stroke).toMatch(/^#[0-9a-f]{6}$/i);
    }
  });
});

// ------------------------------------------------------- DIAG-26 … DIAG-32

/**
 * DIAG-26…32 — the CATEGORICAL palette.
 *
 * The role palette answers "what is this node"; this one answers only "a
 * different one" — pie slices, timeline and journey sections, mindmap and
 * gitGraph branches, xychart series, treemap tiles.
 *
 * It exists because mermaid's `base` theme DERIVES every one of those families
 * from `primaryColor`, and our `primaryColor` is a grey. Measured in Chromium
 * 11.17 before this palette: a pie's slices came out rgb(51,56,61),
 * rgb(25,28,31), rgb(28,31,33) and rgb(2,3,3) — ΔL* 3.89 from the #1e1e1e
 * card at 1.08:1, worst pair ΔL* 0.77; a timeline's three sections were all
 * rgb(0,0,0); a mindmap was rgb(0,0,0); both xychart series were mermaid's
 * stock cream rgb(255,244,221), which on the light card is ΔL* 0.30.
 *
 * Same discipline as the rest of this file: computed, never asserted as hex.
 */
describe('DIAG-26…32 · the categorical palette', () => {
  const CARD = SURFACE;

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  function lstar(hex: string): number {
    const y = luminance(hexToRgb(hex));
    return y > 0.008856 ? 116 * y ** (1 / 3) - 16 : 903.3 * y;
  }

  /** OKLab ΔE ×100 — the instrument the data-viz validator uses. */
  function oklab(hex: string): [number, number, number] {
    const [r, g, b] = hexToRgb(hex).map(toLinear);
    const l = Math.cbrt(0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b);
    const m = Math.cbrt(0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b);
    const s = Math.cbrt(0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b);
    return [
      0.2104542553 * l + 0.793617785 * m - 0.0040720468 * s,
      1.9779984951 * l - 2.428592205 * m + 0.4505937099 * s,
      0.0259040371 * l + 0.7827717662 * m - 0.808675766 * s,
    ];
  }

  function deltaE(a: string, b: string): number {
    const [x, y, z] = oklab(a);
    const [p, q, r] = oklab(b);
    return 100 * Math.hypot(x - p, y - q, z - r);
  }

  function vars(mode: ThemeMode): Record<string, unknown> {
    return mermaidTheme(mode).themeVariables as Record<string, unknown>;
  }

  function slots(mode: ThemeMode): string[] {
    const v = vars(mode);
    return Array.from({ length: CATEGORICAL_SLOTS }, (_, i) => String(v[`cScale${i}`]));
  }

  it.each(MODES)(
    'DIAG-26 · %s states every categorical key mermaid would otherwise derive',
    (mode) => {
      const v = vars(mode);
      // Leave ONE of these unset and mermaid rotates the grey primaryColor
      // into it. theme-base derives cScale0…11, pie1…12, git0…7, fillType0…7,
      // venn1…8 and quadrant1…4Fill from primaryColor.
      for (let i = 0; i < CATEGORICAL_SLOTS; i += 1) {
        expect(v[`cScale${i}`], `${mode} cScale${i}`).toMatch(/^#[0-9a-f]{6}$/i);
        expect(v[`pie${i + 1}`], `${mode} pie${i + 1}`).toMatch(/^#[0-9a-f]{6}$/i);
        expect(v[`cScaleLabel${i}`], `${mode} cScaleLabel${i}`).toBeTruthy();
      }
      for (let i = 0; i < 8; i += 1) {
        expect(v[`git${i}`], `${mode} git${i}`).toMatch(/^#[0-9a-f]{6}$/i);
        expect(v[`fillType${i}`], `${mode} fillType${i}`).toMatch(/^#[0-9a-f]{6}$/i);
        expect(v[`venn${i + 1}`], `${mode} venn${i + 1}`).toMatch(/^#[0-9a-f]{6}$/i);
      }
      // journey leaves `.actor-N` unfilled when actor0…5 are unset and falls
      // back to its own hard-coded cyan / lawngreen / darkseagreen.
      for (let i = 0; i < 6; i += 1) {
        expect(v[`actor${i}`], `${mode} actor${i}`).toMatch(/^#[0-9a-f]{6}$/i);
      }
    },
  );

  it.each(MODES)('DIAG-27 · %s · every slot clears 3:1 on the card', (mode) => {
    for (const [i, hex] of slots(mode).entries()) {
      expect(contrast(hex, CARD[mode]), `${mode} slot ${i + 1} (${hex})`).toBeGreaterThanOrEqual(3);
    }
  });

  it.each(MODES)('DIAG-28 · %s · the on-slice ink clears AA on every slot', (mode) => {
    // mermaid paints a pie's percentage ON the slice with ONE colour for all
    // of them (`pieSectionTextColor`), so the whole set has to take the same
    // ink. That is what sets the lightness band.
    const ink = String(vars(mode).pieSectionTextColor);
    for (const [i, hex] of slots(mode).entries()) {
      expect(contrast(hex, ink), `${mode} slot ${i + 1} (${hex}) on ${ink}`).toBeGreaterThanOrEqual(4.5);
    }
  });

  it.each(MODES)('DIAG-29 · %s · twelve slots are twelve distinct paints', (mode) => {
    const set = slots(mode);
    expect(new Set(set.map((s) => s.toLowerCase())).size).toBe(CATEGORICAL_SLOTS);
  });

  it.each(MODES)('DIAG-30 · %s · neighbouring slots stay apart, and under CVD', (mode) => {
    // The data-viz gates on the default (adjacent) pairlist: normal-vision
    // ΔE ≥ 15 is a hard floor, CVD ΔE ≥ 6 the floor and ≥ 8 the target.
    const set = slots(mode);
    for (let i = 0; i < set.length - 1; i += 1) {
      const pair = `${mode} slots ${i + 1}/${i + 2} (${set[i]}, ${set[i + 1]})`;
      expect(deltaE(set[i], set[i + 1]), pair).toBeGreaterThanOrEqual(15);
      for (const kind of ['protan', 'deutan'] as const) {
        expect(cvdDistance(set[i], set[i + 1], kind), `${pair} ${kind}`).toBeGreaterThan(0);
      }
    }
  });

  it.each(MODES)('DIAG-31 · %s · nothing categorical is green', (mode) => {
    // Same band tests/accent-palette.test.ts polices. It is also why the set
    // is six hues and not twelve: the green/teal/aqua arc is simply gone.
    for (const hex of [...slots(mode), ...hexes(vars(mode).xyChart)]) {
      expect(isGreenish(hex), `${mode} ${hex} is in the green band`).toBe(false);
    }
  });

  it.each(MODES)('DIAG-32 · %s · xychart and quadrant stop using mermaid stock', (mode) => {
    const v = vars(mode);
    const plot = String((v.xyChart as Record<string, string>).plotColorPalette).split(',');
    expect(plot).toHaveLength(8);
    // mermaid's own default opens with #FFF4DD, which is ΔL* 0.30 from the
    // light card. Ours are the first eight categorical slots, in order.
    expect(plot.map((s) => s.toLowerCase())).toEqual(
      slots(mode).slice(0, 8).map((s) => s.toLowerCase()),
    );

    // The four quadrant fills are BACKGROUNDS, so they are judged on ΔL*
    // rather than on hue: theme-base steps them 5 rgb units apart off
    // primaryColor, which measured a worst pair of ΔL* 2.19.
    const quads = [1, 2, 3, 4].map((n) => String(v[`quadrant${n}Fill`]));
    const card = lstar(CARD[mode]);
    const ls = quads.map(lstar);
    for (const [i, l] of ls.entries()) {
      expect(Math.abs(l - card), `${mode} quadrant${i + 1} vs card`).toBeGreaterThan(5);
    }
    for (let i = 0; i < ls.length - 1; i += 1) {
      expect(Math.abs(ls[i + 1] - ls[i]), `${mode} quadrant ${i + 1}/${i + 2}`).toBeGreaterThan(5);
    }
  });

  it.each(MODES)('DIAG-32b · %s · a pie slice is not made translucent', (mode) => {
    // pieOpacity defaults to 0.7, which washes every slice back toward the
    // card and undoes a validated palette. The hairline between slices is the
    // card colour, not mermaid's literal 'black'.
    const v = vars(mode);
    expect(Number(v.pieOpacity)).toBe(1);
    expect(String(v.pieStrokeColor).toLowerCase()).toBe(CARD[mode]);
    expect(String(v.pieOuterStrokeColor).toLowerCase()).toBe(CARD[mode]);
  });

  it('DIAG-32c · the tokens are the source of truth for the set too', () => {
    const tokens: Record<string, string> = {
      '--ts-diagram-cat-1': '#123456',
      '--ts-diagram-cat-ink': '#fedcba',
    };
    vi.stubGlobal('window', {
      getComputedStyle: () => ({
        getPropertyValue: (name: string) => tokens[name] ?? '',
      }),
    });
    vi.stubGlobal('document', { documentElement: {} });
    const set = resolveCategorical('dark', {} as Element);
    expect(set[0]).toBe('#123456');
    expect(categoricalInk('dark', {} as Element)).toBe('#fedcba');
    expect(set).toHaveLength(CATEGORICAL_SLOTS);
  });

  it('DIAG-32d · …and the literals carry it with no DOM at all', () => {
    for (const mode of MODES) {
      const set = resolveCategorical(mode, null);
      expect(set).toHaveLength(CATEGORICAL_SLOTS);
      for (const hex of set) expect(hex).toMatch(/^#[0-9a-f]{6}$/i);
      expect(categoricalInk(mode, null)).toMatch(/^#[0-9a-f]{6}$/i);
    }
  });
});

// ------------------------------------------------------------- DIAG-33…33f

/**
 * DIAG-33 — a misplaced role costs the colour, never the diagram.
 *
 * The orchestrator's prompt now teaches the `:::role` form (2026-09-27,
 * `DIAGRAM_ROLES` in orchestrator/app/engines/__init__.py), which is what makes
 * this reachable at all: a model that reaches for a role in the WRONG diagram
 * type used to lose the whole picture. Measured today, Chromium 153 /
 * mermaid 11.17, against an esbuild bundle of the real <MermaidBlock>:
 * `U:::external` in a sequenceDiagram threw "Parse error on line 6 … got
 * 'TXT'" and the block showed "Couldn't render this diagram — showing the
 * source"; `class U external` threw the same error with "got 'NEWLINE'". With
 * the retry the same two sources draw a 450x267 sequence diagram.
 *
 * `withoutRoleApplications` is the RETRY source, not a filter: MermaidBlock
 * only asks for it after a render has thrown, so none of these removals can
 * touch a diagram that works.
 */
describe('DIAG-33 · a misplaced role costs the colour, never the diagram', () => {
  it('DIAG-33 · a role application in a sequenceDiagram goes, statement and all', () => {
    // Stripping only the `:::external` would leave a bare `U`, which is the
    // SAME parse error — measured: "got 'NEWLINE'" instead of "got 'TXT'".
    const src = [
      'sequenceDiagram',
      '  participant U as Browser',
      '  participant A as Orchestrator',
      '  U->>A: ask',
      '  A-->>U: answer',
      '  U:::external',
    ].join('\n');
    const out = withoutRoleApplications(src);
    expect(out).not.toContain(':::');
    expect(out.split('\n')).toEqual([
      'sequenceDiagram',
      '  participant U as Browser',
      '  participant A as Orchestrator',
      '  U->>A: ask',
      '  A-->>U: answer',
    ]);
  });

  it('DIAG-33b · a `class X <role>` statement goes; an unknown name stays', () => {
    // Narrow on purpose. Outside the flowchart family a line is only a
    // STATEMENT in some grammars: in a mindmap or a timeline the words are
    // content, and a node labelled "class diagram" must survive a retry.
    expect(withoutRoleApplications('sequenceDiagram\n  A->>B: hi\n  class A external'))
      .toBe('sequenceDiagram\n  A->>B: hi');
    const mine = 'sequenceDiagram\n  A->>B: hi\n  class A mine';
    expect(withoutRoleApplications(mine)).toBe(mine);
  });

  it('DIAG-33c · a `:::` inside a quoted label is content, not syntax', () => {
    const src = 'pie title Latency\n  "p99 :::slow" : 40\n  "p50" : 60';
    expect(withoutRoleApplications(src)).toBe(src);
  });

  it('DIAG-33d · a role riding a semicolon goes with its own statement only', () => {
    const out = withoutRoleApplications('erDiagram\n  A ||--o{ B : has; A:::store');
    expect(out).toBe('erDiagram\n  A ||--o{ B : has');
  });

  it('DIAG-33e · a mindmap keeps a node whose label reads like a statement', () => {
    const src = 'mindmap\n  root((Docs))\n    class diagram\n    sequence diagram';
    expect(withoutRoleApplications(src)).toBe(src);
  });

  it('DIAG-33f · a source with no roles at all comes back byte-for-byte', () => {
    for (const src of [
      'sequenceDiagram\n  A->>B: hi',
      'flowchart LR\n  A[One] --> B[Two]',
      'timeline\n  title Rollout\n  2026 : first',
      '',
    ]) {
      expect(withoutRoleApplications(src)).toBe(src);
    }
  });

  it('DIAG-33g · the flowchart family never reaches the retry in the first place', () => {
    // The colour lives where classDefs are legal; prepareDiagramSource is
    // unchanged there, and the block only asks for the retry after a throw.
    const flow = 'flowchart LR\n  A["Gateway"]:::service --> B["Queue"]:::store';
    expect(prepareDiagramSource(flow, 'dark')).toContain(':::service');
    expect(prepareDiagramSource(flow, 'dark')).toContain('classDef service');
    expect(acceptsClassDefs(flow)).toBe(true);
  });
});

// -------------------------------------------------------------- DIAG-35…35n

/**
 * DIAG-35 — the PREAMBLE is the other half of the colour ban.
 *
 * `fix/diagram-roles` said its colour and style ban was "enforced in CODE, not
 * by asking". It was not: mermaid takes an in-source config override from TWO
 * channels, a `%%{init: …}%%` directive AND a YAML frontmatter `config:` block,
 * and only the first was ever removed. Measured today against the real bundle
 * (Chromium 153.0.8010.36, mermaid 11.17.0, esbuild bundle of the real
 * <MermaidBlock>, the same way the role colours were verified):
 *
 *     ---
 *     config:
 *       themeCSS: |
 *         .node rect { fill: #ff0000 !important; stroke: #00ff00 !important;
 *                      stroke-width: 6px !important; }
 *         .flowchart-link { stroke: #ff00ff !important; stroke-width: 5px !important; }
 *     ---
 *     flowchart TD
 *       SVC[Gateway]:::service --> PLAIN[Plain node]
 *
 * drew PLAIN at fill rgb(255,0,0), stroke rgb(0,255,0), stroke-width 6px and
 * its edge at stroke rgb(255,0,255), 5px — against rgb(51,56,61) /
 * rgb(139,148,158) / 1px and rgb(154,163,173) / 1px for the same diagram with
 * the block removed. A `pie` and a `sequenceDiagram` get no classDefs at all,
 * and there EVERY slice and EVERY actor rect went rgb(255,0,0).
 *
 * These tests pin the source the renderer is handed. The colour half — that
 * Chromium then paints the theme's own values — is proved by rendering the
 * corpus in a real browser, exactly as the header of this file says.
 */
describe('DIAG-35 · the preamble cannot carry a display override', () => {
  const FLOW = 'flowchart TD\n  SVC[Gateway]:::service --> PLAIN[Store]';

  const OVERRIDES: [string, string][] = [
    ['themeCSS (block scalar)', 'config:\n  themeCSS: |\n    .node rect { fill: #ff0000 !important; }'],
    ['themeCSS (flow map)', 'config: { themeCSS: ".node rect { fill: #ff0000 !important }" }'],
    ['themeCSS (display:none)', 'config:\n  themeCSS: ".flowchart-link { display: none !important }"'],
    ['theme', 'config:\n  theme: default'],
    ['themeVariables', 'config:\n  themeVariables:\n    primaryColor: "#ff0000"'],
    ['securityLevel', 'config:\n  securityLevel: loose'],
    ['htmlLabels', 'config:\n  htmlLabels: true'],
    ['flowchart.htmlLabels', 'config:\n  flowchart:\n    htmlLabels: true'],
    ['look', 'config:\n  look: handDrawn'],
    ['layout', 'config:\n  layout: elk'],
    ['fontFamily', 'config:\n  fontFamily: "Comic Sans MS"'],
    ['altFontFamily', 'config:\n  altFontFamily: "Comic Sans MS"'],
    ['darkMode', 'config:\n  darkMode: false'],
    ['displayMode', 'displayMode: compact'],
  ];

  it.each(OVERRIDES)('DIAG-35 · a frontmatter %s block is dropped whole', (_name, block) => {
    const out = sanitizeDiagramSource(`---\n${block}\n---\n${FLOW}`);
    expect(out).not.toContain('---');
    expect(out).not.toContain('config');
    expect(out).not.toContain('#ff0000');
    // …and the diagram itself is untouched.
    expect(out).toContain('SVC[Gateway]:::service --> PLAIN[Store]');
    expect(diagramHead(out)).toBe('flowchart');
  });

  it('DIAG-35b · an INDENTED frontmatter block is frontmatter here too', () => {
    // Mermaid captures the indent and accepts the block; a reader that only
    // looks at column 0 misses it. Measured: this painted rgb(255,0,0).
    const out = sanitizeDiagramSource(
      '  ---\n  config:\n    themeCSS: ".node rect { fill: #ff0000 !important }"\n  ---\n  flowchart TD\n    A-->B',
    );
    expect(out).not.toContain('#ff0000');
    expect(out).not.toContain('config');
    expect(out).toContain('A-->B');
  });

  it('DIAG-35c · a CRLF frontmatter block is frontmatter here too', () => {
    // mermaid's cleanupText normalises line endings BEFORE it looks, so this
    // guard has to as well. Measured: this painted rgb(255,0,0).
    const out = sanitizeDiagramSource(
      '---\r\nconfig:\r\n  themeCSS: ".node rect { fill: #ff0000 !important }"\r\n---\r\nflowchart TD\r\n  A-->B',
    );
    expect(out).not.toContain('#ff0000');
    expect(out).toContain('A-->B');
  });

  it('DIAG-35d · a `title:` line is the ONE key that survives', () => {
    const out = sanitizeDiagramSource(`---\ntitle: Request path\n---\n${FLOW}`);
    expect(out).toContain('title: Request path');
    expect(diagramHead(out)).toBe('flowchart');
    expect(prepareDiagramSource(`---\ntitle: Request path\n---\n${FLOW}`, 'dark')).toContain(
      'classDef service',
    );
  });

  it.each([
    ['a second key beside it', '---\ntitle: Kept\nconfig:\n  themeCSS: "x"\n---'],
    ['a block scalar', '---\ntitle: |\n  config:\n---'],
    ['a flow map value', '---\ntitle: {config: {themeCSS: "x"}}\n---'],
    ['an anchor', '---\ntitle: &a x\n---'],
    ['a tag', '---\ntitle: !!str x\n---'],
    ['an empty value', '---\ntitle:\n---'],
  ])('DIAG-35e · a title with %s takes the whole block down with it', (_name, block) => {
    const out = sanitizeDiagramSource(`${block}\n${FLOW}`);
    expect(out).not.toContain('title');
    expect(out).not.toContain('config');
    expect(out).toContain('SVC[Gateway]:::service --> PLAIN[Store]');
  });

  /**
   * The two SECOND-ORDER shapes. Both exist only because this guard rewrites
   * the text, and both were reproduced against a single-pass version of it.
   */
  it('DIAG-35f · dropping one block does not PROMOTE the next one', () => {
    // Mermaid reads only the first block, so a second one is a parse error —
    // until the guard removes the first and the second becomes the first.
    const out = sanitizeDiagramSource(
      '---\nconfig:\n  look: handDrawn\n---\n---\nconfig:\n  themeCSS: ".node rect { fill: #ff0000 !important }"\n---\n' +
        FLOW,
    );
    expect(out).not.toContain('themeCSS');
    expect(out).not.toContain('#ff0000');
    expect(out).not.toContain('---');
    expect(out).toContain('SVC[Gateway]:::service --> PLAIN[Store]');
  });

  it('DIAG-35g · a directive above a block does not PROMOTE it either', () => {
    // Mermaid extracts frontmatter BEFORE directives, so this block is body to
    // mermaid and the source does not parse. Strip the directive and trim the
    // blank line it leaves — which the sanitiser does — and the block lands at
    // column 0 of line 1, alive.
    const out = sanitizeDiagramSource(
      '%%{init: {"maxTextSize": 1000}}%%\n---\nconfig:\n  themeCSS: ".node rect { fill: #ff0000 !important }"\n---\n' +
        FLOW,
    );
    expect(out).not.toContain('themeCSS');
    expect(out).not.toContain('---');
    expect(diagramHead(out)).toBe('flowchart');
  });

  it('DIAG-35h · a kept title stops the promotion, and nothing behind it is config', () => {
    const src =
      '---\ntitle: Kept\n---\n---\nconfig:\n  themeCSS: "x"\n---\n' + FLOW;
    const out = sanitizeDiagramSource(src);
    // The title block is kept, so mermaid stops there too and the rest is
    // body — a parse error, which the block shows as source. Never config.
    expect(out.startsWith('---\ntitle: Kept\n---\n')).toBe(true);
    expect(guardDiagramSource(src).refusal).toBe('');
  });

  /**
   * The one construct that is REFUSED rather than stripped. A `}%%` inside a
   * directive's own string value ends the balanced strip early and leaves a
   * fragment in the middle of a statement. Dropping the fragment's line can
   * delete a real `A-->B`; keeping it draws whatever it parses as. A wrong
   * picture is worse than no picture, so the source is refused whole.
   */
  it('DIAG-35i · a directive with `}%%` in its own value refuses the source', () => {
    const src = '%%{init: {"themeCSS": "a}%% .node rect{fill:#ff0000}"}}%%\n' + FLOW;
    expect(guardDiagramSource(src).refusal).toMatch(/malformed %%\{…\}%% directive/);
    expect(diagramRefusal(src)).toMatch(/malformed/);
    // A refused source gets no classDefs: they are the app's own text and
    // would read as the author's under a refusal notice.
    expect(prepareDiagramSource(src, 'dark')).not.toContain('classDef service');
  });

  it('DIAG-35j · every source the guard accepts carries no refusal', () => {
    for (const src of [
      FLOW,
      `---\ntitle: Kept\n---\n${FLOW}`,
      `---\nconfig:\n  themeCSS: "x"\n---\n${FLOW}`,
      "%%{init: {'theme':'default'}}%%\n" + FLOW,
      'sequenceDiagram\n  A->>B: hi',
      '',
    ]) {
      expect(diagramRefusal(src), JSON.stringify(src)).toBe('');
    }
  });

  it('DIAG-35k · a `---` pair that is NOT frontmatter to mermaid stays body', () => {
    // Mermaid's regex needs a line AFTER the closing `---`, so a trailing
    // block is diagram text. Deleting it would be deleting the author's body.
    const src = 'flowchart TD\n  A-->B\n---\nconfig:\n  themeCSS: "x"\n---';
    expect(guardDiagramSource(src).code).toBe(src);
  });

  it('DIAG-35l · an ordinary source still reaches the Code tab unchanged', () => {
    for (const src of [
      FLOW,
      'sequenceDiagram\n  A->>B: hi\n  B-->>A: ok',
      'pie title Split\n  "A" : 50\n  "B" : 50',
      'timeline\n  title Rollout\n  2026 : first',
    ]) {
      expect(sanitizeDiagramSource(src)).toBe(src);
    }
  });

  /**
   * The second line of defence. `secure` is mermaid's own key blocklist and it
   * is applied to DIRECTIVES only — never to what we pass `initialize` — so
   * listing a key this file sets itself costs nothing and buys the guarantee a
   * third override channel we have not met cannot restyle the diagram.
   */
  it.each([
    'themeCSS',
    'htmlLabels',
    'flowchart',
    'look',
    'layout',
    'fontFamily',
    'altFontFamily',
    'darkMode',
  ])('DIAG-35m · `%s` is secured in the mermaid config too', (key) => {
    expect(SECURE_KEYS).toContain(key);
    for (const mode of MODES) {
      expect(mermaidTheme(mode).secure).toEqual(expect.arrayContaining([key]));
    }
  });

  it('DIAG-35n · securing those keys does not blank our own config', () => {
    // `secure` never runs over `initialize`, so every value this file declares
    // has to still be there — the regression this test exists to catch is
    // someone "fixing" the list by also filtering our own theme.
    for (const mode of MODES) {
      const cfg = mermaidTheme(mode);
      expect(cfg.theme).toBe('base');
      expect(cfg.htmlLabels).toBe(false);
      expect(cfg.flowchart.htmlLabels).toBe(false);
      expect(cfg.securityLevel).toBe('strict');
      expect(cfg.fontFamily).toContain('IBM Plex Sans');
      expect(Object.keys(cfg.themeVariables).length).toBeGreaterThan(40);
    }
  });
});

// -------------------------------------------------------------- DIAG-37…37j

/**
 * DIAG-37 — the holes DIAG-35 left, each one measured in a real browser before
 * it was closed.
 *
 * DIAG-35 shipped with the guard's reach one character short of the sanitiser's
 * own trim, with the statement pass able to promote a block the guard had
 * already walked past, with a pass cap that handed a live block over on
 * exhaustion, with a refusal that fired on sources carrying no directive at
 * all, and with three diagram types whose paint channel the statement ban never
 * named. All five were reproduced on the commit before this one with
 * Chromium 153.0.8010.36 + mermaid 11.17.0 and an esbuild bundle of the real
 * <MermaidBlock>, and against mermaid's OWN `frontMatterRegex` + js-yaml over
 * `prepareDiagramSource`'s output, which answers the only question that
 * matters: what does mermaid read out of the string we hand it?
 *
 * Load average 3.1-15.7 during the measurements, 2026-09-28.
 */
describe('DIAG-37 · the preamble guard reaches as far as the trim does', () => {
  const FLOW = 'flowchart TD\n  SVC[Gateway]:::service --> PLAIN[Store]';
  const BLOCK =
    '---\nconfig:\n  themeCSS: ".node rect { fill: #ff0000 !important }"\n---\n';

  /**
   * `\s` minus `[ \t\n\r]` is exactly this set. Each one is a line the
   * sanitiser's final `/^\s*\n/` deletes, so each one is a line the guard's own
   * trim has to delete FIRST — otherwise the block below it is invisible to
   * `FRONTMATTER_RE` (whose `([^\S\n\r]*)` indent cannot span a newline), the
   * trim promotes it to column 0, and mermaid reads it.
   *
   * Measured on the commit before this one: the `c4:` form of this block moved
   * `c4.width` 216 -> 400 and `personFontSize` 14 -> 40 and redrew a C4 diagram
   * at 700x724 px with 40 px type, inside a chat answer, from ONE preamble
   * character.
   */
  it.each([
    ['NBSP', ' '],
    ['form feed', '\f'],
    ['vertical tab', '\v'],
    ['BOM', '﻿'],
    ['LINE SEPARATOR', ' '],
    ['PARAGRAPH SEPARATOR', ' '],
    ['OGHAM SPACE MARK', ' '],
    ['EN QUAD', ' '],
    ['HAIR SPACE', ' '],
    ['NARROW NO-BREAK SPACE', ' '],
    ['MEDIUM MATHEMATICAL SPACE', ' '],
    ['IDEOGRAPHIC SPACE', '　'],
  ])('DIAG-37 · a %s line above a block does not smuggle it through', (_name, ws) => {
    const out = sanitizeDiagramSource(`${ws}\n${BLOCK}${FLOW}`);
    expect(out).not.toContain('config');
    expect(out).not.toContain('themeCSS');
    expect(out).not.toContain('---');
    expect(out).not.toContain('#ff0000');
    expect(diagramHead(out)).toBe('flowchart');
  });

  /**
   * The same set, against the EXPORTED guard on its own.
   *
   * `sanitizeDiagramSource` alternates the guard with the statement pass to a
   * fixed point, and that alternation happens to catch these too — the
   * statement pass's own trim deletes the whitespace line and the next guard
   * round then sees the block. That is a second line of defence, not the fix:
   * `guardDiagramSource` is an exported function whose whole contract is "the
   * returned code carries no live config", and it has to hold that contract by
   * itself. Narrow its trim back to `[ \t]` and DIAG-37 still passes while this
   * fails, which is exactly the discrimination the one-character fix needs.
   */
  it.each([
    ['NBSP', '\u00a0'],
    ['form feed', '\f'],
    ['BOM', '\ufeff'],
    ['IDEOGRAPHIC SPACE', '\u3000'],
  ])('DIAG-37a · the guard ALONE strips a block hidden by a %s line', (_name, ws) => {
    const guarded = guardDiagramSource(`${ws}\n${BLOCK}${FLOW}`);
    expect(guarded.refusal).toBe('');
    expect(guarded.code).not.toContain('config');
    expect(guarded.code).not.toContain('themeCSS');
    expect(guarded.code).not.toContain('---');
    expect(guarded.code.startsWith('flowchart TD')).toBe(true);
  });

  it('DIAG-37b · a plain-space line is still covered, and the body survives', () => {
    const out = sanitizeDiagramSource(` \n \t \n${BLOCK}${FLOW}`);
    expect(out).not.toContain('config');
    expect(out).toContain('SVC[Gateway]:::service --> PLAIN[Store]');
  });

  /**
   * The promotion the guard cannot see, because the guard runs FIRST.
   * `FRONTMATTER_RE` is anchored at `^`, so a statement on line 1 hides the
   * block; the statement pass then empties line 1, the trim deletes it, and the
   * block lands at column 0 alive. Measured with mermaid's own regex on the
   * commit before this one: `config: [themeCSS]`, and `config: [c4]` for the
   * `c4:` form. Only `looksRenderable` kept mermaid away, by reading `classdef`
   * as the head and refusing to render anything at all — a permanent
   * "Rendering the diagram…" and a hole the moment that check learns to look
   * past a preamble.
   */
  it.each([
    ['classDef', 'classDef zz fill:#fff'],
    ['style', 'style A fill:#fff'],
    ['linkStyle', 'linkStyle 0 stroke:#f00'],
    ['click', 'click A "x"'],
    ['classDef then a NBSP line', 'classDef zz fill:#fff\n '],
    ['a `;`-terminated classDef', 'classDef zz fill:#fff;'],
  ])('DIAG-37c · a stripped %s statement above a block does not promote it', (_n, stmt) => {
    const out = sanitizeDiagramSource(`${stmt}\n${BLOCK}${FLOW}`);
    expect(out).not.toContain('config');
    expect(out).not.toContain('themeCSS');
    expect(out).not.toContain('---');
    expect(out).toContain('SVC[Gateway]:::service --> PLAIN[Store]');
  });

  /**
   * Exhausting the pass cap FAILS CLOSED. Measured on the commit before this
   * one: 65 stacked blocks left block 65 live at the head of the guard's
   * output. Such a source is not a diagram anyway — mermaid reads only the
   * first block, so the other 64 are body and a parse error — so refusing it
   * costs nothing and says something true.
   */
  it('DIAG-37d · more blocks than the cap strips is a refusal, not a pass-through', () => {
    const many = BLOCK.repeat(65);
    expect(diagramRefusal(many + FLOW)).toMatch(/stacks more preamble blocks/);
    expect(prepareDiagramSource(many + FLOW, 'dark')).not.toContain('classDef service');
  });

  it('DIAG-37e · exactly the cap is still stripped, and still drawn', () => {
    const out = sanitizeDiagramSource(BLOCK.repeat(64) + FLOW);
    expect(diagramRefusal(BLOCK.repeat(64) + FLOW)).toBe('');
    expect(out).not.toContain('config');
    expect(out).toBe(FLOW);
  });

  /**
   * The refusal must not fire on a source that never carried a directive.
   * `}%%` is not a directive to mermaid — its own `directiveRegex` needs the
   * opening `%%{` — so a label containing those three characters is an ordinary
   * diagram. Measured: this source drew two nodes in theme colours on
   * fix/diagram-roles and was refused on the commit before this one, with a
   * notice naming a construct its author never wrote.
   */
  it('DIAG-37f · a `}%%` with no directive anywhere is not refused', () => {
    for (const src of [
      'flowchart LR\n  A["50}%% done"] --> B[Next]',
      'flowchart TD\n  A{Ready?}%% decide here\n  A --> B[Go]',
      'sequenceDiagram\n  A->>B: 50}%% done',
    ]) {
      expect(diagramRefusal(src), JSON.stringify(src)).toBe('');
    }
  });

  it('DIAG-37g · the directive that DOES need refusing still refuses', () => {
    const src = '%%{init: {"themeCSS": "a}%% .node rect{fill:#ff0000}"}}%%\n' + FLOW;
    expect(diagramRefusal(src)).toMatch(/malformed %%\{…\}%% directive/);
  });

  /**
   * The three paint channels `COLOUR_DIRECTIVE` does not name. None is a
   * regression of this branch — all three painted identically before it — but
   * this branch IS the colour ban. Every value below was read off the real SVG
   * in Chromium today, on both commits.
   */
  it('DIAG-37h · a C4 `Update*Style` statement carries no colour through', () => {
    const src =
      'C4Context\n  title C4\n  Person(p, "User")\n  System(s, "Sys")\n  Rel(p, s, "uses")\n' +
      '  UpdateElementStyle(p, $bgColor="#ff0000", $fontColor="#00ff00", $borderColor="#ff00ff")\n' +
      '  UpdateRelStyle(p, s, $textColor="#ff0000", $lineColor="#00ff00")\n' +
      '  UpdateLayoutConfig($c4ShapeInRow="1", $c4BoundaryInRow="1")';
    const out = sanitizeDiagramSource(src);
    expect(out).not.toContain('#ff0000');
    expect(out).not.toContain('UpdateElementStyle');
    expect(out).not.toContain('UpdateRelStyle');
    expect(out).not.toContain('UpdateLayoutConfig');
    // The graph itself is untouched: every Person, System and Rel survives.
    expect(out).toContain('Person(p, "User")');
    expect(out).toContain('System(s, "Sys")');
    expect(out).toContain('Rel(p, s, "uses")');
    // Node `mmd-1-p` drew at computed fill rgb(255, 0, 0) before this, and at
    // rgb(8, 66, 123) after — the same value as the same diagram without the
    // statement, at the same 832x493 px and the same 23,058 ink pixels.
  });

  /**
   * `box` is STRUCTURAL — it groups actors and is closed by `end` — so the
   * statement survives with `transparent` written into the segment mermaid
   * reads as the colour. That is what makes the rule fail closed: whatever
   * follows is the title, so an unrecognised colour can only become TEXT.
   * mermaid's own `parseBoxData` falls back to `transparent` when the first
   * word is not a CSS colour and then keeps the WHOLE string as the title,
   * which is why a non-colour first word is kept here too.
   */
  it.each([
    ['rgb()', 'box rgb(255,0,0) Hot path', 'box transparent Hot path'],
    ['rgba()', 'box rgba(255, 0, 0, 0.5) Hot', 'box transparent Hot'],
    ['hsl()', 'box hsl(0, 100%, 50%) Hot', 'box transparent Hot'],
    ['oklch()', 'box oklch(0.7 0.3 20) Hot', 'box transparent Hot'],
    ['a named colour', 'box Red path', 'box transparent path'],
    ['a NON-colour first word', 'box Hot path', 'box transparent Hot path'],
    ['a word that merely starts like one', 'box reddish thing', 'box transparent reddish thing'],
    ['an already-transparent box', 'box transparent Keep', 'box transparent Keep'],
    ['a colour and nothing else', 'box red', 'box transparent'],
  ])('DIAG-37i · a sequence `box` with %s keeps its group and loses its fill', (_n, line, want) => {
    const src = `sequenceDiagram\n  ${line}\n    participant A\n    participant B\n  end\n  A->>B: hi`;
    const out = sanitizeDiagramSource(src);
    expect(out.split('\n')[1]).toBe(`  ${want}`);
    // the group is still a group, and the messages are still there
    expect(out).toContain('participant A');
    expect(out).toContain('\n  end');
    expect(out).toContain('A->>B: hi');
    expect(out).not.toContain('rgb(255,0,0)');
  });

  it('DIAG-37i2 · `box` outside a sequenceDiagram is an ordinary node id', () => {
    // A flowchart node may be called `box`. Rewriting it would delete the
    // author's graph, so the rule is gated on the diagram's own head.
    const src = 'flowchart LR\n  box[Label] --> other[Two]\n  box --> third';
    expect(sanitizeDiagramSource(src)).toBe(src);
  });

  it('DIAG-37j · a quadrantChart point keeps its coordinates and loses its paint', () => {
    const src =
      'quadrantChart\n  title Q\n  x-axis Low --> High\n  y-axis Low --> High\n' +
      '  "A": [0.7, 0.8] radius: 20, color: #ff0000, stroke-color: #00ff00, stroke-width: 6px\n' +
      '  "B": [0.2, 0.3]';
    const out = sanitizeDiagramSource(src);
    expect(out).toContain('"A": [0.7, 0.8]');
    expect(out).toContain('"B": [0.2, 0.3]');
    expect(out).not.toContain('#ff0000');
    expect(out).not.toContain('stroke-color');
    // `<circle … fill="#ff0000" stroke="#00ff00" stroke-width="6px">` before,
    // `fill="#3596f8" stroke="#3596f8" stroke-width="0px"` after — the same
    // values as the same chart with no style clause.
  });

  it('DIAG-37j2 · a quadrantChart `:::role` survives, because it is inert', () => {
    const src = 'quadrantChart\n  "A": [0.7, 0.8]:::hot\n  "B": [0.2, 0.3]';
    expect(sanitizeDiagramSource(src)).toBe(src);
  });

  it('DIAG-37k · none of this changes a diagram that carries no paint', () => {
    for (const src of [
      'C4Context\n  Person(p, "User")\n  System(s, "Sys")\n  Rel(p, s, "uses")',
      'sequenceDiagram\n  participant A\n  A->>A: hi',
      'quadrantChart\n  "A": [0.7, 0.8]\n  "B": [0.2, 0.3]',
      'flowchart LR\n  A --> B',
    ]) {
      expect(sanitizeDiagramSource(src), JSON.stringify(src)).toBe(src);
    }
  });
});
