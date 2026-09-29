/**
 * Voice dictation: the state machine, the microphone, the level meter, and
 * the two roads a recording takes to the server.
 *
 * WHY A STATE MACHINE AND NOT FOUR BOOLEANS. Recording has real races —
 * double-clicking Stop, cancelling while the permission prompt is open,
 * navigating away mid-upload, pressing the button again before the last
 * transcript lands. Booleans let two of those be true at once and the UI
 * ends up in a state nobody designed. Here every transition is named and
 * illegal ones simply do not happen:
 *
 *      idle ──start──► requesting ──granted──► recording ──stop──► finishing
 *        ▲                  │                      │                    │
 *        └───── denied ─────┴────── cancel ────────┘                    │
 *        ◄──────────────────── transcript ─────────────────────────────-┘
 *        ◄──────────────────── failure  ─► error ──dismiss──►
 *
 * `finishing` was called `transcribing` until 2026-09-29. On the session road
 * (below) most of the transcript already exists when Stop is pressed; what is
 * left is the tail of the audio, so the old name described the wrong thing.
 *
 * TWO ROADS, AND WHY THE OLD ONE FAILED LONG RECORDINGS (measured 2026-09-28).
 *
 * The LEGACY road, `transcribe()`, uploads one blob when the person presses
 * Stop, and the recorder stopped itself at ten minutes. Two defects lived on
 * it. The speech engine judged the WHOLE clip by its first 30 seconds: 25 s
 * of quiet before speaking returned an empty transcript in 0.76 s for a
 * 181 s recording, and this module then told the person to move "closer to
 * the microphone" — the owner's screen, and the only long dictation in
 * production (181,427 ms, empty, 2,175 ms). And the ten-minute auto-stop
 * reported 600,000 ms plus 0-6 ms, which the server refuses with a 413, so a
 * recording that reached the limit was discarded whole.
 *
 * The SESSION road (2026-09-29) is the default. The recorder runs ONE
 * MediaRecorder for as long as the person talks and hands every timeslice to
 * a `VoiceSession`, which uploads it as a numbered PART of one continuous
 * container stream while recording continues. The server appends the parts
 * into one stored file, cuts transcription windows at pauses with its own
 * voice-activity detector and never asks the engine's 30-second gate, so a
 * quiet opening removes nothing after it. There is no length limit on this
 * road: an hour is 720 parts of about 80 KB, two hours 1,440.
 *
 * WHAT LEAVES THIS MODULE, AND WHAT IS KEPT. On the session road the audio is
 * STORED on the server under the person's account (the owner asked for that
 * on 2026-09-28), and the bar says so. On this device a part is kept only
 * until the server has acknowledged it: in IndexedDB when the browser has it
 * (so a reload or a sign-out loses nothing), otherwise in memory. The
 * transcript is still a draft the composer treats exactly like typed text.
 *
 * THE MICROPHONE IS RELEASED. `stopTracks` runs on every path out of
 * recording — stop, cancel, error, unmount — because a page that keeps the
 * capture indicator lit after the user pressed Stop has broken a promise
 * about hardware, and no transcript is worth that.
 *
 * Pure TypeScript with no React, so the transitions, the wording and the
 * upload protocol are unit-testable without a DOM.
 */

export type VoiceState =
  | 'idle'
  | 'requesting'
  | 'recording'
  | 'finishing'
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
 * The legacy road's ceiling. It exists only because POST /audio/transcribe
 * refuses anything over 600 s; the session road has no ceiling at all.
 */
export const LEGACY_MAX_MS = 10 * 60 * 1000;
/**
 * The legacy recorder stops this long before its ceiling. Stopping AT 10:00
 * posted duration_ms=600002 (the stop lands a few ms late) and the server's
 * 413 "longer than 10 minutes" threw the recording away (backend verifier
 * afadf78ca3614dad5, item J).
 */
export const LEGACY_STOP_EARLY_MS = 5000;

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
 * Put a re-transcribed recording where its first transcript went, without
 * knowing where that was: only a verbatim, unique match is replaced, and
 * otherwise the draft is returned UNCHANGED — never with a second copy.
 *
 * It used to append the new text when the person had edited the first one
 * even by one character: a 9,000-word draft with one word re-capitalised went
 * from 79,889 to 159,786 characters (measured by the verifier on 74ad85e2).
 * The composer uses `placeRetranscript` with the span it tracked, which also
 * merges the person's edits, and asks when it cannot place the text.
 */
export function replaceTranscript(draft: string, previous: string, next: string): string {
  return placeRetranscript(draft, null, previous, next)?.text ?? draft;
}

/**
 * A duration as the person reads it: m:ss, and h:mm:ss from one hour.
 *
 * It said "a dictation is never long enough to need hours" until 2026-09-29;
 * the session road has no length limit, and 1:00:00 rendered as 60:00.
 */
