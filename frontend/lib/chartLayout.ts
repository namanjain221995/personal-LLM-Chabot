/**
 * Form chooser and layout planner for chat number charts
 * (charts design v2 §3-§5).
 *
 * `chooseForm` keeps the chart type the backend (or the Mermaid block) asked
 * for unless the data or the width would make it unreadable, and says so in a
 * note when it changes the form. `planLayout` turns the chosen form into
 * pixels: margins measured from the real label text, per-category line
 * breaks, the x band, rotation, the zoom window and a `structuralKey` that
 * changes only when the drawn structure changes (C9).
 *
 * The rules that make text never truncate and never overlap live here:
 *   - every category label is broken by `wrapLines` at its band width, and
 *     the x band is as tall as the most lines any label needs;
 *   - labels that do not fit as columns go to the HTML bar list, where each
 *     row is as tall as its own label;
 *   - 45° labels only at ≥ 480 px, single-line, ≤ 16 characters, band ≥ 24;
 *   - a category line that still does not fit gets a zoom window, then
 *     numbered ticks with an HTML key;
 *   - histogram labels are thinned to a uniform interval (a continuous scale).
 *
 * Pure: the text measurer is injected.
 */

import type { ChartContext } from './chartTheme';
import {
  type ChartModel,
  fillTimeRange,
  grainMs,
  monthIndex,
  PART_TO_WHOLE,
  unitsInSpan,
} from './chartModel';
import { axisTickFormatter, formatAxisDate, formatDateByGrain, formatShare, formatValue } from './chartFormat';
import { type Measure, graphemes, lineHeightFor, widest, wrapLines, wrapLinesInfo } from './chartText';

export type ChartForm =
  | 'table'
  | 'empty'
  | 'stat'
  | 'pie'
  | 'donut'
  | 'funnel'
  | 'barlist'
  | 'columns'
  | 'catLine'
  | 'timeLine'
  | 'timeColumns'
  | 'timeBars'
  | 'histogram'
  | 'scatter'
  | 'combo';

export type Renderer = 'echarts' | 'barlist' | 'stat' | 'empty' | 'table';

export interface LayoutCtx {
  /** Content width of the figure, px. */
  width: number;
  context: ChartContext;
  /** `pointer: coarse` — larger slider and targets, no drag-pan. */
  coarse?: boolean;
  measure: Measure;
  /**
   * The offscreen PNG (review V1/R3): the image has no slider, so no zoom
   * window is planned. A category line that would window goes to numbered
   * ticks (thinned uniformly if even the numbers crowd) with the key drawn
   * into the image.
   */
  forExport?: boolean;
}

export interface FormChoice {
  form: ChartForm;
  /** The model to draw (a time bar chart may get its empty units filled in). */
  model: ChartModel;
  notes: string[];
  tableOpen: boolean;
  emptyMessage?: string;
  barList?: { ordered: boolean; percentOfFirst: boolean };
}

export interface ChartLayout {
  renderer: Renderer;
  form: ChartForm;
  model: ChartModel;
  width: number;
  /** Canvas height (ECharts) — reserved before the chunk loads. 0 for HTML forms. */
  height: number;
  grid: { top: number; right: number; bottom: number; left: number };
  plotHeight: number;
  /** id → label text, already broken with '\n' (category axes). */
  catText: Record<string, string>;
  /** Lines per category, in category order. */
  catLines: number[];
  lineHeight: number;
  rotate: 0 | 45;
  /** Category line, last ladder step: ticks are 1…n with an HTML key. */
  numbered: boolean;
  /** Histogram: label every k-th bin (first and last always). 1 = all. */
  labelEvery: number;
  /**
   * Initial dataZoom window, in axis values (category index or UTC ms).
   * `maxValueSpan` (category axes): the widest window the label breaks were
   * planned for — zooming out further would overlap them (review R2), so the
   * slider, the inside zoom and the keyboard all stop there. `maxSpanPct` is
   * the same limit in percent of the full range.
   */
  zoom: {
    startValue: number;
    endValue: number;
    slider: number;
    maxValueSpan?: number;
    maxSpanPct?: number;
  } | null;
  showSymbol: boolean;
  /** Draw value labels at column tips / line ends. */
  tipLabels: boolean;
  pie?: { diameter: number; legend: 'right' | 'below'; centreTotal: boolean };
  funnel?: { labelText: Record<string, string>; labelWidth: number };
  barList?: { ordered: boolean; percentOfFirst: boolean; cap: string };
  /**
   * Time axis extent. `ticks` + `tickText` (review V4): planner-chosen labels
   * for month and short day axes — every k-th unit, first and last always,
   * each measured so none overlaps; drawn through `axisLabel.customValues`.
   * Absent when ECharts chooses the ticks (windowed or long axes).
   */
  timeAxis?: {
    min: number;
    max: number;
    splitNumber: number;
    ticks?: number[];
    tickText?: Record<string, string>;
  };
  notes: string[];
  tableOpen: boolean;
  emptyMessage?: string;
  structuralKey: string;
}

