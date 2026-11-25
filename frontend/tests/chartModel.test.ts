/**
 * ChartModel from a spec + rows (charts design v2 §2, §3.2-3.3, rule 16).
 * Every row here is made up; the shapes mirror the live charts (counts and
 * shapes only were read from the DB).
 */

import { afterEach, describe, expect, it } from 'vitest';
import {
  detectTime,
  fillTimeRange,
  modelFromSpec,
  timeBreaks,
  unitsInSpan,
  type ChartModel,
} from '../lib/chartModel';
import type { ChartSpec, DataRow } from '../lib/types';

function spec(over: Partial<ChartSpec> = {}): ChartSpec {
  return { type: 'bar', x_key: 'x', y_keys: ['y'], title: 'T', stacked: false, ...over };
}

function model(s: ChartSpec, rows: DataRow[]): ChartModel {
  const r = modelFromSpec(s, rows);
  if (!r.model) throw new Error(`no model: ${r.problem}`);
  return r.model;
}

const DAY = 86_400_000;

/** 95 irregular daily points over ~504 days with 9 empty months (made up). */
function irregularDaily(): DataRow[] {
  const rows: DataRow[] = [];
  const start = Date.UTC(2025, 0, 3);
  let t = start;
  let i = 0;
  while (rows.length < 95) {
    const d = new Date(t);
    const month = d.getUTCFullYear() * 12 + d.getUTCMonth();
    // Leave nine whole months empty.
    const emptyMonths = new Set([2025 * 12 + 3, 2025 * 12 + 4, 2025 * 12 + 5, 2025 * 12 + 6, 2025 * 12 + 7, 2025 * 12 + 8, 2025 * 12 + 9, 2025 * 12 + 10, 2025 * 12 + 11]);
    if (!emptyMonths.has(month)) {
      rows.push({ x: d.toISOString().slice(0, 10), y: (i % 11) + 1 });
      i += 1;
    }
    t += DAY * (rows.length % 3 === 0 ? 2 : 3);
  }
  return rows;
}

afterEach(() => {
  delete process.env.TZ;
});

describe('time detection', () => {
  it('needs 100% of non-null x values to be dates (99% is a category axis)', () => {
    const dates = Array.from({ length: 99 }, (_, i) => `2026-01-${String((i % 28) + 1).padStart(2, '0')}`);
    expect(detectTime(dates)).not.toBeNull();
    expect(detectTime([...dates, 'Unknown'])).toBeNull();
  });

  it('leaves rows with no x off the axis and counts them', () => {
    const m = model(spec({ type: 'line' }), [
      { x: '2026-01', y: 1 },
      { x: null, y: 4 },
      { x: '2026-02', y: 2 },
    ]);
    expect(m.x.kind).toBe('time');
    expect(m.x.cats).toHaveLength(2);
    expect(m.x.nullRows).toBe(1);
    expect(m.notes.join(' ')).toMatch(/1 row has no date/);
    expect(m.tableRows).toHaveLength(3);
  });

  it('reads the space-separated midnight day-01 buckets as months', () => {
    const rows = ['2026-01', '2026-02', '2026-03', '2026-04', '2026-05', '2026-06'].map((m, i) => ({
      x: `${m}-01 00:00:00`,
      y: i + 1,
    }));
    const m = model(spec({ type: 'bar' }), rows);
    expect(m.x.kind).toBe('time');
    expect(m.x.grain).toBe('month');
    expect(m.x.cats[0].label).toBe('Jan 2026');
  });

  it('maps all-day-01 dates to months, but 3 day-01 dates among 95 stay days', () => {
    expect(detectTime(['2026-01-01', '2026-02-01', '2026-03-01'])?.grain).toBe('month');
    const daily = irregularDaily();
    expect(daily.filter((r) => String(r.x).endsWith('-01')).length).toBeGreaterThan(0);
    expect(detectTime(daily.map((r) => r.x as string))?.grain).toBe('day');
  });

  it('parses from calendar parts: no day shift west of UTC', () => {
    process.env.TZ = 'America/Los_Angeles';
    const m = model(spec({ type: 'line' }), [
      { x: '2026-03-01', y: 1 },
      { x: '2026-03-02', y: 2 },
    ]);
    expect(m.x.cats[0].label).toBe('1 Mar 2026');
    expect(m.x.cats[0].t).toBe(Date.UTC(2026, 2, 1));
  });

  it('shows a zone suffix as written', () => {
    const m = model(spec({ type: 'line' }), [
      { x: '2026-03-01T09:15:00Z', y: 1 },
      { x: '2026-03-01T10:15:00Z', y: 2 },
    ]);
    expect(m.x.grain).toBe('datetime');
    expect(m.x.cats[0].label).toBe('1 Mar 2026, 09:15 Z');
  });
});