export function formatElapsed(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = total % 60;
  if (hours > 0) {
    return `${hours}:${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}`;
  }
  return `${minutes}:${String(seconds).padStart(2, '0')}`;
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

/** A meter that never rose above this heard nothing at all. */
export const SILENT_METER_LEVEL = 0.02;

/** The transitions, as a table. Anything not listed here cannot happen. */
const TRANSITIONS: Record<VoiceState, VoiceState[]> = {
  idle: ['requesting'],
  requesting: ['recording', 'idle', 'error'],
  recording: ['finishing', 'idle', 'error'],
  finishing: ['idle', 'error'],
  error: ['idle', 'requesting'],
};

export function canTransition(from: VoiceState, to: VoiceState): boolean {
  return TRANSITIONS[from].includes(to);
}

// ===========================================================================
// THE LEGACY ROAD: one blob, one POST, ten minutes at most
// ===========================================================================

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

/** The engine's own clip gate only ever looked at this much of a recording. */
const GATE_WINDOW_MS = 30_000;
/**
 * Above this the orchestrator never takes the ungated second decode
 * (`_RETRY_MAX_SECONDS = 120.0`, orchestrator/app/asr.py), so an empty
 * 'unclear' longer than this was decided by the first 30 seconds alone.
 */
const LEGACY_RETRY_MAX_MS = 120_000;

/**
 * What a person is told when a LEGACY transcript comes back with nothing in it.
 *
 * SILENCE IS A CLAIM, and until 2026-09-21 it was made for every empty draft
 * — including the ones the orchestrator emptied itself after deciding the
 * engine had invented them, and the ones where a second opinion was never
 * taken. Measured on this fleet the same day: 20 s of pink noise decodes as
 * fourteen words, and a gated clip is only re-decoded when it is between 3
 * and 120 seconds long. "Nothing was said" now needs the server to have
 * measured exactly that.
 *
 * SO IS "CLOSER TO THE MICROPHONE" (2026-09-29). It is kept only where the
 * server actually judged the audio unclear. A reply with no confidence at all
 * is the server saying nothing, and it used to get the microphone sentence
 * too. An 'unclear' longer than two minutes was never listened to past its
 * first 30 seconds (the 181 s recording above), and moving the microphone
 * does nothing for a pause at the start.
 */
export const LEGACY_EMPTY_MESSAGES = {
  silent: 'Nothing was said in that recording.',
  unclear: "That recording wasn't clear enough to transcribe. Try again, closer to the microphone.",
  // 'low' is the server's word for "there IS a draft and it may be invented"
  // (orchestrator/app/asr.py CONFIDENCE_LOW). An EMPTY draft marked 'low' is
  // not a judgement that the audio was unclear, so it does not get the
  // microphone sentence (2026-09-29): it got it until then.
  low: 'No words came back, and the server did not say why.',
  unknown: 'No words came back, and the server did not say why.',
  // 'unclear' over two minutes (2026-09-29, backend verifier item K): the
  // server says it for three different things — the gate on the first 30 s
  // emptied the clip, the engine heard speech and its words were judged
  // invented, or the decoder returned nothing with the gate open. Only what
  // is true of all three is stated; the first-30-seconds rule is given as
  // what CAN happen, with what to do about it.
  gatedLong:
    'No words came back for that recording, and the server could not tell whether anything was said. A recording this long is judged by its first 30 seconds, so a quiet start can empty all of it: start speaking right away, or attach long recordings as a file.',
} as const;

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

function emptyMessageFor(confidence: Confidence | null, durationMs: number): string {
  if (confidence === 'unclear' && durationMs > LEGACY_RETRY_MAX_MS && durationMs > GATE_WINDOW_MS) {
    return LEGACY_EMPTY_MESSAGES.gatedLong;
  }
  return LEGACY_EMPTY_MESSAGES[confidence ?? 'unknown'];
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
 * Send one recording and get its text back — the LEGACY road, used only when
 * the server answers the session request with `sessions_off`.
 *
 * THE RECORDING IS THE BODY, not a multipart field, so the orchestrator never
 * spools it through a multipart parser. That is all this can promise now: the
 * orchestrator forwards the clip to the speech engine as multipart form data,
 * and the engine's parser rolls anything over 1,048,576 bytes into a
 * temporary file on its node's disk while it decodes (measured 2026-09-28,
 * Starlette 1.6.0 on the running engine). The comment that stood here said
 * the audio "exists in memory on both ends and nowhere else"; that was never
 * true past about a minute of speech.
 *
 * The abort signal is the reason this takes one: a person who presses X while
 * the bar is waiting has withdrawn the request, and the upload should stop
 * rather than complete into a component that no longer wants it.
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
    // Never over the ceiling the server refuses at: the recorder's own clock
    // runs a few ms past its stop.
    duration_ms: String(Math.round(Math.min(options.durationMs, LEGACY_MAX_MS))),
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
  // not a rejected promise that leaves the bar waiting forever. It is also
  // where pressing X now lands most often — during the wait — and that is
  // still a withdrawal. A body that is not JSON is `null` here.
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
        message: emptyMessageFor(confidence, options.durationMs),
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

// ===========================================================================
// THE SESSION ROAD: parts while recording, stored, no length limit
// ===========================================================================

/**
 * What the person is told, for every reason the session contract names.
 *
 * THE RULES. Nothing on this road says "closer to the microphone": a window
 * is judged by the server's voice-activity detector, never by the engine's
 * 30-second gate, so the empty 'unclear' that sentence was written for cannot
 * happen here. Every failure says whether the audio was kept, because on this
 * road it almost always was. The server sends a closed reason code and a
 * sentence; the wording is ours, and an unknown code gets `generic`.
 *
 * `t` is always a duration from `formatElapsed`.
 */
export const VOICE_MESSAGES = {
  signedOutAtStart: 'You were signed out, so nothing was recorded. Sign in and try again.',
  // "…first." since 2026-09-29: the outbox of an account is deleted when
  // another account signs in on the same browser (security review item 10).
  signedOutKept: (t: string) =>
    `You were signed out. Everything up to ${t} is saved on the server; the last few seconds are kept on this device and will upload when you sign in again, unless someone else signs in on this browser first.`,
  signedOutLost: (t: string) =>
    `You were signed out. Everything up to ${t} is saved on the server; the last few seconds will be lost if you close this tab.`,
  signedOutNothingPending: (t: string) =>
    `You were signed out. Everything up to ${t} is saved on the server.`,
  voiceOff: 'Voice input is turned off for your account. Ask an administrator.',
  voiceUnavailable: "Voice input isn't available on this server right now.",
  legacyHint: "Long recordings aren't enabled here, so this one stops just before 10 minutes.",
  notFound: 'This recording is no longer on the server. It was discarded or has expired.',
  sessionActive:
    "You're already recording in another tab or on another device. End that recording first.",
  sessionActiveAction: 'End that recording',
  lostParts: (t: string) =>
    `The part of your recording after ${t} never reached the server and isn't on this device any more, so the recording ends there. Everything before ${t} is saved and transcribed.`,
  partConflict:
    'This recording is also being uploaded from another tab, so this tab stopped. The copy on the server is kept.',
  closedIdle: (idle: string) =>
    `This recording was closed after ${idle} with no audio arriving, and everything before that is saved. You're not being recorded now. Press the microphone to start a new recording.`,
  closedElsewhere: 'This recording was ended from another tab. What was recorded is saved.',
  unsupportedFormat: (sentence: string) => `${sentence} Try Chrome, Edge or Safari.`,
  rateLimitedCreate: 'Too many recordings were started just now. Wait a moment and try again.',
  capacityFull:
    "Too many people are recording right now, so this one didn't start. Try again in a minute.",
  storageTrouble:
    "The server can't save audio right now. Still recording on this device; it will upload when the server recovers.",
  storageUnavailableAtStart:
    "The server can't save audio right now, so recording didn't start. Try again in a minute.",
  storageFullAtStart: "The server has no space left for recordings, so this one didn't start.",
  storageFullMid: (t: string) =>
    `The server ran out of space, so recording stopped at ${t}. Everything up to then is saved and is being transcribed.`,
  offlineRecording:
    'Connection lost. Still recording; the audio will upload when the connection is back.',
  offlineFinishing: (t: string) =>
    `Waiting for a connection to upload the last ${t} of your recording…`,
  unreachableAtStart: "Can't reach the server, so recording didn't start. Check your connection.",
  sessionBusy: 'This recording is still being transcribed.',
  audioDeleted: "This recording's audio has been deleted, so it can't be transcribed again.",
  generic: (t: string) => `Something went wrong on the server. Your recording up to ${t} is saved.`,
  genericAtStart: "Something went wrong on the server, so recording didn't start. Try again.",
  lowSegments: 'Some parts were hard to make out — check the text before you send it.',
  gaps: (count: number, ranges: string) =>
    `The speech engine couldn't transcribe ${count} part${count === 1 ? '' : 's'} (${ranges}). The audio is saved. Press Retry to transcribe them.`,
  noSpeech: (t: string) =>
    `No speech was detected in this ${t} recording. If you did speak, check that the right microphone is selected and isn't muted. The recording is saved.`,
  noSoundAtAll:
    "The microphone picked up no sound at all during this recording. Check that the right microphone is selected and isn't muted.",
  noWords:
    'Sound was detected, but no words could be made out. It may have been background noise or music. The recording is saved.',
  engineUnavailable: (t: string) =>
    `The speech engine was unavailable, so nothing was transcribed yet. Your ${t} recording is saved. Press Retry, or try again in a few minutes.`,
  undecodable:
    "The server couldn't read the audio in this recording. The file is saved exactly as it arrived.",
  recorderError: (t: string) =>
    `Recording stopped unexpectedly at ${t}. Everything up to then is saved and is being transcribed.`,
  pausedHidden: (from: string, to: string) =>
    `Recording paused while the screen was off (${from} to ${to}). Keep the screen on while recording. Everything else is saved.`,
  discardConfirm: (t: string) =>
    `Discard this ${t} recording? It is saved on the server until you discard it.`,
  saved: (retentionDays: number | null) =>
    retentionDays && retentionDays > 0
      ? `Saved to your account · kept ${retentionDays} days`
      : 'Saved to your account',
  // Said only once the server has acknowledged audio (2026-09-29): it was
  // drawn whenever progress existed, under "Connection lost…" with 0 bytes on
  // the server (backend verifier afadf78ca3614dad5, item H).
  savedSoFar: (saved: string, onDevice: string | null, retentionDays: number | null) =>
    `Saved to your account: ${saved}${onDevice ? ` · ${onDevice} still on this device` : ''}${
      retentionDays && retentionDays > 0 ? ` · kept ${retentionDays} days` : ''
    }`,
  // J: the legacy road keeps a refused recording.
  legacyKept: 'The recording is kept here: try again, or save it as a file.',
  legacyRetry: 'Try again',
  legacySave: 'Save it',
  // 'engine' is the server saying the engine is BUSY; 'engine_unavailable'
  // (fix/voice-server-hardening) that it cannot be reached. Until the server
  // told them apart, an outage read as other people's recordings.
  behind: (t: string, waitingOn: WaitingOn) =>
    waitingOn === 'chat'
      ? `Transcript ${t} behind — paused while someone is waiting for a chat answer`
      : waitingOn === 'engine'
        ? `Transcript ${t} behind — the speech engine is busy with other recordings`
        : waitingOn === 'engine_unavailable'
          ? `Transcript ${t} behind — the speech service is unavailable right now; your audio is saved and will be transcribed when it is back`
          : `Transcript ${t} behind`,
  finishingTail: (t: string) => `Finishing the last ${t} of audio…`,
  finishing: 'Finishing the transcript…',
  notProgressive: 'This browser’s recording can only be transcribed once you stop. It is being saved as you talk.',
  recovered: (t: string) =>
    `A ${t} recording was interrupted when the page closed. It was saved and transcribed.`,
  recoveredAction: 'Insert it',
  retry: 'Retry',
  retrying: 'Transcribing the saved recording again…',
  retryUnreachable:
    "Can't reach the server, so the recording was not transcribed again. Check your connection.",
  retrySignedOut:
    'You were signed out, so the recording was not transcribed again. Sign in and try again.',
  retryGeneric: 'Something went wrong on the server. Your recording is saved.',
  tooShort: 'That was too short to transcribe. Hold the button and speak.',
  // --- 2026-09-29, fix/voice-recorder-edges ---------------------------------
  // A discard is never reported done before the server said so.
  discardPending:
    "Couldn't delete the recording from the server yet — will retry. Nothing of it is kept on this device.",
  discardPendingVolatile:
    "Couldn't delete the recording from the server yet — will retry while this tab stays open. Nothing of it is kept on this device.",
  discardDone: 'The discarded recording is deleted from the server.',
  discardTryNow: 'Try now',
  discardAction: 'Discard',
  sessionActiveDiscarded:
    "A recording you discarded couldn't be deleted from the server yet, and it blocks a new one. Delete it now?",
  // Audio the server would not take is kept on this device, never deleted.
  heldIdle: (idle: string, saved: string, held: string) =>
    `This recording was closed after ${idle} with no audio arriving. Everything up to ${saved} is saved on the server. The last ${held} is kept on this device, because the server would not take it after that. You're not being recorded now.`,
  heldElsewhere: (held: string) =>
    `This recording was ended from another tab. What reached the server is saved; the last ${held} is kept on this device, because the server would not take it after that.`,
  heldStorageFull: (saved: string, held: string) =>
    `The server ran out of space, so recording stopped at ${saved}. Everything up to then is saved and is being transcribed; the last ${held} is kept on this device.`,
  heldVoiceOff: (sentence: string, held: string) =>
    `${sentence} The last ${held} of this recording is kept on this device.`,
  heldGeneric: (saved: string, held: string) =>
    `Something went wrong on the server. Your recording up to ${saved} is saved there; the last ${held} is kept on this device.`,
  heldUndecodable: (held: string) =>
    `The server couldn't read the audio of this recording, so it stopped taking more of it. What arrived is saved exactly as it arrived; the last ${held} is kept on this device.`,
  retranscribeBusy: 'Another recording of yours is being transcribed again right now. Try this one when it is done.',
  retranscribeRateLimited: 'Recordings were transcribed again too often in the last hour. Try again later.',
  heldStill: (held: string) =>
    `The server still won't take the last ${held} of this recording, so it stays on this device.`,
  heldFound: (held: string) =>
    `The last ${held} of an earlier recording is on this device, because the server would not take it.`,
  uploadRest: 'Upload the rest',
  offlineLong: (idle: string) =>
    `Connection lost for over ${idle}. Still recording on this device; the server stops waiting after ${idle}, so what it missed stays here until it can be uploaded.`,
  // Retry never doubles the draft: it replaces its own earlier text, or asks.
  retryUnplaced:
    'The transcript in your message was changed in a way the new one could not be merged into, so it was not put in.',
  // Asked before an explicit logout.
  logoutHeld: (t: string) =>
    `${t} of a recording on this browser hasn't reached the server yet. Signing out deletes it from this browser. Sign out anyway?`,
  // Server capacity (2026-09-29, with fix/voice-server-hardening).
  quotaAtStart:
    "Your recordings have used all the space your account has, so this one didn't start. Delete some on the Recordings page (/recordings) to record again.",
  quotaMid: (t: string) =>
    `Your recordings have used all the space your account has, so recording stopped at ${t}. Everything up to then is saved and is being transcribed. Delete some on the Recordings page (/recordings) to record again.`,
  heldQuota: (saved: string, held: string) =>
    `Your recordings have used all the space your account has, so recording stopped at ${saved}. Everything up to then is saved; the last ${held} is kept on this device. Delete some on the Recordings page (/recordings), then press Upload the rest.`,
  capacityLegacyHint:
    "Too many people are recording right now, so this recording isn't saved to your account and stops just before 10 minutes.",
  engineUnavailableLive:
    'The speech service is unavailable right now; your audio is saved and will be transcribed when it is back.',
  retryConfirm: (t: string) =>
    `Transcribe ${t} of this recording again? The speech engine works on it for a while, and replies are slower for everyone meanwhile.`,
  logoutDiscardPending:
    "A recording you discarded hasn't been deleted from the server yet (it couldn't be reached). It will be deleted the next time you sign in on this browser. Sign out anyway?",
} as const;

/** Past this, discarding a recording asks first (the contract's 60 s). */
export const DISCARD_CONFIRM_AFTER_MS = 60_000;
/**
 * Past this much audio, Retry asks first: it re-decodes that audio at finish
 * priority, and while it runs chat was measured at about 53 tok/s against
 * about 106 (or 69) idle (2026-09-28, the cost verdict on this branch).
 */
export const RETRY_CONFIRM_AFTER_MS = 5 * 60_000;
/** The live preview keeps this many characters; the full text comes at the end. */
export const PREVIEW_CHARS = 600;
/** How far behind the transcript may run before the bar says so. */
export const BACKLOG_NOTICE_MS = 15_000;
/** How long storage trouble must last before the person is told. */
const STORAGE_TROUBLE_AFTER_MS = 60_000;
/**
 * An outbox this long untouched belongs to a tab that is gone. A live tab
 * touches its record with every 5 s part and every retry.
 */
export const OUTBOX_STALE_MS = 30_000;

export type SessionStatus = 'recording' | 'finishing' | 'done' | 'failed' | 'cancelled';
export type SessionOutcome =
  | 'transcribed'
  | 'transcribed_with_gaps'
  | 'no_speech'
  | 'no_words'
  | 'engine_unavailable'
  | 'undecodable';
export type WaitingOn = 'none' | 'chat' | 'engine' | 'engine_unavailable';
export type EndedBy = 'person' | 'recorder_error' | 'lost_parts' | 'page_hidden';

/**
 * The live-transcript socket a session offers (2026-09-29), or none: the
 * server sends `"live": null` when the live path is off, and the legacy road
 * never has one. lib/voiceLive.ts is the client.
 */
export interface LiveConfig {
  /** The socket's path on this origin, with `{id}` where the session id goes. */
  path: string;
  /** Always 16000: the only rate the browser tap produces. */
  sampleRate: number;
  /** Always 40: one 640-sample frame per message. */
  frameMs: number;
  /** How much buffered audio a reconnect may replay, in seconds. */
  resumeMaxS: number;
}

export interface SessionConfig {
  /** The timeslice the recorder MUST use. */
  partMs: number;
  partLimitBytes: number;
  /** Passed as audioBitsPerSecond when not null. */
  bitsPerSecond: number | null;
  idleCloseS: number;
  longPollMaxS: number;
  /** The live-transcript socket, or null when the server has none. */
  live: LiveConfig | null;
}

export const DEFAULT_SESSION_CONFIG: SessionConfig = {
  partMs: 5000,
  partLimitBytes: 8 * 1024 * 1024,
  bitsPerSecond: null,
  idleCloseS: 600,
  longPollMaxS: 25,
  live: null,
};

/**
 * The `live` block of a session's config, or null for anything this client
 * cannot honour. The browser tap produces exactly 16 kHz in 40 ms frames, so a
 * server asking for another rate or frame size gets no live stream rather
 * than audio it would misread; the path must name the session with `{id}`
 * (lib/voiceLive.ts `liveSocketUrl` checks what it becomes). The orchestrator
 * also sends the path as `path_template`; either name is read.
 */
export function parseLiveConfig(raw: unknown): LiveConfig | null {
  if (typeof raw !== 'object' || raw === null || Array.isArray(raw)) return null;
  const r = raw as Record<string, unknown>;
  const path = typeof r.path === 'string' ? r.path : r.path_template;
  if (typeof path !== 'string' || !path.startsWith('/') || !path.includes('{id}')) return null;
  if ((r.sample_rate ?? 16000) !== 16000 || (r.frame_ms ?? 40) !== 40) return null;
  const resume =
    typeof r.resume_max_s === 'number' && Number.isFinite(r.resume_max_s) && r.resume_max_s >= 0
      ? r.resume_max_s
      : 60;
  return { path, sampleRate: 16000, frameMs: 40, resumeMaxS: resume };
}

export interface SessionSegment {
  i: number;
  startMs: number;
  endMs: number;
  text: string;
  language: string | null;
  low: boolean;
}

export interface SessionGap {
  startMs: number;
  endMs: number;
  reason: string;
}

/** The SESSION STATE object every session endpoint returns, parsed defensively. */
export interface SessionState {
  sessionId: string;
  status: SessionStatus;
  rev: number;
  nextPart: number;
  bytesStored: number;
  audioMs: number;
  transcribedMs: number;
  backlogMs: number;
  waitingOn: WaitingOn;
  progressive: boolean;
  cursor: number;
  segments: SessionSegment[];
  tentative: string;
  gaps: SessionGap[];
  outcome: SessionOutcome | string | null;
  text: string | null;
  language: string | null;
  languageCode: string | null;
  speechMs: number;
  endedBy: string | null;
  retentionDays: number | null;
  error: { reason: string; detail: string } | null;
}

const num = (v: unknown, fallback = 0): number =>
  typeof v === 'number' && Number.isFinite(v) ? v : fallback;
const str = (v: unknown): string | null => (typeof v === 'string' ? v : null);

const SESSION_ID = /^[0-9a-f]{32}$/;

export function isSessionId(value: unknown): value is string {
  return typeof value === 'string' && SESSION_ID.test(value);
}

/** Parse a session state, or null when the body is not one. */
export function parseSessionState(body: unknown): SessionState | null {
  if (typeof body !== 'object' || body === null) return null;
  const b = body as Record<string, unknown>;
  if (!isSessionId(b.session_id)) return null;
  const status = str(b.status);
  if (
    status !== 'recording' && status !== 'finishing' && status !== 'done' &&
    status !== 'failed' && status !== 'cancelled'
  ) {
    return null;
  }
  const segments: SessionSegment[] = Array.isArray(b.segments)
    ? b.segments.flatMap((raw) => {
        if (typeof raw !== 'object' || raw === null) return [];
        const s = raw as Record<string, unknown>;
        if (typeof s.i !== 'number' || typeof s.text !== 'string') return [];
        return [
          {
            i: s.i,
            startMs: num(s.start_ms),
            endMs: num(s.end_ms),
            text: s.text,
            language: str(s.language),
            low: s.low === true,
          },
        ];
      })
    : [];
  const gaps: SessionGap[] = Array.isArray(b.gaps)
    ? b.gaps.flatMap((raw) => {
        if (typeof raw !== 'object' || raw === null) return [];
        const g = raw as Record<string, unknown>;
        return [{ startMs: num(g.start_ms), endMs: num(g.end_ms), reason: str(g.reason) ?? '' }];
      })
    : [];
  const waiting = str(b.waiting_on);
  const stored =
    typeof b.stored === 'object' && b.stored !== null
      ? (b.stored as Record<string, unknown>)
      : null;
  const err =
    typeof b.error === 'object' && b.error !== null ? (b.error as Record<string, unknown>) : null;
  return {
    sessionId: b.session_id,
    status,
    rev: num(b.rev, -1),
    nextPart: num(b.next_part),
    bytesStored: num(b.bytes_stored),
    audioMs: num(b.audio_ms),
    transcribedMs: num(b.transcribed_ms),
    backlogMs: num(b.backlog_ms),
    waitingOn:
      waiting === 'chat' || waiting === 'engine' || waiting === 'engine_unavailable' ? waiting : 'none',
    progressive: b.progressive !== false,
    cursor: num(b.cursor),
    segments,
    tentative: str(b.tentative) ?? '',
    gaps,
    outcome: str(b.outcome),
    text: str(b.text),
    language: str(b.language),
    languageCode: str(b.language_code),
    speechMs: num(b.speech_ms),
    endedBy: str(b.ended_by),
    retentionDays: stored ? num(stored.retention_days, 0) : null,
    error: err ? { reason: str(err.reason) ?? '', detail: str(err.detail) ?? '' } : null,
  };
}

/** The CONFIG the create response adds, nested or flat, with the defaults. */
export function parseSessionConfig(body: unknown): SessionConfig {
  const b = (typeof body === 'object' && body !== null ? body : {}) as Record<string, unknown>;
  const c = (typeof b.config === 'object' && b.config !== null ? b.config : b) as Record<
    string,
    unknown
  >;
  const positive = (v: unknown, fallback: number) =>
    typeof v === 'number' && Number.isFinite(v) && v > 0 ? v : fallback;
  return {
    partMs: positive(c.part_ms, DEFAULT_SESSION_CONFIG.partMs),
    partLimitBytes: positive(c.part_limit_bytes, DEFAULT_SESSION_CONFIG.partLimitBytes),
    bitsPerSecond:
      typeof c.bits_per_second === 'number' && c.bits_per_second > 0 ? c.bits_per_second : null,
    idleCloseS: positive(c.idle_close_s, DEFAULT_SESSION_CONFIG.idleCloseS),
    longPollMaxS: positive(c.long_poll_max_s, DEFAULT_SESSION_CONFIG.longPollMaxS),
    live: parseLiveConfig(c.live),
  };
}

/**
 * Join two pieces of transcript for the live PREVIEW only.
 *
 * The final text comes from the server whole, because joining segments with
 * spaces is wrong for scripts written without them. The preview has to join
 * something, so it adds a space only where neither side already has one and
 * neither side is Thai, Lao, Burmese, Khmer, Japanese or Chinese.
 */
export function joinPreview(left: string, right: string): string {
  if (!left) return right;
  if (!right) return left;
  if (/\s$/.test(left) || /^\s/.test(right)) return left + right;
  const noSpace = /[฀-໿က-႟ក-៿぀-ヿ㐀-䶿一-鿿豈-﫿]/;
  if (noSpace.test(left.slice(-1)) && noSpace.test(right.slice(0, 1))) return left + right;
  return `${left} ${right}`;
}

/** "4:10–4:40, 22:05–22:35", at most three ranges and a count of the rest. */
export function describeGaps(gaps: SessionGap[]): string {
  const shown = gaps
    .slice(0, 3)
    .map((g) => `${formatElapsed(g.startMs)}–${formatElapsed(g.endMs)}`);
  const rest = gaps.length - shown.length;
  return rest > 0 ? `${shown.join(', ')} and ${rest} more` : shown.join(', ');
}

/** "10 minutes", from the server's idle close in seconds. */
export function idleWords(seconds: number): string {
  if (seconds >= 60 && seconds % 60 === 0) {
    const minutes = seconds / 60;
    return `${minutes} minute${minutes === 1 ? '' : 's'}`;
  }
  return seconds >= 60 ? formatElapsed(seconds * 1000) : `${seconds} seconds`;
}

// ---------------------------------------------------------------------------
// Talking to the session endpoints
// ---------------------------------------------------------------------------

/**
 * One answer from a session endpoint, sorted into what the caller can do
 * about it. `unreachable` covers every answer that is not the orchestrator
 * speaking: no response at all, the proxy's own 502/504, and an HTML error
 * page from the edge. Those are the ones a dropped network produces, and the
 * ones worth sending again.
 */
export type Reply =
  | { kind: 'ok'; status: number; body: Record<string, unknown> }
  | {
      kind: 'refused';
      status: number;
      reason: string | null;
      detail: string | null;
      body: Record<string, unknown>;
      retryAfterMs: number | null;
    }
  /**
   * `timedOut`: no answer inside the request's own deadline. `restarted`: the
   * browser came back online while it was in flight, so it was abandoned to be
   * sent again at once.
   */
  | { kind: 'unreachable'; status: number | null; timedOut?: boolean; restarted?: boolean }
  | { kind: 'aborted' };

const PROXY_REASONS = new Set(['proxy_unreachable', 'proxy_timeout']);

/**
 * Deadlines for the session requests (2026-09-29).
 *
 * NOTHING TIMED A REQUEST OUT before, and one PUT whose socket died without a
 * FIN or RST (a phone changing networks, a NAT that forgot the flow) never
 * settled: every later part queued behind it. Measured in
 * tests/voice-session-edges.test.ts with a fetch that never answers: eleven
 * minutes of recording plus an `online` event made 7 PUTs and stored 6 of 138
 * slices. A timed-out request is a network failure like any other and is sent
 * again, byte for byte, on the backoff schedule.
 *
 * A part's deadline grows with its size, so a slow link is not mistaken for a
 * dead one: 20 s plus one second per 32 KiB, which assumes 256 kb/s, twice
 * the 128.7 kb/s Chrome records at (a link slower than that cannot keep up
 * with the recording anyway). An 80 KB part gets 23 s; an 8 MiB part coalesced
 * after an outage gets 276 s. Each consecutive time-out of the SAME part
 * doubles its deadline (to 8x), so a link that is merely slow still finishes
 * it — the part cannot be cut smaller instead, because it may already be on
 * the server under its sequence number, and a replay must be byte-identical.
 */
export const REQUEST_TIMEOUT_MS = 30_000;
/** Added to the long-poll's own `wait_s`: the server answers at wait_s at the latest. */
export const LONG_POLL_SLACK_MS = 15_000;
const PART_FLOOR_BYTES_PER_S = 32 * 1024;

export function partTimeoutMs(bytes: number, timeoutsSoFar = 0): number {
  const base = 20_000 + Math.ceil(bytes / PART_FLOOR_BYTES_PER_S) * 1000;
  return base * 2 ** Math.min(Math.max(0, timeoutsSoFar), 3);
}

/**
 * The request a session has in flight, so the browser's `online` event can
 * abort it and send it again at once: a request started on the network that
 * just went away will not come back by itself.
 */
export interface InflightSlot {
  current: (() => void) | null;
}

export async function callSessionApi(
  fetchImpl: typeof fetch,
  url: string,
  init: RequestInit,
  opts: { timeoutMs?: number; slot?: InflightSlot } = {},
): Promise<Reply> {
  const outer = init.signal ?? undefined;
  if (outer?.aborted) return { kind: 'aborted' };
  const controller = new AbortController();
  let expired = false;
  let restarted = false;
  const cut = () => {
    expired = true;
    controller.abort();
  };
  const restart = () => {
    restarted = true;
    controller.abort();
  };
  const onOuterAbort = () => controller.abort();
  outer?.addEventListener('abort', onOuterAbort);
  const timer = opts.timeoutMs ? setTimeout(cut, opts.timeoutMs) : null;
  if (opts.slot) opts.slot.current = restart;
  const settle = () => {
    if (timer !== null) clearTimeout(timer);
    outer?.removeEventListener('abort', onOuterAbort);
    if (opts.slot && opts.slot.current === restart) opts.slot.current = null;
  };
  // An abort of our own (the deadline, or `online`) is a network failure to
  // send again; only the caller's own signal is a withdrawal.
  const failed = (err: unknown, status: number | null): Reply => {
    if (outer?.aborted) return { kind: 'aborted' };
    if (restarted) return { kind: 'unreachable', status, restarted: true };
    if (expired) return { kind: 'unreachable', status, timedOut: true };
    if (isAbort(err)) return { kind: 'aborted' };
    return { kind: 'unreachable', status };
  };
  let response: Response;
  try {
    response = await fetchImpl(url, { ...init, signal: controller.signal, cache: 'no-store' });
  } catch (err) {
    settle();
    return failed(err, null);
  }
  let raw = '';
  try {
    // The deadline still runs: a socket can also die half-way through a body.
    raw =
      typeof response.text === 'function'
        ? await response.text()
        : JSON.stringify(await (response as Response).json());
  } catch (err) {
    settle();
    return failed(err, response.status);
  }
  settle();
  let body: Record<string, unknown> | null = null;
  if (raw.trim()) {
    try {
      const parsed: unknown = JSON.parse(raw);
      if (typeof parsed === 'object' && parsed !== null) body = parsed as Record<string, unknown>;
    } catch {
      body = null;
    }
  }
  if (response.ok) return { kind: 'ok', status: response.status, body: body ?? {} };
  // Not JSON: an edge error page or a proxy that fell over. Not the server.
  if (body === null) return { kind: 'unreachable', status: response.status };
  // FLAT per the contract; a nested FastAPI `{"detail": {...}}` is read too.
  const nested =
    typeof body.detail === 'object' && body.detail !== null
      ? (body.detail as Record<string, unknown>)
      : null;
  const flat = nested ?? body;
  const reason = str(flat.reason);
  if (reason && PROXY_REASONS.has(reason)) return { kind: 'unreachable', status: response.status };
  const retryAfter = Number(response.headers?.get?.('retry-after') ?? NaN);
  return {
    kind: 'refused',
    status: response.status,
    reason,
    detail: str(flat.detail) ?? str(flat.message),
    body: { ...body, ...flat },
    retryAfterMs: Number.isFinite(retryAfter) && retryAfter >= 0 ? retryAfter * 1000 : null,
  };
}

/**
 * The retry schedule for everything that can be sent again: 1, 2, 4, 8 and
 * 16 seconds, then every 30 seconds for as long as it takes, each with
 * ±20% jitter so a room full of phones that lost the same Wi-Fi does not
 * come back in step.
 */
export function backoffMs(attempt: number, random: () => number = Math.random): number {
  const base = attempt < 5 ? 1000 * 2 ** attempt : 30_000;
  return Math.round(base * (0.8 + 0.4 * random()));
}

/** Refusals of a part that mean "send the same bytes again". */
function partRetryable(reply: Reply): boolean {
  if (reply.kind === 'unreachable') return true;
  if (reply.kind !== 'refused') return false;
  const { status } = reply;
  if (status === 408 || status === 422 || status === 429) return true;
  return status >= 500 && status !== 507;
}

export async function sha256Hex(bytes: Uint8Array): Promise<string> {
  const digest = await crypto.subtle.digest('SHA-256', bytes as unknown as ArrayBuffer);
  return Array.from(new Uint8Array(digest), (b) => b.toString(16).padStart(2, '0')).join('');
}

/**
 * A Blob's bytes. `Blob.arrayBuffer` is missing from Safari before 14 and from
 * jsdom, and FileReader is in both.
 */
export async function blobBytes(blob: Blob): Promise<Uint8Array> {
  if (typeof (blob as Blob & { arrayBuffer?: unknown }).arrayBuffer === 'function') {
    return new Uint8Array(await blob.arrayBuffer());
  }
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(new Uint8Array(reader.result as ArrayBuffer));
    reader.onerror = () => reject(reader.error);
    reader.readAsArrayBuffer(blob);
  });
}

