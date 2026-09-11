/**
 * Proof-drawer Files section (§9): the cards for `meta.report_files`.
 *
 * 2026-09-12 (CONTRACT-2 §9): rendered through the SAME FileCard as the
 * Artifact Studio files, by way of the legacy adapter. A report file has no
 * id, no preview and no panel, so its card body is the download link — the
 * whole card was a link before, and still is; what changed is that it now
 * looks like every other generated file in the thread. Downloads still go
 * through the frontend proxy (/api/reports/[filename]).
 *
 * The name is clipped from the LEFT. These names are `<source video>-
 * <hash>-u<id>.transcript.vtt`, and a video the person's phone named after
 * a UUID gives every card the same 36 leading characters — a plain
 * `truncate` cut off the only part that differed (owner screenshot,
 * 2026-09-11).
 */

import { fileKey } from '@/lib/artifacts';
import type { ReportFile } from '@/lib/types';
import { FileCard } from './artifacts/FileCard';
import { toArtifactRef } from './artifacts/legacyAdapter';

export function FileCards({ files }: { files: ReportFile[] }) {
  const group = toArtifactRef(files, 'proof');
  if (group.files.length === 0) return null;
  return (
    <ul className="flex w-full max-w-[680px] flex-col gap-2">
      {group.files.map((file) => (
        <li key={fileKey(group, file)}>
          <FileCard file={file} artifactRef={group} status="completed" clipName="start" />
        </li>
      ))}
    </ul>
  );
}
