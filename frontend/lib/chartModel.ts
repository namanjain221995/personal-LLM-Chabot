/**
 * ChartModel: the one shape every chat number chart is drawn from
 * (charts design v2 §2).
 *
 * Two inputs reach it: a validated `meta.chart` ChartSpec + its rows
 * (`modelFromSpec`), and a Mermaid `xychart-beta` / `pie` block parsed by
 * lib/mermaidChart.ts. Both produce the same model, so the form chooser, the
 * layout planner and both renderers (the HTML bar list and the ECharts
 * canvas) never care where a chart came from.
 *
 * Identity is by ID, never by display text (C12): categories are `c0..cN`,
 * series `s0..sN`. ECharts only ever sees those ids as category values, series
 * names and pie item names; display text reaches the canvas through trusted
 * formatters that look the id up. Two records that share a name, or a
 * `total_count` column next to a `Total count` one, therefore never merge
 * their legend toggles or tooltip rows.
 *
 * Pure module: no DOM, no React, no echarts.
 */

import type { ChartSpec, ChartType, DataRow } from './types';
import {
  type Cell,
  type DateGrain,
  formatCategory,
  formatDateByGrain,
  humanizeKey,
  isNumeric,
  parseCalendar,
  toNumber,
} from './chartFormat';
import { SERIES_SLOTS } from './chartTheme';

// ---------------------------------------------------------------------------
// Types and validation (moved here from lib/chartOption.ts, re-exported there)
// ---------------------------------------------------------------------------

/** The nine types this renderer draws. Anything else is not rendered. */
export const CHART_TYPES: readonly ChartType[] = [
  'bar',
  'line',
  'area',
  'pie',
  'scatter',
  'horizontal_bar',
  'donut',
  'funnel',
  'histogram',
] as const;

const TYPE_SET = new Set<string>(CHART_TYPES);

/** Types that draw shares of a whole and need one non-negative measure. */
export const PART_TO_WHOLE = new Set<string>(['pie', 'donut']);

export function isChartType(value: unknown): value is ChartType {
  return typeof value === 'string' && TYPE_SET.has(value);
}

export type ChartProblem =
  | 'no-data'
  | 'unsupported-type'
  | 'missing-x-column'
  | 'missing-y-column'
  | 'scatter-needs-numeric-x'
  | 'no-numeric-values'
  | 'part-to-whole-needs-positive-values';

/**
 * Why this spec cannot be drawn, or null when it can.
 *
 * The backend already validated the spec against the real result columns.
 * This is the same check on the client, because a persisted conversation
 * can be replayed with rows that a later change reshaped, and a chart that
 * silently draws zeros is worse than one that says it cannot draw.
 *
 * NOTE (design v2 §3, C3): the "total 0" verdict is reported here unchanged,
 * but `modelFromSpec` runs the all-zero rules BEFORE it, so the all-zero donut
 * gets an empty state instead of this message.
 */
export function validateChart(
  spec: ChartSpec | null | undefined,
  input: DataRow[] | null | undefined,
): ChartProblem | null {
  if (!spec || !isChartType(spec.type)) return 'unsupported-type';
  const rows = cleanRows(input);
  if (rows.length === 0) return 'no-data';
  const present = new Set<string>();
  for (const row of rows) for (const k of Object.keys(row)) present.add(k);
  if (typeof spec.x_key !== 'string' || !spec.x_key || !present.has(spec.x_key)) return 'missing-x-column';
  const yKeys = yKeyList(spec).filter((k) => present.has(k));
  if (yKeys.length === 0) return 'missing-y-column';

  if (spec.type === 'scatter') {
    const numericX = rows.some((r) => isNumeric(r[spec.x_key] as Cell));
    if (!numericX) return 'scatter-needs-numeric-x';
  }
  const anyNumeric = yKeys.some((k) => rows.some((r) => isNumeric(r[k] as Cell)));
  if (!anyNumeric) return 'no-numeric-values';

  if (PART_TO_WHOLE.has(spec.type)) {
    const total = rows.reduce((sum, r) => sum + Math.max(toNumber(r[yKeys[0]] as Cell) ?? 0, 0), 0);
    if (total <= 0) return 'part-to-whole-needs-positive-values';
  }
  return null;
}

export type SeriesKind = 'bar' | 'line' | 'area';