export const PLOT_HEIGHT = 280;
export const PLOT_HEIGHT_NARROW = 240;
export const NARROW = 480;
export const MIN_BAND = 28;
export const MIN_TWO_LINE_BAND = 48;
export const MIN_THREE_LINE_BAND = 72;
export const MIN_ROTATE_BAND = 24;
export const MAX_ROTATE_CHARS = 16;
export const MIN_BAR_PX = 6;
export const MAX_CATEGORIES = 200;
export const LABEL_GUTTER = 6;
/** A line or area has no bar to keep wide: its points need only this much room (review V12). */
export const LINE_MIN_BAND = 16;
/** Category lines this short show every point (numbered if need be) rather than open on a window. */
export const SMALL_LINE = 12;
/** A bar + line combo's band floor on the ladder (bars there are ≤ 24 px). */
export const COMBO_MIN_BAND = 24;
/** Room between two planned time-axis labels. */
const TIME_LABEL_GAP = 12;
const AXIS_LABEL_MARGIN = 8;
const SIN45 = Math.SQRT1_2;

// ---------------------------------------------------------------------------
// Form chooser
// ---------------------------------------------------------------------------

function everyValueZero(model: ChartModel): boolean {
  return model.series.every((s) => s.values.every((v) => v === null || v === 0));
}

/** The design's rule table (§3), in order. */
export function chooseForm(input: ChartModel, ctx: LayoutCtx): FormChoice {
  const req = input.requested;
  const notes = [...input.notes];
  let model = input;
  // Review R5: two rows on one date would sit on one time-axis position and
  // hide each other. Draw every row as its own category instead (as v1 did).
  if (model.x.kind === 'time' && repeatedTime(model)) {
    model = {
      ...model,
      x: { ...model.x, kind: 'category', grain: undefined, breaks: [] },
    };
    notes.push('Some dates have more than one row, so each row is drawn on its own.');
  }
  const n = model.x.cats.length;
  const base = { model, notes, tableOpen: false };
  const timeX = model.x.kind === 'time';
  // Review R7: a time x that the time branch draws is exempt from rule 17;
  // a bar list never is (it has no row cap of its own).
  const timeDrawable =
    timeX && (req === 'line' || req === 'area' || req === 'bar' || req === 'horizontal_bar');

  // Rule 1 — part-to-whole with nothing to share.
  if ((PART_TO_WHOLE.has(req) || req === 'funnel') && everyValueZero(model)) {
    return {
      ...base,
      form: 'empty',
      tableOpen: true,
      emptyMessage: 'Every value is 0, so there are no shares to draw.',
    };
  }
  // Rule 17 — too many categories for any readable non-time chart.
  if (n > MAX_CATEGORIES && !timeDrawable && req !== 'scatter') {
    notes.push(`Too many categories to chart readably (${n.toLocaleString()}).`);
    return { ...base, form: 'table', tableOpen: true };
  }
  // Rule 2 — bars and lines that are all zero still draw, with a note.
  if (model.valueFlags.allZero && !PART_TO_WHOLE.has(req) && req !== 'funnel') {
    notes.push('Every value is 0.');
  }
  // Rule 3 — one category, one series: a figure, not a chart.
  if (n === 1 && model.series.length === 1 && req !== 'scatter') {
    return { ...base, form: 'stat' };
  }
  // Rules 4-5 — pie / donut.
  if (PART_TO_WHOLE.has(req)) {
    if (model.valueFlags.anyNegative) {
      notes.push('Shown as bars: a pie cannot show negative values.');
      return { ...base, form: 'barlist', barList: { ordered: true, percentOfFirst: false } };
    }
    if (n >= 7) {
      notes.push(`Shown as bars: ${n} parts are easier to compare as bars.`);
      return { ...base, form: 'barlist', barList: { ordered: true, percentOfFirst: false } };
    }
    return { ...base, form: req === 'donut' ? 'donut' : 'pie' };
  }
  // Rules 13-14 — funnel.
  if (req === 'funnel') {
    if (ctx.width >= NARROW) return { ...base, form: 'funnel' };
    return { ...base, form: 'barlist', barList: { ordered: false, percentOfFirst: true } };
  }
  if (req === 'scatter') return { ...base, form: 'scatter' };
  if (req === 'histogram') return { ...base, form: 'histogram' };
  if (req === 'horizontal_bar' && !(timeX && n > MAX_CATEGORIES)) {
    return { ...base, form: 'barlist', barList: { ordered: false, percentOfFirst: false } };
  }
  if (req === 'horizontal_bar') {
    notes.push(`Shown as columns over time: ${n.toLocaleString()} dated rows are too many for a bar list.`);
  }
  if (req === 'combo') return { ...base, form: 'combo' };

  // Rules 6-7 — time.
  if (timeX) {
    if (req === 'line' || req === 'area') return { ...base, form: 'timeLine' };
    const filled = fillTimeRange(model);
    if (filled) {
      const geo = plotGeometry(filled, ctx, 'columns');
      if (columnsFit(axisLabelModel(filled, 'timeColumns'), geo.plotWidth, ctx.measure).fits) {
        return { ...base, model: filled, form: 'timeColumns' };
      }
    }
    return { ...base, form: 'timeBars' };
  }

  // Rules 8-9 — category bars.
  if (req === 'bar') {
    const geo = plotGeometry(model, ctx, 'columns');
    if (columnsFit(model, geo.plotWidth, ctx.measure).fits) return { ...base, form: 'columns' };
    return { ...base, form: 'barlist', barList: { ordered: false, percentOfFirst: false } };
  }
  // Rule 11 — category line / area.
  return { ...base, form: 'catLine' };
}

