/**
 * Proof-drawer Files section (§9): download cards for report_files.
 * Downloads go through the frontend proxy (/api/reports/[filename]).
 */

import type { ReportFile } from '@/lib/types';
import { fileKind, formatBytes } from '@/lib/format';
import { IconDownload } from './icons';

export function FileCards({ files }: { files: ReportFile[] }) {
  return (
    <ul className="grid gap-2 sm:grid-cols-2">
      {files.map((f) => {
        const kind = fileKind(f.filename);
        return (
          <li key={f.filename}>
            <a
              href={`/api/reports/${encodeURIComponent(f.filename)}`}
              download={f.filename}
              className="group flex items-center gap-3 rounded-ts border border-border bg-surface p-3 no-underline transition-colors duration-ts hover:border-accent/50 hover:bg-surface-2"
            >
              <span
                aria-hidden
                className={`flex h-10 w-10 shrink-0 items-center justify-center rounded-lg font-mono text-[11px] font-semibold ${kind.className}`}
              >
                {kind.label}
              </span>
              <span className="min-w-0 flex-1">
                <span className="block truncate text-sm font-medium text-ink">
                  {f.filename}
                </span>
                <span className="block text-xs text-muted">
                  {f.type.toUpperCase()}
                  {f.size !== undefined ? ` · ${formatBytes(f.size)}` : ''}
                </span>
              </span>
              <IconDownload
                size={16}
                className="shrink-0 text-faint transition-colors duration-ts group-hover:text-accent"
              />
            </a>
          </li>
        );
      })}
    </ul>
  );
}
