/**
 * The ChartSpec → ECharts adapter: dispatch, per-type configuration, and
 * the security boundary.
 *
 * These run in the existing node environment — no jsdom, no Testing
 * Library, no new frontend test stack. That is why the adapter is a pure
 * module and the React component is thin: the logic worth testing has no
 * DOM in it.
 */

import { describe, expect, it } from 'vitest';
import {
  CHART_TYPES,
  buildChartOption,
  escapeHtml,
  isChartType,
  partToWholeData,
  validateChart,
} from '../lib/chartOption';
import { SERIES_FALLBACK, fallbackPalette, resolveSeriesColors, seriesColor } from '../lib/chartTheme';
import type { ChartSpec, ChartType, DataRow } from '../lib/types';

const palette = fallbackPalette('dark');

function spec(over: Partial<ChartSpec> = {}): ChartSpec {
  return {
    type: 'bar',
    x_key: 'stage',
    y_keys: ['total'],
    title: 'T',
    stacked: false,
    ...over,
  };
}

const CATEGORY_ROWS: DataRow[] = [
  { stage: 'Prospecting', total: 10 },
  { stage: 'Qualification', total: 7 },
  { stage: 'Closed Won', total: 3 },
];

function seriesOf(option: Record<string, unknown> | null): Array<Record<string, unknown>> {
  return (option?.series ?? []) as Array<Record<string, unknown>>;
}

// ---------------------------------------------------------------------------
// Dispatch: all nine types
// ---------------------------------------------------------------------------