function repeatedTime(model: ChartModel): boolean {
  const seen = new Set<number>();
  for (const c of model.x.cats) {
    if (c.t === undefined) continue;
    if (seen.has(c.t)) return true;
    seen.add(c.t);
  }
  return false;
}

// ---------------------------------------------------------------------------
// Geometry helpers
// ---------------------------------------------------------------------------

/** The value-axis extent the canvas will draw (stack sums included). */
export function valueExtent(model: ChartModel): { min: number; max: number } {
  let min = 0;
  let max = 0;
  if (model.stacked) {
    const n = model.x.cats.length;
    for (let i = 0; i < n; i++) {
      let pos = 0;
      let neg = 0;
      for (const s of model.series) {
        const v = s.values[i];
        if (v === null || v === undefined) continue;
        if (v >= 0) pos += v;
        else neg += v;
      }
      max = Math.max(max, pos);
      min = Math.min(min, neg);
    }
  } else {
    for (const s of model.series) {
      for (const v of s.values) {
        if (v === null || v === undefined) continue;
        max = Math.max(max, v);
        min = Math.min(min, v);
      }
    }
  }
  return { min, max };
}

/** A round number at or above |v|, like the top tick ECharts will pick. */
function niceCeil(v: number): number {
  const a = Math.abs(v);
  if (a === 0) return 1;
  const p = 10 ** Math.floor(Math.log10(a));
  return Math.ceil(a / p) * p;
}

/** Widest value-axis tick label, measured. */
function valueLabelWidth(model: ChartModel, measure: Measure): number {
  const { min, max } = valueExtent(model);
  const maxAbs = Math.max(Math.abs(min), Math.abs(max));
  const fmt = axisTickFormatter(maxAbs);
  const samples = [fmt(niceCeil(max) || 1)];
  if (min < 0) samples.push(fmt(-niceCeil(min)));
  if (!model.valueFlags.allInteger && maxAbs < 10) samples.push(fmt(maxAbs / 3));
  return widest(samples, measure);
}

interface Geo {
  left: number;
  right: number;
  plotWidth: number;
}

function plotGeometry(model: ChartModel, ctx: LayoutCtx, form: ChartForm): Geo {
  const valueW = valueLabelWidth(model, ctx.measure);
  const left = Math.max(24, Math.ceil(valueW + AXIS_LABEL_MARGIN + 4));
  let right = 16;
  if (form === 'timeLine' || form === 'timeBars') {
    // An edge tick's label is centred on the edge: half of it hangs out.
    const sample = ctx.measure(formatDateByGrain(Date.UTC(2026, 8, 28), model.x.grain ?? 'day'));
    right = Math.max(right, Math.ceil(sample / 2) + 4);
  }
  const plotWidth = Math.max(40, ctx.width - left - right);
  return { left, right, plotWidth };
}

/**
 * Break every category label at `band − 6`. `clean` is false when a word had
 * to be split between letters, which a column axis treats as "does not fit".
 */
function breakLabels(
  model: ChartModel,
  band: number,
  measure: Measure,
): { lines: string[][]; clean: boolean } {
  let clean = true;
  const lines = model.x.cats.map((c) => {
    const r = wrapLinesInfo(c.label, band - LABEL_GUTTER, measure);
    if (r.splitWord) clean = false;
    return r.lines;
  });
  return { lines, clean };
}

/**
 * The same, but stops at the first label that needs a mid-word split or more
 * than `maxLines` lines (review R6: the ladder tries many bands, and one bad
 * label already decides the answer). Null when the band does not fit.
 */
function breakLabelsWithin(
  model: ChartModel,
  band: number,
  measure: Measure,
  maxLines: number,
): string[][] | null {
  const out: string[][] = [];
  for (const c of model.x.cats) {
    const r = wrapLinesInfo(c.label, band - LABEL_GUTTER, measure);
    if (r.splitWord || r.lines.length > maxLines) return null;
    out.push(r.lines);
  }
  return out;
}

/** Design §3.1 — the fit rule for vertical columns. */
export function columnsFit(
  model: ChartModel,
  plotWidth: number,
  measure: Measure,
): { fits: boolean; band: number; lines: string[][] } {
  const n = Math.max(1, model.x.cats.length);
  const band = plotWidth / n;
  const grouped = !model.stacked && model.series.length > 1;
  const barPx = grouped ? (band * 0.8) / model.series.length : band * 0.6;
  const { lines, clean } = breakLabels(model, band, measure);
  const maxLines = Math.max(1, ...lines.map((l) => l.length));
  const fits =
    clean &&
    band >= MIN_BAND &&
    barPx >= MIN_BAR_PX &&
    (maxLines === 1 || (maxLines <= 2 && band >= MIN_TWO_LINE_BAND));
  return { fits, band, lines };
}

