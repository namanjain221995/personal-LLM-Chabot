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

// ---------------------------------------------------------------------------
// Chart v2 formatting (design v2 §6). Every function below is trusted code;
// the backend and the model only ever supply the VALUES they format.
// ---------------------------------------------------------------------------

/**
 * Value text for tooltips, tip labels, legend values and the table.
 *
 * Like `formatNumber`, except that a non-integer below 1 in magnitude keeps
 * three significant digits: 0.0042 used to print as "0" (C20).
 */
export function formatValue(v: number | null | undefined): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return '—';
  if (!Number.isInteger(v) && Math.abs(v) < 1) {
    return v.toLocaleString(undefined, { maximumSignificantDigits: 3 });
  }
  return formatNumber(v);
}

/**
 * One tick style per value axis, chosen from the axis' largest magnitude:
 * full grouped numbers below 10,000, compact (12.3k) from there up. Mixing the
 * two on one axis ("9,500" next to "10k") read as two scales.
 */
export function axisTickFormatter(maxAbs: number): (v: number) => string {
  if (Math.abs(maxAbs) >= COMPACT_THRESHOLD) return compactTick;
  return (v: number) => (Number.isInteger(v) ? formatInteger(v) : formatValue(v));
}

/** Compact from 1,000 up, so every tick on a compact axis reads the same way. */
function compactTick(v: number): string {
  const abs = Math.abs(v);
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
  return formatValue(v);
}

/** A share of a whole, one decimal place. */
export function formatShare(part: number, total: number): string {
  if (!(total > 0)) return '—';
  const pct = (part / total) * 100;
  return `${pct.toLocaleString(undefined, { minimumFractionDigits: 1, maximumFractionDigits: 1 })}%`;
}

/**
 * Category label for a raw x cell. Numbers are NOT grouped (the year 2024
 * stayed "2,024" through formatNumber) and strings are shown as written.
 */
export function formatCategory(v: Cell): string {
  if (v === null || v === undefined || v === '') return '—';
  if (typeof v === 'boolean') return v ? 'Yes' : 'No';
  return String(v);
}

// ------------------------------------------------------------------- dates

export type DateGrain = 'day' | 'month' | 'datetime';

/** Calendar parts of one x value, parsed without any timezone conversion. */
export interface CalendarValue {
  /** UTC milliseconds of the calendar parts as written (never shifted). */
  t: number;
  /** Which form the text used. */
  form: 'month' | 'date' | 'datetime';
  y: number;
  m: number;
  d: number;
  h: number;
  mi: number;
  s: number;
  /** A `Z` or `±HH:MM` suffix, shown as written; null when absent. */
  zone: string | null;
}

const RX_MONTH = /^(\d{4})-(\d{2})$/;
const RX_DATE = /^(\d{4})-(\d{2})-(\d{2})$/;
const RX_DATETIME =
  /^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.\d{1,9})?)?(Z|[+-]\d{2}:?\d{2})?$/;

/**
 * Parse `YYYY-MM`, `YYYY-MM-DD` or `YYYY-MM-DD[T| ]HH:MM[:SS[.fff]][Z|±HH:MM]`
 * from its calendar parts. A suffixed zone is kept as text and never applied,
 * so a monthly bucket never slides to the previous day west of UTC.
 */
export function parseCalendar(raw: Cell): CalendarValue | null {
  if (typeof raw !== 'string') return null;
  const s = raw.trim();
  let m: RegExpExecArray | null;
  let parts: [number, number, number, number, number, number];
  let form: CalendarValue['form'];
  let zone: string | null = null;
  if ((m = RX_MONTH.exec(s))) {
    parts = [Number(m[1]), Number(m[2]), 1, 0, 0, 0];
    form = 'month';
  } else if ((m = RX_DATE.exec(s))) {
    parts = [Number(m[1]), Number(m[2]), Number(m[3]), 0, 0, 0];
    form = 'date';
  } else if ((m = RX_DATETIME.exec(s))) {
    parts = [
      Number(m[1]),
      Number(m[2]),
      Number(m[3]),
      Number(m[4]),
      Number(m[5]),
      Number(m[6] ?? 0),
    ];
    form = 'datetime';
    zone = m[7] ?? null;
  } else {
    return null;
  }
  const [y, mo, d, h, mi, sec] = parts;
  if (mo < 1 || mo > 12 || d < 1 || d > 31 || h > 23 || mi > 59 || sec > 60) return null;
  const t = Date.UTC(y, mo - 1, d, h, mi, sec);
  const back = new Date(t);
  // 2026-02-31 is not a date; Date.UTC would roll it into March.
  if (back.getUTCMonth() !== mo - 1 || back.getUTCDate() !== d) return null;
  return { t, form, y, m: mo, d, h, mi, s: sec, zone };
}

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

