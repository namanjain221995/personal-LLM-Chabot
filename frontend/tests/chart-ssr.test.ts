/**
 * Visual check without a browser (charts design v2 §16, C23).
 *
 * Each canvas fixture is rendered by ECharts' own server-side SVG renderer,
 * with `setPlatformAPI({ measureText })` set to the SAME deterministic
 * measurer the planner uses, so ECharts lays the text out exactly as the
 * planner measured it. Then every <text> box is checked: no two intersect, and
 * none crosses the canvas edge. Set CHART_SSR_OUT=<dir> to also write the SVGs
 * for eyeballing.
 *
 * Residual risk (stated in the design): real browser font metrics are not
 * exercised here. Made-up data.
 */

import { mkdirSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';
import * as echarts from 'echarts/core';
import { BarChart, FunnelChart, LineChart, PieChart, ScatterChart } from 'echarts/charts';
import {
  DataZoomInsideComponent,
  DataZoomSliderComponent,
  GridComponent,
  LegendComponent,
  TitleComponent,
  TooltipComponent,
} from 'echarts/components';
import { LabelLayout } from 'echarts/features';
import { SVGRenderer } from 'echarts/renderers';
import { modelFromSpec, type ChartModel } from '../lib/chartModel';
import { layoutChart } from '../lib/chartLayout';
import { buildExportOption, buildOptionFromModel } from '../lib/chartOption';
import { fallbackPalette } from '../lib/chartTheme';
import { estimateWidth } from '../lib/chartText';
import { parseMermaidChart } from '../lib/mermaidChart';
import type { ChartSpec, DataRow } from '../lib/types';

echarts.use([
  BarChart,
  LineChart,
  PieChart,
  ScatterChart,
  FunnelChart,
  GridComponent,
  LegendComponent,
  TitleComponent,
  TooltipComponent,
  DataZoomSliderComponent,
  DataZoomInsideComponent,
  LabelLayout,
  SVGRenderer,
]);

const measure = (t: string) => estimateWidth(t);
echarts.setPlatformAPI({
  measureText: (text: string, font?: string) => {
    const px = Number(/(\d+(?:\.\d+)?)px/.exec(font ?? '')?.[1] ?? 12);
    return { width: estimateWidth(text, px) };
  },
} as never);

const OUT = process.env.CHART_SSR_OUT;
const DAY = 86_400_000;

function spec(over: Partial<ChartSpec> = {}): ChartSpec {
  return { type: 'bar', x_key: 'x', y_keys: ['y'], title: 'T', stacked: false, ...over };
}
function model(s: ChartSpec, rows: DataRow[]): ChartModel {
  const r = modelFromSpec(s, rows);
  if (!r.model) throw new Error(String(r.problem));
  return r.model;
}
function months(n: number): string[] {
  return Array.from({ length: n }, (_, i) => new Date(Date.UTC(2025, i, 1)).toISOString().slice(0, 7));
}
function irregularDaily(): DataRow[] {
  const rows: DataRow[] = [];
  let t = Date.UTC(2025, 0, 3);
  const empty = new Set(Array.from({ length: 9 }, (_, i) => 2025 * 12 + 3 + i));
  while (rows.length < 95) {
    const d = new Date(t);
    if (!empty.has(d.getUTCFullYear() * 12 + d.getUTCMonth())) {
      rows.push({ x: d.toISOString().slice(0, 10), y: (rows.length % 11) + 1 });
    }
    t += DAY * (rows.length % 3 === 0 ? 2 : 3);
  }
  return rows;
}

interface Box {
  text: string;
  x0: number;
  x1: number;
  y0: number;
  y1: number;
}

function unescape(s: string): string {
  return s
    .replace(/<[^>]+>/g, '')
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'")
    .replace(/&amp;/g, '&');
}

/** Text boxes from ECharts' SVG: translate + anchor + central baseline. */
function textBoxes(svg: string): { boxes: Box[]; rotated: number } {
  const boxes: Box[] = [];
  let rotated = 0;
  for (const m of svg.matchAll(/<text([^>]*)>([\s\S]*?)<\/text>/g)) {
    const attrs = m[1];
    const text = unescape(m[2]);
    if (!text.trim()) continue;
    if (/rotate\(/.test(attrs) || /matrix\(/.test(attrs)) {
      rotated += 1;
      continue;
    }
    const tr = /translate\(([-\d.e]+)[ ,]+([-\d.e]+)\)/.exec(attrs);
    const tx = tr ? Number(tr[1]) : 0;
    const ty = tr ? Number(tr[2]) : 0;
    const x = tx + Number(/\sx="([-\d.e]+)"/.exec(attrs)?.[1] ?? 0);
    const y = ty + Number(/\sy="([-\d.e]+)"/.exec(attrs)?.[1] ?? 0);
    const px = Number(/font-size:(\d+(?:\.\d+)?)px/.exec(attrs)?.[1] ?? 12);
    const w = estimateWidth(text, px);
    const anchor = /text-anchor="(\w+)"/.exec(attrs)?.[1] ?? 'start';
    const x0 = anchor === 'middle' ? x - w / 2 : anchor === 'end' ? x - w : x;
    const central = /dominant-baseline="central"/.test(attrs);
    const y0 = central ? y - px / 2 : y - px;
    boxes.push({ text, x0, x1: x0 + w, y0, y1: y0 + px });
  }
  return { boxes, rotated };
}

function overlaps(a: Box, b: Box): boolean {
  const tol = 0.5;
  return a.x0 < b.x1 - tol && b.x0 < a.x1 - tol && a.y0 < b.y1 - tol && b.y0 < a.y1 - tol;
}

function render(name: string, m: ChartModel, width: number, context: 'inline' | 'drawer' | 'mermaid' = 'inline') {
  const layout = layoutChart(m, { width, context, measure });
  expect(layout.renderer).toBe('echarts');
  const option = buildOptionFromModel(layout.model, layout, fallbackPalette('dark'), { reducedMotion: true });
  expect(option).not.toBeNull();
  const w = layout.form === 'pie' || layout.form === 'donut' ? layout.width : width;
  const chart = echarts.init(null as never, null as never, {
    renderer: 'svg',
    ssr: true,
    width: w,
    height: layout.height,
  });
  chart.setOption(option as never);
  const svg = chart.renderToSVGString();
  chart.dispose();
  if (OUT) {
    mkdirSync(OUT, { recursive: true });
    writeFileSync(join(OUT, `${name}-${width}.svg`), svg);
  }
  return { svg, layout, width: w };
}

function expectClean(name: string, m: ChartModel, width: number) {
  const { svg, layout, width: w } = render(name, m, width);
  const { boxes } = textBoxes(svg);
  expect(boxes.length).toBeGreaterThan(0);
  for (const b of boxes) {
    expect(b.x0, `${name}: "${b.text}" crosses the left edge`).toBeGreaterThanOrEqual(-0.5);
    expect(b.x1, `${name}: "${b.text}" crosses the right edge`).toBeLessThanOrEqual(w + 0.5);
    expect(b.y0, `${name}: "${b.text}" crosses the top`).toBeGreaterThanOrEqual(-0.5);
    expect(b.y1, `${name}: "${b.text}" crosses the bottom`).toBeLessThanOrEqual(layout.height + 0.5);
  }
  for (let i = 0; i < boxes.length; i++) {
    for (let j = i + 1; j < boxes.length; j++) {
      expect(overlaps(boxes[i], boxes[j]), `${name}@${width}: "${boxes[i].text}" overlaps "${boxes[j].text}"`).toBe(false);
    }
  }
  return { boxes, layout };
}

describe('server-side SVG: no text box overlaps another or the edge', () => {
  it('short-label columns at 317 and 710 px', () => {
    const m = model(spec({ show_values: true }), ['North', 'South', 'East', 'West'].map((x, i) => ({ x, y: 1200 + i * 731 })));
    expectClean('columns', m, 317);
    expectClean('columns', m, 710);
  });

  it('12 months over the complete range, two-line labels', () => {
    const rows = months(12)
      .filter((_, i) => i !== 4)
      .map((x, i) => ({ x, y: i * 13 + 2 }));
    const { layout } = expectClean('months-bars', model(spec(), rows), 710);
    expect(layout.form).toBe('timeColumns');
  });

  it('grouped columns with tip labels', () => {
    const rows = ['Q1', 'Q2', 'Q3'].map((x, i) => ({ x, a: 10 + i, b: 22 - i * 3 }));
    expectClean('grouped', model(spec({ y_keys: ['a', 'b'], show_values: true }), rows), 710);
  });

  it('negative columns keep labels clear of the bars', () => {
    const rows = ['Alpha', 'Beta', 'Gamma'].map((x, i) => ({ x, y: (i - 1) * 40 }));
    expectClean('negative', model(spec(), rows), 710);
  });

  it('time lines: 11 months on a phone, 95 irregular days at 317 and 710 px', () => {
    expectClean('months-line', model(spec({ type: 'line' }), months(11).map((x, i) => ({ x, y: i * 4 }))), 262);
    const daily = model(spec({ type: 'line' }), irregularDaily());
    expectClean('daily-line', daily, 317);
    expectClean('daily-line', daily, 710);
  });

  it('95 irregular days as bars open on a window with a slider', () => {
    const { layout } = expectClean('daily-bars', model(spec(), irregularDaily()), 710);
    expect(layout.zoom).not.toBeNull();
  });

  it('a 50-bin histogram at 262 px', () => {
    const rows = Array.from({ length: 50 }, (_, i) => ({ x: `${i * 10} - ${i * 10 + 10}`, y: (i * 7) % 13 }));
    expectClean('histogram', model(spec({ type: 'histogram' }), rows), 262);
  });

  it('a category line that numbers its ticks on a phone, and windows on desktop', () => {
    const rows = Array.from({ length: 14 }, (_, i) => ({ x: `Regional office number ${i + 1} (East)`, y: i * 3 }));
    const m = model(spec({ type: 'line' }), rows);
    const phone = expectClean('catline', m, 262);
    expect(phone.layout.numbered).toBe(true);
    expectClean('catline', m, 710);
  });

  it('a funnel with outside labels at 600 px', () => {
    const rows = ['Applied to the role', 'Screened by recruiter', 'Interviewed', 'Offer made', 'Placed'].map((x, i) => ({
      x,
      y: 1000 - i * 190,
    }));
    expectClean('funnel', model(spec({ type: 'funnel' }), rows), 600);
  });

  it('a Mermaid bar + line combo', () => {
    const parsed = parseMermaidChart(
      'xychart-beta\n title "Plan and actual"\n x-axis [Jan, Feb, Mar, Apr, May, Jun]\n bar [5, 6, 7, 8, 9, 10]\n line [4, 6, 8, 7, 9, 12]',
    )!;
    expectClean('combo', parsed.model, 680);
  });

  it('a donut draws no text on the canvas at all (the legend is HTML)', () => {
    const m = model(spec({ type: 'donut' }), [
      { x: 'A', y: 3 },
      { x: 'B', y: 1 },
    ]);
    const { svg } = render('donut', m, 710);
    expect(textBoxes(svg).boxes).toHaveLength(0);
  });
});

// ---------------------------------------------------------------------------
// Review fixes (2026-11-25): the cases the first SSR pass did not cover —
// zooming out, the PNG, negative tips, planned month ticks. Made-up data.
// ---------------------------------------------------------------------------

function svgOf(option: unknown, width: number, height: number): string {
  const chart = echarts.init(null as never, null as never, { renderer: 'svg', ssr: true, width, height });
  chart.setOption(option as never);
  const svg = chart.renderToSVGString();
  chart.dispose();
  return svg;
}

function overlapsIn(svg: string): string[] {
  const { boxes } = textBoxes(svg);
  const out: string[] = [];
  for (let i = 0; i < boxes.length; i++) {
    for (let j = i + 1; j < boxes.length; j++) {
      if (overlaps(boxes[i], boxes[j])) out.push(`${boxes[i].text} × ${boxes[j].text}`);
    }
  }
  return out;
}

const PLACES = [
  'Alder Harbor', 'Birch Iris', 'Cedar Point', 'Dune Valley', 'Elm Crossing', 'Fern Hollow', 'Glen Marsh',
  'Hazel Ridge', 'Iron Gate', 'Juniper Bay', 'Kestrel Moor', 'Linden Park', 'Maple Shore', 'Nettle Bank',
];
function longCats(n: number, chars: number): string[] {
  return Array.from({ length: n }, (_, i) => {
    const base = `${PLACES[i % PLACES.length]} ${Math.floor(i / PLACES.length) + 1} Office`;
    return base.padEnd(chars, ' x').slice(0, chars).trim();
  });
}

describe('review fixes: no overlap after zoom-out, in the PNG, on negative tips', () => {
  it('R2: a windowed category line cannot widen past its planned span (slider, inside zoom, dispatch)', () => {
    const m = model(spec({ type: 'line' }), longCats(40, 20).map((x, i) => ({ x, y: i })));
    const layout = layoutChart(m, { width: 710, context: 'inline', measure });
    expect(layout.zoom?.maxValueSpan).toBe(layout.zoom!.endValue - layout.zoom!.startValue);
    const option = buildOptionFromModel(layout.model, layout, fallbackPalette('dark'), {
      reducedMotion: true,
      zoom: { start: 0, end: 100 }, // the reader dragged both handles to the ends
    }) as { dataZoom: Array<Record<string, unknown>> };
    for (const dz of option.dataZoom) expect(dz.maxValueSpan).toBe(layout.zoom!.maxValueSpan);
    expect(overlapsIn(svgOf(option, 710, layout.height))).toEqual([]);
  });

  it('R2: the 26-name Mermaid bar + line combo stays readable fully zoomed out', () => {
    const names = Array.from({ length: 26 }, (_, i) => `Recruiter ${String.fromCharCode(65 + i)} Team ${(i % 3) + 1}`);
    const parsed = parseMermaidChart(
      `xychart-beta\n x-axis [${names.map((n) => `"${n}"`).join(', ')}]\n bar [${names.map((_, i) => 30 - i).join(', ')}]\n line [${names.map((_, i) => 25 - i).join(', ')}]`,
    )!;
    const layout = layoutChart(parsed.model, { width: 710, context: 'mermaid', measure });
    if (layout.zoom) {
      const option = buildOptionFromModel(layout.model, layout, fallbackPalette('dark'), {
        reducedMotion: true,
        zoom: { start: 0, end: 100 },
      });
      expect(overlapsIn(svgOf(option, 710, layout.height))).toEqual([]);
    }
  });

  it.each([
    [40, 20],
    [60, 22],
  ])('V1/V2: the PNG of a %i × %i-character category line has no overlap and carries the key', (n, chars) => {
    const labels = longCats(n, chars);
    const m = model(spec({ type: 'line', title: 'Offices' }), labels.map((x, i) => ({ x, y: i % 9 })));
    const layout = layoutChart(m, { width: 1200, context: 'inline', measure, forExport: true });
    expect(layout.zoom).toBeNull();
    const built = buildExportOption(layout.model, layout, fallbackPalette('light'), {
      title: 'Offices',
      measure,
      surface: '#f4f4f4',
    })!;
    const svg = svgOf(built.option, 1200, built.height);
    expect(overlapsIn(svg)).toEqual([]);
    const { boxes } = textBoxes(svg);
    for (const b of boxes) {
      expect(b.x1, `"${b.text}" crosses the right edge`).toBeLessThanOrEqual(1200.5);
      expect(b.y1, `"${b.text}" crosses the bottom`).toBeLessThanOrEqual(built.height + 0.5);
    }
    if (layout.numbered) {
      const text = boxes.map((b) => b.text).join('\n');
      for (const [i, label] of labels.entries()) expect(text).toContain(`${i + 1} · ${label.split(' ')[0]}`);
    }
  });

  it('V5: a negative column’s value label sits below its bar, clear of the category labels', () => {
    const rows = ['Alpha', 'Beta', 'Gamma'].map((x, i) => ({ x, y: (i - 1) * 2733 }));
    const { boxes, layout } = expectClean('negative-tips', model(spec({ show_values: true }), rows), 710);
    expect(layout.tipLabels).toBe(true);
    const zero = boxes.find((b) => b.text === '0' && b.x1 <= layout.grid.left);
    const neg = boxes.find((b) => b.text === '-2,733');
    expect(zero && neg).toBeTruthy();
    expect(neg!.y0).toBeGreaterThan(zero!.y1);
    const alpha = boxes.find((b) => b.text === 'Alpha')!;
    expect(neg!.y1).toBeLessThanOrEqual(alpha.y0);
  });

  it.each([262, 317, 600])('V4: a 6-month line labels its first and last month at %i px', (w) => {
    const rows = months(6).map((x, i) => ({ x, y: i * 4 + 1 }));
    const { boxes } = expectClean('months6-line', model(spec({ type: 'line' }), rows), w);
    const texts = boxes.map((b) => b.text);
    expect(texts).toContain('Jan 2025');
    expect(texts).toContain('Jun');
  });

  it.each([262, 317, 600])('V4: a monthly line with end labels keeps its planned month labels clear at %i px', (w) => {
    const rows = months(11).map((x, i) => ({ x, y: 1000 + i * 1234 }));
    const { boxes, layout } = expectClean('months11-line-tips', model(spec({ type: 'line', show_values: true }), rows), w);
    expect(layout.tipLabels).toBe(true);
    expect(boxes.map((b) => b.text)).toContain('Nov');
  });

  it.each([262, 317, 600])('V4: 12 monthly bars label December at %i px', (w) => {
    const rows = months(12).map((x, i) => ({ x, y: i + 2 }));
    const { boxes } = expectClean('months12-bars', model(spec(), rows), w);
    const texts = boxes.map((b) => b.text);
    expect(texts).toContain('Jan 2025');
    expect(texts).toContain('Dec');
  });

  it('V16: 12 monthly columns print the year once, not under every month', () => {
    const rows = months(12).map((x, i) => ({ x, y: i + 2 }));
    const { boxes, layout } = expectClean('months12-cols', model(spec(), rows), 710);
    expect(layout.form).toBe('timeColumns');
    const texts = boxes.map((b) => b.text);
    expect(texts).toContain('Feb');
    expect(texts).toContain('Dec');
    expect(texts.filter((t) => t.includes('2025'))).toHaveLength(1);
  });
});