/** The label ladder for a category line / area / combo (§4.3 steps 1-5). */
function categoryLadder(
  model: ChartModel,
  ctx: LayoutCtx,
  plotWidth: number,
): {
  lines: string[][];
  rotate: 0 | 45;
  zoomCount: number | null;
  numbered: boolean;
  band: number;
  labelEvery: number;
} {
  const n = model.x.cats.length;
  // A line or area keeps no bar wide, so its floor is the point spacing; a
  // combo's bars (≤ 24 px, one bar series per Mermaid block in practice) stay
  // ≥ 14 px with a ≥ 10 px gap at 24 px (review V12: 8 months at 262 px
  // windowed for a band 0.3 px under the 28 px column floor).
  const lineOnly = model.series.every((s) => s.kind !== 'bar');
  const barSeries = model.series.filter((s) => s.kind === 'bar').length;
  const minBand = lineOnly
    ? LINE_MIN_BAND
    : Math.max(COMBO_MIN_BAND, barSeries > 1 ? (MIN_BAR_PX * barSeries) / 0.8 : 0);
  // A short line shows every point, so before it numbers its ticks it may
  // spend a third label line where the band is wide enough to read one.
  const small = n <= SMALL_LINE;
  const maxLinesAt = (band: number) =>
    small && band >= MIN_THREE_LINE_BAND ? 3 : band >= MIN_TWO_LINE_BAND ? 2 : 1;
  const fitAt = (band: number): string[][] | null =>
    band >= minBand ? breakLabelsWithin(model, band, ctx.measure, maxLinesAt(band)) : null;
  const done = (lines: string[][], band: number, extra: Partial<{ rotate: 0 | 45; zoomCount: number | null; numbered: boolean; labelEvery: number }> = {}) => ({
    lines,
    band,
    rotate: 0 as 0 | 45,
    zoomCount: null as number | null,
    numbered: false,
    labelEvery: 1,
    ...extra,
  });
  // Steps 1-2 — every label at 0°, one or two lines.
  const band = plotWidth / Math.max(1, n);
  const full = fitAt(band);
  if (full) return done(full, band);
  // Step 3 — 45°, desktop only, short single-line labels.
  if (
    ctx.width >= NARROW &&
    band >= MIN_ROTATE_BAND &&
    model.x.cats.every((c) => graphemes(c.label).length <= MAX_ROTATE_CHARS && !/\n/.test(c.label))
  ) {
    return done(
      model.x.cats.map((c) => [c.label]),
      band,
      { rotate: 45 },
    );
  }
  // Step 4 — a window that keeps ≥ 5 categories visible at step 1-2 widths.
  // Not for the PNG (it has no slider) and not for a short line, which
  // shows every point with numbered ticks instead (review V1, V12). The
  // fit is monotone in k (a wider band never needs more lines), so the
  // largest fitting k is found by binary search (review R6).
  if (!ctx.forExport && !small) {
    let lo = 5;
    let hi = Math.min(n - 1, Math.floor(plotWidth / minBand));
    let best: { k: number; lines: string[][] } | null = null;
    while (lo <= hi) {
      const k = (lo + hi) >> 1;
      const lines = fitAt(plotWidth / k);
      if (lines) {
        best = { k, lines };
        lo = k + 1;
      } else {
        hi = k - 1;
      }
    }
    if (best) return done(best.lines, plotWidth / best.k, { zoomCount: best.k });
  }
  // Step 5 — numbered ticks with an HTML key. On screen a window when even
  // the numbers crowd; in the PNG every k-th number instead (uniform, first
  // and last always), since the image cannot scroll.
  const numW = ctx.measure(String(n)) + LABEL_GUTTER + 2;
  const perBand = Math.max(numW, 16);
  const k = Math.floor(plotWidth / perBand);
  const numbers = model.x.cats.map((_, i) => [String(i + 1)]);
  if (k >= n) return done(numbers, band, { numbered: true });
  if (ctx.forExport) {
    const every = Math.max(1, Math.ceil(n / Math.max(2, k)));
    return done(numbers, band, { numbered: true, labelEvery: every });
  }
  const zoomCount = Math.max(2, k);
  return done(numbers, plotWidth / zoomCount, { numbered: true, zoomCount });
}

/**
 * Planner-chosen time-axis labels (review V4): month axes, and day axes of up
 * to 62 days, label every k-th unit from the first, with the last always
 * labelled and never closer than k to the one before. The year shows on the
 * first label and wherever it changes. Null leaves the ticks to ECharts.
 */
const MONTH_STEPS = [1, 2, 3, 4, 6, 12, 24, 36, 60, 120];
const DAY_STEPS = [1, 2, 3, 7, 14, 28];

