'use client';

/**
 * ONE generated file, as a card in the thread (CONTRACT-2 §9).
 *
 * The unit is the FILE, not the version: "share XLSX, Word, PDF and CSV"
 * is four things a person can open and download separately, and one card
 * with four chips hid that behind a disclosure menu. Every card reads the
 * same way — a mark for the format, the title, one line of facts ("PDF ·
 * 14 KB · 2 pages") — whether the file came from Artifact Studio or from
 * an older engine's `report_files` (components/artifacts/legacyAdapter.ts).
 *
 * Two actions, kept apart on purpose. The card body opens the side panel;
 * the Download control is a SIBLING of that button, laid over its corner,
 * so a click on it never reaches onOpen and a click on the card never
 * starts a download. A button may not contain another interactive element,
 * which is what forces the sibling layout — and what makes the separation
 * testable: the two controls have different accessible names and different
 * DOM parents.
 *
 * A file with nothing to preview — a legacy report file, or one whose
 * preview the server could not render — has no panel to open, so its body
 * IS the download link and the copy says so. A click on a card that can
 * only download must not land in an empty panel.
 *
 * Width: the card fills the assistant's text column up to 680 px and no
 * further — cards that spanned the viewport next to a 768 px column of
 * prose looked like a different page pasted in.
 */

import type { KeyboardEvent, MouseEvent, ReactNode } from 'react';
import {
  fileCardDomId,
  fileDownloadUrl,
  fileExtent,
  fileKey,
  formatLabel,
  isLegacyKey,
  isPreviewable,
  previewKindFor,
  statusLabel,
} from '@/lib/artifacts';
import { fileKind, formatBytes } from '@/lib/format';
import type { ArtifactFile, ArtifactRef } from '@/lib/types';
import { IconAlert, IconDownload, IconForFormat } from '../icons';

export interface FileCardProps {
  file: ArtifactFile;
  /** The version the file belongs to — ids for the URLs, the preview kind, the title. */
  artifactRef: ArtifactRef;
  /** The group's title, used when the file does not carry its own. */
  groupTitle?: string;
  /**
   * Open the panel on this file. `originId` is the card control's DOM id,
   * so the panel can hand focus back to the exact card that opened it.
   * Never called for a file that cannot be previewed.
   */
  onOpen?: (fileKey: string, originId: string) => void;
  /** Is this the file the side panel is showing right now? */
  active?: boolean;
  /** The version's status; a chip is shown when it is not plain `completed`. */
  status?: string;
  /**
   * Which end of a long TITLE is kept. `start` clips from the left, for
   * names whose distinguishing part is at the end — the older engines'
   * `<source video>-<hash>-u<id>.transcript.vtt`, where a phone-named
   * source gave every file the same thirty-six leading characters
   * (owner screenshot, 2026-09-11).
   */
  clipName?: 'end' | 'start';
}

/** The one line of facts under the title: "PDF · 14 KB · 2 pages". */
export function fileMetaLine(file: ArtifactFile): string {
  const parts = [formatLabel(file.format)];
  if (typeof file.size === 'number' && Number.isFinite(file.size) && file.size > 0) {
    parts.push(formatBytes(file.size));
  }
  const extent = fileExtent(file);
  if (extent) parts.push(extent);
  return parts.join(' · ');
}

function stopCardClick(e: MouseEvent) {
  // The control is a sibling of the Open button, not a child, so this is
  // belt-and-braces: a portal or a future wrapper must still never turn a
  // download into an open.
  e.stopPropagation();
}

const BODY_CLASS =
  'flex w-full items-center gap-3 rounded-ts p-3 text-left no-underline focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/60';

