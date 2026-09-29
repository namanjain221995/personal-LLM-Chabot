/**
 * Live dictation: the words while they are being spoken (2026-09-29).
 *
 * THE STORED RECORDING STAYS THE TRUTH. A session recording (lib/voice.ts)
 * uploads Opus in 5 s parts and whisper transcribes it in windows cut at
 * pauses, so its preview arrives in steps of five seconds or more, paced by
 * the server's voice-activity detector and by chat. That path is untouched,
 * and its transcript is what goes into the draft, except for a Hindi or
 * Hinglish session whose live transcript heard all of it (chooseFinalText:
 * whisper transcribes those twice as badly). This module is a second,
 * disposable channel beside it: the same microphone, tapped by an AudioWorklet
 * (public/voice/pcm-capture-worklet.js) as 16 kHz PCM, streamed over one
 * WebSocket to a streaming recogniser that answers with a PARTIAL hypothesis a
 * few hundred milliseconds after the words, and a FINAL one per utterance once
 * the speaker pauses. Anything here may fail at any moment; the recording, its
 * upload and its transcript do not notice.
 *
 * WHAT IS HERE, IN ORDER.
 *   - the language preference the start message carries;
 *   - PcmRing: the last 75 s of PCM (a minute of replay and its headroom), so
 *     a dropped connection resumes with the words spoken while it was down
 *     instead of losing them;
 *   - CaptureClock: when a sample was captured, for the clock offset that
 *     maps live samples onto the stored recording, and for the capture-to-
 *     screen latency the server is told about;
 *   - LiveTranscript: what has been heard (a partial replaces, a final
 *     commits, late and repeated events are ignored);
 *   - mergeLiveTranscript: the durable preview and the live words as one text;
 *   - chooseFinalText: which of the two transcripts goes into the draft at
 *     Stop (the live one for Hindi and Hinglish, when it heard everything and
 *     the English-only model wrote none of it);
 *   - attachPcmTap: the worklet on the meter's own AudioContext;
 *   - LiveStream: the socket (subprotocol techsara.voice.v1, reconnect with
 *     resume, backpressure, ping, client_stats, a language change);
 *   - LiveCapture: the one object the recorder hook holds.
 *
 * Pure TypeScript with no React, like lib/voice.ts, so every rule above is
 * unit-testable without a DOM. Nothing touches a browser global at import
 * time: the composer renders on the server too.
 */

import { joinPreview, type DraftSpan, type LiveConfig, type SessionProgress } from '@/lib/voice';

/** The engine's rate and the wire's: 16 samples per millisecond. */
export const LIVE_SAMPLE_RATE = 16000;
const SAMPLES_PER_MS = LIVE_SAMPLE_RATE / 1000;
/** One message: 40 ms. */
export const LIVE_FRAME_SAMPLES = 640;
export const LIVE_SUBPROTOCOL = 'techsara.voice.v1';
/** Served from frontend/public as it is: a worklet module is loaded by URL. */
export const LIVE_WORKLET_URL = '/voice/pcm-capture-worklet.js';
export const LIVE_PROCESSOR = 'techsara-pcm-capture';
/**
 * The furthest back a reconnect replays: the server's resume_max_s, which its
 * arrival ceiling (resume_max_s + 5 s of audio ahead of the wall clock) is
 * built on. A smaller resume_max_s from the config is honoured.
 */
export const LIVE_RESUME_MAX_SECONDS = 60;
/**
 * THE RING IS LONGER THAN THE REPLAY (review, 2026-09-30). The resume point is
 * chosen when the socket opens and read when `ready` comes back, and the
 * microphone keeps filling the ring in between. With the ring exactly as long
 * as the replay (60 s each), a reconnect a minute after the last final chose
 * the ring's OLDEST sample, the next 40 ms frame overwrote it, and the stream
 * reconnected at once, forever: in headless Chromium 117 sockets and 0 audio
 * frames in 20 s, with each attempt admitted by the gateway as an engine
 * stream. These 15 s behind the resume point cover the handshake (the engine
 * connect alone may take 5 s) many times over.
 */
export const LIVE_RESUME_HEADROOM_SECONDS = 15;
/** How much PCM the browser keeps: the longest replay and its headroom (2.4 MB). */
export const LIVE_RING_SECONDS = LIVE_RESUME_MAX_SECONDS + LIVE_RESUME_HEADROOM_SECONDS;
/**
 * Backpressure. Past 256 KiB queued in the socket (about 8 s of PCM) nothing
 * more is handed to it; the frames wait in the ring, and sending resumes once
 * the queue is below 64 KiB. Without a ceiling a slow uplink would queue the
 * whole recording inside the socket, where a reconnect cannot replay it from.
 */
export const LIVE_HIGH_WATER_BYTES = 256 * 1024;
export const LIVE_LOW_WATER_BYTES = 64 * 1024;
/** After Stop, the live stream gets this long to deliver its last words. */
export const LIVE_FINISH_BUDGET_MS = 3000;
/**
 * Once the full pass is in, how much longer its insert may wait for the live
 * stream's last words, and only while they could still decide which
 * transcript goes in (`liveMayStillBeChosen`). A stream not done by then
 * counts as incomplete, and the full pass goes in. Waiting out the whole
 * finish budget held every insert up to 3 s behind a stream slow to say
 * `done` (review, 2026-09-30).
 */
export const LIVE_SETTLE_WAIT_MS = 1000;
/**
 * A language chosen mid-recording replaces the connection only once no other
 * choice came for this long (`LiveStream.setLanguage`).
 */
export const LIVE_LANGUAGE_DEBOUNCE_MS = 500;
/** How long the worklet may take to hand over its last partial frame. */
export const LIVE_TAP_FLUSH_MS = 300;
export const LIVE_STATS_INTERVAL_MS = 5000;
export const LIVE_PING_INTERVAL_MS = 10_000;
/** A ping answered by nothing at all for this long: the connection is dead. */
export const LIVE_PONG_TIMEOUT_MS = 10_000;
/**
 * A connection has proven itself once it brought back words or stayed up this
 * long; only then does the reconnect backoff start over. Resetting it on
 * `ready` alone let a gateway that accepts every stream and fails it at once
 * (4503 right after ready) be retried every 0.4-0.6 s for the whole
 * recording: 94 connections in a minute in the review's probe.
 */
export const LIVE_PROVEN_AFTER_MS = 10_000;
/** The panel redraws the live words at most ten times a second. */
export const LIVE_RENDER_INTERVAL_MS = 100;
/** At most this many latencies per list in one client_stats message. */
const STATS_MAX = 64;
/** Reconnect schedule: 0.5, 1, 2, 4, 8 s, then every 15 s, each ±20%. */
const RECONNECT_DELAYS_MS = [500, 1000, 2000, 4000, 8000];
const RECONNECT_CEILING_MS = 15_000;
const DRAIN_CHECK_MS = 50;
const TICK_MS = 1000;
/** WebSocket.OPEN, spelled out so a test double need not carry the constant. */
const OPEN = 1;
/**
 * Closes that retrying cannot fix: bad protocol, signed out, voice off, no
 * such session or no live path, superseded by another connection, a frame too
 * large. Every other close (1006 dropped, 1012 server restart, 4408 idle,
 * 4429 busy, 4500, 4503) is retried with backoff while the recording lasts.
 */
const TERMINAL_CLOSES: ReadonlySet<number> = new Set([4400, 4401, 4403, 4404, 4409, 4413]);
/** Text the server may send per event; longer is cut, never trusted. */
const MAX_EVENT_TEXT = 4000;
/**
 * The button that puts the live words into the draft when the stored
 * recording's transcript could not be had (components/useVoiceRecorder.ts),
 * when they are the whole recording (`LiveCapture.complete`).
 */
export const LIVE_INSERT_LABEL = 'Insert live transcript';
/**
 * The same button when the live stream missed part of the recording: refused
 * mid-way, a hole in the ring, or a resume that skipped audio. The review
 * inserted "First minute only." out of a recording twice that long under the
 * plain label, beside a line saying no words could be made out.
 */
export const LIVE_INSERT_PARTIAL_LABEL = 'Insert what was heard live (part of the recording)';
/** The quiet line after Stop that says which of the two transcripts went in. */
export const LIVE_INSERTED_LIVE = 'Inserted the live transcript.';
export const LIVE_INSERTED_FULL_PASS = 'Inserted the full-pass transcript.';
/** Its button: the other transcript, in the same place. */
export const LIVE_SWAP_LABEL = 'Use the other one';
/** Its button when the other one is a live transcript that missed part of the recording. */
export const LIVE_SWAP_PARTIAL_LABEL = 'Use what was heard live (part of the recording)';
/** Said instead of swapping once the person has changed the inserted words. */
export const LIVE_SWAP_EDITED = 'The inserted words have been edited, so they were left as they are.';

