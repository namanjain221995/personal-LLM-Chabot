/**
 * Form chooser + layout planner (charts design v2 §3-§5), one fixture per live
 * shape (made-up data) at 262, 317 and 710 px, plus the invariants that make
 * "never truncated, never overlapping" true by construction.
 */

import { describe, expect, it } from 'vitest';
import { modelFromSpec, type ChartModel } from '../lib/chartModel';
import {
  axisLabelModel,
  chooseForm,
  describeChart,
  histogramLabelled,
  layoutChart,
  summaryText,
  type ChartLayout,
  type LayoutCtx,
} from '../lib/chartLayout';
import { buildOptionFromModel } from '../lib/chartOption';
import { fallbackPalette } from '../lib/chartTheme';
import { graphemes } from '../lib/chartText';
import { parseMermaidChart } from '../lib/mermaidChart';
import type { ChartSpec, DataRow } from '../lib/types';

const WIDE = /[\u{1F000}-\u{1FAFF}\u{2E80}-\u{A4CF}]/u;
const measure = (t: string) => graphemes(t).reduce((w, g) => w + (WIDE.test(g) ? 19 : 7), 0);
const ctx = (width: number, over: Partial<LayoutCtx> = {}): LayoutCtx => ({
  width,
  context: 'inline',
  measure,
  ...over,
});
const WIDTHS = [262, 317, 710];
const DAY = 86_400_000;

function spec(over: Partial<ChartSpec> = {}): ChartSpec {
  return { type: 'bar', x_key: 'x', y_keys: ['y'], title: 'T', stacked: false, ...over };
}
function model(s: ChartSpec, rows: DataRow[]): ChartModel {
  const r = modelFromSpec(s, rows);
  if (!r.model) throw new Error(String(r.problem));
  return r.model;
}

const NAMES = [
  'Owner Alpha', 'Owner Bravo Charlie', 'Delta Echo', 'Foxtrot Golf Hotel India',
  'Juliet', 'Kilo Lima Mike', 'November Oscar', 'Papa Quebec Romeo Sierra',
  'Tango', 'Uniform Victor', 'Whiskey X-ray Yankee', 'Zulu Ann',
  'Bea Carter-Long', 'Cy Dunn', 'Dee Evans Ford', 'Eli Fox', 'Fay Grant Hill',
  'Gus Hale', 'Hal Ives Jones Kerr', 'Ida Jay', 'Jo King', 'Kay Lee Moss Noel',
  'Lou Mars', 'Mo Nash Owens', 'Ned Ott', 'Oz Pike Quinn Rhodes Smith',
];

/** The owner's complaint shape: 26 bars, labels up to 26 characters. */
const RANKING: DataRow[] = NAMES.map((x, i) => ({ x, y: 3091 - i * 110 }));