/** A v4 UUID for `client_key`, from randomUUID where the browser has it. */
export function newClientKey(): string {
  const c = globalThis.crypto as Crypto | undefined;
  if (c && typeof c.randomUUID === 'function') return c.randomUUID();
  const b = new Uint8Array(16);
  if (c && typeof c.getRandomValues === 'function') c.getRandomValues(b);
  else for (let i = 0; i < 16; i += 1) b[i] = Math.floor(Math.random() * 256);
  b[6] = (b[6]! & 0x0f) | 0x40;
  b[8] = (b[8]! & 0x3f) | 0x80;
  const h = Array.from(b, (x) => x.toString(16).padStart(2, '0')).join('');
  return `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20)}`;
}

const SESSIONS_URL = '/api/audio/sessions';

export interface SessionDeps {
  fetchImpl?: typeof fetch;
  store?: OutboxStore;
  now?: () => number;
  random?: () => number;
  sha256?: (bytes: Uint8Array) => Promise<string>;
}

function doSleep(ms: number, wake: { current: (() => void) | null }): Promise<void> {
  return new Promise((resolve) => {
    const done = () => {
      clearTimeout(timer);
      if (wake.current === done) wake.current = null;
      resolve();
    };
    const timer = setTimeout(done, ms);
    wake.current = done;
  });
}

/** What opening a session came to. */
export type OpenResult =
  | { kind: 'session'; sessionId: string; config: SessionConfig; state: SessionState }
  /** `capacity_full`: the server has no room for another stored recording right now. */
  | { kind: 'legacy'; reason?: 'capacity_full' }
  | { kind: 'active'; sessionId: string | null; audioMs: number }
  | { kind: 'error'; error: VoiceError }
  | { kind: 'aborted' };

/**
 * POST /api/audio/sessions, with the same client_key on every attempt so a
 * create whose answer was lost never makes two sessions.
 *
 * Only an answer that is not the server's (the network, the proxy, the edge)
 * is tried again, twice; every refusal the server words itself is final.
 *
 * A 404 that does not say `voice_unavailable` means this server has no
 * session endpoint (`sessions_off`, or an orchestrator older than the
 * contract), and the recorder takes the legacy road. That keeps dictation
 * working while the two halves of this change roll out separately.
 */
export async function openSession(
  input: { clientKey: string; mimeType: string; partMs?: number },
  deps: SessionDeps & { signal?: AbortSignal } = {},
): Promise<OpenResult> {
  const fetchImpl = deps.fetchImpl ?? fetch;
  const random = deps.random ?? Math.random;
  const wake = { current: null as (() => void) | null };
  for (let attempt = 0; ; attempt += 1) {
    const reply = await callSessionApi(
      fetchImpl,
      SESSIONS_URL,
      {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({
          client_key: input.clientKey,
          mime_type: input.mimeType,
          language: 'auto',
          part_ms: input.partMs ?? DEFAULT_SESSION_CONFIG.partMs,
        }),
        signal: deps.signal,
      },
      { timeoutMs: REQUEST_TIMEOUT_MS },
    );
    if (reply.kind === 'aborted') return { kind: 'aborted' };
    if (reply.kind === 'ok') {
      const state = parseSessionState(reply.body);
      if (!state) return { kind: 'error', error: { message: VOICE_MESSAGES.genericAtStart, retryable: true } };
      return { kind: 'session', sessionId: state.sessionId, config: parseSessionConfig(reply.body), state };
    }
    if (reply.kind === 'unreachable') {
      if (attempt < 2 && !deps.signal?.aborted) {
        await doSleep(backoffMs(attempt, random), wake);
        continue;
      }
      return { kind: 'error', error: { message: VOICE_MESSAGES.unreachableAtStart, retryable: true } };
    }
    return openRefusal(reply);
  }
}

function openRefusal(reply: Extract<Reply, { kind: 'refused' }>): OpenResult {
  const { status, reason } = reply;
  if (status === 401) {
    return { kind: 'error', error: { message: VOICE_MESSAGES.signedOutAtStart, retryable: false } };
  }
  if (status === 403) {
    return {
      kind: 'error',
      error: { message: reply.detail || VOICE_MESSAGES.voiceOff, retryable: false },
    };
  }
  if (status === 404) {
    if (reason === 'voice_unavailable') {
      return { kind: 'error', error: { message: VOICE_MESSAGES.voiceUnavailable, retryable: false } };
    }
    return { kind: 'legacy' };
  }
  if (status === 409 && reason === 'session_active') {
    const id = reply.body.session_id;
    return {
      kind: 'active',
      sessionId: isSessionId(id) ? id : null,
      audioMs: num(reply.body.audio_ms),
    };
  }
  if (status === 415) {
    return {
      kind: 'error',
      error: {
        message: VOICE_MESSAGES.unsupportedFormat(
          reply.detail || 'This audio format is not supported.',
        ),
        retryable: false,
      },
    };
  }
  if (status === 429) {
    return { kind: 'error', error: { message: VOICE_MESSAGES.rateLimitedCreate, retryable: true } };
  }
  if (status === 503 && reason === 'capacity_full') {
    // The legacy road has its own pool: a short dictation still works, and
    // the recorder says it is not stored (fix/voice-server-hardening decision).
    return { kind: 'legacy', reason: 'capacity_full' };
  }
  if (status === 507 && (reason === 'quota_full' || reason === 'quota_exceeded')) {
    return { kind: 'error', error: { message: VOICE_MESSAGES.quotaAtStart, retryable: false } };
  }
  if (status === 415 && reason === 'bad_content_type') {
    // Our own request was malformed; the audio format was never judged.
    return { kind: 'error', error: { message: VOICE_MESSAGES.genericAtStart, retryable: true } };
  }
  if (status === 503 && reason === 'storage_unavailable') {
    return {
      kind: 'error',
      error: { message: VOICE_MESSAGES.storageUnavailableAtStart, retryable: true },
    };
  }
  if (status === 507) {
    return { kind: 'error', error: { message: VOICE_MESSAGES.storageFullAtStart, retryable: false } };
  }
  return { kind: 'error', error: { message: VOICE_MESSAGES.genericAtStart, retryable: true } };
}

/**
 * What one DELETE came to. `gone` is the server's 204, or its 404 for an id
 * in THIS account's own outbox or tombstones (the outbox is per account, so a
 * 404 there cannot be someone else's recording). `later` is everything that
 * may go through if sent again: no network, a 5xx, a sign-out (the cookie of
 * the account that recorded it will come back), a voice feature turned off.
 */
export type DeleteOutcome = 'gone' | 'later';

export async function deleteSessionOnce(
  sessionId: string,
  deps: SessionDeps = {},
): Promise<DeleteOutcome> {
  const reply = await callSessionApi(
    deps.fetchImpl ?? fetch,
    `${SESSIONS_URL}/${sessionId}`,
    { method: 'DELETE' },
    { timeoutMs: REQUEST_TIMEOUT_MS },
  );
  if (reply.kind === 'ok') return 'gone';
  if (reply.kind === 'refused' && reply.status === 404) return 'gone';
  return 'later';
}

/** DELETE a session: the person's discard, or a session that never got audio. */
export async function deleteSession(
  sessionId: string,
  deps: SessionDeps & { attempts?: number } = {},
): Promise<boolean> {
  const wake = { current: null as (() => void) | null };
  const attempts = deps.attempts ?? 4;
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    if ((await deleteSessionOnce(sessionId, deps)) === 'gone') return true;
    if (attempt + 1 < attempts) await doSleep(backoffMs(attempt, deps.random ?? Math.random), wake);
  }
  return false;
}

/**
 * End a session this tab does not own — the "End that recording" button on a
 * 409 session_active. `last_part: null` ends it at whatever the server holds.
 */
export async function endOtherSession(
  sessionId: string,
  deps: SessionDeps = {},
): Promise<VoiceError | null> {
  const reply = await callSessionApi(
    deps.fetchImpl ?? fetch,
    `${SESSIONS_URL}/${sessionId}/finish`,
    {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ last_part: null, duration_ms: 0, ended_by: 'person' }),
    },
    { timeoutMs: REQUEST_TIMEOUT_MS },
  );
  if (reply.kind === 'ok') return null;
  if (reply.kind === 'refused' && reply.status === 404) return null;
  if (reply.kind === 'refused' && reply.status === 401) {
    return { message: VOICE_MESSAGES.signedOutAtStart, retryable: false };
  }
  if (reply.kind === 'refused' && reply.status === 403) {
    return { message: reply.detail || VOICE_MESSAGES.voiceOff, retryable: false };
  }
  if (reply.kind === 'unreachable') {
    return { message: VOICE_MESSAGES.unreachableAtStart, retryable: true };
  }
  return { message: VOICE_MESSAGES.genericAtStart, retryable: true };
}

// ---------------------------------------------------------------------------
// What a finished session comes to
// ---------------------------------------------------------------------------

/** Something the person can do about a result, drawn as a button beside it. */
export type VoiceOffer =
  | {
      kind: 'retranscribe';
      sessionId: string;
      scope: 'gaps' | 'all';
      /** The text already put into the draft, which the new text replaces. */
      replaces: string | null;
      message: string;
      label: string;
      /**
       * How much audio a Retry sends to the engine again: the whole recording
       * after engine_unavailable, the gaps otherwise. Past five minutes the
       * person is asked first (it slows chat for everyone meanwhile).
       */
      audioMs?: number;
    }
  | { kind: 'end_other'; sessionId: string; message: string; label: string }
  /**
   * A 409 session_active for a recording THIS browser was told to discard and
   * could not yet delete: the button deletes it, rather than finishing (and
   * so keeping) what the person threw away.
   */
  | { kind: 'discard_other'; sessionId: string; message: string; label: string }
  /** The server refused the rest of a recording; it is kept on this device. */
  | {
      kind: 'upload_rest';
      sessionId: string;
      /** The text already put into the draft from this recording, which the full one replaces. */
      replaces: string | null;
      message: string;
      label: string;
    }
  /** A discard whose DELETE has not been confirmed; the button tries again now. */
  | { kind: 'discard_pending'; sessionId: string; message: string; label: string }
  | {
      kind: 'insert';
      text: string;
      message: string;
      label: string;
      /** The recording it came from, so its outbox record goes once the text is in. */
      sessionId?: string;
      /** Set when this text should replace an earlier transcript of the same recording. */
      replaces?: string | null;
      /** What to offer once the text is in (a Retry for gaps, "Upload the rest"). */
      then?: VoiceOffer | null;
    };

export type SessionResult =
  | {
      kind: 'text';
      text: string;
      notices: string[];
      offer: VoiceOffer | null;
      sessionId: string;
      /**
       * The language the full pass heard, when the server said: `language`
       * as its name ("Hindi"), `languageCode` as its code ("hi"). Which
       * transcript goes into the draft depends on it (lib/voiceLive.ts
       * `chooseFinalText`: whisper writes a fifth of Hinglish in Urdu script).
       */
      language?: string;
      languageCode?: string;
    }
  | { kind: 'error'; error: VoiceError; offer: VoiceOffer | null }
  | { kind: 'withdrawn' };

/** The full pass's language as a text result carries it: only what the server said. */
function languageOf(state: SessionState): { language?: string; languageCode?: string } {
  return {
    ...(state.language ? { language: state.language } : {}),
    ...(state.languageCode ? { languageCode: state.languageCode } : {}),
  };
}

/** Whether a result leaves anything for the person to act on later. */
function offerKeepsRecord(offer: VoiceOffer | null): boolean {
  return offer !== null && (offer.kind === 'retranscribe' || offer.kind === 'upload_rest');
}

/**
 * Turn a settled session state into what the person sees. Every outcome the
 * contract names has its own sentence; an outcome this client does not know
 * delivers its text if there is any, and is `generic` if not.
 */
export function describeOutcome(
  state: SessionState,
  context: {
    lowSeen?: boolean;
    notices?: string[];
    peakLevel?: number | null;
    durationMs?: number;
    replaces?: string | null;
  } = {},
): SessionResult {
  const t = formatElapsed(state.audioMs || context.durationMs || 0);
  const notices = [...(context.notices ?? [])];
  const text = (state.text ?? '').trim();
  const lowSeen = context.lowSeen || state.segments.some((s) => s.low);
  const unheardGaps = state.gaps.filter((g) => g.reason !== 'dropped_as_noise');
  const retry = (message: string, audioMs: number): VoiceOffer => ({
    kind: 'retranscribe',
    sessionId: state.sessionId,
    scope: 'gaps',
    replaces: context.replaces ?? null,
    message,
    label: VOICE_MESSAGES.retry,
    audioMs,
  });
  const failure = (message: string, retryable: boolean, offer: VoiceOffer | null = null): SessionResult => ({
    kind: 'error',
    error: { message: [message, ...notices].join(' '), retryable },
    offer,
  });

  if (state.outcome === 'engine_unavailable') {
    const message = VOICE_MESSAGES.engineUnavailable(t);
    return {
      kind: 'error',
      error: { message, retryable: true },
      offer: retry(message, state.audioMs || context.durationMs || 0),
    };
  }
  if (state.outcome === 'undecodable') return failure(VOICE_MESSAGES.undecodable, false);
  if (state.status === 'failed') return failure(VOICE_MESSAGES.generic(t), true);
  if (state.outcome === 'no_speech') {
    const meterHeardNothing =
      typeof context.peakLevel === 'number' && context.peakLevel <= SILENT_METER_LEVEL;
    return failure(
      meterHeardNothing ? VOICE_MESSAGES.noSoundAtAll : VOICE_MESSAGES.noSpeech(t),
      false,
    );
  }
  if (state.outcome === 'no_words') return failure(VOICE_MESSAGES.noWords, false);
  if (!text) return failure(VOICE_MESSAGES.generic(t), true);

  if (lowSeen) notices.unshift(VOICE_MESSAGES.lowSegments);
  const unheard = unheardGaps;
  const offer =
    unheard.length > 0
      ? retry(
          VOICE_MESSAGES.gaps(unheard.length, describeGaps(unheard)),
          unheard.reduce((ms, g) => ms + Math.max(0, g.endMs - g.startMs), 0),
        )
      : null;
  if (offer && offer.kind === 'retranscribe') offer.replaces = text;
  return { kind: 'text', text, notices, offer, sessionId: state.sessionId, ...languageOf(state) };
}

// ---------------------------------------------------------------------------
// The outbox: every part not yet acknowledged, and nothing else
// ---------------------------------------------------------------------------

/** One recording's upload bookkeeping, persisted beside its slices. */
export interface OutboxRecord {
  sessionId: string;
  mimeType: string;
  partMs: number;
  partLimitBytes: number;
  idleCloseS: number;
  /** Epoch ms of the last sign of life from the tab that owns it. */
  aliveAt: number;
  /** The seq the next formed part will take. */
  nextSeq: number;
  /** The index the next slice will take. */
  nextSlice: number;
  /**
   * Every slice below this index is acknowledged. The record is written
   * BEFORE those slices are deleted, so a crash between the two leaves
   * acknowledged slices behind that adoption knows to skip. Without it they
   * would be sent again under a NEW part number and the server would append
   * the same audio twice.
   */
  firstUnacked: number;
  /**
   * The part that is formed and may already be on the server. Its slices are
   * fixed from the moment it is first sent: a replay must be byte-identical
   * or the server calls it a conflict.
   */
  formed: { seq: number; first: number; last: number } | null;
  /** The recorder has stopped; no more slices will come. */
  ended: boolean;
  endedBy: EndedBy | null;
  /** Recorder clock at the last slice. */
  durationMs: number;
  /** Recorder clock at the end of the last acknowledged slice. */
  ackedMs: number;
  /**
   * The server accepted the finish. Nothing is left to upload; the record
   * stays until the transcript is in the composer, because the finish wait can
   * be minutes (a backlog, chat pacing, an engine outage) and that is exactly
   * when people reload. Before 2026-09-29 it was dropped here and a reload
   * found nothing to offer back.
   */
  finished?: boolean;
  /**
   * The server would not take the rest (the session was closed: idle, another
   * tab, storage full). The slices it never stored stay here, and the person is
   * offered "Upload the rest", instead of this device deleting them.
   */
  held?: { reason: SessionInterrupt['kind']; endedBy: string | null } | null;
  /** Text from this recording already put into the draft, which a later full transcript replaces. */
  deliveredText?: string | null;
  /** The Retry the person has not acted on yet (transcript with gaps, engine unavailable). */
  offer?: { scope: 'gaps' | 'all'; replaces: string | null; message: string; audioMs?: number } | null;
  /**
   * CONTINUATION (2026-09-29). The container's init segment, from the first
   * slice, so the rest of a recording the server closed (idle, page closed)
   * can go into a new session that starts as a valid stream.
   */
  init?: ArrayBuffer | null;
  /** Every server session this recording has used, in order; the first is `sessionId`. */
  chain?: string[];
  /** The session parts go to now, when it is a continuation: its first slice and the front of its part 0. */
  target?: { sessionId: string; firstSlice: number; prefix: ArrayBuffer | null } | null;
  /** A continuation being opened: its client_key, reused on every retry so only one is made. */
  opening?: { clientKey: string; firstSlice: number; linked?: boolean } | null;
}

export interface StoredSlice {
  idx: number;
  endMs: number;
  bytes: Uint8Array;
  /**
   * What a new stream starting at this slice needs after the init segment
   * (WebmCutTracker.lead), so the rest of a closed recording can continue in
   * a new session; null where it is not known.
   */
  lead?: Uint8Array | null;
}

/**
 * Where unacknowledged slices wait. `persistent` stores survive a reload;
 * the memory store does not, and says so, because the sign-out message has
 * to tell the person which of the two they have.
 */
export interface OutboxStore {
  readonly persistent: boolean;
  saveRecord(record: OutboxRecord): Promise<void>;
  loadRecord(sessionId: string): Promise<OutboxRecord | null>;
  listRecords(): Promise<OutboxRecord[]>;
  /** Take a stale record for this tab, atomically; null if it is live or gone. */
  claim(sessionId: string, staleBefore: number, now: number): Promise<OutboxRecord | null>;
  /** Mark a record as held by a live tab, so no other tab adopts it meanwhile. */
  touch(sessionId: string, now: number): Promise<void>;
  putSlice(sessionId: string, idx: number, endMs: number, data: Blob, lead?: Uint8Array | null): Promise<void>;
  readSlices(sessionId: string, first: number, last: number): Promise<StoredSlice[]>;
  dropSlices(sessionId: string, first: number, last: number): Promise<void>;
  drop(sessionId: string): Promise<void>;
  /** Bytes of audio this store holds in the tab's memory. */
  heldBytes(): number;
}

/** The outbox without IndexedDB: memory, and only for what is unacknowledged. */
export function createMemoryOutbox(): OutboxStore {
  const records = new Map<string, OutboxRecord>();
  const slices = new Map<string, Map<number, { endMs: number; data: Blob; lead: Uint8Array | null }>>();
  let held = 0;
  return {
    persistent: false,
    async saveRecord(record) {
      records.set(record.sessionId, { ...record });
    },
    async loadRecord(id) {
      const r = records.get(id);
      return r ? { ...r } : null;
    },
    async listRecords() {
      return [...records.values()].map((r) => ({ ...r }));
    },
    async claim() {
      // Memory dies with the tab, so nothing here is ever another tab's.
      return null;
    },
    async touch(id, now) {
      const r = records.get(id);
      if (r) records.set(id, { ...r, aliveAt: now });
    },
    async putSlice(id, idx, endMs, data, lead = null) {
      let m = slices.get(id);
      if (!m) slices.set(id, (m = new Map()));
      const prev = m.get(idx);
      if (prev) held -= prev.data.size;
      m.set(idx, { endMs, data, lead });
      held += data.size;
    },
    async readSlices(id, first, last) {
      const m = slices.get(id);
      const out: StoredSlice[] = [];
      for (let i = first; i <= last; i += 1) {
        const s = m?.get(i);
        if (s) out.push({ idx: i, endMs: s.endMs, bytes: await blobBytes(s.data), lead: s.lead });
      }
      return out;
    },
    async dropSlices(id, first, last) {
      const m = slices.get(id);
      if (!m) return;
      for (let i = first; i <= last; i += 1) {
        const s = m.get(i);
        if (s) {
          held -= s.data.size;
          m.delete(i);
        }
      }
    },
    async drop(id) {
      records.delete(id);
      const m = slices.get(id);
      if (m) for (const s of m.values()) held -= s.data.size;
      slices.delete(id);
    },
    heldBytes: () => held,
  };
}