// ---------------------------------------------------------------------------
// The language the engine is asked for
// ---------------------------------------------------------------------------

/**
 * Per browser, not per account: it describes the person at this microphone.
 * "en" routes to an English-only model, which transcribed LibriSpeech at
 * 4.13% WER against 6.23% for the multilingual one; "auto" and "hi" need the
 * multilingual one. Gujarati is not offered: neither model can transcribe it
 * (FLEURS-gu WER 104%, measured 2026-09-29). The recording bar's language
 * control (components/VoiceBar.tsx) reads and writes it through these.
 */
export type VoiceLanguage = 'auto' | 'en' | 'hi';
export const VOICE_LANGUAGES: readonly VoiceLanguage[] = ['auto', 'en', 'hi'];
export const VOICE_LANGUAGE_KEY = 'techsara-voice-language';

type PrefStorage = Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>;

function localPrefs(): PrefStorage | null {
  try {
    return typeof window !== 'undefined' && window.localStorage ? window.localStorage : null;
  } catch {
    return null; // a sandboxed frame throws on the property itself
  }
}

function asVoiceLanguage(value: unknown): VoiceLanguage | null {
  if (typeof value !== 'string') return null;
  const v = value.trim().toLowerCase();
  return (VOICE_LANGUAGES as readonly string[]).includes(v) ? (v as VoiceLanguage) : null;
}

/** The stored preference; anything unreadable or unknown is "auto". */
export function getVoiceLanguage(storage: PrefStorage | null = localPrefs()): VoiceLanguage {
  try {
    return asVoiceLanguage(storage?.getItem(VOICE_LANGUAGE_KEY)) ?? 'auto';
  } catch {
    return 'auto';
  }
}

/** Remember a preference; "auto" (or anything unknown) removes it. */
export function setVoiceLanguage(language: string, storage: PrefStorage | null = localPrefs()): void {
  try {
    if (!storage) return;
    const known = asVoiceLanguage(language);
    if (known && known !== 'auto') storage.setItem(VOICE_LANGUAGE_KEY, known);
    else storage.removeItem(VOICE_LANGUAGE_KEY);
  } catch {
    /* quota, or a private window: the preference is a convenience */
  }
}

// ---------------------------------------------------------------------------
// The socket's address
// ---------------------------------------------------------------------------

/** The one path shape the frontend's WebSocket relay forwards. */
const RELAYED_PATH = /^\/api\/audio\/sessions\/[A-Za-z0-9_-]{1,64}\/live$/;

/**
 * `ws(s)://<this host><path>` for a session, or null. Same origin only: the
 * page's CSP allows a socket to 'self' and nothing else, and the relay owns
 * exactly RELAYED_PATH, so a config pointing anywhere else cannot work and is
 * not tried.
 */
export function liveSocketUrl(
  config: LiveConfig,
  sessionId: string,
  where: { protocol: string; host: string } | null = typeof location !== 'undefined' ? location : null,
): string | null {
  if (!where || !where.host) return null;
  const path = config.path.split('{id}').join(encodeURIComponent(sessionId));
  if (!RELAYED_PATH.test(path)) return null;
  return `${where.protocol === 'https:' ? 'wss' : 'ws'}://${where.host}${path}`;
}

// ---------------------------------------------------------------------------
// The last minute of PCM
// ---------------------------------------------------------------------------

/**
 * A circular buffer of 16 kHz samples addressed by their absolute index
 * (sample 0 is the first the worklet produced). `start` is the oldest index
 * still held and `end` one past the newest.
 */
export class PcmRing {
  readonly capacity: number;
  private buf: Int16Array;
  start = 0;
  end = 0;
  /** Samples written over before anything read them: the ring overflowed. */
  overwritten = 0;

  constructor(seconds = LIVE_RING_SECONDS) {
    this.capacity = Math.max(LIVE_FRAME_SAMPLES, Math.round(seconds * LIVE_SAMPLE_RATE));
    this.buf = new Int16Array(this.capacity);
  }

  /** Add samples that start at `index`. The worklet's indices are contiguous. */
  push(index: number, samples: Int16Array): void {
    if (this.buf.length === 0) return; // cleared: this recording has no live path
    let from = index;
    let data = samples;
    if (from < this.end) {
      // Overlap (a repeated frame): keep what is already held.
      const skip = this.end - from;
      if (skip >= data.length) return;
      data = data.subarray(skip);
      from = this.end;
    } else if (from > this.end) {
      // A hole: nothing before it can be sent contiguously with what follows.
      this.start = from;
      this.end = from;
    }
    if (data.length > this.capacity) {
      from += data.length - this.capacity;
      data = data.subarray(data.length - this.capacity);
      this.start = from;
    }
    const pos = from % this.capacity;
    const first = Math.min(data.length, this.capacity - pos);
    this.buf.set(data.subarray(0, first), pos);
    if (first < data.length) this.buf.set(data.subarray(first), 0);
    this.end = from + data.length;
    if (this.end - this.start > this.capacity) {
      this.overwritten += this.end - this.capacity - this.start;
      this.start = this.end - this.capacity;
    }
  }

  /** A copy of up to `max` samples from `from`, or null when `from` is no longer held. */
  read(from: number, max: number): Int16Array | null {
    if (this.buf.length === 0 || from < this.start || from > this.end) return null;
    const n = Math.max(0, Math.min(max, this.end - from));
    const out = new Int16Array(n);
    const pos = from % this.capacity;
    const first = Math.min(n, this.capacity - pos);
    out.set(this.buf.subarray(pos, pos + first));
    if (first < n) out.set(this.buf.subarray(0, n - first), first);
    return out;
  }

  /** Let the audio go (no live stream on this recording after all, or it ended). */
  clear(): void {
    this.buf = new Int16Array(0);
    this.start = this.end;
  }
}

// ---------------------------------------------------------------------------
// When a sample was captured
// ---------------------------------------------------------------------------

/** How far back the clock looks for its best estimate: ten seconds of frames. */
const CLOCK_WINDOW_MS = 10_000;

/**
 * performance.now() of a sample, from when the frames arrived.
 *
 * A frame is posted when its last sample leaves the worklet, about one render
 * quantum (128 samples, 2.7 ms at 48 kHz) after that sample was captured, and
 * arrives a moment later unless the page's main thread was busy. So every
 * frame implies a time for sample 0 that is never too EARLY, and a stalled
 * frame implies one that is late by the stall. The clock keeps the earliest
 * such time of the last ten seconds (a sliding minimum): a render that froze
 * the page for half a second does not move it, and the audio clock's drift
 * against performance.now() (well under a millisecond in ten seconds) is
 * forgotten as the window moves. Good to a few milliseconds, which both uses
 * need: the clock offset places utterances (seconds long) against the stored
 * recording, and latencies are reported in tens of milliseconds.
 */
export class CaptureClock {
  private quantumMs: number;
  /** Increasing `zero` values with their arrival times: the window's minimum is first. */
  private readonly window: Array<{ zero: number; at: number }> = [];

  constructor(contextRate = 48000) {
    this.quantumMs = (128 / contextRate) * 1000;
  }

  /** The context's real rate, once the worklet has said it. */
  setRate(contextRate: number): void {
    if (contextRate > 0 && Number.isFinite(contextRate)) this.quantumMs = (128 / contextRate) * 1000;
  }

  note(index: number, length: number, arrivedAt: number): void {
    if (length <= 0 || !Number.isFinite(arrivedAt)) return;
    const zero = arrivedAt - this.quantumMs - (index + length - 1) / SAMPLES_PER_MS;
    const w = this.window;
    while (w.length > 0 && w[w.length - 1]!.zero >= zero) w.pop();
    w.push({ zero, at: arrivedAt });
    while (w.length > 1 && w[0]!.at < arrivedAt - CLOCK_WINDOW_MS) w.shift();
  }

  /** True once a frame has been timed. */
  get running(): boolean {
    return this.window.length > 0;
  }

  /** When `sample` was captured, or null before the first frame. */
  timeOf(sample: number): number | null {
    const best = this.window[0];
    return best ? best.zero + sample / SAMPLES_PER_MS : null;
  }
}

// ---------------------------------------------------------------------------
// What has been heard
// ---------------------------------------------------------------------------

export interface LiveUtterance {
  u: number;
  text: string;
  startSample: number;
  endSample: number;
}

/**
 * The live transcript: committed utterances in u order, and at most one
 * partial. A partial is the WHOLE current hypothesis of its utterance, so it
 * replaces (never appends). A final commits its utterance and clears that
 * partial. Anything for an utterance already committed is late or repeated
 * (a replay after a reconnect) and is ignored, so no word is shown twice.
 */
