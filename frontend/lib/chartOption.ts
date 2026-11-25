/**
 * The trusted ChartModel → ECharts option adapter.
 *
 * ============================ SECURITY BOUNDARY ============================
 * Everything ECharts receives is constructed HERE, by this file, from a
 * `ChartModel` (lib/chartModel.ts) and a `ChartLayout` (lib/chartLayout.ts).
 * Nothing from the backend or the model is ever spread into an option:
 *
 *   - the only spec fields read (by chartModel) are type / x_key / y_keys /
 *     title / stacked / bins / show_legend / show_values, and each is a
 *     closed enum, a bool, a bounded integer or a column NAME; a Mermaid
 *     block contributes only strings and numbers (lib/mermaidChart.ts);
 *   - `type` is checked against a whitelist before dispatch, so a value
 *     from a newer backend renders the fallback instead of reaching
 *     ECharts;
 *   - ECharts only ever sees IDS (`c0…`, `s0…`) as category values, series
 *     names and pie item names (design v2 §2, C12). Display text reaches the
 *     canvas through the formatters below, which look the id up;
 *   - every `formatter` is a function defined in this file. ECharts
 *     accepts functions in options, which is exactly why one must never
 *     be able to arrive over the wire;
 *   - category axes never get `rich`, `width` or `overflow` (C1, C30): a
 *     label like `{a|x}` is never parsed as rich text, and the planner, not
 *     zrender, breaks lines.
 *
 * Tooltip content is HTML, and category labels are Salesforce data (an
 * Account Name is user-controlled text) or model-written Mermaid text. Every
 * interpolated value goes through `escapeHtml` and sits in a `dir="auto"`
 * span.
 * ==========================================================================
 *
 * Pure module: no React, no DOM, no `echarts` import.
 */

import type { ChartSpec, DataRow } from './types';
import { type ChartPalette, seriesColor } from './chartTheme';
import {
  type Cell,
  axisTickFormatter,
  formatCategory,
  formatDateByGrain,
  formatShare,
  formatTimeTick,
  formatValue,
  toNumber,
} from './chartFormat';
import {
  CHART_TYPES,
  type ChartModel,
  type ChartProblem,
  isChartType,
  modelFromSpec,
  validateChart,
} from './chartModel';
import {
  type ChartLayout,
  histogramLabelled,
  layoutChart,
  valueExtent,
} from './chartLayout';
import { CHART_FONT_FAMILY, estimateWidth, type Measure, wrapLines, widest } from './chartText';

export { CHART_TYPES, isChartType, validateChart };
export type { ChartProblem };

/** Structural stand-in for echarts' option type — keeps this module runtime-free. */
export type EChartsOption = Record<string, unknown>;

const MAX_SLICES = 8;
const MAX_CATEGORY_TICKS = 40;

// ---------------------------------------------------------------------------
// Part-to-whole helper (kept for callers and tests of the old API)
// ---------------------------------------------------------------------------

/** Ordered (label, value) pairs for a part-to-whole chart, tail folded into "Other". */
export function partToWholeData(
  spec: ChartSpec,
  rows: DataRow[],
  key: string,
): Array<{ name: string; value: number }> {
  const pairs = rows
    .map((r) => ({
      name: formatCategory(r[spec.x_key] as Cell),
      value: Math.max(toNumber(r[key] as Cell) ?? 0, 0),
    }))
    .sort((a, b) => b.value - a.value);
  if (pairs.length <= MAX_SLICES) return pairs;
  const head = pairs.slice(0, MAX_SLICES - 1);
  const other = pairs.slice(MAX_SLICES - 1).reduce((sum, p) => sum + p.value, 0);
  return [...head, { name: 'Other', value: other }];
}

// ---------------------------------------------------------------------------
// Trusted formatters (functions defined here, never received)
// ---------------------------------------------------------------------------

const ESCAPES: Record<string, string> = {
  '&': '&amp;',
  '<': '&lt;',
  '>': '&gt;',
  '"': '&quot;',
  "'": '&#39;',
};

/**
 * Escape text bound for the tooltip.
 *
 * ECharts renders a string returned from a tooltip formatter as HTML.
 * Category labels are Salesforce values — an Account named
 * `<img src=x onerror=…>` is a perfectly legal Salesforce record — so
 * every interpolated value is escaped. Series names come from column
 * names or Mermaid text, which are equally untrusted.
 */
