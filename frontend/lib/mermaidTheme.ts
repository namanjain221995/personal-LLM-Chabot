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
 * ink-on-fill 10.5-14.9:1, stroke-on-surface 3.69-6.46:1, worst all-pairs CVD
 * stroke separation 42.9 (protan/deutan/tritan, Brettel). The fill is a tint
 * that carries the hue (ΔL* 6.6-10.6 from the card, against the shipped 0.88);
 * the STROKE is what carries the 3:1 separation, which is why the contrast
 * floor is set on the stroke and not on the fill.
 *
 * A stroke has TWO neighbours, though, and only one of them was ever checked.
 * Re-measured 2026-09-27 with the dataviz skill's validator, dark `service`
 * #2f6fb2 was 2.58:1 against its OWN #22303f fill and light `store` #b7791f
 * 2.79:1 against its own #eae0d2 — outlines that cleared the card and then
 * disappeared into the box they were drawing. DIAG-07b is the check that was
 * missing. Lifting the dark blue alone collapsed blue↔violet to an all-pairs
 * normal-vision ΔE of 14.9 (floor 15), so `model` moved with it; the new dark
 * pair scores BETTER than the shipped one on every separation check (CVD 8.2
 * → 9.9, normal-vision 16.5 → 17.2). Stroke on its OWN fill is now
 * 3.27-3.65:1 dark and 3.11-4.97:1 light.
 *
 * These literals are the SSR/test fallback and globals.css is the source of
 * truth, so they must agree value for value — DIAG-25b reads the stylesheet
 * and fails on any drift.
 */