/**
 * ONE DATABASE PER ACCOUNT (2026-09-29): `techsara-voice-outbox:u<id>`, named
 * with lib/auth's `userScopeKey` exactly as the history cache is
 * (lib/idbCache.ts `userDbName`). Until then every account on a browser shared
 * one origin-wide database, and on a shared computer the next person's
 * composer adopted the previous person's unsent audio, got the server's 404
 * for someone else's recording, and deleted it
 * (tests/voice-session-edges.test.ts). Separate names mean another account's
 * recordings are never opened at all; the wipe on logout and on an account
 * switch (below) means they do not stay on the machine for devtools either.
 *
 * The bare name is the pre-2026-09-29 shared database. It never shipped; it is
 * still deleted with the rest.
 */
export const OUTBOX_DB = 'techsara-voice-outbox';
export function outboxDbName(owner: string): string {
  return `${OUTBOX_DB}:${owner}`;
}
/** localStorage: which accounts have an outbox database on this browser. */
const OUTBOX_OWNERS_KEY = 'techsara-voice-outbox-owners';
const RECORD_STORE = 'records';
const SLICE_STORE = 'slices';

type StorageLike = Pick<Storage, 'getItem' | 'setItem' | 'removeItem'> & {
  readonly length?: number;
  key?: (index: number) => string | null;
};

function browserStorage(): StorageLike | null {
  try {
    return typeof window !== 'undefined' && window.localStorage ? window.localStorage : null;
  } catch {
    return null; // a sandboxed frame throws on the property itself
  }
}

function readJson<T>(storage: StorageLike | null, key: string, fallback: T): T {
  if (!storage) return fallback;
  try {
    const raw = storage.getItem(key);
    return raw ? (JSON.parse(raw) as T) : fallback;
  } catch {
    return fallback;
  }
}

function writeJson(storage: StorageLike | null, key: string, value: unknown): void {
  if (!storage) return;
  try {
    storage.setItem(key, JSON.stringify(value));
  } catch {
    /* quota or a private window: the index is a convenience, the wipe also asks indexedDB.databases() */
  }
}

/** Accounts with an outbox database on this browser, from the index. */
export function outboxOwners(storage: StorageLike | null = browserStorage()): string[] {
  const list = readJson<unknown>(storage, OUTBOX_OWNERS_KEY, []);
  return Array.isArray(list) ? list.filter((o): o is string => typeof o === 'string' && o !== '') : [];
}

function deleteDb(factory: IDBFactory, name: string): Promise<void> {
  return new Promise((resolve) => {
    try {
      const req = factory.deleteDatabase(name);
      req.onsuccess = () => resolve();
      req.onerror = () => resolve();
      // Blocked means a connection elsewhere has not closed yet. Every
      // connection this module opens closes itself on `versionchange`, so the
      // deletion completes as soon as they do; nothing to wait for here.
      req.onblocked = () => resolve();
    } catch {
      resolve();
    }
  });
}

/** Every outbox database name on this browser: the index, plus what the browser lists. */
async function outboxDbNames(factory: IDBFactory, storage: StorageLike | null): Promise<string[]> {
  const names = new Set(outboxOwners(storage).map(outboxDbName));
  names.add(OUTBOX_DB);
  const f = factory as IDBFactory & { databases?: () => Promise<Array<{ name?: string }>> };
  if (typeof f.databases === 'function') {
    try {
      for (const db of await f.databases()) {
        if (db.name === OUTBOX_DB || db.name?.startsWith(`${OUTBOX_DB}:`)) names.add(db.name);
      }
    } catch {
      /* not every engine lists databases; the index covers them */
    }
  }
  return [...names];
}

/**
 * An account switch on this browser: delete every OTHER account's outbox,
 * including audio that account never uploaded. That is deliberate (security
 * review 2026-09-29, item 10): raw speech of person A must not stay readable
 * on a shared profile once person B signs in. Person A was told so when the
 * sign-out happened (VOICE_MESSAGES.signedOutKept), and is asked before an
 * explicit logout (voiceLogoutCheck).
 */
export async function wipeOtherOutboxes(
  owner: string,
  factory: IDBFactory | undefined = typeof indexedDB !== 'undefined' ? indexedDB : undefined,
  storage: StorageLike | null = browserStorage(),
): Promise<string[]> {
  if (!factory) return [];
  const keep = outboxDbName(owner);
  const wiped: string[] = [];
  for (const name of await outboxDbNames(factory, storage)) {
    if (name === keep) continue;
    await deleteDb(factory, name);
    wiped.push(name);
  }
  writeJson(storage, OUTBOX_OWNERS_KEY, outboxOwners(storage).filter((o) => o === owner));
  return wiped;
}

/** Logout, or an account whose access ended: every outbox on this browser goes. */
export async function wipeVoiceOutboxes(
  factory: IDBFactory | undefined = typeof indexedDB !== 'undefined' ? indexedDB : undefined,
  storage: StorageLike | null = browserStorage(),
): Promise<void> {
  if (factory) for (const name of await outboxDbNames(factory, storage)) await deleteDb(factory, name);
  try {
    storage?.removeItem(OUTBOX_OWNERS_KEY);
  } catch {
    /* best-effort */
  }
}

/**
 * The outbox database of one account. Registers the account in the index
 * first, so a wipe can find the database even where the browser cannot list
 * them.
 */
export async function openOwnerOutbox(
  owner: string,
  factory: IDBFactory | undefined = typeof indexedDB !== 'undefined' ? indexedDB : undefined,
  storage: StorageLike | null = browserStorage(),
): Promise<OutboxStore> {
  const owners = outboxOwners(storage);
  if (!owners.includes(owner)) writeJson(storage, OUTBOX_OWNERS_KEY, [...owners, owner]);
  return openOutbox(factory, outboxDbName(owner));
}

// ---------------------------------------------------------------------------
// Tombstones: discards the server has not confirmed yet
// ---------------------------------------------------------------------------

/**
 * A discard the server has not confirmed. Pressing X while the server could
 * not be reached used to try DELETE four times over about 15 s and then drop
 * everything on this device anyway, so the recording the person threw away
 * stayed on the server, was transcribed, and was kept for good
 * (tests/voice-session-edges.test.ts: 4 DELETEs, server still recording with
 * 1,930,488 bytes, 0 outbox records left).
 *
 * The tombstone holds no audio, only the session id, and lives in
 * localStorage under the account that recorded it. So it survives a reload, a
 * logout and another person's session on the same browser, and is sent again
 * the next time that account is here, until the server answers 204 or 404.
 */
export interface Tombstone {
  sessionId: string;
  at: number;
}

export interface TombstoneStore {
  readonly durable: boolean;
  list(): Tombstone[];
  add(sessionId: string, now: number): void;
  remove(sessionId: string): void;
  has(sessionId: string): boolean;
}

const TOMBSTONE_PREFIX = 'techsara-voice-discards:';
const volatileTombstones = new Map<string, Map<string, Tombstone>>();

/**
 * The tombstones of one account. `owner` null (the account could not be
 * identified) keeps them in this tab's memory only, and `durable` says so, so
 * the person is told to keep the tab open rather than promised a retry after
 * a reload that would not happen.
 */
export function tombstonesFor(
  owner: string | null,
  storage: StorageLike | null = browserStorage(),
): TombstoneStore {
  const key = owner ? `${TOMBSTONE_PREFIX}${owner}` : null;
  const durable = Boolean(key && storage);
  const memKey = owner ?? '';
  const mem = volatileTombstones.get(memKey) ?? new Map<string, Tombstone>();
  volatileTombstones.set(memKey, mem);
  const read = (): Tombstone[] => {
    if (!durable) return [...mem.values()];
    const list = readJson<unknown>(storage, key!, []);
    return Array.isArray(list)
      ? list.filter(
          (t): t is Tombstone =>
            typeof t === 'object' && t !== null && isSessionId((t as Tombstone).sessionId),
        )
      : [];
  };
  const write = (list: Tombstone[]) => {
    if (!durable) {
      mem.clear();
      for (const t of list) mem.set(t.sessionId, t);
      return;
    }
    if (list.length === 0) {
      try {
        storage!.removeItem(key!);
      } catch {
        /* best-effort */
      }
      return;
    }
    writeJson(storage, key!, list);
  };
  return {
    durable,
    list: read,
    add(sessionId, now) {
      const list = read().filter((t) => t.sessionId !== sessionId);
      write([...list, { sessionId, at: now }]);
    },
    remove(sessionId) {
      write(read().filter((t) => t.sessionId !== sessionId));
    },
    has: (sessionId) => read().some((t) => t.sessionId === sessionId),
  };
}

/** Whether any account has a tombstone on this browser (so the page must look up who it is). */
export function anyTombstones(storage: StorageLike | null = browserStorage()): boolean {
  if (!storage || typeof storage.key !== 'function' || typeof storage.length !== 'number') return false;
  try {
    for (let i = 0; i < storage.length; i += 1) {
      if (storage.key(i)?.startsWith(TOMBSTONE_PREFIX)) return true;
    }
  } catch {
    /* unreadable storage has no tombstones we could act on */
  }
  return false;
}

/**
 * Send every pending discard of this account again. A tombstone goes only on
 * the server's 204 or 404; anything else keeps it for the next try.
 */
export async function flushTombstones(
  tombstones: TombstoneStore,
  deps: SessionDeps = {},
): Promise<{ deleted: string[]; pending: string[] }> {
  const deleted: string[] = [];
  const pending: string[] = [];
  for (const t of tombstones.list()) {
    if ((await deleteSessionOnce(t.sessionId, deps)) === 'gone') {
      tombstones.remove(t.sessionId);
      deleted.push(t.sessionId);
    } else {
      pending.push(t.sessionId);
    }
  }
  return { deleted, pending };
}

/** Bytes as an ArrayBuffer of exactly their length: what every IndexedDB engine clones the same way. */
function toBuffer(bytes: Uint8Array | null | undefined): ArrayBuffer | null {
  if (!bytes) return null;
  return bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength) as ArrayBuffer;
}

function idbRequest<T>(req: IDBRequest<T>): Promise<T> {
  return new Promise((resolve, reject) => {
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

function idbDone(tx: IDBTransaction): Promise<void> {
  return new Promise((resolve, reject) => {
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error);
    tx.onabort = () => reject(tx.error ?? new Error('transaction aborted'));
  });
}

/**
 * The outbox in IndexedDB, so a reload, a crash or a sign-out mid-recording
 * loses nothing that was not yet acknowledged.
 *
 * Slices are stored as ArrayBuffers, not Blobs: Safari's IndexedDB has had
 * Blob bugs, and a buffer is the one thing every engine clones the same way.
 * The tab keeps no copy once a slice is written, so an hour spent offline is
 * an hour on disk, not in a phone's memory.
 *
 * Fails soft like lib/idbCache.ts: when IndexedDB is missing or will not
 * open, the memory outbox is returned instead and `persistent` says so.
 */
export async function openOutbox(
  factory: IDBFactory | undefined = typeof indexedDB !== 'undefined' ? indexedDB : undefined,
  dbName: string = OUTBOX_DB,
): Promise<OutboxStore> {
  if (!factory) return createMemoryOutbox();
  let db: IDBDatabase;
  try {
    db = await new Promise<IDBDatabase>((resolve, reject) => {
      const req = factory.open(dbName, 1);
      req.onupgradeneeded = () => {
        const d = req.result;
        if (!d.objectStoreNames.contains(RECORD_STORE)) {
          d.createObjectStore(RECORD_STORE, { keyPath: 'sessionId' });
        }
        if (!d.objectStoreNames.contains(SLICE_STORE)) {
          d.createObjectStore(SLICE_STORE, { keyPath: ['sessionId', 'idx'] });
        }
      };
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error);
      req.onblocked = () => reject(new Error('indexeddb open blocked'));
    });
  } catch {
    return createMemoryOutbox();
  }
  // A wipe (logout, another account signing in) deletes this database from
  // another tab or from this one; an open connection would block it until the
  // tab closed. Close on request: later calls then fail, and every caller
  // already treats a failing outbox as the safety net it is.
  db.onversionchange = () => db.close();
  const range = (id: string, first: number, last: number) =>
    IDBKeyRange.bound([id, first], [id, last]);
  return {
    persistent: true,
    async touch(id, now) {
      const tx = db.transaction(RECORD_STORE, 'readwrite');
      const store = tx.objectStore(RECORD_STORE);
      const r = await idbRequest(store.get(id) as IDBRequest<OutboxRecord | undefined>);
      if (r) store.put({ ...r, aliveAt: now });
      await idbDone(tx);
    },
    async saveRecord(record) {
      const tx = db.transaction(RECORD_STORE, 'readwrite');
      tx.objectStore(RECORD_STORE).put(record);
      await idbDone(tx);
    },
    async loadRecord(id) {
      const tx = db.transaction(RECORD_STORE, 'readonly');
      const r = await idbRequest(tx.objectStore(RECORD_STORE).get(id) as IDBRequest<OutboxRecord | undefined>);
      return r ?? null;
    },
    async listRecords() {
      const tx = db.transaction(RECORD_STORE, 'readonly');
      return idbRequest(tx.objectStore(RECORD_STORE).getAll() as IDBRequest<OutboxRecord[]>);
    },
    async claim(id, staleBefore, now) {
      // One readwrite transaction: IndexedDB serialises them across tabs, so
      // two tabs that mount together cannot both adopt the same recording.
      const tx = db.transaction(RECORD_STORE, 'readwrite');
      const store = tx.objectStore(RECORD_STORE);
      const r = await idbRequest(store.get(id) as IDBRequest<OutboxRecord | undefined>);
      let taken: OutboxRecord | null = null;
      if (r && r.aliveAt < staleBefore) {
        taken = { ...r, aliveAt: now };
        store.put(taken);
      }
      await idbDone(tx);
      return taken;
    },
    async putSlice(id, idx, endMs, data, lead = null) {
      const bytes = await blobBytes(data);
      const buffer = bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength);
      const tx = db.transaction(SLICE_STORE, 'readwrite');
      tx.objectStore(SLICE_STORE).put({ sessionId: id, idx, endMs, bytes: buffer, lead: toBuffer(lead) });
      await idbDone(tx);
    },
    async readSlices(id, first, last) {
      const tx = db.transaction(SLICE_STORE, 'readonly');
      const rows = await idbRequest(
        tx.objectStore(SLICE_STORE).getAll(range(id, first, last)) as IDBRequest<
          Array<{ idx: number; endMs: number; bytes: ArrayBuffer; lead?: ArrayBuffer | null }>
        >,
      );
      return rows.map((r) => ({
        idx: r.idx,
        endMs: r.endMs,
        bytes: new Uint8Array(r.bytes),
        lead: r.lead ? new Uint8Array(r.lead) : null,
      }));
    },
    async dropSlices(id, first, last) {
      const tx = db.transaction(SLICE_STORE, 'readwrite');
      tx.objectStore(SLICE_STORE).delete(range(id, first, last));
      await idbDone(tx);
    },
    async drop(id) {
      const tx = db.transaction([RECORD_STORE, SLICE_STORE], 'readwrite');
      tx.objectStore(RECORD_STORE).delete(id);
      tx.objectStore(SLICE_STORE).delete(range(id, 0, Number.MAX_SAFE_INTEGER));
      await idbDone(tx);
    },
    heldBytes: () => 0,
  };
}

// ---------------------------------------------------------------------------
// The session itself
// ---------------------------------------------------------------------------

/** What the bar draws while a session records and finishes. */
export interface SessionProgress {
  /** The tail of the final segments, at most PREVIEW_CHARS. */
  preview: string;
  /** Held-back text that may still change; replaced on every answer. */
  tentative: string;
  audioMs: number;
  /**
   * The server's `transcribed_ms`: audio before this point is in `preview`
   * and `tentative` (or is silence). The live transcript shows only what it
   * heard after it (lib/voiceLive.ts `mergeLiveTranscript`). Absent on
   * progress built by hand, where `audioMs - backlogMs` is the same number.
   */
  transcribedMs?: number;
  /**
   * The live transcript's words after what `preview` and `tentative` cover:
   * committed utterances, then the one still being heard. Set by the
   * recorder hook (lib/voiceLive.ts `withLiveWords`), never by VoiceSession;
   * absent when the recording has no live stream.
   */
  live?: { committed: string; partial: string } | null;
  backlogMs: number;
  waitingOn: WaitingOn;
  /** Slices recorded but not yet acknowledged by the server. */
  pendingParts: number;
  /** Recording time not yet acknowledged. */
  pendingMs: number;
  /** Recording time the server has acknowledged (stored); what "Saved to your account" may claim. */
  savedMs?: number;
  offline: boolean;
  /**
   * Offline for longer than the server waits (idle_close_s): the server has
   * probably closed this recording, and "it will upload when the connection is
   * back" is no longer a promise the bar can make.
   */
  offlineLong?: boolean;
  /** The server's idle close, for the sentence above. */
  idleCloseS?: number;
  /** Storage refused for over a minute; the banner, not an error. */
  storageTrouble: boolean;
  lastAckAt: number | null;
  retentionDays: number | null;
  progressive: boolean;
}

/** Why a session had to stop uploading while the recorder was still running. */
export type SessionInterrupt =
  | { kind: 'signed_out' }
  | { kind: 'voice_off'; detail: string | null }
  | { kind: 'not_found' }
  | { kind: 'part_conflict' }
  | { kind: 'session_closed'; endedBy: string | null }
  | { kind: 'storage_full' }
  | { kind: 'quota_exceeded' }
  | { kind: 'unsupported_format'; detail: string | null }
  | { kind: 'lost_parts' }
  | { kind: 'generic' };

/**
 * Interrupts after which the audio this device still has is KEPT, and offered
 * as "Upload the rest". The server stopped taking parts, but nothing says the
 * audio is unwanted or undecodable: the session was closed by its idle timer
 * or from another tab, the disk was full, the feature was switched off, or the
 * server refused in a way this client does not know. Until 2026-09-29 every one
 * of them deleted the outbox: 11 minutes offline kept 24 of 156 slices, and the
 * 132 recorded offline were deleted from IndexedDB on reconnect.
 *
 * The others do delete: `not_found` (the account's own recording is gone from
 * the server — the outbox is per account, so this is never someone else's),
 * `part_conflict` (another tab is uploading the same audio), and
 * `unsupported_format` (the server cannot decode the container).
 */
const HOLDS_AUDIO: ReadonlySet<SessionInterrupt['kind']> = new Set([
  'session_closed',
  'storage_full',
  'quota_exceeded',
  'voice_off',
  'generic',
]);

function keepsSlices(interrupt: SessionInterrupt | null): boolean {
  return interrupt === null || interrupt.kind === 'signed_out' || HOLDS_AUDIO.has(interrupt.kind);
}

interface QueuedSlice {
  idx: number;
  size: number;
  endMs: number;
  /** Kept only when the store could not take it; null once it is stored. */
  data: Blob | null;
  /** The front a continuation starting here needs (see WebmCutTracker); undefined when not read yet. */
  lead?: Uint8Array | null;
}

