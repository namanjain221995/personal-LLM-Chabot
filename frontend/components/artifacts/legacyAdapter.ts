/**
 * `meta.report_files` → an ArtifactRef, so the older engines' files render
 * through the same FileCard as Artifact Studio's (CONTRACT-2 §9).
 *
 * Two file namespaces reach the browser: `meta.artifacts` (owner-scoped,
 * `/api/artifacts/...`, with ids, previews and a panel) and the older
 * `meta.report_files` written by the dataset, SQL, video and report engines
 * (a flat REPORTS_DIR served by `/api/reports/{filename}`, no ids, no
 * previews). Two card components for the same idea — "a file this answer
 * produced" — meant two layouts, two download controls and two sets of
 * tests drifting apart. This adapter is the bridge: it says what a report
 * file IS in the artifact vocabulary and lets everything downstream forget
 * the difference.
 *
 * What it does NOT do: invent. There is no artifact id (a `legacy-<message>`
 * placeholder fails the 32-hex gate on purpose, so no /artifacts URL can
 * ever be built from it), no job, no preview (`preview_url: ''` — the card
 * body downloads), and the file id is `legacy:<filename>`, which the URL
 * builders refuse and only the card key accepts.
 */

import { LEGACY_KEY_PREFIX, reportFileUrl } from '@/lib/artifacts';
import type { ArtifactFile, ArtifactRef, ReportFile } from '@/lib/types';

/** MIME by suffix, for the field's sake — nothing in the browser reads it. */
const MIME: Record<string, string> = {
  pdf: 'application/pdf',
  docx: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  pptx: 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
  xlsx: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
  csv: 'text/csv; charset=utf-8',
  txt: 'text/plain; charset=utf-8',
  md: 'text/markdown; charset=utf-8',
  json: 'application/json',
  srt: 'application/x-subrip',
  vtt: 'text/vtt',
};

/** `report.pdf` → `pdf`; `README` → ''. Lower-cased, for display and the icon map only. */
export function legacyFormat(filename: string, declared?: string): string {
  const dot = filename.lastIndexOf('.');
  const suffix = dot > 0 && dot < filename.length - 1 ? filename.slice(dot + 1) : '';
  return (suffix || declared || '').toLowerCase();
}

/** `pipeline-review-2026-07-22.docx` → `pipeline-review-2026-07-22`. */
export function legacyTitle(filename: string): string {
  const dot = filename.lastIndexOf('.');
  return dot > 0 ? filename.slice(0, dot) : filename;
}

export function toArtifactFile(file: ReportFile): ArtifactFile {
  const format = legacyFormat(file.filename, file.type);
  return {
    file_id: `${LEGACY_KEY_PREFIX}${file.filename}`,
    role: 'primary',
    format,
    filename: file.filename,
    title: legacyTitle(file.filename),
    mime_type: MIME[format] ?? 'application/octet-stream',
    size: typeof file.size === 'number' ? file.size : 0,
    download_url: reportFileUrl(file.filename),
    inline_url: '',
    // No preview exists for a report file, and saying so per file is what
    // makes the card body a download (lib/artifacts.ts isPreviewable).
    preview_url: '',
  };
}

/**
 * One group per message. Every field a card or a panel could read is
 * present and truthful: `completed` (the file exists or the engine would
 * not have listed it), no warnings, no job to poll.
 */
export function toArtifactRef(reportFiles: ReportFile[], messageId: string): ArtifactRef {
  const files = (reportFiles ?? []).filter(
    (f): f is ReportFile => Boolean(f) && typeof f.filename === 'string' && f.filename.length > 0,
  );
  return {
    artifact_id: `legacy-${messageId}`,
    version: 1,
    job_id: '',
    title: files.length === 1 ? legacyTitle(files[0].filename) : 'Files',
    kind: 'document',
    status: 'completed',
    files: files.map(toArtifactFile),
    preview_kind: 'none',
    preview_pages: 0,
    preview_url: '',
    thumbnail_url: '',
    warnings: [],
    created_at: '',
    operation: 'create',
    status_url: '',
  };
}
