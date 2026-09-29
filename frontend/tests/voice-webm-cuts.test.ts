/**
 * Where Chrome's MediaRecorder cuts a WebM/Opus stream into timeslices, and
 * how a recording is continued in a new session from any slice
 * (lib/voice.ts WebmCutTracker, 2026-09-29).
 *
 * The fixture is REAL: four 1 s timeslices recorded by Chrome 151 from its
 * fake-microphone device (MediaRecorder, audio/webm;codecs=opus), saved byte
 * for byte by tests/fixtures/chrome151-webm-opus-1s. Measured on it and on a
 * 62 s run at the recorder's own 5 s timeslice: every slice after the first
 * begins ONE BYTE INTO a SimpleBlock (its 0xA3 ID ends the slice before), and
 * a new Cluster follows 965 bytes later.
 *
 * A continuation built from the tracker's lead was decoded by Chrome to the
 * exact expected length (31.80 s of the 62 s run, cut at slice 6); the naive
 * init + slices lost 60 ms at the cut (31.74 s). This file holds the
 * structural half of that: the continuation parses as WebM from its first
 * byte, keeps every block after the cut, and gives them their true times.
 */
import { describe, expect, it } from 'vitest';
import { WebmCutTracker, clusterHeader } from '@/lib/voice';
import { chromeSlices as slices, concat, walk } from './webm-walk';

describe("Chrome's timeslices, as recorded", () => {
  it('cut every slice one byte into a block, so no slice after the first is a stream by itself', () => {
    const whole = walk(concat(slices));
    let offset = 0;
    for (let k = 1; k < slices.length; k += 1) {
      offset += slices[k - 1]!.byteLength;
      const straddling = whole.blocks.find((b) => b.at < offset && offset < b.at + 3 + b.size);
      expect(straddling?.at).toBe(offset - 1);
      // It is the last block of its cluster: a new Cluster follows it.
      expect(whole.clusters).toContain(straddling!.at + 3 + straddling!.size);
      // And the slice has no header of its own to start a stream with.
      expect([...slices[k]!.subarray(0, 4)]).not.toEqual([0x1a, 0x45, 0xdf, 0xa3]);
    }
  });
});

describe('the tracker', () => {
  it('finds the 146-byte init segment and, for every slice, the front that makes it a stream', () => {
    const tracker = new WebmCutTracker();
    const leads: Array<Uint8Array | null> = [];
    for (const s of slices) {
      leads.push(tracker.lead());
      tracker.feed(s);
    }
    expect(tracker.init?.byteLength).toBe(146);
    expect([...tracker.init!.subarray(0, 4)]).toEqual([0x1a, 0x45, 0xdf, 0xa3]);
    expect(leads[0]).toBeNull(); // the first slice IS the start of the stream
    const whole = walk(concat(slices));
    let offset = 0;
    for (let k = 1; k < slices.length; k += 1) {
      offset += slices[k - 1]!.byteLength;
      // The cluster the cut fell in, and the one byte of the block it went through.
      const clusterStart = Math.max(...whole.clusters.filter((c) => c < offset));
      const tcOf = walk(concat(slices)).blocks.find((b) => b.at > clusterStart)!;
      const expected = concat([clusterHeader(Math.round(tcOf.t / 1020) * 1020), new Uint8Array([0xa3])]);
      expect([...leads[k]!]).toEqual([...expected]);
    }
  });

  it('builds, from any slice, a continuation that parses from its first byte and keeps every block at its true time', () => {
    const tracker = new WebmCutTracker();
    const leads: Array<Uint8Array | null> = [];
    for (const s of slices) {
      leads.push(tracker.lead());
      tracker.feed(s);
    }
    const whole = walk(concat(slices));
    let offset = 0;
    for (let k = 1; k < slices.length; k += 1) {
      offset += slices[k - 1]!.byteLength;
      const continuation = concat([tracker.init!, leads[k]!, ...slices.slice(k)]);
      const cont = walk(continuation);
      const expected = whole.blocks.filter((b) => b.at + 3 + b.size > offset);
      expect(cont.blocks.map((b) => b.t)).toEqual(expected.map((b) => b.t));
      expect(cont.blocks.map((b) => b.size)).toEqual(expected.map((b) => b.size));
    }
  });

  it('knows nothing about a stream that is not WebM, and says so', () => {
    const tracker = new WebmCutTracker();
    tracker.feed(new Uint8Array([0, 0, 0, 0x20, 0x66, 0x74, 0x79, 0x70])); // an MP4 'ftyp'
    tracker.feed(new Uint8Array(100));
    expect(tracker.init).toBeNull();
    expect(tracker.lead()).toBeNull();
  });
});
