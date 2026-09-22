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

import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  DIAGRAM_ROLES,
  LABEL_FLOOR_PX,
  SECURE_KEYS,
  acceptsClassDefs,
  diagramScale,
  mermaidTheme,
  prepareDiagramSource,
  resolveRolePaints,
  roleClassDefs,
  sanitizeDiagramSource,
  smallestLabelPx,
  type ThemeMode,
} from '../lib/mermaidTheme';
import { looksRenderable } from '../lib/mermaid';

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

  it('resolves to the literals with no DOM at all', () => {
    const paints = resolveRolePaints('light', null);
    for (const role of DIAGRAM_ROLES) {
      expect(paints[role].fill).toMatch(/^#[0-9a-f]{6}$/i);
      expect(paints[role].stroke).toMatch(/^#[0-9a-f]{6}$/i);
    }
  });
});
