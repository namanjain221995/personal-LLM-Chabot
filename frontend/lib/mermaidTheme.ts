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

import { withoutPreamble } from './mermaid';

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
    /** quadrantChart's four background regions — tints, not series colours. */
    quadrant: readonly [string, string, string, string];
    /** journey's smiley face, which mermaid otherwise hard-codes to cornsilk. */
    faceFill: string;
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
    quadrant: ['#1f2d3d', '#453625', '#53404e', '#674f47'],
    faceFill: '#33383d',
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
    quadrant: ['#d1e3f9', '#e0ceb9', '#d1baca', '#c6aaa1'],
    faceFill: '#dbe0e4',
  },
};

// ------------------------------------------------------ the categorical set

/**
 * How many categorical slots mermaid asks for. `pie1…pie12` and
 * `cScale0…cScale11` are both twelve; git branches are the first eight.
 */
export const CATEGORICAL_SLOTS = 12;

/**
 * The categorical palette — what a pie slice, a timeline section, a gitGraph
 * branch, a mindmap branch, a journey section and an xychart series are
 * painted with.
 *
 * WHY THIS EXISTS AT ALL
 * ----------------------
 * mermaid's `base` theme DERIVES every categorical family from `primaryColor`
 * (theme-base's `updateColors`: `cScale0 = primaryColor`, `cScale3…11 =
 * adjust(primaryColor, {h: 30…330})`, then a flat `darken(…, 75)` in dark
 * mode; `pie1 = primaryColor`, `git0 = primaryColor`, `quadrant1Fill =
 * primaryColor`). Our `primaryColor` is the grey node fill `#33383d`, so
 * rotating its hue produces twelve greys. Measured in Chromium 11.17 on the
 * dark card, before this palette existed:
 *
 *   pie       slices rgb(51,56,61) rgb(34,48,63) rgb(25,28,31) rgb(2,3,3)
 *             — ΔL* 3.89 from the #1e1e1e card, 1.08:1, worst pair ΔL* 0.77
 *   timeline  every section rgb(0,0,0) — one fill for all three
 *   mindmap   rgb(0,0,0)
 *   quadrant  four fills 5 rgb units apart, worst pair ΔL* 2.19
 *   xychart   BOTH series rgb(255,244,221) — mermaid's stock cream, which on
 *             the light card is ΔL* 0.30 and 1.01:1
 *
 * A categorical family cannot be derived from a neutral; it has to be stated.
 *
 * THE SHAPE: SIX HUE FAMILIES, TWO LIGHTNESS STEPS
 * ------------------------------------------------
 * Twelve distinct hues are not available here. `tests/accent-palette.test.ts`
 * bans the whole green/teal/aqua arc outside chartTheme.ts, and at the
 * lightness a mark needs to clear 3:1 on the card the yellow end turns olive,
 * which reads green to a person even where the test allows it. The usable arc
 * measures 215° (OKLCH hue 250→106 through 0), so the set is six families at
 * ~36° with two lightness steps each: slots 1-6 are six different hues, slots
 * 7-12 the second step of the same six in the same order. A chat pie has 3-8
 * slices, a timeline 3-6 sections and a gitGraph 2-5 branches, so the traffic
 * lands in the first six at full hue separation and degrades to a second step
 * of a hue you already know rather than to a colour nobody can name.
 *
 * Four of the six hues are the product's own, read off the tokens already in
 * globals.css: blue (`--ts-chart-2` / the service outline), amber
 * (`--ts-chart-3` / store), violet (`--ts-chart-4` / model) and rose
 * (`--ts-chart-5` / external). Two more fill the widest gaps.
 *
 * THE BAND
 * --------
 * mermaid paints a pie's percentage ON the slice with ONE colour for every
 * slice (`pieSectionTextColor`), so a single ink has to clear AA 4.5:1 on all
 * twelve. That is what sets the lightness band, not taste: OKLCH L 0.600-0.665
 * on the dark card (near-black ink) and 0.465-0.565 on the light card (white
 * ink), both inside the data-viz band and both ≥ 3:1 against the card.
 *
 * Validated with the dataviz skill's own validator in both modes; the numbers
 * and the measured render are in the commit message.
 *
 * As with the roles, globals.css owns the values (`--ts-diagram-cat-*`) and
 * these literals are the SSR/test fallback.
 */
