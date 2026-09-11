'use client';

/**
 * One generated file (one artifact VERSION), as a card in the thread.
 *
 * Two actions, kept apart on purpose. The card body is ONE button — Open —
 * that hands the ref to the side panel; the Download control is a SIBLING
 * of that button, laid over its corner, so a click on it never reaches
 * onOpen and a click on the card never starts a download (the old FileCards
 * were download-only links, and "open" did not exist). A button may not
 * contain another interactive element, which is what forces the sibling
 * layout — and what makes the separation testable: the two controls have
 * different accessible names and different DOM parents.
 *
 * The status line is TRUTHFUL. While the job runs it names the stage the
 * server reports (useLiveArtifact polls it); it never shows a percentage,
 * because the pipeline has no way to know how far through "Writing the
 * content" it is and a bar that crawls to 90% and waits is a lie people
 * learn to distrust. Once terminal it names the outcome and the notes.
 */

import type { MouseEvent } from 'react';
import {
  apiUrl,
  cardDomId,
  fileExtent,
  isTerminal,
  kindLabel,
  primaryFile,
  statusLine,
} from '@/lib/artifacts';
import { fileKind, formatBytes } from '@/lib/format';
import type { ArtifactFile, ArtifactJob, ArtifactRef } from '@/lib/types';
import {
  IconAlert,
  IconChevronDown,
  IconDownload,
  IconFileText,
  IconGrid,
  IconPresentation,
} from '../icons';
import { Loader } from '../Loader';

/** The kind's mark, tinted like its badge. */
export function KindIcon({ kind, size = 18 }: { kind: string; size?: number }) {
  if (kind === 'presentation') return <IconPresentation size={size} />;
  if (kind === 'workbook') return <IconGrid size={size} />;
  return <IconFileText size={size} />;
}

function kindTint(kind: string): string {
  if (kind === 'presentation') return fileKind('x.pptx').className;
  if (kind === 'workbook') return fileKind('x.xlsx').className;
  return fileKind('x.docx').className;
}

/** "PDF · 3 pages · 120 KB" — one chip per file. */
function FileChip({ file }: { file: ArtifactFile }) {
  const kind = fileKind(file.filename || `x.${file.format}`);
  const extent = fileExtent(file);
  return (
    <span
      className="inline-flex items-center gap-1.5 rounded-md border border-border bg-surface-2 px-1.5 py-0.5 text-[11px] text-muted"
      title={file.filename}
    >
      <span className={`rounded px-1 font-mono text-[10px] font-semibold ${kind.className}`}>
        {kind.label}
      </span>
      {extent && <span>{extent}</span>}
      <span>{formatBytes(file.size)}</span>
    </span>
  );
}

function stopCardClick(e: MouseEvent) {
  // The control is a sibling of the Open button, not a child, so this is
  // belt-and-braces: a portal or a future wrapper must still never turn a
  // download into an open.
  e.stopPropagation();
}

/** The download control: a plain link for one file, a disclosure for several. */
export function DownloadControl({ artifact, files }: { artifact: ArtifactRef; files: ArtifactFile[] }) {
  if (files.length === 0) return null;
  const buttonClass =
    'inline-flex items-center gap-1 rounded-lg border border-border bg-surface px-2 py-1 text-xs font-medium text-muted transition-colors duration-ts hover:border-accent/50 hover:bg-surface-2 hover:text-ink';
  if (files.length === 1) {
    const f = files[0];
    return (
      <a
        href={apiUrl(f.download_url)}
        download={f.filename}
        onClick={stopCardClick}
        aria-label={`Download ${f.filename}`}
        title={`Download ${f.filename}`}
        className={buttonClass}
        data-testid="artifact-download"
      >
        <IconDownload size={14} />
        <span className="hidden sm:inline">Download</span>
      </a>
    );
  }
  return (
    <details className="relative" onClick={stopCardClick} data-testid="artifact-download">
      <summary
        className={`${buttonClass} list-none cursor-pointer [&::-webkit-details-marker]:hidden`}
        aria-label={`Download ${artifact.title} — choose a format`}
        title="Download — choose a format"
      >
        <IconDownload size={14} />
        <span className="hidden sm:inline">Download</span>
        <IconChevronDown size={12} />
      </summary>
      <ul
        role="list"
        className="absolute right-0 z-20 mt-1 min-w-[200px] overflow-hidden rounded-ts border border-border bg-surface py-1 shadow-xl"
      >
        {files.map((f) => {
          const kind = fileKind(f.filename || `x.${f.format}`);
          return (
            <li key={f.format}>
              <a
                href={apiUrl(f.download_url)}
                download={f.filename}
                aria-label={`Download ${f.filename}`}
                className="flex items-center gap-2 px-3 py-1.5 text-xs text-ink no-underline transition-colors duration-ts hover:bg-surface-2"
              >
                <span className={`rounded px-1 font-mono text-[10px] font-semibold ${kind.className}`}>
                  {kind.label}
                </span>
                <span className="min-w-0 flex-1 truncate">{f.filename}</span>
                <span className="shrink-0 text-faint">{formatBytes(f.size)}</span>
              </a>
            </li>
          );
        })}
      </ul>
    </details>
  );
}