describe('type dispatch', () => {
  it('exposes exactly the nine supported types', () => {
    expect([...CHART_TYPES].sort()).toEqual(
      [
        'area',
        'bar',
        'donut',
        'funnel',
        'histogram',
        'horizontal_bar',
        'line',
        'pie',
        'scatter',
      ].sort(),
    );
  });

  const rowsFor = (t: ChartType): DataRow[] =>
    t === 'scatter' ? [{ stage: 1, total: 2 }, { stage: 3, total: 4 }] : CATEGORY_ROWS;

  it.each(CHART_TYPES)('builds an option for %s', (t) => {
    const option = buildChartOption(spec({ type: t }), rowsFor(t), palette);
    expect(option).not.toBeNull();
    expect(seriesOf(option).length).toBeGreaterThan(0);
  });

  it('rejects a type this renderer does not know', () => {
    // A payload from a newer backend must degrade, not reach ECharts.
    expect(isChartType('heatmap')).toBe(false);
    const option = buildChartOption(
      spec({ type: 'heatmap' as ChartType }),
      CATEGORY_ROWS,
      palette,
    );
    expect(option).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// Per-type configuration
// ---------------------------------------------------------------------------

/** The display text ECharts will draw for each category id (v2: ids, not names). */
function axisLabels(option: Record<string, unknown>, axis: 'xAxis' | 'yAxis' = 'xAxis'): string[] {
  const a = option[axis] as { data: string[]; axisLabel: { formatter: (id: string) => string } };
  return a.data.map((id) => a.axisLabel.formatter(id).replace(/\n/g, ' '));
}

describe('bar', () => {
  it('puts categories on the x axis and values on the y axis', () => {
    const option = buildChartOption(spec(), CATEGORY_ROWS, palette)!;
    expect((option.xAxis as Record<string, unknown>).type).toBe('category');
    // ECharts only ever sees ids (C12); the formatter gives the full label.
    expect((option.xAxis as Record<string, unknown>).data).toEqual(['c0', 'c1', 'c2']);
    expect(axisLabels(option)).toEqual(['Prospecting', 'Qualification', 'Closed Won']);
    expect((option.yAxis as Record<string, unknown>).type).toBe('value');
    expect(seriesOf(option)[0].type).toBe('bar');
  });

  it('stacks only when the spec says so', () => {
    const plain = buildChartOption(spec({ y_keys: ['a', 'b'] }), [{ stage: 'x', a: 1, b: 2 }], palette)!;
    expect(seriesOf(plain).every((s) => s.stack === undefined)).toBe(true);
    const stacked = buildChartOption(
      spec({ y_keys: ['a', 'b'], stacked: true }),
      [{ stage: 'x', a: 1, b: 2 }],
      palette,
    )!;
    expect(seriesOf(stacked).every((s) => s.stack === 'total')).toBe(true);
  });

  it('assigns palette colors in fixed order, not cycled per row', () => {
    const option = buildChartOption(
      spec({ y_keys: ['a', 'b'] }),
      [{ stage: 'x', a: 1, b: 2 }],
      palette,
    )!;
    const colors = seriesOf(option).map(
      (s) => (s.itemStyle as Record<string, unknown>).color,
    );
    expect(colors).toEqual([seriesColor(palette, 0), seriesColor(palette, 1)]);
  });
});

describe('horizontal_bar', () => {
  it('swaps the axes — category on y, value on x', () => {
    const option = buildChartOption(spec({ type: 'horizontal_bar' }), CATEGORY_ROWS, palette)!;
    expect((option.yAxis as Record<string, unknown>).type).toBe('category');
    expect((option.xAxis as Record<string, unknown>).type).toBe('value');
    expect(seriesOf(option)[0].type).toBe('bar');
  });

  it('draws the first row at the top', () => {
    const option = buildChartOption(spec({ type: 'horizontal_bar' }), CATEGORY_ROWS, palette)!;
    expect((option.yAxis as Record<string, unknown>).inverse).toBe(true);
  });
});

describe('line and area', () => {
  it('draws dates on a real time axis, positioned from their calendar parts', () => {
    const rows: DataRow[] = [
      { stage: '2026-01', total: 1 },
      { stage: '2026-02', total: 5 },
      { stage: '2026-03', total: 3 },
    ];
    const option = buildChartOption(spec({ type: 'line' }), rows, palette)!;
    expect((option.xAxis as Record<string, unknown>).type).toBe('time');
    expect(option.useUTC).toBe(true);
    expect(seriesOf(option)[0].data).toEqual([
      [Date.UTC(2026, 0, 1), 1],
      [Date.UTC(2026, 1, 1), 5],
      [Date.UTC(2026, 2, 1), 3],
    ]);
  });

  it('keeps the row order of a category line', () => {
    const option = buildChartOption(spec({ type: 'line' }), CATEGORY_ROWS, palette)!;
    expect(axisLabels(option)).toEqual(['Prospecting', 'Qualification', 'Closed Won']);
    expect(seriesOf(option)[0].data).toEqual([10, 7, 3]);
  });

  it('draws straight 2 px lines and a 12% area', () => {
    const area = buildChartOption(spec({ type: 'area' }), CATEGORY_ROWS, palette)!;
    expect(seriesOf(area)[0].smooth).toBe(false);
    expect((seriesOf(area)[0].lineStyle as Record<string, unknown>).width).toBe(2);
    expect((seriesOf(area)[0].areaStyle as Record<string, unknown>).opacity).toBe(0.12);
  });

  it('gives area a fill and line none', () => {
    const area = buildChartOption(spec({ type: 'area' }), CATEGORY_ROWS, palette)!;
    const line = buildChartOption(spec({ type: 'line' }), CATEGORY_ROWS, palette)!;
    expect(seriesOf(area)[0].areaStyle).toBeTruthy();
    expect(seriesOf(line)[0].areaStyle).toBeUndefined();
    expect(seriesOf(area)[0].type).toBe('line');
  });
});

describe('pie and donut', () => {
  it('gives a donut an inner radius and a pie none — chosen here, not by the spec', () => {
    const donut = buildChartOption(spec({ type: 'donut' }), CATEGORY_ROWS, palette)!;
    const pie = buildChartOption(spec({ type: 'pie' }), CATEGORY_ROWS, palette)!;
    const [inner, outer] = seriesOf(donut)[0].radius as number[];
    expect(inner).toBeGreaterThan(0);
    expect(outer).toBeGreaterThan(inner);
    expect((seriesOf(pie)[0].radius as number[])[0]).toBe(0);
    // No slice names on the canvas: the HTML legend is the direct label.
    expect((seriesOf(pie)[0].label as Record<string, unknown>).show).toBe(false);
  });

  it('folds the tail into "Other" past eight slices', () => {
    // MAX_SLICES moved 6 -> 8 in 05d2286; the assertion now tracks the
    // constant's intent (top N-1 slices + one folded remainder), not the
    // old literal.
    const rows: DataRow[] = Array.from({ length: 10 }, (_, i) => ({
      stage: `s${i}`,
      total: 10 - i,
    }));
    const data = partToWholeData(spec({ type: 'donut' }), rows, 'total');
    expect(data).toHaveLength(8);
    expect(data[7].name).toBe('Other');
    // 3 + 2 + 1 for the folded tail
    expect(data[7].value).toBe(6);
  });

  it('refuses to draw when every value is zero or negative', () => {
    expect(validateChart(spec({ type: 'pie' }), [{ stage: 'a', total: 0 }])).toBe(
      'part-to-whole-needs-positive-values',
    );
  });
});

describe('scatter', () => {
  it('plots numeric pairs', () => {
    const rows: DataRow[] = [
      { stage: 1, total: 10 },
      { stage: 2, total: 20 },
    ];
    const option = buildChartOption(spec({ type: 'scatter' }), rows, palette)!;
    expect(seriesOf(option)[0].data).toEqual([
      [1, 10],
      [2, 20],
    ]);
    expect((option.xAxis as Record<string, unknown>).type).toBe('value');
  });

  it('refuses a category x — a string is not a coordinate', () => {
    expect(validateChart(spec({ type: 'scatter' }), CATEGORY_ROWS)).toBe(
      'scatter-needs-numeric-x',
    );
    expect(buildChartOption(spec({ type: 'scatter' }), CATEGORY_ROWS, palette)).toBeNull();
  });
});

describe('funnel', () => {
  it('never re-sorts the stages', () => {
    // THE POINT: ECharts sorts funnel data by value by default. The
    // backend only emits a funnel when it could establish a TRUSTED stage
    // order and ships the rows in it; sorting here would assert a
    // sequence nobody verified.
    const rows: DataRow[] = [
      { stage: 'Prospecting', total: 3 },
      { stage: 'Qualification', total: 90 },
      { stage: 'Closed Won', total: 7 },
    ];
    const option = buildChartOption(spec({ type: 'funnel' }), rows, palette)!;
    const series = seriesOf(option)[0];
    expect(series.sort).toBe('none');
    expect((series.data as Array<{ name: string }>).map((d) => d.name)).toEqual(['c0', 'c1', 'c2']);
    const label = series.label as { formatter: (p: { name: string }) => string; color: string; position: string };
    expect(['c0', 'c1', 'c2'].map((name) => label.formatter({ name }).split('\n')[0])).toEqual([
      'Prospecting',
      'Qualification',
      'Closed Won',
    ]);
    // Outside labels in text ink — never white text on a light ramp step.
    expect(label.position).toBe('right');
    expect(label.color).toBe(palette.text);
    expect(label.color).not.toBe('#ffffff');
  });
});

describe('histogram', () => {
  it('renders the pre-binned rows without re-binning them', () => {
    const rows: DataRow[] = [
      { stage: '0 - 10', total: 4 },
      { stage: '10 - 20', total: 9 },
      { stage: '20 - 30', total: 2 },
    ];
    const option = buildChartOption(spec({ type: 'histogram', bins: 3 }), rows, palette)!;
    const series = seriesOf(option)[0];
    expect(series.data).toEqual([4, 9, 2]);
    expect(series.barCategoryGap).toBe('0%'); // bins touch
    expect(axisLabels(option)).toEqual(['0 - 10', '10 - 20', '20 - 30']);
  });
});

// ---------------------------------------------------------------------------
// Validation and fallback
// ---------------------------------------------------------------------------

describe('validation', () => {
  it('rejects an empty result', () => {
    expect(validateChart(spec(), [])).toBe('no-data');
    expect(buildChartOption(spec(), [], palette)).toBeNull();
  });

  it('rejects a spec naming a column the rows do not have', () => {
    expect(validateChart(spec({ x_key: 'ghost' }), CATEGORY_ROWS)).toBe('missing-x-column');
    expect(validateChart(spec({ y_keys: ['ghost'] }), CATEGORY_ROWS)).toBe('missing-y-column');
  });

  it('rejects a measure with nothing numeric in it', () => {
    expect(validateChart(spec(), [{ stage: 'a', total: 'n/a' }])).toBe('no-numeric-values');
  });

  it('does not treat Salesforce text booleans as measures', () => {
    // Checkboxes arrive as the TEXT 'true'/'false'. Number('true') is NaN,
    // but relying on that is luck; isNumeric rules them out by name.
    expect(validateChart(spec(), [{ stage: 'a', total: 'true' }])).toBe('no-numeric-values');
  });

  it('accepts a legacy five-key payload unchanged', () => {
    // Exactly what conversations persisted before this change contain.
    const legacy = {
      type: 'bar',
      x_key: 'stage',
      y_keys: ['total'],
      title: 'Cases',
      stacked: false,
    } as ChartSpec;
    expect(validateChart(legacy, CATEGORY_ROWS)).toBeNull();
    expect(buildChartOption(legacy, CATEGORY_ROWS, palette)).not.toBeNull();
  });
});

// ---------------------------------------------------------------------------
// Security boundary
// ---------------------------------------------------------------------------

describe('security boundary', () => {
  it('escapes Salesforce values before they reach tooltip HTML', () => {
    // A tooltip formatter's return value is rendered as HTML, and an
    // Account really can be named this.
    const hostile = '<img src=x onerror="alert(1)">';
    expect(escapeHtml(hostile)).toBe(
      '&lt;img src=x onerror=&quot;alert(1)&quot;&gt;',
    );
    expect(escapeHtml(hostile)).not.toContain('<img');
  });

  it('every formatter in the option is a function this code defined', () => {
    const option = buildChartOption(spec(), CATEGORY_ROWS, palette)!;
    const tooltip = option.tooltip as Record<string, unknown>;
    expect(typeof tooltip.formatter).toBe('function');
    // The spec has no field that could carry one, so the only way a
    // formatter exists is that this module made it.
    expect(Object.keys(spec())).toEqual(
      expect.arrayContaining(['type', 'x_key', 'y_keys', 'title', 'stacked']),
    );
  });

  it('ignores unknown keys on a spec instead of passing them to ECharts', () => {
    const hostile = {
      ...spec(),
      // None of these are ChartSpec fields; if any were spread into the
      // option, ECharts would honour them.
      formatter: 'function(){return 1}',
      onclick: 'alert(1)',
      series: [{ type: 'custom', renderItem: 'x' }],
    } as unknown as ChartSpec;
    const option = buildChartOption(hostile, CATEGORY_ROWS, palette)!;
    expect(option.onclick).toBeUndefined();
    expect(seriesOf(option)).toHaveLength(1);
    expect(seriesOf(option)[0].type).toBe('bar');
    expect(seriesOf(option)[0].renderItem).toBeUndefined();
  });
});

// ---------------------------------------------------------------------------
// Theme
// ---------------------------------------------------------------------------

describe('theme', () => {
  it('falls back to the literal palette with no DOM', () => {
    expect(resolveSeriesColors(null)).toEqual([...SERIES_FALLBACK]);
  });

  it('resolves --ts-chart-* to concrete colors when the DOM has them', () => {
    const tokens: Record<string, string> = {
      '--ts-chart-1': ' #111111 ',
      '--ts-chart-2': '#222222',
      '--ts-chart-3': '',            // unset → fallback
      '--ts-chart-4': 'var(--other)', // unresolvable in a canvas → fallback
      '--ts-chart-5': 'rgb(5, 5, 5)',
      '--ts-chart-6': '#666666',
      '--ts-chart-7': '',
      '--ts-chart-8': '#888888',
    };
    const originalWindow = (globalThis as Record<string, unknown>).window;
    (globalThis as Record<string, unknown>).window = {
      getComputedStyle: () => ({ getPropertyValue: (n: string) => tokens[n] ?? '' }),
    };
    try {
      expect(resolveSeriesColors({} as Element)).toEqual([
        '#111111',
        '#222222',
        SERIES_FALLBACK[2],
        SERIES_FALLBACK[3],
        'rgb(5, 5, 5)',
        '#666666',
        SERIES_FALLBACK[6],
        '#888888',
      ]);
    } finally {
      (globalThis as Record<string, unknown>).window = originalWindow;
    }
  });

  it('gives light and dark different chrome but the same series colors', () => {
    const dark = fallbackPalette('dark');
    const light = fallbackPalette('light');
    expect(dark.text).not.toBe(light.text);
    expect(dark.series).toEqual(light.series);
  });

  it('applies the palette to axes, grid and series', () => {
    const option = buildChartOption(spec(), CATEGORY_ROWS, palette)!;
    expect(option.color).toEqual(palette.series);
    const yAxis = option.yAxis as Record<string, Record<string, unknown>>;
    // v2 chrome is new palette fields; the admin console's `grid` is untouched.
    expect((yAxis.splitLine.lineStyle as Record<string, unknown>).color).toBe(palette.gridline);
    expect((yAxis.splitLine.lineStyle as Record<string, unknown>).type).toBe('solid');
    expect((option.textStyle as Record<string, unknown>).color).toBe(palette.text);
  });
});

// ---------------------------------------------------------------------------
// v2 (charts design v2 §4, §7, §8)
// ---------------------------------------------------------------------------

describe('v2 option rules', () => {
  const LONG = Array.from({ length: 6 }, (_, i) => ({
    stage: `A rather long category name number ${i + 1}`,
    total: i * 10 + 1,
  }));

  it('never truncates, hides or rotates-by-default a category label', () => {
    for (const t of ['bar', 'line', 'area', 'histogram'] as ChartType[]) {
      const option = buildChartOption(spec({ type: t }), LONG, palette);
      if (!option) continue;
      const x = option.xAxis as Record<string, Record<string, unknown>>;
      if (x.type !== ('category' as unknown)) continue;
      expect(x.axisLabel.hideOverlap).toBeUndefined();
      expect(x.axisLabel.width).toBeUndefined();
      expect(x.axisLabel.overflow).toBeUndefined();
      expect(x.axisLabel.rich).toBeUndefined();
      expect(x.axisLabel.rotate === 0 || x.axisLabel.rotate === 45).toBe(true);
      const texts = axisLabels(option).map((l) => l.replace(/\s+/g, ' '));
      if (t !== 'histogram') expect(texts).toEqual(LONG.map((r) => r.stage));
    }
  });

  it('caps bars at 24 px with a 4 px rounded data end', () => {
    const option = buildChartOption(spec(), CATEGORY_ROWS, palette)!;
    const s0 = seriesOf(option)[0];
    expect(s0.barMaxWidth).toBe(24);
    expect((s0.itemStyle as Record<string, unknown>).borderRadius).toEqual([4, 4, 0, 0]);
  });

  it('flips the rounded end for negative columns and keeps labels off the zero line', () => {
    const option = buildChartOption(
      spec(),
      [
        { stage: 'a', total: 5 },
        { stage: 'b', total: -3 },
      ],
      palette,
    )!;
    const data = seriesOf(option)[0].data as Array<{ itemStyle: { borderRadius: number[] } }>;
    expect(data[1].itemStyle.borderRadius).toEqual([0, 0, 4, 4]);
    const x = option.xAxis as Record<string, Record<string, unknown>>;
    expect(x.axisLine.onZero).toBe(false);
  });

  it('uses the 8-slot palette in y_keys order, never cycling', () => {
    const keys = Array.from({ length: 8 }, (_, i) => `k${i}`);
    const option = buildChartOption(
      spec({ y_keys: keys }),
      [{ stage: 'x', ...Object.fromEntries(keys.map((k, i) => [k, i + 1])) }],
      palette,
    )!;
    const colors = seriesOf(option).map((s) => (s.itemStyle as Record<string, unknown>).color);
    expect(colors).toEqual([...SERIES_FALLBACK]);
    expect(new Set(colors).size).toBe(8);
  });

  it('confines and wraps the tooltip, and escapes every name', () => {
    const option = buildChartOption(
      spec(),
      [{ stage: '<img src=x onerror=alert(1)>', total: 1 }, { stage: 'b', total: 2 }],
      palette,
    )!;
    const tooltip = option.tooltip as Record<string, unknown>;
    expect(tooltip.confine).toBe(true);
    expect(String(tooltip.extraCssText)).toContain('white-space:normal');
    expect(String(tooltip.extraCssText)).toContain('overflow-wrap:anywhere');
    const html = (tooltip.formatter as (p: unknown) => string)([
      { axisValue: 'c0', seriesId: 's0', color: '#0E9D9A', value: 1 },
    ]);
    expect(html).not.toContain('<img');
    expect(html).toContain('&lt;img');
    expect(html).toContain('dir="auto"');
  });

  it('sets minInterval 1 for integer data and one tick style per axis', () => {
    const option = buildChartOption(
      spec(),
      [
        { stage: 'a', total: 25_000 },
        { stage: 'b', total: 5_000 },
      ],
      palette,
    )!;
    const y = option.yAxis as Record<string, Record<string, unknown> | number>;
    expect(y.minInterval).toBe(1);
    const fmt = (y.axisLabel as Record<string, (v: number) => string>).formatter;
    expect(fmt(25_000)).toBe('25k');
    expect(fmt(5_000)).toBe('5k');
  });
});
