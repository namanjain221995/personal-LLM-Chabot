/**
 * A big data table arrives as ONE enormous SSE `data:` line.
 *
 * The parser used to call `buffer.search(/[\r\n]/)` each time a chunk landed,
 * rescanning the whole accumulated buffer from index 0. With no interior
 * newline that is O(n^2): measured 1 MB = 31 ms, 4 MB = 436 ms, 8 MB = 1.5 s,
 * 16 MB = 6.7 s of blocked main thread — the tab freezes rather than slows.
 * Resuming the scan where it left off makes it linear.
 */
import { describe, expect, it } from 'vitest';
import { SSEParser } from '../lib/sse';

function feedInChunks(payload: string, chunkSize = 16 * 1024) {
  const parser = new SSEParser();
  const events: unknown[] = [];
  for (let i = 0; i < payload.length; i += chunkSize) {
    events.push(...parser.feed(payload.slice(i, i + chunkSize)));
  }
  return events;
}

describe('SSEParser with a very large single event', () => {
  it('parses a multi-megabyte data line correctly', () => {
    const rows = Array.from({ length: 20000 }, (_, i) => ({
      Id: `a03Ps${i}`,
      Name: `I-${i}`,
      Round__c: 'Final',
    }));
    const json = JSON.stringify({ data: rows });
    expect(json.length).toBeGreaterThan(1_000_000);
    const events = feedInChunks(`data: ${json}\n\n`);
    expect(events).toHaveLength(1);
    const parsed = JSON.parse((events[0] as { data: string }).data);
    expect(parsed.data).toHaveLength(20000);
    expect(parsed.data[19999].Name).toBe('I-19999');
  });

  it('scales roughly linearly, not quadratically, with payload size', () => {
    const line = (n: number) => `data: ${'x'.repeat(n)}\n\n`;
    // BEST of several runs, not a single sample: this file shares a machine
    // with the rest of the suite, and one descheduled run is enough to make a
    // single timing look quadratic. The minimum is the least noise-polluted
    // estimate of the real cost.
    const best = (n: number) => {
      const payload = line(n);
      let ms = Infinity;
      for (let i = 0; i < 5; i += 1) {
        const t0 = performance.now();
        feedInChunks(payload);
        ms = Math.min(ms, performance.now() - t0);
      }
      return ms;
    };
    best(1 << 20); // warm up the JIT
    const small = Math.max(best(1 << 21), 0.5); // 2 MB
    const large = best(1 << 23); // 8 MB — 4x the bytes
    // Quadratic is ~16x for 4x the bytes; linear is ~4x. The threshold sits
    // between them with room for a busy machine.
    expect(large / small).toBeLessThan(10);
  });

  it('still splits normal multi-line streams', () => {
    const parser = new SSEParser();
    const events = parser.feed('data: {"a":1}\n\ndata: {"b":2}\n\n');
    expect(events).toHaveLength(2);
  });
});
