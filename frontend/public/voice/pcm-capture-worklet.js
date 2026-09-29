/* global sampleRate, currentTime, registerProcessor, AudioWorkletProcessor */
/**
 * techsara-pcm-capture: the live-dictation tap (2026-09-29).
 *
 * An AudioWorkletProcessor fed by the SAME MediaStreamAudioSourceNode as the
 * composer's level meter (components/useVoiceRecorder.ts builds both on one
 * AudioContext per recording). It turns whatever the context runs at, 48 kHz
 * in every Chromium measured for this feature and 44.1 kHz on some Macs and
 * phones, into what the streaming speech engine takes: 16 kHz mono signed
 * 16-bit PCM in 40 ms frames of 640 samples. The context keeps the device's
 * rate on purpose: Firefox refuses a MediaStreamSource on a context whose
 * rate differs from the track's, so the conversion has to happen here.
 *
 * WHY A FILTER, NOT EVERY THIRD SAMPLE. Keeping every third sample of 48 kHz
 * without a low-pass folds everything between 8 and 24 kHz back into the
 * speech band: a 12 kHz whistle comes back as a full-level 4 kHz tone, and the
 * hiss of every "s" lands where the engine listens hardest. The resampler is
 * one Kaiser-windowed sinc low-pass (6 dB down at 7 kHz, 60 dB design
 * stopband from 8 kHz) evaluated only at the output instants. Measured with
 * this code on sines (tests/voice-live-worklet.test.ts holds the bounds):
 * within 0.01 dB of the input level up to 6 kHz, -62.5 to -63 dB at 8.5 kHz,
 * and -71 dB (48 kHz) or -75 dB (44.1 kHz) at 12 kHz. At 48 kHz that is exactly
 * "filter, then keep every third"; at any other rate it is the same filter
 * read at a fractional input position. The position is kept as an exact
 * fraction (input rate over 16000, reduced) and carried across render quanta,
 * so an hour of 44.1 kHz does not drift by a single sample.
 *
 * TIME. Output sample n is the input instant n x rate / 16000: the first
 * sample the node hears is sample 0, and the filter's look-ahead (about 1 ms)
 * is the only delay it adds. After the first quantum, a quantum that arrives
 * with no channels (a source that went quiet by going away) counts as
 * silence, so sample numbers keep standing for time.
 *
 * WHAT IS POSTED to the page:
 *   {type: 'start', contextTime, sampleRate}  once, with the first quantum
 *       that has input: the context time of sample 0 and the rate resampled.
 *   {type: 'frame', index, samples}  640 samples as the TRANSFERRED
 *       ArrayBuffer of an Int16Array (the page owns it, nothing is copied);
 *       `index` is the 16 kHz number of its first sample, without gaps.
 *   {type: 'flushed', end}  the answer to {type: 'flush'}: every sample up to
 *       `end` has been posted, the last frame possibly shorter than 640.
 * It takes {type: 'flush'} (Stop: emit the partial last frame, reading the
 * look-ahead past the final sample as silence, then end) and {type: 'stop'}
 * (cancel: end without another frame). Ended, `process` returns false so the
 * browser may drop the node.
 *
 * Plain JavaScript, served as-is from /public: a worklet is loaded by URL
 * (audioWorklet.addModule), with no bundler in between. A worklet script is a
 * module, so the exports are legal and ignored; they exist so that
 * tests/voice-live-worklet.test.ts can drive the real resampler and processor
 * under vitest with the worklet's globals stubbed.
 */

/** The engine's rate, and the wire protocol's. */
export const TARGET_RATE = 16000;
/** 40 ms at 16 kHz: one WebSocket message. */
export const FRAME_SAMPLES = 640;
/** A render quantum, for a quantum that arrives without channels. */
const QUANTUM = 128;
/**
 * The low-pass, as fractions of the slower of the two rates: a 7 kHz edge and
 * a 6-8 kHz transition at a 16 kHz output. Nothing above 8 kHz can survive a
 * 16 kHz stream anyway; what matters is that it does not fold back.
 */
const CUTOFF = 0.4375;
const TRANSITION = 0.125;
const STOPBAND_DB = 60;
/**
 * A rate whose fraction has more steps than this (none of the usual ones:
 * 44.1 kHz has 160, 22.05 kHz 320) reads the nearest of this many
 * precomputed positions, under a thousandth of a sample out.
 */
const MAX_PHASES = 512;

function gcd(a, b) {
  while (b) {
    const t = a % b;
    a = b;
    b = t;
  }
  return a;
}

