/**
 * Chart v2 number and date formatting (charts design v2 §6). Made-up data.
 */

import { describe, expect, it } from 'vitest';
import {
  axisTickFormatter,
  chartFileName,
  formatCategory,
  formatDateByGrain,
  formatShare,
  formatTimeTick,
  formatValue,
  humanizeKey,
  parseCalendar,
} from '../lib/chartFormat';

describe('formatValue', () => {
  it('keeps three significant digits below 1 (0.0042 is not "0")', () => {
    expect(formatValue(0.0042)).toBe('0.0042');
    expect(formatValue(0.123456)).toBe('0.123');
  });
  it('prints integers grouped and decimals with two places', () => {
    expect(formatValue(3091)).toBe((3091).toLocaleString());
    expect(formatValue(12.345)).toBe((12.35).toLocaleString());
  });
  it('shows negatives and null', () => {
    expect(formatValue(-5)).toBe((-5).toLocaleString());
    expect(formatValue(null)).toBe('—');
    expect(formatValue(Number.NaN)).toBe('—');
  });
});

describe('axisTickFormatter — one style per axis', () => {
  it('uses full numbers below 10,000', () => {
    const f = axisTickFormatter(9500);
    expect(f(9500)).toBe((9500).toLocaleString());
    expect(f(2000)).toBe((2000).toLocaleString());
  });
  it('uses compact ticks everywhere on an axis that reaches 10,000', () => {
    const f = axisTickFormatter(25_000);
    expect(f(25_000)).toBe('25k');
    expect(f(5_000)).toBe('5k');
    expect(f(0)).toBe('0');
  });
});

describe('percent and shares', () => {
  it('gives shares with one decimal place', () => {
    expect(formatShare(1, 3)).toBe('33.3%');
    expect(formatShare(1, 0)).toBe('—');
  });
});

describe('category labels', () => {
  it('never groups a number used as a category (2024 is not "2,024")', () => {
    expect(formatCategory(2024)).toBe('2024');
    expect(formatCategory(null)).toBe('—');
    expect(formatCategory(true)).toBe('Yes');
  });
});

describe('calendar parsing', () => {
  it('parses the space-separated midnight bucket form', () => {
    const v = parseCalendar('2026-03-01 00:00:00');
    expect(v?.form).toBe('datetime');
    expect(v?.t).toBe(Date.UTC(2026, 2, 1));
  });
  it('keeps a zone suffix as text, never shifting the day', () => {
    const v = parseCalendar('2026-03-01T00:00:00+05:30');
    expect(v?.t).toBe(Date.UTC(2026, 2, 1));
    expect(v?.zone).toBe('+05:30');
    expect(formatDateByGrain(v!.t, 'datetime', v!.zone)).toBe('1 Mar 2026, 00:00 +05:30');
  });
  it('refuses impossible dates', () => {
    expect(parseCalendar('2026-02-31')).toBeNull();
    expect(parseCalendar('2026-13')).toBeNull();
    expect(parseCalendar('March 2026')).toBeNull();
  });
});

describe('dates by grain', () => {
  const t = Date.UTC(2026, 2, 12, 14, 30);
  it('formats day, month and datetime from UTC parts', () => {
    expect(formatDateByGrain(Date.UTC(2026, 2, 12), 'day')).toBe('12 Mar 2026');
    expect(formatDateByGrain(Date.UTC(2026, 2, 1), 'month')).toBe('Mar 2026');
    expect(formatDateByGrain(t, 'datetime')).toBe('12 Mar 2026, 14:30');
  });
  it('shows the year on the first tick and at year starts only', () => {
    expect(formatTimeTick(Date.UTC(2026, 2, 12), 'day', true)).toBe('12 Mar 2026');
    expect(formatTimeTick(Date.UTC(2026, 2, 12), 'day', false)).toBe('12 Mar');
    expect(formatTimeTick(Date.UTC(2026, 0, 1), 'day', false)).toBe('1 Jan 2026');
    expect(formatTimeTick(Date.UTC(2026, 2, 1), 'month', false)).toBe('Mar 2026');
  });
});

describe('names', () => {
  it('humanises column keys', () => {
    expect(humanizeKey('total_count')).toBe('Total count');
    expect(humanizeKey('Amount__c')).toBe('Amount');
    expect(humanizeKey('record_count__c')).toBe('Record count');
  });
  it('slugs a title into a download name', () => {
    expect(chartFileName('Count by Owner — 2026')).toBe('count-by-owner-2026.png');
    expect(chartFileName('', 'csv')).toBe('chart.csv');
  });
});