export class LiveTranscript {
  private readonly list: LiveUtterance[] = [];
  partial: LiveUtterance | null = null;
  /** The highest utterance committed; -1 before the first. */
  lastU = -1;
  /** One past the last sample a final covered: where a resume starts. */
  committedUntil = 0;
  /** Bumped on every change, so a view can tell it is stale. */
  version = 0;

  get committed(): readonly LiveUtterance[] {
    return this.list;
  }

  /** The number the next utterance takes, carried across reconnects. */
  get nextU(): number {
    return this.lastU + 1;
  }

  final(u: number, text: string, startSample: number, endSample: number): boolean {
    if (u <= this.lastU) return false;
    const words = text.trim();
    // "An utterance with no words produces no final"; one that says so anyway
    // still moves the numbering on.
    if (words) this.list.push({ u, text: words, startSample, endSample });
    this.lastU = u;
    this.committedUntil = Math.max(this.committedUntil, endSample);
    if (this.partial && this.partial.u <= u) this.partial = null;
    this.version += 1;
    return true;
  }

  partialUpdate(u: number, text: string, startSample: number, endSample: number): boolean {
    if (u <= this.lastU) return false;
    if (this.partial && u < this.partial.u) return false;
    const words = text.trim();
    const next = words ? { u, text: words, startSample, endSample } : null;
    if (
      next &&
      this.partial &&
      this.partial.u === u &&
      this.partial.text === words &&
      this.partial.endSample === endSample
    ) {
      return false;
    }
    if (!next && !this.partial) return false;
    this.partial = next;
    this.version += 1;
    return true;
  }

  /** Everything heard, as one text: what "Insert live transcript" puts in the draft. */
  text(): string {
    const pieces = this.list.map((utterance) => utterance.text);
    if (this.partial) pieces.push(this.partial.text);
    return joinPieces(pieces).trim();
  }
}

/**
 * Pieces of transcript joined exactly as folding `joinPreview` over them
 * would join them, in one pass. Its separator depends only on the character
 * before it and the one after it, so each is decided from the two
 * neighbouring pieces alone. The fold ran joinPreview's end-of-text test on
 * everything joined so far at every step: at Stop, an hour of dictation
 * (10,000 utterances, 216,659 characters) held the main thread for a second
 * (review, 2026-09-30).
 */
export function joinPieces(pieces: readonly string[]): string {
  const out: string[] = [];
  let previous = '';
  for (const piece of pieces) {
    if (!piece) continue;
    if (previous) out.push(spaceBetween(previous, piece));
    out.push(piece);
    previous = piece;
  }
  return out.join('');
}

// ---------------------------------------------------------------------------
// The durable preview and the live words, as one text
// ---------------------------------------------------------------------------

/** What the panel draws from the live stream. */
export interface LiveView {
  utterances: readonly LiveUtterance[];
  partial: LiveUtterance | null;
  /**
   * Milliseconds from the stored recording's zero (MediaRecorder.start) to
   * live sample 0: an utterance at samples [a, b) sits at
   * (a + b) / 32 + clockOffsetMs in the recording's time.
   */
  clockOffsetMs: number;
  /** False once the stream failed for good: the panel then shows the durable preview alone. */
  active: boolean;
  version: number;
}

export interface MergedTranscript {
  /** The durable preview (final segments). */
  settled: string;
  /** The durable text the server still holds back. */
  held: string;
  /** Committed live utterances after what the durable text covers. */
  live: string;
  /** The live utterance still being heard. */
  partial: string;
}

/** Enough live text for the tail the panel shows (four lines), with room to spare. */
export const LIVE_TAIL_CHARS = 600;

/**
 * How much of the recording the durable text covers. A `transcribed_ms` of 0
 * beside durable text is a server that did not send the field (it parses as
 * 0), not one that has transcribed nothing: trusting it would show every live
 * word a second time after whisper's. Audio minus backlog is the same number
 * by another route.
 */
function coveredMs(progress: SessionProgress | null): number {
  if (!progress) return 0;
  if (progress.transcribedMs !== undefined && progress.transcribedMs > 0) return progress.transcribedMs;
  return Math.max(0, progress.audioMs - progress.backlogMs);
}

function midpointMs(u: LiveUtterance, offsetMs: number): number {
  return (u.startSample + u.endSample) / 2 / SAMPLES_PER_MS + offsetMs;
}

/**
 * The piece of a word the tail drops to start on a whole one: at most 24
 * characters (longer than any word the panel's tail needs to lose), then the
 * space after it.
 */
const TAIL_FRAGMENT = /^\S{1,24}\s+/;

/**
 * The last `max` characters of `text`, starting on a whole word where the
 * script has words.
 *
 * It used to drop everything up to the first space after the cut. Chinese,
 * Japanese and Thai are written without spaces, so twelve 55-character
 * Japanese utterances followed by "OK thanks" left a tail of 9 characters:
 * the panel showed one line where four lines of speech belonged (measured in
 * Chromium at 390 px, review 2026-09-30). Now only a SHORT fragment is dropped,
 * and only when the cut really fell inside it; nor is a surrogate pair (an
 * emoji, a rare CJK character) cut in half.
 */
function tailOf(text: string, max: number): string {
  if (text.length <= max) return text;
  let from = text.length - max;
  const unit = text.charCodeAt(from);
  if (unit >= 0xdc00 && unit <= 0xdfff) from += 1;
  let cut = text.slice(from);
  if (/\S/.test(text[from - 1] ?? '')) {
    const fragment = TAIL_FRAGMENT.exec(cut);
    if (fragment) cut = cut.slice(fragment[0].length);
  }
  return cut.trimStart();
}

/**
 * The durable text covers the recording up to `transcribed_ms`; a live
 * utterance is shown only if its middle lies after that, so a sentence whisper
 * has already written is not written twice, and one it has not reached yet is
 * not missing. Before the durable path has any text, the live words are all
 * there is. A stream that failed for good is not shown at all: the panel is
 * then exactly what it was before live dictation existed.
 */
export function mergeLiveTranscript(
  progress: SessionProgress | null,
  live: LiveView | null,
  tailChars = LIVE_TAIL_CHARS,
): MergedTranscript {
  const settled = progress?.preview ?? '';
  const held = progress?.tentative ?? '';
  if (!live || !live.active) return { settled, held, live: '', partial: '' };
  const durable = Boolean(settled.trim() || held.trim());
  const covered = coveredMs(progress);
  const shown = (u: LiveUtterance) => !durable || midpointMs(u, live.clockOffsetMs) > covered;
  const pieces: string[] = [];
  let chars = 0;
  for (let i = live.utterances.length - 1; i >= 0 && chars < tailChars; i -= 1) {
    const u = live.utterances[i]!;
    // In time order: once one is covered, every earlier one is too.
    if (!shown(u)) break;
    pieces.push(u.text);
    chars += u.text.length + 1;
  }
  const text = tailOf(joinPieces(pieces.reverse()), tailChars);
  const partial = live.partial && shown(live.partial) ? live.partial.text : '';
  return { settled, held, live: text, partial };
}

/** The progress a session reports before its first answer (lib/voice.ts VoiceSession). */
function blankProgress(): SessionProgress {
  return {
    preview: '',
    tentative: '',
    audioMs: 0,
    transcribedMs: 0,
    backlogMs: 0,
    waitingOn: 'none',
    pendingParts: 0,
    pendingMs: 0,
    savedMs: 0,
    offline: false,
    offlineLong: false,
    storageTrouble: false,
    lastAckAt: null,
    retentionDays: null,
    progressive: true,
  };
}

/**
 * The progress the bar draws: the session's own, with the live words after
 * it. The live words can come before the session's first answer (5 s of
 * audio at the earliest), so they then stand on a blank progress, which draws
 * nothing else: savedMs 0 hides "Saved to your account" until the server
 * says so.
 */
export function withLiveWords(
  progress: SessionProgress | null,
  live: LiveView | null,
): SessionProgress | null {
  if (!live) return progress;
  const merged = mergeLiveTranscript(progress, live);
  if (!merged.live && !merged.partial) return progress;
  return { ...(progress ?? blankProgress()), live: { committed: merged.live, partial: merged.partial } };
}

/** The separator the preview puts between two pieces ('' between two CJK or Thai runs). */
export function spaceBetween(left: string, right: string): string {
  if (!left || !right) return '';
  return joinPreview(left, right).length > left.length + right.length ? ' ' : '';
}

// ---------------------------------------------------------------------------
// Which transcript goes into the draft at Stop
// ---------------------------------------------------------------------------

/** The two transcripts a recording with a live stream ends with. */
export type TranscriptSource = 'live' | 'durable';

