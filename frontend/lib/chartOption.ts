/**
 * The trusted ChartSpec → ECharts option adapter.
 *
 * ============================ SECURITY BOUNDARY ============================
 * Everything ECharts receives is constructed HERE, by this file, from a
 * validated `ChartSpec` and the query rows. Nothing from the backend is
 * ever spread into an option object:
 *
 *   - the only spec fields read are type / x_key / y_keys / title /
 *     stacked / bins / show_legend / show_values, and each is a closed
 *     enum, a bool, a bounded integer or a column NAME;
 *   - `type` is checked against a whitelist before dispatch, so a value
 *     from a newer backend renders the fallback instead of reaching
 *     ECharts;
 *   - x_key / y_keys are used only as property lookups on row objects —
 *     a name that is not a column yields no series, never an eval;
 *   - every `formatter` is a function defined in this file. ECharts
 *     accepts functions in options, which is exactly why one must never
 *     be able to arrive over the wire. `ChartSpec` has no field that
 *     could carry one, and there is no passthrough of unknown keys.
 *
 * Tooltip content is HTML, and category labels are Salesforce data (an
 * Account Name is user-controlled text). Every interpolated value goes
 * through `escapeHtml` — see `tooltipRow`.
 * ==========================================================================
 *
 * Pure module: no React, no DOM, no `echarts` import. That keeps it
 * testable under vitest's existing node environment, which is the whole
 * reason the adapter is a separate file from the component.
 */

import type { ChartSpec, ChartType, DataRow } from './types';
import { type ChartPalette, seriesColor } from './chartTheme';
import {
  type Cell,
  formatCell,
  formatCompact,
  formatNumber,
  isNumeric,
  toNumber,
  truncateLabel,
} from './chartFormat';

/** Structural stand-in for echarts' option type — keeps this module runtime-free. */
export type EChartsOption = Record<string, unknown>;

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
const PART_TO_WHOLE = new Set<string>(['pie', 'donut']);

const MAX_SLICES = 8;
const MAX_CATEGORY_TICKS = 40;

export function isChartType(value: unknown): value is ChartType {
  return typeof value === 'string' && TYPE_SET.has(value);
}

// ---------------------------------------------------------------------------
// Validation
// ---------------------------------------------------------------------------

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
 */
