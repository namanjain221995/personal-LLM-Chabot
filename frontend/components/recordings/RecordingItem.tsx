'use client';

/**
 * One stored recording: when, how long, where it stands, what was said, and
 * the three things a person can do with it — play, copy the transcript,
 * delete.
 *
 * THE PLAYER NEVER PRELOADS. `preload="none"` because an hour of dictation is
 * about 58 MB at the 128.7 kb/s Chrome records at (measured 2026-09-28): a
 * page of twenty long recordings would otherwise start pulling over a GB onto
 * the owner's phone just by being opened. Nothing is fetched until Play, and
 * the proxy forwards Range, so seeking fetches only what it needs.
 *
 * A PLAYER THAT FAILS SAYS WHY. <audio> reports only that it failed, and a
 * finished recording may now be kept on the voice archive server
 * (2026-09-30), which can be unreachable for a while with nothing lost. One
 * one-byte request (lib/recordings diagnosePlayback) tells a format this
 * browser cannot play from an archive that is not answering, from audio that
 * is gone.
 *
 * THE TRANSCRIPT IS FETCHED WHEN ASKED FOR, AND COPIED BY A SECOND PRESS. The
 * list carries a 120-character preview; the full text is one plain read of
 * the session state. Copy is its own button on the loaded text rather than a
 * "fetch then copy" in one press: Safari refuses a clipboard write that
 * follows an await on the network, because the press no longer counts as the
 * person's gesture.
 */

import { useCallback, useEffect, useId, useRef, useState } from 'react';
import { ConfirmDialog } from '@/components/ConfirmDialog';
import { CopyButton } from '@/components/CopyButton';
import { IconDownload, IconFileText, IconTrash } from '@/components/icons';
import { formatBytes, formatDay, formatWhen } from '@/lib/format';
import {
  PLAYBACK_MESSAGES,
  STATUS_LABEL,
  deleteRecording,
  diagnosePlayback,
  loadTranscript,
  noTextNote,
  recordingAudioUrl,
  recordingLength,
  removedByRetention,
  type Recording,
  type RecordingStatus,
  type TranscriptResult,
} from '@/lib/recordings';

/** The server's preview is the first 120 characters of the transcript. */
const PREVIEW_LIMIT = 120;

const STATUS_DOT: Record<RecordingStatus, { dot: string; text: string }> = {
  // Live: the one mark on the page that moves, and only without reduced motion.
  recording: { dot: 'bg-danger motion-safe:animate-pulse', text: 'text-ink' },
  finishing: { dot: 'bg-warn', text: 'text-muted' },
  done: { dot: 'bg-ok', text: 'text-muted' },
  failed: { dot: 'bg-danger', text: 'text-danger' },
};

/** The admin area's dot-and-word status mark: colour is never the only carrier. */
function StatusMark({ status }: { status: RecordingStatus }) {
  const style = STATUS_DOT[status];
  return (
    <span className={`inline-flex shrink-0 items-center gap-2 text-xs font-medium ${style.text}`}>
      <span aria-hidden className={`h-1.5 w-1.5 shrink-0 rounded-full ${style.dot}`} />
      <span className="sr-only">Status: </span>
      {STATUS_LABEL[status]}
    </span>
  );
}

export const actionClass =
  'inline-flex min-h-9 items-center gap-1.5 rounded-lg border border-border bg-surface px-3 py-1.5 text-sm text-muted transition-colors duration-ts hover:bg-surface-2 hover:text-ink focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:cursor-not-allowed disabled:opacity-60';

/** The sentence the delete confirmation asks, naming the recording it deletes. */
export function deleteConfirmBody(rec: Recording): string {
  const length = recordingLength(rec);
  const what = `${length ? `The ${length} recording` : 'The recording'} from ${formatWhen(rec.createdAt)}`;
  const live =
    rec.status === 'recording' || rec.status === 'finishing'
      ? ' It is still in progress: deleting it stops it and removes what was saved so far.'
      : '';
  return `${what} and its transcript will be deleted from the server.${live} This can't be undone.`;
}