/**
 * A live transcript at least this Devanagari is a Hindi or Hinglish session
 * (build spec section 10).
 */
export const HINDI_SHARE = 0.2;
const LETTER = /\p{L}/u;
const DEVANAGARI = /\p{Script=Devanagari}/u;
/** Whisper's language for Hindi, by code or by the name the session state also carries. */
const WHISPER_HINDI = new Set(['hi', 'ur', 'hindi', 'urdu']);

/**
 * Devanagari's share of the LETTERS in `text`. Vowel signs and viramas are
 * combining marks, not letters, and are left out of both counts, so a word
 * weighs the same whichever script it is written in.
 */
export function devanagariShare(text: string): number {
  let letters = 0;
  let devanagari = 0;
  for (const ch of text) {
    if (!LETTER.test(ch)) continue;
    letters += 1;
    if (DEVANAGARI.test(ch)) devanagari += 1;
  }
  return letters === 0 ? 0 : devanagari / letters;
}

/**
 * THE FINAL-TEXT POLICY (build spec section 10, amended by section 12 on
 * 2026-09-30). Which transcript goes into the draft once the full pass is
 * done.
 *
 * Measured 2026-09-29 on 34 minutes of Hindi-English lectures (MUCS), same
 * normaliser: whisper-large-v3, the full pass, 40.2% document WER with script
 * set aside, against 19.4% for the live Nemotron stream; whisper also wrote 73
 * of 364 segments in Urdu script and 5 in English. On English the two are
 * close on read speech (LibriSpeech 4.21% against 4.13% for the English live
 * model) and the full pass is clearly ahead on FLEURS-en (6.81% against
 * 10.16%).
 *
 * So a Hindi or Hinglish session (`isHindiSession`) gets the LIVE transcript,
 * but only a complete one that the English-only model had no part in. A live
 * stream that missed part of the recording never stands in for the whole of
 * it. A stream switched from English to Hindi mid-recording still holds what
 * the English model made of the speech before the switch (the new stream
 * resumes after the last final), so the full pass goes in, and the live text
 * is one click away. Everything else — English above all, and any language
 * this was not measured on — gets the full pass, as before live dictation
 * existed.
 */
export function chooseFinalText(input: {
  liveText: string;
  liveComplete: boolean;
  /**
   * The English-only model committed some of the live words (a connection
   * that asked for "en": `LiveStream.englishModelFinals`). Absent is none.
   */
  englishModelFinals?: boolean;
  /** The language the live stream was asked for at the end (the bar's control). */
  userLanguage: VoiceLanguage | null;
  /** The full pass's language, as the session state's language_code (or its name). */
  whisperLanguage: string | null | undefined;
}): TranscriptSource {
  if (!input.liveText.trim() || !input.liveComplete || input.englishModelFinals) return 'durable';
  return isHindiSession(input) ? 'live' : 'durable';
}

/**
 * Spec 12's test for a Hindi or Hinglish session: the person chose Hindi, or
 * at least a fifth of the live words' letters are Devanagari, or whisper heard
 * Hindi or Urdu while the language was left on Auto.
 *
 * THE ENGLISH EXCEPTION (spec 12, the spec owner's amendment of section 10,
 * 2026-09-30 02:20). Whisper hearing Hindi or Urdu does not make a Hindi
 * session when the person chose English. "en" puts the live stream on the
 * English-only model (spec 8A), which scores about 80% WER on the MUCS
 * Hindi-English lectures (spec 12), where whisper, the full pass, scored 55.8%
 * document WER, 40.2% with script set aside (spec 10). For someone who chose
 * English, whisper's text is the better of the two even when it hears Hindi or
 * Urdu, and it goes in. Devanagari in the live words still counts: only the
 * multilingual model writes it (before a switch to English), and
 * `chooseFinalText` turns down every stream the English model added words to.
 */
export function isHindiSession(input: {
  liveText: string;
  userLanguage: VoiceLanguage | null;
  whisperLanguage: string | null | undefined;
}): boolean {
  const whisper = (input.whisperLanguage ?? '').trim().toLowerCase();
  return (
    input.userLanguage === 'hi' ||
    (input.userLanguage !== 'en' && WHISPER_HINDI.has(whisper)) ||
    devanagariShare(input.liveText) >= HINDI_SHARE
  );
}

/**
 * Whether the live stream's last words could still make `chooseFinalText`
 * pick the live transcript. That is the one reason for the insert to wait for
 * them once the full pass is in (components/useVoiceRecorder.ts, at most
 * LIVE_SETTLE_WAIT_MS). They cannot once the English-only model has committed
 * words, nor without a stream still able to finish (`language` null). Nor
 * when the rest is heard in English with under a fifth of the letters so far
 * Devanagari: that model writes no Devanagari, any word it adds is an
 * English-model final, and the English choice keeps whisper's language out of
 * the decision. Whatever still arrives then, the full pass goes in.
 */
export function liveMayStillBeChosen(input: {
  liveText: string;
  englishModelFinals: boolean;
  /** The stream's language (`LiveCapture.language`): the one the words still to come are heard in. */
  language: VoiceLanguage | null;
}): boolean {
  if (input.englishModelFinals || input.language === null) return false;
  return input.language !== 'en' || devanagariShare(input.liveText) >= HINDI_SHARE;
}

/**
 * Put the recording's OTHER transcript where the first one went in (the
 * swap after Stop), or null. Only over the exact words that went in: once the
 * person has changed them, nothing is swapped. The Retry path
 * (lib/voice.ts `placeRetranscript`) merges a person's edits into a new
 * transcript; two different recognisers' texts share too little for that, so
 * the swap never tries.
 */
export function swapInDraft(
  draft: string,
  span: DraftSpan | null,
  current: string,
  other: string,
): { text: string; span: DraftSpan } | null {
  const base = current.trim();
  const fresh = other.trim();
  if (!base || !fresh || !span) return null;
  if (span.start < 0 || span.end > draft.length || span.start >= span.end) return null;
  const region = draft.slice(span.start, span.end);
  if (region !== base && region.trim() !== base) return null;
  return {
    text: draft.slice(0, span.start) + fresh + draft.slice(span.end),
    span: { start: span.start, end: span.start + fresh.length },
  };
}

// ---------------------------------------------------------------------------
// The worklet
// ---------------------------------------------------------------------------

export interface PcmTap {
  /** Ask the worklet for the samples still inside it; resolves once posted, or after `timeoutMs`. */
  flush(timeoutMs: number): Promise<void>;
  /** Take the worklet out of the graph. Idempotent. */
  stop(): void;
}

export interface TapSink {
  frame(index: number, samples: Int16Array, arrivedAt: number): void;
  /** The worklet's first quantum: the context's real rate. */
  started?(contextRate: number): void;
}

function asInt16(data: unknown): Int16Array | null {
  if (data instanceof Int16Array) return data;
  if (ArrayBuffer.isView(data)) {
    return new Int16Array(data.buffer, data.byteOffset, Math.floor(data.byteLength / 2));
  }
  if (data && typeof (data as ArrayBuffer).byteLength === 'number') {
    try {
      return new Int16Array(data as ArrayBuffer, 0, Math.floor((data as ArrayBuffer).byteLength / 2));
    } catch {
      return null;
    }
  }
  return null;
}

/**
 * Put the capture worklet on `context`, fed by `source` (the node the level
 * meter reads, so there is one microphone and one context). Null when this
 * browser has no AudioWorklet, which leaves the recording exactly as it was.
 */
