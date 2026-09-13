'use client';

/**
 * The console's one chart.
 *
 * Every analytics page draws through this component so the whole console
 * shares one grid, one tooltip, one legend and one palette — the alternative
 * is nine slightly different charts, which is how a dashboard starts looking
 * assembled rather than designed.
 *
 * VISUAL RULES (they are the reference's, and they are deliberate):
 *  · one y-axis, ever — two scales on one frame invite false correlations
 *  · horizontal grid only, hairline and dashed; no axis lines, no ticks
 *  · smooth 2px lines over a very light fill, so overlapping series stay
 *    readable instead of becoming a solid block
 *  · the tooltip is the dense surface: every series, aligned, with the exact
 *    number — the axis itself stays sparse
 *  · a legend only when there are two or more series; one series is named by
 *    the section heading above it
 *
 * ECharts is loaded through `next/dynamic` with `ssr: false` (it needs a DOM
 * to measure and a canvas to draw), so no analytics page ships it in the
 * first chunk.
 */

import { useEffect, useMemo, useRef, useState } from 'react';
import dynamic from 'next/dynamic';
import {
  TONE_FALLBACK,
  fallbackPalette,
  resolvePalette,
  resolveTones,
  type ChartPalette,
  type ChartTones,
} from '@/lib/chartTheme';
import type { EChartsOption } from '@/lib/chartOption';
import { bucketLabel, compact, exact } from './format';

const EChart = dynamic(() => import('@/components/EChart'), { ssr: false });

/**
 * Which points a line cannot draw: a measured value with no measured
 * neighbour on either side. A line needs two points, and with `showSymbol:
 * false` ECharts draws NOTHING for one — a single day of traffic rendered as
 * an empty axis scaled to 600 (responsive audit, 2026-09-13), and a lone day
 * between two gaps vanished the same way.
 */
export function isolatedPoints(data: (number | null)[]): boolean[] {
  const measured = (i: number) => i >= 0 && i < data.length && data[i] != null;
  return data.map((_, i) => measured(i) && !measured(i - 1) && !measured(i + 1));
}

/** Legend geometry, shared by the option below and the row estimate. */
const LEGEND_ICON = 8;
const LEGEND_GAP = 18;
const LEGEND_FONT_PX = 11;
/** ECharts puts 5px between a legend icon and its text, and pads the box 5px. */
const LEGEND_ICON_TEXT_GAP = 5;
const LEGEND_PADDING = 5;
/** One legend row: the line's height plus the item gap ECharts also uses between rows. */
const LEGEND_ROW_PITCH = 14 + LEGEND_GAP;
/** Room under the plot for the x labels and one legend row. */
const LEGEND_ONE_ROW_BOTTOM = 34;

/**
 * How many rows ECharts will wrap a horizontal legend into at this chart width.
 *
 * The same greedy pass as its box layout: an item that would end past the
 * right edge starts a new row. The grid only reserved room for ONE row, so on
 * a 360px phone the four-series "Daily active users" legend wrapped and its
 * first row sat on top of the x-axis dates (re-audit, 2026-09-13).
 * `textWidth` is injected so the estimate is testable without a canvas.
 */
export function legendRows(
  names: string[],
  chartWidth: number,
  textWidth: (text: string) => number,
): number {
  if (names.length < 2 || !(chartWidth > 0)) return 1;
  const maxWidth = chartWidth - LEGEND_PADDING * 2;
  let rows = 1;
  let x = 0;
  for (const name of names) {
    const width = LEGEND_ICON + LEGEND_ICON_TEXT_GAP + textWidth(name);
    if (x > 0 && x + width > maxWidth) {
      rows += 1;
      x = 0;
    }
    x += width + LEGEND_GAP;
  }
  return rows;
}

/** The grid's bottom margin for a legend of `rows` rows (none when `rows` is 0). */
export function legendBottom(rows: number): number {
  return rows < 1 ? 8 : LEGEND_ONE_ROW_BOTTOM + (rows - 1) * LEGEND_ROW_PITCH;
}

let measureContext: CanvasRenderingContext2D | null | undefined;
/** Text width in the legend's font; a per-character guess where there is no canvas (jsdom). */
function legendTextWidth(text: string): number {
  if (measureContext === undefined) {
    try {
      measureContext = document.createElement('canvas').getContext('2d');
    } catch {
      measureContext = null;
    }
  }
  if (measureContext) {
    measureContext.font = `${LEGEND_FONT_PX}px sans-serif`;
    return measureContext.measureText(text).width;
  }
  return text.length * LEGEND_FONT_PX * 0.56;
}

