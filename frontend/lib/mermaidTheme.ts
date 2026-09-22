/**
 * Mermaid theme, role palette, source sanitiser and zoom floor — pure
 * functions, no mermaid import, so they unit-test without the 1 MB bundle.
 *
 * WHY `theme: 'base'` AND NOT `'dark'`
 * -----------------------------------
 * The block used to ask for mermaid's built-in `dark` theme and then hand it a
 * 26-entry `themeVariables` block. Mermaid's packaged themes RE-DERIVE their
 * node colours from their own base after applying overrides, so the declared
 * values were decoration: measured in Chromium 11.17, a declared
 * `primaryColor: '#2f2f2f'` painted rgb(31,32,32) and a declared
 * `primaryBorderColor: '#6b6b6b'` painted rgb(204,204,204). A #1f2020 node on
 * the #1e1e1e card is 1.02:1 — ΔL* 0.88, which is no visible box at all, and
 * in a 33-shape architecture diagram every single node got that one fill.
 * That is the owner's "no colour / what is this inside the box".
 *
 * Only `theme: 'base'` makes `themeVariables` authoritative. Both modes now
 * use `base` with a complete variable set, so light stops being mermaid's
 * stock lavender-on-white (#ececff nodes, mediumpurple borders, #ffffde
 * clusters) sitting on our grey #f4f4f4 card.
 *
 * WHY THE ROLE COLOURS ARE NOT DECIDED HERE
 * -----------------------------------------
 * `DIAGRAM_ROLES` is a CLOSED four-name vocabulary shared with the
 * orchestrator (`DIAGRAM_ROLES` in orchestrator/tests/parity/normalise.py).
 * A diagram names a ROLE; it never names a colour. That is the whole colour
 * ban: role names are a vocabulary we can validate, hex values are not.
 *
 * The values live in `app/globals.css` as `--ts-diagram-*` and are resolved at
 * render time with getComputedStyle, exactly as `lib/chartTheme.ts` already
 * does for ECharts. The literals below are the SSR/test fallback and a safety
 * net, not a second source of truth — globals.css wins.
 *
 * Deliberately no teal and no green: `tests/accent-palette.test.ts` fails any
 * hex with hue 80-190 and saturation > 0.15 outside chartTheme.ts, and teal is
 * already the Records engine identity.
 */

/**
 * The closed role vocabulary. Four names, shared verbatim with the
 * orchestrator so a diagram that validates there has a classDef here.
 *
 * `model`, not `ai`. `actor` folds into `external` and `decision` is dropped;
 * both are stated costs of keeping the list to four.
 */
export const DIAGRAM_ROLES = ['service', 'store', 'model', 'external'] as const;

export type DiagramRole = (typeof DIAGRAM_ROLES)[number];
export type ThemeMode = 'dark' | 'light';

export interface RolePaint {
  fill: string;
  stroke: string;
  ink: string;
}

/**
 * Literal fallbacks, mirroring the `--ts-diagram-*` tokens in globals.css.
 *
 * Measured against the real card surfaces (#1e1e1e dark, #f4f4f4 light):
 * ink-on-fill 10.5-14.9:1, stroke-on-surface 3.21-6.46:1, worst all-pairs CVD
 * stroke separation 42.9 (protan/deutan/tritan, Brettel). The fill is a tint
 * that carries the hue (ΔL* 6.6-10.6 from the card, against the shipped 0.88);
 * the STROKE is what carries the 3:1 separation, which is why the contrast
 * floor is set on the stroke and not on the fill.
 */