export function ArtifactCard({
  artifact,
  job = null,
  error = null,
  active = false,
  onOpen,
}: {
  artifact: ArtifactRef;
  /** The last polled job answer, when the ref is not yet terminal. */
  job?: ArtifactJob | null;
  /** A settled refusal from polling, shown under the status line. */
  error?: string | null;
  /** Is this the version the side panel is showing right now? */
  active?: boolean;
  /**
   * Open the side panel on this version. `originId` is the card button's DOM
   * id, so the panel can return focus to the exact card that opened it.
   */
  onOpen: (artifact: ArtifactRef, originId: string) => void;
}) {
  const status = job?.status ?? artifact.status;
  const terminal = isTerminal(status);
  const working = status === 'queued' || status === 'running';
  const failed = status === 'failed' || status === 'cancelled';
  const files = terminal && !failed ? artifact.files ?? [] : [];
  const primary = primaryFile(artifact);
  const domId = cardDomId(artifact.artifact_id, artifact.version);
  const line = statusLine(artifact, job);
  const versionText =
    artifact.version > 1 || artifact.operation !== 'create'
      ? `v${artifact.version}${artifact.operation === 'edit' ? ' · edited' : artifact.operation === 'convert' ? ' · converted' : ''}`
      : 'v1';

  return (
    <div
      className={`relative rounded-ts border bg-surface transition-colors duration-ts ${
        active ? 'border-accent/60' : 'border-border hover:border-accent/50'
      }`}
      data-testid="artifact-card"
      data-status={status}
    >
      <button
        id={domId}
        type="button"
        onClick={() => onOpen(artifact, domId)}
        aria-label={`Open ${artifact.title} (${kindLabel(artifact.kind)}, ${versionText})`}
        // `aria-current`, not `aria-pressed`: Open is not a toggle — pressing
        // it again re-opens, it never closes — and a screen reader that hears
        // "pressed" expects a second press to unpress. "Current" says exactly
        // what is true: this is the version the panel is showing.
        aria-current={active ? 'true' : undefined}
        className="flex w-full items-start gap-3 rounded-ts p-3 text-left focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/60"
      >
        <span
          aria-hidden
          className={`flex h-10 w-10 shrink-0 items-center justify-center rounded-lg ${kindTint(artifact.kind)}`}
        >
          <KindIcon kind={artifact.kind} />
        </span>
        <span className="min-w-0 flex-1 pr-24 sm:pr-28">
          <span className="block truncate text-sm font-medium text-ink" title={artifact.title}>
            {artifact.title}
          </span>
          <span className="mt-0.5 block truncate text-xs text-muted">
            {kindLabel(artifact.kind)} · {versionText}
            {primary && files.length > 0 ? (
              <>
                {' · '}
                <span className="font-mono text-[11px]" title={primary.filename}>
                  {primary.filename}
                </span>
              </>
            ) : null}
          </span>
          {files.length > 0 && (
            <span className="mt-2 flex flex-wrap gap-1.5">
              {files.map((f) => (
                <FileChip key={f.format} file={f} />
              ))}
            </span>
          )}
          <span
            className={`mt-2 flex items-center gap-1.5 text-xs ${
              failed ? 'text-danger' : working ? 'text-muted' : 'text-faint'
            }`}
            data-testid="artifact-status"
          >
            {working ? (
              <Loader size={13} label="Generating" />
            ) : failed ? (
              <IconAlert size={13} className="shrink-0" />
            ) : (
              <span aria-hidden className="h-1.5 w-1.5 shrink-0 rounded-full bg-ok" />
            )}
            <span className="min-w-0 truncate">{line}</span>
          </span>
          {error && (
            <span className="mt-1 block text-xs text-danger" role="status">
              {error}
            </span>
          )}
          {artifact.warnings && artifact.warnings.length > 0 && terminal && (
            <span className="mt-1.5 block space-y-0.5" data-testid="artifact-warnings">
              {artifact.warnings.map((w, i) => (
                <span
                  key={`${i}-${w}`}
                  className="flex items-start gap-1.5 text-[11.5px] leading-snug text-warn"
                >
                  <IconAlert size={12} className="mt-0.5 shrink-0" />
                  <span>{w}</span>
                </span>
              ))}
            </span>
          )}
        </span>
      </button>
      {files.length > 0 && (
        <div className="absolute right-3 top-3">
          <DownloadControl artifact={artifact} files={files} />
        </div>
      )}
    </div>
  );
}