export interface SessionStats {
  /** Bytes of audio this tab holds right now, in the outbox and in flight. */
  heldBytes: number;
  peakHeldBytes: number;
  slicesAdded: number;
  bytesAdded: number;
  partsAcked: number;
  bytesAcked: number;
  putRequests: number;
}

/**
 * One recording on the session road: uploads its slices in order while the
 * recorder runs, then finishes and waits for the transcript.
 *
 * ONE PART IN FLIGHT, STRICTLY IN ORDER. Part k+1 is sent only after part k is
 * acknowledged, so the server never sees a hole and a replay can always be
 * recognised by (seq, SHA-256). A slice leaves the outbox only on a 200.
 *
 * RETRY FOREVER, NOT FOREVER THE SAME. A dropped network, a proxy restart,
 * a 408/422/429 or a 5xx sends the same bytes again on the backoff schedule,
 * for as long as the outbox is not empty; the recorder keeps recording
 * meanwhile. When the uploader has fallen behind it sends several slices as
 * one part, up to the server's part limit, so coming back after an outage
 * costs a few requests rather than hundreds against a 120-a-minute limit.
 * Every request has a deadline (`partTimeoutMs`, `REQUEST_TIMEOUT_MS`), and
 * the browser's `online` event restarts the one in flight.
 *
 * NEVER THE WHOLE RECORDING. The tab holds only what the server has not yet
 * acknowledged — normally one 5 s slice of about 80 KB — and with IndexedDB
 * not even that. The old recorder held every chunk of the recording in one
 * array until Stop and then posted it as one body: 9,652,200 bytes for ten
 * minutes of 128.7 kb/s slices, measured 2026-09-29 by driving the origin/dev
 * hook in a test harness. tests/voice-session-recorder.test.tsx holds an hour
 * of the same slices to at most two held at once (160,874 bytes).
 *
 * NEVER DELETE WHAT THE SERVER HAS NOT STORED (2026-09-29), except when the
 * person discards the recording. The outbox record also outlives the finish:
 * it goes only when the transcript is in the composer (`settleRecord`), so a
 * reload during a long finish can still offer the words back.
 */
export class VoiceSession {
  readonly sessionId: string;
  readonly mimeType: string;
  config: SessionConfig;

  private readonly fetchImpl: typeof fetch;
  private readonly store: OutboxStore;
  private readonly tombstones: TombstoneStore;
  private readonly now: () => number;
  private readonly random: () => number;
  private readonly sha: (bytes: Uint8Array) => Promise<string>;
  private readonly onProgress: (p: SessionProgress) => void;
  private readonly onInterrupt: (i: SessionInterrupt) => void;

  private readonly abort = new AbortController();
  private readonly wake = { current: null as (() => void) | null };
  /** The request in flight, which `nudge` restarts. */
  private readonly slot: InflightSlot = { current: null };
  private queue: QueuedSlice[] = [];
  /** Slices handed to the store and not yet written. */
  private readonly writing = new Set<QueuedSlice>();
  private formed: { seq: number; first: number; last: number } | null = null;
  /** The slices of the formed part, in order; their bytes are in the store. */
  private formedSlices: QueuedSlice[] | null = null;
  private formedBody: { bytes: Uint8Array; sha: string; endMs: number } | null = null;
  /** Consecutive deadlines the formed part has missed; each doubles the next one. */
  private formedTimeouts = 0;
  private splitLimit: number | null = null;
  private nextSeq = 0;
  private nextSlice = 0;
  private firstUnacked = 0;
  private durationMs = 0;
  private ackedMs = 0;
  private ended = false;
  private endedBy: EndedBy | null = null;
  private discarded = false;
  private interrupt: SessionInterrupt | null = null;
  private writeChain: Promise<void> = Promise.resolve();
  private pumping: Promise<void> | null = null;
  private lastTouch = 0;
  /** The outbox record is gone (discarded, or nothing left to do); never write it again. */
  private outboxDropped = false;
  private keepAlive: ReturnType<typeof setInterval> | null = null;
  /** The server accepted the finish. */
  private finishSent = false;
  /** The pagehide finish went out. */
  private beaconSent = false;
  private heldState: OutboxRecord['held'] = null;
  /** A Retry the record carries (settleRecord wrote it); kept through this session's own writes. */
  private offerState: OutboxRecord['offer'] = null;
  /** Set on adoption: the record was already held once, so a second refusal says "still". */
  private heldBefore = false;
  private deliveredText: string | null = null;
  private offlineSince: number | null = null;
  /** Where the recorder's container can be cut to start a new stream (WebM only). */
  private readonly cuts: ContainerCuts | null;
  /** The container's init segment, from the first slice. */
  private init: Uint8Array | null = null;
  /**
   * CONTINUATION (2026-09-29). Every server session this recording has used,
   * in order. It grows when the server closes the session while this device
   * still has audio for it (its idle close after 600 s without a part, or the
   * pagehide finish of a tab that was closed): the rest goes into a NEW
   * session opened with `continues`, and the transcripts are joined in order.
   * Until then the 132 slices recorded during an 11-minute outage were
   * deleted from this device on reconnect.
   */
  private chain: string[];
  /** Where parts go now; for a continuation, its first slice and part 0's front. */
  private target: { sessionId: string; firstSlice: number; prefix: Uint8Array | null };
  /**
   * A continuation being opened; its client_key makes a retried create find
   * the same one. `linked`: it `continues` an idle-closed session and its
   * parts are the held slices byte for byte (the server decodes them after
   * the bytes they continue). Otherwise it is a new recording of its own,
   * whose part 0 must open as a stream (init segment + lead).
   */
  private continuing: { clientKey: string; firstSlice: number; linked: boolean } | null = null;
  /** Who closed the session that is being continued (for the message if it cannot be). */
  private closedBy: string | null = null;
  /** "Upload the rest": a closed session is continued whoever closed it. */
  private continueAnyClose = false;

  private cursor = 0;
  private rev = -1;
  private lowSeen = false;
  private lastState: SessionState | null = null;
  private progress: SessionProgress;
  private storageTroubleSince: number | null = null;

  private stat: SessionStats = {
    heldBytes: 0,
    peakHeldBytes: 0,
    slicesAdded: 0,
    bytesAdded: 0,
    partsAcked: 0,
    bytesAcked: 0,
    putRequests: 0,
  };

  constructor(
    init: {
      sessionId: string;
      mimeType: string;
      config: SessionConfig;
      state?: SessionState | null;
    },
    deps: SessionDeps & {
      store: OutboxStore;
      tombstones?: TombstoneStore;
      /** The container tracker for a mime type; tests supply their own. */
      cuts?: (mimeType: string) => ContainerCuts | null;
    },
    handlers: {
      onProgress?: (p: SessionProgress) => void;
      onInterrupt?: (i: SessionInterrupt) => void;
    } = {},
  ) {
    this.sessionId = init.sessionId;
    this.mimeType = init.mimeType;
    this.config = init.config;
    this.cuts = (deps.cuts ?? cutsFor)(init.mimeType);
    this.chain = [init.sessionId];
    this.target = { sessionId: init.sessionId, firstSlice: 0, prefix: null };
    // Bound now, not looked up per call: a session finishing in the
    // background after its component unmounted keeps talking to the fetch it
    // started with. `bind` also keeps browsers from calling fetch with this
    // object as its receiver, which they refuse as an illegal invocation.
    this.fetchImpl = deps.fetchImpl ?? fetch.bind(globalThis);
    this.store = deps.store;
    this.tombstones = deps.tombstones ?? tombstonesFor(null);
    this.now = deps.now ?? (() => Date.now());
    this.random = deps.random ?? Math.random;
    this.sha = deps.sha256 ?? sha256Hex;
    this.onProgress = handlers.onProgress ?? (() => undefined);
    this.onInterrupt = handlers.onInterrupt ?? (() => undefined);
    this.progress = {
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
      idleCloseS: this.config.idleCloseS,
      storageTrouble: false,
      lastAckAt: null,
      retentionDays: null,
      progressive: true,
    };
    if (init.state) this.apply(init.state);
  }

  /** Write the empty outbox record, so a reload from the first second is recoverable. */
  async open(): Promise<void> {
    await this.persist(true);
    this.startKeepAlive();
  }

  /** Whether the outbox survives a reload (IndexedDB), which decides what the person is promised. */
  get persistent(): boolean {
    return this.store.persistent;
  }

  /**
   * Touch the outbox record every 10 s while this tab owns it. Another tab
   * adopts a record untouched for OUTBOX_STALE_MS (30 s), and a session that
   * is waiting out a 30 s retry after Stop writes nothing else for that long:
   * without this, a second tab opened during an outage could adopt a
   * recording this tab is still uploading.
   */
  private startKeepAlive(): void {
    if (this.keepAlive !== null || this.outboxDropped) return;
    this.keepAlive = setInterval(() => void this.persist(true), 10_000);
  }

  private stopKeepAlive(): void {
    if (this.keepAlive !== null) {
      clearInterval(this.keepAlive);
      this.keepAlive = null;
    }
  }

  /** This device holds nothing of the recording any more. */
  private async dropOutbox(): Promise<void> {
    this.outboxDropped = true;
    this.stopKeepAlive();
    await this.store.drop(this.sessionId).catch(() => undefined);
  }

  /**
   * Adopt a recording another tab left behind (a reload, a crash, a sign-out):
   * the slices still in the outbox are sent, then the session is finished.
   * A record whose finish was already accepted only waits for the transcript;
   * a held one tries its refused slices again ("Upload the rest").
   */
  static async adopt(
    record: OutboxRecord,
    deps: SessionDeps & {
      store: OutboxStore;
      tombstones?: TombstoneStore;
      cuts?: (mimeType: string) => ContainerCuts | null;
    },
    handlers: { onProgress?: (p: SessionProgress) => void } = {},
    opts: { continueAnyClose?: boolean } = {},
  ): Promise<VoiceSession> {
    const session = new VoiceSession(
      {
        sessionId: record.sessionId,
        mimeType: record.mimeType,
        config: {
          ...DEFAULT_SESSION_CONFIG,
          partMs: record.partMs,
          partLimitBytes: record.partLimitBytes,
          idleCloseS: record.idleCloseS,
        },
      },
      deps,
      handlers,
    );
    session.nextSeq = record.nextSeq;
    session.nextSlice = record.nextSlice;
    session.firstUnacked = record.firstUnacked ?? 0;
    session.durationMs = record.durationMs;
    session.ackedMs = record.ackedMs;
    session.endedBy = record.endedBy;
    session.finishSent = record.finished === true;
    session.heldBefore = Boolean(record.held);
    session.heldState = record.held ?? null;
    session.offerState = record.offer ?? null;
    session.deliveredText = record.deliveredText ?? null;
    session.init = record.init ? new Uint8Array(record.init) : null;
    session.chain = record.chain && record.chain.length > 0 ? [...record.chain] : [record.sessionId];
    if (record.target) {
      session.target = {
        sessionId: record.target.sessionId,
        firstSlice: record.target.firstSlice,
        prefix: record.target.prefix ? new Uint8Array(record.target.prefix) : null,
      };
    }
    session.continuing = record.opening
      ? { ...record.opening, linked: record.opening.linked !== false }
      : null;
    session.continueAnyClose = opts.continueAnyClose === true;
    session.lastTouch = 0;
    // What still has to go is every stored slice from the first one not yet
    // acknowledged. Their bytes stay on disk; only the sizes are kept here.
    const from = session.firstUnacked;
    const stored =
      !session.finishSent && record.nextSlice > from
        ? await deps.store.readSlices(record.sessionId, from, record.nextSlice - 1)
        : [];
    if (from > 0) {
      // Acknowledged slices a crash left behind: never sent again.
      await deps.store.dropSlices(record.sessionId, 0, from - 1).catch(() => undefined);
    }
    session.startKeepAlive();
    const meta = stored
      .filter((s) => s.idx >= from)
      .map((s) => ({ idx: s.idx, size: s.bytes.byteLength, endMs: s.endMs, data: null }));
    if (record.formed && !session.finishSent) {
      const f = record.formed;
      session.formed = f;
      session.formedSlices = meta.filter((s) => s.idx >= f.first && s.idx <= f.last);
      session.queue = meta.filter((s) => s.idx > f.last);
    } else {
      session.queue = meta;
    }
    return session;
  }

  /** A timeslice from the recorder. Never blocks; the write and the upload follow. */
  addSlice(data: Blob, endMs: number): void {
    if (this.ended || this.discarded) return;
    // After a sign-out, and after the server closed the session, the last
    // seconds are still KEPT: for when the person signs in again, or for
    // "Upload the rest". The interrupts that mean the server can never take
    // them (another tab has the same audio, an undecodable format, a
    // recording that is gone) take nothing more.
    if (!keepsSlices(this.interrupt)) return;
    if (!data || data.size === 0) return;
    const slice: QueuedSlice = { idx: this.nextSlice, size: data.size, endMs, data };
    this.nextSlice += 1;
    this.durationMs = Math.max(this.durationMs, endMs);
    this.stat.slicesAdded += 1;
    this.stat.bytesAdded += data.size;
    this.writing.add(slice);
    this.notePeak();
    this.writeChain = this.writeChain.then(async () => {
      if (this.discarded || this.outboxDropped) {
        this.writing.delete(slice);
        return;
      }
      // Where this slice cuts the container, for a continuation that might
      // have to start here (read before the slice is fed: the tracker's state
      // is then exactly the cut between the previous slice and this one).
      let lead: Uint8Array | null = null;
      if (this.cuts) {
        try {
          const bytes = await blobBytes(data);
          lead = slice.idx === 0 ? null : this.cuts.lead();
          this.cuts.feed(bytes);
          if (!this.init && this.cuts.init) this.init = this.cuts.init;
        } catch {
          /* a slice this tab cannot read still uploads; it just cannot start a continuation */
        }
      }
      slice.lead = lead;
      try {
        await this.store.putSlice(this.sessionId, slice.idx, slice.endMs, data, lead);
        // Stored: the store holds it now (on disk for IndexedDB), not us.
        slice.data = null;
      } catch {
        // IndexedDB refused (quota, a private window turning hostile). Keep it
        // in memory; it still uploads, it just will not survive a reload.
      }
      this.writing.delete(slice);
      this.queue.push(slice);
      this.notePeak();
      await this.persist();
      this.emit();
      this.kick();
    });
  }

  /**
   * The browser just said it is back online: stop waiting, and abandon the
   * request in flight, which was started on the network that went away and
   * may never answer. It is sent again at once, byte for byte.
   */
  nudge(): void {
    this.slot.current?.();
    this.wake.current?.();
  }

  stats(): SessionStats {
    return { ...this.stat, heldBytes: this.held() };
  }

  /**
   * The page is going away (pagehide) with this recording unfinished. The
   * server would otherwise hold its slot, one of VOICE_SESSION_MAX_ACTIVE
   * fleet-wide plus the person's own single live recording, until its
   * 600 s idle close. `keepalive` lets the request outlive the page (and
   * sendBeacon where fetch cannot). The outbox is left exactly as it is: a
   * reopened tab uploads whatever this device still has.
   *
   * `last_part` is the last acknowledged part when nothing is waiting, and
   * null ("end at what you hold") when something is: those parts then go up
   * later as late parts, which the server accepts only with the change
   * described in the report for this fix (without it they are kept on this
   * device and offered as "Upload the rest").
   */
  beacon(): boolean {
    if (this.finishSent || this.discarded || this.beaconSent || this.outboxDropped) return false;
    // With parts still waiting on this device the session stays open: the
    // server continues only a session IT closed for silence, so a session
    // closed here would leave those parts with nowhere to go. A reopened tab
    // uploads them into it, and if none comes the server's idle close
    // (600 s) finishes it and a later tab continues it.
    const pending = this.queue.length > 0 || this.formed !== null || this.writing.size > 0;
    if (pending) return false;
    const lastPart = this.nextSeq === 0 ? null : this.nextSeq - 1;
    const body = JSON.stringify({
      last_part: lastPart,
      duration_ms: Math.round(this.durationMs),
      ended_by: 'page_hidden',
    });
    const url = `${SESSIONS_URL}/${this.target.sessionId}/finish`;
    let sent = false;
    try {
      void Promise.resolve(
        this.fetchImpl(url, {
          method: 'POST',
          headers: { 'content-type': 'application/json' },
          body,
          keepalive: true,
          credentials: 'same-origin',
        }),
      ).catch(() => undefined);
      sent = true;
    } catch {
      sent = false;
    }
    if (!sent && typeof navigator !== 'undefined' && typeof navigator.sendBeacon === 'function') {
      try {
        sent = navigator.sendBeacon(url, new Blob([body], { type: 'application/json' }));
      } catch {
        sent = false;
      }
    }
    this.beaconSent = sent;
    this.endedBy = this.endedBy ?? 'page_hidden';
    void this.persist(true);
    return sent;
  }

  /**
   * The recorder has stopped. Upload whatever is left, finish, and wait for
   * the transcript. Resolves with what the person should see.
   *
   * The outbox record is NOT dropped here when there are words to deliver or
   * something to act on: the caller calls `settleRecord` once the words are in
   * the composer.
   */
  async end(
    endedBy: EndedBy,
    durationMs: number,
    context: { notices?: string[]; peakLevel?: number | null } = {},
  ): Promise<SessionResult> {
    try {
      return await this.endInner(endedBy, durationMs, context);
    } finally {
      // Whatever the record still holds now is left for `settleRecord`, or
      // for adoption, which needs it to go stale first.
      this.stopKeepAlive();
    }
  }

  private async endInner(
    endedBy: EndedBy,
    durationMs: number,
    context: { notices?: string[]; peakLevel?: number | null },
  ): Promise<SessionResult> {
    this.ended = true;
    this.endedBy = this.endedBy ?? endedBy;
    this.durationMs = Math.max(this.durationMs, durationMs);
    const notices = [...(context.notices ?? [])];
    if (this.finishSent) {
      // Adopted after the finish was accepted: only the words are missing.
      return this.waitForResult(notices, context.peakLevel ?? null, null);
    }
    await this.writeChain;
    await this.persist(true);
    this.kick();
    while (this.pumping) await this.pumping;
    if (this.discarded) return { kind: 'withdrawn' };
    if (this.interrupt) return this.afterInterrupt(this.interrupt, notices, context.peakLevel);
    const lastPart = this.nextSeq - 1;
    if (lastPart < 0 && this.chain.length > 1) {
      // A continuation that never received a part: it goes, and the words
      // come from the sessions before it, which the server closed itself.
      const empty = this.chain.pop()!;
      this.tombstones.add(empty, this.now());
      if (await deleteSession(empty, { fetchImpl: this.fetchImpl, random: this.random })) {
        this.tombstones.remove(empty);
      }
      this.target = { sessionId: this.chain[this.chain.length - 1]!, firstSlice: 0, prefix: null };
      this.lastState = null;
      this.cursor = 0;
      this.rev = -1;
      this.finishSent = true;
      await this.persist(true);
      return this.waitForResult(notices, context.peakLevel ?? null, null);
    }
    if (lastPart < 0) {
      // The recorder produced nothing at all. There is no audio to keep, but
      // the empty session holds this person's one live-recording slot until
      // it idles out, so its delete is kept until the server confirms it.
      this.tombstones.add(this.sessionId, this.now());
      if (await deleteSession(this.sessionId, { fetchImpl: this.fetchImpl, random: this.random })) {
        this.tombstones.remove(this.sessionId);
      }
      await this.dropOutbox();
      return { kind: 'error', error: { message: VOICE_MESSAGES.tooShort, retryable: true }, offer: null };
    }
    return this.finishAndWait(lastPart, this.endedBy ?? endedBy, notices, context.peakLevel ?? null);
  }