export interface ModelCategory {
  /** `c${i}` — the only thing ECharts sees. */
  id: string;
  /** Full display text, never shortened. */
  label: string;
  /** UTC ms of the calendar parts, for a time x. */
  t?: number;
  /** A row exists for this slot; false only for a filled-in empty period. */
  present?: boolean;
}

export interface ModelSeries {
  /** `s${i}` — the only thing ECharts sees. */
  id: string;
  /** Source column / Mermaid series name. */
  key: string;
  /** Display name. */
  label: string;
  kind: SeriesKind;
  /** Colour slot: the series' position in the original y_keys order. */
  slot: number;
  values: Array<number | null>;
}

export interface ChartModel {
  origin: 'spec' | 'mermaid';
  requested: ChartType | 'combo';
  title: string;
  x: {
    key: string;
    label: string;
    kind: 'time' | 'category' | 'number';
    grain?: DateGrain;
    cats: ModelCategory[];
    /** Rows with no x, left off a time axis and listed in a note. */
    nullRows: number;
    /** Indexes i with a gap between cats[i] and cats[i + 1] (time x only). */
    breaks: number[];
    /** Raw numeric x per category (scatter only). */
    nums?: Array<number | null>;
  };
  series: ModelSeries[];
  stacked: boolean;
  /** Draw values at bar tips / line ends (spec `show_values`). */
  showValues: boolean;
  /** Spec `show_legend: false` (honoured for single-series charts only). */
  hideLegend: boolean;
  valueFlags: {
    allInteger: boolean;
    anyNegative: boolean;
    /** Every drawn value is 0 or null. */
    allZero: boolean;
    /** Sum of the first series' positive values (part-to-whole). */
    total: number;
    /** A Mermaid pie whose values sum to 100 ± 0.5: values are percents. */
    percentWhole?: boolean;
  };
  axisCaptions?: { x?: string; y?: string };
  a11y?: { title?: string; description?: string };
  notes: string[];
  /** The charted columns, in the original row order, for the Table and CSV. */
  tableRows: DataRow[];
}

export type ModelResult = { model: ChartModel; problem?: undefined } | { model?: undefined; problem: ChartProblem };

// ---------------------------------------------------------------------------
// Time detection (design v2 §3.2, C8)
// ---------------------------------------------------------------------------

interface TimeDetect {
  grain: DateGrain;
  values: Array<{ t: number; zone: string | null } | null>;
}

const MONTH_MS = 30 * 86_400_000;

/**
 * x is time only when 100% of the non-null x values parse as calendar dates
 * (v1 accepted 90% and silently dropped the rest).
 */
export function detectTime(raw: Cell[]): TimeDetect | null {
  const parsed = raw.map((v) =>
    v === null || v === undefined || v === '' ? null : parseCalendar(v),
  );
  let seen = 0;
  for (let i = 0; i < raw.length; i++) {
    const v = raw[i];
    if (v === null || v === undefined || v === '') continue;
    if (!parsed[i]) return null;
    seen += 1;
  }
  if (seen === 0) return null;
  const vals = parsed.filter((p): p is NonNullable<typeof p> => p !== null);
  const allMonthForm = vals.every((p) => p.form === 'month');
  const allMidnight = vals.every((p) => p.h === 0 && p.mi === 0 && p.s === 0);
  const allDay1 = vals.every((p) => p.d === 1);
  let grain: DateGrain;
  if (allMonthForm) grain = 'month';
  else if (allMidnight && allDay1 && vals.length >= 2) grain = 'month';
  else if (allMidnight) grain = 'day';
  else grain = 'datetime';
  return { grain, values: parsed.map((p) => (p ? { t: p.t, zone: p.zone } : null)) };
}

export function monthIndex(t: number): number {
  const d = new Date(t);
  return d.getUTCFullYear() * 12 + d.getUTCMonth();
}

function median(nums: number[]): number {
  if (nums.length === 0) return 0;
  const s = [...nums].sort((a, b) => a - b);
  const mid = Math.floor(s.length / 2);
  return s.length % 2 ? s[mid] : (s[mid - 1] + s[mid]) / 2;
}

/**
 * Where a time line must stop (design v2 §3.3, C4). `connectNulls: false`
 * only affects null VALUES; a missing date is not a null, so a line would
 * draw straight across nine empty months. A break goes where:
 *   month grain — consecutive points are more than one month apart;
 *   day grain   — the gap is larger than max(7 days, 3 × median gap);
 *   datetime    — the gap is larger than 3 × median gap.
 */
