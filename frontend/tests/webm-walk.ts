/**
 * WebM helpers for the voice tests: a strict walker and the real Chrome
 * recording kept in tests/fixtures/chrome151-webm-opus-1s (see
 * voice-webm-cuts.test.ts for what was measured on it).
 */
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

const DIR = join(__dirname, 'fixtures', 'chrome151-webm-opus-1s');
/** Four 1 s timeslices, byte for byte as Chrome 151's MediaRecorder handed them over. */
export const chromeSlices: Uint8Array[] = [0, 1, 2, 3].map(
  (i) => new Uint8Array(readFileSync(join(DIR, `slice00${i}.bin`))),
);

export function concat(parts: Uint8Array[]): Uint8Array {
  const out = new Uint8Array(parts.reduce((n, p) => n + p.byteLength, 0));
  let at = 0;
  for (const p of parts) {
    out.set(p, at);
    at += p.byteLength;
  }
  return out;
}

/**
 * A strict WebM walker: every element must parse where it starts. Returns
 * the blocks with their absolute times (cluster timecode + block offset).
 */
export function walk(data: Uint8Array): { clusters: number[]; blocks: Array<{ at: number; size: number; t: number }> } {
  const vint = (i: number, keep: boolean) => {
    const f = data[i]!;
    let len = 1;
    while (len <= 8 && !(f & (0x80 >> (len - 1)))) len += 1;
    if (len > 8) throw new Error(`no element at ${i}`);
    let v = keep ? f : f & (0xff >> len);
    let ones = (f & (0xff >> len)) === 0xff >> len;
    for (let k = 1; k < len; k += 1) {
      v = v * 256 + data[i + k]!;
      if (data[i + k] !== 0xff) ones = false;
    }
    return { v, len, unknown: !keep && ones };
  };
  const clusters: number[] = [];
  const blocks: Array<{ at: number; size: number; t: number }> = [];
  let i = 0;
  let tc = 0;
  let inSegment = false;
  // The recording was cut mid-stream, so its last element may be incomplete
  // (it is: the fixture's last byte is the ID of the next block). Only that
  // one, at the very end, is allowed to be; anything else must parse.
  const complete = (at: number, need: number) => at + need <= data.length;
  while (i < data.length) {
    const lenOf = (b: number) => {
      let len = 1;
      while (len <= 8 && !(b & (0x80 >> (len - 1)))) len += 1;
      return len;
    };
    const idLen = lenOf(data[i]!);
    if (!complete(i, idLen + 1) || !complete(i, idLen + lenOf(data[i + idLen]!))) break;
    const id = vint(i, true);
    const size = vint(i + id.len, false);
    const payload = i + id.len + size.len;
    if (id.v === 0x18538067) {
      inSegment = true;
      i = payload;
      continue;
    }
    if (id.v === 0x1f43b675) {
      clusters.push(i);
      i = payload;
      continue;
    }
    if (size.unknown) throw new Error(`unknown size for ${id.v.toString(16)} at ${i}`);
    if (id.v === 0xe7) {
      tc = 0;
      for (let k = 0; k < size.v; k += 1) tc = tc * 256 + data[payload + k]!;
    }
    if (id.v === 0xa3) {
      if (!inSegment || clusters.length === 0) throw new Error(`block outside a cluster at ${i}`);
      // track number vint (1 byte here), then a signed 16-bit offset
      const rel = (data[payload + 1]! << 8) | data[payload + 2]!;
      blocks.push({ at: i, size: size.v, t: tc + (rel > 0x7fff ? rel - 0x10000 : rel) });
    }
    if (payload + size.v > data.length) {
      if (id.v === 0xa3) blocks.pop();
      break;
    }
    i = payload + size.v;
  }
  return { clusters, blocks };
}