  /**
   * The person's discard: stop everything and delete the recording on the
   * server. Resolves `deleted` only on the server's 204 or 404. Otherwise it
   * is `pending`: a tombstone keeps the DELETE owed (flushTombstones sends it
   * again on the next `online`, mic press and page load) and the caller must
   * not tell the person it is gone.
   */
  async discard(): Promise<'deleted' | 'pending'> {
    this.discarded = true;
    this.abort.abort();
    this.nudge();
    // The intent first, where a reload will find it; then this device's copy,
    // which the person no longer wants; then the server's.
    // Every server session of the recording goes: a continuation is the
    // same recording.
    const ids = [...this.chain];
    for (const id of ids) this.tombstones.add(id, this.now());
    await this.writeChain.catch(() => undefined);
    await this.dropOutbox();
    let all = true;
    for (const id of ids) {
      const gone = await deleteSession(id, {
        fetchImpl: this.fetchImpl,
        random: this.random,
        attempts: 2,
      });
      if (gone) this.tombstones.remove(id);
      else all = false;
    }
    return all ? 'deleted' : 'pending';
  }

  // -- internals ------------------------------------------------------------

  /**
   * Every byte of audio this tab is holding: slices being written, slices the
   * store could not take, the store's own memory (zero for IndexedDB), and
   * the one part in flight.
   */
  private held(): number {
    let bytes = 0;
    for (const s of this.writing) if (s.data) bytes += s.size;
    for (const s of this.queue) if (s.data) bytes += s.size;
    for (const s of this.formedSlices ?? []) if (s.data) bytes += s.size;
    return bytes + this.store.heldBytes() + (this.formedBody?.bytes.byteLength ?? 0);
  }

  private notePeak(): void {
    const now = this.held();
    if (now > this.stat.peakHeldBytes) this.stat.peakHeldBytes = now;
  }

  private kick(): void {
    if (this.pumping || this.discarded || this.interrupt) return;
    this.pumping = this.pump().finally(() => {
      this.pumping = null;
      // A slice that was queued while the loop was on its way out would
      // otherwise wait for the next slice to start it.
      if (this.queue.length > 0 || this.formed) this.kick();
    });
  }

  private async persist(force = false): Promise<void> {
    if (this.outboxDropped) return;
    const now = this.now();
    if (!force && now - this.lastTouch < 1000) return;
    this.lastTouch = now;
    const record: OutboxRecord = {
      sessionId: this.sessionId,
      mimeType: this.mimeType,
      partMs: this.config.partMs,
      partLimitBytes: this.config.partLimitBytes,
      idleCloseS: this.config.idleCloseS,
      aliveAt: now,
      nextSeq: this.nextSeq,
      nextSlice: this.nextSlice,
      firstUnacked: this.firstUnacked,
      formed: this.formed,
      ended: this.ended,
      endedBy: this.endedBy,
      durationMs: this.durationMs,
      ackedMs: this.ackedMs,
      finished: this.finishSent,
      held: this.heldState,
      deliveredText: this.deliveredText,
      offer: this.offerState,
      init: toBuffer(this.init),
      chain: this.chain,
      target:
        this.chain.length > 1
          ? {
              sessionId: this.target.sessionId,
              firstSlice: this.target.firstSlice,
              prefix: toBuffer(this.target.prefix),
            }
          : null,
      opening: this.continuing,
    };
    try {
      await this.store.saveRecord(record);
    } catch {
      /* the outbox is a safety net; the upload does not depend on it */
    }
  }

  private pendingSlices(): number {
    return (
      this.queue.length +
      (this.formed ? this.formed.last - this.formed.first + 1 : 0) +
      this.writing.size
    );
  }

  private emit(): void {
    const now = this.now();
    this.progress = {
      ...this.progress,
      pendingParts: this.pendingSlices() - this.writing.size,
      pendingMs: Math.max(0, this.durationMs - this.ackedMs),
      savedMs: this.ackedMs,
      offlineLong:
        this.progress.offline &&
        this.offlineSince !== null &&
        now - this.offlineSince > this.config.idleCloseS * 1000,
      idleCloseS: this.config.idleCloseS,
      storageTrouble:
        this.storageTroubleSince !== null &&
        now - this.storageTroubleSince > STORAGE_TROUBLE_AFTER_MS,
    };
    this.onProgress({ ...this.progress });
  }

  private setOffline(offline: boolean): void {
    if (offline) this.offlineSince ??= this.now();
    else this.offlineSince = null;
    this.progress = { ...this.progress, offline };
  }

  /** Fold a session state into the preview. Segments are final and append-only. */
  private apply(state: SessionState): void {
    this.lastState = state;
    if (state.rev > this.rev) this.rev = state.rev;
    let preview = this.progress.preview;
    for (const seg of state.segments) {
      if (seg.i < this.cursor) continue; // a replayed cursor: already shown
      preview = joinPreview(preview, seg.text.trim());
      if (seg.low) this.lowSeen = true;
      this.cursor = seg.i + 1;
    }
    if (preview.length > PREVIEW_CHARS) preview = preview.slice(-PREVIEW_CHARS);
    this.progress = {
      ...this.progress,
      preview,
      tentative: state.tentative,
      audioMs: state.audioMs,
      transcribedMs: state.transcribedMs,
      backlogMs: state.backlogMs,
      waitingOn: state.waitingOn,
      progressive: state.progressive,
      retentionDays: state.retentionDays ?? this.progress.retentionDays,
    };
  }

  private async sleep(ms: number): Promise<void> {
    if (this.discarded) return;
    await doSleep(ms, this.wake);
  }

  private stop(interrupt: SessionInterrupt): void {
    this.interrupt = interrupt;
    this.emit();
    this.onInterrupt(interrupt);
  }

  /** The upload loop. One runs at a time; it exits when there is nothing to send. */
  private async pump(): Promise<void> {
    let attempt = 0;
    while (!this.discarded && !this.interrupt) {
      if (this.continuing) {
        const next = await this.continueInNewSession();
        if (this.discarded) return;
        if (next === 'opened') {
          attempt = 0;
          continue;
        }
        if (next === 'retry') {
          this.emit();
          await this.persist();
          await this.sleep(backoffMs(attempt, this.random));
          attempt += 1;
          continue;
        }
        this.continuing = null;
        await this.persist(true);
        this.stop(next === 'refused' ? { kind: 'session_closed', endedBy: this.closedBy } : next);
        return;
      }
      if (!this.formed) {
        if (this.queue.length === 0) return;
        // Coalesce whatever has queued up, to the server's part limit (less
        // the front a continuation's part 0 carries).
        const front = this.nextSeq === 0 ? this.target.prefix?.byteLength ?? 0 : 0;
        const limit = Math.max(1, this.config.partLimitBytes - front);
        const cap = this.splitLimit ?? Number.MAX_SAFE_INTEGER;
        let bytes = 0;
        let count = 0;
        while (
          count < this.queue.length &&
          count < cap &&
          (count === 0 || bytes + this.queue[count]!.size <= limit)
        ) {
          bytes += this.queue[count]!.size;
          count += 1;
        }
        const taken = this.queue.splice(0, count);
        this.formed = { seq: this.nextSeq, first: taken[0]!.idx, last: taken[taken.length - 1]!.idx };
        this.formedSlices = taken;
        this.formedTimeouts = 0;
        await this.persist(true);
      }
      if (!this.formedBody) {
        const body = await this.readFormed();
        if (!body) {
          // The bytes are gone from this device (storage failed under us).
          this.stop({ kind: 'lost_parts' });
          return;
        }
        this.formedBody = body;
        this.notePeak();
      }
      const { seq } = this.formed;
      const { bytes, sha, endMs } = this.formedBody;
      this.stat.putRequests += 1;
      const reply = await callSessionApi(
        this.fetchImpl,
        `${SESSIONS_URL}/${this.target.sessionId}/parts/${seq}?cursor=${this.cursor}`,
        {
          method: 'PUT',
          headers: {
            'content-type': this.mimeType || 'application/octet-stream',
            'x-part-sha256': sha,
            'x-part-end-ms': String(Math.round(endMs)),
          },
          body: bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength) as ArrayBuffer,
          signal: this.abort.signal,
        },
        { timeoutMs: partTimeoutMs(bytes.byteLength, this.formedTimeouts), slot: this.slot },
      );
      if (reply.kind === 'aborted' || this.discarded) return;

      if (reply.kind === 'ok') {
        attempt = 0;
        const state = parseSessionState(reply.body);
        if (state) this.apply(state);
        await this.acknowledge();
        this.setOffline(false);
        this.progress = { ...this.progress, lastAckAt: this.now() };
        this.storageTroubleSince = null;
        this.emit();
        continue;
      }

      if (reply.kind === 'refused') {
        const reason = reply.reason;
        if (reply.status === 413 && this.formed.last > this.formed.first) {
          // Nothing was stored for this seq, so it can be re-formed smaller.
          const count = this.formed.last - this.formed.first + 1;
          this.splitLimit = Math.max(1, Math.floor(count / 2));
          this.queue.unshift(...(this.formedSlices ?? []));
          this.formed = null;
          this.formedSlices = null;
          this.formedBody = null;
          continue;
        }
        if (reply.status === 409 && reason === 'session_closed') {
          const endedBy = str(reply.body.ended_by);
          // The server continues only a session it closed itself for silence
          // (fix/voice-server-hardening `_continuation_of`): that is done at
          // once, recording on. Any other close is continued only when the
          // person presses "Upload the rest", as a new recording.
          if (endedBy === 'idle' || (this.continueAnyClose && endedBy !== 'undecodable')) {
            this.closedBy = endedBy;
            this.continuing = {
              clientKey: newClientKey(),
              firstSlice: this.formed.first,
              linked: endedBy === 'idle',
            };
            await this.persist(true);
            continue;
          }
        }
        if (reply.status === 409 && reason === 'out_of_order') {
          // The server is missing parts this device already let go of: they
          // were acknowledged once, so this is the server's loss, not ours.
          this.stop({ kind: 'lost_parts' });
          return;
        }
        if (!partRetryable(reply)) {
          this.stop(this.interruptFor(reply));
          return;
        }
        if (reply.status === 503 && reason === 'storage_unavailable') {
          this.storageTroubleSince ??= this.now();
        }
      } else {
        if (reply.restarted) {
          // `online` fired under this request: send it again now.
          continue;
        }
        if (reply.timedOut) this.formedTimeouts += 1;
        this.setOffline(true);
      }
      this.emit();
      await this.persist();
      const retryAfter = reply.kind === 'refused' ? reply.retryAfterMs ?? 0 : 0;
      await this.sleep(Math.max(backoffMs(attempt, this.random), retryAfter));
      attempt += 1;
    }
  }

  private async readFormed(): Promise<{ bytes: Uint8Array; sha: string; endMs: number } | null> {
    const f = this.formed!;
    const inMemory = new Map<number, QueuedSlice>();
    for (const s of this.formedSlices ?? []) if (s.data) inMemory.set(s.idx, s);
    let stored: StoredSlice[] = [];
    if (inMemory.size < f.last - f.first + 1) {
      try {
        stored = await this.store.readSlices(this.sessionId, f.first, f.last);
      } catch {
        stored = [];
      }
    }
    const byIdx = new Map(stored.map((s) => [s.idx, s]));
    const parts: Uint8Array[] = [];
    let endMs = 0;
    for (let i = f.first; i <= f.last; i += 1) {
      const mem = inMemory.get(i);
      if (mem && mem.data) {
        parts.push(await blobBytes(mem.data));
        endMs = mem.endMs;
        continue;
      }
      const s = byIdx.get(i);
      if (!s) return null;
      parts.push(s.bytes);
      endMs = s.endMs;
    }
    // Part 0 of a continuation starts with the front that makes it a stream
    // of its own. It is fixed when the continuation opens, so a replay of
    // this part is byte-identical.
    if (f.seq === 0 && this.target.prefix && this.target.prefix.byteLength > 0) {
      parts.unshift(this.target.prefix);
    }
    const bytes = concatBytes(parts);
    return { bytes, sha: await this.sha(bytes), endMs };
  }

  /**
   * Open the session the rest of this recording continues in, and move the
   * slices the closed one refused over to it. Returns what the pump should
   * do: go on, try again later, give up (the rest is then held on this
   * device), or stop for a sign-out.
   */
  private async continueInNewSession(): Promise<'opened' | 'retry' | 'refused' | SessionInterrupt> {
    if (this.formed) {
      // The closed session stored nothing under this part number.
      this.queue.unshift(...(this.formedSlices ?? []));
      this.formed = null;
      this.formedSlices = null;
      this.formedBody = null;
      this.formedTimeouts = 0;
      this.splitLimit = null;
    }
    const first = this.queue[0];
    if (!first) return 'refused';
    const opening = (this.continuing ??= { clientKey: newClientKey(), firstSlice: first.idx, linked: true });
    let prefix: Uint8Array | null;
    if (opening.linked || first.idx === 0) {
      // Byte for byte: the server decodes a linked continuation after the
      // bytes it continues, and slice 0 opens the stream itself.
      prefix = new Uint8Array(0);
    } else {
      // A recording of its own must open as a stream (WebmCutTracker).
      let lead = first.lead;
      if (lead === undefined) {
        const stored = await this.store.readSlices(this.sessionId, first.idx, first.idx).catch(() => []);
        lead = stored[0]?.lead ?? null;
      }
      prefix = this.init && lead ? concatBytes([this.init, lead]) : null;
    }
    if (!prefix) return 'refused';
    const reply = await callSessionApi(
      this.fetchImpl,
      SESSIONS_URL,
      {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({
          client_key: opening.clientKey,
          mime_type: this.mimeType,
          language: 'auto',
          part_ms: this.config.partMs,
          ...(opening.linked ? { continues: this.target.sessionId } : {}),
        }),
        signal: this.abort.signal,
      },
      { timeoutMs: REQUEST_TIMEOUT_MS, slot: this.slot },
    );
    if (reply.kind === 'aborted') return 'retry';
    if (reply.kind === 'unreachable') {
      if (!reply.restarted) this.setOffline(true);
      return 'retry';
    }
    let sessionId: string | null = null;
    let state: SessionState | null = null;
    if (reply.kind === 'refused') {
      if (reply.status === 401) return { kind: 'signed_out' };
      if (reply.status === 409 && reply.reason === 'already_continued' && isSessionId(reply.body.session_id)) {
        // Continued already (by a create whose answer was lost): use that one.
        // Its part 0 is replayed byte for byte, so the server tells a
        // duplicate from a conflict by SHA-256 as for any part.
        sessionId = reply.body.session_id;
      } else if (reply.status === 409 && reply.reason === 'not_continuable' && opening.linked) {
        // The server will not link it; the rest can still go up as a
        // recording of its own if it can open as a stream.
        this.continuing = { clientKey: newClientKey(), firstSlice: first.idx, linked: false };
        await this.persist(true);
        return this.continueInNewSession();
      } else if (reply.status === 429 || (reply.status >= 500 && reply.status !== 507)) {
        // Busy, rate-limited or briefly unable to store: the slices wait here.
        return 'retry';
      } else {
        // Another live recording of this person, storage or quota full, voice
        // turned off: the rest is held on this device, and offered.
        return 'refused';
      }
    } else {
      state = parseSessionState(reply.body);
      if (!state) return 'retry';
      sessionId = state.sessionId;
    }
    if (!this.chain.includes(sessionId)) this.chain.push(sessionId);
    this.target = { sessionId, firstSlice: first.idx, prefix };
    this.continuing = null;
    this.nextSeq = 0;
    this.cursor = 0;
    this.rev = -1;
    this.lastState = null;
    if (state) this.apply(state);
    this.setOffline(false);
    await this.persist(true);
    this.emit();
    return 'opened';
  }

  private async acknowledge(): Promise<void> {
    const f = this.formed!;
    const size = this.formedBody?.bytes.byteLength ?? 0;
    this.stat.partsAcked += 1;
    this.stat.bytesAcked += size;
    this.ackedMs = Math.max(this.ackedMs, this.formedBody?.endMs ?? 0);
    for (const s of this.formedSlices ?? []) s.data = null;
    this.formed = null;
    this.formedSlices = null;
    this.formedBody = null;
    this.formedTimeouts = 0;
    this.splitLimit = null;
    this.nextSeq += 1;
    this.firstUnacked = f.last + 1;
    // The server took a part after refusing one: whatever was held is moving.
    if (this.heldState && this.pendingSlices() === 0) this.heldState = null;
    // The record first, then the bytes: a crash between the two leaves
    // acknowledged slices behind, which adoption skips by `firstUnacked`.
    // The other order would leave a record naming a part whose bytes are
    // gone, and the recording would end there for no reason.
    await this.persist(true);
    try {
      await this.store.dropSlices(this.sessionId, f.first, f.last);
    } catch {
      /* skipped on adoption by firstUnacked, and removed there */
    }
  }

  private interruptFor(reply: Extract<Reply, { kind: 'refused' }>): SessionInterrupt {
    const { status, reason } = reply;
    if (status === 401) return { kind: 'signed_out' };
    if (status === 403) return { kind: 'voice_off', detail: reply.detail };
    if (status === 404) return { kind: 'not_found' };
    if (status === 409 && reason === 'part_conflict') return { kind: 'part_conflict' };
    if (status === 409 && reason === 'session_closed') {
      return { kind: 'session_closed', endedBy: str(reply.body.ended_by) };
    }
    if (status === 507 && (reason === 'quota_full' || reason === 'quota_exceeded')) return { kind: 'quota_exceeded' };
    if (status === 507) return { kind: 'storage_full' };
    if (status === 415) return { kind: 'unsupported_format', detail: reply.detail };
    return { kind: 'generic' };
  }

  private t(): string {
    return formatElapsed(this.lastState?.audioMs || this.ackedMs || this.durationMs);
  }

  /** What an interrupted session comes to, once the recorder has stopped. */
  private async afterInterrupt(
    interrupt: SessionInterrupt,
    notices: string[],
    peakLevel: number | null | undefined,
  ): Promise<SessionResult> {
    const fail = (message: string, retryable: boolean): SessionResult => ({
      kind: 'error',
      error: { message, retryable },
      offer: null,
    });
    // Everything recorded has already been written to the store by now
    // (end() awaited the write chain), so this is what the server lacks.
    const pending = this.pendingSlices();
    if (HOLDS_AUDIO.has(interrupt.kind) && pending > 0) {
      return this.holdTheRest(interrupt, notices, peakLevel ?? null);
    }
    switch (interrupt.kind) {
      case 'signed_out': {
        const t = formatElapsed(this.ackedMs);
        // A persistent outbox keeps the rest for adoption after sign-in.
        if (pending === 0) {
          await this.dropOutbox();
          return fail(VOICE_MESSAGES.signedOutNothingPending(t), false);
        }
        return fail(
          this.store.persistent ? VOICE_MESSAGES.signedOutKept(t) : VOICE_MESSAGES.signedOutLost(t),
          false,
        );
      }
      case 'voice_off':
        await this.dropOutbox();
        return fail(interrupt.detail || VOICE_MESSAGES.voiceOff, false);
      case 'not_found':
        await this.dropOutbox();
        return fail(VOICE_MESSAGES.notFound, false);
      case 'part_conflict':
        await this.dropOutbox();
        return fail(VOICE_MESSAGES.partConflict, false);
      case 'unsupported_format':
        await this.dropOutbox();
        return fail(
          VOICE_MESSAGES.unsupportedFormat(interrupt.detail || 'This audio format is not supported.'),
          false,
        );
      case 'session_closed': {
        const message =
          interrupt.endedBy === 'idle'
            ? VOICE_MESSAGES.closedIdle(idleWords(this.config.idleCloseS))
            : VOICE_MESSAGES.closedElsewhere;
        // The server finishes it and keeps it; if its words arrive, they are
        // still this person's draft.
        return this.waitForResult([message, ...notices], peakLevel ?? null, message);
      }
      case 'storage_full': {
        const message = VOICE_MESSAGES.storageFullMid(this.t());
        return this.waitForResult([message, ...notices], peakLevel ?? null, message);
      }
      case 'quota_exceeded': {
        const message = VOICE_MESSAGES.quotaMid(this.t());
        return this.waitForResult([message, ...notices], peakLevel ?? null, message);
      }
      case 'lost_parts': {
        const t = formatElapsed(this.ackedMs);
        // Out of order: the server lost parts this device had already been
        // told were stored. What follows cannot be appended after the hole
        // (the contract numbers parts contiguously), so it ends here.
        if (this.nextSlice > 0) {
          await this.store.dropSlices(this.sessionId, 0, this.nextSlice - 1).catch(() => undefined);
        }
        this.queue = [];
        return this.finishAndWait(null, 'lost_parts', [VOICE_MESSAGES.lostParts(t), ...notices], peakLevel ?? null);
      }
      default:
        return this.finishAndWait(null, 'lost_parts', [VOICE_MESSAGES.generic(this.t()), ...notices], peakLevel ?? null);
    }
  }

  /**
   * The server stopped taking parts while this device still had some. They
   * are KEPT (record `held`), the person is told exactly that, and offered
   * "Upload the rest". Whatever the server did store is still transcribed and
   * delivered as usual.
   */
  private async holdTheRest(
    interrupt: SessionInterrupt,
    notices: string[],
    peakLevel: number | null,
  ): Promise<SessionResult> {
    const heldMs = Math.max(0, this.durationMs - this.ackedMs);
    const held = formatElapsed(heldMs);
    const saved = formatElapsed(this.ackedMs);
    this.heldState = {
      reason: interrupt.kind,
      endedBy: interrupt.kind === 'session_closed' ? interrupt.endedBy : null,
    };
    await this.persist(true);
    let message: string;
    if (this.heldBefore) message = VOICE_MESSAGES.heldStill(held);
    else if (interrupt.kind === 'session_closed') {
      message =
        interrupt.endedBy === 'idle'
          ? VOICE_MESSAGES.heldIdle(idleWords(this.config.idleCloseS), saved, held)
          : interrupt.endedBy === 'quota_full'
            ? VOICE_MESSAGES.heldQuota(saved, held)
            : interrupt.endedBy === 'storage_full'
              ? VOICE_MESSAGES.heldStorageFull(saved, held)
              : interrupt.endedBy === 'undecodable'
                ? VOICE_MESSAGES.heldUndecodable(held)
                : VOICE_MESSAGES.heldElsewhere(held);
    } else if (interrupt.kind === 'storage_full') message = VOICE_MESSAGES.heldStorageFull(saved, held);
    else if (interrupt.kind === 'quota_exceeded') message = VOICE_MESSAGES.heldQuota(saved, held);
    else if (interrupt.kind === 'voice_off') {
      message = VOICE_MESSAGES.heldVoiceOff(interrupt.detail || VOICE_MESSAGES.voiceOff, held);
    } else message = VOICE_MESSAGES.heldGeneric(saved, held);
    const offer = (replaces: string | null): VoiceOffer => ({
      kind: 'upload_rest',
      sessionId: this.sessionId,
      replaces,
      message,
      label: VOICE_MESSAGES.uploadRest,
    });

    let result: SessionResult;
    if (interrupt.kind === 'voice_off') {
      // Nothing of this session can be read back while the feature is off.
      result = { kind: 'error', error: { message, retryable: false }, offer: null };
    } else if (interrupt.kind === 'generic') {
      result = await this.finishAndWait(null, 'lost_parts', [message, ...notices], peakLevel, true);
    } else {
      result = await this.waitForResult([message, ...notices], peakLevel, message, true);
    }
    if (result.kind === 'withdrawn') return result;
    // The server could not decode the stream; the rest of the same stream
    // cannot help it. Kept here (it is never deleted unasked), not offered.
    const uploadable = !(interrupt.kind === 'session_closed' && interrupt.endedBy === 'undecodable');
    if (result.kind === 'text') {
      return {
        ...result,
        notices: result.notices.includes(message) ? result.notices : [message, ...result.notices],
        offer: uploadable ? offer(this.deliveredText ?? result.text) : null,
      };
    }
    return {
      kind: 'error',
      error: { message, retryable: uploadable },
      offer: uploadable ? offer(this.deliveredText) : null,
    };
  }

  private async finishAndWait(
    lastPart: number | null,
    endedBy: EndedBy,
    notices: string[],
    peakLevel: number | null,
    keepRecord = false,
  ): Promise<SessionResult> {
    let attempt = 0;
    let last = lastPart;
    let by = endedBy;
    for (;;) {
      if (this.discarded) return { kind: 'withdrawn' };
      const reply = await callSessionApi(
        this.fetchImpl,
        `${SESSIONS_URL}/${this.target.sessionId}/finish`,
        {
          method: 'POST',
          headers: { 'content-type': 'application/json' },
          body: JSON.stringify({
            last_part: last,
            duration_ms: Math.round(this.durationMs),
            ended_by: by,
          }),
          signal: this.abort.signal,
        },
        { timeoutMs: REQUEST_TIMEOUT_MS, slot: this.slot },
      );
      if (reply.kind === 'aborted' || this.discarded) return { kind: 'withdrawn' };
      if (reply.kind === 'ok') {
        const state = parseSessionState(reply.body);
        if (state) this.apply(state);
        // Finish is accepted: nothing is left to upload, but the record stays
        // until the words are in the composer (settleRecord).
        this.finishSent = true;
        this.setOffline(false);
        await this.persist(true);
        this.emit();
        return this.waitForResult(notices, peakLevel, null, keepRecord);
      }
      if (reply.kind === 'refused') {
        if (reply.status === 409 && reply.reason === 'parts_missing') {
          // Everything this device had was acknowledged, so what the server
          // lacks is gone. End at whatever it holds, and say where.
          last = null;
          by = 'lost_parts';
          notices = [VOICE_MESSAGES.lostParts(formatElapsed(this.ackedMs)), ...notices];
          continue;
        }
        if (!partRetryable(reply)) return this.refusalAfterStop(reply, keepRecord);
      } else if (reply.restarted) {
        continue;
      }
      this.setOffline(reply.kind === 'unreachable');
      this.emit();
      await this.persist();
      await this.sleep(backoffMs(attempt, this.random));
      attempt += 1;
    }
  }

  private async refusalAfterStop(
    reply: Extract<Reply, { kind: 'refused' }>,
    keepRecord = false,
  ): Promise<SessionResult> {
    const fail = (message: string, retryable: boolean): SessionResult => ({
      kind: 'error',
      error: { message, retryable },
      offer: null,
    });
    if (reply.status === 401) {
      // The record stays: once signed in again, adoption finishes it.
      return fail(VOICE_MESSAGES.signedOutNothingPending(this.t()), false);
    }
    // Every other refusal is final, and nothing on this device can change it
    // — except audio the server never stored, which stays held.
    if (!keepRecord) await this.dropOutbox();
    if (reply.status === 403) return fail(reply.detail || VOICE_MESSAGES.voiceOff, false);
    if (reply.status === 404) return fail(VOICE_MESSAGES.notFound, false);
    return fail(VOICE_MESSAGES.generic(this.t()), true);
  }

  /**
   * Long-poll until the session is done or failed. Every request returns
   * within 25 s, under Cloudflare's 125 s first-byte limit, so there is no
   * heartbeat to keep and no ceiling on how long the whole wait may be.
   *
   * An error with nothing left to act on drops the record here; words, and
   * a Retry to offer, keep it for `settleRecord`.
   */
  private async waitForResult(
    notices: string[],
    peakLevel: number | null,
    failureMessage: string | null,
    keepRecord = false,
  ): Promise<SessionResult> {
    this.startKeepAlive();
    const settled = await pollSession(
      this.target.sessionId,
      { cursor: this.cursor, rev: this.rev, state: this.lastState },
      {
        fetchImpl: this.fetchImpl,
        random: this.random,
        signal: this.abort.signal,
        wake: this.wake,
        slot: this.slot,
        waitS: this.config.longPollMaxS,
        onState: (state) => {
          this.apply(state);
          this.emit();
        },
        onOffline: (offline) => {
          this.setOffline(offline);
          this.emit();
        },
      },
    );
    if (this.discarded || settled.kind === 'aborted') return { kind: 'withdrawn' };
    if (settled.kind === 'refused') {
      if (settled.reply.status === 401) {
        // Signed out while waiting: the finished record stays, and the words
        // are offered back after sign-in.
        return this.refusalAfterStop(settled.reply, true);
      }
      if (failureMessage) {
        if (!keepRecord) await this.dropOutbox();
        return { kind: 'error', error: { message: failureMessage, retryable: false }, offer: null };
      }
      return this.refusalAfterStop(settled.reply, keepRecord);
    }
    let result = describeOutcome(settled.state, {
      lowSeen: this.lowSeen,
      notices,
      peakLevel,
      durationMs: this.durationMs,
    });
    if (this.chain.length > 1) result = await this.joinChain(result);
    if (result.kind === 'error' && !offerKeepsRecord(result.offer) && !keepRecord) {
      await this.dropOutbox();
    }
    return result;
  }

  /**
   * A continued recording's words: every session's transcript, in the order
   * they were recorded, as one text. The sessions the server closed finish
   * by themselves; they are only waited for here.
   */
  private async joinChain(last: SessionResult): Promise<SessionResult> {
    if (last.kind === 'withdrawn') return last;
    const results: SessionResult[] = [];
    for (const id of this.chain.slice(0, -1)) {
      const settled = await pollSession(
        id,
        { cursor: 0, rev: -1, state: null },
        {
          fetchImpl: this.fetchImpl,
          random: this.random,
          signal: this.abort.signal,
          wake: this.wake,
          slot: this.slot,
          waitS: this.config.longPollMaxS,
        },
      );
      if (settled.kind === 'aborted' || this.discarded) return { kind: 'withdrawn' };
      if (settled.kind === 'refused') continue; // gone from the server: nothing to deliver
      results.push(describeOutcome(settled.state, {}));
    }
    results.push(last);
    const texts = results.filter(
      (r): r is Extract<SessionResult, { kind: 'text' }> => r.kind === 'text',
    );
    if (texts.length === 0) {
      return results.find((r) => r.kind === 'error' && r.offer !== null) ?? last;
    }
    const offers = results.map((r) => (r.kind === 'withdrawn' ? null : r.offer));
    // The language of the session that holds most of the words speaks for the whole.
    const most = texts.reduce((a, b) => (b.text.length > a.text.length ? b : a));
    return {
      kind: 'text',
      text: texts.reduce((acc, r) => mergeTranscript(acc, r.text), ''),
      notices: [...new Set(texts.flatMap((r) => r.notices))],
      offer: offers.find((o) => o?.kind === 'retranscribe') ?? null,
      sessionId: this.sessionId,
      ...(most.language ? { language: most.language } : {}),
      ...(most.languageCode ? { languageCode: most.languageCode } : {}),
    };
  }
}