export function timeBreaks(ts: number[], grain: DateGrain): number[] {
  const out: number[] = [];
  if (ts.length < 2) return out;
  if (grain === 'month') {
    for (let i = 0; i < ts.length - 1; i++) {
      if (monthIndex(ts[i + 1]) - monthIndex(ts[i]) > 1) out.push(i);
    }
    return out;
  }
  const gaps: number[] = [];
  for (let i = 0; i < ts.length - 1; i++) gaps.push(ts[i + 1] - ts[i]);
  const med = median(gaps.filter((g) => g > 0));
  const limit = grain === 'day' ? Math.max(7 * 86_400_000, 3 * med) : 3 * med;
  if (!(limit > 0)) return out;
  gaps.forEach((g, i) => {
    if (g > limit) out.push(i);
  });
  return out;
}

/** Units (months or days) the time span covers, inclusive. */
export function unitsInSpan(model: ChartModel): number {
  const cats = model.x.cats;
  if (model.x.kind !== 'time' || cats.length === 0) return cats.length;
  const first = cats[0].t ?? 0;
  const last = cats[cats.length - 1].t ?? 0;
  if (model.x.grain === 'month') return monthIndex(last) - monthIndex(first) + 1;
  if (model.x.grain === 'day') return Math.round((last - first) / 86_400_000) + 1;
  return cats.length;
}

/** Milliseconds of one grain unit (a month is approximated as 30 days). */
export function grainMs(grain: DateGrain | undefined): number {
  if (grain === 'month') return MONTH_MS;
  if (grain === 'day') return 86_400_000;
  return 0;
}

/**
 * The same model with every month (or day) between the first and last point
 * present as a category; missing units carry null values and `present: false`
 * (design v2 §3 rule 7: "missing units as labelled empty slots"). Returns null
 * when the grain is irregular or two rows share one unit.
 */
export function fillTimeRange(model: ChartModel, maxUnits = 400): ChartModel | null {
  const { x } = model;
  if (x.kind !== 'time' || (x.grain !== 'month' && x.grain !== 'day')) return null;
  const units = unitsInSpan(model);
  if (units > maxUnits || units < x.cats.length) return null;
  const keyOf = (t: number) => (x.grain === 'month' ? monthIndex(t) : Math.round(t / 86_400_000));
  const index = new Map<number, number>();
  for (let i = 0; i < x.cats.length; i++) {
    const k = keyOf(x.cats[i].t ?? 0);
    if (index.has(k)) return null; // two rows in one unit: cannot slot them
    index.set(k, i);
  }
  const first = new Date(x.cats[0].t ?? 0);
  const cats: ModelCategory[] = [];
  const pick: Array<number | null> = [];
  for (let u = 0; u < units; u++) {
    const t =
      x.grain === 'month'
        ? Date.UTC(first.getUTCFullYear(), first.getUTCMonth() + u, 1)
        : (x.cats[0].t ?? 0) + u * 86_400_000;
    const src = index.get(keyOf(t));
    cats.push({
      id: `c${u}`,
      label: src !== undefined ? x.cats[src].label : formatDateByGrain(t, x.grain),
      t,
      present: src !== undefined,
    });
    pick.push(src ?? null);
  }
  return {
    ...model,
    x: { ...x, cats, breaks: [] },
    series: model.series.map((s) => ({
      ...s,
      values: pick.map((i) => (i === null ? null : s.values[i])),
    })),
  };
}

// ---------------------------------------------------------------------------
// Series cap (design v2 §3 rule 16, C21)
// ---------------------------------------------------------------------------

/** Most series a chat chart draws; one per categorical colour slot. */
export const MAX_SERIES = SERIES_SLOTS;