describe('gap breaks', () => {
  it('month grain: a break where consecutive months are more than one apart', () => {
    const t = [0, 1, 2, 5, 6].map((m) => Date.UTC(2026, m, 1));
    expect(timeBreaks(t, 'month')).toEqual([2]);
  });

  it('day grain: a break larger than max(7 days, 3 × median gap)', () => {
    const t = [0, 1, 2, 3, 20, 21].map((d) => Date.UTC(2026, 0, 1) + d * DAY);
    expect(timeBreaks(t, 'day')).toEqual([3]);
    // A 5-day gap in daily data is under the 7-day floor: no break.
    const t2 = [0, 1, 2, 7, 8].map((d) => Date.UTC(2026, 0, 1) + d * DAY);
    expect(timeBreaks(t2, 'day')).toEqual([]);
  });

  it('the irregular daily line breaks across the empty months', () => {
    const m = model(spec({ type: 'line' }), irregularDaily());
    expect(m.x.cats).toHaveLength(95);
    expect(m.x.breaks.length).toBeGreaterThanOrEqual(1);
    const [b] = m.x.breaks;
    const gap = (m.x.cats[b + 1].t ?? 0) - (m.x.cats[b].t ?? 0);
    expect(gap).toBeGreaterThan(200 * DAY);
    expect(unitsInSpan(m)).toBeGreaterThan(450);
  });

  it('fills a monthly bar range with labelled empty slots', () => {
    const m = model(spec({ type: 'bar' }), [
      { x: '2026-01', y: 1 },
      { x: '2026-02', y: 2 },
      { x: '2026-04', y: 4 },
    ]);
    const filled = fillTimeRange(m)!;
    expect(filled.x.cats.map((c) => c.label)).toEqual(['Jan 2026', 'Feb 2026', 'Mar 2026', 'Apr 2026']);
    expect(filled.x.cats[2].present).toBe(false);
    expect(filled.series[0].values).toEqual([1, 2, null, 4]);
  });
});

describe('ids, names and series', () => {
  it('humanises series names and drops __c', () => {
    const m = model(spec({ y_keys: ['record_count', 'Amount__c'] }), [{ x: 'a', record_count: 1, Amount__c: 2 }]);
    expect(m.series.map((s) => s.label)).toEqual(['Record count', 'Amount']);
  });

  it('keeps ids unique when names collide or repeat', () => {
    const m = model(spec({ y_keys: ['total_count', 'Total count'] }), [
      { x: 'Same name', total_count: 1, 'Total count': 2 },
      { x: 'Same name', total_count: 3, 'Total count': 4 },
    ]);
    expect(m.series.map((s) => s.id)).toEqual(['s0', 's1']);
    expect(new Set(m.series.map((s) => s.label)).size).toBe(2);
    expect(m.x.cats.map((c) => c.id)).toEqual(['c0', 'c1']);
    expect(m.x.cats.map((c) => c.label)).toEqual(['Same name', 'Same name']);
  });

  it('never humanises or groups category values', () => {
    const m = model(spec(), [
      { x: 2024, y: 1 },
      { x: 'some_value', y: 2 },
    ]);
    expect(m.x.cats.map((c) => c.label)).toEqual(['2024', 'some_value']);
  });

  it('folds stacked series past 8 into "Other" and keeps the totals', () => {
    const keys = Array.from({ length: 11 }, (_, i) => `k${i}`);
    const rows: DataRow[] = [{ x: 'a', ...Object.fromEntries(keys.map((k, i) => [k, i + 1])) }];
    const m = model(spec({ y_keys: keys, stacked: true }), rows);
    expect(m.series).toHaveLength(8);
    expect(m.series[7].label).toBe('Other (4 series)');
    const total = m.series.reduce((a, s) => a + (s.values[0] ?? 0), 0);
    expect(total).toBe(keys.reduce((a, _, i) => a + i + 1, 0));
    expect(m.notes.join(' ')).toMatch(/combined as "Other"/);
  });

  it('keeps the 8 largest grouped series, coloured in y_keys order (not by rank)', () => {
    const keys = Array.from({ length: 10 }, (_, i) => `k${i}`);
    // k0 and k1 are the two smallest; they are the ones left out.
    const rows: DataRow[] = [{ x: 'a', ...Object.fromEntries(keys.map((k, i) => [k, i === 0 ? 1 : i === 1 ? 2 : 100 + i])) }];
    const m = model(spec({ y_keys: keys }), rows);
    expect(m.series.map((s) => s.key)).toEqual(keys.slice(2));
    expect(m.series.map((s) => s.slot)).toEqual([0, 1, 2, 3, 4, 5, 6, 7]);
    expect(m.notes.join(' ')).toMatch(/2 more series are in the table/);
    // The table still has every measure.
    expect(Object.keys(m.tableRows[0])).toEqual(['x', ...keys]);
  });

  it('flags an all-zero chart instead of refusing it', () => {
    const m = model(spec({ type: 'donut' }), [
      { x: 'a', y: 0 },
      { x: 'b', y: 0 },
    ]);
    expect(m.valueFlags.allZero).toBe(true);
  });

  it('sorts part-to-whole data largest first', () => {
    const m = model(spec({ type: 'pie' }), [
      { x: 'small', y: 1 },
      { x: 'big', y: 9 },
    ]);
    expect(m.x.cats.map((c) => c.label)).toEqual(['big', 'small']);
    expect(m.series[0].values).toEqual([9, 1]);
  });

  it('still refuses a spec that names a missing column', () => {
    expect(modelFromSpec(spec({ x_key: 'ghost' }), [{ x: 'a', y: 1 }]).problem).toBe('missing-x-column');
  });
});