/**
 * ONE LONG-POLL PER TAB. The server answers a third concurrent long-poll of a
 * person with 429 too_many_polls (fix/voice-server-hardening: at most two,
 * each one holds a Postgres-polling loop). A tab can have several sessions
 * waiting at once — its own recording, one adopted from a closed tab, the
 * sessions of a continued recording — so their GETs take turns here.
 */
let pollQueue: Promise<void> = Promise.resolve();
function pollTurn(): Promise<() => void> {
  let release!: () => void;
  const mine = new Promise<void>((resolve) => (release = resolve));
  const before = pollQueue;
  pollQueue = before.then(() => mine);
  return before.then(() => release);
}

type PollResult =
  | { kind: 'settled'; state: SessionState }
  | { kind: 'refused'; reply: Extract<Reply, { kind: 'refused' }> }
  | { kind: 'aborted' };

/** GET the session until it is done or failed, retrying what can be retried. */
export async function pollSession(
  sessionId: string,
  from: { cursor: number; rev: number; state: SessionState | null },
  opts: {
    fetchImpl: typeof fetch;
    random?: () => number;
    signal?: AbortSignal;
    wake?: { current: (() => void) | null };
    slot?: InflightSlot;
    waitS?: number;
    onState?: (state: SessionState) => void;
    onOffline?: (offline: boolean) => void;
  },
): Promise<PollResult> {
  let { cursor, rev } = from;
  if (from.state && (from.state.status === 'done' || from.state.status === 'failed')) {
    return { kind: 'settled', state: from.state };
  }
  const wake = opts.wake ?? { current: null };
  const waitS = Math.min(25, Math.max(0, opts.waitS ?? 25));
  let attempt = 0;
  for (;;) {
    if (opts.signal?.aborted) return { kind: 'aborted' };
    const query = new URLSearchParams({
      cursor: String(cursor),
      since_rev: String(rev),
      wait_s: String(waitS),
    });
    const done = await pollTurn();
    let reply: Reply;
    try {
      reply = await callSessionApi(
        opts.fetchImpl,
        `${SESSIONS_URL}/${sessionId}?${query}`,
        { method: 'GET', signal: opts.signal },
        // The server answers by wait_s; past that plus slack the socket is dead.
        { timeoutMs: waitS * 1000 + LONG_POLL_SLACK_MS, slot: opts.slot },
      );
    } finally {
      done();
    }
    if (reply.kind === 'aborted') return { kind: 'aborted' };
    if (reply.kind === 'ok') {
      attempt = 0;
      opts.onOffline?.(false);
      const state = parseSessionState(reply.body);
      if (!state) {
        await doSleep(backoffMs(0, opts.random), wake);
        continue;
      }
      for (const seg of state.segments) if (seg.i + 1 > cursor) cursor = seg.i + 1;
      const changed = state.rev > rev;
      if (changed) rev = state.rev;
      opts.onState?.(state);
      if (state.status === 'done' || state.status === 'failed') return { kind: 'settled', state };
      if (state.status === 'cancelled') {
        return {
          kind: 'refused',
          reply: { kind: 'refused', status: 404, reason: 'not_found', detail: null, body: {}, retryAfterMs: null },
        };
      }
      // A long-poll that came back with nothing new was held for up to 25 s
      // already. One that came back AT ONCE with nothing new (a proxy that
      // does not hold requests, an older server) must not become a loop
      // that asks again as fast as the network allows.
      if (!changed) await doSleep(1000, wake);
      continue;
    }
    if (reply.kind === 'refused' && !partRetryable(reply)) return { kind: 'refused', reply };
    if (reply.kind === 'unreachable' && reply.restarted) continue;
    opts.onOffline?.(reply.kind === 'unreachable');
    await doSleep(backoffMs(attempt, opts.random), wake);
    attempt += 1;
  }
}

/**
 * "Retry" on a transcript with gaps or a session the engine could not reach:
 * the audio is stored, so this re-reads it instead of asking the person to
 * speak again.
 */
export async function retranscribeSession(
  offer: Extract<VoiceOffer, { kind: 'retranscribe' }>,
  deps: SessionDeps & { signal?: AbortSignal } = {},
): Promise<SessionResult> {
  const fetchImpl = deps.fetchImpl ?? fetch;
  const reply = await callSessionApi(
    fetchImpl,
    `${SESSIONS_URL}/${offer.sessionId}/retranscribe`,
    {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ scope: offer.scope }),
      signal: deps.signal,
    },
    { timeoutMs: REQUEST_TIMEOUT_MS },
  );
  const fail = (message: string, retryable: boolean, again: VoiceOffer | null = null): SessionResult => ({
    kind: 'error',
    error: { message, retryable },
    offer: again,
  });
  if (reply.kind === 'aborted') return { kind: 'withdrawn' };
  if (reply.kind === 'unreachable') return fail(VOICE_MESSAGES.retryUnreachable, true, offer);
  if (reply.kind === 'refused') {
    if (reply.status === 409 && reply.reason === 'retranscribe_busy') {
      return fail(VOICE_MESSAGES.retranscribeBusy, true, offer);
    }
    if (reply.status === 429) return fail(VOICE_MESSAGES.retranscribeRateLimited, true, offer);
    if (reply.status === 409) return fail(VOICE_MESSAGES.sessionBusy, true);
    if (reply.status === 410) return fail(VOICE_MESSAGES.audioDeleted, false);
    if (reply.status === 404) return fail(VOICE_MESSAGES.notFound, false);
    if (reply.status === 401) return fail(VOICE_MESSAGES.retrySignedOut, false);
    if (reply.status === 403) return fail(reply.detail || VOICE_MESSAGES.voiceOff, false);
    return fail(VOICE_MESSAGES.retryGeneric, true, offer);
  }
  const state = parseSessionState(reply.body);
  let lowSeen = false;
  const settled = await pollSession(
    offer.sessionId,
    {
      cursor: 0,
      rev: state?.rev ?? -1,
      state: state && (state.status === 'done' || state.status === 'failed') ? state : null,
    },
    {
      fetchImpl,
      random: deps.random,
      signal: deps.signal,
      onState: (s) => {
        if (s.segments.some((seg) => seg.low)) lowSeen = true;
      },
    },
  );
  if (settled.kind === 'aborted') return { kind: 'withdrawn' };
  if (settled.kind === 'refused') {
    return fail(settled.reply.status === 404 ? VOICE_MESSAGES.notFound : VOICE_MESSAGES.retryGeneric, false);
  }
  return describeOutcome(settled.state, { replaces: offer.replaces, lowSeen });
}

/**
 * The words of a recording have reached the person (or the person dismissed
 * them): its outbox record goes, unless something is still owed — a Retry
 * the person has not pressed, or audio held on this device. `deliveredText`
 * is what now stands in the draft, which a later full transcript replaces.
 */
export async function settleRecord(
  store: OutboxStore,
  sessionId: string,
  keep: {
    deliveredText?: string | null;
    offer?: OutboxRecord['offer'];
  } | null,
): Promise<void> {
  try {
    const record = await store.loadRecord(sessionId);
    if (!record) return;
    if (!keep && !record.held) {
      await store.drop(sessionId);
      return;
    }
    await store.saveRecord({
      ...record,
      deliveredText: keep?.deliveredText !== undefined ? keep.deliveredText : record.deliveredText ?? null,
      offer: keep?.offer !== undefined ? keep.offer : record.held ? record.offer ?? null : null,
    });
  } catch {
    /* the outbox is a safety net */
  }
}

/** Recording time held on this device across a store's records (for the logout question). */
export async function heldOnDeviceMs(store: OutboxStore): Promise<number> {
  let ms = 0;
  let records: OutboxRecord[] = [];
  try {
    records = await store.listRecords();
  } catch {
    return 0;
  }
  for (const r of records) {
    if (r.finished) continue;
    let slices: StoredSlice[] = [];
    try {
      slices = await store.readSlices(r.sessionId, r.firstUnacked ?? 0, Math.max(0, r.nextSlice - 1));
    } catch {
      slices = [];
    }
    if (slices.length > 0) ms += Math.max(r.partMs * slices.length, r.durationMs - r.ackedMs);
  }
  return ms;
}

