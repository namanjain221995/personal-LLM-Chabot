/**
 * Chart palette, resolved from the design system's CSS custom properties.
 *
 * `app/globals.css` has defined `--ts-chart-1..5` since the design system
 * landed, but the old Recharts renderer could not read them — Recharts
 * needs literal colors — so it hard-coded the same five hexes and they had
 * to be kept in sync by hand. ECharts has the same constraint (a canvas
 * cannot resolve `var()`), but it takes its colors at option-build time, so
 * we can resolve the tokens ourselves with getComputedStyle and pass the
 * concrete values through.
 *
 * The literal fallbacks below are the same five colors, used when there is
 * no DOM (SSR, tests) or when a token is missing. They are a safety net,
 * not a second source of truth: whatever globals.css says wins.
 *
 * The palette is the 8-slot categorical order (teal → blue → amber →
 * violet → rose → olive → magenta → orange) chosen for CVD separation and
 * ≥3:1 contrast on every surface a chart sits on, assigned in fixed order.
 * Slots 6-8 were added 2026-11-25 (charts design v2 §7.1) and validated with
 * the dataviz validator on #f4f4f4, #f8f8f8, #1e1e1e and #121212: worst
 * adjacent CVD ΔE 9.3 (deutan, olive↔rose), worst normal-vision ΔE 15.1.
 *
 * No color in here can come from the backend. `ChartSpec` has no color
 * field, by design.
 */

export interface ChartPalette {
  /** Series colors, in fixed assignment order. */
  series: string[];
  text: string;
  axis: string;
  grid: string;
  surface: string;
  tooltipBg: string;
  tooltipText: string;
  // --- chat-chart chrome (design v2 §7.3). NEW fields, so the admin console,
  // which reads `grid`/`axis`/`text` above, keeps its look (C17).
  /** Axis tick and label ink (AA on the chart surface). */
  axisText: string;
  /** Value-axis gridline: a solid, recessive hairline. */
  gridline: string;
  /** The zero / category baseline. */
  baseline: string;
  /** dataZoom handles and border (≥ 3:1 non-text contrast). */
  zoomHandle: string;
  zoomFill: string;
  /** Focus ring colour (the accent). */
  focus: string;
  /** Ordinal ramp for funnel stages, most emphatic first. */
  ordinal: string[];
}

export const SERIES_FALLBACK = [
  '#0E9D9A',
  '#2F6FB2',
  '#B7791F',
  '#6D5AE6',
  '#C0566B',
  '#4F771E',
  '#AA4BA9',
  '#E95C2C',
] as const;

const CHROME = {
  dark: {
    grid: '#262626',
    axis: '#262626',
    text: '#a3a3a3',
    surface: '#1e1e1e',
    tooltipBg: '#2a2a2a',
    tooltipText: '#f5f5f5',
    axisText: '#a3a3a3',
    gridline: '#333333',
    baseline: '#525252',
    zoomHandle: '#6b6b6b',
    zoomFill: 'rgba(96,165,250,0.18)',
    focus: '#60a5fa',
    // dark → light: on a dark surface the lighter step is the louder one, so
    // stage 1 takes the LAST step.
    // Review V11/A16: every step ≥ 3:1 on the card and drawer grounds.
    ordinal: ['#c4e1ff', '#a9cdfb', '#8eb9f0', '#73a5e4', '#5891d9', '#3b7dcd'],
  },
  light: {
    grid: '#e3e3e3',
    axis: '#e3e3e3',
    text: '#5d5d5d',
    surface: '#f4f4f4',
    tooltipBg: '#ffffff',
    tooltipText: '#0d0d0d',
    axisText: '#5d5d5d',
    gridline: '#dcdcdc',
    baseline: '#a8a8a8',
    zoomHandle: '#8a8a8a',
    zoomFill: 'rgba(29,78,216,0.12)',
    focus: '#1d4ed8',
    ordinal: ['#001c61', '#002f75', '#0a418b', '#1f54a0', '#3167b7', '#437bcd'],
  },
} as const;

/** Where a chart sits; the 2 px gaps and rings take that background (C19). */
export type ChartContext = 'inline' | 'drawer' | 'mermaid';

const SURFACES: Record<'dark' | 'light', Record<ChartContext, string>> = {
  // The drawer is `bg-surface/60` over the page, so its real ground is darker
  // (dark) or lighter (light) than the card surface.
  dark: { inline: '#1e1e1e', mermaid: '#1e1e1e', drawer: '#121212' },
  light: { inline: '#f4f4f4', mermaid: '#f4f4f4', drawer: '#f8f8f8' },
};