export async function attachPcmTap(
  context: BaseAudioContext,
  source: AudioNode,
  sink: TapSink,
  deps: {
    AudioWorkletNodeImpl?: typeof AudioWorkletNode;
    url?: string;
    now?: () => number;
    /** Checked after the module loads: the recording may have ended meanwhile. */
    stale?: () => boolean;
  } = {},
): Promise<PcmTap | null> {
  const NodeImpl =
    deps.AudioWorkletNodeImpl ?? (globalThis as { AudioWorkletNode?: typeof AudioWorkletNode }).AudioWorkletNode;
  const worklet = (context as { audioWorklet?: AudioWorklet }).audioWorklet;
  if (!worklet || typeof worklet.addModule !== 'function' || typeof NodeImpl !== 'function') return null;
  await worklet.addModule(deps.url ?? LIVE_WORKLET_URL);
  if (deps.stale?.() || context.state === 'closed') return null;
  const node = new NodeImpl(context, LIVE_PROCESSOR, {
    numberOfInputs: 1,
    numberOfOutputs: 1,
    outputChannelCount: [1],
    // The browser mixes the microphone down to mono before the worklet sees
    // it; the worklet averages anything that still arrives with more.
    channelCount: 1,
    channelCountMode: 'explicit',
    channelInterpretation: 'speakers',
  });
  const now = deps.now ?? (() => performance.now());
  let onFlushed: (() => void) | null = null;
  node.port.onmessage = (event: MessageEvent) => {
    const data = event.data as { type?: unknown; index?: unknown; samples?: unknown; sampleRate?: unknown } | null;
    if (!data) return;
    if (data.type === 'frame' && typeof data.index === 'number') {
      const samples = asInt16(data.samples);
      if (samples && samples.length > 0) sink.frame(data.index, samples, now());
    } else if (data.type === 'start' && typeof data.sampleRate === 'number') {
      sink.started?.(data.sampleRate);
    } else if (data.type === 'flushed') {
      onFlushed?.();
    }
  };
  // An AudioWorkletNode that reaches no destination is not run by every
  // engine (Safari was the open question). Through a gain of zero it runs
  // everywhere and makes no sound.
  const mute = context.createGain();
  mute.gain.value = 0;
  source.connect(node);
  node.connect(mute);
  mute.connect(context.destination);
  let stopped = false;
  return {
    flush(timeoutMs) {
      if (stopped) return Promise.resolve();
      return new Promise<void>((resolve) => {
        const done = () => {
          clearTimeout(timer);
          onFlushed = null;
          resolve();
        };
        const timer = setTimeout(done, timeoutMs);
        onFlushed = done;
        try {
          node.port.postMessage({ type: 'flush' });
        } catch {
          done();
        }
      });
    },
    stop() {
      if (stopped) return;
      stopped = true;
      onFlushed?.();
      try {
        node.port.postMessage({ type: 'stop' });
      } catch {
        /* the context is already gone */
      }
      node.port.onmessage = null;
      const cuts = [() => source.disconnect(node), () => node.disconnect(), () => mute.disconnect()];
      for (const cut of cuts) {
        try {
          cut();
        } catch {
          /* already disconnected, or the context is closed */
        }
      }
      try {
        node.port.close();
      } catch {
        /* nothing to close */
      }
    },
  };
}

// ---------------------------------------------------------------------------
// The socket
// ---------------------------------------------------------------------------

/** How a live stream ended: its done, the finish budget, the person, a refusal, or no audio at all. */
export type LiveEnd = 'done' | 'timeout' | 'aborted' | 'refused' | 'nothing';

export type LivePhase = 'waiting' | 'connecting' | 'streaming' | 'backoff' | 'closed' | 'failed';

export interface LiveStreamOptions {
  url: string;
  ring: PcmRing;
  clock: CaptureClock;
  transcript: LiveTranscript;
  /** performance.now() when MediaRecorder.start() ran: the stored recording's zero. */
  recorderStartedAt: number;
  /** The language the first `start` asks for; `setLanguage` changes it later. */
  language: VoiceLanguage;
  /** How far back a reconnect may replay (config.live.resume_max_s). */
  resumeMaxS: number;
  onChange?: () => void;
  onEnd?: (end: LiveEnd) => void;
  WebSocketImpl?: typeof WebSocket;
  /** performance.now, injectable. */
  now?: () => number;
  random?: () => number;
}

const isCount = (v: unknown): v is number => typeof v === 'number' && Number.isInteger(v) && v >= 0;

/**
 * One live stream for one recording session, across as many connections as
 * it takes.
 *
 * A connection sends `start`, waits for `ready`, then sends PCM from the
 * resume point: sample 0 the first time (the ring holds what was spoken
 * before the session existed), and after a drop the later of the last final's
 * end and a minute back (never closer to the ring's oldest sample than its
 * headroom), with `next_u` continuing the numbering. So an outage shorter
 * than a minute loses no words, and the words already committed are not sent
 * again.
 *
 * It never gives up while the recording lasts, except on the refusals that
 * retrying cannot fix (TERMINAL_CLOSES, or an error the server marked not
 * retryable). Every other failure goes through the backoff, which starts over
 * only once a connection has proven itself (LIVE_PROVEN_AFTER_MS).
 */
export class LiveStream {
  private readonly opts: LiveStreamOptions;
  private readonly WS: typeof WebSocket;
  private readonly now: () => number;
  private readonly random: () => number;
  private ws: WebSocket | null = null;
  private phase: LivePhase = 'waiting';
  private ready = false;
  /** When this connection's `ready` came; it has proven itself LIVE_PROVEN_AFTER_MS later. */
  private readyAt: number | null = null;
  /** The next sample to send on this connection. */
  private sent = 0;
  private paused = false;
  private attempt = 0;
  /** What the next `start` asks for; the bar's control changes it mid-recording. */
  private language: VoiceLanguage;
  /** What this connection's `start` asked for; null until it is sent. */
  private sentLanguage: VoiceLanguage | null = null;
  /** A final with words came back on a connection that asked for "en" (`englishModelFinals`). */
  private englishFinals = false;
  /** A language change waiting out LIVE_LANGUAGE_DEBOUNCE_MS (`setLanguage`). */
  private languageTimer: ReturnType<typeof setTimeout> | null = null;
  /** A connection started after audio no final covered: those words were never heard. */
  private skipped = false;
  private ended: LiveEnd | null = null;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private drainTimer: ReturnType<typeof setTimeout> | null = null;
  private tickTimer: ReturnType<typeof setInterval> | null = null;
  private finishTimer: ReturnType<typeof setTimeout> | null = null;
  private lastMessageAt = 0;
  private lastPingAt = 0;
  private lastStatsAt = 0;
  private finishing = false;
  private flushSent = false;
  private finished: Promise<void> | null = null;
  private resolveFinished: (() => void) | null = null;
  /** `retryable` of the last error message, or null when there was none. */
  private errorRetryable: boolean | null = null;
  private clockOffset: number | null = null;
  private pendingPartialEnd: number | null = null;
  private pendingFinalEnds: number[] = [];
  private partialMs: number[] = [];
  private finalMs: number[] = [];
  /** Connections opened, and times the ring overwrote audio before it could be sent. */
  connects = 0;
  gaps = 0;

  constructor(opts: LiveStreamOptions) {
    this.opts = opts;
    this.language = opts.language;
    this.WS = opts.WebSocketImpl ?? (globalThis as { WebSocket: typeof WebSocket }).WebSocket;
    this.now = opts.now ?? (() => performance.now());
    this.random = opts.random ?? Math.random;
  }

  /** Where the stream stands; for tests and the view. */
  get state(): LivePhase {
    return this.phase;
  }

  /** True until a refusal retrying cannot fix. */
  get active(): boolean {
    return this.phase !== 'failed';
  }

  /** How the stream ended, or null while it lasts. */
  get end(): LiveEnd | null {
    return this.ended;
  }

  /** The language the stream asks the engine for now. */
  get currentLanguage(): VoiceLanguage {
    return this.language;
  }

  /**
   * True when the live transcript covers the whole recording: the stream
   * ended with the server's `done` after the flush (so it was never refused
   * for good, and the last words arrived), no audio fell out of the ring
   * before it was sent, and no connection resumed past audio that no final
   * covered. Anything less is part of the recording, and is never offered or
   * inserted as if it were all of it.
   */
  get complete(): boolean {
    return this.ended === 'done' && this.gaps === 0 && !this.skipped;
  }

  /**
   * Whether the English-only model wrote any of the committed words: a final
   * with words came back on a connection whose `start` asked for "en". A
   * language switch keeps them (the new stream resumes after the last final),
   * so a stream switched from English to Hindi still holds the English model's
   * attempt at whatever was said before the switch, and the final-text policy
   * keeps the full pass (spec 12). A final the old connection sends during a
   * switch's debounce is its model's, whatever the new choice.
   */
  get englishModelFinals(): boolean {
    return this.englishFinals;
  }

  /**
   * The person picked another language mid-recording. The engine reads the
   * language once, at `start`, so a new stream in the new language takes over,
   * through the same resume a dropped connection uses: it starts where the last
   * final ended, so the committed words stay as they are and the utterance
   * being spoken is heard again, in the new language. Whatever the old
   * connection still sends is ignored (it is no longer this.ws), and `next_u`
   * carries the numbering on, so no word is lost or shown twice.
   *
   * DEBOUNCED (review, 2026-09-30). The choice counts at once: any `start`
   * sent from now on says it. The connection in use is only replaced once no
   * other choice has come for LIVE_LANGUAGE_DEBOUNCE_MS, and not at all when
   * the choice is back to the language it asked for. The control is a set of
   * native radios, so a held arrow key walks auto -> en -> hi -> auto, one
   * change per key repeat: 24 presses 33 ms apart opened 24 sockets in under a
   * second, each one counted against the gateway's 30 connections a minute and
   * each one an engine stream on the worker, and a real network drop in that
   * minute would then have been refused. Waiting out a backoff, the next
   * attempt simply says the new language. After Stop the choice no longer
   * matters (`finish`).
   */
  setLanguage(language: VoiceLanguage): void {
    if (this.finishing || this.phase === 'closed' || this.phase === 'failed') return;
    if (language === this.language) return;
    this.language = language;
    this.clearLanguageTimer();
    this.languageTimer = setTimeout(() => {
      this.languageTimer = null;
      this.applyLanguage();
    }, LIVE_LANGUAGE_DEBOUNCE_MS);
  }

