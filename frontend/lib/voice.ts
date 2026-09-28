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
 * Put a re-transcribed recording where its first transcript went.
 *
 * "Retry" on a transcript with gaps re-reads the STORED audio and returns the
 * whole text again, gaps filled. The first version is already in the draft,
 * so appending would duplicate most of it. When the draft still contains the
 * first version verbatim it is replaced in place; when the person has edited
 * it since, their edits win and the new text is added after them instead.
 */
export function replaceTranscript(draft: string, previous: string, next: string): string {
  const old = previous.trim();
  const spoken = next.trim();
  if (old && spoken && draft.includes(old)) return draft.replace(old, spoken);
  return mergeTranscript(draft, next);
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
  low: "That recording wasn't clear enough to transcribe. Try again, closer to the microphone.",
  unknown: 'No words came back, and the server did not say why.',
  gatedLong:
    'The first 30 seconds of that recording sounded silent, so the rest of it was not transcribed. Start speaking right away, or attach long recordings as a file.',
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
  signedOutKept: (t: string) =>
    `You were signed out. Everything up to ${t} is saved on the server; the last few seconds are kept on this device and will upload when you sign in again.`,
  signedOutLost: (t: string) =>
    `You were signed out. Everything up to ${t} is saved on the server; the last few seconds will be lost if you close this tab.`,
  signedOutNothingPending: (t: string) =>
    `You were signed out. Everything up to ${t} is saved on the server.`,
  voiceOff: 'Voice input is turned off for your account. Ask an administrator.',
  voiceUnavailable: "Voice input isn't available on this server right now.",
  legacyHint: "Long recordings aren't enabled here, so this one stops at 10 minutes.",
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
  behind: (t: string, waitingOn: WaitingOn) =>
    waitingOn === 'chat'
      ? `Transcript ${t} behind — paused while someone is waiting for a chat answer`
      : waitingOn === 'engine'
        ? `Transcript ${t} behind — the speech engine is busy with other recordings`
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
} as const;

/** Past this, discarding a recording asks first (the contract's 60 s). */
export const DISCARD_CONFIRM_AFTER_MS = 60_000;
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
export type WaitingOn = 'none' | 'chat' | 'engine';
export type EndedBy = 'person' | 'recorder_error' | 'lost_parts' | 'page_hidden';

export interface SessionConfig {
  /** The timeslice the recorder MUST use. */
  partMs: number;
  partLimitBytes: number;
  /** Passed as audioBitsPerSecond when not null. */
  bitsPerSecond: number | null;
  idleCloseS: number;
  longPollMaxS: number;
}

export const DEFAULT_SESSION_CONFIG: SessionConfig = {
  partMs: 5000,
  partLimitBytes: 8 * 1024 * 1024,
  bitsPerSecond: null,
  idleCloseS: 600,
  longPollMaxS: 25,
};

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
    waitingOn: waiting === 'chat' || waiting === 'engine' ? waiting : 'none',
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

