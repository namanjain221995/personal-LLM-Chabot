'use client';

/**
 * One file on the "My files" page: what it is, when it arrived, which chat
 * it belongs to, what of it is still stored, and only the actions that state
 * can back.
 *
 * EVERY ACTION GOES THROUGH A ROUTE THAT ALREADY CHECKS OWNERSHIP. Download is
 * a plain link to the streaming proxies the chat uses (a 4 GB video must never
 * be buffered by a JSON proxy); Preview reuses the chat's attachment dialog
 * and its loaders, which keep its allowlist — an uploaded HTML or SVG file is
 * never drawn, whatever it is called; Delete exists for recordings only, the
 * one kind a person can already delete on its own (a chat's files go with the
 * chat), and calls the same DELETE the Recordings page does.
 *
 * NOTHING IS FETCHED BY RENDERING. The player is preload="none" (an hour of
 * dictation is ~58 MB) and a preview asks the server only when opened.
 */

import { useCallback, useEffect, useId, useRef, useState, type ReactNode } from 'react';
import { AttachmentPreview, type ServerPreviewLoaders } from '@/components/AttachmentPreview';
import { ConfirmDialog } from '@/components/ConfirmDialog';
import {
  IconDownload,
  IconForFormat,
  IconMic,
  IconTrash,
  IconZoomIn,
} from '@/components/icons';
import { fetchUploadBlob, previewKindFor, previewMimeFor, type ResolvedAttachment } from '@/lib/attachments';
import { fileKind, formatBytes, formatWhen } from '@/lib/format';
import {
  AVAILABILITY_LABEL,
  KIND_LABEL,
  availabilityNote,
  chatUrl,
  downloadUrl,
  previewPlanFor,
  rowDomId,
  summaryFromProfile,
  type Availability,
  type MyFile,
  type Retention,
} from '@/lib/myfiles';
import { fetchDocumentText, fetchUploadProfile } from '@/lib/previewData';
import { deleteRecording } from '@/lib/recordings';
import { formatElapsed } from '@/lib/voice';

/**
 * The row's two text links ("In chat …", "Also in Recordings") sit in a line
 * of 12 px text, 17 px tall. On a phone or any coarse pointer they become a
 * 44 px row of their own (the line grows, nothing overlaps); with a mouse
 * they stay inline. Measured at 390x844 in the 2026-09-30 end-to-end run.
 */
const TOUCH_LINK =
  'max-sm:inline-flex max-sm:min-h-11 max-sm:items-center [@media(pointer:coarse)]:inline-flex [@media(pointer:coarse)]:min-h-11 [@media(pointer:coarse)]:items-center';

/** 44 px tall: every control on this page is a comfortable touch target. */
export const myFilesActionClass =
  'inline-flex min-h-11 items-center gap-1.5 rounded-lg border border-border bg-surface px-3 text-sm text-muted transition-colors duration-ts hover:bg-surface-2 hover:text-ink focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:cursor-not-allowed disabled:opacity-60';

const MARK: Record<Availability, { dot: string; text: string }> = {
  available: { dot: 'bg-ok', text: 'text-muted' },
  text_only: { dot: 'bg-accent', text: 'text-muted' },
  summary_only: { dot: 'bg-accent', text: 'text-muted' },
  processing: { dot: 'bg-warn', text: 'text-muted' },
  expired: { dot: 'bg-faint', text: 'text-faint' },
};

/** Dot and word: the colour is never the only thing saying it. */
function AvailabilityMark({ availability }: { availability: Availability }) {
  const style = MARK[availability];
  return (
    <span className={`inline-flex shrink-0 items-center gap-2 text-xs font-medium ${style.text}`}>
      <span aria-hidden className={`h-1.5 w-1.5 shrink-0 rounded-full ${style.dot}`} />
      <span className="sr-only">Status: </span>
      {AVAILABILITY_LABEL[availability]}
    </span>
  );
}

/** A strip of film — a video. Same 24-grid and stroke as components/icons. */
function IconFilm({ size = 18 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <rect x="3" y="4" width="18" height="16" rx="2" />
      <path d="M7 4v16M17 4v16M3 9h4M3 15h4M17 9h4M17 15h4" />
    </svg>
  );
}

/** A sound wave — an audio file (a recording keeps the microphone). */
function IconWave({ size = 18 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <path d="M3 12h2M7 8v8M11 5v14M15 9v6M19 7v10M21 12h0" />
    </svg>
  );
}

function extensionOf(name: string): string {
  const m = /\.([a-z0-9]{1,5})$/i.exec(name.trim());
  return m ? m[1]!.toLowerCase() : '';
}