  /** The debounce is over: replace the connection if it asked for another language. */
  private applyLanguage(): void {
    if (this.finishing || (this.phase !== 'connecting' && this.phase !== 'streaming')) return;
    // Not open yet (its `start` will say the choice), or back where it was.
    if (this.sentLanguage === null || this.sentLanguage === this.language) return;
    this.drop();
    this.connect();
  }

  private clearLanguageTimer(): void {
    if (this.languageTimer !== null) {
      clearTimeout(this.languageTimer);
      this.languageTimer = null;
    }
  }

  /**
   * Stop came inside a language change's debounce, so the change never reached
   * the engine: the words still to come are heard in the language the open
   * connection asked for. That is the language the stream reports from now on,
   * because the final-text policy is about the model that heard the words. The
   * choice itself is still this browser's preference for the next recording.
   */
  private dropPendingLanguage(): void {
    if (this.languageTimer === null) return;
    this.clearLanguageTimer();
    if (this.ws !== null && this.sentLanguage !== null) this.language = this.sentLanguage;
  }

  /**
   * Milliseconds from the recorder's start to sample 0: negative when the tap
   * started first, which it normally does (it starts at the permission grant,
   * the recorder only once the session exists). Fixed at the first connection
   * so every start message and the panel's merge agree; bounded to ±60 s as
   * the server bounds it.
   */
  get clockOffsetMs(): number {
    if (this.clockOffset === null) {
      const zero = this.opts.clock.timeOf(0);
      if (zero === null) return 0;
      const raw = Math.round(zero - this.opts.recorderStartedAt);
      this.clockOffset = Math.max(-60_000, Math.min(60_000, raw));
    }
    return this.clockOffset;
  }

  /** Begin: connect as soon as there is audio to send. */
  start(): void {
    if (this.phase === 'waiting' && this.hasAudio()) this.connect();
  }

  /** A frame landed in the ring. */
  audioArrived(): void {
    if (this.phase === 'waiting') {
      if (this.hasAudio()) this.connect();
      return;
    }
    if (this.phase === 'streaming') this.pump();
  }

  /**
   * Stop was pressed and every sample is in the ring: send the rest and
   * `flush`, wait for `done`, close. Never longer than `budgetMs`, and never
   * in the way of the stored recording's own finish, which does not wait.
   */
  finish(budgetMs = LIVE_FINISH_BUDGET_MS): Promise<void> {
    if (this.finished) return this.finished;
    this.dropPendingLanguage();
    this.finished = new Promise<void>((resolve) => {
      this.resolveFinished = resolve;
    });
    if (this.phase === 'closed' || this.phase === 'failed') {
      this.resolveFinished!();
      return this.finished;
    }
    this.finishing = true;
    this.finishTimer = setTimeout(() => this.close('timeout'), budgetMs);
    if (this.phase === 'waiting') {
      if (this.hasAudio()) this.connect();
      else this.close('nothing');
    } else if (this.phase === 'backoff') {
      this.connect();
    } else {
      this.pump();
    }
    return this.finished;
  }

  /** Cancel, discard, page going away: close now (1000), and never again. */
  abort(): void {
    this.close('aborted');
  }

  /** The browser is back online: a reconnect waiting out its backoff goes now. */
  nudge(): void {
    if (this.phase !== 'backoff') return;
    this.connect();
  }

  /**
   * The panel has just drawn what arrived since its last draw: the latency of
   * each final, and of the partial on screen (the ones it replaced were never
   * seen), from the capture of the last sample it covers to now.
   */
  rendered(at: number = this.now()): void {
    const measure = (end: number, into: number[]) => {
      const captured = this.opts.clock.timeOf(Math.max(0, end - 1));
      if (captured === null) return;
      into.push(Math.max(0, Math.min(60_000, Math.round(at - captured))));
      if (into.length > STATS_MAX) into.splice(0, into.length - STATS_MAX);
    };
    for (const end of this.pendingFinalEnds) measure(end, this.finalMs);
    if (this.pendingPartialEnd !== null) measure(this.pendingPartialEnd, this.partialMs);
    this.pendingFinalEnds = [];
    this.pendingPartialEnd = null;
  }

  // -- connection -----------------------------------------------------------

  private hasAudio(): boolean {
    return this.opts.clock.running && this.opts.ring.end > this.opts.ring.start;
  }

  /**
   * The later of the last final's end and the oldest sample a replay may
   * start from. The replay reaches back at most resume_max_s, and never so far
   * that the frames arriving before `ready` could overwrite where it starts:
   * LIVE_RESUME_HEADROOM_SECONDS of the ring stay behind it (a quarter of a
   * ring shorter than that, which only tests build).
   */
  private resumePoint(): number {
    const { ring, transcript, resumeMaxS } = this.opts;
    const asked = Math.floor(Math.min(LIVE_RESUME_MAX_SECONDS, Math.max(0, resumeMaxS)) * LIVE_SAMPLE_RATE);
    const headroom = Math.min(LIVE_RESUME_HEADROOM_SECONDS * LIVE_SAMPLE_RATE, Math.floor(ring.capacity / 4));
    const window = Math.min(asked, ring.capacity - headroom);
    const oldest = Math.max(ring.start, ring.end - window);
    return Math.min(ring.end, Math.max(transcript.committedUntil, oldest));
  }

  private connect(): void {
    this.clearReconnect();
    let ws: WebSocket;
    try {
      ws = new this.WS(this.opts.url, [LIVE_SUBPROTOCOL]);
    } catch {
      // A URL or a policy the browser refused outright: the same as a drop.
      this.retry();
      return;
    }
    this.connects += 1;
    this.ws = ws;
    this.ready = false;
    this.readyAt = null;
    this.paused = false;
    this.flushSent = false;
    this.errorRetryable = null;
    this.sentLanguage = null;
    this.phase = 'connecting';
    try {
      ws.binaryType = 'arraybuffer';
    } catch {
      /* only affects what we receive, and the server sends text */
    }
    ws.onopen = () => {
      if (this.ws !== ws) return;
      this.lastMessageAt = this.now();
      this.lastPingAt = this.lastMessageAt;
      this.lastStatsAt = this.lastMessageAt;
      // Decided now, not when the socket was asked for: a ring that kept
      // overflowing while it connected has moved its oldest sample since.
      const resume = this.resumePoint();
      this.sent = resume;
      this.sentLanguage = this.language;
      this.send(ws, JSON.stringify(this.startMessage(resume)));
      this.startTicking();
    };
    ws.onmessage = (event: MessageEvent) => this.onMessage(ws, event.data);
    ws.onerror = () => undefined; // the close that follows decides
    ws.onclose = (event: CloseEvent) => this.onClose(ws, event.code);
  }

  private startMessage(resume: number): Record<string, unknown> {
    return {
      type: 'start',
      v: 1,
      encoding: 'pcm_s16le',
      sample_rate: LIVE_SAMPLE_RATE,
      channels: 1,
      frame_ms: 40,
      source: 'mic',
      resume_from_sample: resume,
      next_u: this.opts.transcript.nextU,
      clock_offset_ms: this.clockOffsetMs,
      language: this.language,
    };
  }

  private onMessage(ws: WebSocket, data: unknown): void {
    if (this.ws !== ws) return;
    this.lastMessageAt = this.now();
    if (typeof data !== 'string') return;
    let msg: Record<string, unknown>;
    try {
      const parsed: unknown = JSON.parse(data);
      if (typeof parsed !== 'object' || parsed === null) return;
      msg = parsed as Record<string, unknown>;
    } catch {
      return;
    }
    switch (msg.type) {
      case 'ready':
        this.onReady(msg);
        return;
      case 'partial':
      case 'final':
        this.onWords(msg.type, msg);
        return;
      case 'done':
        this.close('done');
        return;
      case 'error':
        this.errorRetryable = msg.retryable === true ? true : msg.retryable === false ? false : null;
        return;
      default:
        // pong, speech, and whatever a newer server adds.
        return;
    }
  }