const ROLE_FALLBACK: Record<ThemeMode, Record<DiagramRole, RolePaint>> = {
  dark: {
    service: { fill: '#22303f', stroke: '#2f6fb2', ink: '#ececec' },
    store: { fill: '#40321e', stroke: '#b7791f', ink: '#ececec' },
    model: { fill: '#362c4e', stroke: '#8b5cf6', ink: '#ececec' },
    external: { fill: '#462934', stroke: '#d55181', ink: '#ececec' },
  },
  light: {
    service: { fill: '#d4dfe9', stroke: '#2f6fb2', ink: '#0d0d0d' },
    store: { fill: '#eae0d2', stroke: '#b7791f', ink: '#0d0d0d' },
    model: { fill: '#ded3f0', stroke: '#6d28d9', ink: '#0d0d0d' },
    external: { fill: '#e7d6d9', stroke: '#a33a4d', ink: '#0d0d0d' },
  },
};

/** Chrome that is NOT role-coloured: surfaces, edges, default nodes, ink. */
const CHROME: Record<
  ThemeMode,
  {
    surface: string;
    nodeFill: string;
    nodeBorder: string;
    ink: string;
    inkMuted: string;
    line: string;
    clusterBkg: string;
    clusterBorder: string;
    edgeLabelBkg: string;
    noteBkg: string;
    noteBorder: string;
    altRow: string;
  }
> = {
  dark: {
    surface: '#1e1e1e',
    nodeFill: '#33383d',
    nodeBorder: '#8b949e',
    ink: '#ececec',
    inkMuted: '#c7c7c7',
    line: '#9aa3ad',
    clusterBkg: '#191c1f',
    clusterBorder: '#4a525a',
    edgeLabelBkg: '#1e1e1e',
    noteBkg: '#3a3a2e',
    noteBorder: '#6b6b52',
    altRow: '#252a2e',
  },
  light: {
    surface: '#f4f4f4',
    nodeFill: '#e4e7ea',
    nodeBorder: '#6b737b',
    ink: '#0d0d0d',
    inkMuted: '#41474d',
    line: '#5b6b7f',
    clusterBkg: '#eaedf0',
    clusterBorder: '#aab2ba',
    edgeLabelBkg: '#f4f4f4',
    noteBkg: '#f6efd6',
    noteBorder: '#c9bd8e',
    altRow: '#eceff2',
  },
};

// ------------------------------------------------------------ token reading

