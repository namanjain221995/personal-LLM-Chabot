/**
 * The live-dictation capture worklet (public/voice/pcm-capture-worklet.js),
 * run for real: the file is imported with the AudioWorkletGlobalScope's
 * globals stubbed (registerProcessor, AudioWorkletProcessor, sampleRate,
 * currentTime), and the class it registers is driven one 128-sample render
 * quantum at a time, exactly as a browser drives it.
 *
 * What it must hold, and why each matters to a speech engine:
 *   - 16 kHz out of 48 kHz AND 44.1 kHz in, at the level it went in: a
 *     resampler that is only right at 48 kHz fails every Mac on 44.1;
 *   - no aliasing: a 12 kHz tone decimated without a low-pass comes back as a
 *     full-level 4 kHz one, inside the band the engine listens to hardest;
 *   - 640-sample frames whose indices never skip or repeat, across render
 *     quanta, odd block sizes and a rate whose phase is fractional: the
 *     server counts samples, and resume is by sample index;
 *   - the frame's buffer is transferred, not copied;
 *   - flush emits the partial last frame, then says where the audio ended.
 */
import { afterAll, beforeAll, describe, expect, it, vi } from 'vitest';

interface Posted {
  m: Record<string, unknown>;
  transfer?: unknown[];
}

interface FakePort {
  posted: Posted[];
  onmessage: ((event: { data: unknown }) => void) | null;
  postMessage(m: unknown, transfer?: unknown[]): void;
}

interface Processor {
  port: FakePort;
  process(inputs: Float32Array[][], outputs?: Float32Array[][]): boolean;
}

type ProcessorClass = new (options?: unknown) => Processor;

interface WorkletModule {
  PcmCaptureProcessor: ProcessorClass;
  PcmResampler: new (inputRate: number, outputRate?: number) => {
    push(input: Float32Array | null, sink: (v: number) => void, n?: number): void;
    flush(sink: (v: number) => void): void;
  };
  FRAME_SAMPLES: number;
  TARGET_RATE: number;
}

const registered = new Map<string, ProcessorClass>();
let worklet: WorkletModule;

beforeAll(async () => {
  vi.stubGlobal(
    'AudioWorkletProcessor',
    class {
      port: FakePort = {
        posted: [],
        onmessage: null,
        postMessage(m: unknown, transfer?: unknown[]) {
          this.posted.push({ m: m as Record<string, unknown>, transfer });
        },
      };
    },
  );
  vi.stubGlobal('registerProcessor', (name: string, cls: ProcessorClass) => registered.set(name, cls));
  vi.stubGlobal('sampleRate', 48000);
  vi.stubGlobal('currentTime', 1.25);
  worklet = (await import('../public/voice/pcm-capture-worklet.js')) as unknown as WorkletModule;
});

afterAll(() => {
  vi.unstubAllGlobals();
});

/** A processor at `rate`, as the browser would construct it. */
function processorAt(rate: number): Processor {
  vi.stubGlobal('sampleRate', rate);
  return new worklet.PcmCaptureProcessor({});
}

/** `seconds` of `amp`·sin(2π·freq·t) at `rate`, pushed in render quanta of `block`. */
function feed(p: Processor, rate: number, freq: number, seconds: number, amp = 0.5, block = 128): number {
  const total = Math.round(rate * seconds);
  for (let off = 0; off < total; off += block) {
    const n = Math.min(block, total - off);
    const q = new Float32Array(n);
    for (let i = 0; i < n; i += 1) q[i] = amp * Math.sin((2 * Math.PI * freq * (off + i)) / rate);
    p.process([[q]]);
  }
  return total;
}

function frames(p: Processor): Array<{ index: number; samples: Int16Array; transfer?: unknown[] }> {
  return p.port.posted
    .filter((x) => x.m.type === 'frame')
    .map((x) => ({
      index: x.m.index as number,
      samples: new Int16Array(x.m.samples as ArrayBuffer),
      transfer: x.transfer,
    }));
}

function flush(p: Processor): void {
  p.port.onmessage!({ data: { type: 'flush' } });
}

/** Every output sample as a float, checking on the way that the indices are contiguous. */
function output(p: Processor): Float32Array {
  const list = frames(p);
  let expected = 0;
  for (const f of list) {
    expect(f.index).toBe(expected);
    expected += f.samples.length;
  }
  const out = new Float32Array(expected);
  let at = 0;
  for (const f of list) {
    for (let i = 0; i < f.samples.length; i += 1) out[at + i] = f.samples[i]! / 32768;
    at += f.samples.length;
  }
  return out;
}