  private onReady(msg: Record<string, unknown>): void {
    if (this.ready) return;
    this.ready = true;
    // The backoff is NOT reset here: see LIVE_PROVEN_AFTER_MS.
    this.readyAt = this.now();
    const echoed = msg.resume_from_sample;
    if (isCount(echoed) && echoed >= this.opts.ring.start && echoed <= this.opts.ring.end) this.sent = echoed;
    // Audio after the last final and before this point reached no decoder
    // that committed it: whatever was said there is missing from the text.
    if (this.sent > this.opts.transcript.committedUntil) this.skipped = true;
    this.phase = 'streaming';
    this.pump();
  }

  private onWords(kind: 'partial' | 'final', msg: Record<string, unknown>): void {
    const { u, text } = msg;
    if (!isCount(u) || typeof text !== 'string') return;
    // Words back from the engine: this connection works.
    this.attempt = 0;
    const start = isCount(msg.start_sample) ? msg.start_sample : 0;
    const end = isCount(msg.end_sample) ? Math.max(start, msg.end_sample) : start;
    const words = text.length > MAX_EVENT_TEXT ? text.slice(0, MAX_EVENT_TEXT) : text;
    const { transcript } = this.opts;
    const changed =
      kind === 'final'
        ? transcript.final(u, words, start, end)
        : transcript.partialUpdate(u, words, start, end);
    if (!changed) return;
    if (kind === 'final') {
      // Words committed by the English-only model (an empty final commits none).
      if (this.sentLanguage === 'en' && words.trim()) this.englishFinals = true;
      this.pendingFinalEnds.push(end);
      if (this.pendingFinalEnds.length > STATS_MAX) this.pendingFinalEnds.shift();
    } else {
      this.pendingPartialEnd = end;
    }
    this.opts.onChange?.();
  }

  private onClose(ws: WebSocket, code: number): void {
    if (this.ws !== ws) return;
    this.ws = null;
    this.ready = false;
    this.paused = false;
    this.stopTicking();
    this.clearDrain();
    if (this.phase === 'closed' || this.phase === 'failed') return;
    if (TERMINAL_CLOSES.has(code) || this.errorRetryable === false) {
      this.fail();
      return;
    }
    this.retry();
  }

  private retry(): void {
    if (this.phase === 'closed' || this.phase === 'failed') return;
    const base =
      this.attempt < RECONNECT_DELAYS_MS.length ? RECONNECT_DELAYS_MS[this.attempt]! : RECONNECT_CEILING_MS;
    this.attempt += 1;
    // ±20%, so a room of browsers that lost the same Wi-Fi does not return in step.
    const delay = Math.round(base * (0.8 + 0.4 * this.random()));
    this.phase = 'backoff';
    this.clearReconnect();
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      this.connect();
    }, delay);
  }

  // -- sending --------------------------------------------------------------

  private send(ws: WebSocket, data: string | ArrayBuffer): boolean {
    try {
      ws.send(data);
      return true;
    } catch {
      return false; // closing under us; its close event decides what next
    }
  }

  /** Hand the socket everything the ring holds past `sent`, within the backpressure bounds. */
  private pump(): void {
    const ws = this.ws;
    // Nothing after `flush`: the server finalises on it, and a frame behind
    // it would be audio past the end it was told about.
    if (!ws || !this.ready || this.flushSent || ws.readyState !== OPEN) return;
    const { ring } = this.opts;
    while (this.sent < ring.end) {
      if (this.sent < ring.start) {
        this.resync();
        return;
      }
      const queued = ws.bufferedAmount;
      if (this.paused ? queued >= LIVE_LOW_WATER_BYTES : queued > LIVE_HIGH_WATER_BYTES) {
        this.paused = true;
        this.armDrain();
        return;
      }
      this.paused = false;
      const chunk = ring.read(this.sent, LIVE_FRAME_SAMPLES);
      if (!chunk || chunk.length === 0) break;
      if (!this.send(ws, chunk.buffer as ArrayBuffer)) return;
      this.sent += chunk.length;
    }
    if (this.finishing && this.sent >= ring.end) {
      if (this.send(ws, JSON.stringify({ type: 'flush' }))) this.flushSent = true;
    }
  }

  /**
   * Audio the ring overwrote before it could be sent (the uplink was slower
   * than the microphone for a minute, or a handshake outlasted the resume
   * headroom). Nothing can be sent across that hole on this connection — the
   * server counts samples — so a new one starts from the ring as it is then,
   * and the hole is counted. Through the backoff, never at once: an overwrite
   * that happens during every handshake would otherwise reconnect in a tight
   * loop, each attempt an engine stream on the worker.
   */
  private resync(): void {
    this.gaps += 1;
    this.drop();
    this.retry();
  }

  private armDrain(): void {
    if (this.drainTimer !== null) return;
    this.drainTimer = setTimeout(() => {
      this.drainTimer = null;
      this.pump();
    }, DRAIN_CHECK_MS);
  }

  private clearDrain(): void {
    if (this.drainTimer !== null) {
      clearTimeout(this.drainTimer);
      this.drainTimer = null;
    }
  }

  private clearReconnect(): void {
    if (this.reconnectTimer !== null) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
  }

  // -- liveness and statistics ----------------------------------------------

  private startTicking(): void {
    this.stopTicking();
    this.tickTimer = setInterval(() => this.tick(), TICK_MS);
  }

  private stopTicking(): void {
    if (this.tickTimer !== null) {
      clearInterval(this.tickTimer);
      this.tickTimer = null;
    }
  }

  private tick(): void {
    const ws = this.ws;
    if (!ws || ws.readyState !== OPEN) return;
    const now = this.now();
    if (this.ready && this.readyAt !== null && now - this.readyAt >= LIVE_PROVEN_AFTER_MS) {
      // Up this long: whatever failed before is behind us.
      this.attempt = 0;
    }
    if (this.lastPingAt > this.lastMessageAt) {
      // A ping is out and nothing at all has come back since. Open by the
      // browser's account, silent by the server's: a phone that changed
      // networks keeps such a socket for minutes. Start again. (A second ping
      // is not sent meanwhile: it would restart the wait it is timing.)
      if (now - this.lastPingAt > LIVE_PONG_TIMEOUT_MS) {
        this.drop();
        this.retry();
        return;
      }
    } else if (now - this.lastPingAt >= LIVE_PING_INTERVAL_MS) {
      this.lastPingAt = now;
      this.send(ws, JSON.stringify({ type: 'ping', t: Math.round(now) }));
    }
    if (this.ready && now - this.lastStatsAt >= LIVE_STATS_INTERVAL_MS) {
      this.lastStatsAt = now;
      if (this.partialMs.length || this.finalMs.length) {
        this.send(
          ws,
          JSON.stringify({ type: 'client_stats', partial_ms: this.partialMs, final_ms: this.finalMs }),
        );
        this.partialMs = [];
        this.finalMs = [];
      }
    }
  }

  // -- ending ---------------------------------------------------------------

  /** Close this connection without a reconnect decision (its close event is ignored). */
  private drop(): void {
    const ws = this.ws;
    this.ws = null;
    this.ready = false;
    this.paused = false;
    this.stopTicking();
    this.clearDrain();
    if (!ws) return;
    ws.onopen = null;
    ws.onmessage = null;
    ws.onerror = null;
    ws.onclose = null;
    try {
      ws.close(1000);
    } catch {
      /* already closing */
    }
  }

  private settle(): void {
    this.clearReconnect();
    this.clearLanguageTimer();
    if (this.finishTimer !== null) {
      clearTimeout(this.finishTimer);
      this.finishTimer = null;
    }
    this.drop();
    this.resolveFinished?.();
  }

  private close(end: LiveEnd): void {
    if (this.phase === 'closed' || this.phase === 'failed') return;
    this.phase = 'closed';
    this.ended = end;
    this.settle();
    this.opts.onEnd?.(end);
  }

  private fail(): void {
    if (this.phase === 'closed' || this.phase === 'failed') return;
    this.phase = 'failed';
    this.ended = 'refused';
    this.settle();
    this.opts.onEnd?.('refused');
    this.opts.onChange?.();
  }
}

// ---------------------------------------------------------------------------
// What the recorder hook holds
// ---------------------------------------------------------------------------

export interface LiveCaptureDeps {
  /**
   * The live words changed. Called at most every LIVE_RENDER_INTERVAL_MS
   * (the last change always lands), with what the panel should draw now.
   */
  onChange?: (view: LiveView | null) => void;
  /** The stream ended: at Stop, on cancel, or refused for good mid-recording ('refused'). */
  onEnd?: (end: LiveEnd) => void;
  WebSocketImpl?: typeof WebSocket;
  AudioWorkletNodeImpl?: typeof AudioWorkletNode;
  now?: () => number;
  random?: () => number;
  workletUrl?: string;
}

