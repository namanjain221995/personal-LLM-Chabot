// @vitest-environment jsdom
/**
 * M-17: the CSV object URL must outlive the click that consumes it.
 *
 * `downloadCsv` revoked the blob URL on the same synchronous tick as
 * `a.click()`. Chromium captures the blob during the click and so survived it;
 * Safari and Firefox resolve the object URL after the click's task returns,
 * where a revoked URL yields no file or an empty one. `exportMarkdown` and
 * `MermaidBlock` already defer their revokes for exactly this reason — this
 * pins the third one to the same rule.
 *
 * The assertions are about ORDER and TIMING only. Nothing here touches the CSV
 * text, the encoding, the filename or the row count; those stay covered by
 * tests/csv-download-name.test.ts.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { downloadCsv, rowsToCsv } from '@/lib/csv';

const rows = [
  { name: 'A', count: 10 },
  { name: 'B', count: 20 },
];

let created: string[];
let revoked: string[];
let clickedHref: string | null;

beforeEach(() => {
  vi.useFakeTimers();
  created = [];
  revoked = [];
  clickedHref = null;

  let n = 0;
  Object.defineProperty(URL, 'createObjectURL', {
    configurable: true,
    value: () => {
      const url = `blob:csv/${(n += 1)}`;
      created.push(url);
      return url;
    },
  });
  Object.defineProperty(URL, 'revokeObjectURL', {
    configurable: true,
    value: (url: string) => {
      revoked.push(url);
    },
  });

  // Record the href AT CLICK TIME — the anchor is removed straight after, so
  // reading it later would prove nothing about what the browser saw.
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (
    this: HTMLAnchorElement,
  ) {
    clickedHref = this.getAttribute('href');
  });
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe('M-17 · downloadCsv object-URL lifetime', () => {
  it('creates an object URL and clicks an anchor pointing at it', () => {
    downloadCsv(rows, 'report');
    expect(created).toHaveLength(1);
    expect(clickedHref).toBe(created[0]);
  });

  it('does NOT revoke on the same tick as the click', () => {
    downloadCsv(rows, 'report');
    // This is the regression: the URL was dead before the browser could use it.
    expect(revoked).toEqual([]);
  });

  it('revokes once the tick has passed, so nothing is leaked', () => {
    downloadCsv(rows, 'report');
    vi.advanceTimersByTime(0);
    expect(revoked).toEqual(created);
  });

  it('gives each download its own URL, and revokes each exactly once', () => {
    downloadCsv(rows, 'first');
    downloadCsv(rows, 'second');
    expect(created).toHaveLength(2);
    expect(new Set(created).size).toBe(2);
    expect(revoked).toEqual([]);

    vi.advanceTimersByTime(0);
    expect(revoked.slice().sort()).toEqual(created.slice().sort());
  });

  it('leaves the filename and the CSV body exactly as they were', () => {
    downloadCsv(rows, 'report');
    // Filename behaviour is asserted in csv-download-name.test.ts; this only
    // guards against the revoke fix disturbing the payload.
    expect(rowsToCsv(rows)).toBe('name,count\r\nA,10\r\nB,20\r\n');
  });
});