export function escapeHtml(text: string): string {
  return text.replace(/[&<>"']/g, (c) => ESCAPES[c]);
}

/** Only colours this module chose ever reach a style attribute; still escaped. */
function swatch(color: string, ring: string): string {
  return (
    `<span style="display:inline-block;flex:none;width:10px;height:10px;border-radius:50%;` +
    `margin-right:6px;background:${escapeHtml(color)};box-shadow:0 0 0 1px ${escapeHtml(ring)}"></span>`
  );
}

function name(text: string): string {
  return `<span dir="auto">${escapeHtml(text)}</span>`;
}

/** One tooltip line: the key, the value first (bold), then the name. */
function tooltipRow(color: string, ring: string, label: string, value: string): string {
  return (
    `<div style="display:flex;align-items:baseline;gap:2px;margin-top:2px">${swatch(color, ring)}` +
    `<span><b style="font-variant-numeric:tabular-nums">${escapeHtml(value)}</b>&nbsp;${name(label)}</span></div>`
  );
}

function header(text: string): string {
  return `<div style="font-weight:600;margin-bottom:4px">${name(text)}</div>`;
}

interface TooltipParam {
  seriesName?: string;
  seriesId?: string;
  seriesIndex?: number;
  name?: string;
  color?: string;
  value?: unknown;
  data?: unknown;
  dataIndex?: number;
  percent?: number;
  axisValue?: string | number;
  axisValueLabel?: string;
}

function numberFrom(value: unknown, axis: 'x' | 'y' = 'y'): number | null {
  if (Array.isArray(value)) return toNumber(value[axis === 'y' ? 1 : 0] as Cell);
  if (value && typeof value === 'object' && 'value' in (value as object)) {
    return numberFrom((value as { value: unknown }).value, axis);
  }
  return toNumber(value as Cell);
}

/** Half-opacity ring in the tooltip ink, so a low-contrast swatch still reads (C19). */
function ringOf(palette: ChartPalette): string {
  const ink = palette.tooltipText;
  return /^#[0-9a-f]{6}$/i.test(ink) ? `${ink}80` : ink;
}

// ---------------------------------------------------------------------------
// Option construction
// ---------------------------------------------------------------------------

export interface BuildOpts {
  reducedMotion?: boolean;
  /** Legend selection by series / item id (carried across rebuilds, C9). */
  legendSelected?: Record<string, boolean>;
  /** Current zoom window (from the datazoom event), overriding the layout's initial one. */
  zoom?: { start?: number; end?: number; startValue?: number; endValue?: number } | null;
  /** Offscreen PNG export: title, legend, full range, no animation. */
  forExport?: boolean;
  /** The background the chart sits on (gaps, rings, export fill). */
  surface?: string;
  /** Coarse pointer: no drag-pan inside zoom. */
  coarse?: boolean;
  /** Measurer for export text (defaults to the estimate). */
  measure?: Measure;
  /** Export only: where the canvas legend starts (below the title). */
  exportLegendTop?: number;
  /**
   * False for a rebuild of a chart already on screen (theme, layout step):
   * no second enter animation (design §8.7); updates still take 200 ms.
   */
  firstDraw?: boolean;
}

interface Ctx {
  model: ChartModel;
  layout: ChartLayout;
  palette: ChartPalette;
  opts: BuildOpts;
  surface: string;
  catLabel: (id: unknown) => string;
  seriesLabel: (id: unknown) => string;
  colorOf: (seriesIndex: number) => string;
}

function makeCtx(model: ChartModel, layout: ChartLayout, palette: ChartPalette, opts: BuildOpts): Ctx {
  const cats = new Map(model.x.cats.map((c) => [c.id, c.label]));
  const series = new Map(model.series.map((s) => [s.id, s.label]));
  return {
    model,
    layout,
    palette,
    opts,
    surface: opts.surface ?? palette.surface,
    catLabel: (id) => cats.get(String(id)) ?? '',
    seriesLabel: (id) => series.get(String(id)) ?? cats.get(String(id)) ?? '',
    colorOf: (i) => seriesColor(palette, model.series[i]?.slot ?? i),
  };
}

function baseOption(c: Ctx): EChartsOption {
  const { palette, opts } = c;
  const animate = !opts.reducedMotion && !opts.forExport;
  const big = c.model.x.cats.length > 2000;
  return {
    animation: animate && !big,
    animationDuration: opts.firstDraw === false ? 0 : 300,
    animationDurationUpdate: 200,
    animationEasing: 'cubicOut',
    // Dates are positioned from their calendar parts (Date.UTC), so the axis
    // must read them in UTC too or a monthly bucket slides a day west of UTC.
    useUTC: true,
    color: palette.series,
    backgroundColor: opts.forExport ? c.surface : 'transparent',
    textStyle: { color: palette.text, fontSize: 12, fontFamily: CHART_FONT_FAMILY },
    tooltip: {
      confine: true,
      triggerOn: 'mousemove|click',
      transitionDuration: opts.reducedMotion ? 0 : 0.2,
      backgroundColor: palette.tooltipBg,
      borderColor: palette.grid,
      borderWidth: 1,
      textStyle: { color: palette.tooltipText, fontSize: 13, fontFamily: CHART_FONT_FAMILY },
      extraCssText:
        'border-radius:10px;white-space:normal;max-width:min(320px,80vw);overflow-wrap:anywhere;',
    },
  };
}

/** The canvas legend: hidden on screen (the HTML legend drives it), drawn for export. */
function legendOf(c: Ctx, ids: string[]): EChartsOption {
  const selected: Record<string, boolean> = {};
  for (const id of ids) selected[id] = c.opts.legendSelected?.[id] !== false;
  if (!c.opts.forExport || ids.length < 2) {
    return { show: false, data: ids, selected };
  }
  const measure = c.opts.measure ?? ((t: string) => estimateWidth(t));
  return {
    show: true,
    type: 'plain',
    data: ids,
    selected,
    top: c.opts.exportLegendTop ?? 16,
    left: 24,
    right: 24,
    icon: 'roundRect',
    itemWidth: 10,
    itemHeight: 10,
    itemGap: 16,
    textStyle: { color: c.palette.text, fontSize: 12, lineHeight: 16 },
    // A canvas legend item never wraps by itself; a long name is pre-broken.
    formatter: (id: string) => wrapLines(c.seriesLabel(id), 320, measure).join('\n'),
  };
}

function categoryAxis(c: Ctx, ids: string[]): EChartsOption {
  const { layout, palette } = c;
  // A histogram's continuous bins, and the PNG's numbered ticks when even
  // the numbers crowd (review V1), label a uniform subset; nothing else does.
  const labelled =
    layout.form === 'histogram' || layout.labelEvery > 1
      ? histogramLabelled(ids.length, layout.labelEvery)
      : null;
  return {
    type: 'category',
    data: ids,
    boundaryGap: true,
    axisLine: { onZero: false, lineStyle: { color: palette.baseline } },
    axisTick: { show: false },
    axisLabel: {
      color: palette.axisText,
      fontSize: 12,
      fontFamily: CHART_FONT_FAMILY,
      lineHeight: layout.lineHeight,
      margin: 8,
      rotate: layout.rotate,
      // Every label is shown (or, for a histogram's continuous bins, a
      // uniform subset). Never `hideOverlap`, never `width`/`overflow`/`rich`.
      interval: labelled ? (index: number) => labelled[index] === true : 0,
      formatter: (id: string) => layout.catText[id] ?? c.catLabel(id),
    },
  };
}

function valueAxis(c: Ctx): EChartsOption {
  const { model, palette, layout } = c;
  const { min, max } = valueExtent(model);
  const allZero = model.valueFlags.allZero;
  // Review V5: a negative column's value label sits below its bar's end, so
  // the axis keeps a label's height of room under the lowest bar (nice
  // rounding then only adds to it) and the label never reaches the
  // category labels.
  const roomBelow = layout.tipLabels && min < 0 && !allZero;
  return {
    type: 'value',
    boundaryGap: roomBelow ? ['10%', 0] : undefined,
    axisLine: { show: false },
    axisTick: { show: false },
    minInterval: model.valueFlags.allInteger ? 1 : undefined,
    min: allZero ? 0 : undefined,
    max: allZero ? 1 : undefined,
    axisLabel: {
      color: palette.axisText,
      fontSize: 12,
      formatter: axisTickFormatter(Math.max(Math.abs(min), Math.abs(max))),
    },
    splitLine: { show: true, lineStyle: { color: palette.gridline, width: 1, type: 'solid' } },
  };
}

function timeAxis(c: Ctx): EChartsOption {
  const { layout, model, palette } = c;
  const grain = model.x.grain ?? 'day';
  const span = (layout.timeAxis?.max ?? 0) - (layout.timeAxis?.min ?? 0);
  const longSpan = span > 330 * 86_400_000;
  const planned = layout.timeAxis?.ticks;
  const plannedText = layout.timeAxis?.tickText ?? {};
  return {
    type: 'time',
    min: layout.timeAxis?.min,
    max: layout.timeAxis?.max,
    splitNumber: layout.timeAxis?.splitNumber,
    axisLine: { onZero: false, lineStyle: { color: palette.baseline } },
    axisTick: { show: false },
    splitLine: { show: false },
    axisLabel: {
      color: palette.axisText,
      fontSize: 12,
      fontFamily: CHART_FONT_FAMILY,
      // Planned labels (review V4): every k-th month or day, first and last
      // always, measured so none collides. Otherwise (a window, a long axis)
      // ECharts picks the ticks of the continuous scale and a crowded one
      // may be skipped: no point is lost (tooltip, keys and table have all).
      customValues: planned,
      hideOverlap: true,
      formatter: (value: number, index: number) =>
        plannedText[String(value)] ?? formatTimeTick(value, grain, index === 0, longSpan),
    },
  };
}

function gridOf(c: Ctx, top?: number): EChartsOption {
  const g = c.layout.grid;
  return {
    top: top ?? g.top,
    right: g.right,
    bottom: c.opts.forExport ? g.bottom - (c.layout.zoom ? c.layout.zoom.slider + 12 : 0) : g.bottom,
    left: g.left,
  };
}

function dataZoomOf(c: Ctx): EChartsOption[] | undefined {
  const { layout, palette, opts } = c;
  if (!layout.zoom || opts.forExport) return undefined;
  const win: Record<string, number | undefined> = {};
  if (opts.zoom && (opts.zoom.start !== undefined || opts.zoom.startValue !== undefined)) {
    if (opts.zoom.startValue !== undefined) {
      win.startValue = opts.zoom.startValue;
      win.endValue = opts.zoom.endValue;
    } else {
      win.start = opts.zoom.start;
      win.end = opts.zoom.end;
    }
  } else {
    win.startValue = layout.zoom.startValue;
    win.endValue = layout.zoom.endValue;
  }
  const isTime = layout.form === 'timeLine' || layout.form === 'timeBars';
  const grain = c.model.x.grain ?? 'day';
  const labelFormatter = isTime
    ? (value: number) => formatDateByGrain(value, grain)
    : (value: number) => c.model.x.cats[Math.round(value)]?.label ?? '';
  // Review R2: a category window may not widen past the span its label
  // breaks were planned for — beyond it every label would overlap.
  const span = layout.zoom.maxValueSpan !== undefined ? { maxValueSpan: layout.zoom.maxValueSpan } : {};
  return [
    {
      type: 'slider',
      xAxisIndex: 0,
      ...win,
      ...span,
      height: layout.zoom.slider,
      bottom: 4,
      handleSize: '100%',
      brushSelect: false,
      showDetail: false,
      labelFormatter,
      borderColor: palette.zoomHandle,
      fillerColor: palette.zoomFill,
      backgroundColor: 'transparent',
      handleStyle: { color: palette.zoomHandle, borderColor: palette.zoomHandle },
      moveHandleStyle: { color: palette.zoomHandle },
      dataBackground: {
        lineStyle: { color: palette.baseline, width: 1 },
        areaStyle: { color: palette.gridline },
      },
      selectedDataBackground: {
        lineStyle: { color: palette.axisText, width: 1 },
        areaStyle: { color: palette.baseline },
      },
      textStyle: { color: palette.axisText },
    },
    {
      type: 'inside',
      xAxisIndex: 0,
      ...win,
      ...span,
      // No wheel zoom (C14): ctrl+wheel is browser zoom and trackpad pinch
      // arrives as ctrl+wheel; capturing either over the chart blocks page
      // zoom for low-vision readers. Drag-pan only, fine pointers only.
      zoomOnMouseWheel: false,
      moveOnMouseWheel: false,
      moveOnMouseMove: true,
      disabled: Boolean(opts.coarse),
    },
  ];
}

// ---- tooltips --------------------------------------------------------------

function axisTooltip(c: Ctx) {
  const { model, palette } = c;
  const ring = ringOf(palette);
  const isTime = c.layout.form === 'timeLine' || c.layout.form === 'timeBars';
  const grain = model.x.grain ?? 'day';
  const breakAt = new Map<number, [number, number]>();
  if (isTime) {
    for (const i of model.x.breaks) {
      const a = model.x.cats[i]?.t;
      const b = model.x.cats[i + 1]?.t;
      if (a !== undefined && b !== undefined) breakAt.set((a + b) / 2, [a, b]);
    }
  }
  return (params: TooltipParam | TooltipParam[]): string => {
    const list = Array.isArray(params) ? params : [params];
    if (list.length === 0) return '';
    const first = list[0];
    if (isTime) {
      const t = Number(first.axisValue);
      const gap = breakAt.get(t);
      if (gap) {
        return header(
          `No rows from ${formatDateByGrain(gap[0], grain)} to ${formatDateByGrain(gap[1], grain)}`,
        );
      }
      const body = list
        .map((p) => {
          const n = numberFrom(p.value);
          return tooltipRow(p.color ?? '', ring, c.seriesLabel(p.seriesId ?? p.seriesName), formatValue(n));
        })
        .join('');
      return header(formatDateByGrain(t, grain)) + body;
    }
    const id = String(first.axisValue ?? first.name ?? '');
    const cat = model.x.cats.find((k) => k.id === id);
    if (cat && cat.present === false) {
      return header(cat.label) + `<div>${name(`No row for ${cat.label}`)}</div>`;
    }
    const body = list
      .map((p) =>
        tooltipRow(
          p.color ?? '',
          ring,
          c.seriesLabel(p.seriesId ?? p.seriesName),
          formatValue(numberFrom(p.value)),
        ),
      )
      .join('');
    return header(c.catLabel(id)) + body;
  };
}

function itemTooltip(c: Ctx) {
  const ring = ringOf(c.palette);
  const total = c.model.valueFlags.total;
  const percentWhole = c.model.valueFlags.percentWhole;
  const firstStage = c.model.series[0]?.values[0] ?? null;
  return (params: TooltipParam | TooltipParam[]): string => {
    const p = Array.isArray(params) ? params[0] : params;
    if (!p) return '';
    const n = numberFrom(p.value);
    let value = percentWhole && n !== null ? `${formatValue(n)}%` : formatValue(n);
    if (c.layout.form === 'funnel') {
      if (firstStage && n !== null) value += ` (${formatShare(n, firstStage)} of the first stage)`;
    } else if (!percentWhole && n !== null) {
      value += ` (${formatShare(n, total)})`;
    }
    return tooltipRow(p.color ?? '', ring, c.catLabel(p.name), value);
  };
}

function scatterTooltip(c: Ctx) {
  const ring = ringOf(c.palette);
  const xLabel = c.model.x.label;
  return (params: TooltipParam | TooltipParam[]): string => {
    const p = Array.isArray(params) ? params[0] : params;
    if (!p) return '';
    const x = numberFrom(p.value, 'x');
    const y = numberFrom(p.value, 'y');
    return (
      tooltipRow(p.color ?? '', ring, xLabel, formatValue(x)) +
      tooltipRow(p.color ?? '', ring, c.seriesLabel(p.seriesId ?? p.seriesName), formatValue(y))
    );
  };
}

function tipLabel(c: Ctx, position: 'top' | 'bottom' = 'top') {
  return {
    show: true,
    position,
    distance: 4,
    color: c.palette.text,
    fontSize: 12,
    formatter: (p: { value?: unknown }) => {
      const n = numberFrom(p.value);
      return n === null ? '' : formatValue(n);
    },
  };
}

// ---- series ------------------------------------------------------------------

const RADIUS_UP = [4, 4, 0, 0];
const RADIUS_DOWN = [0, 0, 4, 4];

function barSeries(c: Ctx, i: number, data: unknown[], opts: { histogram?: boolean } = {}) {
  const { model, layout } = c;
  const s = model.series[i];
  const stacked = model.stacked && model.series.length > 1;
  const negatives = model.valueFlags.anyNegative && !stacked && !opts.histogram;
  // Review V5: a negative bar's value label goes to its outer (lower) end.
  const negLabel = layout.tipLabels ? { label: tipLabel(c, 'bottom') } : {};
  return {
    id: s.id,
    name: s.id,
    type: 'bar',
    data: negatives
      ? data.map((v) => {
          const n = numberFrom(v);
          if (n === null) return v;
          return n < 0
            ? { value: v, itemStyle: { borderRadius: RADIUS_DOWN }, ...negLabel }
            : { value: v, itemStyle: { borderRadius: RADIUS_UP } };
        })
      : data,
    stack: stacked ? 'total' : undefined,
    barMaxWidth: 24,
    barGap: '25%',
    barCategoryGap: opts.histogram ? '0%' : undefined,
    large: data.length > 1000,
    // Review A15: the hovered or keyboard-current bar gets a 2 px ink ring,
    // so the position is visible beyond the tooltip.
    emphasis: {
      focus: model.series.length > 1 ? 'series' : 'none',
      itemStyle: { borderColor: c.palette.text, borderWidth: 2 },
    },
    itemStyle: {
      color: c.colorOf(i),
      borderRadius: stacked || opts.histogram ? 0 : RADIUS_UP,
      // 2 px surface gap between touching fills (stack segments, bins).
      borderColor: stacked || opts.histogram ? c.surface : undefined,
      borderWidth: stacked || opts.histogram ? 1 : 0,
    },
    label: layout.tipLabels ? tipLabel(c) : { show: false },
  };
}

function isolatedPoints(data: Array<number | null>): boolean[] {
  const measured = (i: number) => i >= 0 && i < data.length && data[i] !== null;
  return data.map((_, i) => measured(i) && !measured(i - 1) && !measured(i + 1));
}

function lineSeries(c: Ctx, i: number, data: Array<number | null | [number, number | null]>) {
  const { model, layout } = c;
  const s = model.series[i];
  const area = s.kind === 'area';
  const values = data.map((d) => (Array.isArray(d) ? d[1] : d));
  const isolated = isolatedPoints(values);
  const hasIsolated = isolated.some(Boolean);
  const big = data.length > 2000;
  const showAll = layout.showSymbol && !big;
  const color = c.colorOf(i);
  return {
    id: s.id,
    name: s.id,
    type: 'line',
    data:
      !showAll && hasIsolated
        ? data.map((d, idx) => (isolated[idx] || values[idx] === null ? d : { value: d, symbol: 'none' }))
        : data,
    smooth: false,
    showSymbol: showAll || hasIsolated,
    showAllSymbol: hasIsolated ? true : 'auto',
    symbol: 'circle',
    symbolSize: 8,
    connectNulls: false,
    sampling: big ? 'lttb' : undefined,
    stack: area && model.stacked ? 'total' : undefined,
    lineStyle: { width: 2, color },
    itemStyle: { color, borderColor: c.surface, borderWidth: 2 },
    areaStyle: area ? { color, opacity: 0.12 } : undefined,
    emphasis: { focus: model.series.length > 1 ? 'series' : 'none' },
    endLabel: layout.tipLabels
      ? {
          show: true,
          color: c.palette.text,
          fontSize: 12,
          formatter: (p: { value?: unknown }) => formatValue(numberFrom(p.value)),
        }
      : undefined,
    labelLayout: layout.tipLabels ? { moveOverlap: 'shiftY' } : undefined,
  };
}

/** [t, v] pairs in time order, with a null point inside every gap (design §3.3). */
function timeData(model: ChartModel, values: Array<number | null>, withBreaks: boolean) {
  const out: Array<[number, number | null]> = [];
  const breaks = new Set(withBreaks ? model.x.breaks : []);
  model.x.cats.forEach((cat, i) => {
    out.push([cat.t ?? 0, values[i] ?? null]);
    if (breaks.has(i) && i + 1 < model.x.cats.length) {
      out.push([((cat.t ?? 0) + (model.x.cats[i + 1].t ?? 0)) / 2, null]);
    }
  });
  return out;
}

// ---- per-form options ----------------------------------------------------------

function cartesian(c: Ctx, base: EChartsOption, exportTop?: number): EChartsOption {
  const { model, layout } = c;
  const ids = model.x.cats.map((k) => k.id);
  const seriesIds = model.series.map((s) => s.id);
  const form = layout.form;
  const isTime = form === 'timeLine' || form === 'timeBars';
  const tooltip = {
    ...(base.tooltip as object),
    trigger: 'axis',
    axisPointer:
      form === 'columns' || form === 'timeColumns' || form === 'histogram' || form === 'timeBars'
        ? { type: 'shadow', shadowStyle: { color: c.palette.baseline, opacity: 0.3 } }
        : { type: 'line', lineStyle: { color: c.palette.baseline, width: 1 } },
    formatter: axisTooltip(c),
  };
  let series: EChartsOption[];
  if (form === 'timeLine') {
    series = model.series.map((s, i) => lineSeries(c, i, timeData(model, s.values, true)));
  } else if (form === 'timeBars') {
    series = model.series.map((s, i) => barSeries(c, i, timeData(model, s.values, false)));
  } else if (form === 'catLine') {
    series = model.series.map((s, i) => lineSeries(c, i, s.values));
  } else if (form === 'histogram') {
    series = [barSeries(c, 0, model.series[0].values, { histogram: true })];
  } else if (form === 'combo') {
    series = model.series.map((s, i) =>
      s.kind === 'bar' ? barSeries(c, i, s.values) : lineSeries(c, i, s.values),
    );
  } else {
    series = model.series.map((s, i) => barSeries(c, i, s.values));
  }
  return {
    ...base,
    tooltip,
    legend: legendOf(c, seriesIds),
    grid: gridOf(c, exportTop),
    xAxis: isTime ? timeAxis(c) : categoryAxis(c, ids),
    yAxis: valueAxis(c),
    dataZoom: dataZoomOf(c),
    series,
  };
}

function pieOption(c: Ctx, base: EChartsOption, exportTop?: number): EChartsOption {
  const { model, layout, palette } = c;
  const d = layout.pie?.diameter ?? 240;
  const r = Math.max(40, d / 2 - 4);
  const values = model.series[0].values;
  const ids = model.x.cats.map((k) => k.id);
  return {
    ...base,
    tooltip: { ...(base.tooltip as object), trigger: 'item', formatter: itemTooltip(c) },
    legend: legendOf(c, ids),
    series: [
      {
        id: 'parts',
        name: model.series[0].id,
        type: 'pie',
        radius: layout.form === 'donut' ? [Math.round(r * 0.6), r] : [0, r],
        center: ['50%', exportTop !== undefined ? exportTop + r + 8 : '50%'],
        avoidLabelOverlap: false,
        padAngle: 1,
        data: model.x.cats.map((cat, i) => ({
          name: cat.id,
          value: Math.max(values[i] ?? 0, 0),
          itemStyle: { color: seriesColor(palette, i) },
        })),
        itemStyle: { borderColor: c.surface, borderWidth: 2 },
        // No slice names on the canvas: the HTML legend is the direct label.
        label: { show: false },
        labelLine: { show: false },
        emphasis: { scale: !c.opts.reducedMotion, scaleSize: 4 },
      },
    ],
  };
}

function funnelOption(c: Ctx, base: EChartsOption, exportTop?: number): EChartsOption {
  const { model, layout, palette } = c;
  const values = model.series[0].values;
  const n = model.x.cats.length;
  const ramp = palette.ordinal.length ? palette.ordinal : palette.series;
  const step = (i: number) =>
    n <= ramp.length ? ramp[i] : ramp[Math.round((i * (ramp.length - 1)) / Math.max(1, n - 1))];
  const labelWidth = layout.funnel?.labelWidth ?? 120;
  return {
    ...base,
    tooltip: { ...(base.tooltip as object), trigger: 'item', formatter: itemTooltip(c) },
    legend: { show: false, data: model.x.cats.map((k) => k.id) },
    series: [
      {
        id: 'stages',
        name: model.series[0].id,
        type: 'funnel',
        // `sort: 'none'` is load-bearing. ECharts sorts funnel data by
        // value by default, which would reorder the stages and assert a
        // sequence nobody verified. The backend only ever emits a funnel
        // when it could put the stages in a TRUSTED order, and the rows
        // arrive in that order; re-sorting would throw that away.
        sort: 'none',
        left: 8,
        right: labelWidth + 32,
        top: (exportTop ?? 0) + 8,
        bottom: 8,
        minSize: '18%',
        gap: 2,
        data: model.x.cats.map((cat, i) => ({
          name: cat.id,
          value: Math.max(values[i] ?? 0, 0),
          itemStyle: { color: step(i) },
        })),
        // Labels sit OUTSIDE the shape, in text ink — never white text on a
        // light ramp step.
        label: {
          show: true,
          position: 'right',
          color: palette.text,
          fontSize: 12,
          lineHeight: layout.lineHeight,
          formatter: (p: { name?: string }) => layout.funnel?.labelText[String(p.name)] ?? c.catLabel(p.name),
        },
        labelLine: { show: true, length: 12, lineStyle: { color: palette.baseline } },
        itemStyle: { borderColor: c.surface, borderWidth: 1 },
      },
    ],
  };
}

function scatterOption(c: Ctx, base: EChartsOption, exportTop?: number): EChartsOption {
  const { model } = c;
  const nums = model.x.nums ?? [];
  return {
    ...base,
    tooltip: { ...(base.tooltip as object), trigger: 'item', formatter: scatterTooltip(c) },
    legend: legendOf(c, model.series.map((s) => s.id)),
    grid: gridOf(c, exportTop),
    // Axis titles are HTML captions (design §4.4), never the canvas `name`.
    // The x axis gets its own tick style from the x extent, not the y one.
    xAxis: {
      ...valueAxis(c),
      minInterval: nums.every((v) => v === null || Number.isInteger(v)) ? 1 : undefined,
      min: undefined,
      max: undefined,
      axisLabel: {
        color: c.palette.axisText,
        fontSize: 12,
        formatter: axisTickFormatter(nums.reduce<number>((a, v) => Math.max(a, Math.abs(v ?? 0)), 0)),
      },
      splitLine: { show: false },
      axisLine: { show: true, onZero: false, lineStyle: { color: c.palette.baseline } },
    },
    yAxis: valueAxis(c),
    series: model.series.map((s, i) => ({
      id: s.id,
      name: s.id,
      type: 'scatter',
      data: s.values
        .map((v, j) => [nums[j] ?? null, v])
        .filter((p): p is [number, number] => p[0] !== null && p[1] !== null),
      symbolSize: 9,
      itemStyle: { color: c.colorOf(i), borderColor: c.surface, borderWidth: 1 },
    })),
  };
}

/**
 * Horizontal bars as an ECharts option. On screen a horizontal-bar form is
 * the HTML bar list (design §4.2, C2); this canvas version exists for the PNG
 * export and for `buildChartOption` callers. Labels are pre-broken at 40% of
 * the width, every row as tall as the tallest label needs.
 */
export function horizontalBarOption(
  model: ChartModel,
  palette: ChartPalette,
  opts: BuildOpts & { width?: number; top?: number },
): { option: EChartsOption; height: number } {
  const width = opts.width ?? 720;
  const measure = opts.measure ?? ((t: string) => estimateWidth(t));
  const labelCol = Math.round(width * 0.4);
  const lines = model.x.cats.map((cat) => wrapLines(cat.label, labelCol - 24, measure));
  const lineHeight = 16;
  const pitch = Math.max(28, Math.max(1, ...lines.map((l) => l.length)) * lineHeight + 10);
  const top = opts.top ?? 12;
  const bottom = 32;
  const height = top + model.x.cats.length * pitch + bottom;
  const text: Record<string, string> = {};
  model.x.cats.forEach((cat, i) => {
    text[cat.id] = lines[i].join('\n');
  });
  const layout = {
    renderer: 'echarts',
    form: 'columns',
    model,
    forExport: opts.forExport === true,
    width,
    height,
    grid: { top, right: 72, bottom, left: labelCol },
    plotHeight: height - top - bottom,
    catText: text,
    catLines: lines.map((l) => l.length),
    lineHeight,
    rotate: 0,
    numbered: false,
    labelEvery: 1,
    zoom: null,
    showSymbol: false,
    tipLabels: false,
    notes: [],
    tableOpen: false,
    structuralKey: '',
  } as ChartLayout;
  const c = makeCtx(model, layout, palette, opts);
  const base = baseOption(c);
  const valueW = widest(
    model.series.flatMap((s) => s.values.map((v) => formatValue(v))),
    measure,
  );
  const stacked = model.stacked && model.series.length > 1;
  const option: EChartsOption = {
    ...base,
    tooltip: {
      ...(base.tooltip as object),
      trigger: 'axis',
      axisPointer: { type: 'shadow' },
      formatter: axisTooltip(c),
    },
    legend: legendOf(c, model.series.map((s) => s.id)),
    grid: { top, right: Math.ceil(valueW + 24), bottom, left: labelCol },
    // The axes swap, and only here: horizontal_bar is its own chart type.
    xAxis: valueAxis(c),
    yAxis: {
      ...categoryAxis(c, model.x.cats.map((k) => k.id)),
      inverse: true, // largest/first at the top, matching the report PNG
    },
    series: model.series.map((s, i) => ({
      id: s.id,
      name: s.id,
      type: 'bar',
      data: s.values,
      stack: stacked ? 'total' : undefined,
      barMaxWidth: 20,
      itemStyle: {
        color: c.colorOf(i),
        borderRadius: stacked ? 0 : [0, 4, 4, 0],
        borderColor: stacked ? c.surface : undefined,
        borderWidth: stacked ? 1 : 0,
      },
      label:
        model.series.length === 1 || (stacked && i === model.series.length - 1)
          ? {
              show: true,
              position: 'right',
              color: palette.text,
              fontSize: 12,
              formatter: (p: { dataIndex?: number; value?: unknown }) => {
                if (!stacked) return formatValue(numberFrom(p.value));
                const idx = p.dataIndex ?? 0;
                return formatValue(model.series.reduce((a, ss) => a + (ss.values[idx] ?? 0), 0));
              },
            }
          : { show: false },
    })),
  };
  return { option, height };
}

/**
 * Build the ECharts option for a planned chart.
 *
 * Returns null for forms with no canvas (stat, empty, table) and for the bar
 * list on screen. Never throws.
 */
export function buildOptionFromModel(
  model: ChartModel,
  layout: ChartLayout,
  palette: ChartPalette,
  opts: BuildOpts = {},
): EChartsOption | null {
  try {
    if (layout.renderer === 'barlist') {
      return opts.forExport
        ? horizontalBarOption(model, palette, { ...opts, width: layout.width }).option
        : null;
    }
    if (layout.renderer !== 'echarts') return null;
    const c = makeCtx(model, layout, palette, opts);
    const base = baseOption(c);
    switch (layout.form) {
      case 'pie':
      case 'donut':
        return pieOption(c, base);
      case 'funnel':
        return funnelOption(c, base);
      case 'scatter':
        return scatterOption(c, base);
      default:
        return cartesian(c, base);
    }
  } catch {
    return null;
  }
}

/**
 * The export variant: the same chart with a canvas title and legend above the
 * plot. Text is pre-broken at the export width (C16). `top` is the plot's top
 * edge once the title and legend have taken their room.
 *
 * `layout` must be planned for the image (`layoutChart(..., { forExport: true })`,
 * as lib/chartExport does). An on-screen plan returns null, which the caller
 * reports as a failed export, rather than a PNG whose labels overlap (N2).
 */
export function buildExportOption(
  model: ChartModel,
  layout: ChartLayout,
  palette: ChartPalette,
  opts: BuildOpts & { title: string; measure: Measure },
): { option: EChartsOption; height: number } | null {
  if (!layout.forExport) return null;
  const measure = opts.measure;
  const width = layout.width;
  const titleLines = opts.title ? wrapLines(opts.title, width - 48, (t) => measure(t) * (16 / 12)) : [];
  const pieLike = layout.form === 'pie' || layout.form === 'donut';
  const labels = pieLike ? model.x.cats.map((k) => k.label) : model.series.map((s) => s.label);
  // Legend height, greedy rows, as ECharts lays out a plain legend.
  let legendH = 0;
  if (labels.length >= 2) {
    const maxW = width - 48;
    let x = 0;
    let rowH = 0;
    for (const label of labels) {
      const lines = wrapLines(label, 320, measure);
      const w = 10 + 5 + widest(lines, measure);
      if (x > 0 && x + w > maxW) {
        legendH += rowH + 10;
        x = 0;
        rowH = 0;
      }
      x += w + 16;
      rowH = Math.max(rowH, lines.length * 16);
    }
    legendH += rowH + 10;
  }
  const titleH = titleLines.length ? titleLines.length * 22 + 12 : 0;
  const legendTop = 16 + titleH;
  const top = legendTop + legendH + 12;
  const exportOpts: BuildOpts = {
    ...opts,
    forExport: true,
    reducedMotion: true,
    exportLegendTop: legendTop,
  };
  const title = titleLines.length
    ? {
        text: titleLines.join('\n'),
        left: 24,
        top: 16,
        textStyle: { color: palette.tooltipText, fontSize: 16, fontWeight: 600, lineHeight: 22 },
      }
    : undefined;
  try {
    if (layout.renderer === 'barlist') {
      const { option, height } = horizontalBarOption(model, palette, {
        ...exportOpts,
        width,
        top,
      });
      return { option: { ...option, title }, height };
    }
    if (layout.renderer !== 'echarts') return null;
    const c = makeCtx(model, layout, palette, exportOpts);
    const base = baseOption(c);
    let option: EChartsOption;
    let height = layout.height + (top - layout.grid.top);
    if (layout.zoom) height -= layout.zoom.slider + 12;
    switch (layout.form) {
      case 'pie':
      case 'donut':
        option = pieOption(c, base, top);
        height = top + (layout.pie?.diameter ?? 240) + 24;
        break;
      case 'funnel':
        option = funnelOption(c, base, top);
        height = layout.height + top;
        break;
      case 'scatter':
        option = scatterOption(c, base, top);
        break;
      default:
        option = cartesian(c, base, top);
    }
    // Review V2: numbered ticks need their key IN the image — the HTML key
    // under the figure is not part of the PNG.
    const key = layout.numbered ? exportKey(model, palette, width, height + 8, measure) : null;
    const titles = [title, ...(key?.titles ?? [])].filter(Boolean);
    if (key) {
      height += key.height + 8;
      // The grid is placed from the canvas bottom: keep the plot where it was.
      const grid = option.grid as { bottom?: number } | undefined;
      if (grid && typeof grid.bottom === 'number') {
        option = { ...option, grid: { ...grid, bottom: grid.bottom + key.height + 8 } };
      }
    }
    return { option: { ...option, title: titles.length ? titles : undefined }, height };
  } catch {
    return null;
  }
}

/**
 * The numbered key as canvas text blocks ("1 · North-east region office"),
 * in two columns when there is room, each entry pre-broken at its column
 * width (a canvas text never wraps by itself).
 */
function exportKey(
  model: ChartModel,
  palette: ChartPalette,
  width: number,
  top: number,
  measure: Measure,
): { titles: EChartsOption[]; height: number } {
  const lineH = 16;
  const cols = width >= 600 ? 2 : 1;
  const gap = 24;
  const colW = Math.floor((width - 48 - gap * (cols - 1)) / cols);
  const entries = model.x.cats.map((cat, i) => wrapLines(`${i + 1} · ${cat.label}`, colW, measure));
  const perCol = Math.ceil(entries.length / cols);
  const titles: EChartsOption[] = [];
  let tallest = 0;
  for (let k = 0; k < cols; k++) {
    const lines = entries.slice(k * perCol, (k + 1) * perCol).flat();
    if (lines.length === 0) continue;
    tallest = Math.max(tallest, lines.length);
    titles.push({
      text: lines.join('\n'),
      left: 24 + k * (colW + gap),
      top,
      textStyle: { color: palette.axisText, fontSize: 12, fontWeight: 400, lineHeight: lineH },
    });
  }
  return { titles, height: tallest * lineH + 8 };
}

/**
 * Build the ECharts option for `spec` over `rows` (the v1 entry point, kept
 * for callers and tests): model + a 720 px layout. A horizontal-bar form
 * returns the canvas horizontal option (the one the PNG export uses).
 *
 * Returns null when the chart cannot be drawn — the caller shows the table
 * instead. Never throws.
 */
export function buildChartOption(
  spec: ChartSpec,
  rows: DataRow[],
  palette: ChartPalette,
): EChartsOption | null {
  if (validateChart(spec, rows) !== null) return null;
  try {
    const result = modelFromSpec(spec, rows);
    if (!result.model) return null;
    const layout = layoutChart(result.model, {
      width: 720,
      context: 'inline',
      measure: (t) => estimateWidth(t),
    });
    if (layout.renderer === 'barlist') {
      return horizontalBarOption(layout.model, palette, { width: 720 }).option;
    }
    return buildOptionFromModel(layout.model, layout, palette);
  } catch {
    return null;
  }
}

/** Category count above which a chart is dense enough to warrant a note. */
export const CATEGORY_TICK_LIMIT = MAX_CATEGORY_TICKS;