function capSeries(
  series: ModelSeries[],
  stacked: boolean,
): { series: ModelSeries[]; hidden: number } {
  if (series.length <= MAX_SERIES) return { series, hidden: 0 };
  if (stacked) {
    // Fold the tail into one "Other" so every stack total stays true.
    const head = series.slice(0, MAX_SERIES - 1);
    const tail = series.slice(MAX_SERIES - 1);
    const n = head[0]?.values.length ?? 0;
    const values: Array<number | null> = [];
    for (let i = 0; i < n; i++) {
      let any = false;
      let sum = 0;
      for (const s of tail) {
        const v = s.values[i];
        if (v !== null && v !== undefined) {
          any = true;
          sum += v;
        }
      }
      values.push(any ? sum : null);
    }
    const other: ModelSeries = {
      id: `s${MAX_SERIES - 1}`,
      key: '__other__',
      label: `Other (${tail.length} series)`,
      kind: head[0]?.kind ?? 'bar',
      slot: MAX_SERIES - 1,
      values,
    };
    return { series: [...head, other], hidden: tail.length };
  }
  // Grouped / lines: the 8 with the largest Σ|v|, kept in y_keys order so the
  // colours follow the entity, never its rank.
  const weight = (s: ModelSeries) => s.values.reduce<number>((a, v) => a + Math.abs(v ?? 0), 0);
  const keep = new Set(
    [...series]
      .map((s, i) => ({ i, w: weight(s) }))
      .sort((a, b) => b.w - a.w || a.i - b.i)
      .slice(0, MAX_SERIES)
      .map((e) => e.i),
  );
  const kept = series
    .filter((_, i) => keep.has(i))
    .map((s, j) => ({ ...s, id: `s${j}`, slot: j }));
  return { series: kept, hidden: series.length - kept.length };
}

// ---------------------------------------------------------------------------
// Flags
// ---------------------------------------------------------------------------

export function computeFlags(series: ModelSeries[]): ChartModel['valueFlags'] {
  let allInteger = true;
  let anyNegative = false;
  let allZero = true;
  for (const s of series) {
    for (const v of s.values) {
      if (v === null || v === undefined) continue;
      if (!Number.isInteger(v)) allInteger = false;
      if (v < 0) anyNegative = true;
      if (v !== 0) allZero = false;
    }
  }
  const total = (series[0]?.values ?? []).reduce<number>((a, v) => a + Math.max(v ?? 0, 0), 0);
  return { allInteger, anyNegative, allZero, total };
}

// ---------------------------------------------------------------------------
// Spec → model
// ---------------------------------------------------------------------------

function usableYKeys(spec: ChartSpec, rows: DataRow[]): string[] {
  const present = new Set<string>();
  for (const row of rows) for (const k of Object.keys(row)) present.add(k);
  return yKeyList(spec).filter((k) => present.has(k));
}

/**
 * Review R1: a persisted or replayed `meta.chart` is not re-validated by the
 * backend, so its shape is not trusted here either. Rows that are not plain
 * objects are dropped, and `y_keys` that is not a list of strings counts as
 * no keys — the chart then reports a problem instead of throwing.
 */
export function cleanRows(input: unknown): DataRow[] {
  if (!Array.isArray(input)) return [];
  return input.filter((r): r is DataRow => r !== null && typeof r === 'object' && !Array.isArray(r));
}

function yKeyList(spec: ChartSpec): string[] {
  const keys: unknown = spec.y_keys;
  if (!Array.isArray(keys)) return [];
  return keys.filter((k): k is string => typeof k === 'string');
}

function kindFor(type: ChartType): SeriesKind {
  if (type === 'line') return 'line';
  if (type === 'area') return 'area';
  return 'bar';
}

/** Distinct display names for series whose humanised labels collide. */
function seriesLabels(keys: string[]): string[] {
  const human = keys.map(humanizeKey);
  const count = new Map<string, number>();
  for (const h of human) count.set(h.toLowerCase(), (count.get(h.toLowerCase()) ?? 0) + 1);
  // Two columns that humanise to the same words keep their raw names, so the
  // legend still tells them apart.
  return human.map((h, i) => ((count.get(h.toLowerCase()) ?? 0) > 1 ? keys[i] : h));
}

/**
 * Build the model for a ChartSpec over its rows.
 *
 * Returns a problem only for data that cannot be charted at all. The
 * all-zero part-to-whole case is NOT a problem here (C3): it becomes a model
 * whose `allZero` flag gives the reader an empty state with the table open.
 */