const pad2 = (n: number) => String(n).padStart(2, '0');

/** Full date text by grain, from UTC calendar parts: `12 Mar 2026`, `Mar 2026`, `12 Mar 2026, 14:30`. */
export function formatDateByGrain(t: number, grain: DateGrain, zone?: string | null): string {
  const d = new Date(t);
  if (Number.isNaN(d.getTime())) return '—';
  const mon = MONTHS[d.getUTCMonth()];
  const y = d.getUTCFullYear();
  if (grain === 'month') return `${mon} ${y}`;
  const day = `${d.getUTCDate()} ${mon} ${y}`;
  if (grain === 'day') return day;
  const time = `${pad2(d.getUTCHours())}:${pad2(d.getUTCMinutes())}`;
  return `${day}, ${time}${zone ? ` ${zone}` : ''}`;
}

/**
 * Axis text for a planner-chosen date tick (charts review V4/V16): the month
 * (or day and month), with the year only where `withYear` says so — on the
 * first label and wherever the year changes. Tooltips and the table keep the
 * full date.
 */
export function formatAxisDate(t: number, grain: DateGrain, withYear: boolean): string {
  const d = new Date(t);
  if (Number.isNaN(d.getTime())) return '';
  const mon = MONTHS[d.getUTCMonth()];
  const y = d.getUTCFullYear();
  if (grain === 'month') return withYear ? `${mon} ${y}` : mon;
  return withYear ? `${d.getUTCDate()} ${mon} ${y}` : `${d.getUTCDate()} ${mon}`;
}

/**
 * Short tick text for a time axis. The year is shown on the first tick and at
 * year starts; on an axis spanning about a year or more it is always shown
 * (a zoom window may hold neither), and a month-start tick reads "Jul 2025".
 */
export function formatTimeTick(t: number, grain: DateGrain, first: boolean, longSpan = false): string {
  const d = new Date(t);
  if (Number.isNaN(d.getTime())) return '';
  const mon = MONTHS[d.getUTCMonth()];
  const y = d.getUTCFullYear();
  if (grain === 'month') return `${mon} ${y}`;
  const atMidnightTick = d.getUTCHours() === 0 && d.getUTCMinutes() === 0;
  if (longSpan && d.getUTCDate() === 1 && atMidnightTick) return `${mon} ${y}`;
  const yearStart = d.getUTCMonth() === 0 && d.getUTCDate() <= 7;
  const day = `${d.getUTCDate()} ${mon}${first || yearStart || longSpan ? ` ${y}` : ''}`;
  if (grain === 'day') return day;
  const atMidnight = d.getUTCHours() === 0 && d.getUTCMinutes() === 0;
  return atMidnight ? day : `${day}, ${pad2(d.getUTCHours())}:${pad2(d.getUTCMinutes())}`;
}

/** Human form of a column key: `total_count` -> "Total count", `Amount__c` -> "Amount". */
export function humanizeKey(key: string): string {
  const base = String(key ?? '')
    .replace(/__c$/i, '')
    .replace(/_+/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
  if (!base) return String(key ?? '');
  return base.charAt(0).toUpperCase() + base.slice(1);
}

/** File-safe slug of a chart title for downloads. */
export function chartFileName(title: string, ext = 'png'): string {
  const slug =
    String(title ?? '')
      .toLowerCase()
      .normalize('NFKD')
      .replace(/[̀-ͯ]/g, '')
      .replace(/[^a-z0-9]+/g, '-')
      .replace(/^-+|-+$/g, '')
      .slice(0, 60)
      .replace(/-+$/g, '') || 'chart';
  return `${slug}.${ext}`;
}
