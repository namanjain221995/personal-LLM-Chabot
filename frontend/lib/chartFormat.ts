/**
 * Application-owned value formatting for charts.
 *
 * Every formatter here is a TypeScript function that this file defines. The
 * backend cannot supply a format string, a locale, a currency symbol or a
 * callback: `ChartSpec` has no field that could carry one. That is
 * deliberate — an ECharts `formatter` may be a function, and a function
 * that arrived over the wire is code execution.
 *
 * Currency in particular is left alone unless we actually know it.
 * Salesforce amounts are not implicitly dollars; a multi-currency org
 * stores CurrencyIsoCode per record, and stamping "$" on a figure that is
 * really EUR is a wrong number wearing a right one's clothes. With no
 * currency metadata we print the plain number.
 *
 * Pure module — no DOM, no React. Tested directly under vitest's node
 * environment.
 */

/** Values a result cell can hold once JSON has been parsed. */
export type Cell = string | number | boolean | null | undefined;

const COMPACT_THRESHOLD = 10_000;

/** True for a value that can be treated as a chart measure. */
export function isNumeric(v: Cell): boolean {
  if (typeof v === 'number') return Number.isFinite(v);
  if (typeof v === 'string') {
    const s = v.trim();
    if (!s) return false;
    // Salesforce checkboxes arrive as the TEXT 'true'/'false'. They are not
    // measures, and Number('true') is NaN only by luck of implementation.
    if (s.toLowerCase() === 'true' || s.toLowerCase() === 'false') return false;
    return Number.isFinite(Number(s));
  }
  return false;
}

/** Coerce a cell to a finite number, or null. Booleans are NOT numbers. */
export function toNumber(v: Cell): number | null {
  if (typeof v === 'number') return Number.isFinite(v) ? v : null;
  if (typeof v === 'string' && isNumeric(v)) return Number(v.trim());
  return null;
}

export function formatInteger(v: number): string {
  return Math.round(v).toLocaleString();
}

export function formatDecimal(v: number, places = 2): string {
  return v.toLocaleString(undefined, {
    minimumFractionDigits: 0,
    maximumFractionDigits: places,
  });
}

export function formatPercent(v: number, places = 1): string {
  return `${formatDecimal(v, places)}%`;
}

/**
 * Compact form for axis ticks: 12.3k, 4.5M. Falls back to the full number
 * below the threshold so small counts stay exact.
 */
export function formatCompact(v: number): string {
  const abs = Math.abs(v);
  if (abs < COMPACT_THRESHOLD) return formatNumber(v);
  const units: Array<[number, string]> = [
    [1e12, 'T'],
    [1e9, 'B'],
    [1e6, 'M'],
    [1e3, 'k'],
  ];
  for (const [size, suffix] of units) {
    if (abs >= size) {
      const scaled = v / size;
      const text = Math.abs(scaled) >= 100 ? scaled.toFixed(0) : scaled.toFixed(1);
      return `${text.replace(/\.0$/, '')}${suffix}`;
    }
  }
  return formatNumber(v);
}

/** General number display: integers stay integers, decimals keep 2 places. */
export function formatNumber(v: number): string {
  if (!Number.isFinite(v)) return '—';
  return Number.isInteger(v) ? formatInteger(v) : formatDecimal(v);
}

/**
 * Format an amount in `currency` (an ISO 4217 code, e.g. from Salesforce's
 * CurrencyIsoCode). With no code, or an unusable one, the plain number is
 * returned rather than a guessed symbol.
 */
export function formatCurrency(v: number, currency?: string | null): string {
  const code = (currency || '').trim().toUpperCase();
  if (!/^[A-Z]{3}$/.test(code)) return formatNumber(v);
  try {
    return v.toLocaleString(undefined, { style: 'currency', currency: code });
  } catch {
    return `${formatNumber(v)} ${code}`;
  }
}

const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;
const ISO_DATETIME = /^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}/;

export function formatDate(v: Cell): string {
  const s = String(v ?? '');
  if (!ISO_DATE.test(s)) return s;
  const d = new Date(`${s}T00:00:00`);
  return Number.isNaN(d.getTime())
    ? s
    : d.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
}

export function formatDateTime(v: Cell): string {
  const s = String(v ?? '');
  if (!ISO_DATETIME.test(s)) return formatDate(v);
  const d = new Date(s);
  return Number.isNaN(d.getTime()) ? s : d.toLocaleString();
}

/** Axis-tick / label text for any cell. Never throws, never returns null. */
export function formatCell(v: Cell): string {
  if (v === null || v === undefined) return '—';
  if (typeof v === 'number') return formatNumber(v);
  if (typeof v === 'boolean') return v ? 'Yes' : 'No';
  const s = String(v);
  if (ISO_DATETIME.test(s)) return formatDateTime(s);
  if (ISO_DATE.test(s)) return formatDate(s);
  return s;
}

/** Trim a category label for an axis tick without losing which one it is. */
export function truncateLabel(label: string, max = 24): string {
  return label.length <= max ? label : `${label.slice(0, max - 1)}…`;
}