export function modelFromSpec(spec: ChartSpec, input: DataRow[]): ModelResult {
  const rows = cleanRows(input);
  const problem = validateChart(spec, rows);
  if (problem && problem !== 'part-to-whole-needs-positive-values') return { problem };

  const type = spec.type;
  const allKeys = usableYKeys(spec, rows);
  const singleMeasure = PART_TO_WHOLE.has(type) || type === 'funnel' || type === 'histogram';
  const yKeys = singleMeasure ? allKeys.slice(0, 1) : type === 'scatter' ? allKeys.slice(0, 3) : allKeys;
  const notes: string[] = [];

  // ---- x
  const rawX = rows.map((r) => r[spec.x_key] as Cell);
  const time = type === 'scatter' || type === 'histogram' ? null : detectTime(rawX);
  let order = rows.map((_, i) => i);
  let nullRows = 0;
  let cats: ModelCategory[];
  let xKind: ChartModel['x']['kind'] = 'category';
  let nums: Array<number | null> | undefined;

  if (time) {
    xKind = 'time';
    order = order.filter((i) => time.values[i] !== null);
    nullRows = rows.length - order.length;
    // A time axis is positioned by t; sort (stably) so gaps are measured in
    // time order. The Table keeps the original row order.
    order.sort((a, b) => (time.values[a]!.t - time.values[b]!.t) || a - b);
    cats = order.map((rowIndex, i) => {
      const v = time.values[rowIndex]!;
      return { id: `c${i}`, label: formatDateByGrain(v.t, time.grain, v.zone), t: v.t, present: true };
    });
    if (nullRows > 0) {
      notes.push(
        `${nullRows.toLocaleString()} row${nullRows === 1 ? ' has' : 's have'} no date and ${
          nullRows === 1 ? 'is' : 'are'
        } in the table only.`,
      );
    }
  } else if (type === 'scatter') {
    xKind = 'number';
    nums = rows.map((r) => toNumber(r[spec.x_key] as Cell));
    cats = rows.map((r, i) => ({ id: `c${i}`, label: formatCategory(r[spec.x_key] as Cell) }));
  } else {
    cats = rows.map((r, i) => ({ id: `c${i}`, label: formatCategory(r[spec.x_key] as Cell) }));
  }

  // ---- series
  const labels = seriesLabels(yKeys);
  let series: ModelSeries[] = yKeys.map((key, i) => ({
    id: `s${i}`,
    key,
    label: labels[i],
    kind: kindFor(type),
    slot: i,
    values: order.map((rowIndex) => toNumber(rows[rowIndex][key] as Cell)),
  }));

  // Part-to-whole: largest first, as the pie has always been drawn.
  if (PART_TO_WHOLE.has(type)) {
    const idx = cats.map((_, i) => i);
    const vals = series[0].values;
    idx.sort((a, b) => (vals[b] ?? -Infinity) - (vals[a] ?? -Infinity) || a - b);
    cats = idx.map((i, j) => ({ ...cats[i], id: `c${j}` }));
    series = series.map((s) => ({ ...s, values: idx.map((i) => s.values[i]) }));
  }

  const capped = capSeries(series, Boolean(spec.stacked));
  if (capped.hidden > 0) {
    notes.push(
      spec.stacked
        ? `${capped.hidden} smaller series are combined as "Other"; each is in the table.`
        : `${capped.hidden} more series ${capped.hidden === 1 ? 'is' : 'are'} in the table.`,
    );
  }
  series = capped.series;

  const breaks =
    time && (type === 'line' || type === 'area')
      ? timeBreaks(cats.map((c) => c.t ?? 0), time.grain)
      : [];

  // ---- table: x + every usable measure (capped ones included), original order.
  const tableRows: DataRow[] = rows.map((r) => {
    const out: DataRow = { [spec.x_key]: r[spec.x_key] };
    for (const k of allKeys) out[k] = r[k];
    return out;
  });

  const model: ChartModel = {
    origin: 'spec',
    requested: type,
    title: spec.title ?? '',
    x: {
      key: spec.x_key,
      label: humanizeKey(spec.x_key),
      kind: xKind,
      grain: time?.grain,
      cats,
      nullRows,
      breaks,
      nums,
    },
    series,
    stacked: Boolean(spec.stacked),
    showValues: Boolean(spec.show_values),
    hideLegend: spec.show_legend === false && series.length < 2,
    valueFlags: computeFlags(series),
    axisCaptions:
      type === 'scatter'
        ? { x: humanizeKey(spec.x_key), y: series.length === 1 ? series[0].label : undefined }
        : undefined,
    notes,
    tableRows,
  };
  return { model };
}

/** Every visible series' value at category `i`, for summaries and the live region. */
export function valuesAt(model: ChartModel, i: number, hidden?: ReadonlySet<string>) {
  return model.series
    .filter((s) => !hidden?.has(s.id))
    .map((s) => ({ series: s, value: s.values[i] ?? null }));
}