export function labelledUnits(units: number, k: number): number[] {
  if (units <= 0) return [];
  if (units === 1) return [0];
  const out: number[] = [];
  for (let i = 0; i <= units - 1 - k; i += k) out.push(i);
  if (out.length === 0 || out[out.length - 1] !== units - 1) out.push(units - 1);
  if (out[0] !== 0) out.unshift(0);
  return out;
}

function unitTimes(model: ChartModel): number[] | null {
  const cats = model.x.cats;
  const grain = model.x.grain;
  if (cats.length === 0 || (grain !== 'month' && grain !== 'day')) return null;
  const first = cats[0].t ?? 0;
  const last = cats[cats.length - 1].t ?? 0;
  if (grain === 'month') {
    const u = monthIndex(last) - monthIndex(first) + 1;
    if (u < 1 || u > 1200) return null;
    const d = new Date(first);
    return Array.from({ length: u }, (_, i) => Date.UTC(d.getUTCFullYear(), d.getUTCMonth() + i, 1));
  }
  const u = Math.round((last - first) / 86_400_000) + 1;
  if (u < 1 || u > 62) return null;
  return Array.from({ length: u }, (_, i) => first + i * 86_400_000);
}

function axisDateTexts(times: number[], idx: number[], grain: 'month' | 'day'): string[] {
  let prevYear: number | null = null;
  return idx.map((i) => {
    const y = new Date(times[i]).getUTCFullYear();
    const withYear = prevYear === null || y !== prevYear;
    prevYear = y;
    return formatAxisDate(times[i], grain, withYear);
  });
}

function planTimeTicks(
  model: ChartModel,
  axisMin: number,
  axisMax: number,
  plotWidth: number,
  measure: Measure,
): { ticks: number[]; tickText: Record<string, string> } | null {
  const times = unitTimes(model);
  const grain = model.x.grain;
  if (!times || (grain !== 'month' && grain !== 'day') || axisMax <= axisMin) return null;
  const px = (t: number) => ((t - axisMin) / (axisMax - axisMin)) * plotWidth;
  const steps = grain === 'month' ? MONTH_STEPS : DAY_STEPS;
  const tryStep = (k: number) => {
    const idx = labelledUnits(times.length, k);
    const texts = axisDateTexts(times, idx, grain);
    const widths = texts.map((t) => measure(t));
    for (let j = 1; j < idx.length; j++) {
      const room = px(times[idx[j]]) - px(times[idx[j - 1]]);
      if (room < (widths[j - 1] + widths[j]) / 2 + TIME_LABEL_GAP) return null;
    }
    const tickText: Record<string, string> = {};
    idx.forEach((i, j) => {
      tickText[String(times[i])] = texts[j];
    });
    return { ticks: idx.map((i) => times[i]), tickText };
  };
  for (const k of steps) {
    if (k > 1 && k >= times.length) break;
    const r = tryStep(k);
    if (r) return r;
  }
  // First and last only; then the first alone.
  return tryStep(Math.max(1, times.length - 1)) ?? tryStep(times.length + 1);
}

/** Histogram (§4.3): the smallest uniform k so every k-th label fits at 0°. */
function histogramEvery(model: ChartModel, plotWidth: number, measure: Measure): number {
  const n = model.x.cats.length;
  const band = plotWidth / Math.max(1, n);
  const widestLabel = widest(
    model.x.cats.map((c) => c.label),
    measure,
  );
  for (let k = 1; k <= n; k++) {
    if (k * band - LABEL_GUTTER >= widestLabel) return k;
  }
  return n;
}

/** Which histogram bins carry a label: every k-th, plus the last, never two closer than k. */
export function histogramLabelled(n: number, k: number): boolean[] {
  return Array.from({ length: n }, (_, i) => {
    if (i === n - 1) return true;
    return i % k === 0 && n - 1 - i >= k;
  });
}

function barListCap(ctx: LayoutCtx): string {
  // Review V8: on a phone a long list must not become a scroller that fills
  // the screen (a touch that starts in it would never scroll the page).
  const cap = ctx.context === 'drawer' ? '400px' : ctx.width < NARROW ? '480px' : '520px';
  return ctx.width < NARROW ? `min(${cap}, 60vh)` : cap;
}

/** Short, cheap fingerprint of the planned label text (review R4/V10). */
function textHash(parts: string[]): string {
  let h = 2166136261;
  for (const p of parts) {
    for (let i = 0; i < p.length; i++) {
      h ^= p.charCodeAt(i);
      h = Math.imul(h, 16777619);
    }
    h ^= 0x1f;
    h = Math.imul(h, 16777619);
  }
  return (h >>> 0).toString(36);
}

// ---------------------------------------------------------------------------
// Planner
// ---------------------------------------------------------------------------