/** A CSS colour we are willing to hand to mermaid / a canvas renderer. */
function isUsableColor(value: string): boolean {
  const v = value.trim();
  if (!v) return false;
  // `var(--x)` would resolve to nothing inside the SVG mermaid builds.
  if (v.startsWith('var(')) return false;
  return (
    /^(#[0-9a-f]{3,8}|rgba?\(|hsla?\(|oklch\(|color\()/i.test(v) ||
    /^[a-z]+$/i.test(v)
  );
}

function readToken(
  style: CSSStyleDeclaration | null,
  name: string,
  fallback: string,
): string {
  if (!style) return fallback;
  const resolved = style.getPropertyValue(name);
  return isUsableColor(resolved) ? resolved.trim() : fallback;
}

function rootStyle(root?: Element | null): CSSStyleDeclaration | null {
  const el =
    root ?? (typeof document !== 'undefined' ? document.documentElement : null);
  if (!el || typeof window === 'undefined' || !window.getComputedStyle) {
    return null;
  }
  try {
    return window.getComputedStyle(el);
  } catch {
    return null;
  }
}

/**
 * The four role paints for `mode`, resolved from `--ts-diagram-*`.
 *
 * Safe during SSR and in tests: with no DOM it returns the literals above.
 */
export function resolveRolePaints(
  mode: ThemeMode,
  root?: Element | null,
): Record<DiagramRole, RolePaint> {
  const fallback = ROLE_FALLBACK[mode] ?? ROLE_FALLBACK.dark;
  const style = rootStyle(root);
  const out = {} as Record<DiagramRole, RolePaint>;
  for (const role of DIAGRAM_ROLES) {
    out[role] = {
      fill: readToken(style, `--ts-diagram-${role}-fill`, fallback[role].fill),
      stroke: readToken(
        style,
        `--ts-diagram-${role}-stroke`,
        fallback[role].stroke,
      ),
      ink: readToken(style, `--ts-diagram-ink`, fallback[role].ink),
    };
  }
  return out;
}

// --------------------------------------------------------------- the config

/**
 * Keys a `%%{init: …}%%` directive in MODEL OUTPUT may not override.
 *
 * Mermaid's own defaults are `['secure', 'securityLevel', 'startOnLoad',
 * 'maxTextSize', 'suppressErrorRendering']`. Adding `theme` and
 * `themeVariables` is what actually stops the colour ban being a polite
 * request: measured, a model-emitted `%%{init: {'theme':'default'}}%%` used to
 * paint mermaid's light lavender (#ececff nodes) inside our dark chat, and
 * with these two keys secured the same source renders in our theme.
 */
export const SECURE_KEYS = [
  'secure',
  'securityLevel',
  'startOnLoad',
  'maxTextSize',
  'suppressErrorRendering',
  'theme',
  'themeVariables',
] as const;

/**
 * The complete mermaid config for `mode`.
 *
 * `securityLevel: 'strict'` and `htmlLabels: false` are load-bearing and stay:
 * strict keeps model-authored labels from carrying raw HTML, and
 * htmlLabels:false draws labels as native SVG <text> instead of
 * <foreignObject>, which is what keeps the canvas untainted so "Download PNG"
 * works. The colour problem is fixable with both untouched.
 *
 * One config for the whole page: `mermaid.initialize` is global, so per-block
 * configs would race between four blocks in one answer.
 */
export function mermaidTheme(mode: ThemeMode, root?: Element | null) {
  const c = CHROME[mode] ?? CHROME.dark;
  const roles = resolveRolePaints(mode, root);
  return {
    startOnLoad: false,
    securityLevel: 'strict' as const,
    suppressErrorRendering: true,
    secure: [...SECURE_KEYS],
    // 'base' — NOT 'dark'/'default'. See the file header: the packaged themes
    // re-derive node colours and silently discard what we declare here.
    theme: 'base' as const,
    fontFamily: "'IBM Plex Sans', system-ui, sans-serif",
    htmlLabels: false,
    // useMaxWidth is OFF: it pins the SVG to the column width, which is what
    // shrank a wide diagram's labels to 6 px beside 17 px answer text. The
    // block sizes the SVG itself — see `diagramScale`.
    flowchart: { htmlLabels: false, useMaxWidth: false },
    sequence: { useMaxWidth: false },
    class: { htmlLabels: false, useMaxWidth: false },
    er: { useMaxWidth: false },
    state: { useMaxWidth: false },
    gantt: { useMaxWidth: false },
    journey: { useMaxWidth: false },
    pie: { useMaxWidth: false },
    themeVariables: {
      darkMode: mode === 'dark',
      background: c.surface,
      // flowchart / generic nodes
      primaryColor: c.nodeFill,
      primaryTextColor: c.ink,
      primaryBorderColor: c.nodeBorder,
      secondaryColor: roles.service.fill,
      secondaryTextColor: c.ink,
      secondaryBorderColor: roles.service.stroke,
      tertiaryColor: c.clusterBkg,
      tertiaryTextColor: c.ink,
      tertiaryBorderColor: c.clusterBorder,
      mainBkg: c.nodeFill,
      nodeBorder: c.nodeBorder,
      nodeTextColor: c.ink,
      lineColor: c.line,
      textColor: c.ink,
      titleColor: c.ink,
      edgeLabelBackground: c.edgeLabelBkg,
      clusterBkg: c.clusterBkg,
      clusterBorder: c.clusterBorder,
      defaultLinkColor: c.line,
      // notes
      noteBkgColor: c.noteBkg,
      noteTextColor: c.ink,
      noteBorderColor: c.noteBorder,
      // sequence
      actorBkg: roles.service.fill,
      actorBorder: roles.service.stroke,
      actorTextColor: c.ink,
      actorLineColor: c.line,
      signalColor: c.line,
      signalTextColor: c.ink,
      labelBoxBkgColor: c.nodeFill,
      labelBoxBorderColor: c.nodeBorder,
      labelTextColor: c.ink,
      loopTextColor: c.ink,
      activationBkgColor: roles.model.fill,
      activationBorderColor: roles.model.stroke,
      sequenceNumberColor: c.surface,
      // state
      transitionColor: c.line,
      transitionLabelColor: c.ink,
      stateBkg: c.nodeFill,
      stateLabelColor: c.ink,
      labelColor: c.ink,
      altBackground: c.altRow,
      compositeBackground: c.clusterBkg,
      compositeTitleBackground: c.clusterBkg,
      compositeBorder: c.clusterBorder,
      innerEndBackground: c.nodeFill,
      specialStateColor: c.ink,
      // entity relationship
      attributeBackgroundColorOdd: c.nodeFill,
      attributeBackgroundColorEven: c.altRow,
      // class diagram
      classText: c.ink,
      // misc ink
      errorBkgColor: c.nodeFill,
      errorTextColor: c.ink,
    },
  };
}

// -------------------------------------------------------------- the classDefs

/**
 * Diagram heads whose grammar accepts a trailing `classDef`.
 *
 * Appending one to a `sequenceDiagram` or an `erDiagram` is a PARSE ERROR, so
 * the role classDefs are only appended where they are legal. Every other type
 * still gets the theme above; it simply has no roles to colour.
 */
const CLASSDEF_HEADS = ['flowchart', 'graph', 'statediagram', 'classdiagram'];

/** The head token of a diagram source, lowercased (`flowchart lr` -> `flowchart`). */
export function diagramHead(code: string): string {
  const first = (code || '')
    .split('\n')
    .map((l) => l.trim())
    .find((l) => l && !l.startsWith('%%'));
  return (first ?? '').toLowerCase().replace(/[\s-].*$/, '');
}

export function acceptsClassDefs(code: string): boolean {
  const head = diagramHead(code);
  return CLASSDEF_HEADS.some((h) => head.startsWith(h));
}

/** The four `classDef` lines that paint the roles, for `mode`. */
export function roleClassDefs(
  mode: ThemeMode,
  root?: Element | null,
): string[] {
  const roles = resolveRolePaints(mode, root);
  return DIAGRAM_ROLES.map((role) => {
    const p = roles[role];
    return `classDef ${role} fill:${p.fill},stroke:${p.stroke},stroke-width:1.5px,color:${p.ink};`;
  });
}

// -------------------------------------------------------------- the sanitiser

/**
 * Strip every colour-bearing directive from a model-authored diagram.
 *
 * Enforced in CODE, not by asking. Both failure modes were measured against
 * the shipped block: a `%%{init: {'theme':'default'}}%%` painted mermaid's
 * light lavender inside the dark chat, and an author `style A fill:#ff0000`
 * survived even with our own classDef appended after it (inline style wins,
 * which is also why fighting it with CSS `!important` is the wrong tool — it
 * would then beat OUR theme in the fullscreen viewer and the PNG export).
 *
 * What is removed: `%%{init …}%%` directives, and any `classDef`, `style`,
 * `linkStyle` or `click` STATEMENT. What survives untouched: `A:::role`,
 * `class A,B role`, and a `classDiagram`'s own `class Foo { … }` blocks —
 * none of which carries a colour.
 *
 * The matches are anchored to the start of a line so a node label that merely
 * contains the word ("A[style guide]") is left alone.
 */
export function sanitizeDiagramSource(code: string): string {
  if (!code) return '';
  // `%%{ … }%%` directives, including multi-line ones.
  let out = code.replace(/%%\{[\s\S]*?\}%%/g, '');
  out = out
    .split('\n')
    .filter((line) => !/^\s*(classDef|style|linkStyle|click)\b/i.test(line))
    .join('\n');
  return out.replace(/^\s*\n/, '').replace(/\n{3,}/g, '\n\n');
}

/**
 * The source that is RENDERED: sanitised, with our role classDefs appended
 * where the grammar takes them.
 *
 * The same string is what the Code tab shows, so what a person copies is what
 * was drawn.
 */
export function prepareDiagramSource(
  code: string,
  mode: ThemeMode,
  root?: Element | null,
): string {
  const clean = sanitizeDiagramSource(code);
  if (!clean.trim() || !acceptsClassDefs(clean)) return clean;
  const defs = roleClassDefs(mode, root);
  return `${clean.replace(/\s+$/, '')}\n${defs.join('\n')}\n`;
}

// ------------------------------------------------------------------ the size

/**
 * The smallest label a diagram may be rendered at, in CSS pixels.
 *
 * The answer around it is set at 16-17 px. 12 px is the floor at which a node
 * label still reads as text rather than as a grey smudge; measured, the
 * shipped policy put the architecture diagram's smallest label at 6.0 px on a
 * 702 px desktop column and 2.3 px on a 360 px phone.
 */
export const LABEL_FLOOR_PX = 12;

export interface DiagramScaleInput {
  /** Usable content width of the host, in CSS px. */
  hostWidth: number;
  /** The SVG's natural width, from its viewBox. */
  naturalWidth: number;
  /** MEASURED smallest label font-size in the rendered SVG's own units. */
  smallestLabelPx: number;
  /** Floor, overridable only so the tests can probe the boundary. */
  floorPx?: number;
}

/**
 * `clamp(hostWidth / naturalWidth, floorPx / smallestLabelPx, 1)`.
 *
 * Fit the column when that keeps the labels legible; otherwise stop shrinking
 * at the floor and let the block scroll sideways. Never scale UP: a diagram
 * narrower than the column stays at its natural size, so one answer cannot
 * carry 8 px text in one diagram and 20 px in another.
 *
 * The floor is computed from the MEASURED smallest label rather than from a
 * constant, because the smallest label in a mermaid SVG is not always the body
 * font — edge labels and cluster titles differ per diagram type.
 */
export function diagramScale({
  hostWidth,
  naturalWidth,
  smallestLabelPx,
  floorPx = LABEL_FLOOR_PX,
}: DiagramScaleInput): number {
  if (!(naturalWidth > 0) || !Number.isFinite(naturalWidth)) return 1;
  if (!(hostWidth > 0) || !Number.isFinite(hostWidth)) return 1;
  const fit = hostWidth / naturalWidth;
  const floor =
    smallestLabelPx > 0 && Number.isFinite(smallestLabelPx)
      ? floorPx / smallestLabelPx
      : 0;
  return Math.min(1, Math.max(fit, floor));
}

/**
 * The smallest label font-size in a RENDERED mermaid SVG, in the SVG's own
 * units (i.e. before the block scales it).
 *
 * Read from the DOM rather than assumed, because the smallest label is not
 * always the body font: edge labels, cluster titles and ER attribute rows are
 * each set differently per diagram type, and it is the SMALLEST one that
 * decides whether the diagram is readable.
 *
 * Returns 0 when there is nothing to measure, which `diagramScale` reads as
 * "no floor" rather than as "infinitely small".
 */
export function smallestLabelPx(svg: SVGElement | null | undefined): number {
  if (!svg || typeof window === 'undefined' || !window.getComputedStyle) return 0;
  let min = Infinity;
  const nodes = svg.querySelectorAll('text, tspan');
  for (const node of Array.from(nodes)) {
    if (!(node.textContent || '').trim()) continue;
    let size = 0;
    try {
      size = parseFloat(window.getComputedStyle(node).fontSize) || 0;
    } catch {
      size = 0;
    }
    if (size > 0 && size < min) min = size;
  }
  return Number.isFinite(min) ? min : 0;
}