/** The zeroth-order modified Bessel function, by its power series: the Kaiser window's shape. */
function besselI0(x) {
  const q = (x * x) / 4;
  let term = 1;
  let sum = 1;
  for (let k = 1; k < 100 && term > sum * 1e-15; k += 1) {
    term *= q / (k * k);
    sum += term;
  }
  return sum;
}

/**
 * The filter taps for every fractional position. Row `k` is the kernel for an
 * output instant k/phases of the way between two input samples; tap j reads
 * input sample (whole - half + 1 + j). Each row is scaled to sum to exactly 1,
 * so a steady level comes out at the level it went in, whatever the phase.
 */
function buildTable(phases, half, fc, beta) {
  const taps = 2 * half;
  const norm = besselI0(beta);
  const table = [];
  for (let k = 0; k < phases; k += 1) {
    const phase = k / phases;
    const row = new Float32Array(taps);
    let sum = 0;
    for (let j = 0; j < taps; j += 1) {
      const t = phase + half - 1 - j; // distance from the output instant, in input samples
      const x = t / half;
      const window = Math.abs(x) >= 1 ? 0 : besselI0(beta * Math.sqrt(1 - x * x)) / norm;
      const arg = 2 * fc * t;
      const sinc = arg === 0 ? 1 : Math.sin(Math.PI * arg) / (Math.PI * arg);
      row[j] = 2 * fc * sinc * window;
      sum += row[j];
    }
    for (let j = 0; j < taps; j += 1) row[j] /= sum;
    table.push(row);
  }
  return table;
}

/**
 * Streaming resampler: push input blocks of any length, get output samples
 * through `sink`, in order, as soon as the look-ahead for each has arrived.
 */
export class PcmResampler {
  constructor(inputRate, outputRate = TARGET_RATE) {
    const inRate = Math.round(inputRate);
    const outRate = Math.round(outputRate);
    if (!(inRate > 0) || !(outRate > 0)) throw new RangeError('sample rates must be positive');
    this.inputRate = inRate;
    this.outputRate = outRate;
    this.passthrough = inRate === outRate;
    const g = gcd(inRate, outRate);
    // Output n sits at input position n x p / q, held as whole + frac / q.
    this.q = outRate / g;
    const p = inRate / g;
    this.stepWhole = Math.floor(p / this.q);
    this.stepFrac = p % this.q;
    this.whole = 0;
    this.frac = 0;
    const base = Math.min(inRate, outRate);
    const fc = (CUTOFF * base) / inRate; // cycles per input sample
    const width = (TRANSITION * base) / inRate;
    // Kaiser's estimate of the taps a stopband this deep needs over this transition.
    const taps = Math.ceil((STOPBAND_DB - 8) / (2.285 * 2 * Math.PI * width)) + 1;
    this.half = Math.max(1, Math.ceil(taps / 2));
    this.phases = this.q <= MAX_PHASES ? this.q : MAX_PHASES;
    this.table = this.passthrough
      ? []
      : buildTable(this.phases, this.half, fc, 0.1102 * (STOPBAND_DB - 8.7));
    // The input still needed, from absolute index `start`. Before sample 0
    // there is silence: the first output reads half a kernel of it.
    this.buf = new Float32Array(2 * this.half + 2048);
    this.start = -(this.half - 1);
    this.len = this.half - 1;
    /** Real input samples pushed so far. */
    this.count = 0;
  }

  /** Feed one block (a Float32Array, or null for `n` samples of silence). */
  push(input, sink, n = input ? input.length : 0) {
    if (this.passthrough) {
      for (let i = 0; i < n; i += 1) sink(input ? input[i] : 0);
      this.count += n;
      return;
    }
    this.append(input, n);
    this.count += n;
    this.drain(sink, Infinity);
  }

  /** No more input: emit every output up to the last sample, reading silence past it. */
  flush(sink) {
    if (this.passthrough) return;
    this.append(null, this.half);
    this.drain(sink, this.count);
  }

  append(src, n) {
    if (this.len + n > this.buf.length) {
      // Drop what no future output reads, then grow if that was not enough.
      const first = this.whole - this.half + 1;
      const drop = Math.max(0, Math.min(this.len, first - this.start));
      this.buf.copyWithin(0, drop, this.len);
      this.start += drop;
      this.len -= drop;
      if (this.len + n > this.buf.length) {
        const bigger = new Float32Array(Math.max(this.buf.length * 2, this.len + n));
        bigger.set(this.buf.subarray(0, this.len));
        this.buf = bigger;
      }
    }
    if (src) this.buf.set(src.length === n ? src : src.subarray(0, n), this.len);
    else this.buf.fill(0, this.len, this.len + n);
    this.len += n;
  }