function retentionNote(rec: Recording, removed: boolean): string | null {
  if (removed && rec.deleteAfter) {
    return `Its audio and transcript were deleted automatically on ${formatDay(rec.deleteAfter)}.`;
  }
  if (removed) return 'Its audio and transcript are no longer stored.';
  if (rec.deleteAfter) return `Kept until ${formatDay(rec.deleteAfter)}, then deleted automatically.`;
  // delete_after is null for a finished recording only when retention is off.
  if (rec.status === 'done' || rec.status === 'failed') return 'Kept until you delete it.';
  return null;
}

interface RecordingItemProps {
  rec: Recording;
  fetchFn: typeof fetch;
  /** Called once the server no longer holds the recording (204 or 404). */
  onDeleted: (rec: Recording) => void;
  now: number;
}

export function RecordingItem({ rec, fetchFn, onDeleted, now }: RecordingItemProps) {
  const baseId = useId();
  const titleId = `recording-${rec.id}`;
  const panelId = `${baseId}-transcript`;
  const deleteRef = useRef<HTMLButtonElement>(null);
  const transcriptAbort = useRef<AbortController | null>(null);

  const [open, setOpen] = useState(false);
  const [transcript, setTranscript] = useState<TranscriptResult | 'loading' | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const [playError, setPlayError] = useState<string | null>(null);
  const playbackProbe = useRef<AbortController | null>(null);

  useEffect(
    () => () => {
      transcriptAbort.current?.abort();
      playbackProbe.current?.abort();
    },
    [],
  );

  async function explainPlayError() {
    playbackProbe.current?.abort();
    const controller = new AbortController();
    playbackProbe.current = controller;
    // Something at once; the named cause once the server has answered.
    setPlayError(PLAYBACK_MESSAGES.unknown);
    const problem = await diagnosePlayback(fetchFn, rec.id, controller.signal);
    if (controller.signal.aborted) return;
    setPlayError(PLAYBACK_MESSAGES[problem]);
  }

  // Stable, because ConfirmDialog re-runs its focus effect whenever this
  // changes: an unstable one pulled focus back to Cancel on every re-render
  // of the list behind the dialog.
  const cancelDelete = useCallback(() => {
    setConfirming(false);
    deleteRef.current?.focus();
  }, []);

  const removed = removedByRetention(rec, now);
  const when = formatWhen(rec.createdAt);
  const length = recordingLength(rec);
  const note = retentionNote(rec, removed);
  const preview = rec.preview
    ? rec.preview.length >= PREVIEW_LIMIT
      ? `${rec.preview.trimEnd()}…`
      : rec.preview
    : null;

  async function fetchTranscript() {
    transcriptAbort.current?.abort();
    const controller = new AbortController();
    transcriptAbort.current = controller;
    setTranscript('loading');
    const result = await loadTranscript(fetchFn, rec.id, controller.signal);
    if (controller.signal.aborted) return;
    setTranscript(result);
  }

  function toggleTranscript() {
    const next = !open;
    setOpen(next);
    // Fetched on first open, and again after a failure; a loaded transcript
    // is kept while the row lives.
    if (next && (transcript === null || (transcript !== 'loading' && transcript.kind === 'failed'))) {
      void fetchTranscript();
    }
  }

  async function confirmDelete() {
    setConfirming(false);
    setDeleting(true);
    setDeleteError(null);
    const result = await deleteRecording(fetchFn, rec.id);
    if (result.kind === 'deleted') {
      onDeleted(rec);
      return;
    }
    setDeleting(false);
    setDeleteError(result.message);
  }

  // After a refused delete the button is enabled again only once this render
  // commits; focusing it here (not in the handler, where it is still
  // disabled) leaves a keyboard user on the button to try again.
  useEffect(() => {
    if (deleteError) deleteRef.current?.focus();
  }, [deleteError]);

  return (
    <li className="px-4 py-4 sm:px-5" aria-labelledby={titleId} aria-busy={deleting || undefined}>
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <h2
            id={titleId}
            tabIndex={-1}
            className="rounded text-[15px] font-medium leading-[22px] text-ink focus:outline-none focus-visible:ring-2 focus-visible:ring-accent"
          >
            <time dateTime={rec.createdAt}>{when}</time>
          </h2>
          <p className="mt-0.5 flex flex-wrap gap-x-3 text-xs tabular-nums text-muted">
            <span>
              <span className="sr-only">Length: </span>
              {length ?? 'Length not known yet'}
            </span>
            {rec.bytes > 0 && (
              <span>
                <span className="sr-only">Size: </span>
                {formatBytes(rec.bytes)}
              </span>
            )}
          </p>
        </div>
        <StatusMark status={rec.status} />
      </div>

      <p
        className={`mt-2 text-sm [overflow-wrap:anywhere] ${preview ? 'text-ink' : 'text-muted'}`}
        data-testid="recording-preview"
      >
        {removed
          ? 'The audio and transcript are no longer on the server.'
          : (preview ?? noTextNote(rec.status, rec.outcome))}
      </p>

      {!removed && (
        <audio
          controls
          preload="none"
          src={recordingAudioUrl(rec.id)}
          aria-label={`Play the recording from ${when}`}
          onError={() => void explainPlayError()}
          onPlay={() => {
            playbackProbe.current?.abort();
            setPlayError(null);
          }}
          className="mt-3 block h-10 w-full"
        />
      )}
      {playError && (
        <p role="alert" className="mt-2 text-xs text-danger">
          {playError}
        </p>
      )}

      <div className="mt-3 flex flex-wrap gap-2">
        {!removed && (
          <button
            type="button"
            onClick={toggleTranscript}
            aria-expanded={open}
            aria-controls={panelId}
            className={actionClass}
          >
            <IconFileText size={14} />
            {open ? 'Hide transcript' : 'Show transcript'}
          </button>
        )}
        {!removed && (
          <a href={recordingAudioUrl(rec.id)} download className={actionClass}>
            <IconDownload size={14} />
            Download
          </a>
        )}
        <button
          ref={deleteRef}
          type="button"
          onClick={() => {
            setDeleteError(null);
            setConfirming(true);
          }}
          disabled={deleting}
          aria-describedby={deleteError ? `${baseId}-delete-error` : undefined}
          // Pushed to the far end, away from the harmless actions: on a phone
          // it wraps onto its own line, on the right, not under a thumb that
          // was reaching for Download.
          className={`${actionClass} ml-auto hover:border-danger/50 hover:text-danger`}
        >
          <IconTrash size={14} />
          {deleting ? 'Deleting…' : 'Delete'}
          <span className="sr-only"> the recording from {when}</span>
        </button>
      </div>

      {deleteError && (
        <p
          id={`${baseId}-delete-error`}
          role="alert"
          className="mt-3 rounded-ts border border-danger/40 bg-danger/10 px-3 py-2 text-sm text-danger"
        >
          {deleteError}
        </p>
      )}

      {open && !removed && (
        <div
          id={panelId}
          className="mt-3 rounded-ts border border-border bg-bg p-3"
          aria-live="polite"
        >
          {transcript === 'loading' || transcript === null ? (
            <p className="text-sm text-muted">Loading the transcript…</p>
          ) : transcript.kind === 'text' ? (
            <>
              <div className="flex items-center justify-between gap-2">
                <h3 className="text-xs font-medium text-muted">Transcript</h3>
                <CopyButton text={transcript.text} label="Copy transcript" />
              </div>
              {transcript.note && <p className="mt-2 text-xs text-warn">{transcript.note}</p>}
              {/* An hour of speech is ~9,000 words: the box scrolls, and it is
                  a tab stop so the keyboard can scroll it too. */}
              <p
                tabIndex={0}
                aria-label={`Transcript of the recording from ${when}`}
                className="mt-2 max-h-80 overflow-y-auto whitespace-pre-wrap text-sm leading-relaxed text-ink [overflow-wrap:anywhere] focus:outline-none focus-visible:ring-2 focus-visible:ring-accent"
              >
                {transcript.text}
              </p>
            </>
          ) : transcript.kind === 'none' ? (
            <p className="text-sm text-muted">{transcript.message}</p>
          ) : (
            <div className="flex flex-wrap items-center justify-between gap-2">
              <p role="alert" className="text-sm text-danger">
                {transcript.message}
              </p>
              <button type="button" onClick={() => void fetchTranscript()} className={actionClass}>
                Try again
              </button>
            </div>
          )}
        </div>
      )}

      {note && <p className="mt-3 text-xs text-faint">{note}</p>}

      <ConfirmDialog
        open={confirming}
        title="Delete this recording?"
        body={deleteConfirmBody(rec)}
        confirmLabel="Delete recording"
        onConfirm={() => void confirmDelete()}
        onCancel={cancelDelete}
      />
    </li>
  );
}
