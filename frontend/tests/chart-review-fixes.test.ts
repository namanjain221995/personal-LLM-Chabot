/**
 * Option-level checks for the charts review fixes (2026-11-25): what the
 * ECharts option carries for zoom limits, planned month ticks, negative tip
 * labels, the hover cue and the funnel ramp. Geometry is checked in
 * tests/chart-ssr.test.ts. Made-up data.
 */

import { describe, expect, it } from 'vitest';
import { modelFromSpec, type ChartModel } from '../lib/chartModel';
import { layoutChart } from '../lib/chartLayout';
import { buildOptionFromModel } from '../lib/chartOption';
import { fallbackPalette, surfaceFor } from '../lib/chartTheme';
import { estimateWidth } from '../lib/chartText';
import { parseMermaidChart } from '../lib/mermaidChart';
import type { ChartSpec, DataRow } from '../lib/types';

const measure = (t: string) => estimateWidth(t);
function model(over: Partial<ChartSpec>, rows: DataRow[]): ChartModel {
  const r = modelFromSpec({ type: 'bar', x_key: 'x', y_keys: ['y'], title: 'T', stacked: false, ...over }, rows);
  if (!r.model) throw new Error(String(r.problem));
  return r.model;
}
type Opt = Record<string, unknown>;
const series = (o: Opt | null) => (o?.series as Opt[]) ?? [];

function lum(hex: string): number {
  const c = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255);
  const l = c.map((v) => (v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4));
  return 0.2126 * l[0] + 0.7152 * l[1] + 0.0722 * l[2];
}
function contrast(a: string, b: string): number {
  const [x, y] = [lum(a), lum(b)];
  return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05);
}

describe('review fixes: option contents', () => {
  it('V4: a monthly time axis carries planned labels through customValues', () => {
    const rows = Array.from({ length: 6 }, (_, i) => ({ x: `2025-0${i + 1}`, y: i }));
    const m = model({ type: 'line' }, rows);
    const layout = layoutChart(m, { width: 317, context: 'inline', measure });
    const opt = buildOptionFromModel(layout.model, layout, fallbackPalette('dark'))!;
    const label = (opt.xAxis as Opt).axisLabel as Opt & { formatter: (v: number, i: number) => string };
    const ticks = label.customValues as number[];
    expect(ticks[0]).toBe(Date.UTC(2025, 0, 1));
    expect(ticks[ticks.length - 1]).toBe(Date.UTC(2025, 5, 1));
    expect(label.formatter(ticks[0], 0)).toBe('Jan 2025');
    expect(label.formatter(ticks[ticks.length - 1], ticks.length - 1)).toBe('Jun');
  });

  it('V5: a negative bar’s value label goes below its end, with room kept under the axis', () => {
    const m = model({ show_values: true }, ['A', 'B', 'C'].map((x, i) => ({ x, y: (i - 1) * 40 })));
    const layout = layoutChart(m, { width: 710, context: 'inline', measure });
    const opt = buildOptionFromModel(layout.model, layout, fallbackPalette('dark'))!;
    const data = series(opt)[0].data as Array<{ value: number; label?: { position: string } }>;
    expect(data[0].label?.position).toBe('bottom');
    expect(data[2].label).toBeUndefined();
    expect((opt.yAxis as Opt).boundaryGap).toEqual(['10%', 0]);
  });

  it('A15: the hovered or keyboard-current bar gets a visible ink ring', () => {
    const m = model({}, ['A', 'B', 'C'].map((x, i) => ({ x, y: i + 1 })));
    const layout = layoutChart(m, { width: 710, context: 'inline', measure });
    const palette = fallbackPalette('dark');
    const opt = buildOptionFromModel(layout.model, layout, palette)!;
    const emphasis = series(opt)[0].emphasis as { itemStyle: { borderColor: string; borderWidth: number } };
    expect(emphasis.itemStyle).toEqual({ borderColor: palette.text, borderWidth: 2 });
  });

  it('V12: a Mermaid combo of 8 months shows every month at 317 px, and every point at 262 px', () => {
    const parsed = parseMermaidChart(
      'xychart-beta\n x-axis [Jan, Feb, Mar, Apr, May, Jun, Jul, Aug]\n bar [5, 6, 7, 8, 9, 10, 11, 12]\n line [4, 6, 8, 7, 9, 12, 10, 11]',
    )!;
    // Real IBM Plex Sans 400 advances at 12 px (from the review's metrics):
    // "May" is the widest at 22.1 px.
    const PLEX: Record<string, number> = { Jan: 19.34, Feb: 20.26, Mar: 20.56, Apr: 19.06, May: 22.14, Jun: 19.75, Jul: 16.2, Aug: 20.84 };
    const plex = (t: string) => PLEX[t] ?? estimateWidth(t);
    const phone = layoutChart(parsed.model, { width: 317, context: 'mermaid', measure: plex });
    expect(phone.form).toBe('combo');
    expect(phone.zoom).toBeNull();
    expect(phone.numbered).toBe(false);
    // At 262 px "May" needs 24 px of a 21.5 px label room: all 8 points stay
    // in view with numbered ticks rather than opening on a 5-month window.
    const small = layoutChart(parsed.model, { width: 262, context: 'mermaid', measure: plex });
    expect(small.zoom).toBeNull();
  });

  it.each(['dark', 'light'] as const)('V11/A16: every %s funnel stage clears 3:1 on the card and the drawer', (theme) => {
    const palette = fallbackPalette(theme);
    for (const ctx of ['inline', 'drawer'] as const) {
      const ground = surfaceFor(theme, ctx);
      for (const c of palette.ordinal) expect(contrast(c, ground), `${c} on ${ground}`).toBeGreaterThanOrEqual(3);
    }
  });
});