export function FileCard({
  file,
  artifactRef,
  groupTitle,
  onOpen,
  active = false,
  status = artifactRef.status,
  clipName = 'end',
}: FileCardProps) {
  const key = fileKey(artifactRef, file);
  const domId = fileCardDomId(key);
  const title = file.title?.trim() || groupTitle?.trim() || artifactRef.title || file.filename;
  const label = formatLabel(file.format);
  const meta = fileMetaLine(file);
  const href = fileDownloadUrl(artifactRef, file, 'attachment');
  const previewable = isPreviewable(file, artifactRef);
  // A print or tabular format with no preview: the server rendered none
  // (the preview stage failed, or the version predates per-file previews
  // and its kind says `none`). The file itself is fine — say so.
  const previewUnavailable =
    !previewable && !isLegacyKey(file.file_id) && previewKindFor(file) !== 'none';
  const tint = fileKind(`x.${file.format}`).className;
  // The notes of a completed version are shown once, in the group header
  // ("Ready · 2 notes" and the list) — a chip repeating "With notes" on
  // every file under it said nothing four times (the 2026-09-12
  // screenshots). A chip is for a version that is NOT usable yet.
  const chip = status && status !== 'completed' && status !== 'completed_with_warnings' ? statusLabel(status) : null;

  function onKeyDown(e: KeyboardEvent<HTMLButtonElement>) {
    // A native button activates on Enter and Space by itself; handling the
    // keys here as well keeps the behaviour where a host wraps the button
    // in something that swallows the synthetic click. Opening twice is a
    // no-op, so the belt and the braces never disagree.
    if (e.key !== 'Enter' && e.key !== ' ') return;
    e.preventDefault();
    onOpen?.(key, domId);
  }

  const inner: ReactNode = (
    <>
      <span
        aria-hidden
        className={`flex h-9 w-9 shrink-0 items-center justify-center rounded-lg ${tint}`}
      >
        <IconForFormat format={file.format} size={18} />
      </span>
      <span className="min-w-0 flex-1">
        <span
          className={`block truncate text-sm font-medium text-ink ${
            clipName === 'start' ? '[direction:rtl]' : ''
          }`}
          title={file.filename}
        >
          {clipName === 'start' ? <span dir="ltr">{title}</span> : title}
        </span>
        <span className="mt-0.5 flex flex-wrap items-center gap-x-1.5 gap-y-0.5 text-xs text-muted">
          <span className="truncate" data-testid="file-card-meta">
            {meta}
          </span>
          {chip && (
            <span
              className="inline-flex items-center rounded border border-border bg-surface-2 px-1 text-[10.5px] font-medium text-faint"
              data-testid="file-card-status"
            >
              {chip}
            </span>
          )}
        </span>
        {previewUnavailable && (
          <span
            className="mt-1 flex items-center gap-1 text-[11.5px] text-warn"
            data-testid="file-card-preview-unavailable"
          >
            <IconAlert size={12} className="shrink-0" />
            Preview unavailable — download file
          </span>
        )}
      </span>
    </>
  );

  return (
    <div
      className={`file-card relative w-full max-w-[680px] rounded-ts border bg-surface transition-colors duration-ts ${
        active ? 'border-accent/60' : 'border-border hover:border-accent/50'
      }`}
      data-testid="file-card"
      data-file-key={key}
      data-format={file.format}
      data-active={active ? 'true' : undefined}
      data-status={status}
    >
      {previewable ? (
        <button
          id={domId}
          type="button"
          onClick={() => onOpen?.(key, domId)}
          onKeyDown={onKeyDown}
          aria-label={`Open ${title} (${label}, ${file.filename})`}
          // `aria-current`, not `aria-pressed`: Open is not a toggle —
          // pressing it again re-opens, it never closes.
          aria-current={active ? 'true' : undefined}
          className={`${BODY_CLASS} pr-14 sm:pr-32`}
        >
          {inner}
        </button>
      ) : href ? (
        <a
          id={domId}
          href={href}
          download={file.filename}
          aria-label={`Download ${file.filename}`}
          className={`group ${BODY_CLASS}`}
          data-testid="artifact-download"
        >
          {inner}
          <IconDownload
            size={16}
            className="shrink-0 text-faint transition-colors duration-ts group-hover:text-accent"
          />
        </a>
      ) : (
        // No URL could be built (a report name the proxy would refuse): the
        // facts are still shown, but nothing pretends to be a link.
        <div id={domId} className={BODY_CLASS} data-testid="artifact-file-unlinked">
          {inner}
        </div>
      )}
      {previewable && href && (
        <a
          href={href}
          download={file.filename}
          onClick={stopCardClick}
          aria-label={`Download ${file.filename}`}
          title={`Download ${file.filename}`}
          className="absolute right-3 top-1/2 inline-flex -translate-y-1/2 items-center gap-1 rounded-lg border border-border bg-surface px-2 py-1 text-xs font-medium text-muted transition-colors duration-ts hover:border-accent/50 hover:bg-surface-2 hover:text-ink focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/60"
          data-testid="artifact-download"
        >
          <IconDownload size={14} />
          <span className="hidden sm:inline">Download</span>
        </a>
      )}
    </div>
  );
}