/**
 * One recording's live path: the worklet on the meter's context, the ring
 * it fills, and the stream once the session is known.
 *
 *   attach()  right after getUserMedia, so the opening words are in the ring
 *             before the session even exists;
 *   open()    once the recorder has started on a session whose config has a
 *             live path; the stream replays from sample 0;
 *   detach()  when the recording turned out to have no live path;
 *   finish()  at Stop, before the AudioContext closes; `tapDone` resolves when
 *             the worklet no longer needs the context, with its last frames
 *             AND the flush already handed to the socket;
 *   abort()   on cancel, discard, unmount and pagehide.
 */
export class LiveCapture {
  readonly ring = new PcmRing(LIVE_RING_SECONDS);
  readonly clock = new CaptureClock();
  readonly transcript = new LiveTranscript();
  private readonly deps: LiveCaptureDeps;
  private tap: PcmTap | null = null;
  private liveStream: LiveStream | null = null;
  /** Detached or aborted: nothing more happens. */
  private done = false;
  /** The worklet has handed over its last frame: later frames are not this recording's. */
  private sealed = false;
  private finishing: { tapDone: Promise<void>; done: Promise<void> } | null = null;
  private renderTimer: ReturnType<typeof setTimeout> | null = null;
  private lastRenderAt = -Infinity;

  constructor(deps: LiveCaptureDeps = {}) {
    this.deps = deps;
  }

  /** Whether this browser can do any of it: an AudioWorklet and a WebSocket. */
  static supported(context: BaseAudioContext | null | undefined, deps: LiveCaptureDeps = {}): boolean {
    const WS = deps.WebSocketImpl ?? (globalThis as { WebSocket?: typeof WebSocket }).WebSocket;
    const NodeImpl =
      deps.AudioWorkletNodeImpl ?? (globalThis as { AudioWorkletNode?: typeof AudioWorkletNode }).AudioWorkletNode;
    const worklet = context ? (context as { audioWorklet?: AudioWorklet }).audioWorklet : undefined;
    return (
      Boolean(worklet && typeof worklet.addModule === 'function') &&
      typeof NodeImpl === 'function' &&
      typeof WS === 'function'
    );
  }

  /** The stream, once opened; for tests and diagnostics. */
  get stream(): LiveStream | null {
    return this.liveStream;
  }

  private get now(): () => number {
    return this.deps.now ?? (() => performance.now());
  }

  /** Start the worklet. False when it cannot run here; the recording is unaffected either way. */
  async attach(context: BaseAudioContext, source: AudioNode): Promise<boolean> {
    try {
      const tap = await attachPcmTap(
        context,
        source,
        {
          frame: (index, samples, at) => this.onFrame(index, samples, at),
          started: (rate) => this.clock.setRate(rate),
        },
        {
          AudioWorkletNodeImpl: this.deps.AudioWorkletNodeImpl,
          url: this.deps.workletUrl,
          now: this.deps.now,
          stale: () => this.done || this.finishing !== null,
        },
      );
      if (!tap) return false;
      if (this.done || this.finishing) {
        tap.stop();
        return false;
      }
      this.tap = tap;
      return true;
    } catch {
      // No module (a 404, a CSP refusal), or a processor that would not
      // construct: no live words, and the recording carries on as before.
      return false;
    }
  }

  private onFrame(index: number, samples: Int16Array, at: number): void {
    if (this.done || this.sealed) return;
    this.ring.push(index, samples);
    this.clock.note(index, samples.length, at);
    this.liveStream?.audioArrived();
  }

  /**
   * Open the stream for a session. False, and the capture detached, when the
   * config names no socket this page can reach. Idempotent.
   */
  open(opts: {
    config: LiveConfig;
    sessionId: string;
    recorderStartedAt: number;
    language: VoiceLanguage;
    location?: { protocol: string; host: string } | null;
  }): boolean {
    if (this.done) return false;
    if (this.liveStream) return true;
    const url = liveSocketUrl(opts.config, opts.sessionId, opts.location);
    if (!url) {
      this.detach();
      return false;
    }
    this.liveStream = new LiveStream({
      url,
      ring: this.ring,
      clock: this.clock,
      transcript: this.transcript,
      recorderStartedAt: opts.recorderStartedAt,
      language: opts.language,
      resumeMaxS: opts.config.resumeMaxS,
      onChange: () => this.scheduleRender(),
      onEnd: (end) => this.deps.onEnd?.(end),
      WebSocketImpl: this.deps.WebSocketImpl,
      now: this.deps.now,
      random: this.deps.random,
    });
    this.liveStream.start();
    return true;
  }

  /** This recording has no live path: the worklet and its audio go. */
  detach(): void {
    if (this.done) return;
    this.done = true;
    this.tap?.stop();
    this.tap = null;
    this.ring.clear();
    this.cancelRender();
  }

  /** Stop pressed. See the class comment for the order this guarantees. */
  finish(): { tapDone: Promise<void>; done: Promise<void> } {
    if (this.finishing) return this.finishing;
    const tap = this.done ? null : this.tap;
    this.tap = null;
    let streamDone: Promise<void> = Promise.resolve();
    const tapDone = (tap ? tap.flush(LIVE_TAP_FLUSH_MS) : Promise.resolve()).then(() => {
      tap?.stop();
      this.sealed = true;
      // Every sample is in the ring now. The rest and the flush go to the
      // socket here, synchronously, before whoever awaits `tapDone` closes the
      // context.
      if (this.liveStream && !this.done) streamDone = this.liveStream.finish();
    });
    this.finishing = { tapDone, done: tapDone.then(() => streamDone) };
    return this.finishing;
  }

  /**
   * Resolves once a finish has run its course (at most LIVE_FINISH_BUDGET_MS
   * after the tap), or once `withinMs` has passed, whichever comes first.
   */
  settled(withinMs?: number): Promise<void> {
    const done = this.finishing?.done ?? Promise.resolve();
    if (withinMs === undefined) return done;
    return new Promise<void>((resolve) => {
      const timer = setTimeout(resolve, Math.max(0, withinMs));
      void done.then(() => {
        clearTimeout(timer);
        resolve();
      });
    });
  }

  abort(): void {
    this.done = true;
    this.tap?.stop();
    this.tap = null;
    this.liveStream?.abort();
    this.ring.clear();
    this.cancelRender();
  }

  nudge(): void {
    this.liveStream?.nudge();
  }

  /** The panel drew the latest view (for the capture-to-screen latency). */
  rendered(at?: number): void {
    this.liveStream?.rendered(at);
  }

  /** Everything heard so far: the fallback insert, and the Hindi transcript at Stop. */
  text(): string {
    return this.transcript.text();
  }

  /** Whether `text()` is the whole recording (LiveStream.complete); false with no stream at all. */
  complete(): boolean {
    return this.liveStream?.complete ?? false;
  }

  /** Whether the English-only model wrote any of `text()` (LiveStream.englishModelFinals). */
  englishModelFinals(): boolean {
    return this.liveStream?.englishModelFinals ?? false;
  }

  /**
   * The language the stream asks for now (after Stop, the one its last words
   * are heard in); null with no stream, or one refused for good.
   */
  get language(): VoiceLanguage | null {
    const stream = this.liveStream;
    return stream && stream.active ? stream.currentLanguage : null;
  }

  /** Hear the rest in another language (LiveStream.setLanguage). */
  setLanguage(language: VoiceLanguage): void {
    if (this.done) return;
    this.liveStream?.setLanguage(language);
  }

  /** What the panel draws, or null while there is nothing to draw. */
  view(): LiveView | null {
    const stream = this.liveStream;
    if (!stream) return null;
    const { transcript } = this;
    if (transcript.committed.length === 0 && !transcript.partial) return null;
    return {
      // A copy: the list grows in place, and a view drawn later must not show
      // a final beside the older partial it replaced.
      utterances: transcript.committed.slice(),
      partial: transcript.partial,
      clockOffsetMs: stream.clockOffsetMs,
      active: stream.active,
      version: transcript.version,
    };
  }

  /**
   * At most one redraw per LIVE_RENDER_INTERVAL_MS. A partial can arrive every
   * decode step (160 ms per stream on the engine, faster during a replay);
   * the panel draws the newest one, and the last change always lands.
   */
  private scheduleRender(): void {
    if (this.renderTimer !== null || !this.deps.onChange) return;
    const wait = Math.max(0, this.lastRenderAt + LIVE_RENDER_INTERVAL_MS - this.now());
    this.renderTimer = setTimeout(() => {
      this.renderTimer = null;
      this.lastRenderAt = this.now();
      this.deps.onChange?.(this.view());
    }, wait);
  }

  private cancelRender(): void {
    if (this.renderTimer !== null) {
      clearTimeout(this.renderTimer);
      this.renderTimer = null;
    }
  }
}