export interface Series {
  /** Legend and tooltip name. */
  name: string;
  /** One value per x point; null draws a gap, which is what "not measured" is. */
  data: (number | null)[];
  /** Filled area under the line — the default for volume-shaped series. */
  area?: boolean;
  /** Draw as bars instead (distributions, not trends). */
  bar?: boolean;
  /**
   * Take a SEMANTIC colour instead of the next palette slot — "failed" is red
   * on every page it appears on. A raw CSS string would not work here: the
   * chart draws to a canvas, which cannot resolve `var()`, so tones come
   * resolved from lib/chartTheme, beside the series palette.
   */
  tone?: keyof ChartTones;
  /** Formatter for this series' tooltip value. Defaults to an exact count. */
  format?: (value: number) => string;
}

/**
 * Resolve the design system's chart tokens once the DOM exists.
 *
 * A canvas cannot read `var()`, so the colors are looked up from computed
 * style at option-build time; before hydration the literal fallbacks stand in
 * (they are the same five hexes), which keeps the first paint from flashing
 * a different palette.
 */
interface Chrome {
  palette: ChartPalette;
  tones: ChartTones;
}

function useChrome(): Chrome {
  const [chrome, setChrome] = useState<Chrome>(() => ({
    palette: fallbackPalette('dark'),
    tones: TONE_FALLBACK,
  }));
  useEffect(() => {
    const root = document.documentElement;
    const read = () =>
      setChrome({
        palette: resolvePalette(
          root.classList.contains('light') ? 'light' : 'dark',
          root,
        ),
        tones: resolveTones(root),
      });
    read();
    const observer = new MutationObserver(read);
    observer.observe(root, { attributes: true, attributeFilter: ['class'] });
    return () => observer.disconnect();
  }, []);
  return chrome;
}

function tooltipRow(
  color: string,
  name: string,
  value: string,
  palette: ChartPalette,
): string {
  return (
    `<div style="display:flex;align-items:center;gap:8px;margin-top:4px">` +
    `<span style="width:8px;height:8px;border-radius:9999px;background:${color};flex:none"></span>` +
    `<span style="color:${palette.text};flex:1">${name}</span>` +
    `<span style="font-variant-numeric:tabular-nums;font-weight:600;color:${palette.tooltipText}">${value}</span>` +
    `</div>`
  );
}

interface TooltipParam {
  seriesName?: string;
  dataIndex?: number;
  value?: unknown;
  color?: string;
}

