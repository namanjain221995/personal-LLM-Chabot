/**
 * The Data-section download: what it is CALLED and what is IN it.
 *
 * Owner report 2026-08-31: every download arrived as `techsara-data.csv`, so a
 * folder of them was indistinguishable and each overwrote the last; and the
 * file held the preview rather than the result the table was captioned with.
 */

import { describe, expect, it } from 'vitest';
import { csvFilenameFor, rowsToCsv } from '@/lib/csv';
import type { Meta } from '@/lib/types';

const at = (iso: string): Meta => ({
  route: 'sql',
  salesforce_sources: {
    source: 'live',
    objects: ['Interview__c'],
    query_timestamp: iso,
  } as Meta['salesforce_sources'],
});

describe('csvFilenameFor', () => {
  it('names the file after the object that was queried', () => {
    expect(csvFilenameFor(at('2026-08-31T14:32:00+00:00'))).toMatch(
      /^interview-\d{4}-\d{2}-\d{2}-\d{4}\.csv$/,
    );
  });

  it('never returns the old fixed name for a Salesforce answer', () => {
    expect(csvFilenameFor(at('2026-08-31T14:32:00+00:00'))).not.toBe(
      'techsara-data.csv',
    );
  });

  it('gives two pulls of the same object two different names', () => {
    const morning = csvFilenameFor(at('2026-08-31T09:05:00+00:00'));
    const afternoon = csvFilenameFor(at('2026-08-31T14:32:00+00:00'));
    expect(morning).not.toBe(afternoon);
  });

  it('drops the __c suffix and other characters a filesystem dislikes', () => {
    const meta = at('2026-08-31T14:32:00+00:00');
    meta.salesforce_sources!.objects = ['My Custom/Obj__c'];
    expect(csvFilenameFor(meta)).toMatch(/^my-customobj-/);
  });

  it('joins a multi-object answer instead of picking one at random', () => {
    const meta = at('2026-08-31T14:32:00+00:00');
    meta.salesforce_sources!.objects = ['Opportunity', 'Account'];
    expect(csvFilenameFor(meta)).toMatch(/^opportunity-account-/);
  });

  it('falls back to the route, then to the old name, rather than throwing', () => {
    expect(csvFilenameFor({ route: 'search' } as Meta)).toMatch(/^search-/);
    expect(csvFilenameFor({} as Meta)).toMatch(/^techsara-data-/);
  });

  it('survives a timestamp it cannot parse', () => {
    const meta = at('not a date');
    expect(csvFilenameFor(meta)).toBe('interview.csv');
  });

  it('always ends in .csv', () => {
    expect(csvFilenameFor(at('2026-08-31T14:32:00Z')).endsWith('.csv')).toBe(true);
  });
});

describe('rowsToCsv', () => {
  it('writes every row it is handed', () => {
    const rows = Array.from({ length: 1000 }, (_, i) => ({ Id: i, Name: `r${i}` }));
    // header + 1000 rows, trailing CRLF
    expect(rowsToCsv(rows).trimEnd().split('\r\n')).toHaveLength(1001);
  });

  it('quotes cells holding commas, quotes or newlines', () => {
    const csv = rowsToCsv([{ Name: 'Acme, Inc. "HQ"\nfloor 2' }]);
    expect(csv).toContain('"Acme, Inc. ""HQ""\nfloor 2"');
  });
});