/**
 * Before an explicit logout. Sends every pending discard while the cookie is
 * still valid, then asks the person if anything would be lost or left behind:
 * audio this browser never uploaded is deleted by the logout (it must not stay
 * readable for the next person on a shared computer), and a discard the
 * server has not confirmed stays owed until they sign in here again.
 *
 * Resolves false when the person chose to stay signed in.
 */
export async function voiceLogoutCheck(
  owner: string | null,
  deps: {
    fetchImpl?: typeof fetch;
    confirm?: (message: string) => boolean;
    factory?: IDBFactory;
    storage?: StorageLike | null;
  } = {},
): Promise<boolean> {
  const storage = deps.storage === undefined ? browserStorage() : deps.storage;
  const factory = deps.factory ?? (typeof indexedDB !== 'undefined' ? indexedDB : undefined);
  const ask = deps.confirm ?? ((m: string) => (typeof window !== 'undefined' ? window.confirm(m) : true));
  let pendingDiscards = 0;
  if (owner) {
    const { pending } = await flushTombstones(tombstonesFor(owner, storage), {
      fetchImpl: deps.fetchImpl,
    });
    pendingDiscards = pending.length;
  }
  let heldMs = 0;
  if (factory) {
    for (const o of outboxOwners(storage)) {
      const store = await openOutbox(factory, outboxDbName(o));
      heldMs += await heldOnDeviceMs(store);
    }
  }
  const questions: string[] = [];
  if (heldMs > 0) questions.push(VOICE_MESSAGES.logoutHeld(formatElapsed(heldMs)));
  if (pendingDiscards > 0) questions.push(VOICE_MESSAGES.logoutDiscardPending);
  if (questions.length === 0) return true;
  return ask(questions.join('\n\n'));
}

// ---------------------------------------------------------------------------
// Putting a re-transcribed recording where its first transcript went
// ---------------------------------------------------------------------------

/** Where in the draft a transcript was put: [start, end) in UTF-16 units. */
export interface DraftSpan {
  start: number;
  end: number;
}

/** `mergeTranscript`, also saying where the transcript landed. */
export function mergeTranscriptAt(
  draft: string,
  transcript: string,
): { text: string; span: DraftSpan | null } {
  const spoken = transcript.trim();
  if (!spoken) return { text: draft, span: null };
  const text = mergeTranscript(draft, spoken);
  const start = text.length - spoken.length;
  return { text, span: { start, end: text.length } };
}

/**
 * Follow a span through one edit of the draft. An edit before the span moves
 * it, one after leaves it, one inside (or across its edge) grows or shrinks
 * it: the transcript region is then "the transcript as the person edited it".
 * Null when nothing of it is left.
 */
export function shiftSpan(span: DraftSpan | null, before: string, after: string): DraftSpan | null {
  if (!span || before === after) return span;
  let a = 0;
  const min = Math.min(before.length, after.length);
  while (a < min && before.charCodeAt(a) === after.charCodeAt(a)) a += 1;
  let b = 0;
  while (
    b < min - a &&
    before.charCodeAt(before.length - 1 - b) === after.charCodeAt(after.length - 1 - b)
  ) {
    b += 1;
  }
  const oldEnd = before.length - b;
  const delta = after.length - before.length;
  let next: DraftSpan;
  if (oldEnd <= span.start) next = { start: span.start + delta, end: span.end + delta };
  else if (a >= span.end) next = span;
  else next = { start: Math.min(span.start, a), end: Math.max(span.end, oldEnd) + delta };
  if (next.start < 0 || next.end > after.length || next.start >= next.end) return null;
  return next;
}

function tokens(text: string): string[] {
  return text.match(/\s+|\S+/g) ?? [];
}

interface Hunk {
  /** base[a0, a1) is replaced by `ins`. */
  a0: number;
  a1: number;
  ins: string[];
}

/** Past this many token edits a merge is not attempted: the person is asked. */
const MAX_MERGE_EDITS = 400;

/**
 * Myers' O(ND) diff over tokens, as replace-hunks against `base`. Null when
 * the two differ by more than MAX_MERGE_EDITS tokens. Common prefix and suffix
 * are cut first, so a 9,000-word transcript with a few edits costs almost
 * nothing.
 */
function diffHunks(base: string[], other: string[]): Hunk[] | null {
  let pre = 0;
  while (pre < base.length && pre < other.length && base[pre] === other[pre]) pre += 1;
  let suf = 0;
  while (
    suf < base.length - pre &&
    suf < other.length - pre &&
    base[base.length - 1 - suf] === other[other.length - 1 - suf]
  ) {
    suf += 1;
  }
  const A = base.slice(pre, base.length - suf);
  const B = other.slice(pre, other.length - suf);
  const N = A.length;
  const M = B.length;
  if (N === 0 && M === 0) return [];
  if (N === 0) return [{ a0: pre, a1: pre, ins: B }];
  if (M === 0) return [{ a0: pre, a1: pre + N, ins: [] }];
  const limit = Math.min(N + M, MAX_MERGE_EDITS);
  const off = limit + 1;
  const v = new Int32Array(2 * limit + 3);
  const trace: Int32Array[] = [];
  let found = -1;
  for (let d = 0; d <= limit && found < 0; d += 1) {
    for (let k = -d; k <= d; k += 2) {
      let x =
        k === -d || (k !== d && v[off + k - 1]! < v[off + k + 1]!) ? v[off + k + 1]! : v[off + k - 1]! + 1;
      let y = x - k;
      while (x < N && y < M && A[x] === B[y]) {
        x += 1;
        y += 1;
      }
      v[off + k] = x;
      if (x >= N && y >= M) {
        found = d;
        break;
      }
    }
    trace.push(v.slice(off - d, off + d + 1));
  }
  if (found < 0) return null;
  // Walk back from (N, M), collecting the edit steps in reverse.
  type Step = { op: 'eq' | 'del' | 'ins'; a: number; b: number };
  const steps: Step[] = [];
  let x = N;
  let y = M;
  for (let d = found; d > 0; d -= 1) {
    const prev = trace[d - 1]!; // index k + (d - 1)
    const at = (k: number) => prev[k + d - 1]!;
    const k = x - y;
    const down = k === -d || (k !== d && at(k - 1) < at(k + 1));
    const pk = down ? k + 1 : k - 1;
    const px = at(pk);
    const py = px - pk;
    while (x > px + (down ? 0 : 1) && y > py + (down ? 1 : 0)) {
      x -= 1;
      y -= 1;
      steps.push({ op: 'eq', a: x, b: y });
    }
    if (down) {
      y -= 1;
      steps.push({ op: 'ins', a: x, b: y });
    } else {
      x -= 1;
      steps.push({ op: 'del', a: x, b: y });
    }
  }
  while (x > 0 && y > 0) {
    x -= 1;
    y -= 1;
    steps.push({ op: 'eq', a: x, b: y });
  }
  steps.reverse();
  const hunks: Hunk[] = [];
  let open: Hunk | null = null;
  for (const s of steps) {
    if (s.op === 'eq') {
      if (open) hunks.push(open);
      open = null;
      continue;
    }
    open ??= { a0: s.a + pre, a1: s.a + pre, ins: [] };
    if (s.op === 'del') open.a1 = s.a + pre + 1;
    else open.ins.push(B[s.b]!);
  }
  if (open) hunks.push(open);
  return hunks;
}

/**
 * Three-way merge of token edits: `mine` (the person's edits to the first
 * transcript) and `theirs` (the new transcript) against `base` (the first
 * transcript). Null on a conflict — the same stretch changed on both sides —
 * or when either side changed too much to merge safely.
 */
export function mergeEdits(base: string, mine: string, theirs: string): string | null {
  const b = tokens(base);
  const hm = diffHunks(b, tokens(mine));
  const ht = diffHunks(b, tokens(theirs));
  if (!hm || !ht) return null;
  for (const x of hm) {
    for (const y of ht) {
      const xEmpty = x.a0 === x.a1;
      const yEmpty = y.a0 === y.a1;
      if (xEmpty && yEmpty) {
        if (x.a0 === y.a0) return null; // both insert at the same point
      } else if (xEmpty) {
        if (y.a0 < x.a0 && x.a0 < y.a1) return null;
      } else if (yEmpty) {
        if (x.a0 < y.a0 && y.a0 < x.a1) return null;
      } else if (x.a0 < y.a1 && y.a0 < x.a1) {
        return null;
      }
    }
  }
  // Apply both, in base order; at one point an insertion goes before a replacement.
  const all = [...hm, ...ht].sort((p, q) => p.a0 - q.a0 || (p.a1 - p.a0) - (q.a1 - q.a0));
  const out: string[] = [];
  let i = 0;
  for (const h of all) {
    while (i < h.a0) out.push(b[i++]!);
    out.push(...h.ins);
    i = Math.max(i, h.a1);
  }
  while (i < b.length) out.push(b[i++]!);
  return out.join('');
}

/**
 * Put a re-transcribed recording where its first transcript went, or say it
 * cannot (null) — never append a second copy.
 *
 * Until 2026-09-29 `replaceTranscript` looked for the first transcript
 * verbatim and, when the person had changed one character of it, appended the
 * whole new transcript after it: a 9,000-word draft with one word
 * re-capitalised went from 79,889 to 159,786 characters. Now the composer
 * tracks where the transcript went (`span`, kept up to date by `shiftSpan`):
 * an untouched region is replaced; an edited one gets the new words merged
 * into the person's edits (`mergeEdits`); and if they collide, or the span is
 * lost and the text cannot be found verbatim, the caller asks.
 */
export function placeRetranscript(
  draft: string,
  span: DraftSpan | null,
  previous: string,
  next: string,
): { text: string; span: DraftSpan } | null {
  const base = previous.trim();
  const fresh = next.trim();
  if (!fresh) return null;
  const splice = (start: number, end: number, replacement: string) => ({
    text: draft.slice(0, start) + replacement + draft.slice(end),
    span: { start, end: start + replacement.length },
  });
  if (span && span.start >= 0 && span.end <= draft.length && span.start < span.end) {
    const region = draft.slice(span.start, span.end);
    if (region === base || region.trim() === base) return splice(span.start, span.end, fresh);
    if (!base) return null;
    const merged = mergeEdits(base, region, fresh);
    return merged === null ? null : splice(span.start, span.end, merged);
  }
  if (!base) return null;
  const at = draft.indexOf(base);
  if (at < 0 || draft.indexOf(base, at + 1) >= 0) return null;
  return splice(at, at + base.length, fresh);
}

// ---------------------------------------------------------------------------
// Continuing a recording in a new session: where each slice cuts the stream
// ---------------------------------------------------------------------------

/**
 * WHY A SLICE CANNOT START A STREAM BY ITSELF, measured 2026-09-29 in Chrome
 * 151 (MediaRecorder, audio/webm;codecs=opus, the fake-microphone device, 62 s
 * at a 5 s timeslice and 9.5 s at 1 s): every slice after the first begins ONE
 * BYTE INTO a SimpleBlock. The block's ID byte (0xA3) ends the previous slice,
 * its 2-byte size and 963-byte payload open the next, and a new Cluster starts
 * 965 bytes later. The first 146 bytes are the init segment (EBML header,
 * Segment, Info, Tracks); clusters come every 5,040 ms (1,020 ms at 1 s).
 *
 * So when the server closes a recording and the rest has to go into a new
 * session (`continues`), the held slices need a front: the init segment, a
 * Cluster header carrying the timecode of the cluster the cut fell in, and the
 * bytes of the element the cut went through. Decoded by Chrome from the same
 * recording, cut at slice 6 (expected 31.80 s of the 61.98 s):
 *   init + held slices                         31.74 s (the demuxer skips to
 *                                              the next Cluster: 60 ms lost)
 *   init + Cluster(25,200 ms) + 0xA3 + held    31.80 s, every block kept
 * This tracker reads each slice as it is recorded and says, for the slice
 * about to come, exactly that front (`lead`). It is kept with the slice, so a
 * reopened tab can build the continuation long after the earlier slices left
 * this device.
 *
 * Safari records audio/mp4, whose fragments were not measured (no Safari run
 * exists); for any container but WebM there is no lead, and the rest of a
 * closed recording is held on this device and offered as "Upload the rest".
 */
const EBML_HEADER_ID = 0x1a45dfa3;
const SEGMENT_ID = 0x18538067;
const CLUSTER_ID = 0x1f43b675;
const TIMECODE_ID = 0xe7;
/** Elements that live directly under Segment; seen inside an unknown-size Cluster, they end it. */
const LEVEL1_IDS = new Set([
  0x114d9b74, // SeekHead
  0x1549a966, // Info
  0x1654ae6b, // Tracks
  CLUSTER_ID,
  0x1c53bb6b, // Cues
  0x1043a770, // Chapters
  0x1254c367, // Tags
  0x1941a469, // Attachments
]);
/** More init than this is not a recorder's init segment. */
const MAX_INIT_BYTES = 256 * 1024;
/** An element straddling a cut is at most one block; a lead bigger than this is not trusted. */
const MAX_LEAD_BYTES = 256 * 1024;

function concatBytes(parts: Uint8Array[]): Uint8Array {
  const total = parts.reduce((n, p) => n + p.byteLength, 0);
  const out = new Uint8Array(total);
  let at = 0;
  for (const p of parts) {
    out.set(p, at);
    at += p.byteLength;
  }
  return out;
}

/** A Cluster of unknown size (live mode, as Chrome writes them) with its Timecode. */
export function clusterHeader(timecodeMs: number): Uint8Array {
  const tc: number[] = [];
  let v = Math.max(0, Math.floor(timecodeMs));
  do {
    tc.unshift(v & 0xff);
    v = Math.floor(v / 256);
  } while (v > 0);
  return new Uint8Array([
    0x1f, 0x43, 0xb6, 0x75, 0x01, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff,
    0xe7, 0x80 | tc.length, ...tc,
  ]);
}

/** An EBML element header: [id, size or null for unknown, header length], or null if incomplete. */
function readHeader(h: number[]): { id: number; size: number | null } | null {
  if (h.length === 0) return null;
  const first = h[0]!;
  let idLen = 1;
  while (idLen <= 4 && !(first & (0x80 >> (idLen - 1)))) idLen += 1;
  if (idLen > 4) throw new Error('bad id');
  if (h.length < idLen + 1) return null;
  let id = 0;
  for (let i = 0; i < idLen; i += 1) id = id * 256 + h[i]!;
  const s0 = h[idLen]!;
  let sizeLen = 1;
  while (sizeLen <= 8 && !(s0 & (0x80 >> (sizeLen - 1)))) sizeLen += 1;
  if (sizeLen > 8) throw new Error('bad size');
  if (h.length < idLen + sizeLen) return null;
  let size = s0 & (0xff >> sizeLen);
  let allOnes = size === 0xff >> sizeLen;
  for (let i = 1; i < sizeLen; i += 1) {
    const b = h[idLen + i]!;
    if (b !== 0xff) allOnes = false;
    size = size * 256 + b;
  }
  return { id, size: allOnes ? null : size };
}

/** What the container tracking needs from a tracker; tests supply their own. */
export interface ContainerCuts {
  /** The bytes every new stream of this recording must start with, once known. */
  readonly init: Uint8Array | null;
  /** The front a stream starting at the next fed byte needs after `init`, or null if unknown. */
  lead(): Uint8Array | null;
  feed(bytes: Uint8Array): void;
}

export class WebmCutTracker implements ContainerCuts {
  init: Uint8Array | null = null;
  private broken = false;
  private seen: Uint8Array[] = [];
  private seenLen = 0;
  private pos = 0;
  private level: 'top' | 'segment' | 'cluster' = 'top';
  private hdr: number[] = [];
  private hdrStart = 0;
  /** Bytes of the element open right now, from its first header byte. */
  private unit: Uint8Array[] = [];
  private unitLen = 0;
  private skip = 0;
  private tcBytes: number[] | null = null;
  private tcLeft = 0;
  /** The header of a Cluster whose Timecode has not arrived yet. */
  private clusterHead: Uint8Array | null = null;
  private clusterTc: number | null = null;

  lead(): Uint8Array | null {
    if (this.broken || !this.init || this.level === 'top') return null;
    if (this.unitLen > MAX_LEAD_BYTES) return null;
    const partial = concatBytes(this.unit);
    if (this.level === 'cluster') {
      if (this.clusterTc === null) return this.clusterHead ? concatBytes([this.clusterHead, partial]) : null;
      return concatBytes([clusterHeader(this.clusterTc), partial]);
    }
    return partial;
  }

  feed(bytes: Uint8Array): void {
    if (this.broken) return;
    if (!this.init) {
      this.seen.push(bytes.slice());
      this.seenLen += bytes.byteLength;
      if (this.seenLen > MAX_INIT_BYTES * 4) this.broken = true;
    }
    try {
      this.parse(bytes);
    } catch {
      this.broken = true;
    }
  }

  private take(chunk: Uint8Array): void {
    if (this.unitLen <= MAX_LEAD_BYTES) this.unit.push(chunk.slice());
    this.unitLen += chunk.byteLength;
  }

  private endUnit(): void {
    this.unit = [];
    this.unitLen = 0;
  }

  private parse(b: Uint8Array): void {
    let i = 0;
    while (i < b.length && !this.broken) {
      if (this.skip > 0) {
        const n = Math.min(this.skip, b.length - i);
        this.take(b.subarray(i, i + n));
        this.skip -= n;
        i += n;
        this.pos += n;
        if (this.skip === 0) this.endUnit();
        continue;
      }
      if (this.tcBytes) {
        const byte = b[i]!;
        this.take(b.subarray(i, i + 1));
        i += 1;
        this.pos += 1;
        this.tcBytes.push(byte);
        this.tcLeft -= 1;
        if (this.tcLeft === 0) {
          this.clusterTc = this.tcBytes.reduce((v, x) => v * 256 + x, 0);
          this.clusterHead = null;
          this.tcBytes = null;
          this.endUnit();
        }
        continue;
      }
      if (this.hdr.length === 0) this.hdrStart = this.pos;
      this.hdr.push(b[i]!);
      this.take(b.subarray(i, i + 1));
      i += 1;
      this.pos += 1;
      if (this.hdr.length > 12) {
        this.broken = true;
        return;
      }
      const head = readHeader(this.hdr);
      if (!head) continue;
      this.hdr = [];
      this.element(head.id, head.size);
    }
  }

  private element(id: number, size: number | null): void {
    if (this.level === 'top') {
      if (id === SEGMENT_ID) {
        this.level = 'segment';
        this.endUnit();
        return;
      }
      if (id !== EBML_HEADER_ID || size === null) {
        this.broken = true;
        return;
      }
      this.skip = size;
      if (size === 0) this.endUnit();
      return;
    }
    if (id === CLUSTER_ID) {
      if (!this.init) {
        const all = concatBytes(this.seen);
        this.init = all.slice(0, this.hdrStart);
        this.seen = [];
        this.seenLen = 0;
      }
      this.level = 'cluster';
      this.clusterHead = concatBytes(this.unit);
      this.clusterTc = null;
      this.endUnit();
      return;
    }
    const clusterChild = this.level === 'cluster' && !LEVEL1_IDS.has(id);
    if (!clusterChild) this.level = 'segment';
    if (size === null) {
      this.broken = true; // only Segment and Cluster may be of unknown size here
      return;
    }
    if (clusterChild && id === TIMECODE_ID && size > 0 && size <= 8) {
      this.tcBytes = [];
      this.tcLeft = size;
      return;
    }
    this.skip = size;
    if (size === 0) this.endUnit();
  }
}

/** The tracker for a recorder's container, or null where continuing is not supported. */
export function cutsFor(mimeType: string): ContainerCuts | null {
  return /^audio\/webm/i.test(mimeType) || /^video\/webm/i.test(mimeType) ? new WebmCutTracker() : null;
}
