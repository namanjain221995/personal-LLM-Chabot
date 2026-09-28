/**
 * "Your recordings" — the person's own stored dictations (2026-09-29).
 *
 * WHY THIS EXISTS. Every press of the chat microphone now stores the recording
 * on the server, with no length limit, and VOICE_RETENTION_DAYS defaults to 0,
 * which keeps it until someone deletes it. The orchestrator already had list,
 * download and delete routes, but nothing in the UI called them, so a person
 * could not find or delete what was stored about them (voice security review,
 * item 6). This module is the page's whole conversation with those routes.
 *
 * THE ROUTES (the recording-session contract, confirmed against
 * orchestrator/app/audio_api.py and dictation.py on wip/backend-long-form):
 *   GET    /api/audio/sessions?limit=&before=  newest first, cancelled excluded
 *          -> {sessions: [{session_id, created_at, status, outcome, audio_ms,
 *              bytes, mime_type, delete_after, preview}], next_before}
 *   GET    /api/audio/sessions/{id}            the session state; `text` is set
 *                                              only when status is done
 *   GET    /api/audio/sessions/{id}/audio      the stored file (Range works)
 *   DELETE /api/audio/sessions/{id}            204; 404 when already gone
 *
 * Every non-2xx body is flat, {detail, reason}; lib/voice.callSessionApi reads
 * it (and a nested FastAPI body, and a proxy that fell over) so this module
 * does not parse refusals a second way.
 */

import {
  VOICE_MESSAGES,
  callSessionApi,
  describeGaps,
  formatElapsed,
  isSessionId,
  parseSessionState,
  type Reply,
} from './voice';

export const RECORDINGS_URL = '/api/audio/sessions';

/**
 * Twenty rows a page. The list itself is small (no audio, a 120-character
 * preview each), but the owner reads it on a phone, where twenty rows with a
 * player each is already several screens.
 */
export const RECORDINGS_PAGE_SIZE = 20;

export type RecordingStatus = 'recording' | 'finishing' | 'done' | 'failed';

export interface Recording {
  id: string;
  /** ISO time the recording started (the server's clock). */
  createdAt: string;
  status: RecordingStatus;
  outcome: string | null;
  /** The server's own measurement of the decoded audio; 0 before decoding. */
  audioMs: number;
  bytes: number;
  mimeType: string | null;
  /** ISO time retention will delete it, or null: kept until deleted. */
  deleteAfter: string | null;
  /** First 120 characters of the transcript, only once status is done. */
  preview: string | null;
}

export interface RecordingPage {
  recordings: Recording[];
  /** Pass back as `before` for the next (older) page; null on the last page. */
  nextBefore: string | null;
}

const STATUSES: ReadonlySet<string> = new Set(['recording', 'finishing', 'done', 'failed']);

const num = (v: unknown): number => (typeof v === 'number' && Number.isFinite(v) ? v : 0);
const str = (v: unknown): string | null => (typeof v === 'string' ? v : null);

/**
 * The list URL. `before` goes through URLSearchParams on purpose: the
 * server's cursor is Python isoformat, `2026-09-29T04:05:06.123456+00:00`,
 * and a bare `+` in a query string is decoded as a space, which the server
 * then refuses as "before must be an ISO 8601 time" — the second page would
 * never load.
 */
export function recordingsListUrl(before: string | null, limit = RECORDINGS_PAGE_SIZE): string {
  const q = new URLSearchParams({ limit: String(limit) });
  if (before) q.set('before', before);
  return `${RECORDINGS_URL}?${q.toString()}`;
}

export function recordingAudioUrl(id: string): string {
  return `${RECORDINGS_URL}/${id}/audio`;
}