export function AnalyticsChart({
  labels,
  series,
  height = 260,
  bucket = 'day',
  ariaLabel,
  stacked = false,
  yFormat = compact,
}: {
  /** ISO timestamps for a time series, or plain strings for a distribution. */
  labels: string[];
  series: Series[];
  height?: number;
  bucket?: 'hour' | 'day' | 'none';
  ariaLabel: string;
  /** Stack the areas — only for parts of one whole (a route mix). */
  stacked?: boolean;
  yFormat?: (value: number | null) => string;
}) {
  const { palette, tones } = useChrome();
  const box = useRef<HTMLDivElement | null>(null);
  const [width, setWidth] = useState(0);

  useEffect(() => {
    const el = box.current;
    if (!el) return;
    setWidth(el.clientWidth);
    if (typeof ResizeObserver === 'undefined') return;
    const observer = new ResizeObserver(() => setWidth(el.clientWidth));
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  // A handful of names measured per render; `rows` is a number, so the
  // option below is rebuilt only when the row count actually changes.
  const rows =
    series.length > 1
      ? legendRows(series.map((s) => s.name), width, legendTextWidth)
      : 0;

  const option = useMemo<EChartsOption>(() => {
    const axisLabels = labels.map((l) =>
      bucket === 'none' ? l : bucketLabel(l, bucket),
    );
    const longLabels = labels.map((l) =>
      bucket === 'none' ? l : bucketLabel(l, bucket, true),
    );
    const colorAt = (i: number) => {
      const tone = series[i]?.tone;
      if (tone) return tones[tone];
      return palette.series[i % palette.series.length] ?? '#888';
    };

    return {
      // No animation on data change: these charts refresh on a filter click,
      // and a 1s morph between two months of data reads as lag.
      animationDuration: 220,
      color: series.map((_, i) => colorAt(i)),
      grid: {
        left: 8,
        right: 12,
        top: 12,
        bottom: legendBottom(rows),
        containLabel: true,
      },
      tooltip: {
        trigger: 'axis',
        backgroundColor: palette.tooltipBg,
        borderWidth: 0,
        padding: [8, 10],
        textStyle: { color: palette.tooltipText, fontSize: 12 },
        extraCssText:
          'border-radius:10px;box-shadow:0 8px 24px rgba(0,0,0,.35);min-width:150px',
        axisPointer: {
          type: 'line',
          lineStyle: { color: palette.axis, width: 1, type: 'solid' },
        },
        formatter: (params: TooltipParam[] | TooltipParam) => {
          const rows = Array.isArray(params) ? params : [params];
          if (!rows.length) return '';
          const index = rows[0]?.dataIndex ?? 0;
          const head =
            `<div style="font-weight:600;color:${palette.tooltipText}">` +
            `${longLabels[index] ?? ''}</div>`;
          const body = rows
            .map((row) => {
              const spec = series.find((s) => s.name === row.seriesName);
              const raw = row.value;
              const text =
                raw == null || typeof raw !== 'number'
                  ? '—'
                  : (spec?.format ?? exact)(raw);
              return tooltipRow(
                String(row.color ?? ''),
                String(row.seriesName ?? ''),
                text,
                palette,
              );
            })
            .join('');
          return head + body;
        },
      },
      legend: series.length > 1
        ? {
            bottom: 0,
            itemWidth: LEGEND_ICON,
            itemHeight: LEGEND_ICON,
            icon: 'circle',
            itemGap: LEGEND_GAP,
            textStyle: { color: palette.text, fontSize: LEGEND_FONT_PX },
          }
        : { show: false },
      xAxis: {
        type: 'category',
        data: axisLabels,
        boundaryGap: series.some((s) => s.bar),
        axisLine: { show: false },
        axisTick: { show: false },
        // A month of days is 30 ticks; ECharts thins them itself, and forcing
        // every label produces the diagonal sawtooth this console avoids.
        axisLabel: {
          color: palette.text,
          fontSize: 11,
          hideOverlap: true,
          margin: 12,
        },
      },
      yAxis: {
        type: 'value',
        splitLine: {
          lineStyle: { color: palette.grid, width: 1, type: [3, 4] },
        },
        axisLine: { show: false },
        axisTick: { show: false },
        axisLabel: {
          color: palette.text,
          fontSize: 11,
          formatter: (value: number) => yFormat(value),
        },
        // Four bands is the reference's density: enough to read a level off,
        // few enough that the grid stays background.
        splitNumber: 4,
        minInterval: 1,
      },
      series: series.map((s, i) => {
        const color = colorAt(i);
        if (s.bar) {
          return {
            name: s.name,
            type: 'bar',
            data: s.data,
            // Wide enough that a six-bucket histogram across a 1440px page
            // reads as a distribution rather than as six pins.
            barMaxWidth: 56,
            itemStyle: { color, borderRadius: [4, 4, 0, 0] },
          };
        }
        // Isolated points get a dot; every other point keeps the bare line.
        const isolated = isolatedPoints(s.data);
        const hasIsolated = isolated.some(Boolean);
        return {
          name: s.name,
          type: 'line',
          data: hasIsolated
            ? s.data.map((value, idx) =>
                isolated[idx] || value == null ? value : { value, symbol: 'none' },
              )
            : s.data,
          smooth: 0.35,
          showSymbol: hasIsolated,
          // 'auto' thins symbols on a crowded axis; an isolated point must
          // not be the one thinned away.
          showAllSymbol: hasIsolated ? true : 'auto',
          symbol: 'circle',
          symbolSize: 6,
          // The 2px line is the mark; the fill only gives it weight.
          lineStyle: { width: 2, color },
          itemStyle: { color },
          stack: stacked ? 'total' : undefined,
          areaStyle: s.area
            ? {
                opacity: stacked ? 0.5 : 0.16,
                color,
              }
            : undefined,
          // A gap is a gap: connecting across a null would draw a straight
          // line through a period nobody measured.
          connectNulls: false,
        };
      }),
    };
  }, [labels, series, palette, tones, bucket, stacked, yFormat, rows]);

  return (
    <div ref={box} className="w-full min-w-0">
      <EChart option={option} height={height} ariaLabel={ariaLabel} />
    </div>
  );
}