export function validateChart(
  spec: ChartSpec | null | undefined,
  rows: DataRow[] | null | undefined,
): ChartProblem | null {
  if (!spec || !isChartType(spec.type)) return 'unsupported-type';
  if (!rows || rows.length === 0) return 'no-data';
  const present = new Set<string>();
  for (const row of rows) for (const k of Object.keys(row)) present.add(k);
  if (!spec.x_key || !present.has(spec.x_key)) return 'missing-x-column';
  const yKeys = (spec.y_keys ?? []).filter((k) => present.has(k));
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

// ---------------------------------------------------------------------------
// Row normalization
// ---------------------------------------------------------------------------

function usableYKeys(spec: ChartSpec, rows: DataRow[]): string[] {
  const present = new Set<string>();
  for (const row of rows) for (const k of Object.keys(row)) present.add(k);
  return (spec.y_keys ?? []).filter((k) => present.has(k));
}

function categoriesOf(spec: ChartSpec, rows: DataRow[]): string[] {
  return rows.map((r) => formatCell(r[spec.x_key] as Cell));
}

function valuesOf(rows: DataRow[], key: string): Array<number | null> {
  return rows.map((r) => toNumber(r[key] as Cell));
}

/** Ordered (label, value) pairs for a part-to-whole chart, tail folded into "Other". */
export function partToWholeData(
  spec: ChartSpec,
  rows: DataRow[],
  key: string,
): Array<{ name: string; value: number }> {
  const pairs = rows
    .map((r) => ({
      name: formatCell(r[spec.x_key] as Cell),
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
 * names, which are equally untrusted (a custom field's API name).
 */
export function escapeHtml(text: string): string {
  return text.replace(/[&<>"']/g, (c) => ESCAPES[c]);
}

function swatch(color: string): string {
  return (
    `<span style="display:inline-block;width:9px;height:9px;border-radius:50%;` +
    `margin-right:6px;background:${escapeHtml(color)}"></span>`
  );
}

function tooltipRow(color: string, name: string, value: string): string {
  return `<div>${swatch(color)}${escapeHtml(name)}: <b>${escapeHtml(value)}</b></div>`;
}

interface TooltipParam {
  seriesName?: string;
  name?: string;
  color?: string;
  value?: unknown;
  percent?: number;
  axisValueLabel?: string;
}

function numberFrom(value: unknown, axis: 'x' | 'y' = 'y'): number | null {
  if (Array.isArray(value)) return toNumber(value[axis === 'y' ? 1 : 0] as Cell);
  return toNumber(value as Cell);
}

function axisTooltipFormatter(params: TooltipParam | TooltipParam[]): string {
  const list = Array.isArray(params) ? params : [params];
  if (list.length === 0) return '';
  const header = list[0].axisValueLabel ?? list[0].name ?? '';
  const body = list
    .map((p) => {
      const n = numberFrom(p.value);
      return tooltipRow(p.color ?? '', p.seriesName ?? '', n === null ? '—' : formatNumber(n));
    })
    .join('');
  return `<div style="font-weight:600;margin-bottom:4px">${escapeHtml(header)}</div>${body}`;
}

function itemTooltipFormatter(params: TooltipParam | TooltipParam[]): string {
  const p = Array.isArray(params) ? params[0] : params;
  if (!p) return '';
  const n = numberFrom(p.value);
  const value = n === null ? '—' : formatNumber(n);
  const pct = typeof p.percent === 'number' ? ` (${p.percent.toFixed(1)}%)` : '';
  return tooltipRow(p.color ?? '', p.name ?? '', `${value}${pct}`);
}

function scatterTooltipFormatter(spec: ChartSpec, yKey: string) {
  return (params: TooltipParam | TooltipParam[]): string => {
    const p = Array.isArray(params) ? params[0] : params;
    if (!p) return '';
    const x = numberFrom(p.value, 'x');
    const y = numberFrom(p.value, 'y');
    return (
      tooltipRow(p.color ?? '', spec.x_key, x === null ? '—' : formatNumber(x)) +
      tooltipRow(p.color ?? '', yKey, y === null ? '—' : formatNumber(y))
    );
  };
}

function valueLabel(params: { value?: unknown }): string {
  const n = numberFrom(params.value);
  return n === null ? '' : formatNumber(n);
}

// ---------------------------------------------------------------------------
// Option construction
// ---------------------------------------------------------------------------

function baseOption(spec: ChartSpec, palette: ChartPalette): EChartsOption {
  return {
    animation: true,
    animationDuration: 350, // §9: draw-in ≤ 400ms
    animationEasing: 'cubicOut',
    color: palette.series,
    textStyle: { color: palette.text, fontSize: 12 },
    tooltip: {
      backgroundColor: palette.tooltipBg,
      borderColor: palette.grid,
      borderWidth: 1,
      textStyle: { color: palette.tooltipText, fontSize: 13 },
      extraCssText: 'border-radius:10px;',
    },
  };
}

function legendOf(spec: ChartSpec, palette: ChartPalette, show: boolean): EChartsOption | undefined {
  if (!show || spec.show_legend === false) return undefined;
  return {
    show: true,
    bottom: 0,
    icon: 'circle',
    itemWidth: 9,
    itemHeight: 9,
    textStyle: { color: palette.text, fontSize: 13 },
  };
}

function categoryAxis(palette: ChartPalette, data: string[], rotate = false): EChartsOption {
  return {
    type: 'category',
    data,
    axisLine: { lineStyle: { color: palette.axis } },
    axisTick: { show: false },
    axisLabel: {
      color: palette.text,
      fontSize: 12,
      hideOverlap: true,
      rotate: rotate ? 35 : 0,
      formatter: (v: string) => truncateLabel(String(v)),
    },
  };
}

function valueAxis(palette: ChartPalette, showSplit = true): EChartsOption {
  return {
    type: 'value',
    axisLine: { show: false },
    axisTick: { show: false },
    axisLabel: { color: palette.text, fontSize: 12, formatter: (v: number) => formatCompact(v) },
    splitLine: { show: showSplit, lineStyle: { color: palette.grid } },
  };
}

function labelOption(spec: ChartSpec, palette: ChartPalette, position: string) {
  if (!spec.show_values) return { show: false };
  return { show: true, position, color: palette.text, fontSize: 11, formatter: valueLabel };
}

/**
 * Build the ECharts option for `spec` over `rows`.
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
    return buildOption(spec, rows, palette);
  } catch {
    return null;
  }
}

function buildOption(spec: ChartSpec, rows: DataRow[], palette: ChartPalette): EChartsOption {
  const yKeys = usableYKeys(spec, rows);
  const multi = yKeys.length > 1;
  const base = baseOption(spec, palette);

  switch (spec.type) {
    case 'pie':
    case 'donut':
      return partToWholeOption(spec, rows, palette, base, yKeys[0]);
    case 'funnel':
      return funnelOption(spec, rows, palette, base, yKeys[0]);
    case 'scatter':
      return scatterOption(spec, rows, palette, base, yKeys[0]);
    case 'horizontal_bar':
      return horizontalBarOption(spec, rows, palette, base, yKeys, multi);
    case 'histogram':
      return histogramOption(spec, rows, palette, base, yKeys[0]);
    case 'line':
    case 'area':
      return lineOption(spec, rows, palette, base, yKeys, multi);
    case 'bar':
    default:
      return barOption(spec, rows, palette, base, yKeys, multi);
  }
}

function barOption(
  spec: ChartSpec,
  rows: DataRow[],
  palette: ChartPalette,
  base: EChartsOption,
  yKeys: string[],
  multi: boolean,
): EChartsOption {
  const cats = categoriesOf(spec, rows);
  const rotate = cats.length > 8 || cats.some((c) => c.length > 10);
  return {
    ...base,
    tooltip: { ...(base.tooltip as object), trigger: 'axis', formatter: axisTooltipFormatter },
    legend: legendOf(spec, palette, multi),
    grid: { top: 16, right: 12, bottom: multi ? 36 : 8, left: 8, containLabel: true },
    xAxis: categoryAxis(palette, cats, rotate),
    yAxis: valueAxis(palette),
    series: yKeys.map((key, i) => ({
      name: key,
      type: 'bar',
      data: valuesOf(rows, key),
      stack: spec.stacked ? 'total' : undefined,
      barMaxWidth: 36,
      itemStyle: {
        color: seriesColor(palette, i),
        borderRadius: spec.stacked ? 0 : [3, 3, 0, 0],
      },
      label: labelOption(spec, palette, 'top'),
    })),
  };
}

function horizontalBarOption(
  spec: ChartSpec,
  rows: DataRow[],
  palette: ChartPalette,
  base: EChartsOption,
  yKeys: string[],
  multi: boolean,
): EChartsOption {
  const cats = categoriesOf(spec, rows);
  return {
    ...base,
    tooltip: { ...(base.tooltip as object), trigger: 'axis', formatter: axisTooltipFormatter },
    legend: legendOf(spec, palette, multi),
    grid: { top: 12, right: 24, bottom: multi ? 36 : 8, left: 8, containLabel: true },
    // The axes swap, and only here. There is no `orientation` field on
    // ChartSpec: horizontal_bar is its own chart type, so orientation is
    // never a value the model can set on some other chart.
    xAxis: valueAxis(palette),
    yAxis: {
      ...categoryAxis(palette, cats),
      inverse: true, // largest/first at the top, matching the report PNG
    },
    series: yKeys.map((key, i) => ({
      name: key,
      type: 'bar',
      data: valuesOf(rows, key),
      stack: spec.stacked ? 'total' : undefined,
      barMaxWidth: 28,
      itemStyle: {
        color: seriesColor(palette, i),
        borderRadius: spec.stacked ? 0 : [0, 3, 3, 0],
      },
      label: labelOption(spec, palette, 'right'),
    })),
  };
}

function lineOption(
  spec: ChartSpec,
  rows: DataRow[],
  palette: ChartPalette,
  base: EChartsOption,
  yKeys: string[],
  multi: boolean,
): EChartsOption {
  const cats = categoriesOf(spec, rows);
  const isArea = spec.type === 'area';
  return {
    ...base,
    tooltip: { ...(base.tooltip as object), trigger: 'axis', formatter: axisTooltipFormatter },
    legend: legendOf(spec, palette, multi),
    grid: { top: 16, right: 16, bottom: multi ? 36 : 8, left: 8, containLabel: true },
    xAxis: { ...categoryAxis(palette, cats, cats.length > 10), boundaryGap: isArea ? false : true },
    yAxis: valueAxis(palette),
    series: yKeys.map((key, i) => ({
      name: key,
      type: 'line',
      data: valuesOf(rows, key),
      smooth: true,
      showSymbol: rows.length <= 60,
      symbolSize: 6,
      connectNulls: false,
      stack: isArea && spec.stacked ? 'total' : undefined,
      lineStyle: { width: 2, color: seriesColor(palette, i) },
      itemStyle: { color: seriesColor(palette, i) },
      // Area styling is set HERE, by type. The spec cannot describe a fill.
      areaStyle: isArea ? { color: seriesColor(palette, i), opacity: 0.22 } : undefined,
      label: labelOption(spec, palette, 'top'),
    })),
  };
}

function partToWholeOption(
  spec: ChartSpec,
  rows: DataRow[],
  palette: ChartPalette,
  base: EChartsOption,
  yKey: string,
): EChartsOption {
  const data = partToWholeData(spec, rows, yKey);
  // Radii are chosen here, by chart type — never by the model.
  const radius = spec.type === 'donut' ? ['45%', '72%'] : ['0%', '72%'];
  return {
    ...base,
    tooltip: { ...(base.tooltip as object), trigger: 'item', formatter: itemTooltipFormatter },
    legend: legendOf(spec, palette, true),
    series: [
      {
        name: yKey,
        type: 'pie',
        radius,
        center: ['50%', '46%'],
        data: data.map((d, i) => ({
          name: d.name,
          value: d.value,
          itemStyle: { color: seriesColor(palette, i) },
        })),
        padAngle: 2,
        itemStyle: { borderColor: palette.surface, borderWidth: 2 },
        label: spec.show_values
          ? { show: true, color: palette.text, formatter: '{b}: {d}%' }
          : { show: false },
        labelLine: { show: Boolean(spec.show_values) },
      },
    ],
  };
}

function funnelOption(
  spec: ChartSpec,
  rows: DataRow[],
  palette: ChartPalette,
  base: EChartsOption,
  yKey: string,
): EChartsOption {
  const data = rows.map((r, i) => ({
    name: formatCell(r[spec.x_key] as Cell),
    value: Math.max(toNumber(r[yKey] as Cell) ?? 0, 0),
    itemStyle: { color: seriesColor(palette, i) },
  }));
  return {
    ...base,
    tooltip: { ...(base.tooltip as object), trigger: 'item', formatter: itemTooltipFormatter },
    legend: legendOf(spec, palette, true),
    series: [
      {
        name: yKey,
        type: 'funnel',
        // `sort: 'none'` is load-bearing. ECharts sorts funnel data by
        // value by default, which would reorder the stages and assert a
        // sequence nobody verified. The backend only ever emits a funnel
        // when it could put the stages in a TRUSTED order (a Salesforce
        // standard picklist or an operator-supplied one), and the rows
        // arrive in that order; re-sorting would throw that away.
        sort: 'none',
        left: '8%',
        right: '8%',
        top: 12,
        bottom: 36,
        minSize: '18%',
        gap: 2,
        data,
        label: { show: true, position: 'inside', color: '#ffffff', fontSize: 12 },
        itemStyle: { borderColor: palette.surface, borderWidth: 1 },
      },
    ],
  };
}

function histogramOption(
  spec: ChartSpec,
  rows: DataRow[],
  palette: ChartPalette,
  base: EChartsOption,
  yKey: string,
): EChartsOption {
  // The rows are ALREADY binned — `chart_data.build_histogram` did that in
  // Python, over the full result, and shipped (bin_label, count). Nothing
  // is binned here, so the browser and the report PNG cannot disagree.
  const cats = categoriesOf(spec, rows);
  return {
    ...base,
    tooltip: { ...(base.tooltip as object), trigger: 'axis', formatter: axisTooltipFormatter },
    legend: undefined,
    grid: { top: 16, right: 12, bottom: 8, left: 8, containLabel: true },
    xAxis: categoryAxis(palette, cats, cats.length > 8),
    yAxis: valueAxis(palette),
    series: [
      {
        name: yKey,
        type: 'bar',
        data: valuesOf(rows, yKey),
        barCategoryGap: '0%', // adjacent bins touch — that is what makes it a histogram
        itemStyle: { color: seriesColor(palette, 0), borderColor: palette.surface, borderWidth: 1 },
        label: labelOption(spec, palette, 'top'),
      },
    ],
  };
}

function scatterOption(
  spec: ChartSpec,
  rows: DataRow[],
  palette: ChartPalette,
  base: EChartsOption,
  yKey: string,
): EChartsOption {
  // Numeric x only. A category string is not a coordinate; plotting one
  // would put points at positions that mean nothing.
  const points = rows
    .map((r) => [toNumber(r[spec.x_key] as Cell), toNumber(r[yKey] as Cell)])
    .filter((p): p is [number, number] => p[0] !== null && p[1] !== null);
  return {
    ...base,
    tooltip: {
      ...(base.tooltip as object),
      trigger: 'item',
      formatter: scatterTooltipFormatter(spec, yKey),
    },
    legend: undefined,
    grid: { top: 16, right: 16, bottom: 8, left: 8, containLabel: true },
    xAxis: { ...valueAxis(palette), name: spec.x_key, nameLocation: 'end', nameGap: 8 },
    yAxis: { ...valueAxis(palette), name: yKey },
    series: [
      {
        name: yKey,
        type: 'scatter',
        data: points,
        symbolSize: 9,
        itemStyle: { color: seriesColor(palette, 0), borderColor: palette.surface, borderWidth: 1 },
      },
    ],
  };
}

/** Category count above which a chart is dense enough to warrant a note. */
export const CATEGORY_TICK_LIMIT = MAX_CATEGORY_TICKS;