/** Parse one list page, dropping rows that are not a recording this page can act on. */
export function parseRecordingPage(body: unknown): RecordingPage | null {
  if (typeof body !== 'object' || body === null) return null;
  const b = body as Record<string, unknown>;
  if (!Array.isArray(b.sessions)) return null;
  const recordings = b.sessions.flatMap((raw): Recording[] => {
    if (typeof raw !== 'object' || raw === null) return [];
    const r = raw as Record<string, unknown>;
    const status = str(r.status);
    const createdAt = str(r.created_at);
    // A row without a valid id cannot be played or deleted, and one in a
    // state the list promises to leave out (cancelled) is already gone.
    if (!isSessionId(r.session_id) || !status || !STATUSES.has(status) || !createdAt) return [];
    const preview = str(r.preview);
    return [
      {
        id: r.session_id,
        createdAt,
        status: status as RecordingStatus,
        outcome: str(r.outcome),
        audioMs: num(r.audio_ms),
        bytes: num(r.bytes),
        mimeType: str(r.mime_type),
        deleteAfter: str(r.delete_after),
        preview: preview && preview.trim() ? preview : null,
      },
    ];
  });
  return { recordings, nextBefore: str(b.next_before) };
}

/** Append an older page without repeating a row the list already shows. */
export function appendPage(current: Recording[], older: Recording[]): Recording[] {
  const seen = new Set(current.map((r) => r.id));
  return [...current, ...older.filter((r) => !seen.has(r.id))];
}

export const STATUS_LABEL: Record<RecordingStatus, string> = {
  recording: 'Recording',
  finishing: 'Finishing',
  done: 'Done',
  failed: 'Failed',
};

/**
 * True when retention has already removed this recording's audio and
 * transcript. The list keeps such a row as a tombstone and says nothing more
 * (no `kept` flag), but `delete_after` is exactly the moment the hourly sweep
 * may remove it, so a past `delete_after` means the player would only get a
 * 410. The row stays deletable.
 */
export function removedByRetention(rec: Recording, now: number = Date.now()): boolean {
  if (!rec.deleteAfter) return false;
  const at = Date.parse(rec.deleteAfter);
  return Number.isFinite(at) && at <= now;
}

/** Length as the recorder shows it (h:mm:ss past an hour), or null before decoding. */
export function recordingLength(rec: Recording): string | null {
  return rec.audioMs > 0 ? formatElapsed(rec.audioMs) : null;
}

/**
 * What to show where a transcript would be, when there is no text. The
 * wording follows the recorder's messages, minus the advice that only makes
 * sense while the person is still at the microphone.
 */
export function noTextNote(status: RecordingStatus, outcome: string | null): string {
  if (status === 'recording') return 'Still recording. The transcript appears once it ends.';
  if (status === 'finishing') return 'Being transcribed now.';
  switch (outcome) {
    case 'no_speech':
      return 'No speech was detected in this recording.';
    case 'no_words':
      return 'Sound was detected, but no words could be made out.';
    case 'engine_unavailable':
      return 'Not transcribed: the speech engine was unavailable. The audio is kept.';
    case 'undecodable':
      return "The server couldn't read the audio in this recording. The file is kept exactly as it arrived.";
    default:
      return status === 'failed'
        ? 'Transcription failed. The audio is kept.'
        : 'This recording has no transcript text.';
  }
}

/* -------------------------------------------------------------- requests */

type Action = 'list' | 'transcript' | 'delete';

const UNREACHABLE = "The server couldn't be reached. Check your connection and try again.";

/**
 * One sentence per refusal and action. A delete that fails always says so in
 * its first two words, "Not deleted.": the person must never read a failure
 * as a deletion.
 */
export function refusalMessage(reply: Exclude<Reply, { kind: 'ok' }>, action: Action): string {
  const lead =
    action === 'delete'
      ? 'Not deleted.'
      : action === 'transcript'
        ? "The transcript couldn't be opened."
        : "Your recordings couldn't be loaded.";
  const say = (sentence: string) => `${lead} ${sentence}`;
  if (reply.kind === 'aborted') return say('The request was cancelled.');
  if (reply.kind === 'unreachable') return say(UNREACHABLE);
  if (reply.status === 401) return say('You were signed out. Sign in and try again.');
  if (reply.status === 403 && reply.reason === 'voice_off') {
    // The server gates the list, the file and DELETE on the voice-input
    // feature as well as on ownership (review item 7), so a person whose
    // voice input was switched off meets this on their own recordings.
    return say('Voice input is turned off for your account. Ask an administrator.');
  }
  if (reply.status === 404 && action === 'list') {
    return say("Recordings aren't available on this server right now.");
  }
  if (reply.status === 404) return VOICE_MESSAGES.notFound;
  return say(
    reply.detail
      ? `The server answered with error ${reply.status}: "${reply.detail}" Try again.`
      : `The server answered with error ${reply.status}. Try again.`,
  );
}