/** The amplitude of the `freq` component, fitted over the steady middle of `x` (16 kHz). */
function amplitudeAt(x: Float32Array, freq: number, skip = 200): number {
  let s = 0;
  let c = 0;
  let n = 0;
  for (let i = skip; i < x.length - skip; i += 1) {
    s += x[i]! * Math.sin((2 * Math.PI * freq * i) / 16000);
    c += x[i]! * Math.cos((2 * Math.PI * freq * i) / 16000);
    n += 1;
  }
  return (2 * Math.hypot(s, c)) / n;
}

function rms(x: Float32Array, skip = 200): number {
  let e = 0;
  let n = 0;
  for (let i = skip; i < x.length - skip; i += 1) {
    e += x[i]! * x[i]!;
    n += 1;
  }
  return Math.sqrt(e / n);
}

const dB = (ratio: number) => 20 * Math.log10(ratio);

describe('the capture worklet as the browser loads it', () => {
  it('registers techsara-pcm-capture, the class it exports', () => {
    expect(registered.get('techsara-pcm-capture')).toBe(worklet.PcmCaptureProcessor);
    expect(worklet.FRAME_SAMPLES).toBe(640);
    expect(worklet.TARGET_RATE).toBe(16000);
  });

  it('says nothing and counts nothing until a source is connected', () => {
    const p = processorAt(48000);
    expect(p.process([[]])).toBe(true);
    expect(p.process([])).toBe(true);
    expect(p.port.posted).toEqual([]);
  });
});

describe('48 kHz and 44.1 kHz in, 16 kHz out', () => {
  it.each([
    [48000, 440],
    [48000, 1000],
    [44100, 440],
    [44100, 3000],
  ])('%i Hz: a %i Hz sine comes out at 16 kHz, in 640-sample frames, at the level it went in', (rate, freq) => {
    const p = processorAt(rate);
    const total = feed(p, rate, freq, 2);
    const start = p.port.posted[0]!.m;
    // The first thing posted is where sample 0 sits on the context's clock.
    expect(start).toEqual({ type: 'start', contextTime: 1.25, sampleRate: rate });
    const before = frames(p);
    // Every frame while recording is exactly 40 ms.
    expect(before.every((f) => f.samples.length === 640)).toBe(true);
    flush(p);
    const out = output(p);
    // Output n sits at input n·rate/16000, so exactly this many fall inside the input.
    expect(out.length).toBe(Math.floor(((total - 1) * 16000) / rate) + 1);
    // The level is kept: within 0.01 dB of the 0.5 that went in.
    expect(Math.abs(dB(amplitudeAt(out, freq) / 0.5))).toBeLessThan(0.01);
    // And it IS that sine, sample for sample, not merely something as loud.
    let worst = 0;
    for (let i = 200; i < out.length - 200; i += 1) {
      worst = Math.max(worst, Math.abs(out[i]! - 0.5 * Math.sin((2 * Math.PI * freq * i) / 16000)));
    }
    expect(worst).toBeLessThan(0.002);
  });

  it.each([48000, 44100])('%i Hz: speech-band content up to 6 kHz passes within 0.02 dB', (rate) => {
    const p = processorAt(rate);
    feed(p, rate, 6000, 1);
    flush(p);
    expect(Math.abs(dB(amplitudeAt(output(p), 6000) / 0.5))).toBeLessThan(0.02);
  });

  it.each([
    [48000, 12000],
    [44100, 12000],
    [48000, 8500],
    [44100, 8500],
  ])('%i Hz: a %i Hz tone does not fold back into the speech band', (rate, freq) => {
    // Without the low-pass, 12 kHz at 48 kHz decimated by 3 is a 4 kHz tone at
    // FULL level (and 8.5 kHz a 7.5 kHz one). Measured with this code: -71 dB
    // and -75 dB at 12 kHz, -63 dB and -62.5 dB at 8.5 kHz.
    const p = processorAt(rate);
    feed(p, rate, freq, 1);
    flush(p);
    const out = output(p);
    expect(dB(rms(out) / (0.5 * Math.SQRT1_2))).toBeLessThan(-60);
    // Nothing of it at the alias frequency either.
    const alias = Math.abs(16000 - freq) % 16000;
    expect(amplitudeAt(out, alias)).toBeLessThan(0.0005);
  });

  it('keeps the fractional phase across render quanta and odd block sizes', () => {
    // 44.1 kHz has 160 phases; a resampler that restarted its phase per
    // quantum would drift and click. The same second of audio pushed as 128s,
    // as 37s and as one block must give identical frames.
    const runs = [128, 37, 44100].map((block) => {
      const p = processorAt(44100);
      feed(p, 44100, 997, 1, 0.5, block);
      flush(p);
      return output(p);
    });
    expect(runs[1]).toEqual(runs[0]);
    expect(runs[2]).toEqual(runs[0]);
  });

  it('holds its phase over ten minutes of 44.1 kHz without drifting a sample', () => {
    // An exact fraction, not a float step: after 600 s of input the output
    // count is exactly 16000 per second, and so it is after any length.
    const r = new worklet.PcmResampler(44100);
    let produced = 0;
    const block = new Float32Array(44100);
    for (let s = 0; s < 600; s += 1) r.push(block, () => (produced += 1));
    r.flush(() => (produced += 1));
    expect(produced).toBe(600 * 16000);
  });

  it('passes 16 kHz through untouched', () => {
    const p = processorAt(16000);
    const q = new Float32Array(128).map((_, i) => (i - 64) / 128);
    for (let k = 0; k < 10; k += 1) p.process([[q]]);
    flush(p);
    const out = output(p);
    expect(out.length).toBe(1280);
    for (let i = 0; i < 128; i += 1) expect(out[i]).toBeCloseTo(Math.round(q[i]! * 32768) / 32768, 6);
  });
});