export function planLayout(choice: FormChoice, ctx: LayoutCtx): ChartLayout {
  const { form } = choice;
  const model = choice.model;
  const notes = [...choice.notes];
  const n = model.x.cats.length;
  const narrow = ctx.width < NARROW;
  const plotH = narrow ? PLOT_HEIGHT_NARROW : PLOT_HEIGHT;
  const slider = ctx.coarse ? 40 : 28;
  const lineHeight = lineHeightFor(model.x.cats.map((c) => c.label));

  const layout: ChartLayout = {
    renderer: 'echarts',
    form,
    model,
    width: ctx.width,
    height: 0,
    grid: { top: 12, right: 16, bottom: 8, left: 24 },
    plotHeight: plotH,
    catText: {},
    catLines: [],
    lineHeight,
    rotate: 0,
    numbered: false,
    labelEvery: 1,
    zoom: null,
    showSymbol: n <= 60,
    tipLabels: false,
    notes,
    tableOpen: choice.tableOpen,
    emptyMessage: choice.emptyMessage,
    structuralKey: '',
  };

  const finish = (): ChartLayout => {
    // Everything the option bakes in that a width change can alter: a pure
    // resize keeps the old option, so a stale line break or pie radius would
    // otherwise be drawn into the new width (review R4/V10).
    layout.structuralKey = [
      layout.renderer,
      form,
      layout.rotate,
      layout.numbered ? 'num' : '',
      layout.labelEvery,
      layout.zoom ? `zoom${layout.zoom.maxValueSpan ?? ''}` : '',
      layout.pie ? `${layout.pie.legend}${layout.pie.diameter}${layout.pie.centreTotal ? 'c' : ''}` : '',
      layout.catLines.join(','),
      textHash(Object.values(layout.catText)),
      layout.timeAxis?.ticks ? textHash(Object.values(layout.timeAxis.tickText ?? {})) : '',
      layout.funnel ? textHash(Object.values(layout.funnel.labelText)) : '',
      layout.lineHeight,
      layout.tipLabels ? 'tips' : '',
      narrow ? 'narrow' : 'wide',
      ctx.coarse ? 'coarse' : 'fine',
    ].join('|');
    return layout;
  };

  if (form === 'table' || form === 'empty' || form === 'stat') {
    layout.renderer = form;
    return finish();
  }
  if (form === 'barlist') {
    layout.renderer = 'barlist';
    layout.barList = { ...(choice.barList ?? { ordered: false, percentOfFirst: false }), cap: barListCap(ctx) };
    return finish();
  }

  if (form === 'pie' || form === 'donut') {
    const legendRight = ctx.width >= 560;
    // In 16 px steps, so a drag-resize rebuilds the pie a few times, not per frame.
    const diameter = Math.max(120, Math.min(260, Math.floor((ctx.width - 32) / 16) * 16));
    layout.pie = { diameter, legend: legendRight ? 'right' : 'below', centreTotal: form === 'donut' && diameter >= 160 };
    layout.height = diameter + 16;
    layout.width = legendRight ? diameter + 16 : ctx.width;
    return finish();
  }

  if (form === 'funnel') {
    const maxW = Math.floor(ctx.width * 0.3);
    const first = model.series[0]?.values[0] ?? 0;
    const labelText: Record<string, string> = {};
    let total = 0;
    let labelWidth = 0;
    model.x.cats.forEach((c, i) => {
      const v = model.series[0]?.values[i] ?? null;
      const valueLine = `${formatValue(v)}${first && v !== null ? ` (${formatShare(v, first)})` : ''}`;
      const lines = [...wrapLines(c.label, maxW, ctx.measure), valueLine];
      labelText[c.id] = lines.join('\n');
      labelWidth = Math.max(labelWidth, widest(lines, ctx.measure));
      total += Math.max(44, lines.length * lineHeight + 8);
    });
    layout.funnel = { labelText, labelWidth: Math.ceil(labelWidth) };
    layout.height = Math.max(240, total + 16);
    return finish();
  }

  // ---- cartesian forms
  const geo = plotGeometry(model, ctx, form);
  layout.grid.left = geo.left;
  layout.grid.right = geo.right;
  let xBand = lineHeight + 12;

  if (form === 'columns' || form === 'timeColumns' || form === 'catLine' || form === 'combo') {
    let lines: string[][];
    if (form === 'columns' || form === 'timeColumns') {
      lines = columnsFit(axisLabelModel(model, form), geo.plotWidth, ctx.measure).lines;
    } else {
      const ladder = categoryLadder(model, ctx, geo.plotWidth);
      lines = ladder.lines;
      layout.rotate = ladder.rotate;
      layout.numbered = ladder.numbered;
      layout.labelEvery = ladder.labelEvery;
      if (ladder.zoomCount !== null && ladder.zoomCount < n) {
        const span = ladder.zoomCount - 1;
        layout.zoom = {
          startValue: 0,
          endValue: span,
          slider,
          maxValueSpan: span,
          maxSpanPct: n > 1 ? (span / (n - 1)) * 100 : 100,
        };
      }
      if (ladder.numbered) notes.push('Labels are numbered; the key is below.');
    }
    model.x.cats.forEach((c, i) => {
      layout.catText[c.id] = lines[i].join('\n');
    });
    layout.catLines = lines.map((l) => l.length);
    const maxLines = Math.max(1, ...layout.catLines);
    if (layout.rotate === 45) {
      const w = widest(
        model.x.cats.map((c) => c.label),
        ctx.measure,
      );
      xBand = Math.ceil(SIN45 * w + SIN45 * lineHeight + 8);
      const firstW = ctx.measure(model.x.cats[0]?.label ?? '');
      layout.grid.left = Math.max(layout.grid.left, Math.ceil(SIN45 * firstW + 4));
    } else {
      xBand = maxLines * lineHeight + 12;
    }
  } else if (form === 'histogram') {
    layout.labelEvery = histogramEvery(model, geo.plotWidth, ctx.measure);
    model.x.cats.forEach((c) => {
      layout.catText[c.id] = c.label;
    });
    layout.catLines = model.x.cats.map(() => 1);
    xBand = lineHeight + 12;
  } else if (form === 'timeLine' || form === 'timeBars') {
    const cats = model.x.cats;
    const first = cats[0]?.t ?? 0;
    const last = cats[cats.length - 1]?.t ?? 0;
    const unit = grainMs(model.x.grain);
    const pad = form === 'timeBars' ? (unit || 0) / 2 : 0;
    const sample = ctx.measure(formatDateByGrain(Date.UTC(2026, 8, 28), model.x.grain ?? 'day'));
    const splitNumber = Math.max(2, Math.floor(geo.plotWidth / (sample + 24)));
    layout.timeAxis = { min: first - pad, max: last + pad, splitNumber };
    if (ctx.forExport) {
      // The PNG is the full range: no window.
    } else if (form === 'timeLine') {
      if (geo.plotWidth / Math.max(1, n) < 2) {
        const k = Math.max(2, Math.floor(geo.plotWidth / 6));
        layout.zoom = { startValue: cats[Math.max(0, n - k)].t ?? first, endValue: last, slider };
      }
    } else {
      const units = model.x.grain === 'datetime' ? n : unitsInSpan(model);
      if (geo.plotWidth / Math.max(1, units) < 4) {
        const k = Math.max(2, Math.floor(geo.plotWidth / 4));
        let start: number;
        if (model.x.grain === 'month') {
          const d = new Date(last);
          start = Date.UTC(d.getUTCFullYear(), d.getUTCMonth() - (k - 1), 1);
        } else if (model.x.grain === 'day') {
          start = last - (k - 1) * 86_400_000;
        } else {
          start = cats[Math.max(0, n - k)].t ?? first;
        }
        layout.zoom = { startValue: Math.max(first, start) - pad, endValue: last + pad, slider };
      }
    }
    layout.showSymbol = n <= 60;
    xBand = lineHeight + 12;
  } else if (form === 'scatter') {
    xBand = lineHeight + 12;
  }

  // Tip labels: only where each bar is wider than the value text, and never on
  // stacked segments (no text inside or across coloured fills).
  if (model.showValues) {
    if (form === 'columns' || form === 'timeColumns') {
      const band = geo.plotWidth / Math.max(1, n);
      const grouped = !model.stacked && model.series.length > 1;
      const barPx = grouped ? (band * 0.8) / model.series.length : Math.min(24, band * 0.6);
      const valueW = widest(
        model.series.flatMap((s) => s.values.map((v) => formatValue(v))),
        ctx.measure,
      );
      // A single bar's label is centred in its band; grouped labels share it.
      const room = grouped ? barPx : band - LABEL_GUTTER;
      layout.tipLabels = !model.stacked && valueW + 2 <= room;
    } else if (form === 'catLine' || form === 'timeLine') {
      layout.tipLabels = model.series.length <= 4;
      if (layout.tipLabels) {
        const valueW = widest(
          model.series.map((s) => formatValue(lastValue(s.values))),
          ctx.measure,
        );
        layout.grid.right = Math.max(layout.grid.right, Math.ceil(valueW + 12));
      }
    }
  }

  // Review V4: on a time axis the reader sees whole (no window), the planner
  // picks the labelled months/days so the first and last always show. Planned
  // last, once every margin (end labels included) is known.
  if ((form === 'timeLine' || form === 'timeBars') && !layout.zoom && layout.timeAxis) {
    const g = model.x.grain;
    const times = g === 'month' || g === 'day' ? unitTimes(model) : null;
    if (times && (g === 'month' || g === 'day')) {
      const firstW = ctx.measure(formatAxisDate(times[0], g, true));
      const lastW = ctx.measure(formatAxisDate(times[times.length - 1], g, true));
      // An edge label is centred on its tick; up to half of it hangs past
      // the plot (all of the half for a line, less for padded bars).
      layout.grid.left = Math.max(layout.grid.left, Math.ceil(firstW / 2) + 2);
      layout.grid.right = Math.max(layout.grid.right, Math.ceil(lastW / 2) + 2);
      const plotWidth = Math.max(40, ctx.width - layout.grid.left - layout.grid.right);
      const planned = planTimeTicks(model, layout.timeAxis.min, layout.timeAxis.max, plotWidth, ctx.measure);
      if (planned) layout.timeAxis = { ...layout.timeAxis, ...planned };
    }
  }

  layout.grid.top = layout.tipLabels && form !== 'catLine' && form !== 'timeLine' ? 24 : 12;
  const zoomBand = layout.zoom ? layout.zoom.slider + 12 : 0;
  layout.grid.bottom = xBand + zoomBand;
  layout.height = layout.grid.top + plotH + layout.grid.bottom;
  return finish();
}