export type ListResult =
  | { kind: 'ok'; page: RecordingPage }
  | { kind: 'failed'; message: string; signedOut: boolean };

export async function loadRecordings(
  fetchImpl: typeof fetch,
  before: string | null,
  signal?: AbortSignal,
): Promise<ListResult> {
  const reply = await callSessionApi(fetchImpl, recordingsListUrl(before), { method: 'GET', signal });
  if (reply.kind === 'ok') {
    const page = parseRecordingPage(reply.body);
    if (page) return { kind: 'ok', page };
    return {
      kind: 'failed',
      message: "Your recordings couldn't be loaded. The server's answer couldn't be read. Try again.",
      signedOut: false,
    };
  }
  return {
    kind: 'failed',
    message: refusalMessage(reply, 'list'),
    signedOut: reply.kind === 'refused' && reply.status === 401,
  };
}

export type TranscriptResult =
  | { kind: 'text'; text: string; note: string | null }
  | { kind: 'none'; message: string }
  | { kind: 'failed'; message: string };

/**
 * The full transcript, by a PLAIN read of the session state: no wait_s, so
 * the long-poll the recorder uses never holds this request open.
 */
export async function loadTranscript(
  fetchImpl: typeof fetch,
  id: string,
  signal?: AbortSignal,
): Promise<TranscriptResult> {
  const reply = await callSessionApi(fetchImpl, `${RECORDINGS_URL}/${id}`, { method: 'GET', signal });
  if (reply.kind !== 'ok') return { kind: 'failed', message: refusalMessage(reply, 'transcript') };
  const state = parseSessionState(reply.body);
  if (!state || state.status === 'cancelled') {
    return state
      ? { kind: 'failed', message: VOICE_MESSAGES.notFound }
      : {
          kind: 'failed',
          message: "The transcript couldn't be opened. The server's answer couldn't be read. Try again.",
        };
  }
  const text = (state.text ?? '').trim();
  if (state.status === 'done' && text) {
    // Gaps the engine could not hear are worth saying next to the text;
    // stretches dropped as noise are not, as in the recorder.
    const unheard = state.gaps.filter((g) => g.reason !== 'dropped_as_noise');
    const note = unheard.length
      ? `Part of this recording couldn't be transcribed (${describeGaps(unheard)}). The audio is kept.`
      : null;
    return { kind: 'text', text, note };
  }
  if (state.status === 'failed' && state.error?.detail) {
    return { kind: 'none', message: `${state.error.detail} The audio is kept.` };
  }
  return { kind: 'none', message: noTextNote(state.status, state.outcome) };
}

export type DeleteResult = { kind: 'deleted' } | { kind: 'not_deleted'; message: string };

/**
 * ONE attempt, reported as it happened. The recorder's deleteSession retries
 * for about 15 s behind a closed composer; here the person is watching the
 * button, so a failure comes back at once and they choose to try again.
 * 404 is a deletion: the recording is not on the server, which is what they
 * asked for (the contract's "treat as deleted").
 */
export async function deleteRecording(fetchImpl: typeof fetch, id: string): Promise<DeleteResult> {
  const reply = await callSessionApi(fetchImpl, `${RECORDINGS_URL}/${id}`, { method: 'DELETE' });
  if (reply.kind === 'ok') return { kind: 'deleted' };
  if (reply.kind === 'refused' && reply.status === 404) return { kind: 'deleted' };
  return { kind: 'not_deleted', message: refusalMessage(reply, 'delete') };
}