function KindTile({ file }: { file: MyFile }) {
  let tint = 'file-icon-other';
  let icon: ReactNode;
  if (file.kind === 'recording') {
    tint = 'bg-accent/15 text-accent';
    icon = <IconMic size={18} />;
  } else if (file.kind === 'video') {
    icon = <IconFilm />;
  } else if (file.kind === 'audio') {
    icon = <IconWave />;
  } else {
    const ext = extensionOf(file.name);
    tint = fileKind(file.name).className;
    let format = ext;
    if (file.kind === 'dataset') {
      // A dataset is a table unless it is a workbook or a package of files.
      format = ext === 'xlsx' ? 'xlsx' : ['zip', 'tar', 'tgz', 'gz'].includes(ext) ? 'zip' : 'csv';
    }
    icon = <IconForFormat format={format} size={18} />;
  }
  return (
    <span aria-hidden className={`flex h-10 w-10 shrink-0 items-center justify-center rounded-lg ${tint}`}>
      {icon}
    </span>
  );
}

/** What the name of a row is, in sentences: a recording is named by its time. */
export function describe(file: MyFile): string {
  return file.kind === 'recording' ? `the voice recording from ${formatWhen(file.createdAt)}` : file.name;
}

interface MyFileRowProps {
  file: MyFile;
  retention: Retention | null;
  fetchFn: typeof fetch;
  /** The server no longer holds the recording (204 or 404 on delete). */
  onDeleted: (file: MyFile) => void;
  /** A preview found the bytes gone (410): the row should say so. */
  onExpired: (file: MyFile) => void;
}

