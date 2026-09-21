/**
 * Voice dictation: the state machine, the microphone, and the level meter.
 *
 * WHY A STATE MACHINE AND NOT FOUR BOOLEANS. Recording has real races —
 * double-clicking Stop, cancelling while the permission prompt is open,
 * navigating away mid-upload, pressing the button again before the last
 * transcript lands. Booleans let two of those be true at once and the UI
 * ends up in a state nobody designed. Here every transition is named and
 * illegal ones simply do not happen:
 *
 *      idle ──start──► requesting ──granted──► recording ──stop──► transcribing
 *        ▲                  │                      │                    │
 *        └───── denied ─────┴────── cancel ────────┘                    │
 *        ◄──────────────────── transcript ─────────────────────────────-┘
 *        ◄──────────────────── failure  ─► error ──dismiss──►
 *
 * WHAT LEAVES THIS MODULE. A blob and a duration. Nothing is stored, nothing
 * is uploaded from here — `transcribe()` is the only network call and its
 * result is a string the composer treats exactly like typed text.
 *
 * THE MICROPHONE IS RELEASED. `stopTracks` runs on every path out of
 * recording — stop, cancel, error, unmount — because a page that keeps the
 * capture indicator lit after the user pressed Stop has broken a promise
 * about hardware, and no transcript is worth that.
 *
 * Pure TypeScript with no React, so the transitions are unit-testable without
 * a DOM.
 */

export type VoiceState =
  | 'idle'
  | 'requesting'
  | 'recording'
  | 'transcribing'
  | 'error';

/** How a recording ended, which decides whether it is transcribed at all. */
export type VoiceEnd = 'stop' | 'cancel';

export interface VoiceError {
  /** Shown to the person. Complete sentences, no error codes. */
  message: string;
  /** Whether trying again could plausibly work. */
  retryable: boolean;
  /** Set when the browser or the user, not the server, refused. */
  permission?: boolean;
}

/** Everything the UI needs to draw, in one object. */
export interface VoiceSnapshot {
  state: VoiceState;
  /** Seconds recorded so far, for the timer. */
  elapsedMs: number;
  /** 0..1 per bar, newest last — the waveform's data. */
  levels: number[];
  error: VoiceError | null;
}

/**
 * How many bars the meter keeps. 48 at ~16 fps is three seconds of history,
 * which is enough for the trace to look alive without becoming a scroll.
 */
export const LEVEL_BARS = 48;

/** Below this a recording is a mis-click, not speech. */
export const MIN_RECORDING_MS = 350;

/**
 * Capture constraints.
 *
 * The three processors are ON. They are designed for speech and this is
 * speech: an office microphone with keyboard noise and a fan transcribes
 * measurably worse without them. They are also what the browser's own
 * conferencing path uses, so this is the tuned road, not the exotic one.
 * `channelCount: 1` because the model wants mono and sending stereo just
 * doubles the upload.
 */
export const AUDIO_CONSTRAINTS: MediaTrackConstraints = {
  echoCancellation: true,
  noiseSuppression: true,
  autoGainControl: true,
  channelCount: 1,
};

/**
 * Container preference, best first.
 *
 * Opus is the point: 15 seconds of speech is 145 KB as WebM/Opus against
 * 2.1 MB as WAV, measured on the reference clip, and the engine decodes both
 * identically. MP4/AAC is here for Safari, which has never supported WebM in
 * MediaRecorder. The empty string is the last resort: let the browser pick
 * and let the engine work it out — it decodes through ffmpeg.
 */
export const MIME_PREFERENCES = [
  'audio/webm;codecs=opus',
  'audio/webm',
  'audio/ogg;codecs=opus',
  'audio/mp4;codecs=mp4a.40.2',
  'audio/mp4',
  '',
];

/** The first container this browser will actually record, or '' for its default. */
export function pickMimeType(
  supported: (type: string) => boolean = (type) =>
    typeof MediaRecorder !== 'undefined' && MediaRecorder.isTypeSupported(type),
): string {
  for (const type of MIME_PREFERENCES) {
    if (!type) return '';
    try {
      if (supported(type)) return type;
    } catch {
      // A browser that throws from isTypeSupported has answered "no".
    }
  }
  return '';
}

/**
 * Turn a getUserMedia rejection into a sentence.
 *
 * The distinction that matters is PERMISSION versus everything else: a denied
 * microphone is fixed in browser settings and re-prompting will not help, so
 * the UI must say so once and stop asking.
 */
export function describeCaptureError(err: unknown): VoiceError {
  const name =
    typeof err === 'object' && err !== null && 'name' in err
      ? String((err as { name: unknown }).name)
      : '';
  switch (name) {
    case 'NotAllowedError':
    case 'SecurityError':
      return {
        message:
          'Microphone access is blocked. Allow it for this site in your browser settings, then try again.',
        retryable: false,
        permission: true,
      };
    case 'NotFoundError':
    case 'OverconstrainedError':
      return {
        message: 'No microphone was found. Connect one and try again.',
        retryable: false,
      };
    case 'NotReadableError':
      return {
        message:
          'The microphone is in use by another application. Close it and try again.',
        retryable: true,
      };
    case 'AbortError':
      return { message: 'Recording stopped unexpectedly.', retryable: true };
    default:
      return {
        message: 'Recording could not start. Check your microphone and try again.',
        retryable: true,
      };
  }
}