function idleWords(seconds: number): string {
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
  | { kind: 'unreachable'; status: number | null }
  | { kind: 'aborted' };

const PROXY_REASONS = new Set(['proxy_unreachable', 'proxy_timeout']);

export async function callSessionApi(
  fetchImpl: typeof fetch,
  url: string,
  init: RequestInit,
): Promise<Reply> {
  let response: Response;
  try {
    response = await fetchImpl(url, { ...init, cache: 'no-store' });
  } catch (err) {
    if (isAbort(err, init.signal ?? undefined)) return { kind: 'aborted' };
    return { kind: 'unreachable', status: null };
  }
  let raw = '';
  try {
    raw =
      typeof response.text === 'function'
        ? await response.text()
        : JSON.stringify(await (response as Response).json());
  } catch (err) {
    if (isAbort(err, init.signal ?? undefined)) return { kind: 'aborted' };
    return { kind: 'unreachable', status: response.status };
  }
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
  | { kind: 'legacy' }
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
    const reply = await callSessionApi(fetchImpl, SESSIONS_URL, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({
        client_key: input.clientKey,
        mime_type: input.mimeType,
        language: 'auto',
        part_ms: input.partMs ?? DEFAULT_SESSION_CONFIG.partMs,
      }),
      signal: deps.signal,
    });
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
    return { kind: 'error', error: { message: VOICE_MESSAGES.capacityFull, retryable: true } };
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

/** DELETE a session: the person's discard, or a session that never got audio. */
export async function deleteSession(
  sessionId: string,
  deps: SessionDeps = {},
): Promise<boolean> {
  const fetchImpl = deps.fetchImpl ?? fetch;
  const wake = { current: null as (() => void) | null };
  for (let attempt = 0; attempt < 4; attempt += 1) {
    const reply = await callSessionApi(fetchImpl, `${SESSIONS_URL}/${sessionId}`, {
      method: 'DELETE',
    });
    if (reply.kind === 'ok') return true;
    if (reply.kind === 'refused' && reply.status === 404) return true;
    if (reply.kind === 'refused' && !partRetryable(reply)) return false;
    if (reply.kind === 'aborted') return false;
    await doSleep(backoffMs(attempt, deps.random ?? Math.random), wake);
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
  const reply = await callSessionApi(deps.fetchImpl ?? fetch, `${SESSIONS_URL}/${sessionId}/finish`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ last_part: null, duration_ms: 0, ended_by: 'person' }),
  });
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
    }
  | { kind: 'end_other'; sessionId: string; message: string; label: string }
  | { kind: 'insert'; text: string; message: string; label: string };

export type SessionResult =
  | { kind: 'text'; text: string; notices: string[]; offer: VoiceOffer | null; sessionId: string }
  | { kind: 'error'; error: VoiceError; offer: VoiceOffer | null }
  | { kind: 'withdrawn' };

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
  const retry = (message: string): VoiceOffer => ({
    kind: 'retranscribe',
    sessionId: state.sessionId,
    scope: 'gaps',
    replaces: context.replaces ?? null,
    message,
    label: VOICE_MESSAGES.retry,
  });
  const failure = (message: string, retryable: boolean, offer: VoiceOffer | null = null): SessionResult => ({
    kind: 'error',
    error: { message: [message, ...notices].join(' '), retryable },
    offer,
  });

  if (state.outcome === 'engine_unavailable') {
    const message = VOICE_MESSAGES.engineUnavailable(t);
    return { kind: 'error', error: { message, retryable: true }, offer: retry(message) };
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
  const unheard = state.gaps.filter((g) => g.reason !== 'dropped_as_noise');
  const offer =
    unheard.length > 0
      ? retry(VOICE_MESSAGES.gaps(unheard.length, describeGaps(unheard)))
      : null;
  if (offer && offer.kind === 'retranscribe') offer.replaces = text;
  return { kind: 'text', text, notices, offer, sessionId: state.sessionId };
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
}

export interface StoredSlice {
  idx: number;
  endMs: number;
  bytes: Uint8Array;
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
  putSlice(sessionId: string, idx: number, endMs: number, data: Blob): Promise<void>;
  readSlices(sessionId: string, first: number, last: number): Promise<StoredSlice[]>;
  dropSlices(sessionId: string, first: number, last: number): Promise<void>;
  drop(sessionId: string): Promise<void>;
  /** Bytes of audio this store holds in the tab's memory. */
  heldBytes(): number;
}