/**
 * The labels a category axis prints (review V16): a monthly column axis says
 * "Jan 2025, Feb, Mar … Jan 2026" — the year on the first column and each
 * January — instead of repeating the year under every column. Tooltips, the
 * key, the summary and the table keep the full "Feb 2025".
 */
export function axisLabelModel(model: ChartModel, form: ChartForm): ChartModel {
  if (form !== 'timeColumns' || model.x.grain !== 'month') return model;
  let prevYear: number | null = null;
  const cats = model.x.cats.map((c) => {
    const t = c.t ?? 0;
    const y = new Date(t).getUTCFullYear();
    const withYear = prevYear === null || y !== prevYear;
    prevYear = y;
    return { ...c, label: formatAxisDate(t, 'month', withYear) };
  });
  return { ...model, x: { ...model.x, cats } };
}

function lastValue(values: Array<number | null>): number | null {
  for (let i = values.length - 1; i >= 0; i--) if (values[i] !== null) return values[i];
  return null;
}

/** Convenience: choose + plan in one call. */
export function layoutChart(model: ChartModel, ctx: LayoutCtx): ChartLayout {
  return planLayout(chooseForm(model, ctx), ctx);
}

// ---------------------------------------------------------------------------
// Screen-reader summary (design v2 §9)
// ---------------------------------------------------------------------------