/**
 * Combine a draft with a transcript the way a person would expect.
 *
 * The rules are small and all of them come from watching the alternative go
 * wrong: never lose what was already typed (that is somebody's sentence);
 * separate with exactly one space; do not add a space after an opening
 * bracket or before punctuation; and capitalise nothing — the model already
 * punctuates, and second-guessing it mangles names.
 */
export function mergeTranscript(draft: string, transcript: string): string {
  const spoken = transcript.trim();
  if (!spoken) return draft;
  if (!draft) return spoken;
  const needsSpace = !/[\s([{"'‘“-]$/.test(draft);
  return `${draft}${needsSpace ? ' ' : ''}${spoken}`;
}

/**
 * One bar height, 0..1, from a slice of time-domain samples.
 *
 * RMS, not peak: peak makes every bar full height the moment a chair creaks,
 * where RMS tracks how loud the voice actually is. The curve afterwards is
 * cosmetic and deliberate — speech sits low in a linear scale, so a bare RMS
 * meter looks broken during ordinary talking.
 */
export function levelFrom(samples: Uint8Array): number {
  if (!samples.length) return 0;
  let sum = 0;
  for (let i = 0; i < samples.length; i += 1) {
    const centred = (samples[i]! - 128) / 128;
    sum += centred * centred;
  }
  const rms = Math.sqrt(sum / samples.length);
  // ~3.2x gain, then a gentle curve. Silence still reads as silence: the
  // floor is 0 and a quiet room measures under 0.02.
  return Math.min(1, Math.pow(Math.min(1, rms * 3.2), 0.7));
}

/** The transitions, as a table. Anything not listed here cannot happen. */
const TRANSITIONS: Record<VoiceState, VoiceState[]> = {
  idle: ['requesting'],
  requesting: ['recording', 'idle', 'error'],
  recording: ['transcribing', 'idle', 'error'],
  transcribing: ['idle', 'error'],
  error: ['idle', 'requesting'],
};

export function canTransition(from: VoiceState, to: VoiceState): boolean {
  return TRANSITIONS[from].includes(to);
}

/**
 * How sure the server is of a draft. A closed vocabulary — the orchestrator
 * sends one of these words or null, and the wording below is ours.
 * `app/asr.py` decides it from the engine's own no-speech probability and its
 * plausibility check, both of which used to be computed and thrown away.
 */
export type Confidence = 'low' | 'unclear' | 'silent';

export interface TranscriptionResult {
  text: string;
  language: string | null;
  durationMs: number | null;
  processingMs: number | null;
  /**
   * One short line to show BESIDE the transcript, or null. Never a reason to
   * withhold the text: the words still go into the composer, because a draft
   * a person can edit beats a warning they cannot act on.
   */
  notice: string | null;
}

/**
 * What a person is told when a transcript comes back with nothing in it.
 *
 * SILENCE IS A CLAIM, and until 2026-09-21 it was made for every empty draft
 * — including the ones the orchestrator emptied itself after deciding the
 * engine had invented them, and the ones where a second opinion was never
 * taken. Measured on this fleet the same day: 20 s of pink noise decodes as
 * fourteen words, and a gated clip is only re-decoded when it is between 3
 * and 120 seconds long. "Nothing was said" now needs the server to have
 * measured exactly that.
 */
const EMPTY_MESSAGES: Record<Confidence | 'unknown', string> = {
  silent: 'Nothing was said in that recording.',
  unclear: "That recording wasn't clear enough to transcribe. Try again, closer to the microphone.",
  low: "That recording wasn't clear enough to transcribe. Try again, closer to the microphone.",
  unknown: "That recording wasn't clear enough to transcribe. Try again, closer to the microphone.",
};

/**
 * And what they are told when there IS a draft but it may be invented.
 *
 * One line, shown once, next to text that is already in the composer. Not a
 * dialog and not a block: the person can read the words and decide, which is
 * something no threshold here can do for them.
 */
const LOW_CONFIDENCE_NOTICE =
  'That was hard to make out — check the text before you send it.';

function confidenceOf(value: unknown): Confidence | null {
  return value === 'low' || value === 'unclear' || value === 'silent'
    ? value
    : null;
}

export interface TranscribeFailure {
  error: VoiceError;
}

const GENERIC_FAILURE = 'Transcription couldn’t be completed. Please try again.';

function isAbort(err: unknown, signal?: AbortSignal): boolean {
  return (
    Boolean(signal?.aborted) ||
    (typeof err === 'object' &&
      err !== null &&
      'name' in err &&
      (err as { name: string }).name === 'AbortError')
  );
}

/**
 * A failed transcription, from its status and the server's sentence. The same
 * mapping whether the status came on the status line or, after a heartbeat,
 * in the body.
 *
 * 403 and 404 are the server saying the feature is not for this account or
 * not on this deployment; both are worth quoting verbatim, because they tell
 * the person what to do. So are the refusals about the recording itself, a
 * busy engine, and a 504 (the server words it from the clip's length: "Try a
 * shorter one" for a long clip, "did not answer in time" for a short one).
 * Everything else gets one sentence.
 */
function failureFor(status: number, detail: unknown): TranscribeFailure {
  const quotable =
    status === 403 || status === 404 || status === 413 || status === 422 ||
    status === 429 || status === 503 || status === 504;
  return {
    error: {
      message: (quotable && typeof detail === 'string' && detail) || GENERIC_FAILURE,
      retryable: status !== 403 && status !== 404,
    },
  };
}

/**
 * Send one recording and get its text back.
 *
 * THE RECORDING IS THE BODY, not a multipart field. A FormData upload looks
 * more conventional and costs the person their privacy promise: the server
 * parses multipart through a spooled temporary file that rolls over onto disk
 * past 1 MB — about ninety seconds of speech — before any of our code has seen
 * a byte. Posting the blob itself means the audio exists in memory on both
 * ends and nowhere else. The two scalars that travelled beside it are query
 * parameters, which is all they ever needed to be.
 *
 * The abort signal is the reason this takes one: a person who presses X while
 * "Transcribing…" is showing has withdrawn the request, and the upload should
 * stop rather than complete into a component that no longer wants it.
 */
export async function transcribe(
  blob: Blob,
  options: {
    durationMs: number;
    mimeType: string;
    signal?: AbortSignal;
    fetchImpl?: typeof fetch;
  },
): Promise<TranscriptionResult | TranscribeFailure> {
  const query = new URLSearchParams({
    duration_ms: String(Math.round(options.durationMs)),
    language: 'auto',
  });

  const doFetch = options.fetchImpl ?? fetch;
  let response: Response;
  try {
    response = await doFetch(`/api/audio/transcribe?${query}`, {
      method: 'POST',
      // The container, so the server can refuse a format before spending a
      // GPU slot on it. `blob.type` is what the recorder actually produced;
      // `mimeType` is the fallback for a browser that leaves it empty.
      headers: { 'content-type': blob.type || options.mimeType || 'audio/webm' },
      body: blob,
      signal: options.signal,
    });
  } catch (err) {
    // The caller withdrew. Not an error to report.
    if (isAbort(err, options.signal)) return { error: { message: '', retryable: true } };
    return { error: { message: GENERIC_FAILURE, retryable: true } };
  }

  // A long transcription arrives as a streamed 200: whitespace heartbeats
  // (insignificant in JSON, so `json()` reads straight past them) and then the
  // JSON, over minutes. The read can therefore break mid-way (a proxy restart,
  // a dropped network), and that has to be a failure the person can retry,
  // not a rejected promise that leaves the bar on "Transcribing…" forever. It
  // is also where pressing X now lands most often — during the wait — and
  // that is still a withdrawal. A body that is not JSON is `null` here.
  let payload: Record<string, unknown> | null = null;
  try {
    const parsed: unknown = await response.json();
    if (typeof parsed === 'object' && parsed !== null) {
      payload = parsed as Record<string, unknown>;
    }
  } catch (err) {
    if (isAbort(err, options.signal)) return { error: { message: '', retryable: true } };
    payload = null;
  }

  if (!response.ok) {
    return failureFor(response.status, payload?.detail);
  }
  if (payload === null) {
    return { error: { message: GENERIC_FAILURE, retryable: true } };
  }
  // A failure after the heartbeat started: the status line was already 200,
  // so the server carries the real one in the body. It is mapped exactly as
  // that status would have been, and never read as a (silent) transcript.
  if (typeof payload.status === 'number' && payload.status >= 400) {
    return failureFor(payload.status, payload.detail);
  }

  const text = typeof payload.text === 'string' ? payload.text.trim() : '';
  const confidence = confidenceOf(payload.confidence);
  if (!text) {
    return {
      error: {
        message: EMPTY_MESSAGES[confidence ?? 'unknown'],
        retryable: true,
      },
    };
  }
  return {
    text,
    language: typeof payload.language === 'string' ? payload.language : null,
    durationMs:
      typeof payload.duration_ms === 'number' ? payload.duration_ms : null,
    processingMs:
      typeof payload.processing_ms === 'number' ? payload.processing_ms : null,
    notice: confidence === 'low' ? LOW_CONFIDENCE_NOTICE : null,
  };
}

/** True when this browser can record at all. Checked before the button draws. */
export function voiceSupported(): boolean {
  return (
    typeof navigator !== 'undefined' &&
    typeof navigator.mediaDevices?.getUserMedia === 'function' &&
    typeof MediaRecorder !== 'undefined'
  );
}
