'use client';

/**
 * One artifact VERSION, as a GROUP in the thread (CONTRACT-2 §9): a header
 * — the kind's mark, the title, "v2 · Updated", the truthful status line,
 * the notes, and "Download all" when there are two or more files — over
 * ONE FileCard PER FILE.
 *
 * Until 2026-09-12 this was the card: one per version, its files as chips
 * and a download disclosure. A version that is four files ("share XLSX,
 * Word, PDF and CSV") is four things to open and four to download, so the
 * card moved down a level (FileCard.tsx) and this became the frame around
 * them. The exports other modules imported — `ArtifactCard`, `KindIcon` —
 * keep their names.
 *
 * The status line is TRUTHFUL. While the job runs it names the stage the
 * server reports (useLiveArtifact polls it); it never shows a percentage,
 * because the pipeline has no way to know how far through "Writing the
 * content" it is and a bar that crawls to 90% and waits is a lie people
 * learn to distrust. Once terminal it names the outcome and the notes —
 * once, here, not on every file card under it.
 */

import { artifactUrls, fileKey, isTerminal, kindLabel, statusLine } from '@/lib/artifacts';
import { fileKind } from '@/lib/format';
import type { ArtifactJob, ArtifactRef } from '@/lib/types';
import { IconAlert, IconFileText, IconGrid, IconPackage, IconPresentation } from '../icons';
import { Loader } from '../Loader';
import { FileCard } from './FileCard';

/** Open the panel on one file of one version. `originId` is the card control's DOM id. */
export type OpenFile = (artifact: ArtifactRef, originId: string, fileKey: string) => void;

/** The kind's mark, tinted like its badge. */
export function KindIcon({ kind, size = 18 }: { kind: string; size?: number }) {
  if (kind === 'presentation') return <IconPresentation size={size} />;
  if (kind === 'workbook') return <IconGrid size={size} />;
  return <IconFileText size={size} />;
}

export function kindTint(kind: string): string {
  if (kind === 'presentation') return fileKind('x.pptx').className;
  if (kind === 'workbook') return fileKind('x.xlsx').className;
  return fileKind('x.docx').className;
}

/** "v1 · Created" · "v2 · Updated" · "v2 · Converted" — the version and how it came to be. */
export function versionText(artifact: Pick<ArtifactRef, 'version' | 'operation'>): string {
  const how =
    artifact.operation === 'edit'
      ? 'Updated'
      : artifact.operation === 'convert'
        ? 'Converted'
        : 'Created';
  return `v${Math.trunc(artifact.version)} · ${how}`;
}

export function ArtifactCard({
  artifact,
  job = null,
  error = null,
  activeKey = null,
  onOpen,
}: {
  artifact: ArtifactRef;
  /** The last polled job answer, when the ref is not yet terminal. */
  job?: ArtifactJob | null;
  /** A settled refusal from polling, shown under the status line. */
  error?: string | null;
  /** The key (lib/artifacts.ts fileKey) of the file the panel is showing. */
  activeKey?: string | null;
  onOpen: OpenFile;
}) {
  const status = job?.status ?? artifact.status;
  const terminal = isTerminal(status);
  const working = status === 'queued' || status === 'running';
  const failed = status === 'failed' || status === 'cancelled';
  const files = terminal && !failed ? (artifact.files ?? []) : [];
  const line = statusLine(artifact, job);
  // Offered only when the server offers it (the field is the signal) and
  // there is more than one file to bundle; the URL itself is rebuilt from
  // the validated ids, like every other one.
  const zipHref =
    files.length >= 2 && typeof artifact.download_all_url === 'string' && artifact.download_all_url
      ? artifactUrls.zip(artifact.artifact_id, artifact.version)
      : '';
  const zipCount = artifact.package?.count ?? files.length;

  return (
    <div
      className="flex w-full max-w-[680px] flex-col gap-2"
      data-testid="artifact-card"
      data-status={status}
    >
      <div className="flex items-start gap-2.5 px-0.5">
        <span
          aria-hidden
          className={`flex h-8 w-8 shrink-0 items-center justify-center rounded-lg ${kindTint(artifact.kind)}`}
        >
          <KindIcon kind={artifact.kind} size={16} />
        </span>
        <div className="min-w-0 flex-1">
          <p className="truncate text-sm font-medium text-ink" title={artifact.title}>
            {artifact.title}
          </p>
          <p className="truncate text-xs text-muted" data-testid="artifact-version">
            {kindLabel(artifact.kind)} · {versionText(artifact)}
          </p>
          <p
            className={`mt-1 flex items-center gap-1.5 text-xs ${
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
          </p>
          {error && (
            <p className="mt-1 text-xs text-danger" role="status">
              {error}
            </p>
          )}
          {artifact.warnings && artifact.warnings.length > 0 && terminal && (
            <ul className="mt-1.5 space-y-0.5" data-testid="artifact-warnings">
              {artifact.warnings.map((w, i) => (
                <li
                  key={`${i}-${w}`}
                  className="flex items-start gap-1.5 text-[11.5px] leading-snug text-warn"
                >
                  <IconAlert size={12} className="mt-0.5 shrink-0" />
                  <span>{w}</span>
                </li>
              ))}
            </ul>
          )}
        </div>
        {!files.length && (working || failed) && (
          // No file cards yet (or ever): the header itself is the way into
          // the panel's status view — the stage while it runs, the
          // failure and its retry once it has not. Without this a running
          // version had nothing to click (the 2026-09-12 review).
          <button
            type="button"
            id={`artifact-status-${artifact.artifact_id}-v${Math.trunc(artifact.version)}`}
            onClick={(e) => onOpen(artifact, (e.currentTarget as HTMLButtonElement).id, '')}
            aria-label={`${working ? 'Show progress of' : 'Show details of'} ${artifact.title}`}
            className="inline-flex shrink-0 items-center gap-1 rounded-lg border border-border bg-surface px-2 py-1 text-xs font-medium text-muted transition-colors duration-ts hover:border-accent/50 hover:bg-surface-2 hover:text-ink focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/60"
            data-testid="artifact-open-status"
          >
            {working ? 'Progress' : 'Details'}
          </button>
        )}
        {zipHref && (
          <a
            href={zipHref}
            download
            aria-label={`Download all ${zipCount} files as ZIP`}
            title={`Download all ${zipCount} files as ZIP`}
            className="inline-flex shrink-0 items-center gap-1 rounded-lg border border-border bg-surface px-2 py-1 text-xs font-medium text-muted transition-colors duration-ts hover:border-accent/50 hover:bg-surface-2 hover:text-ink focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/60"
            data-testid="artifact-download-all"
          >
            <IconPackage size={14} />
            <span>Download all</span>
          </a>
        )}
      </div>
      {files.length > 0 && (
        <ul className="flex flex-col gap-2" aria-label={`Files of ${artifact.title}`}>
          {files.map((file) => {
            const key = fileKey(artifact, file);
            return (
              <li key={key}>
                <FileCard
                  file={file}
                  artifactRef={artifact}
                  groupTitle={artifact.title}
                  status={status}
                  active={activeKey === key}
                  onOpen={(k, originId) => onOpen(artifact, originId, k)}
                />
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