/** Text with names marked, so the component can wrap them in <bdi>. */
export type SummaryPart = string | { name: string };

const FORM_NOUN: Record<ChartForm, string> = {
  table: 'Table',
  empty: 'Chart',
  stat: 'Figure',
  pie: 'Pie chart',
  donut: 'Donut chart',
  funnel: 'Funnel chart',
  barlist: 'Bar chart',
  columns: 'Bar chart',
  catLine: 'Line chart',
  timeLine: 'Line chart',
  timeColumns: 'Bar chart',
  timeBars: 'Bar chart',
  histogram: 'Histogram',
  scatter: 'Scatter chart',
  combo: 'Bar and line chart',
};

export function describeChart(layout: ChartLayout): SummaryPart[] {
  const { model, form } = layout;
  const cats = model.x.cats;
  const n = cats.length;
  let noun = FORM_NOUN[form];
  if ((form === 'catLine' || form === 'timeLine') && model.requested === 'area') noun = 'Area chart';
  const measures = model.series.map((s) => s.label);
  const parts: SummaryPart[] = [];
  const of =
    measures.length === 1
      ? [`${noun} of `, { name: measures[0] }, ' by ', { name: model.x.label }]
      : [`${noun} of ${measures.length} series by `, { name: model.x.label }];
  parts.push(...of);

  if (form === 'scatter') {
    parts.push(`, ${n.toLocaleString()} points.`);
    return parts;
  }
  if (model.x.kind === 'time' && n > 0) {
    const grain = model.x.grain ?? 'day';
    parts.push(
      `, ${n.toLocaleString()} ${n === 1 ? 'point' : 'points'} from ${formatDateByGrain(cats[0].t ?? 0, grain)} to ${formatDateByGrain(
        cats[n - 1].t ?? 0,
        grain,
      )}`,
    );
    if (model.x.breaks.length) {
      parts.push(`, with ${model.x.breaks.length} ${model.x.breaks.length === 1 ? 'gap' : 'gaps'} without rows`);
    }
  } else {
    parts.push(`, ${n.toLocaleString()} ${n === 1 ? 'category' : 'categories'}`);
  }
  if (PART_TO_WHOLE.has(model.requested) && model.series[0]) {
    parts.push(`, total ${formatValue(model.valueFlags.total)}`);
  }
  parts.push('.');

  if (model.series.length === 1 && n > 1) {
    const s = model.series[0];
    let hi = -1;
    let lo = -1;
    s.values.forEach((v, i) => {
      if (v === null) return;
      if (hi < 0 || v > (s.values[hi] ?? -Infinity)) hi = i;
      if (lo < 0 || v < (s.values[lo] ?? Infinity)) lo = i;
    });
    if (hi >= 0 && lo >= 0) {
      parts.push(' Highest ', { name: cats[hi].label }, ` at ${formatValue(s.values[hi])}; lowest `, {
        name: cats[lo].label,
      }, ` at ${formatValue(s.values[lo])}.`);
    }
  } else if (model.series.length > 1) {
    parts.push(' Series: ');
    model.series.forEach((s, i) => {
      if (i > 0) parts.push(', ');
      parts.push({ name: s.label });
    });
    parts.push('.');
  }
  return parts;
}

/** Plain-text form of the summary (tests, PNG alt text). */
export function summaryText(parts: SummaryPart[]): string {
  return parts.map((p) => (typeof p === 'string' ? p : p.name)).join('');
}