export function MyFileRow({ file, retention, fetchFn, onDeleted, onExpired }: MyFileRowProps) {
  const baseId = useId();
  const titleId = rowDomId(file);
  const previewRef = useRef<HTMLButtonElement>(null);
  const deleteRef = useRef<HTMLButtonElement>(null);
  const previewAbort = useRef<AbortController | null>(null);

  const [source, setSource] = useState<ResolvedAttachment | null>(null);
  const [loaders, setLoaders] = useState<ServerPreviewLoaders>({});
  const [confirming, setConfirming] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);

  useEffect(() => () => previewAbort.current?.abort(), []);

  const plan = previewPlanFor(file);
  const href = downloadUrl(file);
  const note = availabilityNote(file, retention);
  const when = formatWhen(file.createdAt);
  const what = describe(file);
  const duration = file.media?.durationMs ? formatElapsed(file.media.durationMs) : null;

  function openPreview() {
    if (!plan || plan.kind === 'audio') return;
    const blank: ResolvedAttachment = { name: file.name, mime: '', blob: null, size: file.bytes, kind: 'none' };
    if (plan.kind === 'text') {
      setLoaders({ loadDocumentText: (signal) => fetchDocumentText(plan.conversationId, plan.name, signal) });
      setSource(blank);
      return;
    }
    if (plan.kind === 'summary') {
      setLoaders({
        loadWorkbook: async (signal) => {
          const found = await fetchUploadProfile(plan.conversationId, plan.uploadId, signal);
          return found ? summaryFromProfile(found.profile, found.filename || file.name) : null;
        },
      });
      setSource(blank);
      return;
    }
    // The bytes: downloaded once, when asked, and aborted if the dialog closes.
    setLoaders({});
    setSource({ ...blank, kind: 'loading' });
    previewAbort.current?.abort();
    const controller = new AbortController();
    previewAbort.current = controller;
    void fetchUploadBlob(plan.ref, controller.signal).then((outcome) => {
      if (controller.signal.aborted) return;
      if (outcome.status === 'expired') {
        setSource({ ...blank, kind: 'expired' });
        onExpired(file);
        return;
      }
      if (outcome.status !== 'ok') {
        setSource({ ...blank, kind: 'unavailable' });
        return;
      }
      // The renderer is chosen from the NAME by the dialog's allowlist, never
      // from the type the server (or the uploader) claimed.
      const mime = previewMimeFor(file.name, outcome.blob.type) ?? outcome.blob.type;
      setSource({
        name: file.name,
        mime,
        blob: outcome.blob,
        size: outcome.blob.size,
        kind: previewKindFor(file.name, mime),
      });
    });
  }

  function closePreview() {
    previewAbort.current?.abort();
    setSource(null);
    previewRef.current?.focus();
  }

  // Stable: ConfirmDialog re-runs its focus effect when this changes.
  const cancelDelete = useCallback(() => {
    setConfirming(false);
    deleteRef.current?.focus();
  }, []);

  async function confirmDelete() {
    if (!file.recordingId) return;
    setConfirming(false);
    setDeleting(true);
    setDeleteError(null);
    const result = await deleteRecording(fetchFn, file.recordingId);
    if (result.kind === 'deleted') {
      onDeleted(file);
      return;
    }
    setDeleting(false);
    setDeleteError(result.message);
  }

  useEffect(() => {
    if (deleteError) deleteRef.current?.focus();
  }, [deleteError]);

  return (
    <li className="px-4 py-4 sm:px-5" aria-labelledby={titleId} aria-busy={deleting || undefined}>
      <div className="flex gap-3">
        <KindTile file={file} />
        <div className="min-w-0 flex-1">
          <div className="flex items-start justify-between gap-3">
            <h2
              id={titleId}
              tabIndex={-1}
              className="min-w-0 rounded text-[15px] font-medium leading-[22px] text-ink [overflow-wrap:anywhere] focus:outline-none focus-visible:ring-2 focus-visible:ring-accent"
            >
              {file.name}
            </h2>
            <span className="pt-0.5">
              <AvailabilityMark availability={file.availability} />
            </span>
          </div>

          <p className="mt-0.5 flex flex-wrap gap-x-3 text-xs tabular-nums text-muted">
            <span>{KIND_LABEL[file.kind]}</span>
            {file.bytes !== null && file.bytes > 0 && (
              <span>
                <span className="sr-only">Size: </span>
                {formatBytes(file.bytes)}
              </span>
            )}
            {duration && (
              <span>
                <span className="sr-only">Length: </span>
                {duration}
              </span>
            )}
            <span>
              <span className="sr-only">Added </span>
              <time dateTime={file.createdAt}>{when}</time>
            </span>
          </p>

          <p className="mt-1 text-xs text-muted">
            {file.conversation ? (
              <>
                In chat{' '}
                <a
                  href={chatUrl(file.conversation.id)}
                  className={`rounded font-medium text-accent underline-offset-2 [overflow-wrap:anywhere] hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent ${TOUCH_LINK}`}
                >
                  <span className="sr-only">Open the chat </span>
                  {file.conversation.title || 'Untitled chat'}
                </a>
              </>
            ) : (
              <a
                href="/recordings"
                className={`rounded font-medium text-accent underline-offset-2 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent ${TOUCH_LINK}`}
              >
                Also in Recordings, with its transcript
              </a>
            )}
          </p>

          {note && <p className="mt-2 text-xs leading-relaxed text-muted">{note}</p>}

          {plan?.kind === 'audio' && (
            <audio
              controls
              preload="none"
              src={plan.url}
              aria-label={`Play ${what}`}
              className="mt-3 block h-10 w-full"
            />
          )}

          <div className="mt-3 flex flex-wrap items-center gap-2">
            {plan && plan.kind !== 'audio' && (
              <button ref={previewRef} type="button" onClick={openPreview} className={myFilesActionClass}>
                <IconZoomIn size={14} />
                Preview
                <span className="sr-only"> {what}</span>
              </button>
            )}
            {href && (
              <a href={href} download className={myFilesActionClass}>
                <IconDownload size={14} />
                Download
                <span className="sr-only"> {what}</span>
              </a>
            )}
            {file.can.delete && file.recordingId ? (
              <button
                ref={deleteRef}
                type="button"
                onClick={() => {
                  setDeleteError(null);
                  setConfirming(true);
                }}
                disabled={deleting}
                aria-describedby={deleteError ? `${baseId}-delete-error` : undefined}
                // At the far end, away from the harmless actions (the
                // Recordings page's rule, for the same thumb).
                className={`${myFilesActionClass} ml-auto hover:border-danger/50 hover:text-danger`}
              >
                <IconTrash size={14} />
                {deleting ? 'Deleting…' : 'Delete'}
                <span className="sr-only"> {what}</span>
              </button>
            ) : (
              file.conversation && (
                <p className="ml-auto text-xs text-faint">To remove it, delete its chat.</p>
              )
            )}
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
        </div>
      </div>

      {source && <AttachmentPreview source={source} onClose={closePreview} {...loaders} />}

      <ConfirmDialog
        open={confirming}
        title="Delete this recording?"
        body={`The recording from ${when} and its transcript will be deleted from the server. This can't be undone.`}
        confirmLabel="Delete recording"
        onConfirm={() => void confirmDelete()}
        onCancel={cancelDelete}
      />
    </li>
  );
}