const ROLE_FALLBACK: Record<ThemeMode, Record<DiagramRole, RolePaint>> = {
  dark: {
    service: { fill: '#22303f', stroke: '#3783be', ink: '#ececec' },
    store: { fill: '#40321e', stroke: '#b7791f', ink: '#ececec' },
    model: { fill: '#362c4e', stroke: '#9b6bff', ink: '#ececec' },
    external: { fill: '#462934', stroke: '#d55181', ink: '#ececec' },
  },
  light: {
    service: { fill: '#d4dfe9', stroke: '#2f6fb2', ink: '#0d0d0d' },
    store: { fill: '#eae0d2', stroke: '#ac721d', ink: '#0d0d0d' },
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
 * Config keys an IN-SOURCE override may not set.
 *
 * Mermaid applies both channels — a `%%{init: …}%%` directive and a YAML
 * frontmatter `config:` block — through the same `addDirective`, and
 * `mermaidAPI` deletes every key named here from a directive before it merges
 * (`sanitize` in config.ts; `secure` is never applied to what WE pass to
 * `mermaid.initialize`, so listing a key we set ourselves costs nothing —
 * mermaid's own five defaults are all keys it sets itself).
 *
 * Mermaid's own defaults are `['secure', 'securityLevel', 'startOnLoad',
 * 'maxTextSize', 'suppressErrorRendering']`. Everything after them is ours,
 * and each one was measured today (Chromium 153.0.8010.36 / mermaid 11.17.0,
 * real esbuild bundle of <MermaidBlock>) to change what the diagram looks like
 * when an author sets it:
 *
 *   theme            `%%{init: {'theme':'default'}}%%` painted mermaid's light
 *                    lavender (#ececff nodes) inside our dark chat.
 *   themeVariables   the whole palette this file declares.
 *   themeCSS         raw CSS, namespaced to the diagram's own `#mmd-N` and
 *                    injected into its `<style>`: a frontmatter block set a
 *                    plain node to rgb(255,0,0) on a rgb(0,255,0) 6 px
 *                    outline, its edge to rgb(255,0,255) at 5 px, every pie
 *                    slice and every sequence actor to rgb(255,0,0), and
 *                    `display:none` on `.flowchart-link` erased both edges of
 *                    a three-node flowchart — a WRONG picture, not an ugly one.
 *   htmlLabels       re-enables <foreignObject> labels: a `<b style=
 *                    "color:#ff0000">` label then computed rgb(255,0,0), and
 *                    the export canvas went from clean to tainted
 *                    (`SecurityError` out of getImageData, 0 ink pixels), so
 *                    "Download PNG" silently degrades to the SVG fallback.
 *   flowchart        carries its own `htmlLabels`, same effect.
 *   look             `look: handDrawn` swapped every `g.node` for a rough.js
 *                    `.rough-node` path set (measured: 0 nodes, 2 rough nodes,
 *                    `data-look="handDrawn"`).
 *   layout           picks a different layout engine for the same graph.
 *   fontFamily /     fed into `--mermaid-font-family`. Measured inert on its
 *   altFontFamily    own (mermaid namespaces its `:root` rule into
 *                    `#mmd-N :root`, which matches nothing) — secured anyway,
 *                    because "inert" here is one upstream bug fix away from
 *                    "Comic Sans in a chat answer".
 *   fontSize,        size and wrapping of the same content.
 *   markdownAutoWrap
 *   darkMode         flips mermaid's own light/dark derivations.
 *   class, sequence, the per-diagram config objects that carry their own
 *   gantt, journey,  fonts, paddings and colours.
 *   pie, quadrantChart,
 *   xyChart, mindmap,
 *   timeline, gitGraph,
 *   requirement, er,
 *   state, block, sankey,
 *   packet, radar, treemap,
 *   architecture, kanban
 *
 * This list is the SECOND line of defence. The first is
 * `sanitizeDiagramSource`, which removes both override channels from the
 * source before mermaid ever sees them; this is what holds if a future mermaid
 * grows a third channel we have not met.
 */
export const SECURE_KEYS = [
  'secure',
  'securityLevel',
  'startOnLoad',
  'maxTextSize',
  'suppressErrorRendering',
  'theme',
  'themeVariables',
  'themeCSS',
  'htmlLabels',
  'flowchart',
  'look',
  'layout',
  'fontFamily',
  'altFontFamily',
  'fontSize',
  'markdownAutoWrap',
  'darkMode',
  'class',
  'sequence',
  'gantt',
  'journey',
  'pie',
  'quadrantChart',
  'xyChart',
  'mindmap',
  'timeline',
  'gitGraph',
  'requirement',
  'er',
  'state',
  'block',
  'sankey',
  'packet',
  'radar',
  'treemap',
  'architecture',
  'kanban',
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

// --------------------------------------------------------- the preamble guard

/**
 * The leading YAML frontmatter block, in EXACTLY mermaid's own shape.
 *
 * Copied character for character from `frontMatterRegex` in mermaid 11.17.0
 * (`dist/chunks/mermaid.core/chunk-DU6HZSFF.mjs`). It has to be the same
 * regex, not a similar one: a block this misses but mermaid matches is a
 * config override we do not see and it does, and a block this matches but
 * mermaid does not is a piece of DIAGRAM BODY we would delete. Three details
 * are load-bearing and were all reproduced today:
 *
 *  - `([^\S\n\r]*)` — only HORIZONTAL whitespace before the opening `---`,
 *    captured, and the closing `---` must carry the same indent. An indented
 *    block is frontmatter to mermaid (measured: an indented `themeCSS` painted
 *    rgb(255,0,0)), so it has to be one here.
 *  - `[\n\r]` rather than `\n` — and the CRLF normalisation below, because
 *    mermaid's `cleanupText` runs first. A CRLF `config: themeCSS` block
 *    painted rgb(255,0,0) too.
 *  - the trailing `[\n\r]+` — a block at the very end of the text with no line
 *    after it is NOT frontmatter to mermaid, so it must stay body here.
 */
const FRONTMATTER_RE = /^([^\S\n\r]*)-{3}\s*[\n\r](.*?)[\n\r]\1-{3}\s*[\n\r]+/s;

/**
 * How many times a preamble may PROMOTE before the source is refused.
 *
 * Shared by the guard's own loop and by the sanitise fixed point below, so the
 * two cannot disagree about where "too much preamble" starts.
 */
const MAX_PREAMBLE_PASSES = 64;

/**
 * The ONE thing a frontmatter block may carry: a caption.
 *
 * This is a positive allowlist and it is deliberately the narrowest one that
 * keeps a legitimate diagram whole: a single `title:` line with a plain scalar
 * value. `config:` is the channel this guard exists to close, but the rule is
 * not "no config" — it is "nothing but a title", so a key nobody here has met
 * (mermaid's `displayMode`, or whatever 11.18 adds) is refused by default
 * rather than admitted by omission.
 *
 * Why a line test and not a YAML parse: agreeing with mermaid's parser would
 * mean shipping js-yaml into this file and matching its schema, resolution and
 * indent handling exactly — a second parser whose disagreements ARE the bug.
 * A block that is not exactly one `title:` line is dropped whole instead, so
 * there is nothing to disagree about.
 *
 * The value must not open with a YAML indicator: `|`/`>` start a block scalar
 * whose content would be on lines this rule has already refused to allow,
 * `&`/`*` are an anchor and an alias, `!` is a tag (a custom tag makes
 * mermaid's own `load` THROW, which the block would show as a failed render
 * rather than as a drawing), and
 * `{`/`[` start a collection — mermaid renders `parsed.title.toString()`, so a
 * map here draws the literal text "[object Object]".
 */
const TITLE_LINE = /^title:[ \t]+([^\s|>&*!{[][^\n]*)$/;

/** The `title:` line to keep, or `''` when this block may not be kept. */
function allowedFrontmatter(body: string[]): string {
  if (body.length !== 1) return '';
  const line = body[0].trimEnd();
  return TITLE_LINE.test(line) ? line : '';
}

/**
 * Both IN-SOURCE config channels, removed before mermaid can read either.
 *
 * Mermaid takes a config override from a `%%{init: …}%%` directive AND from a
 * YAML frontmatter `config:` block, merges them with `cleanAndMerge` and
 * applies the result through the same `addDirective`. Only the directive was
 * ever removed here. Measured today, Chromium 153.0.8010.36 / mermaid 11.17.0,
 * real esbuild bundle of `<MermaidBlock>`:
 *
 *     ---
 *     config:
 *       themeCSS: |
 *         .node rect { fill: #ff0000 !important; stroke: #00ff00 !important;
 *                      stroke-width: 6px !important; }
 *         .flowchart-link { stroke: #ff00ff !important; stroke-width: 5px
 *                      !important; }
 *     ---
 *     flowchart TD
 *       SVC[Gateway]:::service --> PLAIN[Plain node]
 *
 * drew PLAIN at `fill: rgb(255, 0, 0)`, `stroke: rgb(0, 255, 0)`,
 * `stroke-width: 6px` and its edge at `stroke: rgb(255, 0, 255)`, `5px`,
 * against rgb(51,56,61)/rgb(139,148,158)/1px and rgb(154,163,173)/1px for the
 * same diagram without the block. A roled node was the only thing that held,
 * and only by accident: `classDef` lands as an INLINE `!important` style,
 * which beats a stylesheet rule. A `sequenceDiagram` or a `pie` gets no
 * classDefs at all, and there every actor rect and every slice went
 * rgb(255,0,0).
 *
 * The same channel also carries `htmlLabels: true`, which put a
 * `<b style="color:#ff0000">` label at computed rgb(255,0,0) and tainted the
 * export canvas (`SecurityError` from getImageData, 0 ink pixels — "Download
 * PNG" degrades to the SVG fallback), and `look: handDrawn`, which replaced
 * every `g.node` with a rough.js path set.
 *
 * STRIP OR REFUSE. The preamble is stripped, not refused, because neither
 * channel can change WHAT is drawn: mermaid removes both from the text before
 * it parses a single statement, so they carry presentation and nothing else,
 * and the picture that comes out of a stripped source is the same graph in our
 * own theme. A refusal here would cost a correct diagram for nothing. The one
 * case that IS refused is a directive crafted so `}%%` sits inside its own
 * string value: the balanced strip stops at that inner `}%%` and leaves a
 * fragment mid-statement, and there is no way to remove the fragment without
 * guessing where the statement around it began — dropping its line can delete
 * a real `A-->B`, and keeping it draws whatever the fragment happens to parse
 * as. A wrong picture is worse than no picture, so the whole source is
 * refused and the block shows the source instead.
 */
export interface GuardedSource {
  /** The source to render, and to show. */
  code: string;
  /** Why the source must not be rendered at all; `''` when it may be. */
  refusal: string;
}

export function guardDiagramSource(code: string): GuardedSource {
  if (!code) return { code: '', refusal: '' };
  // mermaid's `cleanupText` normalises line endings before it looks for
  // frontmatter, so this has to as well — a CRLF block is invisible to an
  // `\n`-only reader and fully live in the renderer.
  let out = code.replace(/\r\n?/g, '\n');
  /** The one allowed `---\ntitle: …\n---` block, once it has been found. */
  let head = '';
  /**
   * TO A FIXED POINT, because removing one preamble PROMOTES the next.
   *
   * Two ways round a single pass, both caught by writing this loop and then
   * reproducing them against it:
   *
   *  1. two frontmatter blocks. Mermaid extracts only the FIRST, so a second
   *     one is body and a parse error — but once this guard drops a
   *     config-bearing first block, the second block becomes the first thing in
   *     the text, and the source mermaid is then handed has a live
   *     `config: themeCSS` at its head.
   *  2. a `%%{init}%%` line ABOVE a frontmatter block. Mermaid extracts
   *     frontmatter before directives, so that block is not frontmatter to
   *     mermaid and the whole thing fails to parse — but this guard strips the
   *     directive, and `sanitizeDiagramSource` then trims the blank line it
   *     left, which lifts the block to column 0 of line 1 and makes it live.
   *
   * Both exist only because the guard itself rewrites the text. (1) needs the
   * LOOP: with `pass < 1` the second block survives and DIAG-35f fails. (2)
   * needs the ORDER inside it — directives, then the blank-line trim, then the
   * frontmatter scan: move the scan above the strip and DIAG-35g fails. Each
   * pass strictly shortens `out`, so the bound guards a future edit rather than
   * any source seen here.
   */
  let passes = 0;
  for (; passes < MAX_PREAMBLE_PASSES; passes += 1) {
    const before = out;
    // `%%{ … }%%` directives, including multi-line ones. Run before the
    // statement split so a directive that itself contains `;` cannot confuse
    // it, and before the frontmatter scan so case 2 above cannot hide a block.
    out = out.replace(/%%\{[\s\S]*?\}%%/g, '');
    // An unterminated `%%{init: …` is a comment to mermaid rather than a
    // directive, but it must not reach the Code tab looking like one.
    out = out.replace(/%%\{[^\n]*/g, '');
    // ...and only when a directive was actually OPENED. `}%%` on its own is
    // not a directive to mermaid — its `directiveRegex` needs the `%%{` — so a
    // label that merely contains the three characters is an ordinary diagram.
    // Measured: `flowchart LR\n  A["50}%% done"] --> B[Next]` drew two nodes
    // in theme colours before this refusal existed, and without the `%%\{`
    // half of this test it is refused with a notice naming a construct its
    // author never wrote. The attack shape still has its opening `%%{`.
    if (/\}%%/.test(out) && /%%\{/.test(code)) {
      return { code: out, refusal: 'it carries a malformed %%{…}%% directive' };
    }
    // The leading blank lines `sanitizeDiagramSource` trims at the end anyway.
    // Trimming them HERE is what makes case 2 visible instead of smuggled.
    //
    // `[^\S\n\r]` and not `[ \t]`: the trim at the end of
    // `sanitizeDiagramSource` is `/^\s*\n/`, so ANY whitespace-only line it
    // would delete has to be a line this one deletes too. The difference is
    // one character class and it was a complete bypass — a single U+00A0,
    // U+FEFF, \f, \v, U+2028/9, U+1680, U+2000-200A, U+202F, U+205F or U+3000
    // line above a `config:` block hid the block from `FRONTMATTER_RE` (whose
    // `([^\S\n\r]*)` indent cannot span a newline), the sanitiser then deleted
    // the line, and the string handed to `mermaid.render` BEGAN with the live
    // block. Measured on the commit before this one: `\u00a0\n---\nconfig:\n
    // c4:\n    width: 400\n    personFontSize: 40\n---\nC4Context …` moved
    // c4.width 216 -> 400 and personFontSize 14 -> 40 and redrew the diagram
    // at 700x1041 px with 40 px type; with this class it is byte-identical to
    // the same source without the preamble line.
    out = out.replace(/^(?:[^\S\n\r]*\n)+/, '');
    const fm = FRONTMATTER_RE.exec(out);
    if (!fm) break;
    const indent = fm[1];
    const body = fm[2]
      .split('\n')
      .map((l) => (indent && l.startsWith(indent) ? l.slice(indent.length) : l));
    const title = allowedFrontmatter(body);
    out = out.slice(fm[0].length);
    if (title) {
      // Mermaid stops at the first block too, so nothing behind this one can
      // be config to it either. Keep it and stop promoting.
      head = `---\n${title}\n---\n`;
      break;
    }
    if (out === before) break;
  }
  /**
   * EXHAUSTING the bound is a refusal, not a pass-through.
   *
   * Every pass above strictly shortens `out`, so no source reaches this line
   * by accident — but a source CAN reach it on purpose. Measured on the commit
   * before this one: 65 stacked `config:` blocks left block 65 live at the head
   * of the returned code (0.1 ms), and the only thing that kept mermaid away
   * was `looksRenderable` reading `---` as the head and declining to render at
   * all — a permanent "Rendering the diagram…" with no message to the reader,
   * and a hole the moment anything teaches `looksRenderable` to look past a
   * preamble it did not strip. Such a source is not a diagram either way:
   * mermaid reads only the FIRST block, so blocks 2..65 are body and a parse
   * error. Refusing costs nothing and says so out loud.
   */
  if (passes >= MAX_PREAMBLE_PASSES && FRONTMATTER_RE.test(head + out)) {
    return { code: head + out, refusal: 'it stacks more preamble blocks than we will strip' };
  }
  return { code: head + out, refusal: '' };
}

/**
 * Why this source may not be rendered at all, or `''`.
 *
 * This asks the FIXED POINT (`guardedSanitize`), not the guard's single pass:
 * the string the renderer is handed is `prepareDiagramSource`'s, so the refusal
 * has to be the one that string earns. Asking the guard alone would let a
 * refusal the statement pass exposes go unreported and the residue be drawn.
 */
export function diagramRefusal(code: string): string {
  return guardedSanitize(code).refusal;
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
 * What is removed: the PREAMBLE, via `guardDiagramSource` — a YAML frontmatter
 * block reduced to at most a `title:` line, and `%%{ … }%%` directives wherever
 * they appear, including the multi-line form — and then any `classDef`,
 * `style`, `linkStyle` or `click` STATEMENT, whether it opens its line or
 * follows a `;`.
 *
 * The preamble is removed FIRST and by that guard, not here, so a frontmatter
 * `themeCSS` block can never be read as diagram statements: its CSS lines are
 * full of `{`, `}` and `;` and `splitStatements` has no business seeing them.
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
/**
 * The colour words mermaid's `box` statement accepts, as `CSS.supports` judges
 * them: every CSS named colour, plus the keywords that are legal `color`
 * values. Closed by the CSS spec, and DRIFT IS COSMETIC — a colour missing from
 * here survives as a word in the box's title, never as its fill, because
 * `transparent` is written into the segment mermaid reads as the colour either
 * way. `#rrggbb` is absent on purpose: mermaid does not accept a hex colour on
 * a `box` at all ("#hex codes are not supported for now because of the way the
 * char # is handled" — its own comment).
 */
const BOX_COLOUR_WORDS = [
  'aliceblue', 'antiquewhite', 'aqua', 'aquamarine', 'azure', 'beige', 'bisque',
  'black', 'blanchedalmond', 'blue', 'blueviolet', 'brown', 'burlywood',
  'cadetblue', 'chartreuse', 'chocolate', 'coral', 'cornflowerblue', 'cornsilk',
  'crimson', 'cyan', 'darkblue', 'darkcyan', 'darkgoldenrod', 'darkgray',
  'darkgreen', 'darkgrey', 'darkkhaki', 'darkmagenta', 'darkolivegreen',
  'darkorange', 'darkorchid', 'darkred', 'darksalmon', 'darkseagreen',
  'darkslateblue', 'darkslategray', 'darkslategrey', 'darkturquoise',
  'darkviolet', 'deeppink', 'deepskyblue', 'dimgray', 'dimgrey', 'dodgerblue',
  'firebrick', 'floralwhite', 'forestgreen', 'fuchsia', 'gainsboro',
  'ghostwhite', 'gold', 'goldenrod', 'gray', 'green', 'greenyellow', 'grey',
  'honeydew', 'hotpink', 'indianred', 'indigo', 'ivory', 'khaki', 'lavender',
  'lavenderblush', 'lawngreen', 'lemonchiffon', 'lightblue', 'lightcoral',
  'lightcyan', 'lightgoldenrodyellow', 'lightgray', 'lightgreen', 'lightgrey',
  'lightpink', 'lightsalmon', 'lightseagreen', 'lightskyblue', 'lightslategray',
  'lightslategrey', 'lightsteelblue', 'lightyellow', 'lime', 'limegreen',
  'linen', 'magenta', 'maroon', 'mediumaquamarine', 'mediumblue',
  'mediumorchid', 'mediumpurple', 'mediumseagreen', 'mediumslateblue',
  'mediumspringgreen', 'mediumturquoise', 'mediumvioletred', 'midnightblue',
  'mintcream', 'mistyrose', 'moccasin', 'navajowhite', 'navy', 'oldlace',
  'olive', 'olivedrab', 'orange', 'orangered', 'orchid', 'palegoldenrod',
  'palegreen', 'paleturquoise', 'palevioletred', 'papayawhip', 'peachpuff',
  'peru', 'pink', 'plum', 'powderblue', 'purple', 'rebeccapurple', 'red',
  'rosybrown', 'royalblue', 'saddlebrown', 'salmon', 'sandybrown', 'seagreen',
  'seashell', 'sienna', 'silver', 'skyblue', 'slateblue', 'slategray',
  'slategrey', 'snow', 'springgreen', 'steelblue', 'tan', 'teal', 'thistle',
  'tomato', 'turquoise', 'violet', 'wheat', 'white', 'whitesmoke', 'yellow',
  'yellowgreen',
  'transparent', 'currentcolor', 'inherit', 'initial', 'unset', 'revert',
  'revert-layer',
];

/** A leading `box` colour: a colour function, or one of the words above. */
const BOX_COLOUR = new RegExp(
  '^(?:(?:rgba?|hsla?|hwb|lab|lch|oklab|oklch|color|color-mix)\\s*\\([^)]*\\)' +
    `|(?:${BOX_COLOUR_WORDS.join('|')}))(?![\\w-])[ \\t]*`,
  'i',
);

/**
 * The colour statements that are NOT `classDef`/`style`/`linkStyle`/`click`.
 *
 * `COLOUR_DIRECTIVE` is a flowchart-family vocabulary, and three other diagram
 * types the app renders carry a paint channel of their own. None of these is a
 * regression of this branch — all three painted identically on the commit
 * before it — but this branch IS the colour ban, and measured today in Chromium
 * 153.0.8010.36 / mermaid 11.17.0 each one put the attacker's literal colour on
 * screen in a chat answer:
 *
 *  - `C4Context … UpdateElementStyle(p, $bgColor="#ff0000", $fontColor="#00ff00",
 *    $borderColor="#ff00ff")` drew node `mmd-1-p` at computed fill
 *    rgb(255, 0, 0). `UpdateRelStyle` does the same to an arrow, and
 *    `UpdateLayoutConfig` re-lays the diagram out. All three are presentation
 *    only: a C4 diagram draws its full graph without them, so the whole
 *    statement goes.
 *  - `sequenceDiagram … box rgb(255,0,0) Hot path` emitted
 *    `<rect … fill="rgb(255,0,0)">` behind the actors. `box` is STRUCTURAL —
 *    it groups actors and is closed by `end` — so the statement has to survive
 *    with its colour replaced. `transparent` is not a guess: mermaid's own
 *    `parseBoxData` matches `^((?:rgba?|hsla?)\s*\(.*\)|\w*)(.*)$`, tests the
 *    first segment with `CSS.supports('color', …)` and falls back to
 *    `transparent` when it fails, so an uncoloured box IS a transparent box.
 *    Writing `transparent` into that first segment is what makes this rule
 *    FAIL CLOSED: whatever follows is the title to mermaid, so a colour this
 *    code did not recognise can only end up as TEXT in a label, never as paint.
 *    The recognised token is then dropped so the label does not read
 *    "rgb(255,0,0) Hot path", and `BOX_COLOUR_WORDS` being incomplete costs a
 *    stray word in a title and nothing else.
 *  - `quadrantChart … "A": [0.7, 0.8] radius: 20, color: #ff0000,
 *    stroke-color: #00ff00, stroke-width: 6px` emitted
 *    `<circle … fill="#ff0000" stroke="#00ff00" stroke-width="6px">`. The
 *    coordinates are the data and the clause after `]` is the paint, so the
 *    clause goes and the point stays where the author put it. A `:::name` is
 *    kept: it is inert once its `classDef` has been stripped, exactly as in the
 *    flowchart family.
 *
 * Each rule is gated on the diagram's own HEAD, which is narrower than a
 * line-anchored keyword on purpose and for the reason `CLASS_STATEMENT` gives:
 * outside its own grammar `box` is an ordinary flowchart node id
 * (`box[Label] --> other`) and deleting or rewriting it would delete the
 * author's graph.
 */
function stripTypedColourStatements(code: string): string {
  const head = diagramHead(code);
  if (head.startsWith('c4')) {
    return code
      .split('\n')
      .filter((line) => !/^\s*Update(?:ElementStyle|RelStyle|LayoutConfig)\s*\(/i.test(line))
      .join('\n');
  }
  if (head.startsWith('sequencediagram')) {
    return code
      .split('\n')
      .map((line) => {
        const box = /^(\s*box)[ \t]+(.*)$/.exec(line);
        if (!box) return line;
        // Drop the leading token only when it IS a colour. mermaid sets the
        // title to the WHOLE string when the first word is not one, so eating
        // it unconditionally would delete a word of `box Hot path`.
        const title = box[2].replace(BOX_COLOUR, '');
        return `${box[1]} transparent${title ? ` ${title}` : ''}`;
      })
      .join('\n');
  }
  if (head.startsWith('quadrantchart')) {
    return code
      .split('\n')
      .map((line) => {
        const point = /^(\s*(?:"[^"\n]*"|[^:\n]+?)\s*:\s*\[[^\]\n]*\])(.*)$/.exec(line);
        if (!point) return line;
        const role = /^\s*(:::[\w-]+)\s*$/.exec(point[2]);
        return role ? `${point[1]}${role[1]}` : point[1];
      })
      .join('\n');
  }
  return code;
}

function stripColourStatements(code: string): string {
  return code
    .split('\n')
    .map((line) => {
      const parts = splitStatements(line);
      if (!parts.some((p) => COLOUR_DIRECTIVE.test(p))) return line;
      const kept = parts.filter((p) => p.trim() && !COLOUR_DIRECTIVE.test(p));
      return kept.join(';');
    })
    .filter((line, i, all) => !(line === '' && all[i - 1] === ''))
    .join('\n')
    .replace(/^\s*\n/, '');
}

/**
 * The guard and the statement pass, alternated TO A FIXED POINT.
 *
 * The guard's own loop already handles a preamble promoted by removing the
 * preamble above it. This loop handles the other direction, which the guard
 * cannot see because it runs first: THE STATEMENT PASS ALSO PROMOTES.
 *
 *     classDef zz fill:#fff
 *     ---
 *     config:
 *       themeCSS: |
 *         .node rect { fill: #ff0000 !important }
 *     ---
 *     flowchart TD
 *       SVC[Gateway]:::service --> PLAIN[Plain node]
 *
 * `FRONTMATTER_RE` is anchored at `^`, so with a statement on line 1 the guard
 * sees no frontmatter at all and returns the text unchanged. The statement pass
 * then empties line 1, the trim deletes it, and the block lands at column 0 —
 * live. Measured on the commit before this one with mermaid 11.17.0's own
 * `frontMatterRegex`: mermaid read `config: [themeCSS]`, and for the `c4:`
 * variant `config: [c4]`, out of the string the app handed it. A leading
 * `click`, `style` or `linkStyle` line does the same, and so does a `classDef`
 * followed by a whitespace-only line.
 *
 * Nothing but `looksRenderable` stopped those: it read `classdef` as the head,
 * found no diagram type and never called `mermaid.render`, so the reader got a
 * permanent "Rendering the diagram…" and no message (rendered=false, err=null,
 * measured both commits). That is an accident of a check written for streaming,
 * and the engineer's own plan to teach `looksRenderable` to share this guard
 * would have ARMED every one of them.
 *
 * So: guard, strip, guard again, until the text stops changing. Each round
 * either deletes something or terminates, and exhausting the bound is a
 * refusal for the same reason it is inside the guard.
 */
function guardedSanitize(code: string): GuardedSource {
  if (!code) return { code: '', refusal: '' };
  let out = code;
  for (let pass = 0; pass < MAX_PREAMBLE_PASSES; pass += 1) {
    const guarded = guardDiagramSource(out);
    if (guarded.refusal) return guarded;
    const next = stripTypedColourStatements(stripColourStatements(guarded.code));
    if (next === out) return { code: next, refusal: '' };
    out = next;
  }
  const guarded = guardDiagramSource(out);
  if (guarded.refusal) return guarded;
  if (FRONTMATTER_RE.test(guarded.code)) {
    return {
      code: guarded.code,
      refusal: 'it stacks more preamble blocks than we will strip',
    };
  }
  return { code: guarded.code, refusal: '' };
}

export function sanitizeDiagramSource(code: string): string {
  if (!code) return '';
  return guardedSanitize(code).code.replace(/\n{3,}/g, '\n\n');
}

/** The characters a role name is made of, after `:::`. */
const ROLE_NAME_CHAR = /[\w-]/;

/**
 * A whole `class A,B role` statement — the other way a class is attached.
 *
 * `class\s+` and not `class`, so `classDef` (handled by COLOUR_DIRECTIVE) can
 * never match this, and a `classDiagram`'s `class Foo {` cannot either: it ends
 * in a brace, and a classDiagram takes our classDefs anyway, so this is never
 * run over one.
 *
 * The trailing name must be one of the four ROLES, which is narrower than
 * "any class statement" on purpose: outside the flowchart family a line is
 * only a statement in SOME grammars, and in a `mindmap` or a `timeline` the
 * words are free text — a node whose label happens to read "class diagram"
 * must not be deleted. What this defends against is the one shape the prompt
 * now teaches, in the one place it is fatal.
 */
const CLASS_STATEMENT = /^\s*class\s+[\w,.-]+\s+([\w-]+)\s*$/;

function isRoleClassStatement(part: string): boolean {
  const match = CLASS_STATEMENT.exec(part);
  return !!match && (DIAGRAM_ROLES as readonly string[]).includes(match[1]);
}

/** Drop every `:::role` from ONE statement, leaving quoted labels alone. */
function withoutRoleSuffixes(part: string): string {
  if (!part.includes(':::')) return part;
  let out = '';
  let inQuote = false;
  for (let i = 0; i < part.length; i += 1) {
    const ch = part[i];
    if (inQuote) {
      out += ch;
      if (ch === '"') inQuote = false;
      continue;
    }
    if (ch === '"') {
      inQuote = true;
      out += ch;
      continue;
    }
    if (ch === '%' && part[i + 1] === '%') {
      // A comment runs to end of line and is inert: kept verbatim.
      out += part.slice(i);
      break;
    }
    if (ch === ':' && part.startsWith(':::', i)) {
      let j = i + 3;
      while (j < part.length && ROLE_NAME_CHAR.test(part[j])) j += 1;
      i = j - 1;
      continue;
    }
    out += ch;
  }
  return out;
}

/** What is left of a statement once its role is gone: just an identifier. */
const BARE_ID = /^\s*[\w.-]+\s*$/;

/**
 * The same diagram with every role APPLICATION removed — the RETRY source.
 *
 * Measured today, Chromium 153 / mermaid 11.17: outside the flowchart family a
 * role is not ignored, it is FATAL. In a sequenceDiagram `U:::external` raises
 * "Parse error on line 6 … Expecting '()', 'SOLID_OPEN_ARROW', … got 'TXT'"
 * and `class U external` the same error with "got 'NEWLINE'". The block
 * catches it and the answer shows source under "Couldn't render this diagram"
 * — no diagram at all, which is strictly worse than the grey one this track is
 * fixing. The orchestrator's prompt now teaches the `:::` form
 * (`DIAGRAM_ROLES` in orchestrator/app/engines) and says it is for
 * `flowchart`/`graph` only; this is the half that does not depend on the model
 * obeying.
 *
 * WHY THIS IS A RETRY AND NOT A FILTER
 * -----------------------------------
 * MermaidBlock calls it only after a render has already THROWN, so a diagram
 * that works is never touched — which is what makes it safe to be blunt here.
 * A filter on the happy path would have to know, per diagram type, whether a
 * role is fatal (sequenceDiagram), inert (an unknown name in a flowchart:
 * measured, it degrades to the default node, which DIAG-19 pins) or legal, and
 * whether the identifier left behind is a statement or a node's own label — a
 * bare word is content in `mindmap` and in `timeline`. Guessing that per
 * grammar is how a filter deletes a node nobody asked it to touch. After a
 * failure the trade is unambiguous: the role could not have been painted (we
 * append classDefs only where the grammar takes them), so dropping it and the
 * identifier it leaves behind costs at most one node of a diagram that was
 * showing nothing.
 *
 * Three removals, in order, per STATEMENT rather than per line — `;` is a
 * separator and a role can ride behind one:
 *   1. `:::role` wherever it appears outside a quoted label,
 *   2. a whole `class A,B <role>` statement, role names only (see
 *      CLASS_STATEMENT: a `mindmap` label reading "class diagram" is content),
 *   3. a statement a removal has reduced to a bare identifier, which is what
 *      `U:::external` leaves behind and is itself a parse error where the
 *      original was.
 */
export function withoutRoleApplications(code: string): string {
  if (!code || (!code.includes(':::') && !/\bclass\s/.test(code))) return code;
  const out: string[] = [];
  for (const line of code.split('\n')) {
    const parts = splitStatements(line);
    const kept: string[] = [];
    for (const part of parts) {
      if (isRoleClassStatement(part)) continue;
      const stripped = withoutRoleSuffixes(part);
      // Only a statement we CHANGED can have been reduced to a bare id; an
      // identifier the author wrote on its own is left alone.
      if (stripped !== part && BARE_ID.test(stripped)) continue;
      kept.push(stripped);
    }
    const rebuilt = kept.join(';');
    if (rebuilt === line) {
      out.push(line);
      continue;
    }
    // A line that was ONLY a role application leaves no statement behind;
    // pushing the empty string would leave a blank line where it was.
    if (rebuilt.trim()) out.push(rebuilt);
  }
  return out.join('\n');
}

/**
 * The source that is RENDERED: sanitised, with our role classDefs appended
 * where the grammar takes them.
 *
 * The same string is what the Code tab shows, so what a person copies is what
 * was drawn — including when the block has had to fall back to
 * `withoutRoleApplications` to draw anything at all.
 */
export function prepareDiagramSource(
  code: string,
  mode: ThemeMode,
  root?: Element | null,
): string {
  const clean = sanitizeDiagramSource(code);
  // A refused source is never rendered, so it gets no classDefs either: four
  // `classDef` lines under a "this diagram was refused" notice would be the
  // app's own text presented as the author's.
  if (!clean.trim() || diagramRefusal(code) || !acceptsClassDefs(clean)) return clean;
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
