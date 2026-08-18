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
 * The palette is the 5-slot categorical order (teal → blue → amber →
 * violet → rose) chosen for CVD separation and ≥3:1 contrast on both
 * surfaces, assigned in fixed order.
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
}

export const SERIES_FALLBACK = [
  '#0E9D9A',
  '#2F6FB2',
  '#B7791F',
  '#6D5AE6',
  '#C0566B',
] as const;

const CHROME = {
  dark: {
    grid: '#262626',
    axis: '#262626',
    text: '#a3a3a3',
    surface: '#1e1e1e',
    tooltipBg: '#2a2a2a',
    tooltipText: '#f5f5f5',
  },
  light: {
    grid: '#e3e3e3',
    axis: '#e3e3e3',
    text: '#5d5d5d',
    surface: '#f4f4f4',
    tooltipBg: '#ffffff',
    tooltipText: '#0d0d0d',
  },
} as const;

export type ThemeName = keyof typeof CHROME;

const TOKEN_NAMES = [
  '--ts-chart-1',
  '--ts-chart-2',
  '--ts-chart-3',
  '--ts-chart-4',
  '--ts-chart-5',
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
 * Resolve `--ts-chart-1..5` against the document root.
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
  return { series: resolveSeriesColors(root), ...chrome };
}

/** Palette with no DOM involved — SSR, tests, and the error path. */
export function fallbackPalette(theme: ThemeName): ChartPalette {
  const chrome = CHROME[theme] ?? CHROME.dark;
  return { series: [...SERIES_FALLBACK], ...chrome };
}

/** Series color for slot `i`, cycling only if a spec has >5 measures. */
export function seriesColor(palette: ChartPalette, i: number): string {
  const colors = palette.series.length ? palette.series : [...SERIES_FALLBACK];
  return colors[i % colors.length];
}