/** The background a chart in `context` actually sits on. */
export function surfaceFor(theme: 'dark' | 'light', context: ChartContext): string {
  return (SURFACES[theme] ?? SURFACES.dark)[context] ?? SURFACES.dark.inline;
}

export type ThemeName = keyof typeof CHROME;

const TOKEN_NAMES = [
  '--ts-chart-1',
  '--ts-chart-2',
  '--ts-chart-3',
  '--ts-chart-4',
  '--ts-chart-5',
  '--ts-chart-6',
  '--ts-chart-7',
  '--ts-chart-8',
] as const;

/** A CSS color we are willing to hand to a canvas renderer. */
function isUsableColor(value: string): boolean {
  const v = value.trim();
  if (!v) return false;
  // `var(--x)` would resolve to nothing inside a canvas; treat it as absent.
  if (v.startsWith('var(')) return false;
  return /^(#[0-9a-f]{3,8}|rgba?\(|hsla?\(|oklch\(|color\()/i.test(v) || /^[a-z]+$/i.test(v);
}

/**
 * Resolve `--ts-chart-1..8` against the document root.
 *
 * Returns the literal fallbacks when there is no DOM or a token is unset,
 * so this is safe to call during SSR and from tests.
 */
export function resolveSeriesColors(root?: Element | null): string[] {
  const el =
    root ?? (typeof document !== 'undefined' ? document.documentElement : null);
  if (!el || typeof window === 'undefined' || !window.getComputedStyle) {
    return [...SERIES_FALLBACK];
  }
  let style: CSSStyleDeclaration;
  try {
    style = window.getComputedStyle(el);
  } catch {
    return [...SERIES_FALLBACK];
  }
  return TOKEN_NAMES.map((name, i) => {
    const resolved = style.getPropertyValue(name);
    return isUsableColor(resolved) ? resolved.trim() : SERIES_FALLBACK[i];
  });
}

/** The full palette for `theme`. Series colors come from the CSS tokens. */
export function resolvePalette(theme: ThemeName, root?: Element | null): ChartPalette {
  const chrome = CHROME[theme] ?? CHROME.dark;
  return { series: resolveSeriesColors(root), ...chrome, ordinal: [...chrome.ordinal] };
}

/** Palette with no DOM involved — SSR, tests, and the error path. */
export function fallbackPalette(theme: ThemeName): ChartPalette {
  const chrome = CHROME[theme] ?? CHROME.dark;
  return { series: [...SERIES_FALLBACK], ...chrome, ordinal: [...chrome.ordinal] };
}

/** Number of categorical slots; the chart model never draws more series than this. */
export const SERIES_SLOTS = SERIES_FALLBACK.length;

/**
 * Series color for slot `i`. Chat charts cap their series at SERIES_SLOTS, so
 * the modulo only ever matters for the admin console's own callers.
 */
export function seriesColor(palette: ChartPalette, i: number): string {
  const colors = palette.series.length ? palette.series : [...SERIES_FALLBACK];
  return colors[i % colors.length];
}

/**
 * Status colours, as literals a canvas can paint.
 *
 * Same reason `SERIES_FALLBACK` is here: ECharts draws to a canvas and cannot
 * resolve `var()`, so the analytics console's "failed" series has to be given
 * a concrete hex. These mirror `--ts-danger` / `--ts-warn` / `--ts-ok` in
 * app/globals.css, which stays the single source of truth — `resolveTones`
 * reads the real values whenever there is a DOM, and these only stand in for
 * SSR and tests.
 */
export interface ChartTones {
  danger: string;
  warn: string;
  ok: string;
}

export const TONE_FALLBACK: ChartTones = {
  danger: '#ef5a5f',
  warn: '#f0a92e',
  ok: '#3fb950',
};

export function resolveTones(root?: Element | null): ChartTones {
  const el = root ?? (typeof document === 'undefined' ? null : document.documentElement);
  if (!el || typeof getComputedStyle !== 'function') return TONE_FALLBACK;
  const style = getComputedStyle(el);
  const token = (name: string, fallback: string) =>
    style.getPropertyValue(name).trim() || fallback;
  return {
    danger: token('--ts-danger', TONE_FALLBACK.danger),
    warn: token('--ts-warn', TONE_FALLBACK.warn),
    ok: token('--ts-ok', TONE_FALLBACK.ok),
  };
}