describe('the frames the page receives', () => {
  it('transfers each frame’s buffer and numbers frames without a gap', () => {
    const p = processorAt(48000);
    feed(p, 48000, 440, 1);
    const list = frames(p);
    expect(list.length).toBe(24); // 1 s is 25 frames; the last waits for its look-ahead
    list.forEach((f, k) => {
      expect(f.index).toBe(k * 640);
      expect(f.samples.byteLength).toBe(1280);
      // The posted buffer is in the transfer list: moved, not copied.
      expect(f.transfer).toHaveLength(1);
      expect(f.transfer![0]).toBe(p.port.posted.find((x) => x.m.index === f.index)!.m.samples);
    });
  });

  it('averages every channel into one, rather than keeping the left', () => {
    const p = processorAt(48000);
    for (let off = 0; off < 48000; off += 128) {
      const left = new Float32Array(128);
      const right = new Float32Array(128);
      for (let i = 0; i < 128; i += 1) left[i] = 0.5 * Math.sin((2 * Math.PI * 1000 * (off + i)) / 48000);
      p.process([[left, right]]);
    }
    flush(p);
    // Half the level: (L + 0) / 2.
    expect(amplitudeAt(output(p), 1000)).toBeCloseTo(0.25, 3);
  });

  it('clamps a signal past full scale instead of wrapping it into a crack', () => {
    const p = processorAt(16000);
    for (let k = 0; k < 5; k += 1) p.process([[new Float32Array(128).fill(k % 2 ? -3 : 3)]]);
    flush(p);
    const values = frames(p).flatMap((f) => [...f.samples]);
    expect(Math.max(...values)).toBe(32767);
    expect(Math.min(...values)).toBe(-32768);
    expect(values.every((v) => v === 32767 || v === -32768)).toBe(true);
  });

  it('counts a quantum with no channels after the first as silence, so sample numbers stay time', () => {
    const p = processorAt(16000);
    p.process([[new Float32Array(128).fill(0.25)]]);
    p.process([[]], [[new Float32Array(128)]]);
    p.process([[new Float32Array(128).fill(0.25)]]);
    flush(p);
    const out = output(p);
    expect(out.length).toBe(384);
    expect(out[127]).toBeCloseTo(0.25, 4);
    expect(out[128]).toBe(0);
    expect(out[255]).toBe(0);
    expect(out[256]).toBeCloseTo(0.25, 4);
  });
});

describe('flush and stop', () => {
  it('flush emits the short last frame, then where the audio ended, then nothing more', () => {
    const p = processorAt(48000);
    feed(p, 48000, 440, 0.1); // 4,800 samples in -> 1,600 out: two full frames and 320
    flush(p);
    const list = frames(p);
    expect(list.map((f) => f.samples.length)).toEqual([640, 640, 320]);
    const last = p.port.posted[p.port.posted.length - 1]!.m;
    expect(last).toEqual({ type: 'flushed', end: 1600 });
    // Ended: the browser may drop the node, and nothing else is posted.
    const posted = p.port.posted.length;
    expect(p.process([[new Float32Array(128)]])).toBe(false);
    flush(p);
    expect(p.port.posted.length).toBe(posted);
  });

  it('stop ends it without another frame', () => {
    const p = processorAt(48000);
    feed(p, 48000, 440, 0.05);
    const before = p.port.posted.length;
    p.port.onmessage!({ data: { type: 'stop' } });
    expect(p.process([[new Float32Array(128)]])).toBe(false);
    flush(p);
    expect(p.port.posted.length).toBe(before);
  });
});