  drain(sink, limit) {
    const { half, table, q, phases, buf } = this;
    const taps = 2 * half;
    const newest = this.start + this.len - 1;
    while (this.whole < limit) {
      let whole = this.whole;
      let phase = this.frac;
      if (phases !== q) {
        phase = Math.round((this.frac * phases) / q);
        if (phase === phases) {
          phase = 0;
          whole += 1;
        }
      }
      if (whole + half > newest) break; // its look-ahead has not arrived yet
      const row = table[phase];
      const offset = whole - half + 1 - this.start;
      let acc = 0;
      for (let j = 0; j < taps; j += 1) acc += row[j] * buf[offset + j];
      sink(acc);
      this.whole += this.stepWhole;
      this.frac += this.stepFrac;
      if (this.frac >= q) {
        this.frac -= q;
        this.whole += 1;
      }
    }
  }
}

const Base = typeof AudioWorkletProcessor === 'function' ? AudioWorkletProcessor : class {};

function contextTime() {
  return typeof currentTime === 'number' ? currentTime : 0;
}

export class PcmCaptureProcessor extends Base {
  constructor(options) {
    super(options);
    this.rate = typeof sampleRate === 'number' && sampleRate > 0 ? sampleRate : TARGET_RATE;
    this.resampler = new PcmResampler(this.rate, TARGET_RATE);
    this.frame = new Int16Array(FRAME_SAMPLES);
    this.fill = 0;
    /** Index of the first sample of the frame being filled. */
    this.frameIndex = 0;
    this.mono = null;
    this.started = false;
    this.ended = false;
    this.sink = (value) => this.put(value);
    if (this.port) this.port.onmessage = (event) => this.command(event.data);
  }

  put(value) {
    // Clamped before it is stored: an Int16Array wraps an out-of-range value
    // around instead of saturating, and a clipped shout would become a crack.
    const scaled = Math.round(value * 32768);
    this.frame[this.fill] = scaled > 32767 ? 32767 : scaled < -32768 ? -32768 : scaled;
    this.fill += 1;
    if (this.fill === FRAME_SAMPLES) this.emit();
  }

  emit() {
    if (this.fill === 0) return;
    const pcm = this.fill === FRAME_SAMPLES ? this.frame : this.frame.slice(0, this.fill);
    const index = this.frameIndex;
    this.frameIndex += this.fill;
    // A fresh frame every time: the posted buffer is transferred, which
    // detaches it here.
    this.frame = new Int16Array(FRAME_SAMPLES);
    this.fill = 0;
    this.port.postMessage({ type: 'frame', index, samples: pcm.buffer }, [pcm.buffer]);
  }

  command(message) {
    if (this.ended || !message) return;
    if (message.type === 'flush') {
      this.resampler.flush(this.sink);
      this.emit();
      this.ended = true;
      this.port.postMessage({ type: 'flushed', end: this.frameIndex });
    } else if (message.type === 'stop') {
      this.ended = true;
    }
  }

  process(inputs, outputs) {
    if (this.ended) return false;
    const input = inputs && inputs[0];
    const channels = input ? input.length : 0;
    if (!this.started) {
      // Nothing connected yet: no sample 0, and no time passes for it.
      if (channels === 0 || !input[0]) return true;
      this.started = true;
      this.port.postMessage({ type: 'start', contextTime: contextTime(), sampleRate: this.rate });
    }
    if (channels === 0 || !input[0]) {
      const out = outputs && outputs[0] && outputs[0][0];
      this.resampler.push(null, this.sink, out ? out.length : QUANTUM);
      return true;
    }
    const n = input[0].length;
    let mono = input[0];
    if (channels > 1) {
      // The node asks the browser for one channel; anything that still
      // arrives with more is averaged, never just its left side.
      if (!this.mono || this.mono.length !== n) this.mono = new Float32Array(n);
      mono = this.mono;
      for (let i = 0; i < n; i += 1) {
        let sum = 0;
        for (let c = 0; c < channels; c += 1) sum += input[c][i];
        mono[i] = sum / channels;
      }
    }
    this.resampler.push(mono, this.sink);
    return true;
  }
}

if (typeof registerProcessor === 'function') {
  registerProcessor('techsara-pcm-capture', PcmCaptureProcessor);
}