const CATEGORICAL_FALLBACK: Record<ThemeMode, readonly string[]> = {
  // slots 1-6: blue, gold, magenta, orange, violet, rose
  // slots 7-12: the second lightness step of the same six, same order
  dark: [
    '#3596f8', '#ca8200', '#bc51a6', '#d05320', '#776de0', '#e75f7c',
    '#1981e1', '#b07000', '#d265bb', '#e76838', '#8981f7', '#d04a69',
  ],
  light: [
    '#0076d5', '#a36700', '#8f257d', '#993200', '#5343b3', '#c43e5f',
    '#005aa4', '#7c4e00', '#b0469b', '#c44810', '#6d62d4', '#a11943',
  ],
};

/**
 * The one ink every categorical fill carries.
 *
 * Near-black on the dark card and white on the light one, because the fills
 * themselves are inverted between the modes: on a dark card a categorical
 * mark has to be LIGHT to clear 3:1, and light marks take dark text. This is
 * the same direction mermaid's own dark theme takes (`scaleLabelColor:
 * 'black'` when `darkMode`), for the same reason.
 */
const CATEGORICAL_INK: Record<ThemeMode, string> = {
  dark: '#0d0d0d',
  light: '#ffffff',
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

/**
 * The twelve categorical fills for `mode`, resolved from `--ts-diagram-cat-N`.
 *
 * Safe during SSR and in tests, same as `resolveRolePaints`.
 */
export function resolveCategorical(
  mode: ThemeMode,
  root?: Element | null,
): string[] {
  const fallback = CATEGORICAL_FALLBACK[mode] ?? CATEGORICAL_FALLBACK.dark;
  const style = rootStyle(root);
  return fallback.map((fb, i) =>
    readToken(style, `--ts-diagram-cat-${i + 1}`, fb),
  );
}

/** The ink that sits ON a categorical fill, for `mode`. */
export function categoricalInk(
  mode: ThemeMode,
  root?: Element | null,
): string {
  return readToken(
    rootStyle(root),
    '--ts-diagram-cat-ink',
    CATEGORICAL_INK[mode] ?? CATEGORICAL_INK.dark,
  );
}

/**
 * Every mermaid theme variable that carries a CATEGORICAL colour, stated.
 *
 * Each family below was read out of mermaid 11.17's `theme-base` (the `base`
 * theme is `Theme` / `getThemeVariables` in dist/mermaid.js), because every
 * one of them derives from `primaryColor` when we leave it alone:
 *
 *   cScale0…11        timeline sections, mindmap branches, journey sections,
 *                     treemap tiles. `cScaleN = primaryColor` rotated, then
 *                     flat-darkened by 75 in dark mode — which is how three
 *                     timeline sections all arrived as rgb(0,0,0).
 *   cScaleLabel0…11   the text ON those fills; defaults to `labelTextColor`.
 *   cScaleInv0…11     `invert(cScaleN)`, used for mindmap edges (`lineColorN`)
 *                     and section outlines. Inverting a mid-tone fill gives a
 *                     complementary colour nobody chose, so it is stated as
 *                     the theme's own line colour instead.
 *   pie1…12           pie slices. NOTE the 1-based index, unlike every other
 *                     family here.
 *   git0…7            gitGraph branches; gitInv/gitBranchLabel are their inks.
 *   fillType0…7       journey/requirement section fills.
 *   venn1…8           venn set fills.
 *   quadrant1…4Fill   quadrant BACKGROUNDS — see below, they are not series.
 *   xyChart.plotColorPalette  a comma-separated string, not a set of keys, and
 *                     it defaults to mermaid's stock cream list, which is why
 *                     both xychart series painted rgb(255,244,221).
 *
 * `theme-base.calculate` applies our overrides, runs `updateColors`, then
 * applies them AGAIN — so a key mermaid assigns unconditionally (`pieN =
 * cScaleN`) still ends up with our value. Setting both is deliberate, not
 * redundant: it makes the result independent of that re-apply.
 *
 * The quadrant fills are the one family that must NOT be a series colour: they
 * are the four background regions of the plot, with the point marks and the
 * labels drawn on top. They are stated as four tints of the first four hues,
 * mixed most of the way to the card so they read as background and still step
 * apart from it and from each other.
 */
function categoricalVariables(
  mode: ThemeMode,
  c: (typeof CHROME)[ThemeMode],
  root?: Element | null,
): Record<string, string> {
  const cat = resolveCategorical(mode, root);
  const ink = categoricalInk(mode, root);
  const out: Record<string, string> = {};
  for (let i = 0; i < CATEGORICAL_SLOTS; i += 1) {
    const fill = cat[i] ?? cat[i % cat.length];
    out[`cScale${i}`] = fill;
    out[`cScaleLabel${i}`] = ink;
    out[`cScaleInv${i}`] = c.line;
    out[`pie${i + 1}`] = fill;
  }
  for (let i = 0; i < 8; i += 1) {
    const fill = cat[i] ?? cat[i % cat.length];
    out[`git${i}`] = fill;
    out[`gitInv${i}`] = ink;
    out[`gitBranchLabel${i}`] = ink;
    out[`fillType${i}`] = fill;
    out[`venn${i + 1}`] = fill;
  }
  // journey actors. `actor0…5` ARE theme variables, but the journey renderer
  // leaves `.actor-N` unfilled when they are unset and falls back to its own
  // hard-coded list — measured on the light card: rgb(0,255,255) cyan,
  // rgb(124,252,0) lawngreen and rgb(143,188,143) darkseagreen, three colours
  // the product has decided against, sitting in the middle of a chat answer.
  for (let i = 0; i < 6; i += 1) {
    out[`actor${i}`] = cat[i] ?? cat[i % cat.length];
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
  const cat = resolveCategorical(mode, root);
  const catInk = categoricalInk(mode, root);
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

      // ------------------------------------------------ categorical families
      // Twelve stated fills plus their inks; see `categoricalVariables` for
      // what mermaid derives from `primaryColor` when these are left alone.
      ...categoricalVariables(mode, c, root),

      // pie chrome. `pieOpacity` defaults to 0.7, which washes every slice
      // back toward the card and undoes a validated palette; the slices are
      // separated by a hairline in the CARD colour instead of mermaid's
      // default literal 'black', which is invisible on our dark card and a
      // hard rule on the light one.
      pieOpacity: '1',
      pieStrokeColor: c.surface,
      pieOuterStrokeColor: c.surface,
      pieSectionTextColor: catInk,
      pieTitleTextColor: c.ink,
      pieLegendTextColor: c.ink,

      // gitGraph chrome: the commit and tag labels sit on OUR surfaces, not
      // on a branch colour, so they take the theme ink rather than catInk.
      commitLabelColor: c.ink,
      commitLabelBackground: c.nodeFill,
      tagLabelColor: c.ink,
      tagLabelBackground: c.nodeFill,
      tagLabelBorder: c.nodeBorder,
      branchLabelColor: catInk,
      // mermaid ships these at 10px, and a gitGraph is not scaled up (the
      // block never scales UP — see `diagramScale`), so 10px is what reaches
      // the screen: measured at 10.0 px beside 17 px answer text. The track's
      // own floor is 12.
      commitLabelFontSize: '12px',
      tagLabelFontSize: '12px',

      // journey: the smiley face defaults to a hard-coded cornsilk #FFF8DC,
      // which is ΔL* 1.27 from the light card — a face you cannot see.
      faceColor: c.faceFill,

      // quadrantChart: the four fills are BACKGROUND REGIONS with the points
      // and the axis labels drawn on top, so they are tints rather than
      // series colours. The point takes the first categorical fill, which is
      // what makes a plotted item findable on them.
      quadrant1Fill: c.quadrant[0],
      quadrant2Fill: c.quadrant[1],
      quadrant3Fill: c.quadrant[2],
      quadrant4Fill: c.quadrant[3],
      quadrant1TextFill: c.ink,
      quadrant2TextFill: c.ink,
      quadrant3TextFill: c.ink,
      quadrant4TextFill: c.ink,
      quadrantPointFill: cat[0],
      quadrantPointTextFill: c.ink,
      quadrantXAxisTextFill: c.ink,
      quadrantYAxisTextFill: c.ink,
      quadrantTitleFill: c.ink,
      quadrantInternalBorderStrokeFill: c.nodeBorder,
      quadrantExternalBorderStrokeFill: c.nodeBorder,

      // xychart is a nested OBJECT, and its series colours are one
      // comma-separated string. Left alone it keeps mermaid's stock cream
      // list, which is how both series arrived as rgb(255,244,221).
      xyChart: {
        backgroundColor: c.surface,
        titleColor: c.ink,
        dataLabelColor: c.ink,
        legendTextColor: c.ink,
        xAxisTitleColor: c.ink,
        xAxisLabelColor: c.inkMuted,
        xAxisTickColor: c.line,
        xAxisLineColor: c.line,
        yAxisTitleColor: c.ink,
        yAxisLabelColor: c.inkMuted,
        yAxisTickColor: c.line,
        yAxisLineColor: c.line,
        plotColorPalette: cat.slice(0, 8).join(','),
      },
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
  // Past the preamble: a `---\nconfig: …\n---` block would otherwise be read
  // as the head, and a flowchart carrying one would get no role classDefs.
  const first = withoutPreamble(code)
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

/** The statement keywords that can carry a colour. Matched per STATEMENT. */
const COLOUR_DIRECTIVE = /^\s*(classDef|style|linkStyle|click)\b/i;

/**
 * Split ONE line into mermaid statements on `;`.
 *
 * `;` is a statement separator in the flowchart grammar, which is the whole
 * reason a line-anchored filter was not enforcement: measured on this branch,
 * `C-->D; style A fill:#ff0000,stroke:#00ff00` painted rgb(255,0,0) on a
 * rgb(0,255,0) outline in BOTH themes, because the line does not START with
 * `style`.
 *
 * A `;` inside a LABEL must not split, so the scan tracks mermaid's label
 * delimiters: `"…"`, `[…]`, `(…)`, `{…}` and the `|…|` of an edge label.
 *
 * Two deliberate details:
 *
 *  - only `"` opens a string, never `'`. An apostrophe is ordinary prose
 *    ("Don't"), and treating it as a delimiter would leave the rest of the
 *    line "inside a string" — which is exactly how a trailing `; style …`
 *    would slip back through. mermaid spells a literal double quote `#quot;`.
 *  - a delimiter that does not BALANCE on the line is not treated as a
 *    delimiter at all — `"`, `[]`, `()`, `{}` and `|` alike. An unbalanced
 *    opener would otherwise swallow the rest of the line and re-open the hole
 *    this function exists to close (measured: `A["unclosed --> B; style A
 *    fill:#ff0000` kept its `style` while `[` was trusted). Ignoring it costs
 *    nothing, because a source with unbalanced delimiters does not parse.
 *
 * `%%` starts a comment that runs to end of line, so everything after it is
 * inert and is kept verbatim rather than split.
 */
function splitStatements(line: string): string[] {
  const balanced = (open: string, close = open) =>
    open === close
      ? (line.split(open).length - 1) % 2 === 0
      : line.split(open).length === line.split(close).length;
  const quotes = balanced('"');
  const pipes = balanced('|');
  const squares = balanced('[', ']');
  const parens = balanced('(', ')');
  const braces = balanced('{', '}');
  const out: string[] = [];
  let buf = '';
  let inQuote = false;
  let square = 0;
  let paren = 0;
  let brace = 0;
  let inPipe = false;
  for (let i = 0; i < line.length; i += 1) {
    const ch = line[i];
    if (inQuote) {
      buf += ch;
      if (ch === '"') inQuote = false;
      continue;
    }
    if (ch === '%' && line[i + 1] === '%' && !square && !paren && !brace) {
      // A comment: inert to mermaid, so it is never split or stripped.
      buf += line.slice(i);
      break;
    }
    if (ch === '"' && quotes) {
      inQuote = true;
      buf += ch;
      continue;
    }
    if (ch === '[' && squares) square += 1;
    else if (ch === ']' && squares) square = Math.max(0, square - 1);
    else if (ch === '(' && parens) paren += 1;
    else if (ch === ')' && parens) paren = Math.max(0, paren - 1);
    else if (ch === '{' && braces) brace += 1;
    else if (ch === '}' && braces) brace = Math.max(0, brace - 1);
    else if (ch === '|' && pipes) inPipe = !inPipe;
    else if (ch === ';' && !square && !paren && !brace && !inPipe) {
      out.push(buf);
      buf = '';
      continue;
    }
    buf += ch;
  }
  out.push(buf);
  return out;
}

/**
 * Strip every colour-bearing directive from a model-authored diagram.
 *
 * Enforced in CODE, not by asking — and enforced per STATEMENT, not per line.
 * Three failure modes were measured against the shipped block, and the third
 * against this branch's own first attempt:
 *
 *  1. `%%{init: {'theme':'default'}}%%` painted mermaid's light lavender
 *     inside the dark chat.
 *  2. an author `style A fill:#ff0000` survived even with our own classDef
 *     appended after it — inline style wins, which is also why fighting it
 *     with CSS `!important` is the wrong tool: `!important` would then beat
 *     OUR theme in the fullscreen viewer and the PNG export.
 *  3. `C-->D; style A fill:#ff0000,stroke:#00ff00` rode a SEMICOLON past the
 *     line-anchored filter and rendered red-on-green in both themes. `;` is a
 *     statement separator in the flowchart grammar, so "the line starts with
 *     style" was never the right question.
 *
 * What is removed: `%%{ … }%%` directives wherever they appear, including the
 * multi-line form, and any `classDef`, `style`, `linkStyle` or `click`
 * STATEMENT — whether it opens its line or follows a `;`.
 *
 * What survives untouched: `A:::role`, `class A,B role`, and a
 * `classDiagram`'s own `class Foo { … }` blocks. None of those carries a
 * colour: they NAME something, and the four names that mean anything are the
 * closed role vocabulary. An application that names a class nobody defined —
 * `class A mine`, once its `classDef mine` has been stripped — is inert, and
 * paints the default node (measured, both themes).
 *
 * A statement is only rewritten when something was actually dropped from its
 * line, so ordinary sources reach the Code tab byte-for-byte unchanged.
 */
export function sanitizeDiagramSource(code: string): string {
  if (!code) return '';
  // `%%{ … }%%` directives, including multi-line ones. Run before the
  // statement split so a directive that itself contains `;` cannot confuse it.
  let out = code.replace(/%%\{[\s\S]*?\}%%/g, '');
  // An unterminated `%%{init: …` is a comment to mermaid rather than a
  // directive, but it must not reach the Code tab looking like one.
  out = out.replace(/%%\{[^\n]*/g, '');
  out = out
    .split('\n')
    .map((line) => {
      const parts = splitStatements(line);
      if (!parts.some((p) => COLOUR_DIRECTIVE.test(p))) return line;
      const kept = parts.filter((p) => p.trim() && !COLOUR_DIRECTIVE.test(p));
      return kept.join(';');
    })
    .filter((line, i, all) => !(line === '' && all[i - 1] === ''))
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