/** The outbox without IndexedDB: memory, and only for what is unacknowledged. */
export function createMemoryOutbox(): OutboxStore {
  const records = new Map<string, OutboxRecord>();
  const slices = new Map<string, Map<number, { endMs: number; data: Blob }>>();
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
    async putSlice(id, idx, endMs, data) {
      let m = slices.get(id);
      if (!m) slices.set(id, (m = new Map()));
      const prev = m.get(idx);
      if (prev) held -= prev.data.size;
      m.set(idx, { endMs, data });
      held += data.size;
    },
    async readSlices(id, first, last) {
      const m = slices.get(id);
      const out: StoredSlice[] = [];
      for (let i = first; i <= last; i += 1) {
        const s = m?.get(i);
        if (s) out.push({ idx: i, endMs: s.endMs, bytes: await blobBytes(s.data) });
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

const OUTBOX_DB = 'techsara-voice-outbox';
const RECORD_STORE = 'records';
const SLICE_STORE = 'slices';

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
): Promise<OutboxStore> {
  if (!factory) return createMemoryOutbox();
  let db: IDBDatabase;
  try {
    db = await new Promise<IDBDatabase>((resolve, reject) => {
      const req = factory.open(OUTBOX_DB, 1);
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
  const range = (id: string, first: number, last: number) =>
    IDBKeyRange.bound([id, first], [id, last]);
  return {
    persistent: true,
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
    async putSlice(id, idx, endMs, data) {
      const bytes = await blobBytes(data);
      const buffer = bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength);
      const tx = db.transaction(SLICE_STORE, 'readwrite');
      tx.objectStore(SLICE_STORE).put({ sessionId: id, idx, endMs, bytes: buffer });
      await idbDone(tx);
    },
    async readSlices(id, first, last) {
      const tx = db.transaction(SLICE_STORE, 'readonly');
      const rows = await idbRequest(
        tx.objectStore(SLICE_STORE).getAll(range(id, first, last)) as IDBRequest<
          Array<{ idx: number; endMs: number; bytes: ArrayBuffer }>
        >,
      );
      return rows.map((r) => ({ idx: r.idx, endMs: r.endMs, bytes: new Uint8Array(r.bytes) }));
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
  backlogMs: number;
  waitingOn: WaitingOn;
  /** Slices recorded but not yet acknowledged by the server. */
  pendingParts: number;
  /** Recording time not yet acknowledged. */
  pendingMs: number;
  offline: boolean;
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
  | { kind: 'unsupported_format'; detail: string | null }
  | { kind: 'lost_parts' }
  | { kind: 'generic' };

interface QueuedSlice {
  idx: number;
  size: number;
  endMs: number;
  /** Kept only when the store could not take it; null once it is stored. */
  data: Blob | null;
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
 *
 * NEVER THE WHOLE RECORDING. The tab holds only what the server has not yet
 * acknowledged — normally one 5 s slice of about 80 KB — and with IndexedDB
 * not even that. The old recorder held every chunk of the recording in one
 * array until Stop and then posted it as one body: 9,652,200 bytes for ten
 * minutes of 128.7 kb/s slices, measured 2026-09-29 by driving the origin/dev
 * hook in a test harness. tests/voice-session-recorder.test.tsx holds an hour
 * of the same slices to at most two held at once (160,874 bytes).
 */
export class VoiceSession {
  readonly sessionId: string;
  readonly mimeType: string;
  config: SessionConfig;

  private readonly fetchImpl: typeof fetch;
  private readonly store: OutboxStore;
  private readonly now: () => number;
  private readonly random: () => number;
  private readonly sha: (bytes: Uint8Array) => Promise<string>;
  private readonly onProgress: (p: SessionProgress) => void;
  private readonly onInterrupt: (i: SessionInterrupt) => void;

  private readonly abort = new AbortController();
  private readonly wake = { current: null as (() => void) | null };
  private queue: QueuedSlice[] = [];
  /** Slices handed to the store and not yet written. */
  private readonly writing = new Set<QueuedSlice>();
  private formed: { seq: number; first: number; last: number } | null = null;
  /** The slices of the formed part, in order; their bytes are in the store. */
  private formedSlices: QueuedSlice[] | null = null;
  private formedBody: { bytes: Uint8Array; sha: string; endMs: number } | null = null;
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
  /** The outbox record is gone (finished or discarded); never write it again. */
  private outboxDropped = false;
  private keepAlive: ReturnType<typeof setInterval> | null = null;

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
    deps: SessionDeps & { store: OutboxStore },
    handlers: {
      onProgress?: (p: SessionProgress) => void;
      onInterrupt?: (i: SessionInterrupt) => void;
    } = {},
  ) {
    this.sessionId = init.sessionId;
    this.mimeType = init.mimeType;
    this.config = init.config;
    // Bound now, not looked up per call: a session finishing in the
    // background after its component unmounted keeps talking to the fetch it
    // started with. `bind` also keeps browsers from calling fetch with this
    // object as its receiver, which they refuse as an illegal invocation.
    this.fetchImpl = deps.fetchImpl ?? fetch.bind(globalThis);
    this.store = deps.store;
    this.now = deps.now ?? (() => Date.now());
    this.random = deps.random ?? Math.random;
    this.sha = deps.sha256 ?? sha256Hex;
    this.onProgress = handlers.onProgress ?? (() => undefined);
    this.onInterrupt = handlers.onInterrupt ?? (() => undefined);
    this.progress = {
      preview: '',
      tentative: '',
      audioMs: 0,
      backlogMs: 0,
      waitingOn: 'none',
      pendingParts: 0,
      pendingMs: 0,
      offline: false,
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
   */
  static async adopt(
    record: OutboxRecord,
    deps: SessionDeps & { store: OutboxStore },
    handlers: { onProgress?: (p: SessionProgress) => void } = {},
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
    session.lastTouch = 0;
    // What still has to go is every stored slice from the first one not yet
    // acknowledged. Their bytes stay on disk; only the sizes are kept here.
    const from = session.firstUnacked;
    const stored =
      record.nextSlice > from
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
    if (record.formed) {
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
    // Signed out mid-recording: nothing more can upload, but the last few
    // seconds are still kept for when the person signs in again. Every other
    // interrupt means the server will not take them.
    if (this.interrupt && this.interrupt.kind !== 'signed_out') return;
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
      try {
        await this.store.putSlice(this.sessionId, slice.idx, slice.endMs, data);
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

  /** Stop waiting and try now — the browser just said it is back online. */
  nudge(): void {
    this.wake.current?.();
  }

  stats(): SessionStats {
    return { ...this.stat, heldBytes: this.held() };
  }

  /**
   * The recorder has stopped. Upload whatever is left, finish, and wait for
   * the transcript. Resolves with what the person should see.
   */
  async end(
    endedBy: EndedBy,
    durationMs: number,
    context: { notices?: string[]; peakLevel?: number | null } = {},
  ): Promise<SessionResult> {
    try {
      return await this.endInner(endedBy, durationMs, context);
    } finally {
      // Whatever the record still holds now (a sign-out's unsent seconds, a
      // finish refused for a sign-out) is left for adoption, which needs it
      // to go stale first.
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
    await this.writeChain;
    await this.persist(true);
    this.kick();
    while (this.pumping) await this.pumping;
    if (this.discarded) return { kind: 'withdrawn' };
    const notices = [...(context.notices ?? [])];
    if (this.interrupt) return this.afterInterrupt(this.interrupt, notices, context.peakLevel);
    const lastPart = this.nextSeq - 1;
    if (lastPart < 0) {
      // The recorder produced nothing at all. There is no audio to keep.
      await deleteSession(this.sessionId, { fetchImpl: this.fetchImpl, random: this.random });
      await this.dropOutbox();
      return { kind: 'error', error: { message: VOICE_MESSAGES.tooShort, retryable: true }, offer: null };
    }
    return this.finishAndWait(lastPart, this.endedBy ?? endedBy, notices, context.peakLevel ?? null);
  }

  /** The person's discard: stop everything and delete the recording on the server. */
  async discard(): Promise<void> {
    this.discarded = true;
    this.abort.abort();
    this.nudge();
    await deleteSession(this.sessionId, { fetchImpl: this.fetchImpl, random: this.random });
    await this.writeChain.catch(() => undefined);
    await this.dropOutbox();
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
    };
    try {
      await this.store.saveRecord(record);
    } catch {
      /* the outbox is a safety net; the upload does not depend on it */
    }
  }

  private emit(): void {
    const pendingSlices =
      this.queue.length + (this.formed ? this.formed.last - this.formed.first + 1 : 0);
    this.progress = {
      ...this.progress,
      pendingParts: pendingSlices,
      pendingMs: Math.max(0, this.durationMs - this.ackedMs),
      storageTrouble:
        this.storageTroubleSince !== null &&
        this.now() - this.storageTroubleSince > STORAGE_TROUBLE_AFTER_MS,
    };
    this.onProgress({ ...this.progress });
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
      if (!this.formed) {
        if (this.queue.length === 0) return;
        // Coalesce whatever has queued up, to the server's part limit.
        const limit = this.config.partLimitBytes;
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
        `${SESSIONS_URL}/${this.sessionId}/parts/${seq}?cursor=${this.cursor}`,
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
      );
      if (reply.kind === 'aborted' || this.discarded) return;

      if (reply.kind === 'ok') {
        attempt = 0;
        const state = parseSessionState(reply.body);
        if (state) this.apply(state);
        await this.acknowledge();
        this.progress = { ...this.progress, offline: false, lastAckAt: this.now() };
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
        this.progress = { ...this.progress, offline: true };
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
    const total = parts.reduce((n, p) => n + p.byteLength, 0);
    const bytes = new Uint8Array(total);
    let offset = 0;
    for (const p of parts) {
      bytes.set(p, offset);
      offset += p.byteLength;
    }
    return { bytes, sha: await this.sha(bytes), endMs };
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
    this.splitLimit = null;
    this.nextSeq += 1;
    this.firstUnacked = f.last + 1;
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
    const pending = this.queue.length + (this.formed ? 1 : 0);
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
        await this.dropOutbox();
        const message =
          interrupt.endedBy === 'idle'
            ? VOICE_MESSAGES.closedIdle(idleWords(this.config.idleCloseS))
            : VOICE_MESSAGES.closedElsewhere;
        // The server finishes it and keeps it; if its words arrive, they are
        // still this person's draft.
        return this.waitForResult([message, ...notices], peakLevel ?? null, message);
      }
      case 'storage_full': {
        await this.dropOutbox();
        const message = VOICE_MESSAGES.storageFullMid(this.t());
        return this.waitForResult([message, ...notices], peakLevel ?? null, message);
      }
      case 'lost_parts': {
        const t = formatElapsed(this.ackedMs);
        await this.dropOutbox();
        return this.finishAndWait(null, 'lost_parts', [VOICE_MESSAGES.lostParts(t), ...notices], peakLevel ?? null);
      }
      default: {
        await this.dropOutbox();
        return this.finishAndWait(null, 'lost_parts', [VOICE_MESSAGES.generic(this.t()), ...notices], peakLevel ?? null);
      }
    }
  }

  private async finishAndWait(
    lastPart: number | null,
    endedBy: EndedBy,
    notices: string[],
    peakLevel: number | null,
  ): Promise<SessionResult> {
    let attempt = 0;
    let last = lastPart;
    let by = endedBy;
    for (;;) {
      if (this.discarded) return { kind: 'withdrawn' };
      const reply = await callSessionApi(this.fetchImpl, `${SESSIONS_URL}/${this.sessionId}/finish`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({
          last_part: last,
          duration_ms: Math.round(this.durationMs),
          ended_by: by,
        }),
        signal: this.abort.signal,
      });
      if (reply.kind === 'aborted' || this.discarded) return { kind: 'withdrawn' };
      if (reply.kind === 'ok') {
        const state = parseSessionState(reply.body);
        if (state) this.apply(state);
        // Finish is accepted: nothing on this device is needed any more.
        await this.dropOutbox();
        this.emit();
        return this.waitForResult(notices, peakLevel, null);
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
        if (!partRetryable(reply)) return this.refusalAfterStop(reply);
      }
      this.progress = { ...this.progress, offline: reply.kind === 'unreachable' };
      this.emit();
      await this.persist();
      await this.sleep(backoffMs(attempt, this.random));
      attempt += 1;
    }
  }

  private refusalAfterStop(reply: Extract<Reply, { kind: 'refused' }>): SessionResult {
    const fail = (message: string, retryable: boolean): SessionResult => ({
      kind: 'error',
      error: { message, retryable },
      offer: null,
    });
    if (reply.status === 401) {
      // The record stays: once signed in again, adoption finishes it.
      return fail(VOICE_MESSAGES.signedOutNothingPending(this.t()), false);
    }
    // Every other refusal is final, and nothing on this device can change it.
    void this.dropOutbox();
    if (reply.status === 403) return fail(reply.detail || VOICE_MESSAGES.voiceOff, false);
    if (reply.status === 404) return fail(VOICE_MESSAGES.notFound, false);
    return fail(VOICE_MESSAGES.generic(this.t()), true);
  }

  /**
   * Long-poll until the session is done or failed. Every request returns
   * within 25 s, under Cloudflare's 125 s first-byte limit, so there is no
   * heartbeat to keep and no ceiling on how long the whole wait may be.
   */
  private async waitForResult(
    notices: string[],
    peakLevel: number | null,
    failureMessage: string | null,
  ): Promise<SessionResult> {
    const settled = await pollSession(
      this.sessionId,
      { cursor: this.cursor, rev: this.rev, state: this.lastState },
      {
        fetchImpl: this.fetchImpl,
        random: this.random,
        signal: this.abort.signal,
        wake: this.wake,
        waitS: this.config.longPollMaxS,
        onState: (state) => {
          this.apply(state);
          this.emit();
        },
        onOffline: (offline) => {
          this.progress = { ...this.progress, offline };
          this.emit();
        },
      },
    );
    if (this.discarded || settled.kind === 'aborted') return { kind: 'withdrawn' };
    if (settled.kind === 'refused') {
      if (failureMessage) return { kind: 'error', error: { message: failureMessage, retryable: false }, offer: null };
      return this.refusalAfterStop(settled.reply);
    }
    return describeOutcome(settled.state, {
      lowSeen: this.lowSeen,
      notices,
      peakLevel,
      durationMs: this.durationMs,
    });
  }
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
  let attempt = 0;
  for (;;) {
    if (opts.signal?.aborted) return { kind: 'aborted' };
    const query = new URLSearchParams({
      cursor: String(cursor),
      since_rev: String(rev),
      wait_s: String(Math.min(25, Math.max(0, opts.waitS ?? 25))),
    });
    const reply = await callSessionApi(opts.fetchImpl, `${SESSIONS_URL}/${sessionId}?${query}`, {
      method: 'GET',
      signal: opts.signal,
    });
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
  const reply = await callSessionApi(fetchImpl, `${SESSIONS_URL}/${offer.sessionId}/retranscribe`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ scope: offer.scope }),
    signal: deps.signal,
  });
  const fail = (message: string, retryable: boolean, again: VoiceOffer | null = null): SessionResult => ({
    kind: 'error',
    error: { message, retryable },
    offer: again,
  });
  if (reply.kind === 'aborted') return { kind: 'withdrawn' };
  if (reply.kind === 'unreachable') return fail(VOICE_MESSAGES.retryUnreachable, true, offer);
  if (reply.kind === 'refused') {
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