function months(n: number, from = [2025, 0]): string[] {
  return Array.from({ length: n }, (_, i) => {
    const d = new Date(Date.UTC(from[0], from[1] + i, 1));
    return d.toISOString().slice(0, 7);
  });
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

function catAxis(option: Record<string, unknown> | null): Record<string, unknown> | undefined {
  const x = option?.xAxis as Record<string, unknown> | undefined;
  return x?.type === 'category' ? x : undefined;
}

/** The invariants every canvas layout must hold. */
function checkInvariants(layout: ChartLayout, width: number) {
  const { model } = layout;
  if (layout.renderer !== 'echarts') return;
  // 1. No label shortened: the broken text, rejoined, is the full label.
  if (!layout.numbered && Object.keys(layout.catText).length) {
    // A monthly column axis prints "Jan 2025, Feb … Jan 2026" (review V16);
    // everything else prints the full label.
    const printed = axisLabelModel(model, layout.form);
    for (const c of printed.x.cats) {
      const text = layout.catText[c.id];
      // Every character survives (a break after a hyphen adds no space).
      expect(text.replace(/\s+/g, '')).toBe(c.label.replace(/\s+/g, ''));
      if (layout.rotate === 0 && layout.form !== 'histogram') {
        const band = (width - layout.grid.left - layout.grid.right) / (layout.zoom ? layout.zoom.endValue - layout.zoom.startValue + 1 : model.x.cats.length);
        for (const line of text.split('\n')) expect(measure(line)).toBeLessThanOrEqual(band);
      }
    }
  }
  // 2. The height includes the plot and the x band.
  const maxLines = Math.max(1, ...layout.catLines);
  expect(layout.height).toBeGreaterThanOrEqual(layout.grid.top + layout.plotHeight);
  if (layout.rotate === 0 && layout.catLines.length) {
    expect(layout.grid.bottom).toBeGreaterThanOrEqual(maxLines * layout.lineHeight);
  }
  // 3. The option never asks ECharts to cut, hide, skip or wrap a category.
  const option = buildOptionFromModel(model, layout, fallbackPalette('dark'));
  expect(option).not.toBeNull();
  const axis = catAxis(option);
  if (axis) {
    const label = axis.axisLabel as Record<string, unknown>;
    expect(label.width).toBeUndefined();
    expect(label.overflow).toBeUndefined();
    expect(label.rich).toBeUndefined();
    expect(label.hideOverlap).toBeUndefined();
    if (layout.form !== 'histogram') expect(label.interval).toBe(0);
    expect((axis.axisLine as Record<string, unknown>).onZero).toBe(false);
  }
  // 4. The structure does not change for a ±8 px width change.
  const plus = layoutChart(model, ctx(width + 8));
  const minus = layoutChart(model, ctx(width - 8));
  if (plus.form === layout.form && minus.form === layout.form) {
    expect([plus.structuralKey, minus.structuralKey].filter((k) => k === layout.structuralKey).length).toBeGreaterThanOrEqual(1);
  }
}

describe('the complaint shape: 26 long names', () => {
  it.each(WIDTHS)('becomes the HTML bar list at %i px', (w) => {
    const layout = layoutChart(model(spec(), RANKING), ctx(w));
    expect(layout.renderer).toBe('barlist');
    // Review V8: never a scroller taller than 60% of a phone screen.
    expect(layout.barList?.cap).toBe(w < 480 ? 'min(480px, 60vh)' : '520px');
  });

  it('converts from the Mermaid xychart too', () => {
    const code = [
      'xychart-beta',
      '  title "Records by owner"',
      `  x-axis [${NAMES.join(', ')}]`,
      '  y-axis "Count" 0 --> 3500',
      `  bar [${RANKING.map((r) => r.y).join(', ')}]`,
    ].join('\n');
    const parsed = parseMermaidChart(code)!;
    const layout = layoutChart(parsed.model, ctx(710, { context: 'mermaid' }));
    expect(layout.renderer).toBe('barlist');
    expect(layout.barList?.cap).toBe('520px');
  });
});

describe('bar list forms', () => {
  it('horizontal_bar is always a bar list', () => {
    const rows = RANKING.slice(0, 13);
    for (const w of WIDTHS) {
      expect(layoutChart(model(spec({ type: 'horizontal_bar' }), rows), ctx(w)).renderer).toBe('barlist');
    }
  });

  it('a 200-character label goes to the bar list; no other row is affected', () => {
    const rows = [{ x: 'L'.repeat(40) + ' ' + 'word '.repeat(32).trim(), y: 5 }, { x: 'Short', y: 3 }];
    expect(rows[0].x.length).toBeGreaterThanOrEqual(200);
    const layout = layoutChart(model(spec(), rows), ctx(710));
    expect(layout.renderer).toBe('barlist');
  });

  it('a pie with 9 parts is a ranked bar list with a note', () => {
    const rows = Array.from({ length: 9 }, (_, i) => ({ x: `Part ${i + 1}`, y: 9 - i }));
    const layout = layoutChart(model(spec({ type: 'pie' }), rows), ctx(710));
    expect(layout.renderer).toBe('barlist');
    expect(layout.barList?.ordered).toBe(true);
    expect(layout.notes.join(' ')).toMatch(/9 parts are easier to compare as bars/);
  });

  it('a funnel is a stage-order bar list at 400 px and a funnel at 600 px', () => {
    const rows = ['Applied', 'Screened', 'Interviewed', 'Offered', 'Placed'].map((x, i) => ({ x, y: 100 - i * 20 }));
    const narrow = layoutChart(model(spec({ type: 'funnel' }), rows), ctx(400));
    expect(narrow.renderer).toBe('barlist');
    expect(narrow.barList?.percentOfFirst).toBe(true);
    expect(narrow.barList?.ordered).toBe(false);
    const wide = layoutChart(model(spec({ type: 'funnel' }), rows), ctx(600));
    expect(wide.form).toBe('funnel');
    expect(wide.funnel?.labelText.c0).toContain('Applied');
    expect(wide.funnel!.labelWidth).toBeLessThanOrEqual(600 * 0.3 + 60);
  });
});

describe('columns', () => {
  it('keeps a few short labels as vertical columns', () => {
    const rows = ['North', 'South', 'East', 'West'].map((x, i) => ({ x, y: i + 1 }));
    for (const w of WIDTHS) {
      const layout = layoutChart(model(spec(), rows), ctx(w));
      expect(layout.form).toBe('columns');
      checkInvariants(layout, w);
    }
  });

  it('12 monthly bars draw over the complete range at 710 px, one labelled slot per month', () => {
    const ms = months(12).filter((_, i) => i !== 4);
    const rows = ms.map((x, i) => ({ x, y: i + 1 }));
    const layout = layoutChart(model(spec(), rows), ctx(710));
    expect(layout.form).toBe('timeColumns');
    expect(layout.model.x.cats).toHaveLength(12);
    expect(layout.model.x.cats[4].present).toBe(false);
    checkInvariants(layout, 710);
  });

  it('the same 12 months on a phone use a time axis instead of squeezing', () => {
    const rows = months(12).map((x, i) => ({ x, y: i + 1 }));
    const layout = layoutChart(model(spec(), rows), ctx(262));
    expect(['timeBars', 'timeColumns']).toContain(layout.form);
    checkInvariants(layout, 262);
  });

  it('8 stacked series and negative values keep the category axis off the zero line', () => {
    const keys = Array.from({ length: 8 }, (_, i) => `k${i}`);
    const rows = ['Q1', 'Q2', 'Q3'].map((x, j) => ({
      x,
      ...Object.fromEntries(keys.map((k, i) => [k, (i - 3) * (j + 1)])),
    }));
    const layout = layoutChart(model(spec({ y_keys: keys, stacked: true }), rows), ctx(710));
    expect(layout.form).toBe('columns');
    checkInvariants(layout, 710);
  });
});

describe('time series', () => {
  it.each([6, 11])('a %i-month line is a time-axis line', (n) => {
    const rows = months(n).map((x, i) => ({ x, y: i * 3 }));
    for (const w of WIDTHS) {
      const layout = layoutChart(model(spec({ type: 'line' }), rows), ctx(w));
      expect(layout.form).toBe('timeLine');
      expect(layout.zoom).toBeNull();
      checkInvariants(layout, w);
    }
  });

  it('the irregular 95-point daily line draws its full range with breaks', () => {
    const layout = layoutChart(model(spec({ type: 'line' }), irregularDaily()), ctx(710));
    expect(layout.form).toBe('timeLine');
    expect(layout.zoom).toBeNull();
    expect(layout.model.x.breaks.length).toBeGreaterThan(0);
    checkInvariants(layout, 710);
  });

  it('the same data as bars opens on a recent window with ≥ 4 px per day', () => {
    const layout = layoutChart(model(spec({ type: 'bar' }), irregularDaily()), ctx(710));
    expect(layout.form).toBe('timeBars');
    expect(layout.zoom).not.toBeNull();
    const plot = 710 - layout.grid.left - layout.grid.right;
    const days = (layout.zoom!.endValue - layout.zoom!.startValue) / DAY;
    expect(plot / days).toBeGreaterThanOrEqual(3.9);
    expect(layout.grid.bottom).toBeGreaterThanOrEqual(layout.zoom!.slider);
    checkInvariants(layout, 710);
  });
});

describe('part-to-whole', () => {
  it.each([2, 3])('a %i-slice donut stays a donut with its total in the hole', (n) => {
    const rows = Array.from({ length: n }, (_, i) => ({ x: `Part ${i}`, y: i + 2 }));
    const layout = layoutChart(model(spec({ type: 'donut' }), rows), ctx(710));
    expect(layout.form).toBe('donut');
    expect(layout.pie?.centreTotal).toBe(true);
    expect(layout.pie?.legend).toBe('right');
    expect(layoutChart(model(spec({ type: 'donut' }), rows), ctx(317)).pie?.legend).toBe('below');
  });

  it('an all-zero donut is an empty state with the table open (before validateChart)', () => {
    const layout = layoutChart(model(spec({ type: 'donut' }), [{ x: 'a', y: 0 }, { x: 'b', y: 0 }]), ctx(710));
    expect(layout.renderer).toBe('empty');
    expect(layout.tableOpen).toBe(true);
    expect(layout.emptyMessage).toMatch(/Every value is 0/);
  });

  it('an all-zero line still draws, with a note and a 0-1 axis', () => {
    const rows = ['a', 'b', 'c'].map((x) => ({ x, y: 0 }));
    const layout = layoutChart(model(spec({ type: 'line' }), rows), ctx(710));
    expect(layout.renderer).toBe('echarts');
    expect(layout.notes).toContain('Every value is 0.');
    const option = buildOptionFromModel(layout.model, layout, fallbackPalette('dark'))!;
    expect((option.yAxis as Record<string, unknown>).max).toBe(1);
  });

  it('a 4-slice percent pie from Mermaid stays a pie', () => {
    const parsed = parseMermaidChart('pie title Share\n"A" : 30\n"B" : 30\n"C" : 25\n"D" : 15')!;
    const layout = layoutChart(parsed.model, ctx(680, { context: 'mermaid' }));
    expect(layout.form).toBe('pie');
    expect(parsed.model.valueFlags.percentWhole).toBe(true);
  });
});

describe('the label ladder', () => {
  it('a 50-bin histogram at 262 px thins its labels uniformly and never scrolls', () => {
    const rows = Array.from({ length: 50 }, (_, i) => ({ x: `${i * 10} - ${i * 10 + 10}`, y: (i * 7) % 13 }));
    const layout = layoutChart(model(spec({ type: 'histogram' }), rows), ctx(262));
    expect(layout.form).toBe('histogram');
    expect(layout.labelEvery).toBeGreaterThan(1);
    expect(layout.zoom).toBeNull();
    const shown = histogramLabelled(50, layout.labelEvery);
    expect(shown[0]).toBe(true);
    expect(shown[49]).toBe(true);
    const idx = shown.map((s, i) => (s ? i : -1)).filter((i) => i >= 0);
    for (let k = 1; k < idx.length; k++) expect(idx[k] - idx[k - 1]).toBeGreaterThanOrEqual(layout.labelEvery);
    checkInvariants(layout, 262);
  });

  it('a category line with long labels windows, then numbers its ticks with a key', () => {
    const rows = NAMES.map((x, i) => ({ x: `${x} region office`, y: i }));
    const mid = layoutChart(model(spec({ type: 'line' }), rows.slice(0, 12)), ctx(710));
    expect(mid.form).toBe('catLine');
    expect(mid.zoom !== null || mid.numbered || mid.rotate === 45).toBe(true);
    const phone = layoutChart(model(spec({ type: 'line' }), rows), ctx(262));
    expect(phone.numbered).toBe(true);
    expect(phone.notes).toContain('Labels are numbered; the key is below.');
    expect(phone.rotate).toBe(0);
    checkInvariants(phone, 262);
  });

  it('rotates only at ≥ 480 px, for labels of ≤ 16 characters with band ≥ 24', () => {
    const rows = Array.from({ length: 22 }, (_, i) => ({ x: `Week ${String(i + 1).padStart(2, '0')} 2026`, y: i }));
    const desktop = layoutChart(model(spec({ type: 'line' }), rows), ctx(710));
    expect(desktop.rotate).toBe(45);
    const phone = layoutChart(model(spec({ type: 'line' }), rows), ctx(317));
    expect(phone.rotate).toBe(0);
    const long = rows.map((r) => ({ ...r, x: `${r.x} (seventeen+)` }));
    expect(layoutChart(model(spec({ type: 'line' }), long), ctx(710)).rotate).toBe(0);
  });
});

describe('summary', () => {
  it('describes a ranking with its highest and lowest', () => {
    const layout = layoutChart(model(spec(), RANKING), ctx(710));
    const text = summaryText(describeChart(layout));
    expect(text).toMatch(/^Bar chart of Y by X, 26 categories\./);
    expect(text).toContain('Highest Owner Alpha at');
  });
  it('describes a gappy time line with its range', () => {
    const layout = layoutChart(model(spec({ type: 'line' }), irregularDaily()), ctx(710));
    expect(summaryText(describeChart(layout))).toMatch(/95 points from 3 Jan 2025 to .* with \d+ gaps? without rows/);
  });
  it('choose: one category, one series is a stat figure', () => {
    expect(chooseForm(model(spec(), [{ x: 'Only', y: 7 }]), ctx(710)).form).toBe('stat');
  });
});

describe('review fixes (2026-11-25)', () => {
  it('R4/V10: new line breaks or a new pie size change the structural key, so a resize rebuilds', () => {
    const m = model(spec(), ['Big Echo', 'Team Echo 1', 'Account Delta Success 2'].map((x, i) => ({ x, y: i + 1 })));
    const wide = layoutChart(m, ctx(459));
    const narrow = layoutChart(m, ctx(296));
    if (wide.form === narrow.form && JSON.stringify(wide.catText) !== JSON.stringify(narrow.catText)) {
      expect(narrow.structuralKey).not.toBe(wide.structuralKey);
    }
    const donut = model(spec({ type: 'donut' }), [
      { x: 'A', y: 3 },
      { x: 'B', y: 1 },
    ]);
    const big = layoutChart(donut, ctx(320));
    const small = layoutChart(donut, ctx(200));
    expect(small.pie!.diameter).toBeLessThan(big.pie!.diameter);
    expect(small.structuralKey).not.toBe(big.structuralKey);
  });

  it('R5: rows that share a date are drawn as separate categories, never on one time position', () => {
    const rows = [
      { x: '2026-01-05', y: 3 },
      { x: '2026-01-05', y: 7 },
      { x: '2026-01-06', y: 2 },
    ];
    for (const type of ['bar', 'line'] as const) {
      const layout = layoutChart(model(spec({ type }), rows), ctx(710));
      expect(layout.model.x.kind).toBe('category');
      expect(layout.form === 'columns' || layout.form === 'catLine' || layout.form === 'barlist').toBe(true);
      expect(layout.notes).toContain('Some dates have more than one row, so each row is drawn on its own.');
      const option = buildOptionFromModel(layout.model, layout, fallbackPalette('dark'));
      const data = (option?.series as Array<{ data: unknown[] }> | undefined)?.[0]?.data ?? [];
      expect(data).toHaveLength(3);
    }
  });

  it('R7: a horizontal bar over thousands of dates is drawn on a time axis, not as thousands of rows', () => {
    const rows = Array.from({ length: 5000 }, (_, i) => ({
      x: new Date(Date.UTC(2010, 0, 1) + i * DAY).toISOString().slice(0, 10),
      y: i % 17,
    }));
    const layout = layoutChart(model(spec({ type: 'horizontal_bar' }), rows), ctx(710));
    expect(layout.renderer).toBe('echarts');
    expect(layout.form).toBe('timeBars');
    expect(layout.notes.join(' ')).toMatch(/too many for a bar list/);
    // A pie of 300 dated parts is a table, not a 300-row bar list.
    const pie = layoutChart(model(spec({ type: 'pie' }), rows.slice(0, 300).map((r) => ({ ...r, y: r.y + 1 }))), ctx(710));
    expect(pie.renderer).toBe('table');
  });

  it('V12: a short category line shows every point at every width (no window), numbered if need be', () => {
    const rows = ['North-east hub', 'South-west hub', 'Central depot', 'Harbour store', 'Airport kiosk', 'Hill station', 'River dock', 'Old town'].map(
      (x, i) => ({ x, y: i * 3 }),
    );
    for (const w of [262, 317, 600, 710]) {
      const layout = layoutChart(model(spec({ type: 'line' }), rows), ctx(w));
      expect(layout.zoom, `${w}px`).toBeNull();
      checkInvariants(layout, w);
    }
    // 8 short labels on a line fit in a 27 px band at 262 px: there is no bar
    // to keep 28 px wide (MIN_BAND is a column rule).
    const mon = ['W1', 'W2', 'W3', 'W4', 'W5', 'W6', 'W7', 'W8'].map((x, i) => ({ x, y: i }));
    const phone = layoutChart(model(spec({ type: 'line' }), mon), ctx(262));
    expect(phone.zoom).toBeNull();
    expect(phone.numbered).toBe(false);
  });

  it('R6: a 200 × 180-character category line plans with a bounded number of measurements', () => {
    let calls = 0;
    const counting = (t: string) => {
      calls += 1;
      return measure(t);
    };
    const word = 'Region office north-east department ';
    const rows = Array.from({ length: 200 }, (_, i) => ({ x: `${i} ${word.repeat(5)}`.slice(0, 180), y: i }));
    const m = model(spec({ type: 'line' }), rows);
    const layout = layoutChart(m, { width: 1280, context: 'inline', measure: counting });
    expect(layout.renderer).toBe('echarts');
    // The old linear ladder made ~958k calls here; binary search and the
    // early bail cut that by more than an order of magnitude.
    expect(calls).toBeLessThan(120_000);
  });

  it('V4: planned month ticks always include the first and the last month', () => {
    for (const n of [6, 11, 12]) {
      for (const w of [262, 317, 600]) {
        const layout = layoutChart(model(spec({ type: 'line' }), months(n).map((x, i) => ({ x, y: i }))), ctx(w));
        const ticks = layout.timeAxis?.ticks ?? [];
        expect(ticks[0], `${n}@${w}`).toBe(layout.model.x.cats[0].t);
        expect(ticks[ticks.length - 1], `${n}@${w}`).toBe(layout.model.x.cats[n - 1].t);
      }
    }
  });

  it('V1: the export plan never windows; numbered ticks thin uniformly when even the numbers crowd', () => {
    const rows = Array.from({ length: 180 }, (_, i) => ({ x: `Office number ${i + 1} of the east region`, y: i }));
    const layout = layoutChart(model(spec({ type: 'line' }), rows), { width: 1200, context: 'inline', measure, forExport: true });
    expect(layout.zoom).toBeNull();
    expect(layout.numbered).toBe(true);
    const shown = histogramLabelled(180, layout.labelEvery);
    expect(shown[0]).toBe(true);
    expect(shown[179]).toBe(true);
    const band = (1200 - layout.grid.left - layout.grid.right) / 180;
    expect(band * layout.labelEvery).toBeGreaterThanOrEqual(measure('180') + 6);
  });
});
